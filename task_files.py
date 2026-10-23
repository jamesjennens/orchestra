"""Files attached to a task: storage, limits, the host journal and the reserved records (kittrial-5bb.370).

Slice 1 of docs/TASK_FILES_DESIGN.md (kittrial-5bb.350, approved 2026-10-22). A coordinator hands
the assignee a file ("to the worker"); the assignee hands results back ("from the worker").

Where the bytes live. ``<project>/.task-files/`` beside the kit's other project-owned state,
never in the tracker or in source control. A blob is stored under a name the server makes from
its digest, ``<first two hex>/<sha256>``: the name a person gives is a label only. The folder,
and every folder inside it, is refused when it is a symlink or not a directory (as
``read_cache`` refuses a planted ``.read-cache``), so nothing is ever written or served through
a link.

What is authority. A file is attached to a task only when its HOST JOURNAL entry exists,
``.task-files/records/<first 32 hex of the task id's sha256>/<sha256 of the file>.json``, written by
:func:`apply` alone: on the canonical path that is the web service's own endpoint action under
the project's coordination lock, after live authority was verified (the assignee-followon
rule, coordinator's decision D1 on the plan). The reserved comment (``Kind: task-file-v1``,
``Kind: task-file-withdrawn-v1``) is the task's history record of it, not its authority: on
the SSH path a comment's author is self-declared, and a kit rolled back to before the prefix
was reserved accepts a raw one. Such a comment naming another task's digest (blobs are
content-addressed and shared within the project) therefore grants nothing. The journal lives
INSIDE ``.task-files`` so the files backup of a later slice carries it and the coordination
sidecar, whose restore list an older kit fixes, is untouched.

Every check here is made on the live state when it is asked: who may download is decided on
the task row read for that request, never from a list kept with the file.

This module is import-safe on every platform (no ``fcntl``): the caller holds the lock.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path

import record_json

FOLDER = '.task-files'
TMP = 'tmp'
RECORDS = 'records'
STEP = 64 * 1024
PREFIX = 'Kind: task-file-v1\n'
WITHDRAWN_PREFIX = 'Kind: task-file-withdrawn-v1\n'
TO_WORKER = 'to-worker'
FROM_WORKER = 'from-worker'
DIRECTIONS = (TO_WORKER, FROM_WORKER)
NAME_LIMIT = 128
NOTE_LIMIT = 1000
REASON_LIMIT = 1000
MEDIA_TYPE_LIMIT = 127
SHA256 = re.compile(r'[0-9a-f]{64}')
MEDIA_TYPE = re.compile(r'[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,62}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,62}')
OPAQUE = 'application/octet-stream'
#: The four operations of :func:`apply`. Which one a request is decides the capability its
#: live-authority descriptor must name (``http_authority.BLOCK_CAPABILITIES['task-file']``).
OPERATIONS = ('coordinator-record', 'assignee-record', 'coordinator-withdraw', 'author-withdraw')

MIB = 1024 * 1024
#: The design's 2c defaults. Each is one setting under ``task_files`` in deployment.private.json.
LIMIT_DEFAULTS = {
    'max_file_bytes': 25 * MIB,
    'max_files_per_task': 20,
    'max_task_bytes': 100 * MIB,
    'max_project_bytes': 2048 * MIB,
    'min_free_bytes': 1024 * MIB,
    'max_transfers': 4,
    'max_transfers_per_actor': 2,
    # r2 (review s1): one project may hold at most this many of the service's transfers, so one project's uploads
    # cannot shut every other project out.
    'max_transfers_per_project': 3,
    # r2 (review s1): a transfer may take at most transfer_grace_seconds + bytes / min_transfer_bytes_per_second; a
    # slower one (a client that trickles, or reads a download a byte at a time) is cut off and gives its slot back.
    # The defaults let 25 MiB take 60 s + 800 s.
    'min_transfer_bytes_per_second': 32 * 1024,
    'transfer_grace_seconds': 60,
}
#: The two that may be 0 (keep no disk free) without meaning "nothing at all".
LIMIT_ZERO_ALLOWED = ('min_free_bytes',)


class Refusal(ValueError):
    """One refused request: an HTTP status, a code and the one sentence a person is told."""

    def __init__(self, status, code, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


def setting(key):
    return 'task_files.' + key


def size_text(count):
    """Bytes as a person reads them: 31 MiB, 512 KiB, 900 bytes."""
    if count >= MIB:
        value = count / MIB
        return ('%d MiB' % value) if value == int(value) else ('%.1f MiB' % value)
    if count >= 1024:
        value = count / 1024
        return ('%d KiB' % value) if value == int(value) else ('%.1f KiB' % value)
    return '%d bytes' % count


# -- the limits -----------------------------------------------------------------------------------
def limits(root, warnings=None):
    """The 2c limits of this installation: ``deployment.private.json``'s ``task_files`` object.

    Absent, or a key absent, means the default. A value that is not a whole number (or is
    negative, or 0 where 0 would refuse everything) reads as the DEFAULT with a warning naming
    the setting, as ``admin.coordinator_rules`` reads a bad switch: a typo must never lift a
    limit, and must not stop every upload either. A file that cannot be read at all is the
    caller's configuration fault, raised as ``admin`` raises it.
    """
    values = dict(LIMIT_DEFAULTS)
    if root is None:
        return values
    marker = Path(root) / 'deployment.private.json'
    if not marker.is_file():
        return values
    from admin import audit_field, deployment_document
    section = deployment_document(marker).get('task_files')
    if section is None:
        return values

    def warn(message):
        if warnings is not None:
            warnings.append(message)
        else:
            import sys
            print(message, file=sys.stderr)

    if not isinstance(section, dict):
        warn('WARNING: deployment task_files is %s, not an object; every task-files limit keeps its default'
             % audit_field(section))
        return values
    for key, default in LIMIT_DEFAULTS.items():
        if key not in section:
            continue
        value = section[key]
        floor = 0 if key in LIMIT_ZERO_ALLOWED else 1
        if type(value) is not int or value < floor:
            warn('WARNING: deployment %s is %s, not a whole number of at least %d; it keeps its default %d'
                 % (setting(key), audit_field(value), floor, default))
            continue
        values[key] = value
    return values


# -- names ----------------------------------------------------------------------------------------
def clean_name(name):
    """The display name, checked: a basename of 1-128 characters, or :class:`Refusal` 422.

    The same rule ``http_service.validate_attachments`` applies (no ``/`` or ``\\``, no control
    character, not ``.`` or ``..``), plus no leading or trailing space and no DEL. It is a label
    only: nothing is ever stored or opened under it.
    """
    if not isinstance(name, str) or not name:
        raise Refusal(422, 'invalid', 'Give the file a name (?name=).')
    if len(name) > NAME_LIMIT:
        raise Refusal(422, 'invalid', 'A file name is at most %d characters; this one has %d.'
                      % (NAME_LIMIT, len(name)))
    if '/' in name or '\\' in name or name in ('.', '..') or name != name.strip() \
            or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise Refusal(422, 'invalid', 'A file name must be a plain name: no / or \\, no control '
                      'characters, not . or .., and no leading or trailing space.')
    plain(name, 'A file name')
    return name


def plain(value, label):
    # r2 (review s3): the kit's rule for text other people read (review_workflow.plain_text): no control, bidi,
    # zero-width, C1, tag or variation-selector characters. A name with a right-to-left override that shows as
    # invoiceexe.jpg is refused, and so is a note or a reason that hides text.
    from review_workflow import plain_text
    try:
        plain_text(value, label)
    except ValueError as refused:
        raise Refusal(422, 'invalid', str(refused) + '.') from None
    return value


def check_sha256(value):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise Refusal(422, 'invalid', 'A file is named by its sha256: 64 lower-case hexadecimal characters.')
    return value


def check_direction(value):
    if value not in DIRECTIONS:
        raise Refusal(422, 'invalid', 'direction must be to-worker or from-worker.')
    return value


def check_media_type(value):
    """The declared type: display metadata only (every download is served opaque)."""
    if value is None or value == '':
        return OPAQUE
    if not isinstance(value, str) or len(value) > MEDIA_TYPE_LIMIT or not MEDIA_TYPE.fullmatch(value):
        raise Refusal(422, 'invalid', 'media_type must look like type/subtype.')
    return value.lower()


def check_text(value, label, limit, required=False):
    if value is None or value == '':
        if required:
            raise Refusal(422, 'invalid', 'Give a %s.' % label)
        return None
    if not isinstance(value, str) or len(value) > limit or '\0' in value:
        raise Refusal(422, 'invalid', 'The %s is text of at most %d characters.' % (label, limit))
    return plain(value, 'The ' + label)


def task_key(task):
    # The journal folder of one task: 128 bits of the task id's digest. Short enough to keep a
    # journal path within Windows' 260 characters; an entry still names its task, and is honoured
    # only for that task, so two ids that shared a folder could not take each other's files.
    return hashlib.sha256(str(task).encode('utf-8')).hexdigest()[:32]


# -- folders --------------------------------------------------------------------------------------
UNUSABLE = ('The project\'s %s cannot be used (%s); nothing was stored or served. An operator '
            'must look at it.')


def _directory(path, create):
    """``path`` as a real directory (made when ``create``), or :class:`Refusal` 503.

    ``lstat`` only: a symlink is refused, never followed, and so is anything that is not a
    directory. Made with mode 0700.
    """
    try:
        info = os.lstat(str(path))
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(str(path), 0o700)
        except FileExistsError:
            pass
        except OSError as failed:
            raise Refusal(503, 'unavailable', UNUSABLE % (FOLDER, 'it could not be made: %s'
                                                          % (failed.strerror or type(failed).__name__)))
        info = os.lstat(str(path))
    if stat.S_ISLNK(info.st_mode):
        raise Refusal(503, 'unavailable', UNUSABLE % (FOLDER, '%s is a symlink' % _shown(path)))
    if not stat.S_ISDIR(info.st_mode):
        raise Refusal(503, 'unavailable', UNUSABLE % (FOLDER, '%s is not a folder' % _shown(path)))
    return path


def _shown(path):
    parts = Path(path).parts
    if FOLDER in parts:
        return '/'.join(parts[parts.index(FOLDER):])
    return Path(path).name


def folder(project, create=False):
    """``<project>/.task-files`` as a real directory, or None when absent and not ``create``."""
    if project is None:
        raise Refusal(501, 'not_implemented', 'Task files are not available on this server.')
    return _directory(Path(project) / FOLDER, create)


def _sub(project, name, create):
    base = folder(project, create)
    if base is None:
        return None
    return _directory(base / name, create)


def blob_path(project, sha256, create=False):
    """Where the blob of ``sha256`` is stored; its folder is checked like every other."""
    check_sha256(sha256)
    directory = _sub(project, sha256[:2], create)
    return None if directory is None else directory / sha256


def blob_size(project, sha256):
    """The stored blob's size, or None when it is not there (or is not a regular file)."""
    try:
        path = blob_path(project, sha256)
    except Refusal:
        raise
    if path is None:
        return None
    try:
        info = os.lstat(str(path))
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return info.st_size


