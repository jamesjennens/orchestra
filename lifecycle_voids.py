"""Host-issued corrections of named lifecycle events, with native-first retries.

The journal is the host boundary, as for integration reverts. A native comment
alone is never a correction. Authorization is checked at write time; an issued
correction stays effective after operator replacement until an authorized linked
retraction. Actor names on an unbound host remain attribution, not authentication.
Nothing in this module creates a directory on a read.
"""
import json
import hashlib
import os
import re
import stat
from pathlib import Path

import record_json
from requirements import canonical_bytes, content_hash

DIRECTORY = '.lifecycle-voids'
MANIFEST = '.lifecycle-voids-manifest.json'
RESTORE_MARKER = '.lifecycle-void-restore.json'
#: Abandonments live BESIDE the family, never inside it (kittrial-5bb.271 item 1).
#: The released F0 reader (main 04ed25d) reads a schema-version-1 family member
#: and refuses any operation but ``void``/``retract``; an abandonment written as
#: a family member therefore turned EVERY lifecycle fact of the project unknown
#: (``lifecycle-void-journal-unreadable``). Kept out of ``DIRECTORY``, a family
#: that holds an abandonment stays a family the released reader reads unchanged.
ABANDON_FILE = '.lifecycle-void-abandons.json'
PREFIX = 'Kind: lifecycle-event-correction-v1\n'
MAX_RECORD_BYTES = 24000
MAX_RECORDS = 1000
MAX_FAMILY_BYTES = 4 * 1024 * 1024
#: Printed by ``admin.py lifecycle-event-abandon`` before it writes, and stated in
#: docs/OPERATIONS.md: an abandonment also closes a void whose journal entry was
#: lost, so the operator must rule that out first (kittrial-5bb.271 item 3).
ABANDON_LOST_ENTRY_RULE = (
    'Before abandoning, rule out a LOST journal entry rather than a cut: compare the newest complete '
    'backup\'s .lifecycle-voids-manifest.json with this project\'s live .lifecycle-voids directory. A member '
    '<sha256>.json listed there but absent live is a LOST entry - restore that family from the backup '
    'instead of abandoning. Abandon only when no complete backup lists a missing member, i.e. the '
    'correction never took effect. Abandonment closes the comment for good: its exact retry is refused.')
BINDING_FIELDS = {'event_id', 'task', 'dimension', 'value', 'created_by', 'payload',
                  'created_at', 'payload_sha256', 'native_sha256'}
PAYLOAD_FIELDS = {'schema_version', 'operation', 'operation_id', 'actor', 'reason',
                  'evidence', 'binding', 'target_void'}
ENTRY_FIELDS = {'schema_version', 'payload', 'sha256', 'comment_id', 'created_at'}
UNKNOWN_JOURNAL = 'lifecycle-void-journal-unreadable'


def entry_bytes(entry):
    # Exactly the serialization coordination.atomic publishes (including its
    # separator spaces), so admission cannot emit a file its reader rejects.
    return json.dumps(entry, ensure_ascii=False, sort_keys=True).encode('utf-8')


def _text(value, name, limit=500):
    if not isinstance(value, str) or not value.strip() or '\0' in value or len(value) > limit:
        raise ValueError(name + ': require bounded nonblank text')
    return value


def _identity(value, name):
    _text(value, name, 300)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@/-]*', value):
        raise ValueError('Invalid ' + name)
    return value


def event_binding(row):
    """Bind an exact lifecycle-shaped native row, including raw/untrusted facts."""
    from lifecycle import native_event, DIMENSIONS, ENABLED, LIVE
    event = native_event(row)
    if event is None or event['dimension'] not in (*DIMENSIONS, 'lifecycle-scope', ENABLED, LIVE):
        raise ValueError('Target is not a readable lifecycle state event')
    for key in ('id', 'created_by', 'created_at'):
        _text(row.get(key), 'native ' + key, 300)
    parents = [{'depends_on_id': d.get('depends_on_id'), 'type': d.get('type')}
               for d in row.get('dependencies', []) if d.get('type') == 'parent-child']
    bound = {key: row.get(key) for key in
             ('id', 'issue_type', 'title', 'description', 'created_by', 'created_at')}
    bound['parents'] = parents
    return dict(event_id=row['id'], task=event['task'], dimension=event['dimension'],
                value=event['value'], created_by=row['created_by'], created_at=row['created_at'],
                payload=event['payload'],
                payload_sha256=content_hash(event['payload']) if event['payload'] else None,
                native_sha256=content_hash(bound))


def validate_payload(payload, allow_abandon=False):
    """Validate one correction payload.

    ``allow_abandon`` is true only for the abandonment store (and for reading a
    legacy family the reviewed-but-unintegrated kit could have written). A journal
    member is validated WITHOUT it, so the released F0 rule is preserved exactly:
    a schema-version-1 family member may name only ``void`` or ``retract``, and any
    other operation is refused with 'Invalid lifecycle correction operation'.
    """
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_FIELDS:
        raise ValueError('Invalid lifecycle correction fields')
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1:
        raise ValueError('Invalid lifecycle correction version')
    allowed = ('void', 'retract', 'abandon') if allow_abandon else ('void', 'retract')
    if payload['operation'] not in allowed:
        raise ValueError('Invalid lifecycle correction operation')
    _identity(payload['operation_id'], 'operation identity')
    _identity(payload['actor'], 'actor')
    _text(payload['reason'], 'reason', 1000)
    evidence = payload['evidence']
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 20:
        raise ValueError('Evidence needs one to twenty nonblank pointers')
    for pointer in evidence:
        _text(pointer, 'evidence')
    binding = payload['binding']
    if not isinstance(binding, dict) or set(binding) != BINDING_FIELDS:
        raise ValueError('Invalid lifecycle correction target binding')
    for key in ('event_id', 'task'):
        _identity(binding[key], key)
    for key in ('dimension', 'value', 'created_by', 'created_at'):
        _text(binding[key], key, 300)
    from lifecycle import DIMENSIONS, ENABLED, LIVE
    if binding['dimension'] not in (*DIMENSIONS, 'lifecycle-scope', ENABLED, LIVE):
        raise ValueError('Invalid lifecycle correction dimension')
    for key in ('native_sha256', 'payload_sha256'):
        if key == 'payload_sha256' and binding[key] is None:
            continue
        if not isinstance(binding[key], str) or not re.fullmatch('[a-f0-9]{64}', binding[key]):
            raise ValueError('Invalid lifecycle correction hash')
    original = binding['payload']
    if original is None:
        if binding['payload_sha256'] is not None:
            raise ValueError('Raw lifecycle correction target has a payload hash')
    else:
        from lifecycle import validate_payload as validate_fact
        validate_fact(original)
        if (content_hash(original) != binding['payload_sha256']
                or any(original[key] != binding[key] for key in ('task', 'dimension', 'value'))
                or original['actor'] != binding['created_by']):
            raise ValueError('Lifecycle correction target payload binding mismatch')
    if payload['operation'] == 'void':
        if payload['target_void'] is not None:
            raise ValueError('A void has no retraction target')
    elif payload['operation'] == 'retract':
        _identity(payload['target_void'], 'void operation identity')
        if payload['target_void'] == payload['operation_id']:
            raise ValueError('A retraction cannot name itself')
    else:
        # An abandonment names the cut correction operation it closes, so the
        # record binds the exact native comment rather than only its task/event.
        _identity(payload['target_void'], 'abandoned operation identity')
        if payload['target_void'] == payload['operation_id']:
            raise ValueError('An abandonment cannot name itself')
    if len(canonical_bytes(payload)) > MAX_RECORD_BYTES:
        raise ValueError('Lifecycle correction exceeds its byte bound')
    return payload


def validate_entry(entry, name=None, allow_abandon=False):
    if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
        raise ValueError('Invalid lifecycle void journal entry')
    if type(entry['schema_version']) is not int or entry['schema_version'] != 1:
        raise ValueError('Invalid lifecycle void journal version')
    validate_payload(entry['payload'], allow_abandon=allow_abandon)
    _identity(entry['comment_id'], 'native correction comment identity')
    _text(entry['created_at'], 'native correction timestamp', 100)
    from review_workflow import _recorded_at
    if _recorded_at(entry['created_at']) is None:
        raise ValueError('Invalid native lifecycle correction timestamp')
    digest = content_hash(entry['payload'])
    if entry['sha256'] != digest or (name is not None and name != digest + '.json'):
        raise ValueError('Lifecycle void journal hash/path does not match its payload')
    if len(entry_bytes(entry)) > MAX_RECORD_BYTES:
        raise ValueError('Lifecycle void journal entry exceeds its byte bound')
    return entry


