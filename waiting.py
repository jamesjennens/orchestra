#!/usr/bin/env python3
"""A blocking wait: poll cheaply, wake only when it is your move (kittrial-5bb.285).

The owner asked for the thing a harness does around its model: *instead of waking the
model every few minutes, run something that polls and returns only when there is work
for it* (as the VS Code bridge does for its agent), and the same for the coordinator.

This module is the ONE answer both forms and both transports share. It holds the
projection (the rows whose move is the caller's), the fingerprint that makes a restart
safe, the argument shapes, and the blocking loop. The loop never reads the tracker
itself: the caller passes a ``read`` callable, so the SSH client polls one endpoint
action and a web agent polls one HTTP route, and the tests pass a fake read and a fake
clock and never sleep.

Two forms:

* the **worker** form (``client.py ... wait``): one cheap read of the caller's own
  queue per poll; returns as soon as a row whose move is the caller's appears or
  changes.
* the **coordinator** form (``wait --coordinator``): one cheap read per poll covering
  the four things a coordinator wakes for - a new delivery awaiting review, approved
  work awaiting integration, a checkpoint addressed to the coordinator, and a lane
  quiet on its own move or past a declared wait (the lane board of kittrial-5bb.263).

ONE cheap read per poll (kittrial-5bb.281 decided the read path: a read takes the
project's coordination lock SHARED and one ``work`` read is one native snapshot): the
endpoint action of this kit loads ONE export and derives the queue and the lane board
from it, so a poll is one read and never a read per task or a second export.

Exit codes, for both forms and the HTTP read alike:

* ``0`` something is the caller's move: the rows and the fingerprint are on stdout.
* ``1`` the wait timed out (``--timeout``): the last read is on stdout with
  ``timed_out: true``.
* ``2`` the arguments or the endpoint refused the read (a usage error).
* ``3`` the service could not be reached, or answered something that is not a read.

The fingerprint is a short versioned string (``wait1:<64 hex>``) over the *stable*
fields of the move rows only. Ages and read times are deliberately NOT in it: a clock
moving is not a change, or every poll would wake the model. ``--since FINGERPRINT``
sets the state the caller already saw, so a restart that finds the same state blocks
instead of returning the same rows twice.

It is a library, imported by the client (``client.py wait``) and by the endpoint's one
read; it has no ``__main__`` block and is never started as a program.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import time

#: The output schema. Additive fields may be added to it; a reader that does not know
#: one ignores it, the way the rest of the kit's JSON is read.
WAIT_SCHEMA = 'wait-v1'

#: The default poll interval and the floor. The owner asked for "every few minutes";
#: 300 s is the cadence the harness used, and a poll that is cheaper than a wake may
#: run faster but never faster than one a minute (a tighter loop would be a busy wait
#: on the same shared lock every other reader needs).
DEFAULT_INTERVAL = 300
MIN_INTERVAL = 60

#: ``--idle-minutes`` is an argument of the lane board read, not a setting
#: (kittrial-5bb.263); the same bounds ``lanes.py`` enforces.
DEFAULT_IDLE_MINUTES = 15
IDLE_MINUTES_MIN, IDLE_MINUTES_MAX = 1, 10080

#: The backoff of kittrial-5bb.327. A wait that returns empty prints the wait to run next:
#: the timeout starts at ``--timeout`` and doubles after two empty returns in a row, never
#: above ``--max-timeout`` (whose default is the 3600 s cap) and never below the base
#: ``--timeout``. After ``--give-up-after`` seconds of the idle spell the caller started at
#: ``--idle-since`` (12 hours by default) the wait says to checkpoint and stop.
DEFAULT_MAX_TIMEOUT = 3600
DEFAULT_GIVE_UP_AFTER = 12 * 60 * 60
DOUBLING_AFTER = 2

#: The client's small local cache (kittrial-5bb.327 review items 6 and 7): beside its
#: config file the client keeps the last fingerprint it printed and the last item list,
#: so a call without ``--since`` resumes from that fingerprint instead of re-reading the
#: baseline, and a wake can say which task fields changed and which items were added or
#: removed. The SERVER stays stateless; this file is the caller's own memory. It is
#: bounded (see MAX_STATE_ITEMS) and holds only the work fields already printed, never a
#: config value or a credential.
WAIT_STATE_SCHEMA = 'wait-state-v1'
WAIT_STATE_NAME = '.orchestra-wait-state.json'
MAX_STATE_ITEMS = 100
MAX_STATE_TEXT = 200
MAX_STATE_LIST = 20
MAX_STATE_BYTES = 64 * 1024

#: An idle-since instant is ISO-8601 WITH an offset (``2026-10-17T15:00:00-04:00``, ``...Z``).
#: A naive value is refused: it would need the caller's zone, which the server does not know.
_INSTANT = re.compile(r'^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})'
                      r'(?:\.(\d{1,6}))?(?:[Zz]|([+-])(\d{2}):?(\d{2}))$')
_INSTANT_ADVICE = ('Invalid wait: --idle-since takes an ISO-8601 time with an offset '
                   '(for example 2026-10-17T15:00:00-04:00)')
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

EXIT_WORK = 0
EXIT_TIMEOUT = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3

#: The stable fields a move row is fingerprinted over. Everything here changes only
#: when the task's work state changes; an age or a generated_at is not here. The
#: canonical ``work`` row and the web service's queue row name a couple of these
#: differently, so :func:`normalize_item` flattens both spellings before the
#: fingerprint is taken and the two transports cannot disagree about one task.
MOVE_FIELDS = ('task', 'review_state', 'whose_move', 'next_actor',
               'contribution_id', 'pending_review_items', 'open_items', 'blocking_items',
               'checkpoint_at', 'newer_activity', 'newer_activity_marker',
               'redelivered', 'guidance_version')

#: The fields a WORKER's move row is fingerprinted over (kittrial-5bb.369). It drops what
#: only the caller's own checkpoint changes - ``checkpoint_at``, the two checkpoint item
#: counts, the checkpoint-relative ``newer_activity`` boolean and the marker that resets
#: on that checkpoint - and replaces the marker with the checkpoint-INDEPENDENT
#: ``wait_newer_activity_marker`` (newest entry by another actor, plus the current
#: title/description/acceptance digest). Everything by others still wakes: a comment or
#: an edit moves that marker, and ``review_state``, ``whose_move``, ``next_actor``,
#: ``pending_review_items``, ``owner`` (the assignment), ``status`` (claim/release),
#: ``contribution_id`` and ``redelivered`` are carried directly. The coordinator form
#: keeps :data:`MOVE_FIELDS` unchanged.
WORKER_MOVE_FIELDS = ('task', 'review_state', 'whose_move', 'next_actor', 'owner', 'status',
                      'contribution_id', 'pending_review_items', 'redelivered',
                      'guidance_version', 'wait_newer_activity_marker')

#: The four things a coordinator wakes for, in the order the task names them.
COORDINATOR_TRIGGERS = ('awaiting-review', 'awaiting-integration',
                        'checkpoint-to-coordinator', 'lane-quiet')

#: The lane board flags a coordinator acts on: a lane quiet on its own move, or past a
#: declared wait. ``waiting_too_long`` and ``expect_by_passed`` are the declared waits;
#: ``your_move_idle`` is the lane's own move with no write for too long.
LANE_FLAGS = ('your_move_idle', 'waiting_too_long', 'expect_by_passed')

#: The review states that mean "a delivery is waiting for a reviewer" and "approved
#: work is waiting for integration", from the one review-state vocabulary.
AWAITING_REVIEW_STATES = ('awaiting-review', 'legacy-review-ready')
AWAITING_INTEGRATION_STATES = ('awaiting-integration', 'approved')

FINGERPRINT = 'wait1:'


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


class WaitUnreachable(Exception):
    """The service could not be reached, or did not answer a wait read (exit 3)."""


class WaitRefused(Exception):
    """The endpoint reached but refused the read (exit 2)."""


def _scalar(value):
    """A value the fingerprint can carry: bool/int/float/str/None, or its text."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def normalize_item(item):
    """One move row reduced to its stable fields, JSON-safe.

    A canonical ``work`` row and the web service's queue row of the same task carry the
    same facts under different names, so both are flattened here and the fingerprint
    cannot depend on which transport read the row: ``pending_review_items`` is the
    canonical count, ``open_requests`` the web row's count and ``pending_request_ids``
    its ids, and an ``attention`` block is lifted onto the row. No age is carried.
    """
    if not isinstance(item, dict):
        return {}
    row = {name: _scalar(item.get(name)) for name in MOVE_FIELDS}
    # A work row names the assignee ``owner``; the web queue row names it ``assignee``.
    # Carry either under ``owner`` so the worker fingerprint cannot depend on transport.
    row['wait_newer_activity_marker'] = _scalar(item.get('wait_newer_activity_marker'))
    row['owner'] = _scalar(item.get('owner') if item.get('owner') is not None else item.get('assignee'))
    row['status'] = _scalar(item.get('status'))
    attention = item.get('attention')
    if isinstance(attention, dict):
        for name in ('open_items', 'blocking_items', 'checkpoint_at', 'newer_activity',
                     'newer_activity_marker', 'wait_newer_activity_marker',
                     'redelivered', 'pending_review_items'):
            if row.get(name) is None and name in attention:
                row[name] = _scalar(attention.get(name))
    if not isinstance(row.get('pending_review_items'), int):
        pending = item.get('open_requests')
        if isinstance(pending, int):
            row['pending_review_items'] = pending
        else:
            ids = item.get('pending_request_ids')
            if isinstance(ids, (list, tuple)):
                row['pending_review_items'] = len(ids)
    if row.get('contribution_id') is None:
        contribution = item.get('contribution')
        if isinstance(contribution, dict) and contribution.get('id') is not None:
            row['contribution_id'] = _scalar(contribution.get('id'))
    # The stable newer-activity marker is fingerprinted (it is in MOVE_FIELDS). The
    # counts it was read with are carried for the reader but deliberately NOT
    # fingerprinted: incrementing `newer_activity_own` for the caller's own comment
    # must not wake the caller that wrote it (kittrial-5bb.351). The current fields
    # digest (kittrial-5bb.367) is carried the same way: the marker already moves on
    # a field edit, so fingerprinting the digest too would be redundant.
    for name in ('newer_activity_by_others', 'newer_activity_own',
                 'newer_activity_fields', 'newer_activity_coverage', 'unresolved_directions'):
        if row.get(name) is None:
            value = item.get(name)
            if value is None and isinstance(attention, dict):
                value = attention.get(name)
            row[name] = _scalar(value)
    return row