def open_blob(project, sha256):
    """The stored blob opened for reading, refused unless it is a regular file (no link)."""
    path = blob_path(project, sha256)
    if path is None:
        raise Refusal(409, 'conflict', 'The stored file is missing: its record is here, its bytes are not.')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    try:
        descriptor = os.open(str(path), flags)
    except FileNotFoundError:
        raise Refusal(409, 'conflict', 'The stored file is missing: its record is here, its bytes are not.') from None
    except OSError:
        raise Refusal(503, 'unavailable', UNUSABLE % (FOLDER, 'a stored file cannot be opened')) from None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode) or os.path.islink(str(path)):
        os.close(descriptor)
        raise Refusal(503, 'unavailable', UNUSABLE % (FOLDER, 'a stored file is not a regular file'))
    return os.fdopen(descriptor, 'rb')


def referenced(project):
    """Every sha256 a journal entry of any task names, live or withdrawn (r2, review s2)."""
    records = _sub(project, RECORDS, False) if folder(project) is not None else None
    found = set()
    if records is None:
        return found
    with os.scandir(str(records)) as tasks:
        for task in tasks:
            if not task.is_dir(follow_symlinks=False):
                continue
            with os.scandir(task.path) as items:
                for item in items:
                    name = item.name
                    if name.endswith('.json') and SHA256.fullmatch(name[:-5]) and item.is_file(follow_symlinks=False):
                        found.add(name[:-5])
    return found


