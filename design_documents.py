"""Design documents on the server (TRACEABILITY_DESIGN.md §5.1; kittrial-5bb.418, part 1).

A `design.<scope>` reference entry carries the document it selects. Its authority is
`{type: "repo-document", path, commit, anchor?, sha256}`, written in a `reference-entry-v3`
record, so a kit before this one reads such an entry `unsupported` and never misreads it
(reference_records.unsupported_reason). The proposer sends the document's bytes exactly as
they are at the pinned commit; the server:

1. checks them: strict UTF-8, no NUL, at most DOCUMENT_MAX_BYTES, a `design-v1` front block
   whose `scope` is the key's scope, at most HEADINGS_MAX headings and TOKENS_MAX `req:` and
   `design:` tokens, and an index of at most INDEX_MAX_BYTES once encoded;
2. stores them in the project's content-addressed task-files store, journaled as a task-file
   entry of direction `design` on the entry's anchor, so backup, restore, verify and the
   project's byte limit count it and purge never takes it for an orphan or a closed task's file;
3. derives the index itself and writes it as one reserved `Kind: design-index-v1` record on the
   anchor. The index is a function of the bytes, the key, the path and the commit only, so two
   revisions that pin the same document share it and an accepted revision keeps its own.

Acceptance of a `repo-document` revision (reference_records.acceptance_rules) re-reads the
stored bytes and checks their sha256 against the authority, that exactly the index derived from
them is on the anchor (no other index for the same document), and the front block's scope.
"""
import hashlib
import io
import re

from export_requirements import parse_json
from requirements import canonical_bytes, content_hash

DESIGN_KEY_PREFIX = 'design.'
AUTHORITY_TYPE = 'repo-document'
DOCUMENT_MAX_BYTES = 1024 * 1024
HEADINGS_MAX = 400
TOKENS_MAX = 2000
#: The index is one native comment, passed to bd as one argument: Linux refuses a single
#: argument over 128 KiB, so the encoded index stays well below that.
INDEX_MAX_BYTES = 100_000
TITLE_MAX = 200
FRONT_ITEMS_MAX = 64
FRONT_ITEM_MAX = 200
PHASE_LINES_MAX = 20
PHASE_LINE_MAX = 300
INDEX_PREFIX = 'Kind: design-index-v1\n'
MEDIA_TYPE = 'text/markdown'
FRONT_STATUSES = ('candidate', 'selected', 'superseded', 'rejected')
FRONT_LISTS = ('requirements', 'decisions-open', 'alternatives', 'supersedes')
SCOPE = re.compile(r'[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*')
SHA256 = re.compile(r'[0-9a-f]{64}')
ISSUE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}')
FRONT_BLOCK = re.compile(r'\A﻿?[ \t\r\n]*<!--[ \t]*design-v1[ \t]*\r?\n(.*?)-->', re.S)
FRONT_LINE = re.compile(r'([a-z][a-z-]*)[ \t]*:[ \t]*(.*)')
PLAN_PHASE = re.compile(r'plan-phase-[0-9]{1,4}')
PHASE_FIELD = re.compile(r'[ \t]*(?:[-*+][ \t]+)?(?:\*\*)?(contract|test[ -]oracles?|requirements|epic)(?:\*\*)?'
                         r'[ \t]*:[ \t]*(?:\*\*)?[ \t]*(.*)', re.I)
INDEX_FIELDS = ('schema_version', 'key', 'doc_sha256', 'bytes', 'path', 'commit', 'front', 'headings',
                'plan_phases', 'sha256')
FRONT_FIELDS = ('scope', 'status', 'requirements', 'decisions_open', 'alternatives', 'supersedes')


def is_design_key(key):
    return isinstance(key, str) and key.startswith(DESIGN_KEY_PREFIX)


def scope_of(key):
    return key[len(DESIGN_KEY_PREFIX):]


def is_document_authority(authority):
    return isinstance(authority, dict) and authority.get('type') == AUTHORITY_TYPE


def document_bytes(text):
    """The exact bytes of a document sent as text, or ValueError naming the bound it breaks."""
    if not isinstance(text, str):
        raise ValueError('document must be the Markdown text of the design document')
    if '\x00' in text:
        raise ValueError('document must not contain a NUL character')
    try:
        data = text.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('document must be valid UTF-8 text') from None
    if not data:
        raise ValueError('document must not be empty')
    if len(data) > DOCUMENT_MAX_BYTES:
        raise ValueError('document: %d bytes, the limit is %d (1 MiB)' % (len(data), DOCUMENT_MAX_BYTES))
    return data


