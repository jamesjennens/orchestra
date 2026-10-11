"""Owner terminal transitions, serialized by the canonical project/authority locks.

The exact pair is saved before either native comment. A retry reads native state
and writes only missing evidence. Acceptance and content are never rewritten.
"""
import hashlib
import math
import time
from datetime import datetime, timezone
import keyed_records as core
import requirement_governance as governance
import requirement_owner_records as owner
import requirement_records as records
from coordination import atomic, identifier
from requirements import canonical_bytes, content_hash, load_json

JOURNAL = '.requirement-owner-requests'
NATIVE_TIMEOUT = 120


def write_window(service_now=None, endpoint_timeout=150):
    """Server launch configuration and service time, never browser request fields."""
    moment = time.time() if service_now is None else service_now
    if (isinstance(moment, bool) or not isinstance(moment, (int, float))
            or not math.isfinite(moment)):
        raise ValueError('Terminal service time cannot be verified')
    # Starting the full window again at effect time is conservative when the
    # endpoint spent time waiting for its locks. Native descendants have their
    # own timeout; the service also terminates their process group on timeout.
    return dict(service_started_at=moment, endpoint_timeout=endpoint_timeout,
                native_timeout=NATIVE_TIMEOUT,
                abandon_after=max(moment, time.time()) + endpoint_timeout + NATIVE_TIMEOUT)


def seal(receipt):
    saved = dict(receipt)
    saved.pop('terminal_sha256', None)
    saved['terminal_sha256'] = hashlib.sha256(canonical_bytes(saved)).hexdigest()
    return saved


def abandonment_wait(saved):
    until = saved['write_window']['abandon_after']
    if time.time() < until:
        retry = datetime.fromtimestamp(until, timezone.utc).isoformat(timespec='seconds')
        return 'The original writer may still be running. Reload and retry abandonment after %s.' % retry
    return None


def state_written(saved, row):
    if row is None:
        return False
    try:
        state, _ = owner.state_ledger(row, allow_partial=True)
    except (ValueError, TypeError, KeyError):
        return False
    return state == saved['owner_state']


def effective_pending(saved, row=None):
    return saved['status'] == 'pending' or saved['status'] == 'released' and state_written(saved, row)


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
    if seal(receipt)['terminal_sha256'] != receipt.get('terminal_sha256'):
        raise ValueError('Terminal receipt status or history cannot be verified')
    window = receipt.get('write_window')
    if (not isinstance(window, dict)
            or set(window) != {'service_started_at', 'endpoint_timeout', 'native_timeout', 'abandon_after'}
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                   for value in window.values())
            or window['endpoint_timeout'] <= 0 or window['native_timeout'] != NATIVE_TIMEOUT
            or window['abandon_after'] < window['service_started_at'] + window['endpoint_timeout'] + NATIVE_TIMEOUT):
        raise ValueError('Invalid terminal write deadline')
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
    if disposition is not None:
        fields = {'project', 'actor', 'at', 'operation_id', 'prior_receipt_sha256', 'reason', 'via', 'sha256'}
        if (not isinstance(disposition, dict) or set(disposition) != fields
                or disposition['project'] != state['project']
                or not isinstance(disposition['at'], str) or not disposition['at'].strip()
                or not isinstance(disposition['reason'], str) or not disposition['reason'].strip()
                or len(disposition['reason']) > 1000
                or disposition['via'] not in ('owner', 'operator')
                or content_hash(disposition) != disposition['sha256']):
            raise ValueError('Invalid terminal abandonment disposition')
        governance.digest(disposition['prior_receipt_sha256'])
        identifier(disposition['actor'])
        identifier(disposition['operation_id'])
    elif receipt['status'] == 'released':
        raise ValueError('Missing terminal abandonment disposition')
    completion = receipt.get('completion')
    if receipt['status'] == 'complete':
        if (not isinstance(completion, dict)
                or set(completion) != {'actor', 'at', 'operation_id', 'reason', 'via', 'pending_receipt_sha256'}
                or not isinstance(completion['at'], str) or not completion['at'].strip()
                or not isinstance(completion['reason'], str) or len(completion['reason']) > 1000
                or completion['via'] not in ('owner', 'operator')):
            raise ValueError('Invalid terminal completion history')
        identifier(completion['actor']); identifier(completion['operation_id'])
        governance.digest(completion['pending_receipt_sha256'])
    elif completion is not None:
        raise ValueError('Unexpected terminal completion history')
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


