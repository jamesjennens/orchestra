"""Delegated decision rights: server-written delegation records and their live check.

kittrial-5bb.407 (slice 1 of docs/design/TRACEABILITY_DESIGN.md, as changed by the owner's
direction of 2026-10-25). A person owner may delegate a decision right to a coordinator, a
person or an agent, for a stated scope and with a stated escalation line. The first right is
``record-acceptance``: an agent holding the project's coordinator grant may accept or verify
a reference or capability record only within a current delegation, and that delegation is
also what lifts the own-party refusal (the endpoint's ``party_check``).

Why a reserved record and not lines in an issue body: a native issue's author (``created_by``)
is only the ``--actor`` label of whoever wrote it, and over the host route that label is not
bound for an unbound key and can be any unused name for a bound one (endpoint
``key_principal_refusal``; reference_records.decision_acceptance_rules says the same). So the
delegation is a ``Kind: delegation-v1`` comment on a decision issue, a reserved prefix that a
raw comment cannot write (reserved_comments), written only by the dedicated operation:

- the web route, for a person owner's own web SESSION (never an agent credential), whose
  live role in the project is ``owner``; ``via: session`` and the account are server-filled;
- the operator host path (``admin.py delegation-record``, also reachable by a bound key as a
  coordinator command), for an actor on the installation's operator allowlist;
  ``via: operator``.

Every use is checked live: the grant's recorder must still be a person owner (or still a
listed operator) when the delegation is used, the agent's coordinator grant must still be
effective, and the newest narrow or revoke record wins. Nothing is cached across a write.
This module has no store and runs no native command itself; callers pass the issue row.
"""
import datetime
import re

import record_json
from requirements import canonical_bytes, content_hash

PREFIX = 'Kind: delegation-v1\n'
LABEL = 'delegation'
RIGHTS = ('record-acceptance',)
SCOPES = ('references', 'capabilities')
ACTIONS = ('grant', 'narrow', 'revoke')
VIA = ('session', 'operator')
GRANTEE = re.compile(r'(?:agent_[0-9a-f]{16}|coordinator-grant)\Z')
ACCOUNT = re.compile(r'usr_[0-9a-f]{16}\Z')
ACTOR = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}\Z')
ISSUE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}\Z')
PROJECT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z')
ESCALATE_MAX = 300
REASON_MAX = 1000
REQUEST_FIELDS = frozenset({'schema_version', 'operation_id', 'decision', 'title', 'action', 'right',
                            'grantee', 'scope', 'escalate', 'reason'})
TITLE_MAX = 200
RECORD_FIELDS = REQUEST_FIELDS | frozenset({'project', 'recorded_by', 'via', 'account', 'at', 'sha256'})


JOURNAL_DIR = '.delegations'
JOURNAL_FILE_LIMIT = 16384


