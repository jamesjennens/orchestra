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
        # Every value is checked before any reader uses it (review of part 1: the native author of an index
        # proves nothing, so a reader that cannot re-derive it from the bytes trusts only a well-formed one).
        front = index['front']
        if not isinstance(front, dict) or tuple(sorted(front)) != tuple(sorted(FRONT_FIELDS)) \
                or not isinstance(front['scope'], str) or not SCOPE.fullmatch(front['scope']) \
                or front['scope'] != scope_of(index['key']) or front['status'] not in FRONT_STATUSES \
                or any(not _strings(front[name], FRONT_ITEMS_MAX)
                       or any(not item or len(item) > FRONT_ITEM_MAX for item in front[name])
                       for name in ('requirements', 'decisions_open', 'alternatives', 'supersedes')):
            return None
        headings = index['headings']
        if not isinstance(headings, list) or len(headings) > HEADINGS_MAX or any(
                not isinstance(item, dict) or set(item) != {'anchor', 'level', 'title', 'tokens'}
                or not isinstance(item['anchor'], str) or not 0 < len(item['anchor']) <= 1000
                or type(item['level']) is not int or not 1 <= item['level'] <= 6
                or not isinstance(item['title'], str) or len(item['title']) > TITLE_MAX
                or not _strings(item['tokens'], TOKENS_MAX) for item in headings) \
                or sum(len(item['tokens']) for item in headings) > TOKENS_MAX:
            return None
        phases = index['plan_phases']
        if not isinstance(phases, list) or len(phases) > HEADINGS_MAX or any(
                not isinstance(phase, dict)
                or set(phase) != {'anchor', 'contract', 'test_oracles', 'requirements', 'epic'}
                or not isinstance(phase['anchor'], str) or not PLAN_PHASE.fullmatch(phase['anchor'])
                or any(not _strings(phase[name], PHASE_LINES_MAX)
                       or any(len(line) > PHASE_LINE_MAX for line in phase[name])
                       for name in ('contract', 'test_oracles', 'requirements'))
                or (phase['epic'] is not None and (not isinstance(phase['epic'], str)
                                                   or not ISSUE_ID.fullmatch(phase['epic'])))
                for phase in phases):
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



# -- verification (TRACEABILITY_DESIGN §5.1 steps 4-5; kittrial-5bb.418 part 2) ------------------
#
# Whether the stored bytes really are the file at the pinned commit is checked where the code is:
# `trace --verify` reads the file at `<commit>:<path>` and reports. A report is a reserved
# `Kind: design-verification-v1` record on the anchor. Trust mirrors capability verification
# (capability_verification.is_trusted): only the host route (`admin.py design-verify`, an actor in
# the operator allowlist or the verifiers list) writes identity `verified`, and it counts only while
# its native author is that actor and still listed. Any failing report opens `design-drift`; a drift
# opened only by untrusted reports says so (`trusted: false`, `reported_by`). A later trusted pass
# clears it; for a pinned commit the way out is otherwise a revision.

VERIFICATION_PREFIX = 'Kind: design-verification-v1\n'
VERIFICATION_FIELDS = ('schema_version', 'key', 'doc_sha256', 'path', 'commit', 'observed_sha256', 'result',
                       'reason', 'submitter', 'identity', 'at', 'sha256')
PAYLOAD_FIELDS = ('schema_version', 'key', 'doc_sha256', 'path', 'commit', 'observed_sha256', 'result', 'reason')
REASONS = {'pass': ('match',), 'fail': ('mismatch', 'missing', 'not-a-regular-file', 'not-utf8')}
ROUTES = ('host', 'endpoint', 'web')
UNTRUSTED_REPORTS_MAX = 50