def apply(project_path, context, action, body, operation, run, task,
          finished_by=None, recovery_operation_id=None, completion_reason='', completion_via='owner'):
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
    pending, uncertain = pending_ids(project_path, context.project, rows)
    require_unreserved_successor(project_path, context.project, task, rows)
    if task in uncertain:
        raise ValueError('Requirement transition receipt is damaged; ask the host operator to reconcile it')
    if task in pending and prior is None:
        raise ValueError('A requirement transition is pending; finish or abandon it on the requirements page')
    if prior and prior['status'] == 'released' and state != prior['owner_state']:
        raise ValueError('This requirement transition was abandoned; start a new transition')
    if body['expected_state_sha256'] is not None:
        raise ValueError('A terminal requirement cannot be changed or reactivated; create a new requirement')
    if state is not None and (prior is None or state != prior['owner_state']
                              or reason is not None and reason != prior['owner_state_reason']):
        raise ValueError('Requirement already has a different terminal transition')
    if prior and prior['status'] == 'complete' and (state, reason) != (prior['owner_state'], prior['owner_state_reason']):
        raise ValueError('Completed terminal evidence is missing; ask the host operator to reconcile it')
    # The native state is the irreversible decision, even when its reason write
    # was interrupted. Its exact successor must never be selected again.
    successor = body.get('successor')
    proved_state = prior and state == prior['owner_state']
    if successor is not None and not proved_state:
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
                     owner_state=evidence, owner_state_reason=explanation,
                     write_window=context.terminal_window or write_window())
        prior = seal(prior)
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
        completion = dict(actor=finished_by or context.account_id, at=core.now(),
                          operation_id=recovery_operation_id or operation, reason=completion_reason,
                          via=completion_via, pending_receipt_sha256=receipt_sha256(prior))
        completed = seal(dict(prior, status='complete', completion=completion))
        validate_receipt(completed); atomic(path, completed)
    else:
        completed = prior
    return dict(id=task, operation_id=operation, revision=record['revision'], sha256=record['sha256'],
                acceptance_state='accepted', requirement_state=evidence['state'],
                state=evidence, reason=explanation, completion=completed['completion'],
                reconciled=state is not None)


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


def pending_ids(project_path, project, rows=None):
    receipts, damaged = scan(project_path, project)
    return {r['id'] for r in receipts if effective_pending(r, records.find(rows, r['id']) if rows is not None else None)}, {r['id'] for r in damaged if r['id'] is not None}


def require_unreserved_successor(project_path, project, task, rows=None):
    """A pending predecessor owns its exact successor until finish or abandon."""
    receipts, _ = scan(project_path, project)
    predecessors = sorted(r['id'] for r in receipts if effective_pending(r, records.find(rows, r['id']) if rows is not None else None)
                          and r['owner_state']['superseded_by'] is not None
                          and r['owner_state']['superseded_by']['id'] == task)
    if predecessors:
        raise ValueError('Requirement is the successor of pending transition %s; '
                         'finish or abandon that predecessor before changing this requirement'
                         % ', '.join(predecessors))


def absence(row, rows, run, native_ids=None, native_error=None):
    """A complete export AND membership read, under the caller's existing locks."""
    import record_json
    if native_error:
        raise ValueError(native_error)
    ids = record_json.native_ids(run) if native_ids is None else native_ids
    if (any(r.get('malformed') for r in rows) or set(ids) != {r.get('id') for r in rows}
            or row is None):
        raise ValueError('Native absence cannot be proved from this read; reload before abandoning')
    state, reason = owner.state_ledger(row, allow_partial=True)
    return state is None and reason is None


def recover(project_path, context, action, task, body, run, recovery_operation_id=None, completion_via='owner'):
    """The current owner authorizes the action; stored evidence keeps its author."""
    path = receipt_path(project_path, body['original_operation_id'])
    saved = read_receipt(path)
    if saved['id'] != task:
        raise ValueError('Terminal recovery does not belong to this requirement')
    governance.validate_evidence(project_path, context.project, saved['owner_state'])
    rows = records.read_rows(run); row = records.find(rows, task)
    written = state_written(saved, row)
    if saved['status'] == 'released' and not written:
        if action == 'abandon' and body['expected_receipt_sha256'] in (receipt_sha256(saved), saved['terminal_disposition']['prior_receipt_sha256']):
            return dict(id=task, operation_id=body['original_operation_id'], disposition='released',
                        recovery_operation_id=saved['terminal_disposition']['operation_id'], reconciled=True)
        raise ValueError('This requirement transition was abandoned; start a new transition')
    if (receipt_sha256(saved) != body['expected_receipt_sha256']
            and not (saved['status'] == 'complete'
                     and saved['completion']['pending_receipt_sha256'] == body['expected_receipt_sha256'])):
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
        return apply(project_path, original, saved['operation'], saved['request'],
                     body['original_operation_id'], attributed, task,
                     finished_by=context.account_id, recovery_operation_id=recovery_operation_id,
                     completion_reason=body.get('reason', ''), completion_via=completion_via)
    if written:
        raise ValueError('A terminal state was already written; finish this transition instead of abandoning')
    wait = abandonment_wait(saved)
    if wait:
        raise ValueError(wait)
    if not absence(row, rows, run):
        raise ValueError('A terminal state was already written; finish this transition instead of abandoning')
    if saved['status'] != 'pending':
        raise ValueError('This requirement transition is already complete')
    disposition = dict(project=saved['owner_state']['project'], actor=context.account_id, at=core.now(),
                       operation_id=recovery_operation_id or body['original_operation_id'],
                       prior_receipt_sha256=receipt_sha256(saved), reason=body['reason'], via=completion_via)
    disposition['sha256'] = content_hash(disposition)
    released = seal(dict(saved, status='released', terminal_disposition=disposition))
    validate_receipt(released); atomic(path, released)
    if hasattr(run, 'wrote'): run.wrote = True
    return dict(id=task, operation_id=body['original_operation_id'], disposition='released',
                recovery_operation_id=disposition['operation_id'], reconciled=False)


