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
