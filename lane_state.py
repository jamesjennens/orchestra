#!/usr/bin/env python3
"""The reserved ``Kind: lane-state-v1`` record (kittrial-5bb.263).

One small JSON object written beside a checkpoint in the same act, as
``docs/LANE_BOARD_DESIGN.md`` section 1 specifies: ``schema_version`` (1), ``task``,
``checkpoint`` (the comment id of the checkpoint written with it), ``state``,
``waiting_on`` and ``expect_by``. It is deliberately NOT part of the checkpoint object:
``briefing.checkpoint_problems`` builds a closed field set, and it stays closed. The
checkpoint writer pops the optional sibling ``lane_state`` key out of the payload file
before that check, so nothing existing grows a key and no older reader has to know this
record to keep reading and re-checkpointing a task.

Two properties follow from the separate form and are both tested:

* the prefix is in :data:`reserved_comments.RESERVED`, so a contributor's raw
  ``comments add`` can never write one, while the checkpoint writer posts it through the
  internal run path that also writes the checkpoint; and
* the kind is deliberately not in ``reserved_comments.RECORD_COMMENT_FAMILIES`` and
  anchors no row, so every surface -- an older kit included -- reads the comment as
  ordinary text and shows it in ``history``.

Every rule refuses rather than coerces: no person is inferred, no actor is turned into a
role, and a past ``expect_by`` is accepted (and flagged on read, ``expect_by_passed``)
while only an unparseable one is refused.
"""
import json
import re
from datetime import datetime, timezone

import record_json
from requirements import canonical_bytes
from reserved_comments import LANE_STATE_PREFIX as PREFIX, _reserved_prefix_view

SCHEMA_VERSION = 1
#: The five declared states. The four ``waiting-*`` values are the waiting states: a
#: ``waiting_on`` is required with one and refused with any other.
STATES = ('working', 'waiting-on-coordinator', 'waiting-on-review', 'waiting-on-user', 'idle')
WAITING_STATES = tuple(state for state in STATES if state.startswith('waiting-'))
#: The roles ``waiting_on`` may name instead of an actor id. The flag keys off the role;
#: an actor id is shown beside it and is never converted into a role by guessing.
ROLES = ('coordinator', 'reviewer', 'user')
#: The bound shared with every other actor-shaped field in the kit (field_limits).
WAITING_ON_LIMIT = 96
CHECKPOINT_LIMIT = 128
#: An actor id as the endpoint and the session registry accept one.
ACTOR = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}')

REQUIRED_FIELDS = ('schema_version', 'task', 'state')
#: ``checkpoint`` is NOT sent in the request: the writer sets it to the comment id it
#: publishes, which is the only way the record can be guaranteed to name the task's
#: newest checkpoint at write time. ``waiting_on`` and ``expect_by`` are optional by
#: rule. The written record's field set is exactly these six.
OPTIONAL_FIELDS = ('checkpoint', 'waiting_on', 'expect_by')
FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS


