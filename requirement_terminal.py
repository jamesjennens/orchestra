"""Owner terminal transitions, serialized by the canonical project/authority locks.

The exact pair is saved before either native comment. A retry reads native state
and writes only missing evidence. Acceptance and content are never rewritten.
"""
import keyed_records as core
import requirement_governance as governance
import requirement_owner_records as owner
import requirement_records as records
from coordination import atomic, identifier
from requirements import canonical_bytes, content_hash, load_json

JOURNAL = '.requirement-owner-requests'


def receipt_path(project_path, operation):
    return core.receipt_path(core.journal_dir(project_path, JOURNAL),
                             content_hash({'operation_id': operation}), 'requirement')


def identity(context, action, task, body):
    return content_hash({'actor': context.account_id, 'action': action, 'id': task, 'body': body})


def read_receipt(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Unusable owner terminal receipt; ask the host operator to reconcile it')
    try:
        return validate_receipt(load_json(path))
    except (ValueError, OSError, TypeError, KeyError) as error:
        raise ValueError('Owner terminal receipt cannot be verified; ask the host operator to reconcile it') from error


def validate_receipt(receipt):
    core.validate_receipt(receipt)
    state = owner.validate(receipt.get('owner_state'), state=True)
    reason = owner.validate_reason(receipt.get('owner_state_reason'))
    if (receipt.get('operation') not in ('withdraw', 'supersede')
            or receipt['status'] not in ('pending', 'complete')
            or receipt.get('actor') != state['account_id'] or receipt.get('id') != state['id']
            or receipt.get('revision') != state['revision']
            or state['state'] != {'withdraw': 'withdrawn', 'supersede': 'superseded'}[receipt['operation']]
            or state['previous_state_sha256'] is not None
            or reason['state_sha256'] != state['sha256']
            or any(reason[key] != state[key] for key in ('project', 'id', 'account_id', 'at', 'operation_id'))
            or receipt.get('request') is None):
        raise ValueError('Invalid owner terminal receipt binding')
    from requirement_http import checked_body, OwnerContext
    body = checked_body(receipt['request'], receipt['operation'])
    if (body['expected_revision'] != state['revision'] or body['expected_sha256'] != state['record_sha256']
            or body['expected_state_sha256'] is not None or body['reason'] != reason['reason']
            or body.get('successor') != state['superseded_by']
            or identity(OwnerContext(state['project'], state['account_id'], state['governance']),
                        receipt['operation'], state['id'], body) != receipt['sha256']):
        raise ValueError('Terminal receipt conflicts with its exact request')
    return receipt


def checked_current(project_path, context, row, body):
    if row is None or records.existing_kind(row) != 'requirement':
        raise ValueError('Select a readable requirement in this project')
    revisions = records.existing_revisions(row)
    record = records.latest_revision(revisions)
    if (record is None or record['revision'] != body['expected_revision']
            or record['sha256'] != body['expected_sha256']):
        raise ValueError('Requirement changed; reload before trying again')
    if record['acceptance_state'] != 'accepted' or not records.resolved_acceptance(row, record):
        raise ValueError('Select a currently accepted requirement with no pending draft')
    for evidence in owner.existing_acceptances(row).values():
        governance.validate_evidence(project_path, context.project, evidence)
    return record


def apply(project_path, context, action, body, operation, run, task):
    identifier(task); identifier(operation)
    rows = records.read_rows(run)
    row = records.find(rows, task)
    record = checked_current(project_path, context, row, body)
    state, reason = owner.state_ledger(row, allow_partial=True)
    if state is not None:
        governance.validate_evidence(project_path, context.project, state)
    path = receipt_path(project_path, operation)
    if path.is_symlink() or path.exists() and not path.is_file():
        raise ValueError('Unusable owner terminal receipt; ask the host operator to reconcile it')
    prior = read_receipt(path) if path.exists() else None
    digest = identity(context, action, task, body)
    if prior and prior['sha256'] != digest:
        raise ValueError('This operation was used for different requirement content')
    pending, uncertain = pending_ids(project_path, context.project)
    if uncertain or task in pending and prior is None:
        raise ValueError('Another requirement transition is pending; retry its original request')
    if body['expected_state_sha256'] is not None:
        raise ValueError('A terminal requirement cannot be changed or reactivated; create a new requirement')
    if state is not None and (prior is None or state != prior['owner_state']
                              or reason is not None and reason != prior['owner_state_reason']):
        raise ValueError('Requirement already has a different terminal transition')
    if prior and prior['status'] == 'complete' and (state, reason) != (prior['owner_state'], prior['owner_state_reason']):
        raise ValueError('Completed terminal evidence is missing; ask the host operator to reconcile it')
    # A completed historical replay keeps the exact pointer even if its successor
    # later becomes terminal. An incomplete operation rechecks its live target.
    successor = body.get('successor')
    proved_pair = prior and (state, reason) == (prior['owner_state'], prior['owner_state_reason'])
    if successor is not None and not proved_pair:
        if successor['id'] in pending:
            raise ValueError('Supersession target has a pending transition; retry after reconciliation')
        if successor['id'] == task:
            raise ValueError('A requirement cannot supersede itself')
        target = records.find(rows, successor['id'])
        checked_current(project_path, context, target, dict(expected_revision=successor['revision'],
                                                          expected_sha256=successor['sha256']))
        target_state, _ = owner.state_ledger(target)
        if target_state is not None:
            raise ValueError('Supersession needs a currently active accepted requirement')
    if prior is None:
        evidence, explanation = owner.terminal_evidence(
            context.project, record, context.account_id, operation, context.governance,
            'withdrawn' if action == 'withdraw' else 'superseded', successor, body['reason'], core.now())
        prior = dict(sha256=digest, status='pending', actor=context.account_id, id=task,
                     revision=record['revision'], operation=action, request=body,
                     owner_state=evidence, owner_state_reason=explanation)
        validate_receipt(prior); atomic(path, prior)
    evidence, explanation = prior['owner_state'], prior['owner_state_reason']
    governance.validate_evidence(project_path, context.project, evidence)
    if state is None:
        run(['comments', 'add', task, owner.body(evidence, state=True), '--json'])
    if reason is None:
        run(['comments', 'add', task, owner.reason_body(explanation), '--json'])
    # Never complete solely from a successful write answer. Read back the pair,
    # attribution and immutable revision before projecting labels or completing.
    confirmed = records.find(records.read_rows(run), task)
    checked_current(project_path, context, confirmed, body)
    if owner.state_ledger(confirmed) != (evidence, explanation):
        raise ValueError('Requirement terminal evidence was not confirmed; keep the original request')
    label = 'requirement:' + evidence['state']
    if label not in (confirmed.get('labels') or []):
        run(['update', task, '--add-label', label, '--json'])
    if prior['status'] != 'complete':
        atomic(path, dict(prior, status='complete'))
    return dict(id=task, revision=record['revision'], sha256=record['sha256'],
                acceptance_state='accepted', requirement_state=evidence['state'],
                state=evidence, reason=explanation, reconciled=state is not None)


def recoverable(project_path, context, action, task, body, operation):
    path = receipt_path(project_path, operation)
    if path.is_symlink() or not path.is_file():
        raise ValueError('No matching terminal receipt was confirmed; keep the original operation unknown')
    receipt = read_receipt(path)
    if (receipt['sha256'] != identity(context, action, task, body)
            or receipt['owner_state']['operation_id'] != operation):
        raise ValueError('Terminal receipt does not match this exact owner request')
    return receipt


def pending_ids(project_path, project):
    """One local receipt scan, no native calls; unprovable pending state is unknown."""
    path = project_path / JOURNAL
    if not path.exists():
        return set(), False
    if path.is_symlink() or not path.is_dir():
        return set(), True
    found = set()
    for entry in path.glob('*.json'):
        try:
            if entry.is_symlink() or not entry.is_file():
                raise ValueError('Unusable owner receipt')
            saved = load_json(entry)
            if not isinstance(saved, dict):
                raise ValueError('Unusable owner receipt')
            if (saved.get('operation') not in ('withdraw', 'supersede')
                    and 'owner_state' not in saved and 'owner_state_reason' not in saved):
                continue
            validate_receipt(saved)
            if entry.name != content_hash({'operation_id': saved['owner_state']['operation_id']}) + '.json':
                raise ValueError('Terminal receipt path mismatch')
            governance.validate_evidence(project_path, project, saved['owner_state'])
            if saved['status'] == 'pending':
                found.add(saved['id'])
        except (ValueError, OSError, TypeError, KeyError):
            return found, True
    return found, False