def project_bytes(project):
    """The bytes the project's records hold: every blob a journal entry names, live or withdrawn.

    r2 (review s2): a blob no entry names (an upload whose record was then refused, a process killed between the
    two) is not counted, so it can never use up the project's limit; it is removed where it is made (discard) or by
    the operator purge of a later slice.
    """
    total = 0
    for sha256 in referenced(project):
        size = blob_size(project, sha256)
        if size is not None:
            total += size
    return total


def discard(project, sha256):
    """Remove a blob that THIS upload stored when no journal entry names it (r2, review s2); True when removed."""
    if sha256 in referenced(project):
        return False
    path = blob_path(project, sha256)
    if path is None:
        return False
    try:
        if stat.S_ISREG(os.lstat(str(path)).st_mode):
            os.unlink(str(path))
            return True
    except FileNotFoundError:
        pass
    return False


def free_bytes(project):
    return shutil.disk_usage(str(project)).free


# -- the journal ----------------------------------------------------------------------------------
ENTRY_FIELDS = ('schema_version', 'task', 'sha256', 'name', 'bytes', 'media_type', 'direction',
                'note', 'by', 'at', 'record_comment', 'withdrawn')


def _valid_entry(entry, task, sha256):
    if not isinstance(entry, dict) or set(entry) != set(ENTRY_FIELDS) or entry.get('schema_version') != 1:
        return False
    if entry.get('task') != task or entry.get('sha256') != sha256:
        return False
    if type(entry.get('bytes')) is not int or entry['bytes'] < 0:
        return False
    if entry.get('direction') not in DIRECTIONS or not isinstance(entry.get('by'), str):
        return False
    try:
        clean_name(entry.get('name'))
    except Refusal:
        return False
    withdrawn = entry.get('withdrawn')
    return withdrawn is None or (isinstance(withdrawn, dict) and isinstance(withdrawn.get('by'), str))