def _fingerprinted(row):
    """The stable subset a fingerprint is taken over; extra reader fields stay out."""
    return {name: row.get(name) for name in MOVE_FIELDS}


def _worker_fingerprinted(row):
    """The worker fingerprint's subset: no field that only the caller's own checkpoint moves."""
    return {name: row.get(name) for name in WORKER_MOVE_FIELDS}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _sorted_rows(rows):
    """Rows in one deterministic order: by task id, then by their own canonical text."""
    return sorted(rows, key=lambda row: (str(row.get('task') or row.get('lane') or ''),
                                         _canonical(row)))


def digest(value):
    """The versioned fingerprint of any JSON value (``wait1:<sha256 hex>``)."""
    return FINGERPRINT + hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def fingerprint(items):
    """The fingerprint of a list of move rows."""
    return digest(_sorted_rows([_fingerprinted(normalize_item(item))
                                for item in items if isinstance(item, dict)]))


def is_fingerprint(value):
    """Whether ``value`` is a fingerprint this kit writes."""
    return bool(isinstance(value, str) and len(value) == len(FINGERPRINT) + 64
                and value.startswith(FINGERPRINT)
                and all(character in '0123456789abcdef' for character in value[len(FINGERPRINT):]))


def state_path(config_path, project=None, actor=None, form='worker'):
    """Where the client keeps the wait's small local state (review items 6 and 7).

    The path is derived from the caller's ``--config`` path - never a hard-coded
    ``~/.orchestra`` - so it lives in the SAME DIRECTORY as the config file, on Windows
    and POSIX alike (``os.path`` joins the components). One file per caller identity
    (project, actor and form) keeps a worker's list from being compared with a
    coordinator's, or one project's with another's; the identity is a short hash and is
    not itself stored in the file. ``None`` when the caller has no config path.
    """
    if not config_path:
        return None
    directory = os.path.dirname(os.path.abspath(config_path))
    identity = '\x00'.join(str(value or '') for value in (project, actor, form))
    suffix = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
    stem, extension = os.path.splitext(WAIT_STATE_NAME)
    return os.path.join(directory, '%s-%s%s' % (stem, suffix, extension))


