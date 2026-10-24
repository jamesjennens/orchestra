#!/usr/bin/env python3
"""One bounded read of the lanes board (kittrial-5bb.263; docs/LANE_BOARD_DESIGN.md section 3).

ONE native export plus the project's session registry, one pass, and NO per-task comment
read: ``work.execute`` has already loaded the export for the ``work`` action, and every row
here is derived from that snapshot and the registry. The aging and the three flags are
computed here once; ``work lanes`` (the client) and the endpoint expose exactly this, so
there is no second implementation of either.

The read is read-only and therefore safe on the contributor route, but the CROSS-LANE view
is an operator/coordinator concern: on the contributor route (``scope='own'``, which the
endpoint passes for a key that is not bound to a configured operator) a caller sees only
its own lane's row. A member of another project sees nothing at all, which
``endpoint.key_project_refusal`` already enforces before any read.

Every row's ``review_state``/``whose_move``/``next_actor`` come from the SAME shared
projection and the SAME decision as the work queue -- ``work.workflow`` and
``work.move_decision`` -- never from a second rule. ``whose_move`` here is the HOLDER's
move (the decision taken with the lane as the caller), so ``'you'`` is the lane that must
act; ``next_actor`` is that same decision's caller-neutral role.
"""
import argparse
from datetime import datetime, timezone

DEFAULT_IDLE_MINUTES = 15
IDLE_MINUTES_MIN, IDLE_MINUTES_MAX = 1, 10080          # one minute .. seven days
LANES_LIMIT_MIN, LANES_LIMIT_MAX = 1, 100
LANES_LIMIT_DEFAULT = 20
#: "who is free": a registered session that holds nothing and wrote within this window.
IDLE_WINDOW_HOURS = 24
#: The open-item kinds the coordinator helper keeps; the prose fallback below uses them.
PROSE_WAIT_ITEMS = ('blocker', 'question', 'dependency')
FLAG_NAMES = ('waiting_too_long', 'expect_by_passed', 'your_move_idle')
COVERAGE = ('One lane per registered session name; a task with no lane reads as its assignee actor. '
            'The declared state, who it waits on and expect_by come from the newest usable '
            'Kind: lane-state-v1 record; inferred true marks a row whose state is not declared. '
            'Read-only, from one export and the session registry; no per-task comment scan.')


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parse_args(args):
    """The arguments of the read. ``--idle-minutes`` is an argument, never a setting."""
    parser = Parser(prog='work lanes', add_help=False)
    parser.add_argument('--idle-minutes', type=int, default=DEFAULT_IDLE_MINUTES, dest='idle_minutes')
    parser.add_argument('--limit', type=int, default=LANES_LIMIT_DEFAULT)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--json', action='store_true')   # the output is always JSON
    parsed = parser.parse_args(args)
    if not IDLE_MINUTES_MIN <= parsed.idle_minutes <= IDLE_MINUTES_MAX:
        raise ValueError('Invalid lanes read: --idle-minutes must be %d..%d'
                         % (IDLE_MINUTES_MIN, IDLE_MINUTES_MAX))
    if not LANES_LIMIT_MIN <= parsed.limit <= LANES_LIMIT_MAX or parsed.offset < 0:
        raise ValueError('Invalid lanes page: --limit must be %d..%d and --offset must be >= 0'
                         % (LANES_LIMIT_MIN, LANES_LIMIT_MAX))
    return parsed


def moment(value):
    """A timezone-aware UTC datetime for an ISO-8601 string, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def seconds_since(value, now):
    """Whole seconds between an ISO-8601 value and ``now``, or ``None``.

    A value in the future reads 0 (never a negative age); an unreadable value reads
    ``None`` and raises no flag, because nothing is known rather than guessed.
    """
    when = moment(value)
    return None if when is None else max(0, int((now - when).total_seconds()))


def _newest(values):
    found = [value for value in (moment(item) for item in values) if value is not None]
    return max(found) if found else None


def registry_names(registry):
    """``actor -> registered lane name`` from a validated session registry."""
    records = registry.get('records') if isinstance(registry, dict) else None
    names = {}
    for record in (records or {}).values():
        if isinstance(record, dict) and isinstance(record.get('actor'), str):
            names[record['actor']] = str(record.get('name') or record['actor'])
    return names


def lane_writes(rows, registry, actor):
    """The newest write by one lane anywhere: any comment plus its registry activity.

    The registry half counts a run start/heartbeat/end and a resume, so a session that
    has only heartbeat activity is still aged correctly.
    """
    stamps = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for comment in row.get('comments') or []:
            if isinstance(comment, dict) and comment.get('author') == actor:
                stamps.append(comment.get('created_at'))
    for key in ('runs', 'resumes'):
        for item in (registry.get(key) or {}).values():
            if isinstance(item, dict) and item.get('actor') == actor:
                stamps.extend([item.get('started_at'), item.get('last_heartbeat'),
                               item.get('ended_at'), item.get('timestamp')])
    return _newest(stamps)


def declared_state(row, task):
    """The newest usable ``Kind: lane-state-v1`` record on a task.

    ``(record_comment, payload, checkpoint_comment)`` or ``None`` when the task's
    checkpoint carries no usable record (an older checkpoint, or one whose record is
    malformed -- a bad record is skipped, never raised).
    """
    from lane_state import newest
    return newest(row.get('comments') or [], task)


def newest_checkpoint(row):
    """``(payload, comment)`` of the task's newest VALID checkpoint, or ``None``."""
    from briefing import checkpoint_state
    try:
        state = checkpoint_state(row, normalize=False)
    except (ValueError, TypeError, KeyError):
        return None
    return state.get('current')