def _front_list(value, name):
    value = value.strip()
    if not (value.startswith('[') and value.endswith(']')):
        raise ValueError('design front block: %s must be a [list]' % name)
    inner = value[1:-1].strip()
    items = [item.strip() for item in inner.split(',')] if inner else []
    if len(items) > FRONT_ITEMS_MAX:
        raise ValueError('design front block: %s has %d items, the limit is %d' % (name, len(items), FRONT_ITEMS_MAX))
    for item in items:
        if not item or len(item) > FRONT_ITEM_MAX or any(ord(ch) < 32 or ch in '[]' for ch in item):
            raise ValueError('design front block: every %s item is 1..%d characters of one line' % (name, FRONT_ITEM_MAX))
    return items


def front_block(text):
    """The `<!-- design-v1 ... -->` block that opens the document (§2.1), parsed strictly."""
    match = FRONT_BLOCK.match(text)
    if not match:
        raise ValueError('the document must open with a <!-- design-v1 ... --> front block (TRACEABILITY_DESIGN §2.1)')
    seen = {}
    for raw in match.group(1).splitlines():
        line = re.sub(r'(?:^|[ \t])#.*$', '', raw).strip()
        if not line:
            continue
        parsed = FRONT_LINE.fullmatch(line)
        if not parsed:
            raise ValueError('design front block: every line is "name: value"')
        name, value = parsed.group(1), parsed.group(2).strip()
        if name not in ('scope', 'status') + FRONT_LISTS:
            raise ValueError('design front block: unknown field %s' % name)
        if name in seen:
            raise ValueError('design front block: %s is given twice' % name)
        seen[name] = value
    scope, status = seen.get('scope'), seen.get('status')
    if not isinstance(scope, str) or not SCOPE.fullmatch(scope):
        raise ValueError('design front block: scope must be the design scope (lowercase, digits, hyphens, dots)')
    if status not in FRONT_STATUSES:
        raise ValueError('design front block: status must be candidate, selected, superseded or rejected')
    front = {'scope': scope, 'status': status}
    for name in FRONT_LISTS:
        front[name.replace('-', '_')] = _front_list(seen[name], name) if name in seen else []
    return front


def derive_index(text, key, path, commit):
    """The `design-index-v1` record of a document: a function of its bytes, key, path and commit."""
    from briefing import parse_context_pointers
    from capabilities import markdown_headings
    data = document_bytes(text)
    front = front_block(text)
    if front['scope'] != scope_of(key):
        raise ValueError('the front block scope %s is not the scope of %s' % (front['scope'], key))
    found = markdown_headings(data)
    if len(found) > HEADINGS_MAX:
        raise ValueError('document: %d headings, the limit is %d' % (len(found), HEADINGS_MAX))
    lines = [line.rstrip('\r') for line in text.split('\n')]
    starts = [item[0] for item in found]
    headings, phases, total = [], [], 0
    for position, (number, level, title, anchor, _end, _summary) in enumerate(found):
        own_end = starts[position + 1] - 1 if position + 1 < len(starts) else len(lines)
        own = lines[number:own_end]
        tokens = []
        for line in own:
            for pointer in parse_context_pointers(line):
                if pointer['kind'] in ('req', 'design') and pointer['pointer'] not in tokens:
                    tokens.append(pointer['pointer'])
        total += len(tokens)
        if total > TOKENS_MAX:
            raise ValueError('document: more than %d req: and design: tokens, the limit' % TOKENS_MAX)
        headings.append({'anchor': anchor, 'level': level, 'title': title[:TITLE_MAX], 'tokens': tokens})
        if PLAN_PHASE.fullmatch(anchor):
            phase = {'anchor': anchor, 'contract': [], 'test_oracles': [], 'requirements': [], 'epic': None}
            for line in own:
                field = PHASE_FIELD.fullmatch(line)
                if not field:
                    continue
                name = field.group(1).lower()
                value = field.group(2).strip()[:PHASE_LINE_MAX]
                if name == 'epic':
                    epic = re.search(r'[A-Za-z0-9][A-Za-z0-9_.-]*-[A-Za-z0-9_.-]+', value)
                    if phase['epic'] is None and epic and ISSUE_ID.fullmatch(epic.group(0)):
                        phase['epic'] = epic.group(0)
                    continue
                bucket = 'test_oracles' if name.startswith('test') else name
                if value and len(phase[bucket]) < PHASE_LINES_MAX:
                    phase[bucket].append(value)
            phase['requirements'] = phase['requirements'] or [token for token in tokens if token.startswith('req:')]
            phases.append(phase)
    index = {'schema_version': 1, 'key': key, 'doc_sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
             'path': path, 'commit': commit, 'front': front, 'headings': headings, 'plan_phases': phases}
    index['sha256'] = content_hash(index)
    body = index_body(index)
    if len(body.encode('utf-8')) > INDEX_MAX_BYTES:
        raise ValueError('the document index would be %d bytes, the limit is %d: split the document'
                         % (len(body.encode('utf-8')), INDEX_MAX_BYTES))
    return index


def index_body(index):
    return INDEX_PREFIX + canonical_bytes(index).decode('utf-8')