class DelegationRefusal(ValueError):
    """A refusal with an HTTP-style status; the message is the reason shown to the caller."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _line(value, limit, what, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError('%s must be %s text of at most %d characters'
                         % (what, 'one line of' if not empty else 'one line (or empty)', limit))
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError('%s must be one line of plain text' % what)
    from review_workflow import plain_text
    plain_text(value, what)


def validate_request(payload, recorded=False):
    """The closed request of one delegation event. Attribution is never part of it.

    A grant either names an existing decision issue (``decision`` set, ``title`` null) or asks
    for a new one (``decision`` null, ``title`` its one-line title; the reason becomes its
    description), so a person owner who works only on the web can record the decision and
    its delegation in one act. A narrowing or a revocation always names its decision. In a
    stored record (``recorded=True``) the decision is always the issue's id.
    """
    if not isinstance(payload, dict) or set(payload) != REQUEST_FIELDS:
        raise ValueError('A delegation request has exactly the fields %s' % ', '.join(sorted(REQUEST_FIELDS)))
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1:
        raise ValueError('A delegation request schema_version must be 1')
    if not isinstance(payload['operation_id'], str) or not ACTOR.fullmatch(payload['operation_id']):
        raise ValueError('A delegation request needs a stable operation_id')
    if payload['decision'] is None and not recorded:
        if payload['action'] != 'grant':
            raise ValueError('A narrowing or a revocation names the decision it changes')
        _line(payload['title'], TITLE_MAX, 'title')
    else:
        if not isinstance(payload['decision'], str) or not ISSUE_ID.fullmatch(payload['decision']):
            raise ValueError('decision must be the id of a native decision issue in this project')
        if payload['title'] is not None and (not recorded or payload['action'] != 'grant'):
            raise ValueError('title is given only to create a new decision with a grant')
        if payload['title'] is not None:
            _line(payload['title'], TITLE_MAX, 'title')
    if payload['action'] not in ACTIONS:
        raise ValueError('action must be one of %s' % ', '.join(ACTIONS))
    if payload['right'] not in RIGHTS:
        raise ValueError('right must be one of %s' % ', '.join(RIGHTS))
    if not isinstance(payload['grantee'], str) or not GRANTEE.fullmatch(payload['grantee']):
        raise ValueError('grantee must be an exact agent id (agent_ and 16 hex digits) or coordinator-grant')
    scope = payload['scope']
    if (not isinstance(scope, list) or any(item not in SCOPES for item in scope)
            or len(set(scope)) != len(scope) or scope != sorted(scope)):
        raise ValueError('scope must be a sorted list without repeats drawn from %s' % ', '.join(SCOPES))
    if payload['action'] == 'revoke':
        if scope or payload['escalate'] != '':
            raise ValueError('A revocation carries an empty scope and an empty escalate line')
    else:
        if not scope:
            raise ValueError('A grant or a narrowing needs a nonempty scope')
        _line(payload['escalate'], ESCALATE_MAX, 'escalate')
    _line(payload['reason'], REASON_MAX, 'reason')


def _validate_record(record):
    if not isinstance(record, dict) or set(record) != RECORD_FIELDS:
        raise ValueError('A delegation record has its exact field set')
    validate_request({key: record[key] for key in REQUEST_FIELDS}, recorded=True)
    if not isinstance(record['project'], str) or not PROJECT.fullmatch(record['project']):
        raise ValueError('A delegation record names its project')
    if record['via'] not in VIA:
        raise ValueError('A delegation record was written by a session or an operator')
    if not isinstance(record['recorded_by'], str) or not ACTOR.fullmatch(record['recorded_by']):
        raise ValueError('A delegation record names its recorder')
    if record['via'] == 'session':
        if not isinstance(record['account'], str) or not ACCOUNT.fullmatch(record['account']) \
                or record['recorded_by'] != record['account']:
            raise ValueError('A session-written delegation record names the person account that wrote it')
    elif record['account'] is not None:
        raise ValueError('An operator-written delegation record names no account')
    if not isinstance(record['at'], str):
        raise ValueError('A delegation record needs the server time')
    try:
        at = datetime.datetime.fromisoformat(record['at'].replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('A delegation record needs the server time') from exc
    if at.tzinfo is None or at.utcoffset() != datetime.timedelta(0):
        raise ValueError('A delegation record needs the server UTC time')
    if record['sha256'] != content_hash(record):
        raise ValueError('A delegation record hash differs')


def make_record(payload, *, project, recorded_by, via, account, at):
    """Build a record from a validated request and trusted server attribution only."""
    validate_request(payload, recorded=True)
    record = dict(payload, project=project, recorded_by=recorded_by, via=via, account=account, at=at)
    record['sha256'] = content_hash(record)
    _validate_record(record)
    return record


def body(record):
    _validate_record(record)
    return PREFIX + canonical_bytes(record).decode('utf-8')


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('Duplicate delegation record field')
        out[key] = value
    return out


def parse_comment(comment, *, project, decision):
    """An exact record bound to its native comment author, its project and its issue, or None."""
    if not isinstance(comment, dict):
        return None
    text = comment.get('text')
    if not isinstance(text, str) or not text.startswith(PREFIX):
        return None
    try:
        record = record_json.loads(text[len(PREFIX):], object_pairs_hook=_unique)
        _validate_record(record)
        if body(record) != text or record['project'] != project or record['decision'] != decision:
            return None
        if comment.get('author') != record['recorded_by']:
            return None
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None
    return record


def open_journal(project):
    """The host journal directory for a writer, or a refusal BEFORE any native write.

    A delegation is honoured only with its host journal entry, as the lane-fault clears and
    the follow-ons are: a ``Kind: delegation-v1`` comment planted where the prefix was not
    reserved (an older kit) has no entry and grants nothing."""
    from pathlib import Path
    if project is None:
        raise DelegationRefusal(503, 'Recording a delegation needs the project directory that holds its host journal')
    directory = Path(project) / JOURNAL_DIR
    if directory.is_symlink():
        raise DelegationRefusal(503, 'Refusing %s: the path is a symlink' % JOURNAL_DIR)
    try:
        directory.mkdir(exist_ok=True)
    except OSError as exc:
        raise DelegationRefusal(503, 'Cannot create %s: %s' % (JOURNAL_DIR, exc)) from None
    if not directory.is_dir():
        raise DelegationRefusal(503, '%s is not a directory' % JOURNAL_DIR)
    from uuid import uuid4
    probe = directory / ('.writable-' + uuid4().hex + '.tmp')
    try:
        probe.write_text('probe', encoding='utf-8')
    except OSError as exc:
        raise DelegationRefusal(503, 'Cannot use %s: the directory is not writable (%s)' % (JOURNAL_DIR, exc)) from None
    try:
        probe.unlink()
    except OSError:
        pass
    return directory


def publish(project, record):
    """Persist the journal entry of one validated record, named by its hash."""
    from coordination import atomic
    _validate_record(record)
    directory = open_journal(project)
    path = directory / (record['sha256'] + '.json')
    if path.is_symlink():
        raise DelegationRefusal(503, 'Refusing journal entry %s: it is a symlink' % path.name)
    atomic(path, record)
    return path


def validate_entry(record, name=None):
    """One journal entry (the record itself) and its ``<sha256>.json`` name, for a backup."""
    _validate_record(record)
    if name is not None and name != record['sha256'] + '.json':
        raise ValueError('Delegation journal path mismatch')
    return record


def journaled(project, record):
    """Whether the host journal holds exactly this record (fail-closed on any doubt)."""
    from pathlib import Path
    if project is None:
        return False
    directory = Path(project) / JOURNAL_DIR
    path = directory / (record['sha256'] + '.json')
    try:
        if directory.is_symlink() or not directory.is_dir() or path.is_symlink() or not path.is_file():
            return False
        if path.stat().st_size > JOURNAL_FILE_LIMIT:
            return False
        stored = record_json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique)
        _validate_record(stored)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return False
    return stored == record


def is_decision_issue(row):
    """The kit's one decision predicate (reference_records._decision_issue, proposal_records)."""
    return isinstance(row, dict) and (row.get('issue_type') == 'decision' or 'decision' in (row.get('labels') or []))


