"""The lane nudge request: records, closed lists, limits and expiry (kittrial-5bb.413).

Slice 1 of docs/design/LANE_NUDGE_DESIGN.md (kittrial-5bb.409). A coordinator asks, through an
audited server-written record, that a fixed message reach a stalled lane. This module is the pure
half: what a request may say, the record the server keeps in ``state['nudges'][project]``, the rate
limits and the lazy expiry. Authority, the lane read, the audit and the write are the service's
(``Service.request_nudge``); there is no carrier yet, so a request that nobody claims ends as
``not-deliverable`` once its time is up. Nothing here reads a file, a clock or the network: the
caller passes ``now`` as epoch seconds.

Library only: it has no ``__main__`` block.
"""
import re
import secrets

SCHEMA = 'lane-nudge-v1'

#: The closed message set (design section 2). Only the key is ever stored or sent; the text belongs
#: to a carrier (slice 2), so no request can make a lane receive anything but one of these.
MESSAGES = ('resume', 'restart')
REASONS = ('stalled-at-prompt', 'api-error', 'lost-wake', 'no-answer', 'other')
NOTE_MAX = 200

PENDING = 'pending'
NOT_DELIVERABLE = 'not-deliverable'
NO_CARRIER = 'no-carrier'

#: A request nobody carried out is over after this long (design section 1.3 rule 5).
PENDING_TTL = 15 * 60
KEEP_RECORDS = 200
KEEP_SECONDS = 30 * 86400

#: Starting bounds of design section 1.3 rule 3. ``gap``: seconds between two requests for one lane;
#: ``lane_hour``: per lane per hour; ``project_hour`` and ``agent_hour``: per project, per agent. Every
#: record counts, whoever wrote it, so an agent cannot nudge a lane a person has just nudged.
PERSON_LIMITS = {'gap': 5 * 60, 'lane_hour': 6, 'project_hour': 30}
AGENT_LIMITS = {'gap': 15 * 60, 'lane_hour': 3, 'agent_hour': 10, 'agents_project_hour': 10}
HOUR = 3600

WAIT_STATES = ('waiting-on-coordinator', 'waiting-on-review', 'waiting-on-user')
LANE_LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}')
CONTROL = re.compile(r'[\x00-\x1f\x7f]')

#: Fixed sentences: the closed ``waiting_on`` value is the only thing that varies.
DECLARED_WAIT = 'This lane says it is waiting on %s since %s. Nudge it anyway? Send the request again with acknowledge_declared_wait set to true.'
RATE_LIMITED = 'Too many nudges for this lane or project. Try again in %d seconds.'
NOT_HERE = 'Lane not found'
OWN_PARTY = 'An agent cannot nudge its own lane or the lane of its own account.'


def parse_request(payload):
    """The checked fields of one request body; ``ValueError`` names the first bad one.

    The closed lists are exact: a value outside them is refused, never mapped. The note is audit-only
    text, at most ``NOTE_MAX`` characters and no control character; one that is too long is refused,
    not cut.
    """
    lane = payload.get('lane')
    if not isinstance(lane, str) or not LANE_LABEL.fullmatch(lane):
        raise ValueError('lane must be the lane\'s actor label (1-96 characters: letters, digits and . _ @ / -)')
    message = payload.get('message')
    if message not in MESSAGES:
        raise ValueError('message must be one of %s' % ', '.join(MESSAGES))
    reason = payload.get('reason')
    if reason not in REASONS:
        raise ValueError('reason must be one of %s' % ', '.join(REASONS))
    note = payload.get('note')
    if note is not None:
        if not isinstance(note, str) or len(note) > NOTE_MAX or CONTROL.search(note):
            raise ValueError('note must be text of at most %d characters with no control character' % NOTE_MAX)
        note = note.strip() or None
    acknowledged = payload.get('acknowledge_declared_wait', False)
    if type(acknowledged) is not bool:
        raise ValueError('acknowledge_declared_wait must be true or false')
    return {'lane': lane, 'message': message, 'reason': reason, 'note': note,
            'acknowledge_declared_wait': acknowledged}


def declared_wait(row):
    """The lane's own declared wait on a lanes-board row, or ``None``.

    Only a DECLARED ``waiting-on-*`` state counts (a guess from prose does not): the held task whose
    state is the oldest, as ``{task, state, waiting_on, since, whose_move}``. ``whose_move`` is kept
    so the audit shows whether the declaration agreed with the board's own decision.
    """
    found = []
    for entry in (row.get('held') if isinstance(row, dict) else None) or []:
        if isinstance(entry, dict) and entry.get('declared') and entry.get('state') in WAIT_STATES:
            found.append(entry)
    if not found:
        return None
    entry = min(found, key=lambda held: (str(held.get('state_since') or ''), str(held.get('task') or '')))
    return {'task': str(entry.get('task') or '')[:120], 'state': entry['state'],
            'waiting_on': str(entry.get('waiting_on') or '')[:40] or None,
            'since': entry.get('state_since') if isinstance(entry.get('state_since'), str) else None,
            'whose_move': entry.get('whose_move') if isinstance(entry.get('whose_move'), str) else None}


def waiting_sentence(snapshot):
    return DECLARED_WAIT % (snapshot.get('waiting_on') or snapshot['state'], snapshot.get('since') or 'an unknown time')