def _state_value(value, depth=0):
    """One item value as the state may carry it: JSON-safe, bounded text, bounded lists.

    Nesting is cut at four levels (a value deeper than that becomes its text), so no
    payload can make this recursion expensive or deep.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_STATE_TEXT]
    if isinstance(value, (list, tuple)):
        if depth >= 4:
            return str(value)[:MAX_STATE_TEXT]
        return [_state_value(item, depth + 1) for item in list(value)[:MAX_STATE_LIST]]
    text = str(value)
    return text[:MAX_STATE_TEXT]


def _state_row(row):
    """One item reduced to the work fields the printed view already carries.

    Nothing else is stored: no config, no credential, no actor or host record, only task
    ids and the stable fields of the row. Text and lists are bounded so the file stays
    small and cannot grow with an unusual payload.
    """
    return dict((name, _state_value(row[name])) for name in sorted(row))


def read_state(path):
    """The state last written at ``path``, or ``None``.

    A missing, unreadable, corrupt or foreign file is treated as ABSENT: a wait works
    from the caller's flags alone (a call without ``--since`` then returns the whole
    baseline) and the next return rewrites it. Nothing here may break a wait: the text is
    bounded before it is parsed and a recursive payload is caught rather than raised.
    """
    if not path:
        return None
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            text = handle.read(MAX_STATE_BYTES + 1)
        if len(text) > MAX_STATE_BYTES:
            return None
        value = json.loads(text)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None
    if not isinstance(value, dict) or value.get('schema') != WAIT_STATE_SCHEMA:
        return None
    remembered = value.get('fingerprint')
    if not is_fingerprint(remembered):
        remembered = None
    items = value.get('items')
    if not isinstance(items, list):
        items = []
    items = [_state_row(item) for item in items[:MAX_STATE_ITEMS] if isinstance(item, dict)]
    return {'schema': WAIT_STATE_SCHEMA, 'fingerprint': remembered, 'items': items}


def write_state(path, fingerprint, items):
    """Write the small cache after a return; a failure is ignored.

    The file is written to a temporary name in the same directory and moved into place,
    so a concurrent reader never sees a half-written file. It is capped at
    ``MAX_STATE_BYTES``: a larger item list is dropped and only the fingerprint kept (the
    next wake then reports the whole list as added), so this client can always read back
    what it wrote. A directory this process cannot write (or a path occupied by a
    directory) leaves the caller with no cache, which is exactly the first-run behaviour:
    the wait itself never fails because of it.
    """
    if not path or not is_fingerprint(fingerprint) or not isinstance(items, list):
        return False
    payload = {'schema': WAIT_STATE_SCHEMA, 'fingerprint': fingerprint,
               'items': [_state_row(item) for item in items[:MAX_STATE_ITEMS]
                         if isinstance(item, dict)]}

    def encoded(value):
        return json.dumps(value, ensure_ascii=True, sort_keys=True,
                          separators=(',', ':'))

    try:
        text = encoded(payload)
    except (ValueError, TypeError):
        return False
    if len(text) > MAX_STATE_BYTES:
        payload['items'] = []
        try:
            text = encoded(payload)
        except (ValueError, TypeError):
            return False
        if len(text) > MAX_STATE_BYTES:
            return False
    temporary = '%s.%d.tmp' % (path, os.getpid())
    try:
        with open(temporary, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.write('\n')
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError, UnicodeError):
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass
        return False
    return True


def _item_key(row):
    """The name a changed block uses for one item: its task, else its lane, else its digest."""
    for name in ('task', 'lane'):
        if row.get(name) is not None:
            return str(row.get(name))
    return digest(row)


def _keyed_items(items):
    """The items of one view by key, in their printed order; a duplicate key gets ``#2``."""
    keyed = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = _item_key(item)
        if key in keyed:
            number = 2
            while '%s#%d' % (key, number) in keyed:
                number += 1
            key = '%s#%d' % (key, number)
        keyed[key] = _state_row(item)
    return keyed


