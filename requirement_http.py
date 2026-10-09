"""Service-only owner adapter and member reads for project requirements.

The host adapter still checks the operator allowlist. This adapter supplies its
own validated hooks to the same native mutation engine; operator=True is never
used to impersonate host authority.
"""
import json
from dataclasses import dataclass

import keyed_records as core
import requirement_governance as governance
import requirement_owner_records as owner_records
import requirement_records as records
import record_json
from coordination import identifier
from requirements import content_hash

JOURNAL = '.requirement-owner-requests'


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
    from http_authority import read_state
    from project_creation import service_descriptor
    descriptor = service_descriptor(request, authority_config, 'set-onboarding')
    if (descriptor.get('project') != project or descriptor.get('credential_id') is not None
            or not owner_records.HUMAN.fullmatch(request['actor'])):
        raise ValueError('Requirements editing needs the signed-in project owner')
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
               'governance': {'mode', 'expected_revision', 'expected_sha256'}}
    if action not in allowed or not isinstance(body, dict) or set(body) - allowed[action]:
        raise ValueError('Unsupported requirements fields or action')
    if action != 'create':
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


def _spec(context, body, action, operation_id):
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
        prior = prior_owner.get(record['revision'])
        if prior is not None:
            if (prior['account_id'] != context.account_id or prior['project'] != context.project
                    or prior['operation_id'] != operation_id
                    or prior['record_sha256'] != record['sha256']
                    or prior['governance'] != context.governance):
                raise ValueError('Requirements changed. Reload and try again.')
            stage['at'] = prior['at']
            decision = prior['decision']['decision_id']
        else:
            stage['at'] = core.now()
            # A placeholder only during zero-write preflight; it is replaced by
            # the generated native decision before any evidence is written.
            decision = 'pending-' + content_hash({'operation_id': operation_id})[:24]
        return {'record_sha256': record['sha256'], 'decision_id': decision,
                'owners': [context.account_id], 'approvers': [context.account_id],
                'policy': 'any-owner', 'evidence': decision}

    def before_evidence(run, task, revision, record, actor, bound):
        identity = content_hash({'owner_requirement_decision': operation_id})
        label = 'owner-requirement-decision:' + identity
        title = 'Accept requirement ' + task
        description = ('## Decision\nAccept the exact requirement content.\n\n## Rationale\n'
                       'The project owner accepted this content in simple mode.\n\n'
                       '## Alternatives Considered\nLeave it as a draft.\n\n'
                       'Requirement: %s revision %d sha256 %s\n' % (task, revision, record['sha256']))
        rows = records.read_rows(run)
        matches = [row for row in rows if label in (row.get('labels') or [])]
        if len(matches) > 1:
            raise ValueError('Ambiguous generated requirement decision; reconcile the operation')
        if matches:
            decision = matches[0]
            if (decision.get('issue_type') != 'decision' or decision.get('title') != title
                    or decision.get('description') != description
                    or decision.get('created_by') != actor):
                raise ValueError('Generated requirement decision does not match this operation')
            decision_id = decision['id']
        else:
            args = ['create', '--title', title, '--description', description, '--type', 'decision',
                    '--no-inherit-labels', '--labels', label, '--json']
            run(args + ['--dry-run'])
            decision = json.loads(run(args))
            if not isinstance(decision, dict) or not decision.get('id'):
                raise ValueError('Generated decision outcome is uncertain; reconcile the operation')
            decision_id = decision['id']
        return dict(bound, decision_id=decision_id, evidence=decision_id)

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
    payload = _payload(action, body, operation_id, run, task)
    if task is not None:
        row = records.find(records.read_rows(run), task)
        for evidence in owner_records.existing_acceptances(row).values():
            governance.validate_evidence(project_path, context.project, evidence)
    return core.apply_native(payload, context.account_id, run, project_path,
                             _spec(context, body, action, operation_id))


