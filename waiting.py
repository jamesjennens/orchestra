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
import hashlib
import json
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
               'checkpoint_at', 'newer_activity', 'redelivered', 'guidance_version')

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
    attention = item.get('attention')
    if isinstance(attention, dict):
        for name in ('open_items', 'blocking_items', 'checkpoint_at', 'newer_activity',
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
    return row


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
    return digest(_sorted_rows([normalize_item(item) for item in items
                                if isinstance(item, dict)]))


def is_fingerprint(value):
    """Whether ``value`` is a fingerprint this kit writes."""
    return bool(isinstance(value, str) and len(value) == len(FINGERPRINT) + 64
                and value.startswith(FINGERPRINT)
                and all(character in '0123456789abcdef' for character in value[len(FINGERPRINT):]))


def movement(items):
    """The rows whose move is the caller's, normalized and ordered."""
    return _sorted_rows([normalize_item(item) for item in items or []
                         if isinstance(item, dict) and item.get('whose_move') == 'you'])


def worker_view(items):
    """The worker form's answer: the rows whose move is the caller's plus a fingerprint."""
    rows = movement(items)
    return {'schema': WAIT_SCHEMA, 'form': 'worker', 'items': rows,
            'fingerprint': digest(rows)}


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
    return {'schema': WAIT_SCHEMA, 'form': 'coordinator', 'triggers': grouped,
            'items': flat, 'fingerprint': digest(grouped)}


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
             clock=None, sleep=None, form=None):
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
    """
    clock = time.monotonic if clock is None else clock
    sleep = time.sleep if sleep is None else sleep
    form = form or 'worker'
    started = clock()
    polls = 0
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
        if since is None:
            if view.get('items'):
                return EXIT_WORK, dict(view, polls=polls)
        elif view.get('fingerprint') != since:
            return EXIT_WORK, dict(view, polls=polls)
        elapsed = clock() - started
        if timeout is not None and elapsed >= timeout:
            return EXIT_TIMEOUT, dict(view, polls=polls, timed_out=True)
        nap = interval if timeout is None else min(interval, max(0.0, timeout - elapsed))
        if nap <= 0:
            return EXIT_TIMEOUT, dict(view, polls=polls, timed_out=True)
        sleep(nap)
