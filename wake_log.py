#!/usr/bin/env python3
"""A bounded host log of EMPTY worker wakes (kittrial-5bb.397).

A worker's blocking wait wakes when the fingerprint of its move rows changes.  Some
of those wakes carry nothing to act on: a task changed, but none of the changed rows
is the worker's move any more (an integrated-but-open task whose review state moved
is the case kittrial-5bb.390 left behind).  The worker sees a wake with an empty
``items`` list, which is indistinguishable from a bad wake unless it is recorded.

This module is the ONE rule for that, shared by the two transports:

* the CLI endpoint read (``work.execute``'s ``wait`` action, called by ``client.py
  wait``), and
* the agent HTTP read (``ApiHandler.agents_me_wait``).

It is deliberately additive and READ-ONLY for the tracker.  The only new persistent
state is a per-lane file under ``<project>/.wait-wakes/``; no ``bd`` write, no task
comment and no review record is made here.

Layout, for one actor::

    <project>/.wait-wakes/<sha256(actor)[:16]>.jsonl   the audit (one entry per wake)
    <project>/.wait-wakes/<sha256(actor)[:16]>.last.json  the rows the caller last saw
    <project>/.wait-wakes/<sha256(actor)[:16]>.lock    the writer lock

The ``.jsonl`` is append-only in meaning: an entry is written only for an empty
wake, once per ``(actor, task, fingerprint)``, and a file is capped at
:data:`MAX_BYTES` bytes / :data:`MAX_ENTRIES` entries with the oldest dropped.  Every
rewrite is atomic (one temporary file in the same directory, then :func:`os.replace`),
under an exclusive ``flock``, so two concurrent waits by the same actor can never
interleave a half-file.  A symlinked directory or file is refused rather than
followed.  Every public entry point that a wait calls is *best effort*: :func:`observe`
swallows its own errors and returns warnings for the answer, so a lost or unreadable
log can never fail, slow or change a wait's answer.

No file here is ever opened in a way that can block: the lane lock is tried with
``LOCK_NB`` for a short bounded budget, and every read/write open uses ``O_NONBLOCK``
and ``O_NOFOLLOW`` and must be a regular file on its descriptor.  A held ``flock``, a
FIFO planted at any lane file, or a symlink is refused with a warning instead of
stalling a wait or an HTTP handler thread.

This module is import-safe on every platform: ``fcntl`` is imported only when the lane
lock is taken, so ``work.py``'s wait path (and every HTTP handler) imports it on
Windows too.  Where ``fcntl`` is unavailable nothing is recorded (the log is
unavailable, not an error), no file is written, and the wait's answer is unchanged.
"""
import contextlib
import errno
import hashlib
import json
import os
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import waiting

#: An in-process lock beside the file lock: two waits can be two threads of one process
#: (a client and a service in the same process in a test), and flock is per open file
#: description rather than per process, so the thread lock is what makes the pair safe.
_THREAD_LOCK = threading.Lock()
#: The most a wait may spend trying to take the lane locks before it gives up with a
#: warning.  A wait is never allowed to stall behind another process's lock.
LOCK_BUDGET_SECONDS = 1.0
LOCK_POLL_SECONDS = 0.005
#: Repeated warnings are emitted once per process, not once per poll (review n3).
WARNING_MEMORY = 64
_WARNINGS_SEEN = set()
_WARNINGS_LOCK = threading.Lock()

SCHEMA = 'wait-wake-v1'
DIRECTORY = '.wait-wakes'
#: One lane's audit is bounded by both entries and bytes; the bound keeps the file
#: small on a filesystem that is read on every lane-board read.
MAX_ENTRIES = 64
MAX_BYTES = 4096
#: An empty wake is older than the shown window after this many seconds.
WINDOW_SECONDS = 24 * 60 * 60
#: How many DIFFERENT fingerprints in the window make the coordinator-wide trigger.
TRIGGER_LIMIT = 3
#: How much of a task id and a trigger a stored entry carries; the whole line stays
#: well inside the byte bound even with a long fingerprint.
TASK_TEXT = 200
TRIGGER_TEXT = 40

#: The fields two reads of the same queue row are compared on.  They are the worker
#: wait's own fields plus the ones its trigger names; a field outside this list (an
#: age, a generated_at) never makes a change.
WATCH_FIELDS = ('review_state', 'work_state', 'owner', 'assignee', 'status',
                'contribution_id', 'pending_review_items', 'redelivered',
                'guidance_version', 'wait_newer_activity_marker',
                'whose_move', 'next_actor')