def _entry_path(project, task, sha256, create=False):
    records = _sub(project, RECORDS, create)
    if records is None:
        return None
    directory = _directory(records / task_key(task), create)
    return None if directory is None else directory / (sha256 + '.json')


def read_entry(project, task, sha256):
    """The journal entry for this file on this task, or None (absent, a link, or not valid)."""
    check_sha256(sha256)
    path = _entry_path(project, task, sha256)
    if path is None:
        return None
    try:
        info = os.lstat(str(path))
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        entry = record_json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return None
    return entry if _valid_entry(entry, task, sha256) else None


def entries(project, task):
    """Every valid journal entry of ``task``, live and withdrawn, newest first.

    Read fresh from the folder on every call. An entry that is not valid, or whose file name
    does not match its digest, is left out: only what :func:`apply` wrote is honoured.
    """
    records = _sub(project, RECORDS, False) if folder(project) is not None else None
    if records is None:
        return []
    directory = records / task_key(task)
    try:
        info = os.lstat(str(directory))
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(info.st_mode):
        raise Refusal(503, 'unavailable', UNUSABLE % (FOLDER, '%s is not a folder' % _shown(directory)))
    found = []
    with os.scandir(str(directory)) as items:
        for item in items:
            name = item.name
            if not name.endswith('.json') or not SHA256.fullmatch(name[:-5]):
                continue
            entry = read_entry(project, task, name[:-5])
            if entry is not None:
                found.append(entry)
    found.sort(key=lambda e: (e.get('at') or '', e['sha256']), reverse=True)
    return found


def live(project, task):
    """The task's files a person may be shown: journal entries not withdrawn."""
    return [entry for entry in entries(project, task) if entry.get('withdrawn') is None]


def _write_entry(project, entry):
    path = _entry_path(project, entry['task'], entry['sha256'], create=True)
    if path.is_symlink():
        raise Refusal(503, 'unavailable', UNUSABLE % (FOLDER, 'a journal entry is a symlink'))
    temp = path.with_name('.t' + uuid.uuid4().hex[:16])
    data = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    descriptor = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp), str(path))
    except BaseException:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise
    return path


