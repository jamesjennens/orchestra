"""Host-issued authority for passed web integration facts.

This family never upgrades ordinary native/legacy facts. An authenticated
endpoint issues one mark after its passed integration write; native JSON and
present-day grants cannot issue or revoke a historical mark. Reads create no
files. Host filesystem access and an operator's restore remain trust boundaries.
"""
import json
import os
import re
import stat
from pathlib import Path

import record_json
from requirements import canonical_bytes, content_hash
from lifecycle_voids import Snapshot as CorrectionSnapshot, context as correction_context

DIRECTORY = '.web-lifecycle-marks'
MANIFEST = '.web-lifecycle-marks-manifest.json'
RESTORE_MARKER = '.web-lifecycle-marks-restore.json'
MAX_RECORD_BYTES = 16000
MAX_RECORDS = 1000
MAX_FAMILY_BYTES = 4 * 1024 * 1024
FIELDS = frozenset(('schema_version', 'project', 'task', 'event_id',
                    'payload_sha256', 'native_sha256', 'created_by',
                    'created_at', 'authority'))
ENTRY_FIELDS = frozenset(('schema_version', 'record', 'sha256'))
UNKNOWN = 'web-lifecycle-mark-family-unreadable'


class Snapshot(CorrectionSnapshot):
    """Host metadata lives on the snapshot, never in native JSON fields."""
    def __init__(self, rows, project, journal):
        correction = getattr(rows, 'lifecycle_corrections', None)
        if correction is None:
            correction = correction_context(rows, dict(entries={}, invalid=[], present=False))
        super().__init__(rows, correction)
        self.web_lifecycle_mark_project = Path(project).name
        self.web_lifecycle_mark_directory = str(project)
        self.web_lifecycle_mark_journal = journal
        if hasattr(rows, 'offline_corrections_consulted'):
            self.offline_corrections_consulted = rows.offline_corrections_consulted


def snapshot(rows, project, journal):
    return Snapshot(rows, project, journal)


def snapshot_eligible(rows, row, operators=None, owners=None):
    """The shared historical recorder decision, with only trusted host context."""
    return recorder_eligible(row, getattr(rows, 'web_lifecycle_mark_project', None),
                             operators, owners, getattr(rows, 'web_lifecycle_mark_journal', None))


def _identity(value, label):
    if (not isinstance(value, str) or len(value) > 300
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value)):
        raise ValueError('Invalid web lifecycle mark ' + label)
    return value


def route_for(project, task):
    _identity(project, 'project')
    _identity(task, 'task')
    return '/v1/projects/' + project + '/tasks/' + task + '/lifecycle/facts/integrated'


def entry_bytes(entry):
    return json.dumps(entry, ensure_ascii=False, sort_keys=True).encode('utf-8')


def validate_record(record):
    if (not isinstance(record, dict) or set(record) != FIELDS
            or type(record['schema_version']) is not int or record['schema_version'] != 1):
        raise ValueError('Invalid web lifecycle mark fields/version')
    for key in ('project', 'task', 'event_id'):
        _identity(record[key], key)
    for key in ('payload_sha256', 'native_sha256'):
        if not isinstance(record[key], str) or not re.fullmatch('[a-f0-9]{64}', record[key]):
            raise ValueError('Invalid web lifecycle mark ' + key)
    for key in ('created_by', 'created_at'):
        if (not isinstance(record[key], str) or not record[key].strip()
                or len(record[key]) > 300 or '\0' in record[key] or '\n' in record[key]):
            raise ValueError('Invalid web lifecycle mark ' + key)
    from review_workflow import _recorded_at
    if _recorded_at(record['created_at']) is None:
        raise ValueError('Invalid web lifecycle mark native timestamp')
    authority = record['authority']
    if authority != {'route': route_for(record['project'], record['task']),
                     'capability': 'coordinate', 'project': record['project']}:
        raise ValueError('Web lifecycle mark authority binding mismatch')
    if len(canonical_bytes(record)) > MAX_RECORD_BYTES:
        raise ValueError('Web lifecycle mark exceeds byte bound')
    return record


def validate_entry(entry, filename=None):
    if (not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS
            or type(entry['schema_version']) is not int or entry['schema_version'] != 1):
        raise ValueError('Invalid web lifecycle mark entry')
    validate_record(entry['record'])
    digest = content_hash(entry['record'])
    if entry['sha256'] != digest or (filename is not None and filename != digest + '.json'):
        raise ValueError('Web lifecycle mark hash/path mismatch')
    if len(entry_bytes(entry)) > MAX_RECORD_BYTES:
        raise ValueError('Web lifecycle mark entry exceeds byte bound')
    return entry


def bind_event(project, row, authority):
    """An exact native row, after the endpoint's verified passed write."""
    from lifecycle_voids import event_binding
    binding = event_binding(row)
    if (binding['dimension'] != 'integrated' or binding['value'] != 'passed'
            or binding['payload'] is None):
        raise ValueError('Web lifecycle mark needs a canonical passed integrated event')
    record = dict(schema_version=1, project=project, task=binding['task'],
                  event_id=binding['event_id'], payload_sha256=binding['payload_sha256'],
                  native_sha256=binding['native_sha256'], created_by=binding['created_by'],
                  created_at=binding['created_at'], authority=authority)
    return validate_record(record)


