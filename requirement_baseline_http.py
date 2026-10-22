"""Checked owner cuts and member-facing immutable requirements documents.

The endpoint holds the project lock. The canonical guarded writer checks live
owner membership before reservation, effect and replay. No caller paths exist.
"""
import copy
import json
from datetime import datetime, timezone

import requirement_baseline_records as schema
import requirement_baselines as store
from requirement_brd import compare, render
from requirements import canonical_bytes, content_hash
from coordination import identifier


def checked_body(body):
    if not isinstance(body, dict) or set(body) != {'name'}:
        raise ValueError('A baseline cut needs only its name.')
    return {'name': schema.baseline_name(body['name'])}


def read(project_path, project, args, run, operators=None):
    if args == ['baselines']:
        return store.list_baselines(project_path, project)
    if not isinstance(args, list) or not args or args[0] not in ('baseline', 'export', 'changes'):
        raise ValueError('Use baselines, baseline NAME, export [NAME] or changes [NAME].')
    action = args[0]
    if len(args) not in ((2,) if action == 'baseline' else (1, 2)):
        raise ValueError('Unexpected requirements document argument.')
    name = schema.baseline_name(args[1]) if len(args) == 2 else None
    if action == 'changes' and name is None:
        pointer = store.current(project_path, project)
        if pointer is None:
            return dict(baseline=None, available=False, message='No baseline has been cut yet.')
        name = pointer['name']
    artifact = store.get(project_path, project, name) if name is not None else None
    if action == 'baseline':
        return dict(name=name, receipt_sha256=artifact['receipt_sha256'], receipt=artifact['receipt'])
    if action == 'export':
        if artifact is None:
            captured = schema.capture(project_path, project, run, operators)
            markdown = render(captured['view'])
            if len(markdown) > schema.MAX_MARKDOWN_BYTES:
                raise ValueError('The Markdown document exceeds the 4194304-byte limit.')
            source = content_hash(captured)
        else:
            markdown = artifact['markdown']; source = artifact['snapshot']['source_sha256']
        return dict(baseline=name, filename='BRD' + ('-' + name if name else '') + '.md',
                    markdown=markdown.decode('utf-8'), sha256=schema.byte_hash(markdown), source_sha256=source)
    current = schema.capture(project_path, project, run, operators)
    # get() first validates the restore binding and original frozen bytes. Bridge
    # the project on a COPY only, to compare source history in this destination.
    before = copy.deepcopy(artifact['snapshot']['view']); before['project'] = project
    result = compare(before, current['view'])
    return dict(result, baseline=name, available=True, receipt_sha256=artifact['receipt_sha256'],
                source_project=artifact['snapshot']['project'])


def web_action(root, project_path, project, request, authority_config, runner, guarded_write):
    from requirement_http import owner_context
    from export_requirements import parse_json
    from http_authority import OperationJournal, journal_path, operation_hash, principal_key, stamp_write
    if request.get('args') != ['baseline']:
        raise ValueError('Unsupported baseline action.')
    attachment = (request.get('attachments') or {}).get('payload')
    if not isinstance(attachment, dict) or not isinstance(attachment.get('text'), str):
        raise ValueError('A baseline cut needs its attached content.')
    body = checked_body(parse_json(attachment['text']))
    operation = identifier(request.get('operation_id'))
    def identity_check():
        context = owner_context(project_path, project, request, authority_config)
        # B1's shared editor also accepts coordinators and granted agents. A
        # baseline cut remains the current human owner's action, on every replay.
        if context.role != 'owner' or context.agent_id is not None:
            raise ValueError('Baseline cuts need the current signed-in project owner')
        return context
    identity_check()
    def effect():
        context = identity_check()
        try:
            answer = store.cut(project_path, context, body['name'], operation, runner,
                datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
        except OSError:
            raise ValueError(store.REPAIR) from None
        return dict(returncode=0, stdout=json.dumps(answer, ensure_ascii=False) + '\n', stderr='')
    path = journal_path(project_path); journal = OperationJournal(path)
    prior = journal.lookup(operation)
    options = dict(authority_config=authority_config, require_authority=True, runner=runner,
                   account_identity=True, identity_check=identity_check)
    answer = guarded_write(root, request, path, effect, **options)
    account_hash = lambda built: operation_hash(built, True, account_identity=True)
    account_key = lambda built: principal_key(built, True, account_identity=True)
    def matches(entry, built):
        return (entry is not None and entry.get('state') in ('unknown', 'in_progress')
                and not journal.expired(entry) and entry.get('principal') == account_key(built)
                and entry.get('request_hash') == account_hash(built))
    if answer.get('returncode') != 124 or not matches(prior, request):
        return answer
    def reconcile():
        context = identity_check()
        if not matches(journal.lookup(operation), request):
            raise ValueError('Baseline recovery identity changed; ask the host operator to reconcile it.')
        store.recoverable(project_path, context, body['name'], operation)
        completed = stamp_write(effect())
        journal._actor = request['actor']; journal._route = request.get('route')
        journal.complete(operation, completed, account_hash(request), account_key(request))
        return completed
    # Unknown outcomes stay unknown in the generic journal. A separate bounded
    # recovery identity can finish only the exact already captured inner cut.
    recovery = dict(request, operation_id='baseline-recover-' + content_hash({
        'original': operation, 'request_sha256': account_hash(request)})[:40],
        route='requirements-baseline-recovery')
    for _ in range(64):
        entry = journal.lookup(recovery['operation_id'])
        if entry is None or entry.get('state') not in ('unknown', 'in_progress'):
            return guarded_write(root, recovery, path, reconcile, **options)
        if not matches(entry, recovery):
            return answer
        recovery['operation_id'] = 'baseline-recover-' + content_hash({
            'original': operation, 'previous': recovery['operation_id'],
            'request_sha256': account_hash(request)})[:40]
    return answer