def history(row, project, journal):
    """The honoured delegation records on one issue, oldest first.

    A record counts only when it parses exactly, is bound to its comment author, project and
    issue, AND its host journal entry exists under ``journal`` (the project directory).
    Ordered by the record's own server time, then the native comment id, so the order never
    depends on how a native read happens to list comments."""
    decision = row.get('id') if isinstance(row, dict) else None
    out = []
    for comment in (row.get('comments') or []) if isinstance(row, dict) else []:
        record = parse_comment(comment, project=project, decision=decision)
        if record is not None and journaled(journal, record):
            out.append((record['at'], str(comment.get('id') or ''), record))
    return [record for _, _, record in sorted(out, key=lambda item: (item[0], item[1]))]


def recorder_live(record, state, project, operators=None, owners=None):
    """Whether the person or operator who wrote ``record`` holds that standing NOW."""
    if record['via'] == 'session':
        users = state.get('users') if isinstance(state.get('users'), dict) else {}
        user = users.get(record['account'])
        if not isinstance(user, dict) or user.get('disabled'):
            return False
        memberships = state.get('memberships') if isinstance(state.get('memberships'), dict) else {}
        return (memberships.get(project) or {}).get(record['account']) == 'owner'
    from review_workflow import listed_operator_actor
    try:
        return bool(listed_operator_actor(record['recorded_by'], operators, owners))
    except (ValueError, OSError):
        return False


def _scope_in_force(records):
    scope = list(records[0]['scope'])
    for record in records[1:]:
        if record['action'] == 'narrow' and set(record['scope']) < set(scope):
            scope = list(record['scope'])
    return scope