def view(entry, may_download=None):
    """What a reader is shown of one entry."""
    item = {key: entry.get(key) for key in ('sha256', 'name', 'bytes', 'media_type', 'direction',
                                            'by', 'at', 'note')}
    if may_download is not None:
        item['may_download'] = bool(may_download)
    return item


# -- receiving the bytes --------------------------------------------------------------------------
def check_room(project, task, length, limit, present=None):
    """Refuse, BEFORE a byte is read, an upload no limit allows; returns nothing.

    ``present`` is the task's live entries (read again when None). Each refusal is one sentence
    naming the limit and its setting (design 2c).
    """
    if length > limit['max_file_bytes']:
        raise Refusal(413, 'payload_too_large', 'Uploads stop at %s a file (%s); this one is %s.'
                      % (size_text(limit['max_file_bytes']), setting('max_file_bytes'), size_text(length)))
    present = live(project, task) if present is None else present
    if len(present) + 1 > limit['max_files_per_task']:
        raise Refusal(409, 'conflict', 'A task holds at most %d files (%s); this one has %d. Withdraw one, '
                      'or put the set in one archive file.'
                      % (limit['max_files_per_task'], setting('max_files_per_task'), len(present)))
    held = sum(entry['bytes'] for entry in present)
    if held + length > limit['max_task_bytes']:
        raise Refusal(413, 'payload_too_large', 'A task holds at most %s of files (%s); this one holds %s, '
                      'and this file is %s.' % (size_text(limit['max_task_bytes']), setting('max_task_bytes'),
                                                size_text(held), size_text(length)))
    stored = project_bytes(project)
    if stored + length > limit['max_project_bytes']:
        raise Refusal(413, 'payload_too_large', 'A project holds at most %s of files (%s); it holds %s, and '
                      'this file is %s.' % (size_text(limit['max_project_bytes']),
                                            setting('max_project_bytes'), size_text(stored), size_text(length)))
    free = free_bytes(project)
    if free - length < limit['min_free_bytes']:
        raise Refusal(507, 'insufficient_storage', 'The server keeps %s of its disk free (%s); storing this '
                      'file (%s) would leave %s.' % (size_text(limit['min_free_bytes']), setting('min_free_bytes'),
                                                     size_text(length), size_text(max(free - length, 0))))


def transfer_seconds(length, limit):
    """How long a transfer of ``length`` bytes may take (r2, review s1)."""
    return limit['transfer_grace_seconds'] + length / float(limit['min_transfer_bytes_per_second'])


def too_slow(length, limit):
    return Refusal(408, 'request_timeout', 'The transfer was slower than the server allows (%s a second after %d '
                   'seconds; %s and %s); nothing was stored.'
                   % (size_text(limit['min_transfer_bytes_per_second']), limit['transfer_grace_seconds'],
                      setting('min_transfer_bytes_per_second'), setting('transfer_grace_seconds')))