def matches(entry, project, row):
    try:
        validate_entry(entry)
        record = entry['record']
        return record == bind_event(project, row, record['authority'])
    except (ValueError, TypeError, KeyError):
        return False


def _regular_bytes(path):
    if path.is_symlink():
        raise ValueError('Web lifecycle mark is a symlink')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES:
            raise ValueError('Web lifecycle mark is not a bounded regular file')
        if os.name != 'nt' and stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError('Web lifecycle mark member is group- or world-writable')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(MAX_RECORD_BYTES + 1)
        if len(data) > MAX_RECORD_BYTES:
            raise ValueError('Web lifecycle mark exceeds byte bound')
        return data
    finally:
        os.close(fd)


def read_journal(project):
    result = dict(entries={}, invalid=[], present=False, bytes=0)
    if project is None:
        return result
    root = Path(project)
    if os.path.lexists(root / RESTORE_MARKER):
        result['invalid'].append('web-lifecycle-mark-restore-incomplete')
    path = root / DIRECTORY
    if not os.path.lexists(path):
        return result
    result['present'] = True
    try:
        if path.is_symlink() or not path.is_dir():
            raise ValueError('Web lifecycle mark family is not a real directory')
        if os.name != 'nt' and stat.S_IMODE(path.stat().st_mode) & 0o022:
            raise ValueError('Web lifecycle mark family is group- or world-writable')
        names = []
        with os.scandir(path) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > MAX_RECORDS:
                    raise ValueError('Web lifecycle mark family exceeds record bound')
        for name in sorted(names):
            if not re.fullmatch(r'[a-f0-9]{64}\.json', name):
                raise ValueError('Invalid web lifecycle mark path')
            blob = _regular_bytes(path / name)
            result['bytes'] += len(blob)
            if result['bytes'] > MAX_FAMILY_BYTES:
                raise ValueError('Web lifecycle mark family exceeds byte bound')
            entry = record_json.loads(blob.decode('utf-8'))
            validate_entry(entry, name)
            event = entry['record']['event_id']
            if event in result['entries']:
                raise ValueError('Conflicting web lifecycle mark event identity')
            result['entries'][event] = entry
    except (ValueError, OSError, UnicodeError) as error:
        result['invalid'].append(str(error))
    return result


def recorder_eligible(row, project_name, operators=None, owners=None, journal=None):
    """One operator/issued-mark decision; no current web role/grant lookup."""
    if not isinstance(row, dict):
        return False
    from review_workflow import listed_operator_actor
    try:
        if listed_operator_actor(row.get('created_by'), operators, owners):
            return True
    except (ValueError, OSError):
        pass
    if not isinstance(journal, dict) or journal.get('invalid'):
        return False
    entry = journal.get('entries', {}).get(row.get('id'))
    return entry is not None and matches(entry, project_name, row)


def issue(project, project_name, row, authority, *, enabled=False):
    """Caller has project lock and verified authority; no native effect here.

    Admission is also checked before the native effect by the web adapter. A
    publication failure after that effect is uncertain, not a zero-write refusal.
    """
    if enabled is not True:
        raise ValueError('Web lifecycle mark writer is OFF; an installation operator '
                         'enables web_lifecycle_mark_writes after restore readiness')
    record = bind_event(project_name, row, authority)
    entry = dict(schema_version=1, record=record, sha256=content_hash(record))
    validate_entry(entry)
    journal = read_journal(project)
    if journal['invalid']:
        raise ValueError('Web lifecycle mark family needs operator repair')
    old = journal['entries'].get(record['event_id'])
    if old is not None:
        if old != entry:
            raise ValueError('Web lifecycle mark event already has different bound content')
        return old
    if (len(journal['entries']) >= MAX_RECORDS
            or journal['bytes'] + len(entry_bytes(entry)) > MAX_FAMILY_BYTES):
        raise ValueError('Web lifecycle mark family exceeds its admission bounds')
    directory = Path(project) / DIRECTORY
    directory.mkdir(mode=0o700, exist_ok=True)
    from coordination import atomic
    atomic(directory / (entry['sha256'] + '.json'), entry, mode=0o600)
    if os.name != 'nt':
        # The file was fsynced by atomic; make both the rename and first family
        # creation durable on the deployment platform before issuing a receipt.
        for path in (directory, Path(project)):
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    return entry


def backup_files(project, rows=None):
    journal = read_journal(project)
    if journal['invalid']:
        raise ValueError('Cannot back up damaged web lifecycle mark family')
    if not journal['present']:
        return {}
    files = {DIRECTORY + '/' + entry['sha256'] + '.json': entry
             for entry in journal['entries'].values()}
    manifest = dict(schema_version=1, family=DIRECTORY, count=len(files),
                    bytes=sum(len(entry_bytes(entry)) for entry in files.values()),
                    files={name: content_hash(entry) for name, entry in sorted(files.items())})
    files[MANIFEST] = manifest
    validate_backup(files, rows)
    return files


