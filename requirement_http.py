"""Service-only owner adapter and member reads for project requirements.

The host adapter still checks the operator allowlist. This adapter supplies its
own validated hooks to the same native mutation engine; operator=True is never
used to impersonate host authority.
"""
import json
import hashlib
from dataclasses import dataclass

import keyed_records as core
import requirement_governance as governance
import requirement_owner_records as owner_records
import requirement_records as records
import record_json
from coordination import atomic, identifier
from requirements import canonical_bytes, content_hash, load_json

JOURNAL = '.requirement-owner-requests'


def validate_receipt(receipt):
    """Legacy receipts remain valid; optional decision bindings are strict."""
    core.validate_receipt(receipt)
    if 'owner_decision' in receipt:
        binding = owner_records.validate(receipt['owner_decision'])
        if (binding['account_id'] != receipt.get('actor')
                or binding['id'] != receipt.get('id')
                or binding['revision'] != receipt.get('revision')
                or receipt.get('operation') != 'revise'):
            raise ValueError('Owner decision does not match its receipt')
        if receipt.get('acceptance') is not None:
            expected = dict(binding['decision'], record_sha256=binding['record_sha256'])
            if receipt['acceptance'] != expected:
                raise ValueError('Owner decision conflicts with completed acceptance')
    if 'release_history' in receipt:
        history = receipt['release_history']
        if (receipt['status'] != 'released' or receipt.get('id') is not None
                or receipt.get('operation') != 'draft' or receipt.get('revision') != 1
                or not isinstance(history, list) or len(history) != 1
                or 'owner_decision' in receipt or receipt.get('acceptance') is not None):
            raise ValueError('Invalid empty owner creation release')
        audit = history[0]
        fields = {'schema_version', 'project', 'account_id', 'operation_id',
                  'prior_receipt_sha256', 'reason', 'at', 'sha256'}
        if not isinstance(audit, dict) or set(audit) != fields or type(audit['schema_version']) is not int or audit['schema_version'] != 1:
            raise ValueError('Invalid owner creation release audit')
        governance.project_name(audit['project']); identifier(audit['operation_id'])
        governance.digest(audit['prior_receipt_sha256']); governance.digest(audit['sha256'])
        if (audit['account_id'] != receipt.get('actor')
                or not isinstance(audit['account_id'], str)
                or not owner_records.HUMAN.fullmatch(audit['account_id'])
                or not isinstance(audit['reason'], str) or not 1 <= len(audit['reason'].strip()) <= 1000
                or not isinstance(audit['at'], str) or not audit['at'].strip()
                or content_hash(audit) != audit['sha256']):
            raise ValueError('Invalid owner creation release audit binding')
    return receipt


def receipt_sha256(receipt):
    return hashlib.sha256(canonical_bytes(receipt)).hexdigest()


def empty_creation(receipt, context, operation_id, run):
    """Prove no native row was allocated; caller holds both canonical locks."""
    validate_receipt(receipt)
    message = ('Cannot clear this failed creation. Retry the original creation; '
               'if it still fails, ask the host operator to reconcile it.')
    if (receipt.get('actor') != context.account_id or receipt['status'] != 'pending'
            or receipt.get('operation') != 'draft' or receipt.get('revision') != 1
            or receipt.get('id') is not None or receipt.get('created')
            or receipt.get('acceptance') is not None or 'owner_decision' in receipt):
        raise ValueError(message)
    rows = records.read_rows(run)
    ids = record_json.native_ids(run)
    row_ids = [row.get('id') for row in rows]
    if (any(not isinstance(value, str) or not value for value in row_ids)
            or len(row_ids) != len(set(row_ids)) or set(row_ids) != ids
            # Pinned bd omits labels entirely on ordinary unlabeled rows.
            # A present null/non-list value still cannot prove absence.
            or any(row.get('malformed') or not isinstance(row.get('labels', []), list)
                   or any(not isinstance(label, str) for label in row.get('labels', [])) for row in rows)):
        raise ValueError(message + ' Native absence cannot be proved from this read.')
    label = 'request:' + content_hash({'operation_id': operation_id})
    if any(label in row.get('labels', []) for row in rows):
        raise ValueError(message + ' A native row already carries this request.')


