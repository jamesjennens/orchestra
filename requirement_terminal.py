"""Owner terminal transitions, serialized by the canonical project/authority locks.

The exact pair is saved before either native comment. A retry reads native state
and writes only missing evidence. Acceptance and content are never rewritten.
"""
import hashlib
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
            or receipt['status'] not in ('pending', 'complete', 'released')
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
    disposition = receipt.get('terminal_disposition')
    if receipt['status'] == 'released':
        fields = {'project', 'actor', 'at', 'prior_receipt_sha256', 'reason', 'sha256'}
        if (not isinstance(disposition, dict) or set(disposition) != fields
                or disposition['project'] != state['project']
                or not isinstance(disposition['at'], str) or not disposition['at'].strip()
                or not isinstance(disposition['reason'], str) or not disposition['reason'].strip()
                or len(disposition['reason']) > 1000
                or content_hash(disposition) != disposition['sha256']):
            raise ValueError('Invalid terminal abandonment disposition')
        governance.digest(disposition['prior_receipt_sha256'])
        identifier(disposition['actor'])
    elif disposition is not None:
        raise ValueError('Unexpected terminal abandonment disposition')
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
        raise ValueError('Select a currently accepted requirement with no pending draft; accept the pending edit first')
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
    if task in uncertain:
        raise ValueError('Requirement transition receipt is damaged; ask the host operator to reconcile it')
    if task in pending and prior is None:
        raise ValueError('A requirement transition is pending; finish or abandon it on the requirements page')
    if prior and prior['status'] == 'released':
        raise ValueError('This requirement transition was abandoned; start a new transition')
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
        if successor['id'] in uncertain:
            raise ValueError('Supersession target receipt is damaged; ask the host operator to reconcile it')
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
    return dict(id=task, operation_id=operation, revision=record['revision'], sha256=record['sha256'],
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


def receipt_sha256(receipt):
    return hashlib.sha256(canonical_bytes(receipt)).hexdigest()


def scan(project_path, project):
    """Damage belongs to its file, and to its id only when that id is readable.

    An unidentifiable file is an operator diagnostic, never proof that every
    requirement is damaged or pending. No special file is opened.
    """
    path = project_path / JOURNAL
    if not path.exists():
        return [], []
    if path.is_symlink() or not path.is_dir():
        return [], [dict(id=None, message='Requirement receipt directory is damaged; ask the host operator to reconcile it')]
    found, damaged = [], []
    for entry in path.glob('*.json'):
        saved = None
        try:
            if entry.is_symlink() or not entry.is_file():
                raise ValueError('Unusable owner receipt')
            saved = load_json(entry)
            if not isinstance(saved, dict):
                raise ValueError('Unusable owner receipt')
            if (saved.get('operation') not in ('withdraw', 'supersede')
                    and 'owner_state' not in saved and 'owner_state_reason' not in saved):
                from requirement_http import validate_receipt as validate_owner_receipt
                validate_owner_receipt(saved)
                continue
            validate_receipt(saved)
            if entry.name != content_hash({'operation_id': saved['owner_state']['operation_id']}) + '.json':
                raise ValueError('Terminal receipt path mismatch')
            governance.validate_evidence(project_path, project, saved['owner_state'])
            found.append(saved)
        except (ValueError, OSError, TypeError, KeyError):
            task = saved.get('id') if isinstance(saved, dict) else None
            try: identifier(task)
            except (ValueError, TypeError): task = None
            damaged.append(dict(id=task, file_sha256=hashlib.sha256(entry.name.encode()).hexdigest(),
                                message='Requirement transition receipt is damaged; ask the host operator to reconcile it'))
    return found, damaged


def pending_ids(project_path, project):
    receipts, damaged = scan(project_path, project)
    return {r['id'] for r in receipts if r['status'] == 'pending'}, {r['id'] for r in damaged if r['id'] is not None}


def absence(row, rows, run):
    """A complete export AND membership read, under the caller's existing locks."""
    import record_json
    ids = record_json.native_ids(run)
    if (any(r.get('malformed') for r in rows) or set(ids) != {r.get('id') for r in rows}
            or row is None):
        raise ValueError('Native absence cannot be proved from this read; reload before abandoning')
    state, reason = owner.state_ledger(row, allow_partial=True)
    return state is None and reason is None


def recover(project_path, context, action, task, body, run):
    """The current owner authorizes the action; stored evidence keeps its author."""
    path = receipt_path(project_path, body['original_operation_id'])
    saved = read_receipt(path)
    if saved['id'] != task:
        raise ValueError('Terminal recovery does not belong to this requirement')
    governance.validate_evidence(project_path, context.project, saved['owner_state'])
    if saved['status'] == 'released':
        if action == 'abandon' and body['expected_receipt_sha256'] in (receipt_sha256(saved), saved['terminal_disposition']['prior_receipt_sha256']):
            return dict(id=task, operation_id=body['original_operation_id'], disposition='released', reconciled=True)
        raise ValueError('This requirement transition was abandoned; start a new transition')
    if (receipt_sha256(saved) != body['expected_receipt_sha256']
            and not (saved['status'] == 'complete'
                     and receipt_sha256(dict(saved, status='pending')) == body['expected_receipt_sha256'])):
        raise ValueError('Requirement transition changed; reload before recovering')
    if action == 'finish':
        from requirement_http import OwnerContext
        original = OwnerContext(context.project, saved['actor'], context.governance)
        # Only the verified server-stored human author may be supplied to native
        # comments. This is not caller-controlled attribution or new evidence.
        def attributed(argv):
            if argv[:2] == ['comments', 'add']:
                argv = [*argv, '--actor', saved['actor']]
            return run(argv)
        try:
            return apply(project_path, original, saved['operation'], saved['request'],
                         body['original_operation_id'], attributed, task)
        except ValueError as error:
            if saved['operation'] == 'supersede' and any(word in str(error).lower() for word in ('target', 'accepted', 'changed')):
                raise ValueError('The supersession successor is no longer eligible; finish is unavailable. Abandon only if no state was written.') from None
            raise
    rows = records.read_rows(run); row = records.find(rows, task)
    if not absence(row, rows, run):
        raise ValueError('A terminal state was already written; finish this transition instead of abandoning')
    if saved['status'] != 'pending':
        raise ValueError('This requirement transition is already complete')
    disposition = dict(project=saved['owner_state']['project'], actor=context.account_id, at=core.now(),
                       prior_receipt_sha256=receipt_sha256(saved), reason=body['reason'])
    disposition['sha256'] = content_hash(disposition)
    released = dict(saved, status='released', terminal_disposition=disposition)
    validate_receipt(released); atomic(path, released)
    if hasattr(run, 'wrote'): run.wrote = True
    return dict(id=task, operation_id=body['original_operation_id'], disposition='released', reconciled=False)


def projection(project_path, project, rows, run):
    receipts, damaged = scan(project_path, project)
    items = []
    for saved in receipts:
        if saved['status'] != 'pending': continue
        row = records.find(rows, saved['id'])
        item = dict(id=saved['id'], operation_id=saved['owner_state']['operation_id'],
                    expected_receipt_sha256=receipt_sha256(saved), state=saved['owner_state'],
                    reason=saved['owner_state_reason'], can_finish=False, can_abandon=False,
                    message='A requirement transition is pending. Finish or abandon it.')
        try:
            original_body = saved['request']
            from requirement_http import OwnerContext
            context = OwnerContext(project, saved['actor'], governance.current(project_path, project))
            checked_current(project_path, context, row, original_body)
            state, reason = owner.state_ledger(row, allow_partial=True)
            if state is not None and (state != saved['owner_state'] or reason is not None and reason != saved['owner_state_reason']):
                raise ValueError('Requirement state conflicts with its transition receipt; ask the host operator')
            item['can_abandon'] = absence(row, rows, run)
            successor = original_body.get('successor')
            if successor and (state, reason) != (saved['owner_state'], saved['owner_state_reason']):
                target = records.find(rows, successor['id'])
                checked_current(project_path, context, target, dict(expected_revision=successor['revision'], expected_sha256=successor['sha256']))
                if owner.state_ledger(target)[0] is not None:
                    raise ValueError('The supersession successor is no longer eligible; finish is unavailable.')
                pending, broken = pending_ids(project_path, project)
                if successor['id'] in pending or successor['id'] in broken:
                    raise ValueError('The supersession successor is unavailable; finish after its recovery.')
            item['can_finish'] = True
        except (ValueError, OSError, TypeError, KeyError) as error:
            # All errors emitted above are fixed kit sentences, never path data.
            item['message'] = 'This transition cannot be finished now. Reload; if its successor is no longer eligible, abandon only when no state was written.'
        items.append(item)
    return dict(items=items, damaged=damaged, total=len(items))