def correction_payload(comment, allow_abandon=False):
    """The validated correction payload a native comment carries, or None.

    One reader for the places that must recognize a native correction
    comment without trusting its JSON shape: the journal/comment match and the
    abandonment target lookup. A comment that does not start the reserved
    prefix, or whose payload is missing or invalid, has no correction payload.

    ``allow_abandon`` (kittrial-5bb.274 round 3) lets the audit parse an
    ABANDONMENT's own native comment - a cut abandonment whose host store write
    never happened - so that orphan comment is named instead of reading
    ``native-correction-payload-unclassifiable``. It confers no authority: the
    caller still needs a host record to read the abandonment as issued.
    """
    text = comment.get('text') if isinstance(comment, dict) else None
    if not isinstance(text, str) or not text.startswith(PREFIX):
        return None
    try:
        return validate_payload(record_json.loads(text[len(PREFIX):]), allow_abandon=allow_abandon)
    except (ValueError, TypeError):
        return None


def abandoned_target(payload, comment):
    """True when ``comment`` is the cut correction an abandonment payload names.

    The abandonment's payload names the abandoned comment by operation identity
    and binding (never by re-creating its words), so this is the only lookup that
    pairs the two. A void/retraction payload has no target.
    """
    if payload['operation'] != 'abandon':
        return False
    original = correction_payload(comment)
    return (original is not None and original['operation'] in ('void', 'retract')
            and original['operation_id'] == payload['target_void']
            and original['binding'] == payload['binding'])


def entry_binds_comment(entry, comment):
    """True when ``entry`` is a host record for the native correction ``comment``.

    A void/retraction entry binds the comment whose text IS its payload. An
    abandonment (kittrial-5bb.274 item 1) writes its OWN native comment - the
    proof - and its payload names the cut comment it closes by operation identity,
    so it binds either its own comment (by text) or the abandoned target it names.
    The stronger proof rule used by :func:`context` - the abandonment's own
    comment, authored by the abandoning operator, and nothing else - is
    deliberately not enforced here, because the offline reader asks only whether
    the journal was consulted about every native correction comment. A raw row
    comment carries no ``task`` of its own; an inventory item from
    :func:`native_correction_comments` does, and it must agree.
    """
    payload = entry['payload']
    if not isinstance(comment, dict):
        return False
    if comment.get('task') is not None and comment.get('task') != payload['binding']['task']:
        return False
    if (str(comment.get('id')) == str(entry['comment_id'])
            and comment.get('text') == PREFIX + canonical_bytes(payload).decode()):
        return True
    return abandoned_target(payload, comment)


def incomplete_correction(rows, comment_id):
    """``binding``/``target_void`` for one unpaired native correction, or None.

    The host abandonment command calls this before its write, so the operator
    names only the comment the audit lists and does not reproduce the original
    reason, evidence or key. Only a void/retraction comment is a correction
    target; an unreadable payload has no binding to record.
    """
    for comment in native_correction_comments(rows):
        if comment['id'] != str(comment_id):
            continue
        original = correction_payload(comment)
        if original is None or original['operation'] not in ('void', 'retract'):
            return None
        return dict(binding=original['binding'], target_void=original['operation_id'])
    return None