def recovery_projection(project_path, context, run):
    from http_authority import OperationJournal, journal_path
    path = project_path / JOURNAL
    if path.is_symlink():
        raise ValueError('Owner recovery receipts need host operator repair')
    if not path.exists():
        return {'items': [], 'truncated': False}
    journal = OperationJournal(journal_path(project_path))
    candidates = journal.pending_owner_creates('owner-account:' + context.account_id)
    items = []
    for entry in candidates[:20]:
        operation = entry['operation_id']
        receipt = core.receipt_path(path, content_hash({'operation_id': operation}), 'requirement')
        if not receipt.is_file():
            continue
        saved = validate_receipt(load_json(receipt))
        if saved.get('actor') != context.account_id or saved.get('operation') != 'draft':
            continue
        item = {'operation_id': operation, 'expected_receipt_sha256': receipt_sha256(saved),
                'can_clear': False}
        try:
            if journal.expired(entry):
                raise ValueError('This failed creation needs host operator reconciliation.')
            if saved['status'] == 'released' and 'release_history' in saved:
                audit = saved['release_history'][0]
                if audit['project'] != context.project or audit['operation_id'] != operation:
                    raise ValueError('This failed creation needs host operator reconciliation.')
                item.update(expected_receipt_sha256=audit['prior_receipt_sha256'], reason=audit['reason'])
            else:
                empty_creation(saved, context, operation, run)
            item['can_clear'] = True
        except ValueError as error:
            item['message'] = str(error)
        items.append(item)
    return {'items': items, 'truncated': len(candidates) > 20}


def release_creation(project_path, context, body, run, runner):
    """Retain an audited terminal receipt, then close the original uncertain intent."""
    from http_authority import OperationJournal, journal_path, stamp_write
    operation = body['original_operation_id']
    path = core.receipt_path(core.journal_dir(project_path, JOURNAL),
                             content_hash({'operation_id': operation}), 'requirement')
    if not path.is_file():
        raise ValueError('No failed creation was found; ask the host operator to reconcile it')
    saved = validate_receipt(load_json(path))
    if saved.get('actor') != context.account_id:
        raise ValueError('Only the owner account that attempted this creation can clear it')
    journal = OperationJournal(journal_path(project_path)); entry = journal.lookup(operation)
    if (entry is None or entry.get('principal') != 'owner-account:' + context.account_id
            or entry.get('route') != 'requirements.create'):
        raise ValueError('The original creation identity cannot be verified; ask the host operator')
    if saved['status'] == 'released' and 'release_history' in saved:
        audit = saved['release_history'][0]
        if (audit['project'] != context.project or audit['operation_id'] != operation
                or audit['reason'] != body['reason']
                or audit['prior_receipt_sha256'] != body['expected_receipt_sha256']):
            raise ValueError('Failed creation recovery changed; reload before trying again')
    else:
        if entry.get('state') not in ('unknown', 'in_progress') or journal.expired(entry):
            raise ValueError('The original creation is not recoverable; ask the host operator')
        if receipt_sha256(saved) != body['expected_receipt_sha256']:
            raise ValueError('Failed creation recovery changed; reload before trying again')
        empty_creation(saved, context, operation, run)
        audit = dict(schema_version=1, project=context.project, account_id=context.account_id,
                     operation_id=operation, prior_receipt_sha256=body['expected_receipt_sha256'],
                     reason=body['reason'], at=core.now())
        audit['sha256'] = content_hash(audit)
        saved = dict(saved, status='released', release_history=[audit])
        validate_receipt(saved); runner.wrote = True; atomic(path, saved)
    # The retained released receipt also refuses any reuse after journal expiry.
    if entry.get('state') in ('unknown', 'in_progress'):
        runner.wrote = True; journal._actor = entry.get('actor'); journal._route = entry['route']
        journal.complete(operation, stamp_write({'returncode': 2, 'stdout': '',
            'stderr': 'Failed creation was cleared without a native write. Start a new creation.'}),
            entry['request_hash'], entry['principal'])
    return {'released': True, 'audit': audit}