def inferred_wait(checkpoint_payload):
    """``('waiting-on-coordinator', 'coordinator')`` when prose is all the row has.

    The coordinator helper's own rule, kept ONLY for a checkpoint with no
    ``lane-state-v1`` record: any open item of kind blocker/question/dependency, or
    ``coordinator`` in ``next_action``. It is a guess and the row is marked
    ``inferred: true``, so a reader can always tell declared from guessed.
    """
    if not isinstance(checkpoint_payload, dict):
        return None
    if 'coordinator' in str(checkpoint_payload.get('next_action') or '').lower():
        return ('waiting-on-coordinator', 'coordinator')
    for item in checkpoint_payload.get('open_items') or []:
        if isinstance(item, dict) and item.get('kind') in PROSE_WAIT_ITEMS:
            return ('waiting-on-coordinator', 'coordinator')
    return None


def holder_idle_seconds(row, actor, now):
    """How long this lane has written nothing on a task.

    The newest comment by the holder on the task; when it never wrote one, the task's own
    ``updated_at`` (then ``created_at``) is the age of the hold.
    """
    stamps = [comment.get('created_at') for comment in row.get('comments') or []
              if isinstance(comment, dict) and comment.get('author') == actor]
    newest = _newest(stamps)
    if newest is not None:
        return max(0, int((now - newest).total_seconds()))
    return seconds_since(row.get('updated_at') or row.get('created_at'), now)


def held_task(row, task, actor, now, threshold, review=None, error=None, comment_authority=None):
    """One held task's row: the shared review facts plus the declared wait state."""
    from lane_state import parse_moment
    if error is not None:
        state = work_state = 'error'
        move, next_actor_value, text = 'unknown', 'operator', error
        contribution, pending_items, redelivered = {}, 0, False
    else:
        review = review or {}
        state = review.get('review_state')
        work_state = review.get('work_state', state)
        contribution = review.get('contribution') or {}
        pending_requests = review.get('pending_requests') or []
        pending_items = len(pending_requests)
        contribution_id = contribution.get('comment_id')
        redelivered = bool(contribution_id) and bool(pending_requests) and all(
            str(pending.get('contribution') or '') != str(contribution_id) for pending in pending_requests)
        from work import claimable, move_decision
        from briefing import assignee_comment_pending
        row_claimable = claimable(row.get('issue_type'), row.get('status'), row.get('assignee'),
                                  contribution.get('comment_id'), state, labels=row.get('labels'))
        # THE one whose-move decision (kittrial-5bb.6). The decision is taken with the
        # LANE as the caller, so whose_move 'you' is the holder's own move; next_actor is
        # that same decision's caller-neutral role.
        move, text, next_actor_value = move_decision(
            state, task, True, pending_items=pending_items, redelivered=redelivered,
            assignee=row.get('assignee'), closed=row.get('status') == 'closed',
            claimable=row_claimable,
            comment_pending=assignee_comment_pending(row, response=review.get('last_response'), authority=comment_authority),
            # The shared projection carries the newest hand-back block, and when it
            # applies it made the move the assignee's (kittrial-5bb.343). The lane
            # board passes that SAME block through the ONE decision, so a lane row
            # cannot keep saying the coordinator's move while work/brief/wait say the
            # holder's.
            followon=review.get('followon') if isinstance(review, dict) else None)
    found = declared_state(row, task)
    checkpoint = newest_checkpoint(row)
    declared = found is not None
    wait_state = waiting_on = expect_by = None
    checkpoint_id = None
    since = None
    if declared:
        _record, payload, checkpoint_comment = found
        checkpoint_id = str(payload.get('checkpoint'))
        since = checkpoint_comment.get('created_at')
        wait_state = payload.get('state')
        waiting_on = payload.get('waiting_on')
        expect_by = payload.get('expect_by')
    else:
        if checkpoint is not None:
            checkpoint_id = str(checkpoint[1].get('id'))
            since = checkpoint[1].get('created_at')
            guessed = inferred_wait(checkpoint[0])
            if guessed is not None:
                wait_state, waiting_on = guessed
    age = seconds_since(since, now)
    expect_moment = moment(expect_by) if isinstance(expect_by, str) else None
    idle_seconds = holder_idle_seconds(row, actor, now)
    flags = []
    if wait_state in ('waiting-on-coordinator', 'waiting-on-review', 'waiting-on-user') \
            and age is not None and age > threshold:
        flags.append('waiting_too_long')
    if expect_moment is not None and expect_moment < now:
        flags.append('expect_by_passed')
    if move == 'you' and idle_seconds is not None and idle_seconds > threshold:
        flags.append('your_move_idle')
    return {
        'task': task,
        'title': str(row.get('title') or '')[:200],
        'review_state': state,
        'work_state': work_state,
        'whose_move': move,
        'next_actor': next_actor_value,
        'next': text,
        'state': wait_state,
        'waiting_on': waiting_on,
        'expect_by': expect_by,
        'declared': declared,
        'inferred': not declared,
        'checkpoint': checkpoint_id,
        'state_since': since,
        'state_age_seconds': age,
        'no_checkpoint': checkpoint is None,
        'held_age_seconds': seconds_since(row.get('updated_at') or row.get('created_at'), now),
        'holder_last_write_seconds': idle_seconds,
        'flags': flags,
        'pending_review_items': pending_items,
        'redelivered': redelivered,
        'contribution_id': contribution.get('comment_id') if isinstance(contribution, dict) else None,
    }