def receive(read, length, project, expected=None, step=None):
    """Store exactly ``length`` bytes from ``read`` (``read(n) -> bytes``); returns ``(sha256, length, created)``.

    ``created`` is True when this call put the blob in place (r2, review s2: the caller removes it again with
    :func:`discard` when its record is then refused). ``read`` may raise :class:`Refusal` itself (the caller's
    deadline, review s1); the temporary file is removed then too.

    The bytes go to a fresh temporary file (``O_EXCL``, mode 0600) in 64 KiB steps while they
    are hashed and counted; nothing is held whole. Fewer bytes than ``length`` (the client
    stopped, or its Content-Length lied) is refused, and nothing beyond ``length`` is ever read:
    the caller closes the connection after the answer, so bytes past a short Content-Length are
    never taken for a next request. ``expected`` (the client's ``X-Orchestra-SHA256``) must
    match the digest. Only a complete, matching file is renamed into place under its digest;
    the temporary file is removed on every other way out. A blob that is already stored is
    kept as it is (content-addressed).
    """
    if expected is not None:
        expected = check_sha256(expected.lower() if isinstance(expected, str) else expected)
    tmp = _sub(project, TMP, True)
    temp = tmp / ('upload-' + uuid.uuid4().hex)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(str(temp), flags, 0o600)
    digest = hashlib.sha256()
    count = 0
    step = step or STEP
    kept = False
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            while count < length:
                chunk = read(min(step, length - count))
                if not chunk:
                    raise Refusal(400, 'invalid', 'The upload ended after %d of the %d bytes its Content-Length '
                                  'announced; nothing was stored.' % (count, length))
                if count + len(chunk) > length:
                    raise Refusal(400, 'invalid', 'The upload sent more bytes than its Content-Length; nothing '
                                  'was stored.')
                handle.write(chunk)
                digest.update(chunk)
                count += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        sha256 = digest.hexdigest()
        if expected is not None and sha256 != expected:
            raise Refusal(422, 'invalid', 'The upload does not match its X-Orchestra-SHA256 (it is %s); nothing '
                          'was stored.' % sha256)
        target = blob_path(project, sha256, create=True)
        try:
            info = os.lstat(str(target))
        except FileNotFoundError:
            info = None
        if info is None:
            os.replace(str(temp), str(target))
            kept = True
        elif not stat.S_ISREG(info.st_mode):
            raise Refusal(503, 'unavailable', UNUSABLE % (FOLDER, 'a stored file is not a regular file'))
        return sha256, count, kept
    finally:
        if not kept:
            try:
                os.unlink(str(temp))
            except OSError:
                pass


# -- the records ----------------------------------------------------------------------------------
def record_text(entry):
    """The reserved comment of an attached file: the task's history record of it."""
    fields = {key: entry[key] for key in ('name', 'sha256', 'bytes', 'media_type', 'direction', 'note')}
    return PREFIX + json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def withdrawn_text(sha256, reason):
    return WITHDRAWN_PREFIX + json.dumps({'reason': reason, 'sha256': sha256}, ensure_ascii=False,
                                         sort_keys=True, separators=(',', ':'))


def _row(run, task):
    """The task's row, read live for this request: ``run(['show', TASK, '--json'])``."""
    text = run(['show', str(task), '--json'])
    try:
        row = record_json.loads(text) if isinstance(text, str) else text
    except ValueError:
        raise Refusal(503, 'unavailable', 'The task could not be read just now; nothing was changed.') from None
    if isinstance(row, list):
        row = row[0] if len(row) == 1 else None
    if not isinstance(row, dict) or row.get('id') != task or row.get('issue_type') == 'event':
        raise Refusal(404, 'not_found', 'Task not found')
    return row


def is_assignee(row, actor):
    """The live rule of 1b A: the task is not closed and its assignee is exactly ``actor``."""
    if not isinstance(row, dict) or row.get('status') == 'closed':
        return False
    assignee = row.get('assignee')
    return isinstance(assignee, str) and bool(assignee) and isinstance(actor, str) and assignee == actor


def payload_check(payload):
    """The request :func:`apply` takes, checked; returns it normalized."""
    if not isinstance(payload, dict):
        raise Refusal(422, 'invalid', 'Expected one task-file JSON object; nothing was written.')
    operation = payload.get('operation')
    if operation not in OPERATIONS:
        raise Refusal(422, 'invalid', 'Unknown task-file operation; nothing was written.')
    record = operation.endswith('-record')
    allowed = {'schema_version', 'operation', 'task', 'sha256'} | (
        {'name', 'bytes', 'media_type', 'direction', 'note'} if record else {'reason'})
    if set(payload) != allowed or payload.get('schema_version') != 1:
        raise Refusal(422, 'invalid', 'The task-file request has the wrong fields; nothing was written.')
    if not isinstance(payload.get('task'), str) or not payload['task']:
        raise Refusal(422, 'invalid', 'The task-file request names no task; nothing was written.')
    check_sha256(payload['sha256'])
    if record:
        clean_name(payload['name'])
        if type(payload.get('bytes')) is not int or payload['bytes'] < 0:
            raise Refusal(422, 'invalid', 'The task-file request has no byte count; nothing was written.')
        check_direction(payload['direction'])
        if payload['media_type'] != check_media_type(payload['media_type']):
            raise Refusal(422, 'invalid', 'media_type must look like type/subtype.')
        check_text(payload.get('note'), 'note', NOTE_LIMIT)
        if operation == 'coordinator-record' and payload['direction'] != TO_WORKER:
            raise Refusal(403, 'forbidden', 'A coordinator attaches files to the worker; a file from the worker '
                          'is attached by the task\'s assignee.')
        if operation == 'assignee-record' and payload['direction'] != FROM_WORKER:
            raise Refusal(403, 'forbidden', 'Only the project\'s coordinators attach files to the worker.')
    else:
        check_text(payload.get('reason'), 'reason', REASON_LIMIT, required=True)
    return payload