@dataclass(frozen=True)
class OwnerContext:
    project: str
    account_id: str
    governance: dict


def owner_context(project_path, project, request, authority_config, simple=True):
    """Check server-bound session AND current project membership, at effect time.

    Called under run_guarded's authority lock. A preflight may call it too, but
    that cannot replace this effect-time check. Never use caller store/lock paths.
    """
    from http_authority import file_lock, read_state
    from project_creation import service_descriptor
    descriptor = service_descriptor(request, authority_config, 'owner-requirements')
    if (descriptor.get('project') != project or descriptor.get('credential_id') is not None
            or not owner_records.HUMAN.fullmatch(request['actor'])):
        raise ValueError('Requirements editing needs the signed-in project owner')
    # The preflight runs before run_guarded takes this lock. On Windows an
    # open authority reader prevents the service's atomic state replacement.
    # Share its existing lock; effect-time calls already hold it re-entrantly.
    with file_lock(authority_config.lock):
        state = read_state(authority_config.store)
    account = descriptor['user_id']
    if state.get('memberships', {}).get(project, {}).get(account) != 'owner':
        raise ValueError('Requirements editing needs the signed-in project owner')
    current = governance.current(project_path, project)
    if simple and current['mode'] != 'simple':
        raise ValueError('This project uses governed requirements. Ask its owner to enable simple editing.')
    return OwnerContext(project, account, current)


def checked_body(body, action):
    allowed = {'create': {'kind', 'parent', 'title', 'description', 'key'},
               'revise': {'expected_revision', 'expected_sha256', 'title', 'description'},
               'accept': {'expected_revision', 'expected_sha256'},
               'release': {'original_operation_id', 'expected_receipt_sha256', 'reason'},
               'governance': {'mode', 'expected_revision', 'expected_sha256'}}
    if action not in allowed or not isinstance(body, dict) or set(body) - allowed[action]:
        raise ValueError('Unsupported requirements fields or action')
    if action == 'release':
        identifier(body.get('original_operation_id')); governance.digest(body.get('expected_receipt_sha256'))
        if (not isinstance(body.get('reason'), str) or not 1 <= len(body['reason'].strip()) <= 1000
                or '\0' in body['reason']):
            raise ValueError('Explain why this failed creation should be cleared (1 to 1000 characters)')
    elif action != 'create':
        if 'expected_sha256' not in body:
            raise ValueError('Expected requirements hash is required; reload and try again')
        expected = body.get('expected_revision')
        if type(expected) is not int or expected < (0 if action == 'governance' else 1):
            raise ValueError('Invalid expected requirements revision')
        if expected:
            governance.digest(body.get('expected_sha256'))
        elif body.get('expected_sha256') is not None:
            raise ValueError('Missing requirements governance has no hash')
    for field, limit in (('title', 200), ('description', 20000)):
        if field in body and (not isinstance(body[field], str) or not body[field].strip()
                              or len(body[field]) > limit or '\0' in body[field]):
            raise ValueError('Requirement %s must contain 1 to %d characters' % (field, limit))
    return dict(body)


