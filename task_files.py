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
import socket
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
    """Every sha256 a journal entry of any task names, live or withdrawn (r2, review s2).

    kittrial-5bb.380: an entry marked purged names bytes an operator removed, so it keeps nothing."""
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
                        if not _purged_file(item.path):
                            found.add(name[:-5])
    return found


def _purged_file(path):
    try:
        entry = record_json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return False
    return isinstance(entry, dict) and bool(entry.get('purged'))


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
#: kittrial-5bb.380 (coordinator's Q2): an entry whose bytes an operator's purge removed carries
#: ``purged: {by, at, reason}``; the history comment is untouched. Optional, so every entry slice 1
#: wrote stays valid.
OPTIONAL_ENTRY_FIELDS = ('purged',)


def _valid_entry(entry, task, sha256):
    if not isinstance(entry, dict) or entry.get('schema_version') != 1:
        return False
    keys = set(entry)
    if not set(ENTRY_FIELDS) <= keys <= set(ENTRY_FIELDS) | set(OPTIONAL_ENTRY_FIELDS):
        return False
    purged = entry.get('purged')
    if purged is not None and not (isinstance(purged, dict) and isinstance(purged.get('by'), str)
                                   and isinstance(purged.get('at'), str)):
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
    """The task's files a person may be shown: journal entries not withdrawn (a purged one is shown as purged)."""
    return [entry for entry in entries(project, task) if entry.get('withdrawn') is None]


def held(project, task):
    """The task's files whose bytes it holds: not withdrawn and not purged (what the limits count)."""
    return [entry for entry in live(project, task) if not entry.get('purged')]


def purged_sentence(entry):
    """What a reader is told of a purged file (kittrial-5bb.380, Q2)."""
    mark = entry.get('purged') or {}
    reason = mark.get('reason')
    return ('This file was purged by an operator on %s%s; its record stays in the task\'s history.'
            % (str(mark.get('at') or '')[:10], (' (' + str(reason)[:200] + ')') if reason else ''))


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
    if entry.get('purged'):
        item['purged'] = {key: entry['purged'].get(key) for key in ('by', 'at', 'reason')}
        item['said'] = purged_sentence(entry)
        if may_download is not None:
            item['may_download'] = False
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
    present = held(project, task) if present is None else present
    if len(present) + 1 > limit['max_files_per_task']:
        raise Refusal(409, 'conflict', 'A task holds at most %d files (%s); this one has %d. Withdraw one, '
                      'or put the set in one archive file.'
                      % (limit['max_files_per_task'], setting('max_files_per_task'), len(present)))
    holding = sum(entry['bytes'] for entry in present)
    if holding + length > limit['max_task_bytes']:
        raise Refusal(413, 'payload_too_large', 'A task holds at most %s of files (%s); this one holds %s, '
                      'and this file is %s.' % (size_text(limit['max_task_bytes']), setting('max_task_bytes'),
                                                size_text(holding), size_text(length)))
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
        if current is not None and current.get('withdrawn') is None and not current.get('purged'):
            if current['name'] == payload['name']:
                return 200, dict(view(current), replayed=True)
            raise Refusal(409, 'conflict', 'This file is already attached to the task as "%s".' % current['name'])
        limit = limit or dict(LIMIT_DEFAULTS)
        present = held(project, task)
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


# -- operations (kittrial-5bb.380, slice 5) -------------------------------------------------------
# Backups of the folder (design 4a A), the operator's verify and purge (design 2d, 2e). The caller
# holds the project's .coordination.lock (and, for a backup, its backup lock): every function here
# reads or changes the folder as one unit under it.
MANIFEST = 'manifest.json'
AUDIT = 'audit.jsonl'
STALE_SECONDS = 24 * 3600          # coordinator's Q1: orphans and temp files older than this are purged


def _blob_names(base):
    # (sha256, path) of every blob file in a folder laid out as <2 hex>/<sha256>; links are skipped.
    found = []
    try:
        tops = list(os.scandir(str(base)))
    except FileNotFoundError:
        return found
    for top in tops:
        if len(top.name) != 2 or not re.fullmatch(r'[0-9a-f]{2}', top.name) or not top.is_dir(follow_symlinks=False):
            continue
        with os.scandir(top.path) as blobs:
            for blob in blobs:
                if SHA256.fullmatch(blob.name) and blob.is_file(follow_symlinks=False) and blob.name[:2] == top.name:
                    found.append((blob.name, Path(blob.path)))
    return sorted(found)


