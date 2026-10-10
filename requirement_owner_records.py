"""Durable owner requirement evidence; strict schemas independent of HTTP policy.

An authenticated human session writes these through the service adapter. Readers
bind the stored native author, not current membership: removing an owner later
does not undo a completed historical decision. Privileged native access remains
the installation's trusted host boundary, not a cryptographic identity proof.
"""
import re

from coordination import identifier
from requirements import canonical_bytes, content_hash
from requirement_governance import digest, project_name

ACCEPTANCE_PREFIX = 'Kind: requirement-owner-acceptance-v1\n'
STATE_PREFIX = 'Kind: requirement-owner-state-v1\n'
REASON_PREFIX = 'Kind: requirement-owner-state-reason-v1\n'
HUMAN = re.compile(r'usr_[0-9a-f]{16}\Z')
ACCEPTANCE_FIELDS = {'schema_version', 'project', 'id', 'revision', 'record_sha256',
                     'account_id', 'at', 'operation_id', 'governance', 'decision',
                     'source', 'sha256'}
STATE_FIELDS = {'schema_version', 'project', 'id', 'revision', 'record_sha256',
                'previous_state_sha256', 'state', 'superseded_by', 'account_id',
                'at', 'operation_id', 'governance', 'sha256'}


def validate(record, state=False):
    fields = STATE_FIELDS if state else ACCEPTANCE_FIELDS
    if (not isinstance(record, dict) or set(record) != fields
            or type(record.get('schema_version')) is not int or record['schema_version'] != 1):
        raise ValueError('Invalid owner requirement evidence fields')
    project_name(record['project'])
    identifier(record['id']); identifier(record['operation_id'])
    if type(record['revision']) is not int or record['revision'] < 1:
        raise ValueError('Invalid owner requirement evidence revision')
    digest(record['record_sha256']); digest(record['sha256'])
    if not isinstance(record['account_id'], str) or not HUMAN.fullmatch(record['account_id']):
        raise ValueError('Owner requirement evidence needs a human account')
    if not isinstance(record['at'], str) or not record['at'].strip():
        raise ValueError('Owner requirement evidence needs a timestamp')
    governance = record['governance']
    if (not isinstance(governance, dict) or set(governance) != {'revision', 'sha256', 'mode'}
            or type(governance['revision']) is not int or governance['revision'] < 1
            or governance['mode'] != 'simple'):
        raise ValueError('Owner requirement evidence needs exact simple governance')
    digest(governance['sha256'])
    if state:
        if record['previous_state_sha256'] is not None:
            digest(record['previous_state_sha256'])
        if record['state'] not in ('withdrawn', 'superseded'):
            raise ValueError('Invalid owner requirement state')
        successor = record['superseded_by']
        if record['state'] == 'withdrawn':
            if successor is not None:
                raise ValueError('Withdrawal has no successor')
        else:
            if not isinstance(successor, dict) or set(successor) != {'id', 'revision', 'sha256'}:
                raise ValueError('Supersession needs an exact successor')
            identifier(successor['id']); digest(successor['sha256'])
            if successor['id'] == record['id']:
                raise ValueError('A requirement cannot supersede itself')
            if type(successor['revision']) is not int or successor['revision'] < 1:
                raise ValueError('Invalid successor revision')
    else:
        # Slice A accepts only direct requirement content. Room/proposal adapters
        # and their bounded references belong to the separately released slice C.
        if record['source'] is not None:
            raise ValueError('Unsupported owner requirement source')
        from requirements import _validate_acceptance
        decision = record['decision']
        if (not isinstance(decision, dict) or set(decision) != {
                'decision_id', 'owners', 'approvers', 'policy', 'evidence'}):
            raise ValueError('Invalid owner acceptance decision')
        _validate_acceptance(dict(decision, manifest_sha256=record['record_sha256']),
                             {'sha256': record['record_sha256']})
        if (decision['owners'] != [record['account_id']]
                or decision['approvers'] != [record['account_id']] or decision['policy'] != 'any-owner'):
            raise ValueError('Owner acceptance decision must bind the signed-in account')
    if content_hash(record) != record['sha256']:
        raise ValueError('Owner requirement evidence hash mismatch')
    return record


def parse(body, state=False):
    prefix = STATE_PREFIX if state else ACCEPTANCE_PREFIX
    if not isinstance(body, str) or not body.startswith(prefix):
        return None
    try:
        from export_requirements import parse_json
        record = validate(parse_json(body[len(prefix):]), state=state)
        if canonical_bytes(record).decode('utf-8') != body[len(prefix):]:
            return None
        return record
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None


def body(record, state=False):
    validate(record, state=state)
    return (STATE_PREFIX if state else ACCEPTANCE_PREFIX) + canonical_bytes(record).decode('utf-8')


def acceptance(project, record, account_id, operation_id, governance, decision_id, at):
    evidence = {'schema_version': 1, 'project': project, 'id': record['id'],
                'revision': record['revision'], 'record_sha256': record['sha256'],
                'account_id': account_id, 'at': at, 'operation_id': operation_id,
                'governance': dict(governance), 'source': None,
                'decision': {'decision_id': decision_id, 'owners': [account_id],
                             'approvers': [account_id], 'policy': 'any-owner',
                             'evidence': decision_id}}
    evidence['sha256'] = content_hash(evidence)
    validate(evidence)
    return evidence