#: First match wins, in the order the plan names them.  ``new-comment`` is the
#: kittrial-5bb.390 rule; everything unknown is an ``edit``.
TRIGGER_ORDER = (
    (('wait_newer_activity_marker',), 'new-comment'),
    (('review_state', 'work_state'), 'review-state'),
    (('owner', 'assignee'), 'assignment'),
    (('contribution_id', 'redelivered'), 'delivery'),
    (('pending_review_items',), 'review-items'),
    (('status',), 'status'),
    (('guidance_version',), 'guidance'),
)

DEFAULT_SUMMARY = {'empty_wakes_24h': 0, 'empty_wake_task': None,
                   'empty_wake_trigger': None, 'empty_wake_at': None}


def _now(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(float(now), timezone.utc)
    if isinstance(now, str):
        parsed = _instant(now)
        if parsed is not None:
            return parsed
    raise TypeError('now must be a datetime, an ISO-8601 instant or an epoch')


def _instant(text):
    """A stored ``at`` as an aware UTC datetime, or ``None``."""
    if not isinstance(text, str) or not text.strip():
        return None
    value = text.strip()
    if value.endswith(('Z', 'z')):
        value = value[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _stamp(moment):
    return moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def actor_key(actor):
    """The file stem for one lane: a short hash, never the actor text itself."""
    return hashlib.sha256(str(actor or '').encode('utf-8')).hexdigest()[:16]


def paths(project, actor):
    """The lane's four paths as ``(directory, audit, snapshot, lock)``.

    ``project`` is a directory (the project directory the endpoint and the lane board
    already use), never the tracker file.  ``None`` means there is nothing to read or
    write and the caller gets the empty summary.
    """
    if project is None:
        return None
    directory = Path(project) / DIRECTORY
    stem = actor_key(actor)
    return (directory, directory / (stem + '.jsonl'),
            directory / (stem + '.last.json'), directory / (stem + '.lock'))


def _ensure_directory(directory):
    """Create the log directory, refusing a symlink at the project or at the log dir."""
    if Path(directory).is_symlink() or Path(directory).parent.is_symlink():
        raise ValueError('the wait-wake directory must not be a symlink')
    if not directory.exists():
        directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError('the wait-wake directory must not be a symlink')
    return directory


def _refuse_symlink(path):
    if Path(path).is_symlink():
        raise ValueError('the wait-wake file must not be a symlink')


def _open_fd(path, flags, mode=0o600):
    """Open a lane file without following a symlink and without ever blocking.

    The descriptor must be a REGULAR file: a FIFO, device, directory or socket is
    refused here, on the descriptor itself, because an ``lstat`` before the open can
    race and a FIFO open can block.  ``O_NONBLOCK`` makes the open itself harmless for
    a FIFO; the type check then refuses it.
    """
    flags = flags | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(str(path), flags, mode)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('the wait-wake file must be a regular file')
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _read_regular(path, limit):
    """At most ``limit`` bytes of a regular lane file; refuses symlinks and specials."""
    _refuse_symlink(path)
    descriptor = _open_fd(path, os.O_RDONLY)
    try:
        with os.fdopen(descriptor, 'rb') as handle:
            descriptor = -1
            return handle.read(limit)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_audit(audit, now):
    """The stored entries inside the window, oldest first; malformed lines are skipped.

    A reader never locks: the writer only ever replaces the file atomically, so a
    reader sees the old file or the new one, never a partial line.
    """
    _refuse_symlink(audit)
    try:
        raw = _read_regular(audit, MAX_BYTES * 8)
    except FileNotFoundError:
        return []
    entries = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line.decode('utf-8'))
        except (ValueError, UnicodeError):
            continue
        if not isinstance(entry, dict) or entry.get('schema') != SCHEMA:
            continue
        at = _instant(entry.get('at'))
        if at is None or at <= now - timedelta(seconds=WINDOW_SECONDS):
            continue
        if entry.get('empty') is not True:
            continue
        entry['at'] = _stamp(at)
        entries.append(entry)
    entries.sort(key=lambda entry: (entry.get('at') or '', entry.get('fingerprint') or '',
                                    entry.get('task') or ''))
    return entries


def _encode(entries):
    return ''.join(json.dumps(entry, ensure_ascii=True, sort_keys=True,
                              separators=(',', ':')) + '\n' for entry in entries)


def _bounded(entries):
    """Keep the newest entries inside both bounds; oldest are dropped first."""
    kept = entries[-MAX_ENTRIES:]
    while len(kept) > 1 and len(_encode(kept).encode('utf-8')) > MAX_BYTES:
        kept = kept[1:]
    # One entry that alone exceeds the byte bound is clipped to its identifying fields
    # rather than dropped, so a wake is never lost to a long task id.
    if kept and len(_encode(kept).encode('utf-8')) > MAX_BYTES:
        kept = [dict(kept[-1], task=str(kept[-1].get('task') or '')[:TASK_TEXT],
                     trigger=str(kept[-1].get('trigger') or '')[:TRIGGER_TEXT])]
    return kept


def _write_atomic(path, text):
    """Replace ``path`` with ``text`` from a temporary file in the same directory.

    The temporary file is opened ``O_EXCL|O_NOFOLLOW|O_NONBLOCK`` and checked regular;
    an existing target that is a symlink or a special file is refused instead of being
    replaced, so a planted FIFO can neither stall nor redirect the write.
    """
    path = Path(path)
    _refuse_symlink(path)
    temporary = path.with_name('.%s.%d.%d.tmp' % (path.name, os.getpid(),
                                                   threading.get_ident()))
    _refuse_symlink(temporary)
    descriptor = -1
    try:
        descriptor = _open_fd(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        data = text.encode('utf-8')
        written = 0
        while written < len(data):
            written += os.write(descriptor, data[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('the wait-wake file must be a regular file')
        os.replace(str(temporary), str(path))
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()
        except OSError:
            pass


@contextlib.contextmanager
def _thread_guard():
    """Take the in-process lane lock, but never wait for it longer than the budget."""
    if not _THREAD_LOCK.acquire(timeout=LOCK_BUDGET_SECONDS):
        raise OSError(errno.EWOULDBLOCK, 'the wait-wake thread lock is busy')
    try:
        yield
    finally:
        _THREAD_LOCK.release()


def _fcntl():
    """The ``fcntl`` module, or ``None`` where the platform has none (Windows).

    Imported HERE, lazily, and never at module import: ``work.py``'s wait path imports
    this module on every platform, so a module-level ``import fcntl`` made the wait
    (and ``test_blocking_wait``) fail with ``ModuleNotFoundError`` on Windows.  Every
    caller treats ``None`` as "the log is unavailable": no record, no snapshot, and the
    wait's answer is untouched.
    """
    try:
        import fcntl
    except ImportError:
        return None
    return fcntl


class _Lock:
    """An exclusive ``flock`` on the lane's lock file, tried without blocking.

    A second waiter retries ``LOCK_NB`` for at most :data:`LOCK_BUDGET_SECONDS` and
    then gives up with ``EWOULDBLOCK``: a stalled lock holder can delay a wait by at
    most that budget, never by the holder's lifetime.
    """

    def __init__(self, path):
        self.path = path
        self.handle = None
        self.fcntl = None

    def __enter__(self):
        module = _fcntl()
        if module is None:
            raise OSError(errno.ENOSYS, 'the wait-wake lane lock needs fcntl (unavailable)')
        _refuse_symlink(self.path)
        self.handle = _open_fd(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.fcntl = module
        deadline = time.monotonic() + LOCK_BUDGET_SECONDS
        while True:
            try:
                module.flock(self.handle, module.LOCK_EX | module.LOCK_NB)
                return self
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    os.close(self.handle)
                    self.handle = None
                    raise
                if time.monotonic() >= deadline:
                    os.close(self.handle)
                    self.handle = None
                    raise OSError(errno.EWOULDBLOCK, 'the wait-wake lane lock is busy')
                time.sleep(LOCK_POLL_SECONDS)

    def __exit__(self, *_exc):
        if self.handle is not None:
            try:
                self.fcntl.flock(self.handle, self.fcntl.LOCK_UN)
            finally:
                os.close(self.handle)
                self.handle = None
        return False



def _key(entry):
    return (entry.get('actor'), entry.get('task'), entry.get('fingerprint'))


def append(project, actor, entries, now=None):
    """Append empty-wake ``entries`` for one lane; returns how many were new.

    The whole file is rewritten atomically under the lane's exclusive lock, so the
    dedupe (actor, task, fingerprint), the 24 h window and both bounds are applied to
    one consistent state and two concurrent waits cannot lose or double an entry.
    """
    place = paths(project, actor)
    if place is None or not entries:
        return 0
    if _fcntl() is None:
        # Recording needs the lane lock; without fcntl (Windows) the log is unavailable.
        return 0
    directory, audit, _snapshot, lock = place
    moment = _now(now)
    try:
        _ensure_directory(directory)
        with _thread_guard(), _Lock(lock):
            existing = _read_audit(audit, moment)
            seen = {_key(entry) for entry in existing}
            fresh = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                item = {
                    'schema': SCHEMA,
                    'at': _stamp(_instant(entry.get('at')) or moment),
                    'actor': str(entry.get('actor') or actor)[:200],
                    'fingerprint': str(entry.get('fingerprint') or ''),
                    'task': str(entry.get('task') or '')[:TASK_TEXT],
                    'trigger': str(entry.get('trigger') or 'edit')[:TRIGGER_TEXT],
                    'empty': entry.get('empty') is True,
                }
                key = _key(item)
                if key in seen:
                    continue
                seen.add(key)
                fresh.append(item)
            if not fresh:
                return 0
            _write_atomic(audit, _encode(_bounded(existing + fresh)))
            return len(fresh)
    except (OSError, ValueError, TypeError):
        raise


def read(project, actor, now=None):
    """The lane's empty-wake summary inside the 24 h window.

    ``empty_wakes_24h`` counts DIFFERENT fingerprints, not lines, so one wake that
    changed four rows counts once.  ``empty_wake_*`` name the newest wake.  A missing
    or unreadable file is the empty summary; nothing here raises into a read.
    """
    moment = _now(now)
    summary = dict(DEFAULT_SUMMARY)
    place = paths(project, actor)
    if place is None:
        return summary
    _directory, audit, _snapshot, _lock = place
    try:
        entries = _read_audit(audit, moment)
    except (OSError, ValueError):
        return summary
    if not entries:
        return summary
    summary['empty_wakes_24h'] = len({entry.get('fingerprint') for entry in entries
                                      if entry.get('fingerprint')})
    newest = entries[-1]
    summary['empty_wake_task'] = newest.get('task')
    summary['empty_wake_trigger'] = newest.get('trigger')
    summary['empty_wake_at'] = newest.get('at')
    return summary


def summaries(project, actors, now=None):
    """One ``read`` per actor, as a mapping; a broken lane is the empty summary."""
    moment = _now(now)
    result = {}
    for actor in actors or ():
        if not isinstance(actor, str) or not actor:
            continue
        try:
            result[actor] = read(project, actor, now=moment)
        except Exception:                                     # noqa: BLE001 - one lane never fails a board
            result[actor] = dict(DEFAULT_SUMMARY)
    return result


def _field(row, attention, name):
    """A row field, else the same field in its nested ``attention`` block.

    A canonical ``work`` row carries the checkpoint fields at the top level; the HTTP
    service's queue row carries them under ``attention`` (the same fallback
    ``http_service.attention_field`` uses).  Both transports must classify alike.
    """
    value = row.get(name)
    if value is None and isinstance(attention, dict):
        value = attention.get(name)
    return value


def normalize_row(row):
    """One queue row reduced to the fields two waits compare, JSON-safe."""
    if not isinstance(row, dict):
        return None
    task = row.get('task') if row.get('task') is not None else row.get('id')
    if task is None:
        return None
    attention = row.get('attention') if isinstance(row.get('attention'), dict) else {}
    owner = _field(row, attention, 'owner')
    if owner is None:
        owner = _field(row, attention, 'assignee')
    review_state = _field(row, attention, 'review_state')
    return {
        'task': str(task)[:TASK_TEXT],
        'review_state': _scalar(review_state),
        'work_state': _scalar(_field(row, attention, 'work_state')
                              if _field(row, attention, 'work_state') is not None
                              else review_state),
        'owner': _scalar(owner),
        'assignee': _scalar(owner),
        'status': _scalar(_field(row, attention, 'status')),
        'contribution_id': _scalar(_field(row, attention, 'contribution_id')),
        'pending_review_items': _scalar(_field(row, attention, 'pending_review_items')),
        'redelivered': _scalar(_field(row, attention, 'redelivered')),
        'guidance_version': _scalar(_field(row, attention, 'guidance_version')),
        'wait_newer_activity_marker': _scalar(_field(row, attention,
                                                     'wait_newer_activity_marker')),
        'whose_move': _scalar(_field(row, attention, 'whose_move')),
        'next_actor': _scalar(_field(row, attention, 'next_actor')),
        # Carried so a later read can tell an OTHER-PARTY change from the caller's own
        # write (review b2).  They are deliberately NOT in WATCH_FIELDS: a count moving
        # is not a trigger, and it must not change a fingerprint.
        'newer_activity_by_others': _scalar(_field(row, attention,
                                                   'newer_activity_by_others')),
        'newer_activity_own': _scalar(_field(row, attention, 'newer_activity_own')),
    }


def _scalar(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)[:200]


def _own_change(before, after):
    """Whether a changed row changed because of the CALLER's own write, not another's.

    The worker's wait marker is "the newest entry by anybody other than the task's
    assignee", and the newer-activity counters say how many entries by others exist.
    So when the other-party count did not grow and the wait marker did not move, no
    other-party entry and no field edit arrived since the rows the caller last saw:
    the change is the caller's own delivery, close, release or checkpoint and is not
    an empty wake at all (review b2).  An added row is an assignment (not the
    caller's write); a removed row left the caller's queue by the caller's release or
    close (or was unassigned), which is likewise not a bad wake.
    """
    if before is None:
        return False
    if after is None:
        return True
    old_other = before.get('newer_activity_by_others')
    new_other = after.get('newer_activity_by_others')
    if isinstance(old_other, int) and isinstance(new_other, int):
        if new_other > old_other:
            return False
        old_marker = before.get('wait_newer_activity_marker')
        new_marker = after.get('wait_newer_activity_marker')
        if (isinstance(old_marker, str) and isinstance(new_marker, str)
                and old_marker != new_marker):
            # No new entry, but an other party edited the task fields: not the caller.
            return False
        return True
    old_marker = before.get('wait_newer_activity_marker')
    new_marker = after.get('wait_newer_activity_marker')
    if isinstance(old_marker, str) and isinstance(new_marker, str):
        return old_marker == new_marker
    return False


def _trigger(diff):
    for names, name in TRIGGER_ORDER:
        if any(field in diff for field in names):
            return name
    return 'edit'


def changed_wakes(previous, rows, view):
    """The changed rows behind one wake.

    Each item is ``{'task', 'trigger', 'whose_move', 'own'}``; ``own`` is true when
    the change is the caller's own write (see :func:`_own_change`) and must not be
    counted as an empty wake.  ``previous`` is the bounded row list the caller last
    read (from ``.last.json``), ``rows`` the caller's full queue rows now, and
    ``view`` the printed worker view (its ``items`` are the rows that are actually
    the caller's move).  A row is changed when any field in :data:`WATCH_FIELDS`
    differs; added and removed rows count too.  The returned list is empty when the
    rows are the same.
    """
    old = {}
    for row in previous or []:
        item = normalize_row(row)
        if item is not None:
            old[item['task']] = item
    new = {}
    for row in rows or []:
        item = normalize_row(row)
        if item is not None:
            new[item['task']] = item
    changed = []
    for task in sorted(set(old) | set(new)):
        before, after = old.get(task), new.get(task)
        if before is None:
            changed.append({'task': task, 'trigger': 'assignment',
                            'whose_move': (after or {}).get('whose_move'), 'own': False})
            continue
        if after is None:
            changed.append({'task': task, 'trigger': 'edit', 'whose_move': None, 'own': True})
            continue
        diff = [field for field in WATCH_FIELDS if before.get(field) != after.get(field)]
        if diff:
            changed.append({'task': task, 'trigger': _trigger(diff),
                            'whose_move': after.get('whose_move'),
                            'own': _own_change(before, after)})
    return changed


def _read_snapshot(snapshot):
    """The last rows the caller saw, read without following a symlink or blocking.

    The format is the same small ``waiting.read_state`` file the wait already kept,
    so an existing log keeps working; an absent, unreadable, malformed or special file
    is simply no remembered rows.
    """
    _refuse_symlink(snapshot)
    try:
        raw = _read_regular(snapshot, waiting.MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > waiting.MAX_STATE_BYTES:
        return None
    try:
        value = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(value, dict) or value.get('schema') != waiting.WAIT_STATE_SCHEMA:
        return None
    fingerprint = value.get('fingerprint')
    if not waiting.is_fingerprint(fingerprint):
        fingerprint = None
    items = value.get('items')
    if not isinstance(items, list):
        items = []
    return {'fingerprint': fingerprint,
            'items': [row for row in items[:waiting.MAX_STATE_ITEMS]
                      if isinstance(row, dict)]}


def _write_snapshot(project, actor, fingerprint, rows, now):
    """Keep the fingerprint and the bounded rows the caller just read, atomically."""
    place = paths(project, actor)
    if place is None:
        return False
    directory, _audit, snapshot, lock = place
    _ensure_directory(directory)
    normalized = [normalize_row(row) for row in rows or []]
    normalized = [row for row in normalized if row is not None]
    payload = {'schema': waiting.WAIT_STATE_SCHEMA, 'fingerprint': fingerprint,
               'items': normalized[:waiting.MAX_STATE_ITEMS]}

    def encoded(value):
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))

    try:
        text = encoded(payload)
    except (ValueError, TypeError):
        return False
    if len(text) > waiting.MAX_STATE_BYTES:
        payload['items'] = []
        try:
            text = encoded(payload)
        except (ValueError, TypeError):
            return False
        if len(text) > waiting.MAX_STATE_BYTES:
            return False
    with _thread_guard(), _Lock(lock):
        _write_atomic(snapshot, text + '\n')
    return True


def observe(project, actor, since, view, rows, now=None):
    """Record one empty wake if this read is one; NEVER raise into the wait.

    ``since`` is the fingerprint the caller already had (``--since``), ``view`` the
    printed worker view and ``rows`` the caller's full ``work --mine`` rows.  A wake
    is recorded only when this read moves the fingerprint (``since`` is a valid
    fingerprint and differs from the view's), at least one row changed, and the view
    carries NO row that is the caller's move.  The last-seen rows are refreshed after
    every read, so the next wake is compared with what the caller actually saw.

    Returns a list of warning strings for the answer; an error here is swallowed and
    at most shown there.  It writes only this actor's own lane and no tracker record.
    """
    warnings = []
    try:
        if _fcntl() is None:
            # The lane lock needs fcntl. Where it is missing (Windows) the log is
            # UNAVAILABLE, not broken: no record, no snapshot file and no warning, so
            # the wait's answer is byte-for-byte what it was before the log existed.
            return warnings
        if project is None or not isinstance(actor, str) or not actor:
            return warnings
        if not isinstance(view, dict):
            return warnings
        fingerprint = view.get('fingerprint')
        if not isinstance(fingerprint, str) or not waiting.is_fingerprint(fingerprint):
            return warnings
        moment = _now(now)
        previous = []
        state = None
        place = paths(project, actor)
        if place is None:
            return warnings
        try:
            state = _read_snapshot(place[2])
            previous = state.get('items') if state else []
        except Exception as error:                            # noqa: BLE001 - best effort
            warnings.append('empty-wake log: could not read the last rows: %s' % str(error)[:160])
        changed = changed_wakes(previous, rows, view)
        # A change the caller caused (its own delivery, close, release or checkpoint)
        # is not an empty wake at all (review b2).
        foreign = [item for item in changed if not item.get('own')]
        if (isinstance(since, str) and waiting.is_fingerprint(since)
                and fingerprint != since and foreign and not (view.get('items') or [])):
            entries = [{
                'schema': SCHEMA,
                'at': _stamp(moment),
                'actor': actor,
                'fingerprint': fingerprint,
                'task': item['task'],
                'trigger': item['trigger'],
                'empty': True,
            } for item in foreign]
            try:
                append(project, actor, entries, now=moment)
            except Exception as error:                        # noqa: BLE001 - best effort
                warnings.append('empty-wake log: could not record: %s' % str(error)[:160])
        # The snapshot only changes with the fingerprint or the rows; an unchanged poll
        # never rewrites it (review n2: the CLI and HTTP read paths now agree and both
        # call this on every poll).
        unchanged = (state is not None and state.get('fingerprint') == fingerprint
                     and not changed)
        if not unchanged:
            try:
                if not _write_snapshot(project, actor, fingerprint, rows, moment):
                    warnings.append('empty-wake log: could not keep the last rows')
            except Exception as error:                        # noqa: BLE001 - best effort
                warnings.append('empty-wake log: could not keep the last rows: %s' % str(error)[:160])
    except Exception as error:                                # noqa: BLE001 - best effort
        warnings.append('empty-wake log: %s' % str(error)[:160])
    return _warn_once(warnings)


def _warn_once(warnings):
    """The warnings a process has not emitted yet (review n3: once, not every poll)."""
    fresh = []
    with _WARNINGS_LOCK:
        for warning in warnings:
            if warning in _WARNINGS_SEEN or len(_WARNINGS_SEEN) >= WARNING_MEMORY:
                continue
            _WARNINGS_SEEN.add(warning)
            fresh.append(warning)
    return fresh


def _reset_warnings():
    """Forget emitted warnings (tests only; a real process emits each one once)."""
    with _WARNINGS_LOCK:
        _WARNINGS_SEEN.clear()