def all_entries(project):
    """Every valid journal entry of every task, with the path of its file (relative to .task-files)."""
    base = folder(project)
    if base is None:
        return []
    records = _sub(project, RECORDS, False)
    if records is None:
        return []
    found = []
    with os.scandir(str(records)) as tasks:
        for task_dir in sorted(tasks, key=lambda d: d.name):
            if not task_dir.is_dir(follow_symlinks=False):
                continue
            with os.scandir(task_dir.path) as items:
                for item in sorted(items, key=lambda i: i.name):
                    name = item.name
                    if not name.endswith('.json') or not SHA256.fullmatch(name[:-5]) or not item.is_file(follow_symlinks=False):
                        continue
                    try:
                        entry = record_json.loads(Path(item.path).read_text(encoding='utf-8'))
                    except (OSError, ValueError, UnicodeError):
                        continue
                    if isinstance(entry, dict) and task_key(entry.get('task', '')) == task_dir.name \
                            and _valid_entry(entry, entry.get('task'), name[:-5]):
                        found.append((RECORDS + '/' + task_dir.name + '/' + name, entry))
    return found


def _hash_file(path):
    digest = hashlib.sha256()
    count = 0
    with open(str(path), 'rb') as handle:
        while True:
            chunk = handle.read(STEP)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
    return digest.hexdigest(), count


def _copy_hashed(source, target):
    # Copy SOURCE to TARGET through a temporary file beside it; returns (sha256, bytes) of what was copied.
    temp = target.with_name('.t' + uuid.uuid4().hex[:16])
    digest = hashlib.sha256()
    count = 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(str(temp), flags, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as out, open(str(source), 'rb') as handle:
            while True:
                chunk = handle.read(STEP)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                count += len(chunk)
            out.flush()
            os.fsync(out.fileno())
        return digest.hexdigest(), count, temp
    except BaseException:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise


def _write_json(path, value):
    temp = path.with_name('.t' + uuid.uuid4().hex[:16])
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1) + '\n').encode('utf-8')
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


def _plain_dir(path, create, what):
    # A directory of a backup: refused when it is a link or not a directory (as the folder itself is).
    try:
        info = os.lstat(str(path))
    except FileNotFoundError:
        if not create:
            return None
        os.mkdir(str(path), 0o700)
        info = os.lstat(str(path))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Refusal(503, 'unavailable', '%s is a symlink or not a folder; nothing was copied through it.' % what)
    return path


def backup_folder(project, target, now=None):
    """Mirror the project's task files into ``target`` (``backups/<name>.task-files``); returns the summary.

    Copied: every blob a journal entry still names (live or withdrawn; a purged entry names nothing)
    and every journal entry. Not copied: tmp/, orphans. Incremental: a blob already in the backup
    with the right size is kept. Each copied blob is hashed while it is copied, so a stored file
    that no longer matches its name is reported and NOT copied. The manifest is removed first and
    written LAST, so a backup that stopped half way has none and is never taken for a complete one.
    A project without the folder leaves an existing backup folder emptied (with an empty manifest).
    """
    target = Path(target)
    source = folder(project)
    if source is None and not os.path.lexists(str(target)):
        return {'files': 0, 'bytes': 0, 'records': 0, 'problems': [], 'absent': True}
    _plain_dir(target, True, 'The task-files backup folder')
    manifest = target / MANIFEST
    if os.path.lexists(str(manifest)):
        os.unlink(str(manifest))
    entries_now = all_entries(project) if source is not None else []
    needed = sorted({entry['sha256'] for _, entry in entries_now if not entry.get('purged')})
    blobs, problems, total = [], [], 0
    for sha256 in needed:
        size = blob_size(project, sha256)
        if size is None:
            problems.append({'sha256': sha256, 'problem': 'missing in the project'})
            continue
        top = _plain_dir(target / sha256[:2], True, 'A task-files backup folder')
        dest = top / sha256
        try:
            info = os.lstat(str(dest))
        except FileNotFoundError:
            info = None
        if info is not None and stat.S_ISREG(info.st_mode) and info.st_size == size:
            blobs.append({'sha256': sha256, 'bytes': size})
            total += size
            continue
        digest, count, temp = _copy_hashed(blob_path(project, sha256), dest)
        if digest != sha256 or count != size:
            os.unlink(str(temp))
            problems.append({'sha256': sha256, 'problem': 'the stored file does not match its name (sha256 %s)' % digest})
            continue
        os.replace(str(temp), str(dest))
        blobs.append({'sha256': sha256, 'bytes': count})
        total += count
    records = []
    _plain_dir(target / RECORDS, True, 'The task-files backup records folder')
    for relative, entry in entries_now:
        parts = relative.split('/')
        task_dir = _plain_dir(target / RECORDS / parts[1], True, 'A task-files backup records folder')
        data = (Path(source) / relative).read_bytes()
        dest = task_dir / parts[2]
        temp = dest.with_name('.t' + uuid.uuid4().hex[:16])
        with open(str(temp), 'xb') as handle:
            handle.write(data)
        os.replace(str(temp), str(dest))
        records.append({'path': relative, 'sha256': hashlib.sha256(data).hexdigest()})
    keep = {blob['sha256'] for blob in blobs}
    for sha256, path in _blob_names(target):
        if sha256 not in keep:
            os.unlink(str(path))
    keep_records = {record['path'] for record in records}
    records_dir = target / RECORDS
    for task_dir in list(os.scandir(str(records_dir))):
        if not task_dir.is_dir(follow_symlinks=False):
            continue
        for item in list(os.scandir(task_dir.path)):
            if RECORDS + '/' + task_dir.name + '/' + item.name not in keep_records and item.is_file(follow_symlinks=False):
                os.unlink(item.path)
    summary = {'schema_version': 1, 'at': now() if now else None, 'files': len(blobs), 'bytes': total,
               'records': len(records), 'blobs': blobs, 'record_files': records, 'problems': problems}
    _write_json(manifest, summary)
    return {'files': len(blobs), 'bytes': total, 'records': len(records), 'problems': problems, 'absent': False}