def apply(payload, actor, run, project, now, limit=None):
    """Record or withdraw one task file; the ONE writer of the journal and the reserved records.

    The caller holds the project's lock and has verified live authority for the capability the
    operation names (``coordinator-*``: coordinate; ``assignee-record``/``author-withdraw``:
    tasks). Here, on the task row read live: an ``assignee-record`` needs the open task's
    assignee to be ``actor`` exactly, an ``author-withdraw`` needs the entry's author to be
    ``actor``. ``run(argv)`` is the native command under ``actor`` (``show``, ``comments add``);
    ``now()`` is the time to record. Returns ``(status, answer)``: 201 for a new record, 200 for
    an identical retry or a withdrawal.
    """
    payload_check(payload)
    operation = payload['operation']
    task = payload['task']
    sha256 = payload['sha256']
    row = _row(run, task)
    current = read_entry(project, task, sha256)
    if operation.endswith('-record'):
        if operation == 'assignee-record' and not is_assignee(row, actor):
            raise Refusal(403, 'forbidden', 'Only the task\'s current assignee attaches files from the worker, '
                          'and only while the task is open.')
        size = blob_size(project, sha256)
        if size is None or size != payload['bytes']:
            raise Refusal(409, 'conflict', 'The stored file is missing: upload it again.')
        if current is not None and current.get('withdrawn') is None:
            if current['name'] == payload['name']:
                return 200, dict(view(current), replayed=True)
            raise Refusal(409, 'conflict', 'This file is already attached to the task as "%s".' % current['name'])
        limit = limit or dict(LIMIT_DEFAULTS)
        present = live(project, task)
        if len(present) + 1 > limit['max_files_per_task']:
            raise Refusal(409, 'conflict', 'A task holds at most %d files (%s); this one has %d.'
                          % (limit['max_files_per_task'], setting('max_files_per_task'), len(present)))
        if sum(e['bytes'] for e in present) + payload['bytes'] > limit['max_task_bytes']:
            raise Refusal(413, 'payload_too_large', 'A task holds at most %s of files (%s).'
                          % (size_text(limit['max_task_bytes']), setting('max_task_bytes')))
        entry = {'schema_version': 1, 'task': task, 'sha256': sha256, 'name': payload['name'],
                 'bytes': payload['bytes'], 'media_type': payload['media_type'],
                 'direction': payload['direction'], 'note': payload.get('note'), 'by': actor,
                 'at': now(), 'record_comment': None, 'withdrawn': None}
        # The folder must take the entry before the native comment is written, so a host that
        # cannot persist it writes nothing at all (followon.open_journal).
        _entry_path(project, task, sha256, create=True)
        written = record_json.loads(run(['comments', 'add', task, record_text(entry), '--json']))
        entry['record_comment'] = str(written.get('id')) if isinstance(written, dict) else None
        _write_entry(project, entry)
        return 201, view(entry)
    if current is None or current.get('withdrawn') is not None:
        raise Refusal(404, 'not_found', 'No such file on this task.')
    if operation == 'author-withdraw' and current.get('by') != actor:
        raise Refusal(403, 'forbidden', 'A file is withdrawn by the one who attached it, or by a coordinator.')
    written = record_json.loads(run(['comments', 'add', task, withdrawn_text(sha256, payload['reason']), '--json']))
    current['withdrawn'] = {'by': actor, 'at': now(), 'reason': payload['reason'],
                            'record_comment': str(written.get('id')) if isinstance(written, dict) else None}
    _write_entry(project, current)
    return 200, dict(view(current), withdrawn=True)