def _spec(context, body, action, operation_id, prior=None, run=None):
    """Closed content hooks; authority and generated fields are never payload fields."""
    values = {name: getattr(records.SPEC, name) for name in core.RecordSpec.VALUES + core.RecordSpec.HOOKS}
    values.update(journal=JOURNAL, acceptance_prefix=owner_records.ACCEPTANCE_PREFIX,
                  apply_command='the owner requirements page',
                  reconcile_command='the owner requirements operation',
                  refuse_before_journal=lambda payload, operator: None)
    stage = {}

    def validate(payload, operator):
        if operator:
            raise ValueError('Owner requirements adapter does not use host operator authority')
        # The standard content schema remains unchanged; only this adapter's
        # generated target state is checked separately from contributor authority.
        records.validate_payload(dict(payload, acceptance_state='draft'))

    def build(payload, task, existing):
        if action == 'create':
            return 1, records.requirement_record(payload, task, 1)
        expected = body['expected_revision']
        previous = existing.get(expected)
        if previous is None or previous['sha256'] != body['expected_sha256']:
            raise ValueError('Requirements changed. Reload and try again.')
        revision = expected + 1
        record = records.requirement_record(payload, task, revision)
        newest = max(existing) if existing else 0
        if newest != expected and (newest != revision or existing.get(revision) != record):
            raise ValueError('Requirements changed. Reload and try again.')
        return revision, record

    def check_acceptance(payload, existing, record, operator, row):
        prior_owner = owner_records.existing_acceptances(row)
        # Read every host/owner evidence ledger before a write, including drafts.
        host = records._host_acceptances(row)
        if action != 'accept':
            return None
        if record['revision'] in host:
            raise ValueError('Requirements changed. Reload and try again.')
        prior_evidence = prior_owner.get(record['revision'])
        if prior_evidence is not None:
            if (prior_evidence['account_id'] != context.account_id or prior_evidence['project'] != context.project
                    or prior_evidence['operation_id'] != operation_id
                    or prior_evidence['record_sha256'] != record['sha256']
                    or prior_evidence['governance'] != context.governance):
                raise ValueError('Requirements changed. Reload and try again.')
            stage['at'] = prior_evidence['at']
            decision = prior_evidence['decision']['decision_id']
        else:
            stage['at'] = core.now()
            # A placeholder only during zero-write preflight; it is replaced by
            # the generated native decision before any evidence is written.
            decision = 'pending-' + content_hash({'operation_id': operation_id})[:24]
        if prior and 'owner_decision' in prior:
            binding = prior['owner_decision']
            if (binding['project'] != context.project or binding['operation_id'] != operation_id
                    or binding['governance'] != context.governance
                    or binding['record_sha256'] != record['sha256']
                    or binding['id'] != record['id'] or binding['revision'] != record['revision']):
                raise ValueError('Owner decision binding does not match this acceptance')
            stage['at'] = binding['at']
            decision = binding['decision']['decision_id']
            native_decision = records.find(records.read_rows(run), decision)
            if native_decision is None or native_decision.get('malformed'):
                raise ValueError('Recorded requirement decision is missing or unreadable; '
                                 'keep this operation unknown and ask the host operator to reconcile it')
        return {'record_sha256': record['sha256'], 'decision_id': decision,
                'owners': [context.account_id], 'approvers': [context.account_id],
                'policy': 'any-owner', 'evidence': decision}

    def before_evidence(run, task, revision, record, actor, bound, receipt):
        identity = content_hash({'owner_requirement_decision': operation_id})
        label = 'owner-requirement-decision:' + identity
        title = 'Accept requirement ' + task
        description = ('## Decision\nAccept the exact requirement content.\n\n## Rationale\n'
                       'The project owner accepted this content in simple mode.\n\n'
                       '## Alternatives Considered\nLeave it as a draft.\n\n'
                       'Requirement: %s revision %d sha256 %s\n' % (task, revision, record['sha256']))
        saved = validate_receipt(load_json(receipt))
        if 'owner_decision' in saved:
            decision_id = saved['owner_decision']['decision']['decision_id']
            decision = records.find(records.read_rows(run), decision_id)
            if decision is None or decision.get('malformed'):
                raise ValueError('Recorded requirement decision is missing or unreadable; '
                                 'keep this operation unknown and ask the host operator to reconcile it')
        else:
            args = ['create', '--title', title, '--description', description, '--type', 'decision',
                    '--no-inherit-labels', '--labels', label, '--json']
            run(args + ['--dry-run'])
            decision = record_json.loads(run(args))
            if not isinstance(decision, dict) or not decision.get('id'):
                raise ValueError('Generated decision outcome is uncertain; reconcile the operation')
            decision_id = decision['id']
            identifier(decision_id)
            saved['owner_decision'] = owner_records.acceptance(
                context.project, record, actor, operation_id, context.governance,
                decision_id, stage['at'])
            validate_receipt(saved)
            atomic(receipt, saved)
        return dict(bound, decision_id=decision_id, evidence=decision_id)

    def receipt_metadata(receipt):
        if receipt is None:
            return {}
        validate_receipt(receipt)
        return {'owner_decision': receipt['owner_decision']} if 'owner_decision' in receipt else {}

    def acceptance_evidence(bound, task, revision, record, actor):
        evidence = owner_records.acceptance(context.project, record, actor, operation_id,
                                            context.governance, bound['decision_id'], stage['at'])
        return evidence, owner_records.body(evidence)

    def result(payload, task, revision, record, created, reconciled, bound, comment_id=None):
        return {'id': task, 'kind': payload['kind'], 'revision': revision,
                'sha256': record['sha256'], 'acceptance_state': record['acceptance_state'],
                'created': created, 'reconciled': reconciled}

    values.update(validate=validate, build_record=build, check_acceptance=check_acceptance,
                  require_selectable=lambda row, payload, operator, existing: records.require_typed(row, payload),
                  acceptance_evidence=acceptance_evidence, before_evidence=before_evidence,
                  receipt_metadata=receipt_metadata,
                  live_acceptances=lambda row, operators: owner_records.existing_acceptances(row), result=result)
    return core.RecordSpec(**values)