def restore_folder(source, project):
    """Place a task-files backup into the project after checking every file of it; returns the outcome.

    ``absent``: there is no backup folder (an older kit, or a copy that lost it): nothing is placed,
    and the records restored with the tracker then say the stored file is missing. ``refused``: the
    backup has no manifest, or a blob or a record does not match it: NOTHING is placed (a partial
    set would look complete). ``restored``: every blob (by its sha256) and every record (by the
    digest in the manifest) was checked first, then copied into the project's .task-files.
    """
    source = Path(source)
    if not os.path.lexists(str(source)):
        return {'status': 'absent', 'files': 0, 'bytes': 0, 'problems': []}
    _plain_dir(source, False, 'The task-files backup folder')
    manifest_path = source / MANIFEST
    try:
        if not stat.S_ISREG(os.lstat(str(manifest_path)).st_mode):
            raise ValueError('not a file')
        manifest = record_json.loads(manifest_path.read_text(encoding='utf-8'))
        blobs = manifest['blobs']
        records = manifest['record_files']
        assert isinstance(blobs, list) and isinstance(records, list)
    except (OSError, ValueError, KeyError, TypeError, AssertionError, UnicodeError):
        return {'status': 'refused', 'files': 0, 'bytes': 0,
                'problems': ['the task-files backup has no complete manifest (the backup did not finish)']}
    problems = []
    for blob in blobs:
        sha256 = blob.get('sha256') if isinstance(blob, dict) else None
        if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
            problems.append('a manifest entry names no sha256')
            continue
        path = source / sha256[:2] / sha256
        if not os.path.isfile(str(path)) or os.path.islink(str(path)):
            problems.append('%s is missing from the backup' % sha256)
            continue
        digest, count = _hash_file(path)
        if digest != sha256 or count != blob.get('bytes'):
            problems.append('%s does not match its name in the backup (sha256 %s, %d bytes)' % (sha256, digest, count))
    for record in records:
        relative = record.get('path') if isinstance(record, dict) else None
        if not isinstance(relative, str) or not re.fullmatch(RECORDS + r'/[0-9a-f]{32}/[0-9a-f]{64}\.json', relative):
            problems.append('a manifest record names no valid path')
            continue
        path = source / relative
        try:
            data = path.read_bytes() if not os.path.islink(str(path)) else None
        except OSError:
            data = None
        if data is None or hashlib.sha256(data).hexdigest() != record.get('sha256'):
            problems.append('the record %s is missing or changed in the backup' % relative)
    if problems:
        return {'status': 'refused', 'files': 0, 'bytes': 0, 'problems': problems}
    total = 0
    for blob in blobs:
        sha256 = blob['sha256']
        dest = blob_path(project, sha256, create=True)
        digest, count, temp = _copy_hashed(source / sha256[:2] / sha256, dest)
        if digest != sha256:
            os.unlink(str(temp))
            return {'status': 'refused', 'files': 0, 'bytes': 0, 'problems': ['%s changed while it was restored' % sha256]}
        os.replace(str(temp), str(dest))
        total += count
    for record in records:
        relative = record['path']
        parts = relative.split('/')
        task_dir = _directory(_sub(project, RECORDS, True) / parts[1], True)
        dest = task_dir / parts[2]
        temp = dest.with_name('.t' + uuid.uuid4().hex[:16])
        with open(str(temp), 'xb') as handle:
            handle.write((source / relative).read_bytes())
        os.replace(str(temp), str(dest))
    return {'status': 'restored', 'files': len(blobs), 'bytes': total, 'records': len(records), 'problems': []}


