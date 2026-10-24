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
"""
import fcntl
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import waiting

#: An in-process lock beside the file lock: two waits can be two threads of one process
#: (a client and a service in the same process in a test), and flock is per open file
#: description rather than per process, so the thread lock is what makes the pair safe.
_THREAD_LOCK = threading.Lock()

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


def _read_audit(audit, now):
    """The stored entries inside the window, oldest first; malformed lines are skipped.

    A reader never locks: the writer only ever replaces the file atomically, so a
    reader sees the old file or the new one, never a partial line.
    """
    _refuse_symlink(audit)
    try:
        with open(audit, 'rb') as handle:
            raw = handle.read(MAX_BYTES * 8)
    except OSError:
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
    """Replace ``path`` with ``text`` from a temporary file in the same directory."""
    _refuse_symlink(path)
    temporary = path.with_name('.%s.%d.%d.tmp' % (path.name, os.getpid(),
                                                   threading.get_ident()))
    _refuse_symlink(temporary)
    try:
        with open(temporary, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        try:
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()
        except OSError:
            pass


class _Lock:
    """An exclusive ``flock`` on the lane's lock file (refusing a symlinked one)."""

    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        _refuse_symlink(self.path)
        self.handle = open(self.path, 'a+', encoding='utf-8')
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc):
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
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
    directory, audit, _snapshot, lock = place
    moment = _now(now)
    try:
        _ensure_directory(directory)
        with _THREAD_LOCK, _Lock(lock):
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


def normalize_row(row):
    """One queue row reduced to the fields two waits compare, JSON-safe."""
    if not isinstance(row, dict):
        return None
    task = row.get('task') if row.get('task') is not None else row.get('id')
    if task is None:
        return None
    owner = row.get('owner') if row.get('owner') is not None else row.get('assignee')
    review_state = row.get('review_state')
    return {
        'task': str(task)[:TASK_TEXT],
        'review_state': _scalar(review_state),
        'work_state': _scalar(row.get('work_state') if row.get('work_state') is not None
                              else review_state),
        'owner': _scalar(owner),
        'assignee': _scalar(owner),
        'status': _scalar(row.get('status')),
        'contribution_id': _scalar(row.get('contribution_id')),
        'pending_review_items': _scalar(row.get('pending_review_items')),
        'redelivered': _scalar(row.get('redelivered')),
        'guidance_version': _scalar(row.get('guidance_version')),
        'wait_newer_activity_marker': _scalar(row.get('wait_newer_activity_marker')),
        'whose_move': _scalar(row.get('whose_move')),
        'next_actor': _scalar(row.get('next_actor')),
    }


def _scalar(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)[:200]


def _trigger(diff):
    for names, name in TRIGGER_ORDER:
        if any(field in diff for field in names):
            return name
    return 'edit'


def changed_wakes(previous, rows, view):
    """The changed rows behind one wake: ``[{'task', 'trigger', 'whose_move'}]``.

    ``previous`` is the bounded row list the caller last read (from ``.last.json``),
    ``rows`` the caller's full queue rows now, and ``view`` the printed worker view
    (its ``items`` are the rows that are actually the caller's move).  A row is
    changed when any field in :data:`WATCH_FIELDS` differs; added and removed rows
    count too.  The returned list is empty when the rows are the same.
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
                            'whose_move': (after or {}).get('whose_move')})
            continue
        if after is None:
            changed.append({'task': task, 'trigger': 'edit', 'whose_move': None})
            continue
        diff = [field for field in WATCH_FIELDS if before.get(field) != after.get(field)]
        if diff:
            changed.append({'task': task, 'trigger': _trigger(diff),
                            'whose_move': after.get('whose_move')})
    return changed


def _write_snapshot(project, actor, fingerprint, rows, now):
    """Keep the fingerprint and the bounded rows the caller just read, atomically."""
    place = paths(project, actor)
    if place is None:
        return False
    directory, _audit, snapshot, lock = place
    _ensure_directory(directory)
    normalized = [normalize_row(row) for row in rows or []]
    normalized = [row for row in normalized if row is not None]
    with _THREAD_LOCK, _Lock(lock):
        return waiting.write_state(str(snapshot), fingerprint, normalized)


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
        if project is None or not isinstance(actor, str) or not actor:
            return warnings
        if not isinstance(view, dict):
            return warnings
        fingerprint = view.get('fingerprint')
        if not isinstance(fingerprint, str) or not waiting.is_fingerprint(fingerprint):
            return warnings
        moment = _now(now)
        previous = []
        place = paths(project, actor)
        if place is None:
            return warnings
        try:
            state = waiting.read_state(str(place[2]))
            previous = state.get('items') if state else []
        except Exception as error:                            # noqa: BLE001 - best effort
            warnings.append('empty-wake log: could not read the last rows: %s' % str(error)[:160])
        changed = changed_wakes(previous, rows, view)
        if (isinstance(since, str) and waiting.is_fingerprint(since)
                and fingerprint != since and changed and not (view.get('items') or [])):
            entries = [{
                'schema': SCHEMA,
                'at': _stamp(moment),
                'actor': actor,
                'fingerprint': fingerprint,
                'task': item['task'],
                'trigger': item['trigger'],
                'empty': True,
            } for item in changed]
            try:
                append(project, actor, entries, now=moment)
            except Exception as error:                        # noqa: BLE001 - best effort
                warnings.append('empty-wake log: could not record: %s' % str(error)[:160])
        try:
            if not _write_snapshot(project, actor, fingerprint, rows, moment):
                warnings.append('empty-wake log: could not keep the last rows')
        except Exception as error:                            # noqa: BLE001 - best effort
            warnings.append('empty-wake log: could not keep the last rows: %s' % str(error)[:160])
    except Exception as error:                                # noqa: BLE001 - best effort
        warnings.append('empty-wake log: %s' % str(error)[:160])
    return warnings