def _payload(action, body, operation_id, run, task=None):
    """Deterministic content used by both application and receipt reconciliation."""
    payload = {'schema_version': 1, 'operation_id': operation_id,
               'operation': 'draft' if action == 'create' else 'revise',
               'acceptance_state': 'accepted' if action == 'accept' else 'draft'}
    if action == 'create':
        payload.update(body)
        if payload.get('kind') == 'requirement':
            payload.setdefault('key', 'req-' + content_hash({'operation_id': operation_id})[:12])
    else:
        identifier(task)
        row = records.find(records.read_rows(run), task)
        if row is None:
            raise ValueError('Requirement was not found')
        previous = records.existing_revisions(row).get(body['expected_revision'])
        if previous is None or previous['sha256'] != body['expected_sha256']:
            raise ValueError('Requirements changed. Reload and try again.')
        payload.update(task=task, kind='requirement' if 'key' in previous else 'brd-section',
                       title=body.get('title', previous['title']),
                       description=body.get('description', previous['description']),
                       revision=body['expected_revision'] + 1)
        if 'key' in previous:
            payload['key'] = previous['key']
    return payload


def apply(project_path, context, action, body, operation_id, run, task=None):
    """Called only by the effect-time checked owner adapter, holding project lock."""
    if (not isinstance(context, OwnerContext) or not owner_records.HUMAN.fullmatch(context.account_id)
            or governance.current(project_path, context.project) != context.governance
            or context.governance['mode'] != 'simple'):
        raise ValueError('Owner requirements context is stale or unavailable')
    body = checked_body(body, action)
    identifier(operation_id)
    if action == 'governance':
        raise ValueError('Governance uses its separate owner action')
    if action == 'accept':
        row = records.find(records.read_rows(run), task)
        revisions = records.existing_revisions(row) if row else {}
        latest = revisions.get(max(revisions)) if revisions else None
        if (latest and latest['revision'] == body['expected_revision']
                and latest['sha256'] == body['expected_sha256']
                and latest['acceptance_state'] == 'accepted'):
            evidence = owner_records.existing_acceptances(row).get(latest['revision'])
            if not records.resolved_acceptance(row, latest):
                raise ValueError('Requirement acceptance cannot be verified')
            if evidence:
                governance.validate_evidence(project_path, context.project, evidence)
            return {'id': task, 'kind': records.existing_kind(row), 'revision': latest['revision'],
                    'sha256': latest['sha256'], 'acceptance_state': 'accepted',
                    'created': False, 'reconciled': True}
    payload = _payload(action, body, operation_id, run, task)
    receipt = core.receipt_path(core.journal_dir(project_path, JOURNAL),
                                content_hash({'operation_id': operation_id}), 'requirement')
    prior = validate_receipt(load_json(receipt)) if receipt.exists() else None
    if prior and prior['status'] == 'released':
        raise ValueError('Failed creation was cleared. Start a new creation with a new operation ID.')
    if action == 'accept' and prior and prior['status'] == 'pending' and 'owner_decision' not in prior:
        raise ValueError('Pending requirement acceptance has no recorded decision binding; '
                         'keep this operation unknown and ask the host operator to reconcile it')
    if task is not None:
        row = records.find(records.read_rows(run), task)
        for evidence in owner_records.existing_acceptances(row).values():
            governance.validate_evidence(project_path, context.project, evidence)
    return core.apply_native(payload, context.account_id, run, project_path,
                             _spec(context, body, action, operation_id, prior, run))