def verify(project, now_seconds=None):
    """Re-hash every stored file a journal entry needs (design 2d); returns what does not match.

    ``mismatched``: the bytes no longer hash to the file's name; ``wrong_size``: the record's byte
    count differs; ``missing``: an entry that is neither withdrawn nor purged has no bytes;
    ``orphans``: stored files no entry names; ``stale_temp``: temporary files older than a day.
    """
    import time as _time
    now_seconds = _time.time() if now_seconds is None else now_seconds
    entries_now = all_entries(project)
    result = {'checked': 0, 'mismatched': [], 'wrong_size': [], 'missing': [], 'orphans': [], 'stale_temp': []}
    if folder(project) is None:
        return result
    named = {}
    for relative, entry in entries_now:
        if entry.get('purged'):
            continue
        named.setdefault(entry['sha256'], []).append(entry)
    on_disk = dict(_blob_names(folder(project)))
    for sha256, entries_of in sorted(named.items()):
        if sha256 not in on_disk:
            if any(entry.get('withdrawn') is None for entry in entries_of):
                result['missing'].append({'sha256': sha256, 'tasks': sorted({e['task'] for e in entries_of})})
            continue
        digest, count = _hash_file(on_disk[sha256])
        result['checked'] += 1
        if digest != sha256:
            result['mismatched'].append({'sha256': sha256, 'actual': digest, 'tasks': sorted({e['task'] for e in entries_of})})
        elif any(entry['bytes'] != count for entry in entries_of):
            result['wrong_size'].append({'sha256': sha256, 'bytes': count})
    for sha256, path in sorted(on_disk.items()):
        if sha256 not in named:
            result['orphans'].append({'sha256': sha256, 'bytes': path.stat().st_size})
    tmp = _sub(project, TMP, False)
    if tmp is not None:
        for item in os.scandir(str(tmp)):
            if item.is_file(follow_symlinks=False) and now_seconds - item.stat(follow_symlinks=False).st_mtime > STALE_SECONDS:
                result['stale_temp'].append(item.name)
    return result


def _closed_before(row, date):
    closed = row.get('closed_at') if isinstance(row, dict) else None
    return isinstance(row, dict) and row.get('status') == 'closed' and isinstance(closed, str) and closed[:10] < date


