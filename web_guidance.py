"""Service-only guidance and member onboarding over existing project files.

The endpoint holds the project lock. Reads and guarded writes also hold the live
HTTP authority lock. No body-supplied actor or host path reaches the writers.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re

import guidance
import http_authority as authority

CAPABILITIES = {'read': authority.CAP_READ, 'ack': authority.CAP_READ,
                'set': authority.CAP_COORDINATE, 'clear': authority.CAP_COORDINATE,
                'status': authority.CAP_COORDINATE, 'onboarding': authority.CAP_READ,
                'coordinate-check': authority.CAP_COORDINATE, 'onboarding-edit': authority.CAP_COORDINATE}
READ_OPERATIONS = frozenset(('read', 'status', 'onboarding', 'coordinate-check', 'onboarding-edit'))
FIELDS = {'read': {'operation'}, 'ack': {'operation', 'version'},
          'set': {'operation'}, 'clear': {'operation'},
          'status': {'operation', 'offset', 'limit'}, 'onboarding': {'operation'},
          'coordinate-check': {'operation'}, 'onboarding-edit': {'operation'}}


def validate(body, attachments=None):
    if not isinstance(body, dict) or not isinstance(body.get('operation'), str):
        raise ValueError('Supply one guidance operation')
    operation = body['operation']
    fields = FIELDS.get(operation)
    if fields is None or set(body) != fields:
        raise ValueError('Invalid fields for guidance operation')
    attachments = {} if attachments is None else attachments
    if not isinstance(attachments, dict) or set(attachments) != ({'text'} if operation == 'set' else set()):
        raise ValueError('Only guidance set carries a text attachment')
    if operation == 'set':
        attachment = attachments['text']
        if not isinstance(attachment, dict) or set(attachment) != {'text', 'flag'} or attachment['flag'] != '--file':
            raise ValueError('Guidance set needs one literal text attachment')
        guidance.validate_text(attachment['text'])
    if operation == 'ack' and (not isinstance(body['version'], str) or not guidance.VERSION.fullmatch(body['version'])):
        raise ValueError('Name the exact guidance version you read')
    if operation == 'status':
        if type(body['offset']) is not int or not 0 <= body['offset'] <= guidance.ACK_LIMIT:
            raise ValueError('Guidance status offset must be 0..500')
        if type(body['limit']) is not int or not 1 <= body['limit'] <= 100:
            raise ValueError('Guidance status limit must be 1..100')
    return operation


def read_request(request):
    """Validate the closed envelope before choosing a project lock mode."""
    args = request.get('args')
    if not isinstance(args, list) or len(args) != 1 or not isinstance(args[0], str):
        raise ValueError('Supply one canonical guidance JSON body')
    import record_json
    return validate(record_json.loads(args[0]), request.get('attachments')) in READ_OPERATIONS


@contextmanager
def _authority_read_lock(path):
    """Share POSIX authority reads; retain the existing Windows lock fallback."""
    if os.name == 'nt':
        with authority.file_lock(path):
            yield
        return
    import fcntl
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        yield


def require_authority(request, authority_config, capability, project):
    """Current exact project/capability/identity; caller holds authority lock."""
    descriptor = request.get('authority')
    if authority_config is None or not isinstance(descriptor, dict):
        raise authority.deny(401, 'unauthenticated', 'Guidance is available only to the authenticated web service')
    if (request.get('project') != project or descriptor.get('project') != project
            or descriptor.get('capability') != capability):
        raise authority.deny(403, 'forbidden', 'Guidance authority must name this project and operation capability')
    descriptor = {k: v for k, v in descriptor.items() if k not in ('store', 'lock')}
    state = authority.read_state(authority_config.store)
    authority.decide(state, descriptor)
    if descriptor.get('via') == 'session':
        record = (state.get('sessions') or {}).get(descriptor.get('session_hash')) or {}
        expected = record.get('user_id')
    else:
        record = (state.get('credentials') or {}).get(descriptor.get('credential_id')) or {}
        expected = record.get('agent_id') or record.get('actor') or record.get('user_id')
    if record.get('user_id') != descriptor.get('user_id') or not expected or request.get('actor') != expected:
        raise authority.deny(403, 'forbidden', 'Guidance uses only the authenticated account, agent or worker identity')
    project_record = (state.get('projects') or {}).get(project) or {}
    if project_record.get('archived'):
        raise authority.deny(409, 'conflict', 'Guidance and onboarding are unavailable for an archived project')
    return state


def _project_actors(path, project, state):
    """Current web reach and registered host actors; never another project's roster."""
    import sessions
    names = set(sessions.registered_actors(path))
    members = (state.get('memberships') or {}).get(project) or {}
    names.update(members)
    names.update(name for name, row in (state.get('users') or {}).items()
                 if isinstance(row, dict) and row.get('superuser'))
    names.update(name for name, row in (state.get('agents') or {}).items()
                 if isinstance(row, dict) and project in (row.get('projects') or []) and row.get('owner') in names)
    names.update(row.get('actor') or row.get('user_id') for row in (state.get('credentials') or {}).values()
                 if isinstance(row, dict) and not row.get('agent_id') and row.get('project_id') == project
                 and row.get('user_id') in names)
    return {name for name in names if isinstance(name, str)}