def web_action(root, project_path, project, request, authority_config, runner, guarded_write):
    """Endpoint adapter; caller holds project lock, guarded_write holds authority lock."""
    from http_authority import OperationJournal, journal_path, operation_hash, principal_key, stamp_write
    # Stable account identity applies only to this checked owner adapter; keep
    # the actual session descriptor intact for every live authority recheck.
    account_hash = lambda built, trusted: operation_hash(built, trusted, account_identity=True)
    account_key = lambda built, trusted: principal_key(built, trusted, account_identity=True)
    from export_requirements import parse_json
    args = request.get('args')
    if args == ['recoveries']:
        def read_effect():
            context = owner_context(project_path, project, request, authority_config)
            return {'returncode': 0, 'stdout': json.dumps(recovery_projection(project_path, context, runner)) + '\n', 'stderr': ''}
        return guarded_write(root, request, journal_path(project_path), read_effect,
                             authority_config=authority_config, require_authority=True, runner=runner,
                             account_identity=True, identity_check=lambda: owner_context(
                                 project_path, project, request, authority_config))
    if (not isinstance(args, list) or not args or args[0] not in ('create', 'revise', 'accept', 'governance', 'release')
            or len(args) != (2 if args[0] in ('revise', 'accept') else 1)):
        raise ValueError('Unsupported owner requirements action')
    action = args[0]
    attachment = (request.get('attachments') or {}).get('payload')
    if not isinstance(attachment, dict) or not isinstance(attachment.get('text'), str):
        raise ValueError('Requirements action needs its attached content')
    body = checked_body(parse_json(attachment['text']), action)
    operation_id = request.get('operation_id')
    identifier(operation_id)
    # Service-only session shape and ordinary refusals precede reservation. The
    # membership check is repeated inside the effect under the authority lock.
    owner_context(project_path, project, request, authority_config, simple=action != 'governance')
    def identity_check():
        # Check current owner membership under the authority lock even when a
        # journal replay returns without calling the effect.
        return owner_context(project_path, project, request, authority_config, simple=action != 'governance')

    def effect():
        context = owner_context(project_path, project, request, authority_config, simple=action != 'governance')
        if action == 'governance':
            result = governance.set_mode(project_path, project, body.get('mode'), context.account_id,
                                         operation_id, body['expected_revision'], body['expected_sha256'])
            runner.wrote = True
        elif action == 'release':
            result = release_creation(project_path, context, body, runner, runner)
        else:
            result = apply(project_path, context, action, body, operation_id, runner,
                           task=args[1] if len(args) == 2 else None)
        return {'returncode': 0, 'stdout': json.dumps(result, ensure_ascii=False) + '\n', 'stderr': ''}
    journal = OperationJournal(journal_path(project_path))
    prior = journal.lookup(operation_id)
    answer = guarded_write(root, request, journal_path(project_path), effect,
                           authority_config=authority_config, require_authority=True, runner=runner,
                           account_identity=True, identity_check=identity_check)
    if (answer.get('returncode') != 124 or prior is None
            or prior.get('state') not in ('unknown', 'in_progress')
            or journal.expired(prior)
            or prior.get('principal') != account_key(request, True)
            or prior.get('request_hash') != account_hash(request, True)):
        return answer

    # A retry does not release or re-run the unknown outer operation. Reconcile
    # its EXACT inner receipt through a separate guarded recovery operation.
    # This preserves normal journal uncertainty everywhere else; no auth policy
    # or generic run_guarded retry rule changes. The caller holds the project lock.
    def reconcile():
        context = owner_context(project_path, project, request, authority_config,
                                simple=action != 'governance')
        entry = journal.lookup(operation_id)
        if (entry is None or entry.get('state') not in ('unknown', 'in_progress')
                or journal.expired(entry)
                or entry.get('principal') != account_key(request, True)
                or entry.get('request_hash') != account_hash(request, True)):
            raise ValueError('Requirements recovery identity changed; ask the project owner to reconcile it')
        if action == 'governance':
            stored = governance.snapshot(project_path, project).get(governance.FILE, {})
            match = next((e for e in stored.get('revisions', []) if e['operation_id'] == operation_id), None)
            if (match is None or match['actor'] != context.account_id
                    or match['mode'] != body.get('mode') or match['via'] != 'session'
                    or match['revision'] != body['expected_revision'] + 1
                    or match['previous_sha256'] != body['expected_sha256']):
                raise ValueError('No matching governance change was confirmed; keep the original operation unknown')
        elif action == 'release':
            pass  # release_creation revalidates the retained audit and original identity.
        else:
            from requirements import load_json
            payload = _payload(action, body, operation_id, runner, args[1] if len(args) == 2 else None)
            path = core.receipt_path(core.journal_dir(project_path, JOURNAL),
                                     content_hash({'operation_id': operation_id}), 'requirement')
            if not path.is_file():
                raise ValueError('No matching requirement receipt was confirmed; keep the original operation unknown')
            receipt = core.validate_receipt(load_json(path))
            if (receipt.get('actor') != context.account_id
                    or receipt.get('sha256') != content_hash({'actor': context.account_id, 'payload': payload})
                    or receipt.get('status') not in ('pending', 'complete')):
                raise ValueError('Requirement recovery receipt does not match this exact owner request')
        completed = stamp_write(effect())  # exact native receipt, same time on future replay
        journal._actor = request['actor']; journal._route = request.get('route')
        journal.complete(operation_id, completed, account_hash(request, True), account_key(request, True))
        return completed

    recovery = dict(request, operation_id='owner-recover-' + content_hash({
        'original': operation_id, 'request_sha256': account_hash(request, True)})[:40],
        route='requirements-recovery')
    # An interrupted recovery remains unknown too. Never rerun that identity:
    # a later attempt gets its own deterministic successor and must again prove
    # the original inner receipt under the authority lock before any effect.
    seen = set()
    while recovery['operation_id'] not in seen:
        seen.add(recovery['operation_id'])
        interrupted = journal.lookup(recovery['operation_id'])
        if interrupted is None or interrupted.get('state') not in ('unknown', 'in_progress'):
            break
        if (journal.expired(interrupted) or interrupted.get('principal') != account_key(recovery, True)
                or interrupted.get('request_hash') != account_hash(recovery, True)):
            return answer  # preserve the conflicting or expired uncertainty
        recovery['operation_id'] = 'owner-recover-' + content_hash({
            'original': operation_id, 'previous': recovery['operation_id'],
            'request_sha256': account_hash(request, True)})[:40]
    else:
        return answer
    return guarded_write(root, recovery, journal_path(project_path), reconcile,
                         authority_config=authority_config, require_authority=True, runner=runner,
                         account_identity=True, identity_check=identity_check)