def purge_plan(project, closed_before, rows, now_seconds=None):
    """What a purge removes, and what it keeps because a live record still needs it (design 2e, Q1).

    ``rows`` maps each task id to its tracker row (status, closed_at). Removed: the bytes of a
    withdrawn entry and of an entry on a task closed before ``closed_before`` (YYYY-MM-DD), each
    only when NO entry that keeps its bytes names the same file (a live entry of a task that is not
    closed before the date, on any task); orphans (no entry names them) and temporary files, both
    older than a day. Nothing is removed here.
    """
    import time as _time
    if not isinstance(closed_before, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', closed_before):
        raise Refusal(422, 'invalid', '--closed-before is a date, YYYY-MM-DD.')
    now_seconds = _time.time() if now_seconds is None else now_seconds
    base = folder(project)
    plan = {'closed_before': closed_before, 'remove': [], 'kept_referenced': [], 'marks': []}
    if base is None:
        return plan
    entries_now = all_entries(project)
    needed, candidates = set(), []
    for relative, entry in entries_now:
        if entry.get('purged'):
            continue
        row = rows.get(entry['task'])
        why = 'withdrawn' if entry.get('withdrawn') is not None else (
            'task closed before %s' % closed_before if _closed_before(row, closed_before) else None)
        if why is None:
            needed.add(entry['sha256'])
        else:
            candidates.append((relative, entry, why))
    on_disk = dict(_blob_names(base))
    removed = set()
    for relative, entry, why in candidates:
        sha256 = entry['sha256']
        if sha256 in needed:
            plan['kept_referenced'].append({'sha256': sha256, 'task': entry['task'], 'why': why})
            continue
        plan['marks'].append({'path': relative, 'task': entry['task'], 'sha256': sha256, 'why': why})
        if sha256 in on_disk and sha256 not in removed:
            removed.add(sha256)
            plan['remove'].append({'kind': 'file', 'sha256': sha256, 'bytes': on_disk[sha256].stat().st_size,
                                   'why': why, 'task': entry['task']})
    named = {entry['sha256'] for _, entry in entries_now if not entry.get('purged')}
    for sha256, path in sorted(on_disk.items()):
        info = path.stat()
        if sha256 not in named and now_seconds - info.st_mtime > STALE_SECONDS:
            plan['remove'].append({'kind': 'orphan', 'sha256': sha256, 'bytes': info.st_size,
                                   'why': 'no record names it, older than a day'})
    tmp = _sub(project, TMP, False)
    if tmp is not None:
        for item in sorted(os.scandir(str(tmp)), key=lambda i: i.name):
            if item.is_file(follow_symlinks=False):
                info = item.stat(follow_symlinks=False)
                if now_seconds - info.st_mtime > STALE_SECONDS:
                    plan['remove'].append({'kind': 'temp', 'name': item.name, 'bytes': info.st_size,
                                           'why': 'an unfinished upload, older than a day'})
    return plan


def purge(project, plan, actor, now, reason=None):
    """Carry out a purge plan: remove its files, mark the entries purged, and write ONE audit line.

    The caller holds the project's lock and checked that ``actor`` is an operator. A file is
    removed only after checking it is a regular file in place (never through a link); an entry is
    marked ``purged: {by, at, reason}`` (its history comment is untouched). Returns the audit line.
    """
    base = folder(project)
    if base is None:
        raise Refusal(404, 'not_found', 'The project has no task files.')
    at = now()
    done = []
    for item in plan['remove']:
        if item['kind'] == 'temp':
            path = _sub(project, TMP, False) / item['name']
        else:
            path = blob_path(project, item['sha256'])
        try:
            if path is not None and stat.S_ISREG(os.lstat(str(path)).st_mode):
                os.unlink(str(path))
                done.append(item)
        except FileNotFoundError:
            pass
    for mark in plan['marks']:
        entry = read_entry(project, mark['task'], mark['sha256'])
        if entry is None or entry.get('purged'):
            continue
        entry['purged'] = {'by': actor, 'at': at, 'reason': reason or mark['why']}
        _write_entry(project, entry)
    line = {'schema_version': 1, 'at': at, 'actor': actor, 'operation': 'purge',
            'closed_before': plan['closed_before'], 'removed': done,
            'bytes': sum(item.get('bytes') or 0 for item in done),
            'marked': [{'task': m['task'], 'sha256': m['sha256'], 'why': m['why']} for m in plan['marks']],
            'kept_referenced': plan['kept_referenced']}
    audit = base / AUDIT
    if os.path.islink(str(audit)):
        raise Refusal(503, 'unavailable', UNUSABLE % (FOLDER, 'its audit file is a symlink'))
    with open(str(audit), 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    return line


# -- the proxy upload check (design 5, kittrial-5bb.380) -------------------------------------------
PROXY_PATH = '/v1/projects/proxy-check/tasks/proxy-check/files?name=proxy-check.bin&direction=to-worker'
PROXY_FIX = ('raise the body limit of the reverse proxy in front of the site (nginx client_max_body_size) to at least '
             '%s, the per-file limit (task_files.max_file_bytes)')


def _read_answer(sock, deadline):
    # The status code and up to 64 KiB of body of an HTTP answer read from SOCK (until close or Content-Length).
    import select
    import time as _time
    data = b''
    while b'\r\n\r\n' not in data:
        left = deadline - _time.monotonic()
        if left <= 0:
            return None, b''
        sock.settimeout(left)
        piece = sock.recv(65536)
        if not piece:
            break
        data += piece
    head, _, body = data.partition(b'\r\n\r\n')
    lines = head.decode('latin-1').split('\r\n')
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return None, b''
    if status == 100:                                 # a 100 Continue: the real answer follows
        return _read_answer_after(sock, body, deadline)
    length = None
    for line in lines[1:]:
        name, _, value = line.partition(':')
        if name.strip().lower() == 'content-length' and value.strip().isdigit():
            length = int(value.strip())
    while (length is None or len(body) < length) and len(body) < 65536:
        left = deadline - _time.monotonic()
        if left <= 0:
            break
        sock.settimeout(left)
        try:
            piece = sock.recv(65536)
        except OSError:
            break
        if not piece:
            break
        body += piece
    return status, body


def _read_answer_after(sock, rest, deadline):
    class Replay:
        def __init__(self, inner, first):
            self.inner, self.first = inner, first

        def settimeout(self, t):
            self.inner.settimeout(t)

        def recv(self, n):
            if self.first:
                out, self.first = self.first[:n], self.first[n:]
                return out
            return self.inner.recv(n)
    return _read_answer(Replay(sock, rest), deadline)


def proxy_check(url, length, cafile=None, timeout=300):
    """Send an UNSIGNED upload of ``length`` bytes through the public address; say whether it reached Orchestra.

    Orchestra answers an unsigned upload with its own JSON 401 (``unauthenticated``) before it
    stores a byte, so that answer means the body's size passed whatever stands in front of the
    site. A 413, any answer that is not Orchestra's, or a connection cut before any answer, means
    something in front (a reverse proxy) limits uploads. The body is sent while the answer is
    watched for, so an answer given before the whole body was read (Orchestra's own, or a proxy's
    413 from the Content-Length alone) is read, not lost to the reset that unread bytes cause.
    Nothing is stored and nothing is signed in.
    """
    import select
    import ssl
    import time as _time
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    path = parts.path.rstrip('/') + PROXY_PATH
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    host_header = parts.hostname if parts.port is None else '%s:%d' % (parts.hostname, parts.port)
    size = size_text(length)
    result = {'url': url, 'bytes': length, 'passed': False, 'status': None, 'sent': 0}
    deadline = _time.monotonic() + timeout
    raw = socket.create_connection((parts.hostname, port), timeout=30)
    sock = raw
    try:
        if parts.scheme == 'https':
            sock = ssl.create_default_context(cafile=cafile).wrap_socket(raw, server_hostname=parts.hostname)
        head = ('PUT %s HTTP/1.1\r\nHost: %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n'
                % (path, host_header, OPAQUE, length)).encode('latin-1')
        sock.sendall(head)
        chunk = b'\0' * STEP
        status = body = None
        while result['sent'] < length:
            readable = select.select([sock], [], [], 0)[0] or (hasattr(sock, 'pending') and sock.pending())
            if readable:
                break                                       # an answer came before the whole body: read it
            piece = chunk[:min(STEP, length - result['sent'])]
            try:
                sock.settimeout(max(1, deadline - _time.monotonic()))
                sock.sendall(piece)
            except OSError:
                break
            result['sent'] += len(piece)
        try:
            status, body = _read_answer(sock, deadline)
        except OSError:
            status, body = None, b''
    finally:
        try:
            sock.close()
        except OSError:
            pass
    result['status'] = status
    if status is None:
        result['sentence'] = ('The connection was cut after %s of the %s test upload, before any answer: something in front '
                              'of the site may limit uploads; %s.' % (size_text(result['sent']), size, PROXY_FIX % size))
        return result
    orchestra = False
    try:
        answer = record_json.loads(body.decode('utf-8'))   # the proxy's or the site's text: guarded
        orchestra = isinstance(answer, dict) and isinstance(answer.get('error'), dict) \
            and answer['error'].get('code') == 'unauthenticated' and 'request_id' in answer
    except (ValueError, UnicodeError):
        orchestra = False
    if status == 401 and orchestra:
        result['passed'] = True
        result['sentence'] = ('An upload of %s through %s reached Orchestra (its own 401 for this unsigned test): nothing in '
                              'front of the site cuts uploads of the per-file limit.' % (size, url))
    elif status == 413:
        result['sentence'] = ('An upload of %s through %s was refused before it reached Orchestra (HTTP 413 from something '
                              'in front of the site): %s.' % (size, url, PROXY_FIX % size))
    else:
        result['sentence'] = ('An upload of %s through %s got HTTP %s, which is not Orchestra\'s answer: a proxy or another '
                              'server answered. Check what serves this address; if it is a reverse proxy, %s.'
                              % (size, url, status, PROXY_FIX % size))
    return result