def changed_items(old_items, new_items):
    """What a wake changed (review item 6): changed fields old -> new, added, removed.

    ``old_items`` is the item list the previous call printed (from the state file) and
    ``new_items`` this wake's list. Items are matched by ``task`` (a lane row by its
    ``lane``); a duplicate key in the printed order gets ``#2`` and so on. ``tasks``
    carries one entry per matched item with at least one field whose value differs, each
    as ``{field, old, new}``; ``added`` and ``removed`` are the keys in only one list. A
    first call with no stored list compares against an empty list, so the whole baseline
    is reported as added. The block is additive: no existing key, exit code or ordering
    guarantee changes.
    """
    old = _keyed_items(old_items)
    new = _keyed_items(new_items)
    tasks = []
    for key in sorted(set(old) & set(new)):
        fields = []
        for name in sorted(set(old[key]) | set(new[key])):
            before, after = old[key].get(name), new[key].get(name)
            if before != after:
                fields.append({'field': name, 'old': before, 'new': after})
        if fields:
            tasks.append({'task': key, 'fields': fields})
    return {'tasks': tasks,
            'added': sorted(set(new) - set(old)),
            'removed': sorted(set(old) - set(new))}


def parse_instant(text):
    """The epoch seconds of an ISO-8601 instant that carries an offset.

    Python 3.6 has no ``datetime.fromisoformat``, so the shape is parsed here once for
    both copies of the wait (``waiting.py`` and the client's inlined copy). A value with
    no offset is refused: an idle spell is measured against the caller's clock.
    """
    if not isinstance(text, str):
        raise ValueError(_INSTANT_ADVICE)
    match = _INSTANT.match(text.strip())
    if match is None:
        raise ValueError(_INSTANT_ADVICE)
    if match.group(8) is not None:
        hours, minutes = int(match.group(9)), int(match.group(10))
        if hours > 23 or minutes > 59:
            raise ValueError(_INSTANT_ADVICE)
        offset = datetime.timedelta(hours=hours, minutes=minutes)
        if match.group(8) == '-':
            offset = -offset
    else:
        offset = datetime.timedelta(0)
    fraction = match.group(7) or ''
    micro = int((fraction + '000000')[:6])
    try:
        moment = datetime.datetime(int(match.group(1)), int(match.group(2)),
                                   int(match.group(3)), int(match.group(4)),
                                   int(match.group(5)), int(match.group(6)), micro,
                                   tzinfo=datetime.timezone(offset))
    except (ValueError, OverflowError):
        raise ValueError(_INSTANT_ADVICE) from None
    return (moment - _EPOCH).total_seconds()