def _read_regular(path, limit=MAX_RECORD_BYTES):
    if path.is_symlink():
        raise ValueError('Lifecycle void journal entry is a symlink')
    flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('Lifecycle void journal entry is not a bounded regular file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            blob = stream.read(limit + 1)
        if len(blob) > limit:
            raise ValueError('Lifecycle void journal entry exceeds its byte bound')
        return blob
    finally:
        os.close(fd)


def abandon_document(records):
    """The closed abandonment store document for ``records`` (keyed by operation id)."""
    return dict(schema_version=1, records={key: records[key] for key in sorted(records)})


def validate_abandons(document):
    """A closed, bounded abandonment store: only abandonment records, hash-checked."""
    if (not isinstance(document, dict) or set(document) != {'schema_version', 'records'}
            or type(document['schema_version']) is not int or document['schema_version'] != 1):
        raise ValueError('Invalid lifecycle abandonment store')
    records = document['records']
    if not isinstance(records, dict) or len(records) > MAX_RECORDS:
        raise ValueError('Invalid lifecycle abandonment store records')
    validated = {}
    total = 0
    for key, record in records.items():
        _identity(key, 'operation identity')
        validate_entry(record, allow_abandon=True)
        if record['payload']['operation'] != 'abandon' or record['payload']['operation_id'] != key:
            raise ValueError('Invalid lifecycle abandonment store record identity')
        total += len(entry_bytes(record))
        if total > MAX_FAMILY_BYTES:
            raise ValueError('Lifecycle abandonment store exceeds its byte bound')
        validated[key] = record
    return validated


def read_abandons(project):
    """Bounded read of the abandonment store beside the family; never creates a file.

    An unreadable store is reported, never trusted: its records are simply absent,
    so an abandoned native correction comment reads ``incomplete`` again (backups
    stop) instead of being silently treated as closed. It can never change a
    voided fact either way.
    """
    result = dict(records={}, problems=[], present=False)
    if project is None:
        return result
    path = Path(project) / ABANDON_FILE
    if not path.exists() and not path.is_symlink():
        return result
    result['present'] = True
    try:
        if path.is_symlink():
            raise ValueError('Lifecycle abandonment store is a symlink')
        blob = _read_regular(path, MAX_FAMILY_BYTES)
        result['records'] = validate_abandons(record_json.loads(blob.decode('utf-8')))
    except (ValueError, OSError, UnicodeError) as error:
        result['problems'].append(str(error)[:300])
    # kittrial-5bb.274 item 6: the store is created 0600 and a group- or
    # world-writable one is reported, never silently trusted. POSIX only: on
    # Windows the synthesized mode bits would flag every ordinary file.
    if os.name == 'posix':
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            mode = None
        if mode is not None and mode & 0o022:
            result['problems'].append(
                'Lifecycle abandonment store is group- or world-writable (mode %04o)' % mode)
    return result


def publish_abandon(project, record):
    """Atomically add one abandonment record to the store beside the family.

    The store is rewritten whole under the caller's project lock. It is never a
    family member, so a family that holds an abandonment stays readable by the
    released F0 kit.
    """
    from coordination import atomic
    validate_entry(record, allow_abandon=True)
    if record['payload']['operation'] != 'abandon':
        raise ValueError('Not an abandonment record')
    path = Path(project) / ABANDON_FILE
    if path.is_symlink() or path.with_suffix('.tmp').is_symlink():
        raise ValueError('Lifecycle abandonment store must not be a symlink')
    current = read_abandons(project)
    if current['problems']:
        raise ValueError('Lifecycle abandonment store cannot be read; operator repairs it before a write')
    records = dict(current['records'])
    existing = records.get(record['payload']['operation_id'])
    if existing is not None and existing != record:
        raise ValueError('Lifecycle correction operation identity already names different content/actor')
    records[record['payload']['operation_id']] = record
    document = abandon_document(records)
    if len(entry_bytes(record)) > MAX_RECORD_BYTES or len(entry_bytes(document)) > MAX_FAMILY_BYTES:
        raise ValueError('Lifecycle abandonment store exceeds its byte bound')
    atomic(path, document)
    # kittrial-5bb.274 item 6: the store is host-only audit evidence, so it is
    # created 0600 and never left group- or world-writable.
    if os.name == 'posix':
        os.chmod(path, 0o600)
    return path


def read_journal(project):
    """Bounded host-family read plus the abandonment store beside it.

    Damage never silently restores a voided fact. ``abandons`` is the separate
    store the writer uses; a family member that itself carries an abandonment (the
    shape the reviewed-but-unintegrated kit wrote once) is still READ - no family
    is turned unreadable by a shape this kit shipped - but :func:`context` no
    longer accepts it as proof, because an abandonment is proved only by its own
    native comment (kittrial-5bb.274 item 1). It therefore reads unproved and the
    audit reads damaged until an operator reconciles it.
    """
    result = dict(entries={}, invalid=[], present=False, bytes=0,
                  abandons={}, abandon_problems=[])
    if project is None:
        return result
    store = read_abandons(project)
    result['abandons'] = store['records']
    result['abandon_problems'] = store['problems']
    # Written before replacing any restore member and cleared only after the
    # complete family/native pair is checked. Any shape means uncertainty.
    if os.path.lexists(str(Path(project) / RESTORE_MARKER)):
        result['invalid'].append('lifecycle-void-restore-incomplete')
    directory = Path(project) / DIRECTORY
    if not directory.exists() and not directory.is_symlink():
        return result
    result['present'] = True
    if directory.is_symlink() or not directory.is_dir():
        result['invalid'].append(UNKNOWN_JOURNAL)
        return result
    total = 0
    before = None
    try:
        before = directory.stat()
        # Stop enumeration at the bound rather than allocating an arbitrary directory.
        with os.scandir(directory) as scan:
            names = []
            for entry in scan:
                names.append(entry.name)
                if len(names) > MAX_RECORDS:
                    raise ValueError('Lifecycle void journal exceeds its record bound')
        from coordination import is_temporary
        for name in sorted(names):
            # Atomic/probe temporaries confer no authority and are not records. A
            # shared atomic write leaves a UNIQUE temporary (``<name>.tmp-<pid>-<hex>``,
            # kittrial-5bb.281) as well as the fixed ``<name>.tmp`` an older kit cut.
            if re.fullmatch(r'(?:[a-f0-9]{64}|\.writable-[a-f0-9]{32})\.tmp', name):
                continue
            if is_temporary(name):
                continue
            try:
                if not re.fullmatch('[a-f0-9]{64}\\.json', name):
                    raise ValueError('Invalid lifecycle void journal file name')
                blob = _read_regular(directory / name)
                total += len(blob)
                if total > MAX_FAMILY_BYTES:
                    raise ValueError('Lifecycle void journal exceeds its byte bound')
                record = validate_entry(record_json.loads(blob.decode('utf-8')), name,
                                        allow_abandon=True)
                key = record['payload']['operation_id']
                if key in result['entries']:
                    raise ValueError('Conflicting lifecycle correction operation identity')
                result['entries'][key] = record
            except (ValueError, OSError, UnicodeError) as error:
                result['invalid'].append(str(error)[:300])
                if total > MAX_FAMILY_BYTES:
                    break
    except (ValueError, OSError) as error:
        result['invalid'].append(str(error)[:300])
    try:
        after = directory.stat()
        if (before is None or (before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_ino, after.st_mtime_ns, after.st_ctime_ns)):
            result['invalid'].append('lifecycle-void-family-changed-during-read')
        if os.path.lexists(str(Path(project) / RESTORE_MARKER)) and 'lifecycle-void-restore-incomplete' not in result['invalid']:
            result['invalid'].append('lifecycle-void-restore-incomplete')
    except OSError:
        result['invalid'].append(UNKNOWN_JOURNAL)
    result['bytes'] = total
    return result


def backup_files(project, rows=None):
    """Allowlisted bounded family, its generation manifest and the abandonment store."""
    journal = read_journal(project)
    if journal['invalid']:
        raise ValueError('Cannot back up an unreadable/incomplete lifecycle VOID family')
    if journal['abandon_problems']:
        raise ValueError('Cannot back up an unreadable lifecycle abandonment store: '
                         + journal['abandon_problems'][0])
    abandons = journal['abandons']
    if not journal['present'] and not abandons:
        if rows is not None and native_correction_comments(rows):
            raise ValueError('Native lifecycle correction comments exist but their VOID family is missing; '
                             'an operator preserves the native data and recovers a complete VOID-capable backup')
        return {}
    if rows is None:
        raise ValueError('A lifecycle VOID backup requires its native snapshot')
    files = {DIRECTORY + '/' + entry['sha256'] + '.json': entry
             for entry in journal['entries'].values()}
    store = abandon_document(abandons) if abandons else None
    manifest_files = {name: hashlib.sha256(entry_bytes(record)).hexdigest()
                      for name, record in files.items()}
    # kittrial-5bb.274 item 1: the store is bound by the manifest exactly as a
    # family member is, so a sidecar whose record was rewritten and rehashed
    # cannot be restored (review 1013-271-r2, rev/in/s13.py).
    if store is not None:
        manifest_files[ABANDON_FILE] = hashlib.sha256(entry_bytes(store)).hexdigest()
    files[MANIFEST] = dict(schema_version=1, family=DIRECTORY, count=len(files),
                           bytes=sum(len(entry_bytes(record)) for record in files.values()),
                           files=manifest_files,
                           native_corrections_sha256=native_generation(rows))
    if store is not None:
        # The abandonment store is a sidecar, never a family member: the released
        # F0 kit reads this backup's family unchanged (it refuses the unknown
        # sidecar path on restore, which docs/OPERATIONS.md states exactly).
        files[ABANDON_FILE] = store
    validate_backup(files, rows)
    return files


def native_correction_comments(rows):
    """Bounded native correction inventory, including unissued/cut comments."""
    comments = []
    total = 0
    for row in rows:
        if not isinstance(row,dict):
            raise ValueError('Native correction comments cannot be checked on an unreadable row')
        values=row.get('comments') or []
        if not isinstance(values,list):
            raise ValueError('Native correction comments cannot be checked on an unreadable comment list')
        for comment in values:
            if not isinstance(comment,dict):
                raise ValueError('Native correction comments cannot be checked on an unreadable comment')
            text=comment.get('text')
            if not isinstance(text,str) or not text.startswith(PREFIX):continue
            item=dict(task=row.get('id'),id=str(comment.get('id')),
                      author=comment.get('author'),created_at=comment.get('created_at'),text=text)
            size=len(canonical_bytes(item));total+=size
            if size>MAX_RECORD_BYTES+1000 or total>MAX_FAMILY_BYTES or len(comments)>=MAX_RECORDS:
                raise ValueError('Native lifecycle correction generation exceeds its bounds')
            comments.append(item)
    return sorted(comments,key=canonical_bytes)


def native_generation(rows):
    """Bound all correction comments, including inert/pending ones, to this backup.

    This is generation evidence only: a native comment still confers no issuance
    authority. It prevents an older sidecar erasing a newer local correction when
    a changed native backup happens to retain the older named event/comment pair.
    """
    return content_hash({'comments':native_correction_comments(rows)})


def validate_backup(files, rows=None):
    """Closed manifest, every member/hash/bound, linked retraction graph and abandonment store."""
    if not isinstance(files, dict) or any(not isinstance(name, str) for name in files):
        raise ValueError('Invalid lifecycle VOID backup files')
    members = {name: value for name, value in files.items() if name.startswith(DIRECTORY + '/')}
    manifest = files.get(MANIFEST)
    store = files.get(ABANDON_FILE)
    if not members and manifest is None and store is None:
        return None
    if (not isinstance(manifest, dict)
            or set(manifest) != {'schema_version', 'family', 'count', 'bytes', 'files', 'native_corrections_sha256'}
            or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1
            or manifest['family'] != DIRECTORY or type(manifest['count']) is not int
            or type(manifest['bytes']) is not int or manifest['count'] != len(members)
            or not 0 <= len(members) <= MAX_RECORDS):
        raise ValueError('Invalid lifecycle VOID backup manifest')
    if not isinstance(manifest['native_corrections_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', manifest['native_corrections_sha256']):
        raise ValueError('Invalid lifecycle VOID native generation hash')
    abandons = validate_abandons(store) if store is not None else {}
    entries = {}
    for name, entry in members.items():
        if not re.fullmatch(re.escape(DIRECTORY) + r'/[a-f0-9]{64}\.json', name):
            raise ValueError('Invalid lifecycle VOID backup path')
        validate_entry(entry, name.partition('/')[2], allow_abandon=True)
        key = entry['payload']['operation_id']
        if key in entries or key in abandons:
            raise ValueError('Conflicting lifecycle VOID backup operation identity')
        entries[key] = entry
    total = sum(len(entry_bytes(record)) for record in members.values())
    expected_hashes = {name: hashlib.sha256(entry_bytes(record)).hexdigest()
                       for name, record in members.items()}
    if store is not None:
        # kittrial-5bb.274 item 1: the store's bytes are bound by the manifest.
        expected_hashes[ABANDON_FILE] = hashlib.sha256(entry_bytes(store)).hexdigest()
    if (total > MAX_FAMILY_BYTES or manifest['bytes'] != total
            or manifest['files'] != expected_hashes):
        raise ValueError('Lifecycle VOID backup manifest member/hash/byte mismatch')
    retracted = set()
    from review_workflow import _recorded_at
    for entry in entries.values():
        payload = entry['payload']
        if payload['operation'] != 'retract':
            continue
        target = entries.get(payload['target_void'])
        if (target is None or target['payload']['operation'] != 'void'
                or target['payload']['binding'] != payload['binding']
                or _recorded_at(entry['created_at']) < _recorded_at(target['created_at'])
                or payload['target_void'] in retracted):
            raise ValueError('Invalid lifecycle VOID backup retraction graph')
        retracted.add(payload['target_void'])
    journal = dict(entries=entries, invalid=[], present=True, abandons=abandons, abandon_problems=[])
    if rows is not None:
        if native_generation(rows) != manifest['native_corrections_sha256']:
            raise ValueError('Lifecycle VOID backup does not bind the native correction comment generation')
        state = context(rows, journal)
        if any(reason != 'voided' for reason in state['reasons'].values()):
            raise ValueError('Lifecycle VOID backup does not bind the native target/comment generation')
        # kittrial-5bb.274 item 2: an abandonment without a native comment by its
        # operator, or one that closes a comment which already has a host entry,
        # is never silently accepted as closing the audit.
        if state['abandon_problems']:
            raise ValueError('Lifecycle VOID backup has an unproved or duplicate abandonment: '
                             + state['abandon_problems'][0])
        if state['incomplete']:
            raise ValueError('Lifecycle VOID backup has native correction comments without their host entries')
    return journal


def restore_files(project, files, rows):
    """Install a validated generation; interrupted reads remain UNKNOWN until exact repair.

    The caller owns the project coordination lock (or an unpublished restore-new
    destination). Restore is trusted host action: this restores issuance evidence,
    never re-grants the recorded actor or changes a native lifecycle/release row.
    """
    from coordination import atomic
    project = Path(project)
    journal = validate_backup(files)
    directory = project / DIRECTORY
    marker = project / RESTORE_MARKER
    store_path = project / ABANDON_FILE
    if journal is None:
        if (os.path.lexists(str(directory)) or os.path.lexists(str(marker))
                or os.path.lexists(str(store_path)) or read_abandons(project)['records']):
            raise ValueError('Legacy backup has no VOID manifest; preserve the current family and reconcile '
                             'with a complete VOID-capable backup before restoring')
        if native_correction_comments(rows):
            # The native restore already happened. Do not allow that published
            # destination to silently expose the original values again.
            atomic(marker,dict(schema_version=1,missing_void_manifest=True,
                               native_corrections_sha256=native_generation(rows)))
            raise ValueError('Native lifecycle correction comments exist but the backup carries no VOID '
                             'manifest or family. Restore refused; the incomplete restore marker is retained. '
                             'Preserve this destination and restore a complete VOID-capable backup into '
                             'an unused destination; do not use or back up this incomplete tracker.')
        return False
    if (directory.is_symlink() or marker.is_symlink() or marker.with_suffix('.tmp').is_symlink()
            or store_path.is_symlink() or store_path.with_suffix('.tmp').is_symlink()):
        raise ValueError('Lifecycle VOID restore paths must not be symlinks')
    if directory.exists() and not directory.is_dir():
        raise ValueError('Lifecycle VOID restore directory is not a directory')
    # Refuse an unrelated incomplete restore rather than changing its meaning.
    identity = dict(schema_version=1, manifest_sha256=content_hash(files[MANIFEST]))
    if marker.exists() and record_json.loads(_read_regular(marker).decode('utf-8')) != identity:
        raise ValueError('Another lifecycle VOID restore is incomplete; reconcile its exact manifest first')
    # A native restore may already have taken place. Bind failure must therefore
    # leave uncertainty visible, even before the first family member is written.
    atomic(marker, identity)
    validate_backup(files, rows)
    directory.mkdir(exist_ok=True)
    _writable(directory)
    old = read_journal(project)
    # The marker's own uncertainty is expected; other family damage is not safe
    # to overwrite automatically. Exact manifest members may repair a cut.
    if any(problem != 'lifecycle-void-restore-incomplete' for problem in old['invalid']):
        raise ValueError('Existing lifecycle VOID family is damaged; operator reconciliation required')
    # The manifest's ``files`` now names the store too (kittrial-5bb.274 item 1),
    # but the store is not a family member: install it below, with its own mode.
    members = {name for name in files[MANIFEST]['files'] if name != ABANDON_FILE}
    for name in sorted(members):
        target = project / name
        if target.is_symlink() or target.with_suffix('.tmp').is_symlink():
            raise ValueError('Lifecycle VOID restore member must not be a symlink')
    for name in sorted(members):
        atomic(project / name, files[name])
    for entry in old['entries'].values():
        name = DIRECTORY + '/' + entry['sha256'] + '.json'
        if name not in members:
            (project / name).unlink()
    # The abandonment store is part of this generation: install the backup's
    # store, or remove a live one the backup does not carry, so a restore never
    # leaves more abandonment evidence than the backup proves.
    if ABANDON_FILE in files:
        atomic(store_path, files[ABANDON_FILE])
        if os.name == 'posix':
            os.chmod(store_path, 0o600)
    elif os.path.lexists(str(store_path)):
        store_path.unlink()
    # Read and compare every installed member before clearing uncertainty.
    actual = read_journal(project)
    if (actual['entries'] != journal['entries']
            or actual['abandons'] != journal['abandons']
            or actual['abandon_problems']
            or actual['invalid'] != ['lifecycle-void-restore-incomplete']):
        raise ValueError('Lifecycle VOID restore verification failed; marker retained')
    validate_backup(files, rows)
    marker.unlink()
    return True


def context(rows, journal):
    """One native snapshot's effective corrections; never recheck today's operator list."""
    reasons = {}
    entries = dict(journal.get('entries') or {})
    # The abandonment store lives beside the family (kittrial-5bb.271 item 1), so
    # merge it here: every reader below then sees one correction view whether a
    # record came from the family or the store. A legacy family member that itself
    # carries an abandonment is already in ``entries``.
    for key, record in (journal.get('abandons') or {}).items():
        entries.setdefault(key, record)
    issues = {}
    for row in rows:
        if isinstance(row, dict) and row.get('id'):
            issues.setdefault(row['id'], []).append(row)
    invalid = list(journal.get('invalid', []))
    try:
        native_comments = native_correction_comments(rows)
    except ValueError as error:
        native_comments = []; invalid.append(str(error))
    valid = {}
    entry_reasons = {}
    task_reasons = {}
    # kittrial-5bb.274 item 1: an abandonment is host-only evidence, so it must
    # never add a ``reasons`` entry and never re-decide the event. It is proved
    # ONLY by its OWN native comment, written by the abandoning actor through the
    # same guarded native write as a void. A store record with no such comment -
    # a hand-added record naming the true author of the cut, or one the
    # reviewed-but-unintegrated kit wrote - is never proof and is flagged below.
    unproved = []
    abandon_targets = {}
    for key, entry in entries.items():
        payload = entry['payload']; binding = payload['binding']; event_id = binding['event_id']
        if payload['operation'] == 'abandon':
            continue
        targets = issues.get(event_id, [])
        # A void/retraction binds the exact current event.
        reason = 'lifecycle-void-target-changed'
        try:
            if len(targets) != 1 or event_binding(targets[0]) != binding:
                raise ValueError('lifecycle-void-target-changed')
            reason = 'lifecycle-void-journal-mismatch'
            tasks = issues.get(binding['task'], [])
            if len(tasks) != 1:
                raise ValueError('lifecycle-void-journal-mismatch')
            comments = [c for c in tasks[0].get('comments', [])
                        if str(c.get('id')) == entry['comment_id']]
            if len(comments) != 1:
                raise ValueError('lifecycle-void-journal-mismatch')
            comment = comments[0]
            if not entry_binds_comment(entry, comment):
                raise ValueError('lifecycle-void-journal-mismatch')
            if (comment.get('author') != payload['actor']
                    or comment.get('created_at') != entry['created_at']):
                raise ValueError('lifecycle-void-journal-mismatch')
            valid[key] = entry
        except (ValueError, TypeError, KeyError):
            reasons[event_id] = reason
            entry_reasons[key] = reason
            if len(targets)==1:
                from lifecycle import native_event
                actual=native_event(targets[0])
                if actual is not None and actual['task']!=binding['task']:
                    task_reasons[binding['task']]=reason
    # The abandonment view (kittrial-5bb.274 item 1). The record is proved only by
    # its own native comment - text, author and native time - and it must still
    # name exactly one native cut correction. Anything else is flagged, never
    # treated as proof and never turned into a lifecycle reason.
    for key, entry in entries.items():
        payload = entry['payload']
        if payload['operation'] != 'abandon':
            continue
        binding = payload['binding']
        tasks = issues.get(binding['task'], [])
        own = None
        if len(tasks) == 1:
            matches = [c for c in tasks[0].get('comments', [])
                       if str(c.get('id')) == str(entry['comment_id'])]
            own = matches[0] if len(matches) == 1 else None
        proved = bool(own is not None
                      and own.get('author') == payload['actor']
                      and own.get('created_at') == entry['created_at']
                      and own.get('text') == PREFIX + canonical_bytes(payload).decode())
        target = None
        if proved:
            targets = [c for c in native_comments if abandoned_target(payload, c)]
            target = targets[0] if len(targets) == 1 else None
        if not proved or target is None:
            unproved.append(key)
            continue
        valid[key] = entry
        abandon_targets[key] = str(target['id'])
    # kittrial-5bb.274 round 3: a cut ABANDONMENT - the process died after the
    # abandonment's OWN guarded native comment and before the store write - must
    # not be a dead end. The native comment is parsed (``allow_abandon``) and its
    # exact retry is named. While its cut is still open the orphan stays UNPAIRED:
    # the host store record is missing, so the audit reads incomplete and the
    # backup keeps refusing - failures stay closed. It is resolved by the exact
    # retry (same operation id/actor), or, when its operator has left the list, by
    # ANY listed operator issuing a fresh abandonment of the SAME cut under a new
    # operation id. Once another proved record has closed the cut the orphan reads
    # SUPERSEDED (never unpaired), the audit reads complete and backups resume.
    orphan_abandons = {}
    already_paired = {(e['payload']['binding']['task'], str(e['comment_id']))
                      for e in entries.values()}
    # A cut comment a PROVED abandonment already closed (kittrial-5bb.274 item 1):
    # another listed operator's fresh abandonment of the same cut is exactly the
    # documented recovery when the orphan's own operator has left the list.
    closed_by_abandon = {target: key for key, target in abandon_targets.items()}
    for comment in native_comments:
        if (comment['task'], comment['id']) in already_paired:
            continue
        payload = correction_payload(comment, allow_abandon=True)
        if payload is None or payload['operation'] != 'abandon':
            continue
        targets = [c for c in native_comments if abandoned_target(payload, c)]
        target = targets[0] if len(targets) == 1 else None
        later = None
        if target is not None:
            for key, entry in valid.items():
                candidate = entry['payload']
                if (candidate['operation'] in ('void', 'retract')
                        and candidate['operation_id'] == payload['target_void']
                        and candidate['binding'] == payload['binding']):
                    later = key
                    break
            if later is None:
                closer = closed_by_abandon.get(str(target['id']))
                if (closer is not None
                        and valid[closer]['payload']['operation_id'] != payload['operation_id']):
                    later = closer
        orphan_abandons[str(comment['id'])] = dict(
            payload=payload, target=str(target['id']) if target is not None else None,
            target_task=target['task'] if target is not None else None,
            superseded_by=later)
    retracted = set()
    from review_workflow import _recorded_at
    for key, entry in valid.items():
        payload = entry['payload']
        if payload['operation'] != 'retract':
            continue
        target = valid.get(payload['target_void'])
        if (target is None or target['payload']['operation'] != 'void' or target['payload']['binding'] != payload['binding']
                or payload['target_void'] in retracted
                or _recorded_at(entry['created_at']) < _recorded_at(target['created_at'])):
            reasons[payload['binding']['event_id']] = 'lifecycle-void-journal-mismatch'
            entry_reasons[key] = 'lifecycle-void-journal-mismatch'
        else:
            retracted.add(payload['target_void'])
    for key, entry in valid.items():
        payload = entry['payload']
        if payload['operation'] == 'void' and key not in retracted:
            reasons.setdefault(payload['binding']['event_id'], 'voided')
    # A native comment is bound by a valid abandonment twice over: the
    # abandonment's OWN comment (``comment_id``) and the cut comment its payload
    # names (``abandon_targets``). Binding the target is what lets backups resume.
    bound_comments={(e['payload']['binding']['task'],e['comment_id']) for e in entries.values()}
    for key, target_id in abandon_targets.items():
        bound_comments.add((entries[key]['payload']['binding']['task'], target_id))
    # A SUPERSEDED orphan abandonment (its own native comment exists, its cut has
    # since been closed by another proved record) is bound twice over too: its own
    # comment and the cut it names. A pending orphan (no later proved record) binds
    # neither, so it stays in ``incomplete`` and the backup keeps refusing.
    for comment_id, orphan in orphan_abandons.items():
        if orphan['superseded_by'] is None or orphan['target'] is None:
            continue
        task = orphan['payload']['binding']['task']
        bound_comments.add((task, comment_id))
        bound_comments.add((orphan['target_task'], orphan['target']))
    incomplete=[c for c in native_comments if (c['task'],c['id']) not in bound_comments]
    # kittrial-5bb.274 item 2: after a rollback to the released kit, the exact
    # retry of an abandoned correction is accepted there, so this family holds a
    # host entry for the very operation the abandonment names. The void in effect
    # WINS and the abandonment reads SUPERSEDED by that later host entry - a
    # defined, complete reading - never a duplicate. Only a PROVED abandonment can
    # be superseded; a hand-added record has no native abandonment comment and
    # stays damaged above.
    superseded = {}
    for key, entry in valid.items():
        payload = entry['payload']
        if payload['operation'] != 'abandon':
            continue
        later = sorted(other for other, candidate in entries.items()
                       if other != key and candidate['payload']['operation'] in ('void', 'retract')
                       and candidate['payload']['operation_id'] == payload['target_void']
                       and candidate['payload']['binding'] == payload['binding'])
        if later:
            superseded[key] = later[0]
    abandon_problems = list(journal.get('abandon_problems') or [])
    for key in sorted(unproved):
        abandon_problems.append(
            ('Lifecycle abandonment %s has no native comment by its operator %s'
             % (key, entries[key]['payload']['actor']))[:300])
    return dict(reasons=reasons, global_reason=UNKNOWN_JOURNAL if invalid else None,
                entries=entries, invalid=invalid, retracted=retracted,
                entry_reasons=entry_reasons,task_reasons=task_reasons,
                native_comments=native_comments,incomplete=incomplete,
                unproved_abandons=sorted(unproved),
                superseded_abandons=superseded, abandon_targets=abandon_targets,
                orphan_abandons=orphan_abandons,
                abandon_problems=abandon_problems)


class Snapshot(list):
    """Host context carried separately from untrusted native JSON fields."""
    def __init__(self, rows, correction_context):
        super().__init__(rows)
        self.lifecycle_corrections = correction_context


def snapshot(rows, project=None, journal=None):
    journal = read_journal(project) if journal is None else journal
    if (not journal['entries'] and not journal['invalid']
            and not journal.get('abandons') and not journal.get('abandon_problems')):
        try:
            if not native_correction_comments(rows):return rows
        except ValueError:pass
    return Snapshot(rows, context(rows, journal))


def operator_listed(operator, operators):
    """Whether a correction row's operator is on the configured operator list (kittrial-5bb.282).

    ``False`` when the list is known and the operator is not on it - the reviewer's case: a native
    abandonment comment written straight to bd by someone who is not a listed operator closes
    nothing, and once a proved record closes the cut it reads ``superseded`` with that operator, so
    the audit must say that the operator is not one this installation lists. ``None`` when the
    caller passed no list: unknown is not a claim about the person.
    """
    if operators is None or operator is None:
        return None
    return operator in set(operators)


def correction_audit(correction, operators=None):
    """Every host record and unpaired native correction, without conferring authority.

    ``operators`` is the installation's configured operator list, used only to answer
    ``operator_listed`` on each row; a caller that does not pass it reads ``None`` there.
    """
    entries=correction.get('entries',{})
    retracted=correction.get('retracted',set())
    native={(c['task'],c['id']):c for c in correction.get('native_comments',[])}
    # kittrial-5bb.274 items 1 and 2: name an abandonment that has no native
    # comment of its own by its operator, and read a PROVED abandonment an older
    # kit superseded on a rollback as ``superseded`` (never as damage).
    unproved=set(correction.get('unproved_abandons') or [])
    superseded=dict(correction.get('superseded_abandons') or {})
    targets=dict(correction.get('abandon_targets') or {})
    result=[]
    for key,entry in entries.items():
        payload=entry['payload'];binding=payload['binding']
        comment=native.get((binding['task'],entry['comment_id']))
        problem=correction.get('entry_reasons',{}).get(key)
        if problem or (payload['operation']=='abandon' and key in unproved):
            status='damaged'
        elif payload['operation']=='abandon' and key in superseded:
            status='superseded'
        elif payload['operation']=='retract':
            status='retraction'
        elif payload['operation']=='abandon':
            status='abandoned'
        else:
            status='retracted' if key in retracted else 'in-effect'
        item=dict(operation_id=key,operation=payload['operation'],task=binding['task'],
                  event_id=binding['event_id'],operator=payload['actor'],
                  created_at=entry['created_at'],reason=payload['reason'],evidence=payload['evidence'],
                  target_void=payload['target_void'],comment_id=entry['comment_id'],status=status,
                  journaled=True,native_author=comment['author'] if comment else None,
                  native_time=comment['created_at'] if comment else None,
                  operator_listed=operator_listed(payload['actor'],operators))
        if payload['operation']=='abandon':
            item['native_operator_proof']=(comment is not None
                                           and comment['author']==payload['actor']
                                           and comment.get('text')==PREFIX+canonical_bytes(payload).decode())
            item['target_comment_id']=targets.get(key)
            item['duplicate_host_entry']=key in superseded
            if key in superseded:
                item['superseded_by']=superseded[key]
        if problem:item['classification_reason']=problem
        elif key in unproved:item['classification_reason']='abandonment-without-native-operator-proof'
        if key in retracted:
            item['retracted_by']=[k for k,e in entries.items() if e['payload']['operation']=='retract'
                                  and e['payload']['target_void']==key
                                  and k not in correction.get('entry_reasons',{})]
        result.append(item)
    for comment in correction.get('incomplete',[]):
        try:
            payload=validate_payload(record_json.loads(comment['text'][len(PREFIX):]),
                                     allow_abandon=True)
        except (ValueError,TypeError):
            payload=None
        binding=payload['binding'] if payload else {}
        item=dict(operation_id=payload['operation_id'] if payload else None,
                  operation=payload['operation'] if payload else None,task=comment['task'],
                  event_id=binding.get('event_id'),operator=payload['actor'] if payload else None,
                  created_at=comment['created_at'],reason=payload['reason'] if payload else None,
                  evidence=payload['evidence'] if payload else [],
                  target_void=payload['target_void'] if payload else None,
                  comment_id=comment['id'],status='incomplete',journaled=False,
                  native_author=comment['author'],native_time=comment['created_at'],
                  classification_reason='native-correction-without-host-entry' if payload else
                                        'native-correction-payload-unclassifiable',
                  operator_listed=operator_listed(payload['actor'] if payload else None,operators))
        # kittrial-5bb.274 round 3: an orphan (cut) abandonment comment now parses,
        # so its row names the exact retry - the operation identity and actor of the
        # abandonment whose store write never happened - and the cut comment it
        # names. The command that completes it is the same
        # ``lifecycle-event-abandon`` call with ``--operation-id`` set to
        # ``retry_operation_id`` and ``--actor`` to ``retry_operator``.
        if payload is not None and payload['operation']=='abandon':
            orphan=(correction.get('orphan_abandons') or {}).get(str(comment['id'])) or {}
            item['retry_operation_id']=payload['operation_id']
            item['retry_operator']=payload['actor']
            item['native_operator_proof']=None
            if orphan.get('target') is not None:
                item['target_comment_id']=orphan['target']
        result.append(item)
    # A SUPERSEDED orphan abandonment has no host entry, so it is not in
    # ``incomplete`` and would otherwise vanish from the audit. It reads
    # ``superseded`` - a defined, complete reading - and names the later proved
    # record that closed the cut it names.
    represented={(c.get('task'),str(c.get('comment_id'))) for c in result}
    for comment_id,orphan in (correction.get('orphan_abandons') or {}).items():
        if not orphan.get('superseded_by') or orphan.get('target') is None:
            continue
        native_comment=native.get((orphan['payload']['binding']['task'],comment_id))
        if native_comment is None or (native_comment['task'],native_comment['id']) in represented:
            continue
        payload=orphan['payload'];binding=payload['binding']
        result.append(dict(operation_id=payload['operation_id'],operation='abandon',
                           task=binding['task'],event_id=binding['event_id'],operator=payload['actor'],
                           created_at=native_comment['created_at'],reason=payload['reason'],
                           evidence=payload['evidence'],target_void=payload['target_void'],
                           comment_id=comment_id,status='superseded',journaled=False,
                           native_author=native_comment['author'],native_time=native_comment['created_at'],
                           native_operator_proof=True,target_comment_id=orphan['target'],
                           duplicate_host_entry=True,superseded_by=orphan['superseded_by'],
                           retry_operation_id=payload['operation_id'],retry_operator=payload['actor'],
                           classification_reason=None,
                           operator_listed=operator_listed(payload['actor'],operators)))
    return sorted(result,key=lambda x:(str(x['created_at'] or ''),str(x['task'] or ''),
                                       str(x['operation_id'] or ''),str(x['comment_id'])))


def audit(rows, operators=None, owners=None, limit=50, offset=0):
    """Read-only bounded event page; attribution/approval evidence is not fraud detection.

    Historical principal ownership cannot be proved by today's registry. The
    approval column therefore names its native-author/approval-assignee snapshot
    basis explicitly, and ambiguous timestamps or missing snapshots are unknown.
    """
    import lifecycle
    import review_workflow as reviews
    import web_lifecycle_marks as marks
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError('Lifecycle audit needs limit 1..100 and nonnegative offset')
    correction=(rows.lifecycle_corrections if isinstance(rows,Snapshot) else
                context(rows,dict(entries={},invalid=[],present=False)))
    events = lifecycle.classified_events(rows if isinstance(rows,Snapshot) else Snapshot(rows,correction))
    issues = {r['id']: r for r in rows if isinstance(r, dict) and r.get('id')
              and r.get('issue_type') != 'event' and not r.get('malformed')}
    native_events = [r for r in rows if isinstance(r, dict)
                     and (r.get('issue_type') == 'event' or record_json.selected(r, types=['event']))]
    classified = {e['id'] for e in events}
    unclassifiable = sum(1 for row in native_events if row.get('id') not in classified)
    ordered = sorted(events, key=lambda e: (e['task'], e['dimension'],
                     e['order'] if e['order'] is not None else -1, e['id']))
    corrections=correction_audit(correction,operators)
    page_total=max(len(ordered),len(corrections))
    if offset > page_total:
        raise ValueError('Lifecycle audit offset exceeds its total')
    items = []
    questionable_total=0
    unclassifiable_total=0
    loaded_owners = []
    def once_owners():
        if not loaded_owners:
            loaded_owners.append(reviews.owner_map(owners))
        return loaded_owners[0]
    native_by_id = {row.get('id'): row for row in native_events if isinstance(row, dict)}
    for index,event in enumerate(ordered):
        issue = issues.get(event['task'], {})
        payload = event['payload']
        recorder = payload['actor'] if payload else next(
            (r.get('created_by') for r in native_events if r.get('id') == event['id']), None)
        stamp = reviews._recorded_at(event['created_at'])
        approval = None
        reason = None
        try:
            operator = reviews.listed_operator_actor(recorder, operators, once_owners)
        except (ValueError, OSError):
            operator = None
        marked = marks.snapshot_eligible(rows, native_by_id.get(event['id']), operators=[])
        if payload is not None and stamp is not None:
            preceding = []
            ambiguous = False
            for comment in issue.get('comments', []):
                if not isinstance(comment.get('text'), str) or not comment['text'].startswith(
                        (reviews.PREFIX, reviews.recovery.PREFIX, reviews.REVERT_PREFIX)):
                    continue
                at = reviews._recorded_at(comment.get('created_at'))
                if at is None or at == stamp:
                    ambiguous = True
                elif at < stamp:
                    preceding.append(comment)
            if not ambiguous:
                try:
                    history = reviews.history(dict(issue, comments=preceding), operators=operators)
                    view = reviews.projection(*history, closed=issue.get('status') == 'closed')
                    contribution = view.get('contribution')
                    decision = reviews.approving_entry(history[0], contribution['comment_id']) if contribution else None
                    if contribution is None:
                        approval = False
                    elif contribution['commit'].lower() != payload['scope']['source_commit'].lower():
                        approval = False
                    elif view.get('review_state') != 'awaiting-integration' or decision is None:
                        approval = False
                    else:
                        snapshot_owner = decision[0].get('assignee_at_approval')
                        if not isinstance(snapshot_owner, str) or not snapshot_owner:
                            reason = 'approval-assignee-history-unavailable'
                        else:
                            approval = (reviews.author_key(decision[1].get('author')) != reviews.author_key(contribution.get('author'))
                                        and reviews.author_key(decision[1].get('author')) != reviews.author_key(snapshot_owner))
                except (ValueError, KeyError, TypeError):
                    reason = 'review-history-unclassifiable'
            else:
                reason = 'review-record-time-unclassifiable'
        else:
            reason = 'lifecycle-payload-or-time-unclassifiable'
        item = dict(task=event['task'], dimension=event['dimension'], event_id=event['id'],
                    native_time=event['created_at'], recorder=recorder,
                    current_assignee=issue.get('assignee'), recorder_is_current_assignee=bool(
                        recorder and recorder == issue.get('assignee')), recorder_is_listed_operator=operator,
                    independent_approval_at_recording=approval,
                    approval_basis='native-authors-and-approval-assignee-snapshot',
                    historical_principal_ownership_verified=False)
        if marked:
            item['recorder_has_web_coordinate_mark'] = True
            item['recorder_eligible'] = True
        if reason:
            item['classification_reason'] = reason
        if event.get('void_reason'):
            item['lifecycle_reason'] = event['void_reason']
        item['questionable'] = bool(reason or event.get('void_reason') or
            (not item['recorder_is_current_assignee'] and operator is not True and not marked) or
            (event['dimension'] in ('reviewed', 'integrated') and approval is not True))
        questionable_total+=item['questionable']
        unclassifiable_total+=bool(item.get('classification_reason'))
        if offset<=index<offset+limit:items.append(item)
    return dict(items=items, total=len(ordered), offset=offset, limit=limit,
                omitted_before=min(offset,len(ordered)), omitted_after=max(0, len(ordered) - offset - len(items)),
                next_offset=offset+limit if offset+limit<page_total else None,
                page_total=page_total,corrections=corrections[offset:offset+limit],
                correction_count=len(corrections),
                corrections_omitted_before=min(offset,len(corrections)),
                corrections_omitted_after=max(0,len(corrections)-offset-limit),
                in_effect_void_count=sum(c['status']=='in-effect' for c in corrections),
                retracted_void_count=sum(c['status']=='retracted' for c in corrections),
                incomplete_correction_count=sum(c['status']=='incomplete' for c in corrections),
                damaged_correction_count=sum(c['status']=='damaged' for c in corrections),
                abandoned_correction_count=sum(c['status']=='abandoned' for c in corrections),
                unproved_abandonment_count=sum(
                    1 for c in corrections if c['operation']=='abandon'
                    and c.get('native_operator_proof') is False),
                duplicate_correction_count=sum(
                    1 for c in corrections if c.get('duplicate_host_entry')),
                superseded_abandonment_count=sum(c['status']=='superseded' for c in corrections),
                family_reason=correction.get('global_reason'),
                family_problems=correction.get('invalid',[]),
                abandon_problems=correction.get('abandon_problems',[]),
                correction_status=('damaged' if correction.get('global_reason') or correction.get('entry_reasons')
                                   or correction.get('abandon_problems')
                                   else 'incomplete' if correction.get('incomplete') else 'complete'),
                questionable_fact_count=questionable_total,
                questionable_native_record_count=questionable_total+unclassifiable,
                unclassifiable_classification_count=unclassifiable_total,
                unclassifiable_native_event_count=unclassifiable,
                unclassifiable_fact_count=sum(e['payload'] is None or reviews._recorded_at(e['created_at']) is None
                                              for e in ordered),
                unclassifiable_in_page=sum(bool(item.get('classification_reason')) for item in items),
                questionable_in_page=sum(item['questionable'] for item in items),
                meaning='Questionable evidence for operator review, not a finding of fraud. Historical '
                        'principal ownership is not proved by the current registry.')


def _writable(directory):
    """Use the existing real pre-write probe, with this family's recovery name."""
    import review_workflow
    try:
        return review_workflow.revert_journal_writable(directory)
    except ValueError as error:
        raise ValueError(str(error).replace(review_workflow.JOURNAL_DIR, DIRECTORY)) from None


def publish(project, entry):
    from coordination import atomic
    validate_entry(entry)
    directory = Path(project) / DIRECTORY
    if directory.is_symlink():
        raise ValueError('Lifecycle void journal is a symlink')
    directory.mkdir(exist_ok=True)
    _writable(directory)
    target = directory / (entry['sha256'] + '.json')
    if target.is_symlink() or target.with_suffix('.tmp').is_symlink():
        raise ValueError('Lifecycle void journal record is a symlink')
    atomic(target, entry)
    return target


def _find_native_correction(rows, actor, payload):
    """The native comments already carrying this exact operation identity.

    Zero or one comment: a second comment with the same identity, or one that
    names different content or actor, is refused. Shared by a void/retraction
    (:func:`apply`) and an abandonment (:func:`abandon`), so the exact retry of
    either reconciles instead of writing twice.
    """
    binding = payload['binding']
    tasks = [r for r in rows if r.get('id') == binding['task']]
    if len(tasks) != 1 or tasks[0].get('issue_type') == 'event':
        raise ValueError('Lifecycle correction task is missing or ambiguous')
    found = []
    for comment in tasks[0].get('comments', []):
        raw = comment.get('text', '')
        if not isinstance(raw, str) or not raw.startswith(PREFIX):
            continue
        try:
            other = record_json.loads(raw[len(PREFIX):])
            validate_payload(other, allow_abandon=(payload['operation'] == 'abandon'))
        except (ValueError, TypeError):
            continue
        if other['operation_id'] == payload['operation_id']:
            if other != payload or comment.get('author') != actor:
                raise ValueError('Lifecycle correction operation identity already names different content/actor')
            found.append(comment)
    if len(found) > 1:
        raise ValueError('Lifecycle correction operation identity has ambiguous native comments')
    return found


def _write_native_correction(run, actor, payload):
    """The ONE guarded native correction write, shared by a void and an abandonment.

    Writes exactly one prefixed comment and then binds the comment the native
    store actually kept (a read back by id), never the transient create reply, so
    the host record can always name a real native comment. A failure here is
    explicitly uncertain and names the stable operation key; the exact retry
    reuses that comment through :func:`_find_native_correction`.
    """
    binding = payload['binding']
    body = PREFIX + canonical_bytes(payload).decode()
    try:
        comment = json.loads(run(['comments', 'add', binding['task'], body, '--json']))
        created_id = str(comment['id'])
        raw = run(['comments', binding['task'], '--json'])
        if len(raw.encode('utf-8')) > MAX_FAMILY_BYTES:
            raise ValueError('Native correction comment read exceeds its byte bound')
        stored = record_json.loads(raw)
        if not isinstance(stored, list):
            raise ValueError('Native correction comment read is not a list')
        stored = [c for c in stored if isinstance(c, dict) and str(c.get('id')) == created_id]
        if (len(stored) != 1 or stored[0].get('author') != actor or stored[0].get('text') != body):
            raise ValueError('Native correction comment cannot be bound after its write')
        return stored[0]
    except Exception as error:
        raise ValueError('Lifecycle correction outcome is uncertain for operation ' + payload['operation_id']
                         + '; reconcile the native comment before retry: ' + str(error)[:300]) from None


def apply(rows, actor, payload, run, project, operators=None, owners=None, enabled=False):
    """Caller holds the project and deployment-config locks; native-first exact retry.

    A publication failure after the one native effect is explicitly uncertain and
    names the stable operation key. Its exact retry republishes that comment's
    missing record, following the existing integration-revert repair pattern.
    """
    from review_workflow import listed_operator_actor
    validate_payload(payload)
    if actor != payload['actor'] or not listed_operator_actor(actor, operators, owners):
        raise ValueError('Lifecycle correction requires a currently listed operator or its bound actor')
    if not enabled:
        raise ValueError('lifecycle_void_writes is off; after restoring support on every installation, '
                         'an operator enables it with admin.py lifecycle_void_writes on --actor OPERATOR; nothing was changed')
    binding = payload['binding']; targets = [r for r in rows if r.get('id') == binding['event_id']]
    if len(targets) != 1 or event_binding(targets[0]) != binding:
        raise ValueError('Lifecycle correction target changed; operator reconciliation required')
    tasks = [r for r in rows if r.get('id') == binding['task']]
    if len(tasks) != 1 or tasks[0].get('issue_type') == 'event':
        raise ValueError('Lifecycle correction task is missing or ambiguous')
    journal = read_journal(project)
    if journal['invalid']:
        raise ValueError('Lifecycle void journal cannot be read; operator repairs it before a write')
    state = context(rows, journal)
    if payload['operation'] == 'retract':
        prior = journal['entries'].get(payload['target_void'])
        if prior is None or prior['payload']['operation'] != 'void' or prior['payload']['binding'] != binding:
            raise ValueError('Retraction must name this exact host-issued void operation')
        if state['reasons'].get(binding['event_id']) not in (None, 'voided'):
            raise ValueError('Void binding is damaged; operator reconciliation required before retraction')
    found = _find_native_correction(rows, actor, payload)
    # kittrial-5bb.271 item 2, the rule this kit states: an abandoned correction
    # is FINAL. apply() therefore consults the abandonment of the operation it
    # binds - whether the record is in the store beside the family or in a legacy
    # family member - and refuses the exact retry instead of re-applying a
    # correction the operator closed. A fresh void under a new key is the way.
    # kittrial-5bb.274 item 2: the ONE exception is an abandonment an older kit
    # already superseded by re-opening the correction on a rollback. The void in
    # effect wins there, so the exact retry reconciles with that later entry
    # instead of being refused as abandoned.
    superseded = set((state.get('superseded_abandons') or {}))
    closers = list((journal.get('abandons') or {}).items())
    closers += [(key, entry) for key, entry in journal['entries'].items()
                if entry['payload']['operation'] == 'abandon']
    abandoned = [record for key, record in closers
                 if record['payload']['target_void'] == payload['operation_id']
                 and key not in superseded]
    if abandoned:
        closed = abandoned[0]
        raise ValueError('Lifecycle correction operation ' + payload['operation_id'] + ' was abandoned by '
                         + closed['payload']['actor'] + ' at ' + closed['created_at']
                         + '; an abandoned correction is final and is never put into effect. Publish a fresh '
                           'void under a new operation identity, or reconcile with that operator.')
    if payload['operation']=='void':
        other_voids=[key for key,entry in journal['entries'].items()
                     if key!=payload['operation_id'] and entry['payload']['operation']=='void'
                     and entry['payload']['binding']['event_id']==binding['event_id']
                     and key not in state['retracted']]
        if other_voids:
            raise ValueError('This lifecycle event is already voided under operation '+sorted(other_voids)[0]
                             +'; this operation cannot publish a second void. Reconcile or retract the existing operation.')
    if not found:
        if payload['operation'] == 'void' and binding['event_id'] in state['reasons']:
            raise ValueError('This lifecycle event is already voided or its correction is damaged')
        if payload['operation'] == 'retract' and payload['target_void'] in state['retracted']:
            raise ValueError('This lifecycle void was already retracted')
    # The bounded reader must be able to read every record the writer emits.
    # Keep room to retract every outstanding void rather than filling the family
    # with corrections that its current operators could no longer retract.
    estimated = dict(schema_version=1, payload=payload, sha256=content_hash(payload),
                     comment_id='x' * 300, created_at='x' * 100)
    estimated_bytes = len(entry_bytes(estimated))
    if estimated_bytes > MAX_RECORD_BYTES:
        raise ValueError('Lifecycle correction plus native binding exceeds the journal record byte bound; no native write attempted')
    if payload['operation_id'] not in journal['entries']:
        active = sum(entry['payload']['operation'] == 'void' and key not in state['retracted']
                     for key, entry in journal['entries'].items())
        reserved = active + (1 if payload['operation'] == 'void' else -1)
        total_bytes = journal['bytes']
        if (len(journal['entries']) + 1 + reserved > MAX_RECORDS
                or total_bytes + estimated_bytes + max(0, reserved) * MAX_RECORD_BYTES > MAX_FAMILY_BYTES):
            raise ValueError('Lifecycle void journal capacity keeps space for outstanding retractions; '
                             'no native write attempted. An operator retains the existing journal and uses '
                             'a restore-capable kit with larger validated bounds before adding corrections.')
    directory = Path(project) / DIRECTORY
    if directory.is_symlink():
        raise ValueError('Lifecycle void journal is a symlink; no native write attempted')
    directory.mkdir(exist_ok=True)
    target = directory / (content_hash(payload) + '.json')
    temporary = target.with_suffix('.tmp')
    if (target.is_symlink() or temporary.is_symlink()
            or temporary.exists() and not temporary.is_file()):
        raise ValueError('Lifecycle void publication path is not a regular host path; no native write attempted')
    _writable(directory)
    if found:
        comment = found[0]
    else:
        # bd's create reply carries its pre-storage nanosecond clock; pinned Dolt
        # persists the comment time at second precision. The shared guarded write
        # binds the actual stored comment returned by a native read.
        comment = _write_native_correction(run, actor, payload)
    try:
        entry = dict(schema_version=1, payload=payload, sha256=content_hash(payload),
                     comment_id=str(comment['id']), created_at=comment['created_at'])
        validate_entry(entry)
        publish(project, entry)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise ValueError('Lifecycle correction native effect may exist, but its host record is uncertain for operation '
                         + payload['operation_id'] + '; retry the exact payload/actor to persist the entry: '
                         + str(error)[:300]) from None
    return dict(operation_id=payload['operation_id'], comment_id=entry['comment_id'],
                event_id=binding['event_id'], task=binding['task'], operation=payload['operation'],
                journal_sha256=entry['sha256'], reconciled=bool(found))


def abandon(rows, actor, payload, run, project, operators=None, owners=None, enabled=False,
            comment_id=None):
    """Close one unpaired native correction comment without recreating its words.

    Host-only and audited. A listed operator (or its bound actor) names an existing
    correction comment that has no host entry (a cut before publication, or a void
    whose entry was lost). kittrial-5bb.274 item 1: the abandonment writes its OWN
    native comment - the proof - through the SAME guarded native write as a void
    (:func:`_write_native_correction`), whose payload names the abandoned comment
    (``target_void`` and ``binding``), the operator, the reason and the evidence;
    the native time is the comment's own. Then it writes the host record beside the
    family (:data:`ABANDON_FILE`, never a family member, so a family that holds an
    abandonment stays readable by the released F0 kit). The abandoned comment is
    never touched, deleted or re-written.

    ANY listed operator or its bound actor may abandon: the proof is the
    abandonment's own comment, so the author of the cut need not still be listed
    (the .271 rule). A correction whose comment already has a host entry (an issued
    void or retraction, in effect or retracted) is refused. ``context`` proves the
    record ONLY from that own comment, so a hand-added store record has no proof and
    reads damaged; it never turns an abandonment into a void reason, and no
    abandonment record ever changes a lifecycle fact.

    An abandonment is FINAL: the exact retry of the abandoned operation is refused
    by :func:`apply` - unless an older kit already re-opened it on a rollback, where
    the void in effect wins and the abandonment reads superseded (kittrial-5bb.274
    item 2). A fresh void under a new operation identity is the way to void the
    event otherwise. The exact retry of the *abandonment* with the same
    ``operation_id`` reconciles the already-written native comment and store record.
    """
    from review_workflow import listed_operator_actor
    validate_payload(payload, allow_abandon=True)
    if payload['operation'] != 'abandon':
        raise ValueError('Not an abandonment payload')
    if actor != payload['actor'] or not listed_operator_actor(actor, operators, owners):
        raise ValueError('Lifecycle abandonment requires a currently listed operator or its bound actor')
    if not enabled:
        raise ValueError('lifecycle_void_writes is off; after restoring support on every installation, '
                         'an operator enables it with admin.py lifecycle_void_writes on --actor OPERATOR; nothing was changed')
    binding = payload['binding']
    journal = read_journal(project)
    if journal['invalid']:
        raise ValueError('Lifecycle void journal cannot be read; operator repairs it before a write')
    if journal['abandon_problems']:
        raise ValueError('Lifecycle abandonment store cannot be read; operator repairs it before a write: '
                         + journal['abandon_problems'][0])
    prior = (journal.get('abandons') or {}).get(payload['operation_id'])
    if prior is None:
        legacy = journal['entries'].get(payload['operation_id'])
        prior = legacy if legacy is not None and legacy['payload']['operation'] == 'abandon' else None
    if prior is not None:
        if prior['payload'] != payload:
            raise ValueError('Lifecycle correction operation identity already names different content/actor')
        return dict(operation_id=payload['operation_id'], comment_id=prior['comment_id'],
                    event_id=binding['event_id'], task=binding['task'], operation='abandon',
                    journal_sha256=prior['sha256'], reconciled=True)
    native = native_correction_comments(rows)
    candidates = [c for c in native if comment_id is None or c['id'] == str(comment_id)]
    matches = []
    for comment in candidates:
        original = correction_payload(comment)
        if (original is not None and original['operation'] in ('void', 'retract')
                and original['operation_id'] == payload['target_void']
                and original['binding'] == binding
                and comment['task'] == binding['task']):
            matches.append((comment, original))
    if len(matches) != 1:
        raise ValueError('Lifecycle abandonment target is missing or ambiguous; name an incomplete native '
                         'correction comment that has no host entry')
    comment, original = matches[0]
    # kittrial-5bb.274 item 1: ANY listed operator or its bound actor may abandon.
    # The cut's author need not still be listed: the abandonment is proved by the
    # comment written below by the ABANDONING actor, not by the cut's author.
    stored_entries = list(journal['entries'].values()) + list((journal.get('abandons') or {}).values())
    conflict = None
    for entry in stored_entries:
        candidate = entry['payload']
        if (candidate['operation'] in ('void', 'retract')
                and str(entry['comment_id']) == str(comment['id'])):
            conflict = candidate['operation_id']
            break
        if (candidate['operation'] == 'abandon'
                and candidate['target_void'] == original['operation_id']
                and candidate['binding'] == binding):
            conflict = candidate['operation_id']
            break
    if conflict is not None:
        # kittrial-5bb.274 round 3: the exact retry of a cut ABANDONMENT whose own
        # native comment was written but whose store record was lost. When a later
        # PROVED record has already closed that cut (another listed operator's
        # fresh abandonment, or a rollback re-opening the correction), there is
        # nothing left to write: reconcile with that later record, which is what
        # the audit already reads as ``superseded``. A target with no native
        # abandonment comment of its own (a fresh key, a hand-typed record) is
        # still refused below.
        own = _find_native_correction(rows, actor, payload)
        if own:
            state = context(rows, journal)
            if (conflict not in state['entry_reasons']
                    and conflict not in state['unproved_abandons']):
                return dict(operation_id=payload['operation_id'],
                            comment_id=str(own[0]['id']),
                            target_comment_id=str(comment['id']),
                            event_id=binding['event_id'], task=binding['task'],
                            operation='abandon', superseded_by=conflict, reconciled=True)
        raise ValueError('That native correction comment already has a host entry; an issued void or '
                         'retraction is never abandoned')
    # Bound the store BEFORE the native write: after it, a refusal would leave the
    # native effect uncertain. The estimate is a conservative upper bound.
    estimated = dict(schema_version=1, payload=payload, sha256=content_hash(payload),
                     comment_id='x' * 300, created_at='x' * 100)
    if len(entry_bytes(estimated)) > MAX_RECORD_BYTES:
        raise ValueError('Lifecycle abandonment plus native binding exceeds the store record byte bound; '
                         'no native write attempted')
    stored = journal.get('abandons') or {}
    if len(stored) + 1 > MAX_RECORDS or journal['bytes'] + len(entry_bytes(estimated)) > MAX_FAMILY_BYTES:
        raise ValueError('Lifecycle abandonment store capacity is exhausted; no native write attempted. '
                         'An operator retains the store and uses a restore-capable kit with larger validated '
                         'bounds before adding abandonments.')
    found = _find_native_correction(rows, actor, payload)
    comment_record = found[0] if found else _write_native_correction(run, actor, payload)
    try:
        entry = dict(schema_version=1, payload=payload, sha256=content_hash(payload),
                     comment_id=str(comment_record['id']), created_at=comment_record['created_at'])
        validate_entry(entry, allow_abandon=True)
        publish_abandon(project, entry)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise ValueError('Lifecycle abandonment native effect may exist, but its host record is uncertain for '
                         'operation ' + payload['operation_id']
                         + '; retry the exact payload/actor to persist the entry: ' + str(error)[:300]) from None
    return dict(operation_id=payload['operation_id'], comment_id=entry['comment_id'],
                target_comment_id=str(comment['id']),
                event_id=binding['event_id'], task=binding['task'], operation='abandon',
                journal_sha256=entry['sha256'], reconciled=bool(found))