def existing_acceptances(row):
    """Owner ledger with exact native author/issue binding; malformed fails closed.

    Pending evidence can precede its content revision during recovery. Consumers
    must separately match the revision/hash before recognizing acceptance.
    """
    from keyed_records import existing_ledger
    def author_bound(comment, record):
        if comment.get('author') != record['account_id']:
            raise ValueError('Owner acceptance native author does not match the human account')
        return True
    return existing_ledger(row, ACCEPTANCE_PREFIX, parse, 'requirement',
                           'owner acceptance', 'revision', keep=author_bound)


REASON_FIELDS = {'schema_version', 'project', 'id', 'state_sha256', 'account_id',
                 'at', 'operation_id', 'reason', 'sha256'}
TERMINAL_LABELS = {'requirement:withdrawn', 'requirement:superseded'}


def validate_reason(record):
    if (not isinstance(record, dict) or set(record) != REASON_FIELDS
            or type(record['schema_version']) is not int or record['schema_version'] != 1):
        raise ValueError('Invalid owner requirement state reason fields')
    project_name(record['project']); identifier(record['id']); identifier(record['operation_id'])
    digest(record['state_sha256']); digest(record['sha256'])
    if not isinstance(record['account_id'], str) or not HUMAN.fullmatch(record['account_id']):
        raise ValueError('State reason needs a human account')
    if not isinstance(record['at'], str) or not record['at'].strip():
        raise ValueError('State reason needs a timestamp')
    if (not isinstance(record['reason'], str) or not 1 <= len(record['reason'].strip()) <= 1000
            or '\0' in record['reason']):
        raise ValueError('State reason must contain 1 to 1000 characters')
    if content_hash(record) != record['sha256']:
        raise ValueError('Owner requirement state reason hash mismatch')
    return record


def reason_body(record):
    return REASON_PREFIX + canonical_bytes(validate_reason(record)).decode('utf-8')


def parse_reason(text):
    if not isinstance(text, str) or not text.startswith(REASON_PREFIX):
        return None
    try:
        from export_requirements import parse_json
        record = validate_reason(parse_json(text[len(REASON_PREFIX):]))
        return record if reason_body(record) == text else None
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None


def terminal_evidence(project, record, account, operation, mode, state, successor, reason, at):
    evidence = dict(schema_version=1, project=project, id=record['id'], revision=record['revision'],
                    record_sha256=record['sha256'], previous_state_sha256=None, state=state,
                    superseded_by=successor, account_id=account, at=at,
                    operation_id=operation, governance=dict(mode))
    evidence['sha256'] = content_hash(evidence); validate(evidence, state=True)
    explanation = dict(schema_version=1, project=project, id=record['id'],
                       state_sha256=evidence['sha256'], account_id=account, at=at,
                       operation_id=operation, reason=reason)
    explanation['sha256'] = content_hash(explanation); validate_reason(explanation)
    return evidence, explanation


def state_ledger(row, allow_partial=False):
    """Immutable terminal evidence; labels cannot manufacture or undo a state.

    A missing reason is an interrupted transition, never an active requirement.
    Only the writer recovering its exact receipt may inspect that partial pair.
    """
    from keyed_records import existing_ledger
    from requirement_records import existing_revisions
    from reserved_comments import _reserved_prefix_view
    for comment in row.get('comments') or []:
        text = comment.get('text', '') if isinstance(comment, dict) else ''
        view = _reserved_prefix_view(text) if isinstance(text, str) else ''
        if view.startswith(('Kind: requirement-owner-state-', 'Kind: requirement-owner-state-v')):
            if view != text or not text.startswith((STATE_PREFIX, REASON_PREFIX)):
                raise ValueError('Malformed or unsupported owner terminal evidence')
    def author_bound(comment, record):
        if comment.get('author') != record['account_id']:
            raise ValueError('Owner state native author does not match the human account')
        return True
    states = existing_ledger(row, STATE_PREFIX, lambda text: parse(text, state=True),
                             'requirement', 'owner state', 'sha256', keep=author_bound)
    reasons = existing_ledger(row, REASON_PREFIX, parse_reason, 'requirement',
                              'owner state reason', 'state_sha256', keep=author_bound)
    if len(states) > 1 or set(reasons) - set(states):
        raise ValueError('Conflicting owner requirement terminal evidence')
    if not states:
        if TERMINAL_LABELS.intersection(row.get('labels') or []):
            raise ValueError('Terminal requirement label has no validated state evidence')
        return None, None
    evidence = next(iter(states.values()))
    revisions = existing_revisions(row)
    record = revisions.get(evidence['revision'])
    if (record is None or record['sha256'] != evidence['record_sha256']
            or record['acceptance_state'] != 'accepted'
            or max(revisions) != evidence['revision'] or evidence['previous_state_sha256'] is not None):
        raise ValueError('Terminal state must bind the unchanged latest accepted revision')
    reason = reasons.get(evidence['sha256'])
    if reason is None:
        if not allow_partial:
            raise ValueError('Requirement terminal transition is incomplete; retry its original request')
    elif (any(reason[key] != evidence[key] for key in ('project', 'id', 'account_id', 'at', 'operation_id'))):
        raise ValueError('Requirement state reason conflicts with its state evidence')
    return evidence, reason