def _strings(value, limit=None):
    return isinstance(value, list) and all(isinstance(item, str) for item in value) and \
        (limit is None or len(value) <= limit)


def parse_index(body):
    """The index iff the comment is exactly what the server writes; None otherwise."""
    if not isinstance(body, str) or not body.startswith(INDEX_PREFIX):
        return None
    rest = body[len(INDEX_PREFIX):]
    try:
        index = parse_json(rest)
        if not isinstance(index, dict) or tuple(sorted(index)) != tuple(sorted(INDEX_FIELDS)):
            return None
        if canonical_bytes(index).decode('utf-8') != rest or content_hash(index) != index['sha256']:
            return None
        if index['schema_version'] != 1 or not is_design_key(index['key']) \
                or not isinstance(index['doc_sha256'], str) or not SHA256.fullmatch(index['doc_sha256']) \
                or type(index['bytes']) is not int or not isinstance(index['path'], str) \
                or not isinstance(index['commit'], str):
            return None
        front = index['front']
        if not isinstance(front, dict) or tuple(sorted(front)) != tuple(sorted(FRONT_FIELDS)):
            return None
        headings = index['headings']
        if not isinstance(headings, list) or len(headings) > HEADINGS_MAX or any(
                not isinstance(item, dict) or set(item) != {'anchor', 'level', 'title', 'tokens'}
                or not isinstance(item['anchor'], str) or not _strings(item['tokens']) for item in headings):
            return None
        if not isinstance(index['plan_phases'], list):
            return None
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None
    return index


def indexes_for(row, key, authority):
    """Every well-formed index on the anchor `row` for this document (key, sha256, path, commit)."""
    result = []
    for comment in (row or {}).get('comments') or []:
        text = comment.get('text', comment.get('body')) if isinstance(comment, dict) else None
        index = parse_index(text)
        if index is not None and index['key'] == key and index['doc_sha256'] == authority.get('sha256') \
                and index['path'] == authority.get('path') and index['commit'] == authority.get('commit'):
            result.append(index)
    return result


# -- the stored bytes --------------------------------------------------------------------------

def read_document(project, sha256):
    """The stored bytes of `sha256`, refused unless present, a regular file, and exactly that digest."""
    import task_files
    try:
        with task_files.open_blob(project, sha256) as handle:
            data = handle.read(DOCUMENT_MAX_BYTES + 1)
    except task_files.Refusal as error:
        raise ValueError('the stored design document %s cannot be read: %s' % (sha256, error)) from None
    if len(data) > DOCUMENT_MAX_BYTES or hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError('the stored design document does not match its sha256 %s' % sha256)
    return data


def design_entries(project):
    """The task-file journal entries of direction `design`."""
    import task_files
    return [entry for _, entry in task_files.all_entries(project) if entry.get('direction') == task_files.DESIGN]


def check_room(project, data, limit):
    """Refuse, before anything is written, a document the project's limits do not allow."""
    import task_files
    if task_files.project_bytes(project) + len(data) > limit['max_project_bytes']:
        raise ValueError('A project holds at most %s of files (%s); this design document does not fit.'
                         % (task_files.size_text(limit['max_project_bytes']), task_files.setting('max_project_bytes')))
    if task_files.free_bytes(project) - len(data) < limit['min_free_bytes']:
        raise ValueError('The server keeps %s of its disk free (%s); this design document does not fit.'
                         % (task_files.size_text(limit['min_free_bytes']), task_files.setting('min_free_bytes')))
    sha256 = hashlib.sha256(data).hexdigest()
    held = {entry['sha256'] for entry in design_entries(project)}
    if sha256 not in held and len(held) + 1 > limit['max_design_documents']:
        raise ValueError('A project holds at most %d design documents (%s); it holds %d.'
                         % (limit['max_design_documents'], task_files.setting('max_design_documents'), len(held)))


def store_blob(project, data):
    """Put the bytes in the content-addressed store; returns (sha256, created)."""
    import task_files
    try:
        sha256, _, created = task_files.receive(io.BytesIO(data).read, len(data), project)
    except task_files.Refusal as error:
        raise ValueError('the design document could not be stored: %s' % error) from None
    return sha256, created


def journal(project, anchor, key, revision, sha256, size, actor, at):
    """The task-file entry that names the blob (idempotent: one entry per anchor and document)."""
    import task_files
    if task_files.read_entry(project, anchor, sha256) is not None:
        return False
    entry = {'schema_version': 1, 'task': anchor, 'sha256': sha256, 'name': key + '.md', 'bytes': size,
             'media_type': MEDIA_TYPE, 'direction': task_files.DESIGN,
             'note': '%s revision %d' % (key, revision), 'by': actor, 'at': at,
             'record_comment': None, 'withdrawn': None}
    task_files._write_entry(project, entry)
    return True