def current(row, project, state, journal, operators=None, owners=None):
    """The delegation in force on one decision issue, as ``(delegation, reason)``.

    The first honoured record must be a grant whose recorder is live NOW (a person owner of
    this project, or a listed operator); a second grant makes the issue ambiguous and grants
    nothing. A later narrow or revoke counts whatever its writer's standing is now:
    restricting a right never needs current standing, so a demoted owner's revocation holds.
    A narrowing must name the same right and grantee and a strict subset of the scope.
    """
    records = history(row, project, journal)
    if not records:
        return None, 'no delegation is recorded on decision %s' % row.get('id')
    grant = records[0]
    if grant['action'] != 'grant':
        return None, 'the first delegation record on decision %s is not a grant' % row.get('id')
    if any(record['action'] == 'grant' for record in records[1:]):
        return None, 'decision %s carries more than one delegation grant; record a new decision instead' % row.get('id')
    if not recorder_live(grant, state, project, operators, owners):
        who = grant['account'] if grant['via'] == 'session' else grant['recorded_by']
        return None, ('the delegation on decision %s was recorded by %s, who is no longer %s'
                      % (row.get('id'), who, 'a person owner of this project' if grant['via'] == 'session'
                         else 'a listed operator'))
    same = [record for record in records[1:]
            if record['right'] == grant['right'] and record['grantee'] == grant['grantee']]
    revoked = next((record for record in same if record['action'] == 'revoke'), None)
    if revoked is not None:
        return None, 'the delegation on decision %s was revoked at %s' % (row.get('id'), revoked['at'])
    delegation = {key: grant[key] for key in ('decision', 'right', 'grantee', 'escalate',
                                                'via', 'recorded_by', 'account', 'at')}
    delegation['scope'] = _scope_in_force([grant] + same)
    narrowed = [record for record in same if record['action'] == 'narrow' and record['scope'] == delegation['scope']]
    if narrowed:
        delegation['escalate'] = narrowed[-1]['escalate']
    delegation['changes'] = [{'action': record['action'], 'at': record['at'], 'recorded_by': record['recorded_by'],
                              'scope': record['scope']} for record in same]
    return delegation, None


def check_acceptance(row, *, project, state, journal, agent, kind, operators=None, owners=None):
    """The live check an agent's record acceptance must pass; returns the delegation.

    ``row`` is the native issue named by the acceptance's ``decision_id`` (with comments), or
    None when this project has no such issue. Raises DelegationRefusal(403) with the first
    failing reason. ``kind`` is ``references`` or ``capabilities``.
    """
    if kind not in SCOPES:
        raise DelegationRefusal(403, 'Delegations cover only references and capabilities')
    if not isinstance(row, dict):
        raise DelegationRefusal(403, 'The named delegation decision does not exist in this project')
    if row.get('malformed'):
        raise DelegationRefusal(403, 'Decision %s cannot be read' % row.get('id'))
    if not is_decision_issue(row):
        raise DelegationRefusal(403, 'Issue %s is not a decision issue (type decision or labelled decision)'
                                % row.get('id'))
    delegation, reason = current(row, project, state, journal, operators, owners)
    if delegation is None:
        raise DelegationRefusal(403, reason[0].upper() + reason[1:])
    if delegation['right'] != 'record-acceptance':
        raise DelegationRefusal(403, 'Decision %s delegates %s, not record acceptance'
                                % (row.get('id'), delegation['right']))
    from http_authority import coordinator_grant
    if coordinator_grant(state, project, agent) is None:
        raise DelegationRefusal(403, 'Agent %s holds no coordinator grant in this project' % agent)
    if delegation['grantee'] != 'coordinator-grant' and delegation['grantee'] != agent:
        raise DelegationRefusal(403, 'The delegation on decision %s is granted to %s, not to %s'
                                % (row.get('id'), delegation['grantee'], agent))
    if kind not in delegation['scope']:
        raise DelegationRefusal(403, 'The delegation on decision %s covers %s, not %s'
                                % (row.get('id'), ', '.join(delegation['scope']), kind))
    return delegation