def projection(project_path, project, rows, run):
    receipts, damaged = scan(project_path, project)
    items = []
    native_ids = None
    native_error = None
    candidates = [saved for saved in receipts if effective_pending(saved, records.find(rows, saved['id']))]
    if candidates:
        import record_json
        try:
            native_ids = record_json.native_ids(run)
        except (ValueError, OSError, TypeError, KeyError):
            native_error = 'Native absence cannot be proved from this read; reload before abandoning'
    for saved in candidates:
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
            try:
                item['can_abandon'] = absence(row, rows, run, native_ids, native_error)
                item['abandon_unavailable'] = abandonment_wait(saved)
                if item['abandon_unavailable']:
                    item['can_abandon'] = False
                elif not item['can_abandon']:
                    item['abandon_unavailable'] = 'A state was written. Finish this recorded transition.'
            except (ValueError, OSError, TypeError, KeyError):
                item['abandon_unavailable'] = 'Native absence cannot be proved; reload or ask the host operator.'
            successor = original_body.get('successor')
            if successor:
                target = records.find(rows, successor['id'])
                target_revisions = records.existing_revisions(target) if target else {}
                target_record = target_revisions.get(successor['revision'])
                item['successor_title'] = target_record['title'] if target_record else 'Unavailable requirement'
            if successor and state != saved['owner_state']:
                target = records.find(rows, successor['id'])
                checked_current(project_path, context, target, dict(expected_revision=successor['revision'], expected_sha256=successor['sha256']))
                if owner.state_ledger(target)[0] is not None:
                    raise ValueError('The supersession successor is no longer eligible; finish is unavailable.')
                pending, broken = pending_ids(project_path, project, rows)
                if successor['id'] in pending or successor['id'] in broken:
                    raise ValueError('The supersession successor is unavailable; finish after its recovery.')
            item['can_finish'] = True
            if context.governance['mode'] != 'simple':
                item['can_finish'] = item['can_abandon'] = False
                item['message'] = 'This project uses governed requirements. Enable simple editing to finish this saved transition, or ask the host operator.'
            if successor and state == saved['owner_state']:
                try:
                    checked_current(project_path, context, target, dict(expected_revision=successor['revision'], expected_sha256=successor['sha256']))
                    changed = owner.state_ledger(target)[0] is not None
                except (ValueError, TypeError, KeyError):
                    changed = True
                if changed and context.governance['mode'] == 'simple':
                    item['message'] = 'The successor has changed. Finish will retain the replacement recorded by this transition; its written decision cannot be abandoned.'
        except (ValueError, OSError, TypeError, KeyError) as error:
            item['message'] = str(error) if isinstance(error, ValueError) else 'This transition cannot be verified; reload or ask the host operator to reconcile it.'
            item['finish_unavailable'] = item['message']
        items.append(item)
    return dict(items=items, damaged=damaged, total=len(items),
                completions={saved['id']:saved['completion'] for saved in receipts if saved['status'] == 'complete'})


def operator_audits(project_path, project):
    """Service audit entries derived from the verified durable operator history.

    The receipt is the shared source: a host process never edits the running
    service's in-memory audit tail, where its write could later be overwritten.
    """
    receipts, _ = scan(project_path, project)
    events = []
    for saved in receipts:
        for disposition, history in (('released', saved.get('terminal_disposition')),
                                     ('complete', saved.get('completion'))):
            if not history or history['via'] != 'operator':
                continue
            operation = saved['owner_state']['operation_id']
            events.append(dict(time=history['at'], request_id='operator-terminal-'+content_hash({
                'operation':operation,'disposition':disposition,'history':history}),
                user_id=None, actor=history['actor'], credential_id=None, project_id=project,
                action='requirements.operator.'+('finish' if disposition=='complete' else 'abandon'),
                outcome='committed', reason=history['reason'], requirement_id=saved['id'],
                original_operation_id=operation, recovery_operation_id=history.get('operation_id')))
    return sorted(events, key=lambda event:(event['time'],event['request_id']))