def _review(row, task, evidence, revert_map, revert_problems, operators, journal):
    """The shared review projection for one row, or ``(None, message)`` when it is malformed."""
    from work import workflow
    scopes = evidence.get(task)
    try:
        return workflow(row, scopes, operators=operators, reverts=revert_map.get(task, []),
                        journal=journal, invalid_reverts=revert_problems.get(task)), None
    except ValueError as error:
        return None, str(error)[:300]


def read(rows, actor, registry, args, now=None, operators=None, journal=None, scope='all', comment_authority=None):
    """The bounded lanes page. ``rows`` is the export, ``registry`` the session registry.

    ``scope`` is ``'all'`` on the host route and the bound-key coordinator route, and
    ``'own'`` on the contributor route: ``'own'`` filters the page to the caller's own
    lane before the page is counted and cut, so another lane's row is never read out.
    """
    parsed = parse_args(args)
    if now is None:
        now = datetime.now(timezone.utc)
    threshold = parsed.idle_minutes * 60
    from lifecycle import integration_evidence
    from review_state import reverts_by_task
    from reserved_comments import is_record_anchor
    from coordination import is_merge_slot
    evidence = {item['id']: item['scopes'] for item in integration_evidence(rows)
                if isinstance(item, dict) and item.get('id')}
    if journal is not None:
        revert_map, revert_problems = reverts_by_task(rows, operators, journal)
    else:
        revert_map, revert_problems = {}, {}
    names = registry_names(registry)
    groups = {}
    notice_groups = {}
    from task_assignment import notices as assignment_notices, parse_comment
    for row in rows:
        if not isinstance(row, dict) or not row.get('id') or row.get('malformed'):
            continue
        if row.get('issue_type') in ('event', 'gate') or is_merge_slot(row) or is_record_anchor(row):
            continue
        project_name=journal.name if journal is not None else ''
        involved=set()
        for comment in row.get('comments') or []:
            notice=parse_comment(comment,project=project_name,task=row['id'])
            if notice:involved.update(x for x in (notice['from'],notice['to']) if x)
        for who in involved:
            block=assignment_notices(row,project_name,who)
            if block['total']:
                group=notice_groups.setdefault(who,{'items':[],'total':0})
                group['items'].extend(block['items']);group['total']+=block['total']
        if row.get('status') == 'closed' or not row.get('assignee'):

            continue
        task = str(row['id'])
        holder = row['assignee']
        group = groups.setdefault(holder, [])
        group.append((task, row))
    lanes = []
    from briefing import comment_author_allowed
    if comment_authority is None:
        comment_authority = lambda author: comment_author_allowed(author,operators,(registry or {}).get('owners'))
    for holder, held in groups.items():
        held.sort(key=lambda item: item[0])
        entries = []
        for task, row in held:
            review, error = _review(row, task, evidence, revert_map, revert_problems, operators, journal)
            entries.append(held_task(row, task, holder, now, threshold, review=review, error=error,
                                     comment_authority=comment_authority))
        flags = {}
        for name in FLAG_NAMES:
            named = [entry['task'] for entry in entries if name in entry['flags']]
            if named:
                flags[name] = named
        last_write = lane_writes(rows, registry, holder)
        lanes.append({
            'lane': names.get(holder, holder),
            'actor': holder,
            'idle': False,
            'held': entries,
            'state': None,
            'waiting_on': None,
            'expect_by': None,
            'inferred': False,
            'state_since': None,
            'state_age_seconds': None,
            'last_write': last_write.isoformat().replace('+00:00', 'Z') if last_write else None,
            'last_write_age_seconds': None if last_write is None else max(0, int((now - last_write).total_seconds())),
            'waiting_too_long': 'waiting_too_long' in flags,
            'expect_by_passed': 'expect_by_passed' in flags,
            'your_move_idle': 'your_move_idle' in flags,
            'flag_tasks': flags,
        })
    # Lanes that hold nothing: every registered session whose newest write anywhere is
    # inside the idle window is listed idle with the age of that write, so "who is free"
    # is answered by the same read.
    window = IDLE_WINDOW_HOURS * 3600
    for holder, name in sorted(names.items()):
        if holder in groups:
            continue
        last_write = lane_writes(rows, registry, holder)
        if last_write is None:
            continue
        age = max(0, int((now - last_write).total_seconds()))
        if age > window:
            continue
        lanes.append({
            'lane': name,
            'actor': holder,
            'idle': True,
            'held': [],
            'state': 'idle',
            'waiting_on': None,
            'expect_by': None,
            'inferred': False,
            'state_since': None,
            'state_age_seconds': None,
            'last_write': last_write.isoformat().replace('+00:00', 'Z'),
            'last_write_age_seconds': age,
            'waiting_too_long': False,
            'expect_by_passed': False,
            'your_move_idle': False,
            'flag_tasks': {},
        })
    existing={lane['actor'] for lane in lanes}
    for holder in sorted(set(notice_groups)-existing):
        lanes.append(dict(lane=names.get(holder,holder),actor=holder,idle=False,held=[],
                          state=None,waiting_on=None,expect_by=None,inferred=False,state_since=None,
                          state_age_seconds=None,last_write=None,last_write_age_seconds=None,
                          waiting_too_long=False,expect_by_passed=False,your_move_idle=False,flag_tasks={}))
    for lane in lanes:
        holder_notices=notice_groups.get(lane['actor'])
        if holder_notices:
            kept=holder_notices['items'][:20]
            lane['assignment_notices']={'items':kept,'total':holder_notices['total'],
                                        'omitted':max(0,holder_notices['total']-len(kept))}
    # kittrial-5bb.397: each lane carries the count of its worker waits that woke with
    # nothing to act on in the last 24 hours, and the newest one's task and trigger.
    # Additive fields; they are not lane flags and they are not in the coordinator's
    # stable move fields, so no existing row or fingerprint changes because of them.
    if journal is not None:
        import wake_log
        wakes=wake_log.summaries(journal,[lane.get('actor') for lane in lanes],now=now)
        for lane in lanes:
            summary=wakes.get(lane.get('actor')) or wake_log.DEFAULT_SUMMARY
            lane['empty_wakes_24h']=summary.get('empty_wakes_24h',0)
            lane['empty_wake_task']=summary.get('empty_wake_task')
            lane['empty_wake_trigger']=summary.get('empty_wake_trigger')
            lane['empty_wake_at']=summary.get('empty_wake_at')
    else:
        for lane in lanes:
            lane['empty_wakes_24h']=0
            lane['empty_wake_task']=None
            lane['empty_wake_trigger']=None
            lane['empty_wake_at']=None
    if scope == 'own':
        lanes = [row for row in lanes if row['actor'] == actor]
    lanes.sort(key=lambda row: (0 if row['held'] else 1, 0 if row['flag_tasks'] else 1,
                                str(row['lane']), str(row['actor'])))
    page = lanes[parsed.offset:parsed.offset + parsed.limit]
    return {
        'schema': 'lanes-v1',
        'generated_at': now.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'idle_minutes': parsed.idle_minutes,
        'scope': scope,
        'total': len(lanes),
        'items': page,
        'next_offset': parsed.offset + parsed.limit if parsed.offset + parsed.limit < len(lanes) else None,
        'flags': {name: sum(1 for row in lanes if row.get(name)) for name in FLAG_NAMES},
        'coverage': COVERAGE,
    }