def check_write(payload, row, *, project, journal):
    """Whether one delegation event may be appended to ``row`` (refusals are 404/409/422)."""
    validate_request(payload, recorded=True)
    if not isinstance(row, dict):
        raise DelegationRefusal(404, 'Decision %s does not exist in this project' % payload['decision'])
    if not is_decision_issue(row):
        raise DelegationRefusal(422, 'Issue %s is not a decision issue; record the delegation on a decision'
                                % payload['decision'])
    records = history(row, project, journal)
    if payload['action'] == 'grant':
        if records:
            raise DelegationRefusal(409, 'Decision %s already carries a delegation; record a new decision to grant again'
                                    % payload['decision'])
        return
    if not records or records[0]['action'] != 'grant':
        raise DelegationRefusal(409, 'Decision %s carries no delegation to %s' % (payload['decision'], payload['action']))
    grant = records[0]
    if payload['right'] != grant['right'] or payload['grantee'] != grant['grantee']:
        raise DelegationRefusal(409, 'A %s must name the same right and grantee as the grant' % payload['action'])
    same = [record for record in records[1:]
            if record['right'] == grant['right'] and record['grantee'] == grant['grantee']]
    if any(record['action'] == 'revoke' for record in same):
        raise DelegationRefusal(409, 'The delegation on decision %s is already revoked' % payload['decision'])
    if payload['action'] == 'narrow':
        scope = _scope_in_force([grant] + same)
        if not set(payload['scope']) < set(scope):
            raise DelegationRefusal(409, 'A narrowing must name a strict subset of the scope in force (%s)'
                                    % ', '.join(scope))


def read_issue(run, decision):
    """One native issue with its comments, or None when this project has no such issue."""
    if not isinstance(decision, str) or not ISSUE_ID.fullmatch(decision):
        return None
    # `bd list --id` returns only the rows that exist (an empty list when none do), so an
    # unknown id -- another project's, or an operation id -- is None, never a native failure
    # (reference_records._decision_issue reads it the same way); then the one row is shown.
    listed = record_json.loads_array_rows(run(['list', '--id', decision, '--all', '--limit', '0', '--json']) or '[]')
    if not any(isinstance(item, dict) and item.get('id') == decision for item in listed):
        return None
    rows = record_json.loads_array_rows(run(['show', decision, '--json', '--include-comments']) or '[]')
    return next((item for item in rows if isinstance(item, dict) and item.get('id') == decision), None)


def apply(payload, *, project, journal, run, recorded_by, via, account, at):
    """Append one delegation event: check, journal readiness, one native comment, the entry.

    The caller has verified the writer's standing (a person owner's own web session, or a
    listed operator on the host path), holds the project's coordination lock and runs this
    inside its guarded write. The journal is proved writable BEFORE the native comment and the
    entry is written after it: a failure in between leaves a comment no reader honours.
    """
    validate_request(payload)
    if payload['decision'] is None:
        # A new decision issue for this grant: the native create is the first write of the act.
        created = record_json.loads(run(['create', payload['title'], '--type', 'decision',
                                         '--description', payload['reason'], '--json']))
        if not isinstance(created, dict) or not isinstance(created.get('id'), str) \
                or not ISSUE_ID.fullmatch(created['id']):
            raise ValueError('The new decision issue cannot be reconciled')
        payload = dict(payload, decision=created['id'])
    row = read_issue(run, payload['decision'])
    check_write(payload, row, project=project, journal=journal)
    record = make_record(payload, project=project, recorded_by=recorded_by, via=via, account=account, at=at)
    open_journal(journal)
    comment = record_json.loads(run(['comments', 'add', payload['decision'], body(record), '--json']))
    if not isinstance(comment, dict) or not comment.get('id'):
        raise ValueError('The delegation record cannot be reconciled')
    publish(journal, record)
    if LABEL not in (row.get('labels') or []):
        run(['update', payload['decision'], '--add-label', LABEL, '--json'])
    return {'schema_version': 1, 'operation_id': payload['operation_id'], 'decision': payload['decision'],
            'record': record, 'comment_id': str(comment['id'])}


def listing(rows, project, state, journal, operators=None, owners=None):
    """Every delegation on the given decision rows, in force or not, with its reason."""
    out = []
    for row in rows:
        if not isinstance(row, dict) or not is_decision_issue(row) or not history(row, project, journal):
            continue
        delegation, reason = current(row, project, state, journal, operators, owners)
        out.append({'decision': row.get('id'), 'title': row.get('title'),
                    'in_force': delegation is not None, 'delegation': delegation, 'reason': reason})
    return out