def check_verification_payload(payload):
    """One `trace --verify` report: the accepted document it checked, and what the checkout holds."""
    if not isinstance(payload, dict) or set(payload) - set(PAYLOAD_FIELDS) - {'operation_id'} \
            or not set(PAYLOAD_FIELDS) <= set(payload):
        raise ValueError('a design verification has exactly: %s' % ', '.join(PAYLOAD_FIELDS))
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1:
        raise ValueError('schema_version must be the integer 1')
    if not is_design_key(payload['key']):
        raise ValueError('a design verification names a design.<scope> entry')
    if not isinstance(payload['doc_sha256'], str) or not SHA256.fullmatch(payload['doc_sha256']):
        raise ValueError('doc_sha256 must be a 64-character lowercase hex sha256')
    observed = payload['observed_sha256']
    if observed is not None and (not isinstance(observed, str) or not SHA256.fullmatch(observed)):
        raise ValueError('observed_sha256 must be a sha256 or null')
    if not isinstance(payload['path'], str) or not isinstance(payload['commit'], str):
        raise ValueError("path and commit are the accepted revision's")
    if payload['result'] not in REASONS or payload['reason'] not in REASONS[payload['result']]:
        raise ValueError('result is pass (reason match) or fail (mismatch, missing, not-a-regular-file, not-utf8)')
    if (payload['result'] == 'pass') != (observed == payload['doc_sha256']):
        raise ValueError("a pass is a checkout file whose sha256 is the stored document's, and only that")
    return payload


def verification_record(payload, actor, route, identity, at):
    record = {name: payload[name] for name in PAYLOAD_FIELDS}
    record.update(submitter={'actor': actor, 'route': route}, identity=identity, at=at)
    record['sha256'] = content_hash(record)
    return record


def verification_body(record):
    return VERIFICATION_PREFIX + canonical_bytes(record).decode('utf-8')


def parse_verification(body):
    """The verification iff the comment is exactly what the server writes; None otherwise."""
    if not isinstance(body, str) or not body.startswith(VERIFICATION_PREFIX):
        return None
    rest = body[len(VERIFICATION_PREFIX):]
    try:
        record = parse_json(rest)
        if not isinstance(record, dict) or tuple(sorted(record)) != tuple(sorted(VERIFICATION_FIELDS)):
            return None
        if canonical_bytes(record).decode('utf-8') != rest or content_hash(record) != record['sha256']:
            return None
        check_verification_payload({name: record[name] for name in PAYLOAD_FIELDS})
        submitter = record['submitter']
        if not isinstance(submitter, dict) or set(submitter) != {'actor', 'route'} \
                or not isinstance(submitter['actor'], str) or submitter['route'] not in ROUTES \
                or record['identity'] not in ('verified', 'unverified') or not isinstance(record['at'], str):
            return None
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None
    return record


def verifications_for(row, key, authority):
    """[(position, record, native author)] for the document `authority` names, in native order."""
    found = []
    for position, comment in enumerate((row or {}).get('comments') or []):
        if not isinstance(comment, dict):
            continue
        record = parse_verification(comment.get('text', comment.get('body')))
        if record is not None and record['key'] == key and record['doc_sha256'] == authority.get('sha256') \
                and record['path'] == authority.get('path') and record['commit'] == authority.get('commit'):
            found.append((position, record, comment.get('author')))
    return found


def is_trusted(record, author, trusted):
    """capability_verification.is_trusted's rule: host-written, its author the submitter, still listed."""
    return record['identity'] == 'verified' and record['submitter']['route'] == 'host' \
        and author == record['submitter']['actor'] and author in trusted


def derive_verification(items, trusted):
    """The `verification` block of an accepted repo-document revision: verified, unverified or design-drift."""
    block = {'state': 'unverified', 'reports': len(items), 'trusted': None, 'reported_by': [],
             'verified_by': None, 'verified_at': None}
    failing = [item for item in items if item[1]['result'] == 'fail']
    cleared = -1
    if failing:
        for position, record, author in reversed(items):
            if position < failing[0][0]:
                break
            if record['result'] == 'pass' and is_trusted(record, author, trusted):
                cleared = position
                break
    open_failing = [item for item in failing if item[0] > cleared]
    if open_failing:
        block.update(state='design-drift',
                     trusted=any(is_trusted(record, author, trusted) for _, record, author in open_failing),
                     reported_by=sorted({record['submitter']['actor'] for _, record, _ in open_failing}),
                     reason=open_failing[-1][1]['reason'], observed_sha256=open_failing[-1][1]['observed_sha256'])
        return block
    passes = [item for item in items if item[1]['result'] == 'pass' and is_trusted(item[1], item[2], trusted)]
    if passes:
        record = passes[-1][1]
        block.update(state='verified', trusted=True, verified_by=record['submitter']['actor'],
                     verified_at=record['at'])
    return block


def selected_index(row, key, authority):
    """The one index the readers use for a document: present and unique (every copy equal), else None."""
    found = indexes_for(row, key, authority)
    if not found or any(index != found[0] for index in found):
        return None
    return found[0]