# -- the framed SSH request (kittrial-5bb.384, design 3b option B) -----------------------------------
# One JSON header line, then exactly ``bytes`` raw bytes (file-put), or an answer line and then the
# raw bytes (file-get). The endpoint and the client both read these; the rules live here once.
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
FRAMED_ACTIONS = ('file-list', 'file-get', 'file-put')
#: The longest header line either side reads or sends; a longer one is refused before a byte of body.
HEADER_LIMIT = 64 * 1024
_FRAME_REQUIRED = {
    'file-list': {'action', 'project', 'actor', 'task'},
    'file-get': {'action', 'project', 'actor', 'task', 'sha256'},
    'file-put': {'action', 'project', 'actor', 'task', 'name', 'direction', 'bytes', 'sha256'},
}
_FRAME_OPTIONAL = {'file-list': set(), 'file-get': set(), 'file-put': {'media_type', 'note'}}



def frame_check(header):
    """The header of a framed request, checked: exactly the fields of its action, each well formed."""
    if not isinstance(header, dict) or header.get('action') not in FRAMED_ACTIONS:
        raise Refusal(422, 'invalid', 'Expected one framed task-file header; nothing was read or stored.')
    action = header['action']
    fields = set(header)
    if not _FRAME_REQUIRED[action] <= fields or fields - _FRAME_REQUIRED[action] - _FRAME_OPTIONAL[action]:
        raise Refusal(422, 'invalid', 'The %s header has the wrong fields; nothing was read or stored.' % action)
    for key in ('project', 'actor', 'task'):
        if not isinstance(header[key], str) or not header[key]:
            raise Refusal(422, 'invalid', 'The %s header names no %s; nothing was read or stored.' % (action, key))
    for key in ('project', 'task'):
        # The task goes into the tracker's argv: a leading '-' would be read as an option.
        if not _ID.fullmatch(header[key]):
            raise Refusal(422, 'invalid', 'The %s header\'s %s is not an identifier; nothing was read or stored.' % (action, key))
    if action != 'file-list':
        check_sha256(header['sha256'])
    if action == 'file-put':
        clean_name(header['name'])
        check_direction(header['direction'])
        if type(header['bytes']) is not int or header['bytes'] < 0:
            raise Refusal(422, 'invalid', 'The file-put header has no byte count; nothing was read or stored.')
        check_media_type(header.get('media_type'))
        check_text(header.get('note'), 'note', NOTE_LIMIT)
    return header


def task_row(run, task):
    """The task's row read live (``show``), refused when it is not a task: what authority is decided on."""
    return _row(run, task)


def listing(project, task, may):
    """The task's live files as the list answers them (HTTP and SSH alike): ``missing`` when the bytes are not there."""
    items = []
    for entry in live(project, task):
        item = view(entry, may_download=may)
        if blob_size(project, entry['sha256']) != entry['bytes']:
            item['missing'] = True
        items.append(item)
    return items


def download_target(project, task, sha256):
    """``(entry, open handle)`` of a live file of ``task``, or :class:`Refusal`; the caller closes the handle."""
    entry = read_entry(project, task, sha256)
    if entry is None or entry.get('withdrawn') is not None:
        raise Refusal(404, 'not_found', 'No such file on this task.')
    handle = open_blob(project, sha256)
    if os.fstat(handle.fileno()).st_size != entry['bytes']:
        handle.close()
        raise Refusal(409, 'conflict', 'The stored file does not match its record (its size changed); it is not '
                      'served. An operator must look at it.')
    return entry, handle


def sweep_uploads(project, age=86400):
    """Remove upload temporaries older than ``age`` seconds: a killed process leaves one behind."""
    import time
    folder = _sub(project, TMP, False)
    if folder is None:
        return
    cutoff = time.time() - age
    try:
        names = os.listdir(str(folder))
    except OSError:
        return
    for name in names:
        if not name.startswith('upload-'):
            continue
        try:
            path = folder / name
            info = os.lstat(str(path))
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                os.unlink(str(path))
        except OSError:
            pass