def format_instant(epoch):
    """The ISO-8601 UTC spelling of an epoch (``2026-10-17T19:55:21Z``).

    Seconds are the unit of the schedule; a fractional part is written only when it is
    there, so an instant this kit prints is an instant this kit parses back exactly.
    """
    moment = _EPOCH + datetime.timedelta(seconds=float(epoch))
    if moment.microsecond:
        return moment.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def _seconds(value):
    """A duration as JSON and as command text: ``600`` rather than ``600.0`` when it is whole."""
    value = float(value)
    return int(value) if value.is_integer() else value


def timeout_cap(base, max_timeout):
    """The ceiling for one wait: ``--max-timeout`` when given, else the 3600 s default cap.

    The base timeout is a floor as well as a start, so a smaller ``--max-timeout`` cannot
    shorten the caller's own ``--timeout``.
    """
    cap = DEFAULT_MAX_TIMEOUT if max_timeout is None else max_timeout
    return max(base, cap)


def schedule_timeout(index, base, cap):
    """The ``index``-th (1-based) wait's timeout in the schedule this kit prints.

    The first two waits are the base timeout and every later wait doubles - that is what
    "after two empty returns in a row" means - up to ``cap`` and never below ``base``.
    """
    if base <= 0:
        return 0
    if index <= DOUBLING_AFTER:
        return base
    if index - DOUBLING_AFTER > 62:
        return cap
    return min(base * float(2 ** (index - DOUBLING_AFTER)), cap)