def status_page(path, project, actor, state, offset, limit, operators=None):
    value = guidance._status_snapshot(path, actor, operators)
    allowed = _project_actors(path, project, state)
    rows = [row for row in value.get('acknowledged', []) if row['actor'] in allowed]
    total = len(rows)
    page = rows[offset:offset + limit]
    value['acknowledged'] = page
    value['acknowledged_total'] = total
    for field in ('up_to_date', 'behind', 'stale'):
        value[field] = [name for name in value.get(field, []) if name in {r['actor'] for r in page}]
    value.update(offset=offset, limit=limit, next_offset=offset + limit if offset + limit < total else None,
                 roster_scope='Current project identities and registered host actors only',
                 retention='At most 500 latest actor acknowledgements; setting drops versions other than current '
                           'and previous, compaction drops stale entries, and oldest entries may be evicted. '
                           'This is not a complete historical acknowledgement roster.')
    return value


def member_onboarding(path):
    import onboarding
    target = Path(path) / 'ONBOARDING.md'
    if not target.exists() and not target.is_symlink():
        return dict(state='not-set', source=None, text=None, bytes=0, limit=onboarding.PROJECT_LIMIT)
    # A directory is a refusal, never an empty document; the existing safe reader
    # checks symlinks, regular file, byte bound and UTF-8 without exposing host paths.
    document = onboarding.read_document(path, 'ONBOARDING.md', onboarding.PROJECT_LIMIT)
    web, _ = onboarding.split_web(document)
    return dict(state='set', source='web' if web else 'operator', text=document,
                bytes=len(document.encode('utf-8')), limit=onboarding.PROJECT_LIMIT)


class _FileEffect:
    attempted_write = False
    wrote = False

    def before_write(self):
        self.attempted_write = True


def web_action(path, project, request, authority_config, operators=None):
    """Map live authority refusals to the endpoint's stable refusal envelope."""
    try:
        return _web_action(path, project, request, authority_config, operators)
    except authority.AuthorityDenied as denied:
        return dict(returncode=126, stdout='', stderr=denied.message + '\n', authority_status=denied.status)


def _web_action(path, project, request, authority_config, operators=None):
    """Canonical envelope. The endpoint holds its project's coordination lock."""
    args = request.get('args')
    if not isinstance(args, list) or len(args) != 1 or not isinstance(args[0], str):
        raise ValueError('Supply one canonical guidance JSON body')
    import record_json
    body = record_json.loads(args[0])
    operation = validate(body, request.get('attachments'))
    capability = CAPABILITIES[operation]

    def authorize():
        return require_authority(request, authority_config, capability, project)

    if operation in READ_OPERATIONS:
        if authority_config is None:
            authorize()
        with _authority_read_lock(authority_config.lock):
            state = authorize()
            if operation == 'read':
                result = guidance.read(path, ['get'], request['actor'], operators)
            elif operation == 'status':
                result = status_page(path, project, request['actor'], state, body['offset'], body['limit'], operators)
            elif operation == 'onboarding':
                result = member_onboarding(path)
            elif operation == 'onboarding-edit':
                import onboarding
                result = member_onboarding(path)
                document = result['text']
                web, text = onboarding.split_web(document) if document is not None else (False, None)
                result['text'] = text if web else None
                if document is None:
                    result.pop('bytes')
                else:
                    result['header'] = onboarding.WEB_HEADER if web else None
            else:
                # Read-only replay guard: no guidance or onboarding file need be readable.
                result = {'authorized': True, 'actor': request['actor']}
        return dict(returncode=0, stdout=json.dumps(result) + '\n', stderr='')

    operation_id = request.get('operation_id')
    if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', operation_id):
        raise ValueError('A stable guidance operation identity is required')
    # Shape is refused before reservation; live authorization is repeated INSIDE
    # run_guarded even for a completed replay. This callback checks actor binding too.
    tracker = _FileEffect()

    def effect():
        actor = request['actor']
        if operation == 'set':
            result = guidance.write_guidance(path, request['attachments']['text']['text'], actor,
                                             before_write=tracker.before_write)
        elif operation == 'ack':
            result = guidance.acknowledge(path, actor, body['version'], before_write=tracker.before_write)
        else:
            result = guidance.clear(path, actor, before_write=tracker.before_write)
        return dict(returncode=0, stdout=json.dumps(result) + '\n', stderr='')

    return authority.run_guarded(request, authority.journal_path(path), effect, authority_config=authority_config,
                                 require_authority=True, identity_check=authorize, runner=tracker)