def read(project_path, project, args, run, operators=None):
    """One read-only native snapshot; current content and exact historical refs."""
    if args == ['governance']:
        return governance.read_state(project_path, project)
    if args == ['snapshot']:
        return governance.snapshot(project_path, project)
    if not isinstance(args, list) or args[:1] not in (['list'], ['brd'], ['get']):
        raise ValueError('Use requirements list, get ID, governance or brd')
    if len(args) != (2 if args[0] == 'get' else 1):
        raise ValueError('Unexpected requirements read argument')
    labels = ['requirement', 'brd-section']
    # Requirements are native tasks carrying these labels. The pinned native
    # tracker has no requirement/brd-section issue types, so its unreadable-row
    # membership probes must use the supported label filters.
    rows = record_json.classify(records.read_rows(run), run, labels)
    items, decisions = [], []
    for row in rows:
        if not (records.TYPE_LABELS.intersection(row.get('labels') or [])
                or row.get('issue_type') in labels or record_json.selected(row, labels)):
            continue
        try:
            if row.get('malformed'):
                raise ValueError('native row is unreadable')
            kind = records.existing_kind(row)
            revisions = records.existing_revisions(row)
            for evidence in owner_records.existing_acceptances(row).values():
                governance.validate_evidence(project_path, project, evidence)
            if not revisions:
                raise ValueError('content was not confirmed; retry the original creation request')
            history, accepted_history = [], []
            for number in sorted(revisions):
                record = revisions[number]
                accepted = records.resolved_acceptance(row, record, operators)
                if record['acceptance_state'] == 'accepted' and not accepted:
                    raise ValueError('acceptance cannot be verified')
                history.append(dict(record))
                if accepted:
                    accepted_history.append(dict(record))
            evidence = records.existing_acceptances(row, operators)
            latest = history[-1]
            accepted = accepted_history[-1] if accepted_history else None
            items.append({'id': row['id'], 'kind': kind, 'current': latest,
                          'accepted': accepted, 'pending_draft': latest if latest['acceptance_state'] == 'draft' else None,
                          'history': history, 'acceptance': evidence.get(latest['revision'])})
            for record in accepted_history:
                proof = evidence.get(record['revision'])
                if proof is not None:
                    decisions.append({'id': proof['decision']['decision_id'], 'requirement_id': row['id'],
                                      'title': 'Accepted ' + record['title'], 'revision': record['revision']})
        except (ValueError, TypeError, KeyError):
            # The affected row stays visible by proved id. Never promote a bad
            # acceptance, infer text from mutable fields, or hide healthy rows.
            items.append({'id': row['id'], 'kind': 'unreadable', 'unreadable': True,
                          'message': 'Requirement %s cannot be read or its acceptance cannot be verified; ask the project owner to reconcile it' % row['id'],
                          'current': None, 'accepted': None, 'pending_draft': None, 'history': []})
    items.sort(key=lambda item: (item['kind'], (item['current'] or {}).get('key', ''), item['id']))
    if args[0] == 'get':
        found = next((item for item in items if item['id'] == args[1]), None)
        if found is None:
            raise ValueError('Requirement was not found')
        if found.get('unreadable'):
            raise ValueError(found['message'])
        return found
    result = {'project': project, 'governance': governance.read_state(project_path, project),
              'items': items, 'total': len(items),
              'jobs': [{'id': row['id'], 'title': row.get('title', '')}
                       for row in rows if row.get('issue_type') in ('epic', 'job', 'task')
                       and not records.TYPE_LABELS.intersection(row.get('labels') or [])
                       and 'gt:slot' not in (row.get('labels') or [])
                       and not row.get('malformed')]}
    if args[0] == 'brd':
        result['decisions'] = decisions
    return result