def empty_returns(elapsed, base, cap):
    """How many empty returns an idle spell of ``elapsed`` seconds implies.

    This is the stateless rule: the client has no memory of earlier invocations, so it
    assumes the idle spell was spent in waits of the schedule's own durations and counts
    how many of them fit in ``elapsed`` (the time since ``--idle-since``, including the
    wait that just ended). A caller that runs each printed ``next_command`` walks the
    schedule exactly: one completed wait gives the base, two give the base, the third
    gives double, and so on.
    """
    if base <= 0:
        return 0
    remaining = max(0.0, float(elapsed))
    count = 0
    index = 1
    while True:
        step = schedule_timeout(index, base, cap)
        if step >= cap:
            if remaining >= step:
                count += int(remaining // step)
            return count
        if remaining < step:
            return count
        remaining -= step
        count += 1
        index += 1


def wait_command(prefix, fingerprint, idle_since, timeout, interval=DEFAULT_INTERVAL,
                 form='worker', idle_minutes=DEFAULT_IDLE_MINUTES, max_timeout=None,
                 give_up_after=DEFAULT_GIVE_UP_AFTER):
    """The exact ``wait`` command to run next, as one line the caller can copy.

    ``prefix`` is how the caller invokes the client (its config/project/actor part when
    the client builds it); the flags echo everything this invocation was given, so the
    next wait keeps the same form, cadence and bounds. ``--idle-since`` is the value the
    schedule uses (unchanged after an empty return, fresh after work), and ``--since`` is
    the fingerprint just printed.
    """
    parts = [prefix, 'wait']
    if form == 'coordinator':
        parts += ['--coordinator', '--idle-minutes', str(idle_minutes)]
    parts += ['--interval', str(_seconds(interval))]
    if fingerprint:
        parts += ['--since', fingerprint]
    parts += ['--idle-since', idle_since, '--timeout', str(_seconds(timeout))]
    if max_timeout is not None:
        parts += ['--max-timeout', str(_seconds(max_timeout))]
    if give_up_after != DEFAULT_GIVE_UP_AFTER:
        parts += ['--give-up-after', str(_seconds(give_up_after))]
    return ' '.join(parts)


def movement(items):
    """The rows whose move is the caller's, normalized and ordered."""
    return _sorted_rows([normalize_item(item) for item in items or []
                         if isinstance(item, dict) and item.get('whose_move') == 'you'])


def worker_view(items, claiming=None, empty_message=None):
    """The worker form's answer: the rows whose move is the caller's plus a fingerprint.

    ``claiming`` is the project's claiming setting this read used, and
    ``empty_message`` the sentence an empty listing carries under ``assigned``
    (kittrial-5bb.352). Both are additive: a reader that does not know them ignores
    them, and a project with no project directory keeps exactly the old answer.
    """
    rows = movement(items)
    view = {'schema': WAIT_SCHEMA, 'form': 'worker', 'items': rows,
            'fingerprint': digest(_sorted_rows([_worker_fingerprinted(row) for row in rows]))}
    if claiming:
        view['claiming'] = claiming
    if not rows and empty_message:
        view['empty_message'] = empty_message
    return view


def _lane_row(lane, entry=None):
    """One lane-board row reduced to its stable fields (no ages, no generated_at)."""
    if entry is None:
        return {'lane': _scalar(lane.get('lane')), 'actor': _scalar(lane.get('actor')),
                'task': None, 'state': _scalar(lane.get('state')),
                'waiting_on': _scalar(lane.get('waiting_on')),
                'whose_move': None, 'next_actor': None, 'review_state': None,
                'flags': sorted(set(lane.get('flag_tasks') or {}))}
    return {'lane': _scalar(lane.get('lane')), 'actor': _scalar(lane.get('actor')),
            'task': _scalar(entry.get('task')), 'state': _scalar(entry.get('state')),
            'waiting_on': _scalar(entry.get('waiting_on')),
            'whose_move': _scalar(entry.get('whose_move')),
            'next_actor': _scalar(entry.get('next_actor')),
            'review_state': _scalar(entry.get('review_state')),
            'flags': sorted(set(entry.get('flags') or []))}


def coordinator_view(queue_items, lanes_payload):
    """The coordinator form's answer: the four trigger groups plus a fingerprint.

    ``queue_items`` are the canonical ``work`` rows of one read (any review state);
    ``lanes_payload`` is one ``lanes-v1`` read derived from the SAME export. Nothing is
    read here: both projections are already in hand, so a coordinator poll is the one
    cheap read the worker form makes, with the lane board derived from it.

    The trigger groups are deliberately kept per trigger: a coordinator can see which
    of the four things woke it, and a test can hold each one to its own rule.
    """
    lanes = lanes_payload if isinstance(lanes_payload, dict) else {}
    triggers = {name: [] for name in COORDINATOR_TRIGGERS}
    for item in queue_items or []:
        if not isinstance(item, dict):
            continue
        state = item.get('review_state') or item.get('work_state')
        if state in AWAITING_REVIEW_STATES and item.get('contribution_id'):
            triggers['awaiting-review'].append(normalize_item(item))
        elif state in AWAITING_INTEGRATION_STATES:
            triggers['awaiting-integration'].append(normalize_item(item))
    for lane in lanes.get('items') or []:
        if not isinstance(lane, dict):
            continue
        lane_flags = set()
        for name in LANE_FLAGS:
            if lane.get(name):
                lane_flags.add(name)
        lane_flags.update(name for name in (lane.get('flag_tasks') or {}) if name in LANE_FLAGS)
        if lane_flags:
            row = _lane_row(lane)
            row['flags'] = sorted(lane_flags)
            triggers['lane-quiet'].append(row)
        for entry in lane.get('held') or []:
            if not isinstance(entry, dict):
                continue
            addressed = (entry.get('state') == 'waiting-on-coordinator'
                         or entry.get('waiting_on') == 'coordinator'
                         or entry.get('next_actor') == 'coordinator')
            if addressed:
                triggers['checkpoint-to-coordinator'].append(_lane_row(lane, entry))
    grouped = {name: _sorted_rows(rows) for name, rows in triggers.items()}
    flat = [dict(row, trigger=name) for name in COORDINATOR_TRIGGERS for row in grouped[name]]
    fingerprinted = {name: _sorted_rows([_fingerprinted(row) for row in rows])
                     for name, rows in triggers.items()}
    return {'schema': WAIT_SCHEMA, 'form': 'coordinator', 'triggers': grouped,
            'items': flat, 'fingerprint': digest(fingerprinted)}


def parse_read_args(args):
    """The arguments the ENDPOINT read takes (never the client's own poll options)."""
    parser = Parser(prog='wait', add_help=False)
    parser.add_argument('--coordinator', action='store_true')
    parser.add_argument('--idle-minutes', type=int, default=DEFAULT_IDLE_MINUTES,
                        dest='idle_minutes')
    parser.add_argument('--json', action='store_true')  # the output is always JSON
    parsed = parser.parse_args(args)
    if not IDLE_MINUTES_MIN <= parsed.idle_minutes <= IDLE_MINUTES_MAX:
        raise ValueError('Invalid wait read: --idle-minutes must be %d..%d'
                         % (IDLE_MINUTES_MIN, IDLE_MINUTES_MAX))
    return parsed


def parse_client_args(args):
    """The client's own arguments: the read options plus the poll's bounds."""
    parser = Parser(prog='wait', add_help=False)
    parser.add_argument('--coordinator', action='store_true')
    parser.add_argument('--idle-minutes', type=int, default=DEFAULT_IDLE_MINUTES,
                        dest='idle_minutes')
    parser.add_argument('--interval', type=int, default=DEFAULT_INTERVAL)
    parser.add_argument('--timeout', type=float, default=None)
    parser.add_argument('--since')
    parser.add_argument('--fresh', action='store_true')
    parser.add_argument('--idle-since')
    parser.add_argument('--max-timeout', type=float, default=None, dest='max_timeout')
    parser.add_argument('--give-up-after', type=float, default=DEFAULT_GIVE_UP_AFTER,
                        dest='give_up_after')
    parser.add_argument('--json', action='store_true')  # the output is always JSON
    parsed = parser.parse_args(args)
    if parsed.interval < MIN_INTERVAL:
        raise ValueError('Invalid wait: --interval must be at least %d seconds (the default is %d)'
                         % (MIN_INTERVAL, DEFAULT_INTERVAL))
    if parsed.timeout is not None and parsed.timeout < 0:
        raise ValueError('Invalid wait: --timeout must be zero or more seconds')
    if parsed.since is not None and not is_fingerprint(parsed.since):
        raise ValueError('Invalid wait: --since takes a fingerprint this kit printed '
                         '(wait1:<64 hex>)')
    if parsed.idle_since is not None:
        parse_instant(parsed.idle_since)  # refuse a bad TIME before any read happens
    if parsed.max_timeout is not None and parsed.max_timeout <= 0:
        raise ValueError('Invalid wait: --max-timeout must be more than zero seconds')
    if parsed.give_up_after <= 0:
        raise ValueError('Invalid wait: --give-up-after must be more than zero seconds')
    if not IDLE_MINUTES_MIN <= parsed.idle_minutes <= IDLE_MINUTES_MAX:
        raise ValueError('Invalid wait: --idle-minutes must be %d..%d'
                         % (IDLE_MINUTES_MIN, IDLE_MINUTES_MAX))
    return parsed


def read_args(options):
    """The endpoint arguments for one poll, from the client's parsed options.

    The client's poll bounds (``--interval``, ``--timeout``, ``--since``) are the
    CLIENT's: the endpoint is asked for one read, not for a blocking call, so an HTTP
    route and a bridge extension can use exactly the same read.
    """
    args = []
    if options.coordinator:
        args += ['--coordinator', '--idle-minutes', str(options.idle_minutes)]
    return args


def run_wait(read, since=None, timeout=None, interval=DEFAULT_INTERVAL,
             clock=None, sleep=None, form=None, idle_since=None, max_timeout=None,
             give_up_after=DEFAULT_GIVE_UP_AFTER, command_prefix='client.py',
             idle_minutes=DEFAULT_IDLE_MINUTES, now=None, state_file=None, fresh=False):
    """Block, polling ``read`` every ``interval`` seconds, until something is the move.

    ``read`` is called once per poll and returns one view (the payload of
    :func:`worker_view` or :func:`coordinator_view`); it raises :class:`WaitUnreachable`
    when the service cannot be reached and :class:`WaitRefused` when it refused. The
    clock and the sleep are resolved from this module at call time (so a caller, or a
    test, may replace ``time.monotonic``/``time.sleep`` or pass its own), and a test
    drives the wait with a fake clock and no real sleeping.

    ``since`` is the fingerprint the caller already saw, or ``None`` to return at the
    first read that already has something for the caller. ``timeout`` bounds the WHOLE
    wait in seconds (``None`` waits without a bound). Returns ``(exit_code, view)``.

    ``idle_since`` is the ISO-8601 instant at which the caller's current idle spell
    began, and it turns the backoff of kittrial-5bb.327 ON: a timeout then carries
    ``next_timeout``, ``next_command`` and ``give_up`` (and, on give-up, ``message``),
    and a return with items resets the schedule. ``now`` is the epoch of this call's
    start for tests; ``command_prefix`` is how the caller invokes the client. A caller
    that passes neither ``idle_since`` nor ``timeout`` sees exactly the old view.

    ``state_file`` is the client's small local cache (review items 6 and 7): its last
    fingerprint and last item list. When it has a fingerprint and the caller gave no
    ``since``, that fingerprint is the state the caller resumes from, so a restarted
    session does not re-read the baseline; ``fresh`` ignores it and asks for the baseline
    again. A wake also carries ``changed`` - the field-by-field difference from the
    stored list, plus the items added and removed - inside the existing output. A
    missing, unreadable or corrupt file is treated as absent and rewritten by the next
    return; a write that fails leaves the wait itself unaffected. ``state_file=None``
    keeps the old, cacheless behaviour.
    """
    clock = time.monotonic if clock is None else clock
    sleep = time.sleep if sleep is None else sleep
    form = form or 'worker'
    started = clock()
    polls = 0
    state = read_state(state_file)
    previous = state.get('items') if state else []
    remembered = None if fresh or state is None else state.get('fingerprint')
    resumed = since if since is not None else remembered
    # The schedule is on only when the caller names the instant its idle spell began AND
    # bounds this wait: an unbounded wait never returns empty, so there is no next command
    # to print. Callers that pass neither new flag keep the old output exactly.
    scheduled = idle_since is not None and timeout is not None
    if scheduled:
        idle_started = parse_instant(idle_since)
        wall_now = time.time() if now is None else now
        idle_before = max(0.0, wall_now - idle_started)
        cap = timeout_cap(timeout, max_timeout)

    def with_schedule(view, elapsed, reset):
        """The view plus the backoff fields, when the caller asked for them."""
        if not scheduled:
            return view
        fingerprint = view.get('fingerprint')
        if reset:
            # Work was returned: the next lone wait starts a fresh idle spell and the
            # base timeout again. The fresh instant is this return, so the printed
            # command is the exact one for the state the caller is in now.
            fields = {
                'next_timeout': _seconds(timeout),
                'next_command': wait_command(command_prefix, fingerprint,
                                             format_instant(wall_now + elapsed), timeout,
                                             interval, form, idle_minutes,
                                             max_timeout, give_up_after),
                'give_up': False,
            }
            return dict(view, **fields)
        idle_elapsed = idle_before + elapsed
        done = empty_returns(idle_elapsed, timeout, cap)
        upcoming = schedule_timeout(done + 1, timeout, cap)
        give_up = idle_elapsed >= give_up_after
        fields = {
            'next_timeout': _seconds(upcoming),
            'next_command': wait_command(command_prefix, fingerprint, idle_since, upcoming,
                                         interval, form, idle_minutes, max_timeout,
                                         give_up_after),
            'give_up': give_up,
        }
        if give_up:
            fields['message'] = ('checkpoint and stop: nothing has been your move since %s'
                                 % idle_since)
        return dict(view, **fields)

    def remember(view):
        """Store this return's fingerprint and item list, if the client keeps a cache."""
        if state_file is not None:
            write_state(state_file, view.get('fingerprint'), view.get('items') or [])

    def with_changes(view):
        """The view plus the changed block, when the client keeps a cache."""
        if state_file is None:
            return view
        return dict(view, changed=changed_items(previous, view.get('items') or []))

    while True:
        polls += 1
        try:
            view = read()
        except WaitUnreachable as error:
            return EXIT_UNREACHABLE, {'schema': WAIT_SCHEMA, 'form': form,
                                      'fingerprint': None, 'items': [],
                                      'error': 'the service cannot be reached: %s' % str(error)[:240],
                                      'polls': polls}
        except WaitRefused as error:
            return EXIT_USAGE, {'schema': WAIT_SCHEMA, 'form': form,
                                'fingerprint': None, 'items': [],
                                'error': str(error)[:300], 'polls': polls}
        if not isinstance(view, dict):
            return EXIT_UNREACHABLE, {'schema': WAIT_SCHEMA, 'form': form,
                                      'fingerprint': None, 'items': [],
                                      'error': 'the service cannot be reached: it did not answer a wait read',
                                      'polls': polls}
        if resumed is None:
            if view.get('items'):
                answer = with_changes(with_schedule(dict(view, polls=polls),
                                                    clock() - started, reset=True))
                remember(answer)
                return EXIT_WORK, answer
        elif view.get('fingerprint') != resumed:
            # Work resets the schedule; a change with no item for this caller is activity
            # but not work, so the idle spell (and the backoff) continues.
            answer = with_changes(with_schedule(dict(view, polls=polls),
                                                clock() - started,
                                                reset=bool(view.get('items'))))
            remember(answer)
            return EXIT_WORK, answer
        elapsed = clock() - started
        if timeout is not None and elapsed >= timeout:
            answer = with_schedule(dict(view, polls=polls, timed_out=True), elapsed,
                                   reset=False)
            remember(answer)
            return EXIT_TIMEOUT, answer
        nap = interval if timeout is None else min(interval, max(0.0, timeout - elapsed))
        if nap <= 0:
            answer = with_schedule(dict(view, polls=polls, timed_out=True), elapsed,
                                   reset=False)
            remember(answer)
            return EXIT_TIMEOUT, answer
        sleep(nap)