def web_action(root, project_path, project, request, authority_config, runner, guarded_write):
    """Endpoint adapter; caller holds project lock, guarded_write holds authority lock."""
    from http_authority import OperationJournal, journal_path, operation_hash, principal_key, stamp_write
    from export_requirements import parse_json
    args = request.get('args')
    if (not isinstance(args, list) or not args or args[0] not in ('create', 'revise', 'accept', 'governance')
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

    def effect():
        context = owner_context(project_path, project, request, authority_config, simple=action != 'governance')
        if action == 'governance':
            result = governance.set_mode(project_path, project, body.get('mode'), context.account_id,
                                         operation_id, body['expected_revision'], body['expected_sha256'])
            runner.wrote = True
        else:
            result = apply(project_path, context, action, body, operation_id, runner,
                           task=args[1] if len(args) == 2 else None)
        return {'returncode': 0, 'stdout': json.dumps(result, ensure_ascii=False) + '\n', 'stderr': ''}
    journal = OperationJournal(journal_path(project_path))
    prior = journal.lookup(operation_id)
    answer = guarded_write(root, request, journal_path(project_path), effect,
                           authority_config=authority_config, require_authority=True, runner=runner)
    if (answer.get('returncode') != 124 or prior is None
            or prior.get('state') not in ('unknown', 'in_progress')
            or journal.expired(prior)
            or prior.get('principal') != principal_key(request, True)
            or prior.get('request_hash') != operation_hash(request, True)):
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
                or entry.get('principal') != principal_key(request, True)
                or entry.get('request_hash') != operation_hash(request, True)):
            raise ValueError('Requirements recovery identity changed; ask the project owner to reconcile it')
        if action == 'governance':
            stored = governance.snapshot(project_path, project).get(governance.FILE, {})
            match = next((e for e in stored.get('revisions', []) if e['operation_id'] == operation_id), None)
            if (match is None or match['actor'] != context.account_id
                    or match['mode'] != body.get('mode') or match['via'] != 'session'
                    or match['revision'] != body['expected_revision'] + 1
                    or match['previous_sha256'] != body['expected_sha256']):
                raise ValueError('No matching governance change was confirmed; keep the original operation unknown')
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
        journal.complete(operation_id, completed, operation_hash(request, True), principal_key(request, True))
        return completed

    recovery = dict(request, operation_id='owner-recover-' + content_hash({
        'original': operation_id, 'request_sha256': operation_hash(request, True)})[:40],
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
        if (journal.expired(interrupted) or interrupted.get('principal') != principal_key(recovery, True)
                or interrupted.get('request_hash') != operation_hash(recovery, True)):
            return answer  # preserve the conflicting or expired uncertainty
        recovery['operation_id'] = 'owner-recover-' + content_hash({
            'original': operation_id, 'previous': recovery['operation_id'],
            'request_sha256': operation_hash(request, True)})[:40]
    else:
        return answer
    return guarded_write(root, recovery, journal_path(project_path), reconcile,
                         authority_config=authority_config, require_authority=True, runner=runner)


def read(project_path, project, args, run, operators=None):
    """One read-only native snapshot; current content and exact historical refs."""
    if args == ['governance']:
        return governance.current(project_path, project)
    if not isinstance(args, list) or args[:1] not in (['list'], ['brd'], ['get']):
        raise ValueError('Use requirements list, get ID, governance or brd')
    if len(args) != (2 if args[0] == 'get' else 1):
        raise ValueError('Unexpected requirements read argument')
    rows = record_json.classify(records.read_rows(run), run, ['requirement', 'brd-section'])
    items = []
    for row in rows:
        if not records.TYPE_LABELS.intersection(row.get('labels') or []):
            continue
        # An unreadable anchor never turns into guessed content from its title.
        if row.get('malformed'):
            raise ValueError('A requirement record cannot be read; ask the project owner to reconcile it')
        kind = records.existing_kind(row)
        revisions = records.existing_revisions(row)
        for evidence in owner_records.existing_acceptances(row).values():
            governance.validate_evidence(project_path, project, evidence)
        if not revisions:
            continue  # an interrupted create has no content to display
        latest = revisions[max(revisions)]
        history = []
        for number in sorted(revisions):
            record = revisions[number]
            accepted = records.resolved_acceptance(row, record, operators)
            if record['acceptance_state'] == 'accepted' and not accepted:
                raise ValueError('Requirement acceptance cannot be verified; ask the project owner to reconcile it')
            history.append(dict(record))
        evidence = records.existing_acceptances(row, operators)
        items.append({'id': row['id'], 'kind': kind, 'current': history[-1],
                      'history': history, 'acceptance': evidence.get(latest['revision'])})
    items.sort(key=lambda item: (item['kind'], item['current'].get('key', ''), item['id']))
    if args[0] == 'get':
        found = [item for item in items if item['id'] == args[1]]
        if not found:
            raise ValueError('Requirement was not found')
        return found[0]
    result = {'project': project, 'governance': governance.current(project_path, project),
              'items': items, 'total': len(items),
              'jobs': [{'id': row['id'], 'title': row.get('title', '')}
                       for row in rows if row.get('issue_type') in ('epic', 'job')
                       and not row.get('malformed')]}
    if args[0] == 'brd':
        result['questions'] = [{'id': row['id'], 'title': row.get('title', ''),
                                'description': row.get('description', '')}
                               for row in rows if row.get('issue_type') == 'question'
                               and row.get('status') != 'closed' and not row.get('malformed')]
        result['decisions'] = [{'id': row['id'], 'title': row.get('title', ''),
                                'description': row.get('description', '')}
                               for row in rows if row.get('issue_type') == 'decision' and not row.get('malformed')]
    return result