def new_record(project_id, fields, requested_by, snapshot, now, iso):
    """One ``lane-nudge-v1`` record, pending. ``iso`` turns epoch seconds into the stored time text."""
    return {'schema': SCHEMA, 'id': 'nudge-' + secrets.token_hex(12), 'project': project_id,
            'lane': fields['lane'], 'message': fields['message'], 'reason': fields['reason'],
            'note': fields['note'], 'requested_by': requested_by, 'requested_at': iso(now),
            'requested_ts': now, 'declared_wait': snapshot, 'state': PENDING,
            'claimed_by': None, 'claim_expires_at': None, 'outcome_at': None, 'outcome_detail': None}


def _stamp(record):
    stamp = record.get('requested_ts')
    return stamp if type(stamp) in (int, float) else 0


def expired(record, now):
    return record.get('state') == PENDING and now - _stamp(record) >= PENDING_TTL


def effective(record, now, iso):
    """The record as it stands at ``now``: a pending one past its time is ``not-deliverable``.

    Reading never writes (``Service.request_nudge`` settles the stored ones on the next request);
    with no carrier in this slice, ``no-carrier`` is the only reason a request goes unanswered.
    """
    if not expired(record, now):
        return record
    return dict(record, state=NOT_DELIVERABLE, outcome_detail=NO_CARRIER, outcome_at=iso(_stamp(record) + PENDING_TTL))


def settle(records, now, iso):
    """Write the lazy expiry and the bound into ``records``; ``True`` when anything changed."""
    changed = False
    for position, record in enumerate(records):
        if isinstance(record, dict) and expired(record, now):
            records[position] = effective(record, now, iso)
            changed = True
    finished = [record for record in records if isinstance(record, dict) and record.get('state') != PENDING]
    drop = [record for record in finished if now - _stamp(record) > KEEP_SECONDS]
    spare = len(records) - len(drop) - KEEP_RECORDS
    if spare > 0:
        # Only records older than an hour go for the bound: the newest hour still counts toward the
        # limits. It holds at most 30 persons' and 10 agents' records per project (and a lane's own).
        kept = [record for record in finished if record not in drop and now - _stamp(record) >= HOUR]
        drop.extend(kept[:spare])
    if drop:
        records[:] = [record for record in records if not any(record is gone for gone in drop)]
        changed = True
    return changed


def pending_for(records, lane, message, now):
    """The open request for this lane and message, or ``None`` (a double click is one nudge)."""
    for record in reversed(records):
        if (isinstance(record, dict) and record.get('lane') == lane and record.get('message') == message
                and record.get('state') == PENDING and not expired(record, now)):
            return record
    return None


def retry_after(records, lane, kind, agent_id, now):
    """Seconds until a new request for this lane is allowed, or ``0``.

    ``records`` are the project's, settled. Each bound is the one of the requester's kind (design
    section 1.3 rule 3); each counts every record in its window, whoever wrote it.
    """
    limits = AGENT_LIMITS if kind == 'agent' else PERSON_LIMITS
    # A stamp in the future (the clock stepped back) counts for nothing: it must not block a lane.
    stamps = [(record, stamp) for record, stamp in ((r, _stamp(r)) for r in records if isinstance(r, dict))
              if stamp <= now]
    lane_stamps = sorted(stamp for record, stamp in stamps if record.get('lane') == lane)
    waits = []
    if lane_stamps and now - lane_stamps[-1] < limits['gap']:
        waits.append(lane_stamps[-1] + limits['gap'] - now)
    recent = [stamp for stamp in lane_stamps if now - stamp < HOUR]
    if len(recent) >= limits['lane_hour']:
        waits.append(recent[len(recent) - limits['lane_hour']] + HOUR - now)

    def hourly(of_kind, bound, only_agent=None):
        mine = sorted(stamp for record, stamp in stamps if now - stamp < HOUR
                      and ((record.get('requested_by') or {}).get('kind') == 'agent') == of_kind
                      and (only_agent is None or (record.get('requested_by') or {}).get('agent_id') == only_agent))
        if len(mine) >= bound:
            waits.append(mine[len(mine) - bound] + HOUR - now)
    # Agents' records never count against persons: each kind has its own project bound.
    if kind == 'agent':
        hourly(True, limits['agent_hour'], agent_id)
        hourly(True, limits['agents_project_hour'])
    else:
        hourly(False, limits['project_hour'])
    return int(max(waits) + 1) if waits else 0


def display(requested_by, users, agents):
    """Who asked, as a sentence part: the person's display name, or the agent's name and ``(agent)``."""
    if not isinstance(requested_by, dict):
        return 'unknown'
    if requested_by.get('kind') == 'agent':
        agent = agents.get(requested_by.get('agent_id')) if isinstance(agents, dict) else None
        name = agent.get('name') if isinstance(agent, dict) else None
        return '%s (agent)' % (str(name)[:60] if name else 'an agent')
    user = users.get(requested_by.get('user_id')) if isinstance(users, dict) else None
    name = user.get('display_name') if isinstance(user, dict) else None
    return str(name)[:60] if name else 'a coordinator'


def view(record, users, agents):
    """What a reader is shown: never the note, a user id, a credential or the declared-wait detail."""
    return {'id': record.get('id'), 'lane': record.get('lane'), 'message': record.get('message'),
            'reason': record.get('reason'), 'state': record.get('state'),
            'requested_at': record.get('requested_at'),
            'requested_by_display': display(record.get('requested_by'), users, agents),
            'outcome_at': record.get('outcome_at'), 'outcome_detail': record.get('outcome_detail'),
            'declared_wait': bool(record.get('declared_wait'))}


def row_view(shown):
    """The lanes row's field: what the board needs, without the reason or the lane (design 1.4)."""
    if not isinstance(shown, dict):
        return None
    return {key: shown.get(key) for key in ('id', 'message', 'state', 'requested_at', 'requested_by_display',
                                            'outcome_at', 'outcome_detail')}