def parse_moment(value, label='expect_by'):
    """A timezone-aware ISO-8601 time, or raise. A PAST moment is a moment.

    The same reading ``activity.parse_moment`` gives every other time in the kit: a
    trailing ``Z`` is accepted as ``+00:00``, a naive value is refused because a wait
    state with no offset cannot be aged, and nothing else is guessed.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError('%s must be ISO-8601 with an offset, such as 2026-10-12T04:00:00+00:00' % label)
    text = value.strip()
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError('%s is not ISO-8601 with an offset: %r' % (label, value)) from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError('%s must carry a timezone offset or Z, so the wait can be aged: %r' % (label, value))
    return moment.astimezone(timezone.utc)


def _waiting_on_problems(value):
    if not isinstance(value, str) or not value.strip():
        return ['Invalid lane state: waiting_on must be a role (%s) or an actor id' % ', '.join(ROLES)]
    if len(value) > WAITING_ON_LIMIT:
        return ['Invalid lane state: waiting_on is longer than %d characters' % WAITING_ON_LIMIT]
    if value in ROLES or ACTOR.fullmatch(value):
        return []
    if not value.isprintable():
        return ['Invalid lane state: waiting_on must be a role (%s) or an actor id, not text with control '
                'characters' % ', '.join(ROLES)]
    return ['Invalid lane state: waiting_on must be a role (%s) or an actor id; %r is neither, and this kit '
            'infers no person' % (', '.join(ROLES), value)]


def problems(payload, task, checkpoint=None):
    """Every problem with one lane-state payload, in order, so one refusal names them all.

    ``checkpoint`` is the comment id of the checkpoint this record is written with. When
    it is ``None`` the record is being validated BEFORE that write (the id does not exist
    yet), so the field may be absent or present; the writer compares a supplied value to
    the id it publishes and refuses a mismatch, which is the "it is not the task's newest
    at write time" rule. When ``checkpoint`` is given, the field must be present and equal.
    """
    if not isinstance(payload, dict):
        return ['Invalid lane state: expected a JSON object']
    found = []
    unknown = sorted(set(payload) - set(FIELDS))
    missing = sorted(field for field in REQUIRED_FIELDS if field not in payload)
    if unknown:
        found.append('Invalid lane state: unknown fields: ' + ', '.join(unknown))
    if missing:
        found.append('Invalid lane state: missing fields: ' + ', '.join(missing))
    if 'schema_version' in payload and (type(payload['schema_version']) is not int
                                        or payload['schema_version'] != SCHEMA_VERSION):
        found.append('Invalid lane state: schema_version must be integer %d' % SCHEMA_VERSION)
    if 'task' in payload and payload['task'] != task:
        found.append('Invalid lane state: task mismatch (the record names %r, this checkpoint is on %r)'
                     % (payload.get('task'), task))
    state = payload.get('state')
    if 'state' in payload and state not in STATES:
        found.append('Invalid lane state: state must be one of ' + ', '.join(STATES))
    if 'checkpoint' in payload:
        value = payload['checkpoint']
        if checkpoint is None:
            # The request may not assert it: the writer knows the id only once it has
            # published the checkpoint, and the record must name the newest at write time.
            found.append('Invalid lane state: checkpoint is written by the checkpoint writer (the comment id '
                         'the same act publishes); omit it from the request and the writer sets it')
        elif not isinstance(value, str) or not value.strip() or len(value) > CHECKPOINT_LIMIT:
            found.append('Invalid lane state: checkpoint must be the comment id of the checkpoint written '
                         'with this record')
        elif value != checkpoint:
            found.append('Invalid lane state: checkpoint names %r, not the checkpoint this write just '
                         'published; the checkpoint write is the only writer, so a record cannot name '
                         'another checkpoint' % (value,))
    elif checkpoint is not None:
        found.append('Invalid lane state: checkpoint is missing; the writer sets it to the checkpoint it '
                     'publishes')
    waiting_on = payload.get('waiting_on')
    if isinstance(state, str) and state.startswith('waiting-'):
        if waiting_on is None:
            found.append('Invalid lane state: waiting_on is required when state is %r' % state)
        else:
            found.extend(_waiting_on_problems(waiting_on))
    elif waiting_on is not None:
        found.append('Invalid lane state: waiting_on is refused when state is %r; it names who is waited on '
                     'and is required only with a waiting state' % (state,))
    if 'expect_by' in payload:
        try:
            parse_moment(payload['expect_by'])
        except ValueError as error:
            found.append('Invalid lane state: ' + str(error))
    return found


def validate(payload, task, checkpoint=None):
    """Return ``payload`` when it is a valid lane-state record for ``task``, else raise."""
    found = problems(payload, task, checkpoint)
    if found:
        raise ValueError('; '.join(found))
    return payload


def is_record(text):
    """True when a comment body is a ``Kind: lane-state-v1`` record, lookalikes included.

    The BOM/CRLF view is the same one the reserved-prefix guard uses, so a body that is a
    lookalike is recognised here too (it can never be written raw in the first place).
    """
    if not isinstance(text, str):
        return False
    return any(view.startswith(PREFIX) for view in (text, _reserved_prefix_view(text)))


def parse(text):
    """The payload object of a lane-state-v1 body, or ``None``.

    ``None`` means "not this record" or "this record is malformed": a reader drops an
    unusable record exactly as it drops an invalid checkpoint from the linked chain, so
    one bad comment never fails the whole read.
    """
    if not is_record(text):
        return None
    try:
        # record_json.loads: a comment body is other people's text, so it is read through
        # the same bounded guard every other record comment uses (a too-deep one raises,
        # and this drops it like an invalid checkpoint rather than failing the whole read).
        payload = record_json.loads(_reserved_prefix_view(text)[len(PREFIX):])
    except (RecursionError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _stamp(comment):
    value = comment.get('created_at') if isinstance(comment, dict) else None
    return str(value or '')


def newest(comments, task):
    """``(record_comment, payload, checkpoint_comment)`` for the newest USABLE record.

    Usable means: the body parses, every field is valid for this task, and the checkpoint
    it names is a comment on this task -- so a record can never be shown beside a
    checkpoint it does not belong to. Malformed or unusable records are skipped, never
    raised; ``None`` means the task carries no usable record (an older checkpoint).
    """
    comments = [comment for comment in (comments or []) if isinstance(comment, dict)]
    ids = {str(comment.get('id')) for comment in comments if comment.get('id') is not None}
    usable = []
    for index, comment in enumerate(comments):
        payload = parse(comment.get('text'))
        if payload is None:
            continue
        named = payload.get('checkpoint')
        if not isinstance(named, str) or named not in ids:
            continue
        if problems(payload, task, named):
            continue
        usable.append((_stamp(comment), index, comment, payload))
    if not usable:
        return None
    usable.sort(key=lambda row: (row[0], row[1]))
    _, _, comment, payload = usable[-1]
    checkpoint = next(item for item in comments if str(item.get('id')) == payload['checkpoint'])
    return comment, payload, checkpoint


def record(payload, task, checkpoint):
    """The validated record dict that is written, with ``checkpoint`` set to the id."""
    written = dict(payload)
    written['checkpoint'] = checkpoint
    validate(written, task, checkpoint)
    return written


def body(payload, task, checkpoint):
    """The exact comment body the checkpoint writer posts for one lane-state record."""
    return PREFIX + canonical_bytes(record(payload, task, checkpoint)).decode()


def ensure(run, comments, actor, task, payload, checkpoint):
    """The lane-state record for this checkpoint, written now if it is not already there.

    A retry of a checkpoint request that was already recorded (``save_checkpoint``'s
    reconciled path) must not publish a second lane-state record, and a crash between the
    two writes leaves none: this completes it. Exact identity is the writing actor, the
    checkpoint it names and the same fields; a retry that would REVISE the state of an
    already-published checkpoint is refused, because a record is written only with its own
    checkpoint and names the newest checkpoint at that moment.
    """
    wanted = record(payload, task, checkpoint)
    for comment in comments or []:
        if not isinstance(comment, dict) or comment.get('author') != actor:
            continue
        existing = parse(comment.get('text'))
        if existing is None or existing.get('checkpoint') != checkpoint:
            continue
        if existing == wanted:
            return {'comment_id': str(comment.get('id')), 'reconciled': True}
        raise ValueError('The checkpoint was already published with a different lane state; a lane-state '
                         'record is written only with its own checkpoint and is never revised on a retry. '
                         'Write the change with a NEW checkpoint instead')
    result = json.loads(run(['comments', 'add', task, body(payload, task, checkpoint), '--json']))
    return {'comment_id': str(result['id']), 'reconciled': False}