def validate_backup(files, rows=None):
    if not isinstance(files, dict) or any(not isinstance(name, str) for name in files):
        raise ValueError('Invalid web lifecycle mark backup')
    members = {name: entry for name, entry in files.items() if name.startswith(DIRECTORY + '/')}
    manifest = files.get(MANIFEST)
    if not members and manifest is None:
        return None
    if (not isinstance(manifest, dict)
            or set(manifest) != {'schema_version', 'family', 'count', 'bytes', 'files'}
            or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1
            or manifest['family'] != DIRECTORY or type(manifest['count']) is not int
            or type(manifest['bytes']) is not int or manifest['count'] != len(members)
            or not 0 <= len(members) <= MAX_RECORDS):
        raise ValueError('Invalid web lifecycle mark backup manifest')
    size = 0
    events = set()
    native = {row.get('id'): row for row in rows or () if isinstance(row, dict)}
    for name, entry in members.items():
        if not re.fullmatch(re.escape(DIRECTORY) + r'/[a-f0-9]{64}\.json', name):
            raise ValueError('Invalid web lifecycle mark backup path')
        validate_entry(entry, name.partition('/')[2])
        record = entry['record']
        if record['event_id'] in events:
            raise ValueError('Conflicting web lifecycle mark backup event identity')
        events.add(record['event_id'])
        size += len(entry_bytes(entry))
        if rows is not None and not matches(entry, record['project'], native.get(record['event_id'])):
            raise ValueError('Web lifecycle mark backup does not match its native event')
    if (size > MAX_FAMILY_BYTES or size != manifest['bytes']
            or manifest['files'] != {name: content_hash(entry) for name, entry in sorted(members.items())}):
        raise ValueError('Web lifecycle mark backup member/byte/hash mismatch')
    return manifest


def restore_files(project, files, rows):
    """Trusted operator restore, preserving the exact original project descriptor.

    Hashes bind contents, not origin. A foreign project copy does not acquire
    the original project's mark authority. Caller holds the coordination lock.
    """
    from coordination import atomic
    root = Path(project)
    manifest = validate_backup(files)
    directory, marker = root / DIRECTORY, root / RESTORE_MARKER
    if directory.is_symlink() or marker.is_symlink() or marker.with_suffix('.tmp').is_symlink():
        raise ValueError('Web lifecycle mark restore paths must not be symlinks')
    if manifest is None:
        if os.path.lexists(marker):
            raise ValueError('Web lifecycle mark restore is already incomplete; preserve and reconcile its exact manifest')
        if os.path.lexists(directory) or os.path.lexists(marker):
            # Native restore may already have happened; silence would expose a
            # previous family's members as though they belonged to this backup.
            atomic(marker, dict(schema_version=1, missing_mark_manifest=True,
                                native_sha256=content_hash({'rows': list(rows)})), mode=0o600)
            raise ValueError('Backup has no web lifecycle mark manifest but this destination has a family; '
                             'preserve it and reconcile a complete mark-capable backup. Restore is incomplete.')
        return False
    identity = dict(schema_version=1, manifest_sha256=content_hash(manifest))
    if marker.exists() and record_json.loads(_regular_bytes(marker).decode('utf-8')) != identity:
        raise ValueError('Another web lifecycle mark restore is incomplete; reconcile its exact manifest first')
    if directory.exists() and not directory.is_dir():
        raise ValueError('Web lifecycle mark restore directory is not a directory')
    atomic(marker, identity, mode=0o600)
    validate_backup(files, rows)
    directory.mkdir(mode=0o700, exist_ok=True)
    old = read_journal(root)
    if any(problem != 'web-lifecycle-mark-restore-incomplete' for problem in old['invalid']):
        raise ValueError('Existing web lifecycle mark family is damaged; operator reconciliation required')
    expected = set(manifest['files'])
    for name in sorted(expected):
        target = root / name
        if target.is_symlink() or target.with_suffix('.tmp').is_symlink():
            raise ValueError('Web lifecycle mark restore member must not be a symlink')
        atomic(target, files[name], mode=0o600)
    for entry in old['entries'].values():
        name = DIRECTORY + '/' + entry['sha256'] + '.json'
        if name not in expected:
            (root / name).unlink()
    actual = read_journal(root)
    wanted = {entry['record']['event_id']: entry for name, entry in files.items() if name in expected}
    if actual['entries'] != wanted or actual['invalid'] != ['web-lifecycle-mark-restore-incomplete']:
        raise ValueError('Web lifecycle mark restore readback failed; incomplete marker retained')
    validate_backup(files, rows)
    if os.name != 'nt':
        for path in (directory, root):
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:os.fsync(fd)
            finally:os.close(fd)
    marker.unlink()
    if os.name != 'nt':
        fd = os.open(root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:os.fsync(fd)
        finally:os.close(fd)
    return True
