#!/usr/bin/env python3
"""Reproduce (or falsify) the six round-3 P1 findings against an HTTP checkout.

Point it at a source tree and it starts a disposable loopback service per finding and
records what the real HTTP surface does. The canonical binding is
``tools/strict_canonical_endpoint.py``, which imports the checkout's own canonical
validators, so the same probe reports ``closed: false`` on the pre-fix revision and
``closed: true`` on the fixed one:

    python tools/http_rev3_probe.py --code-dir <checkout> --label rev2

Findings: 1 canonical-protocol, 2 canonical-lost-result, 3 canonical-revocation,
4 live-membership, 5 idempotency-atomicity, 6 attachment-loss.
"""
import sys
if sys.version_info < (3, 10):
    # Before every other import, and in syntax Python 3.6 reads: an older interpreter failed in
    # an import further down, with a traceback that hid the cause (kittrial-5bb.191).
    sys.stderr.write('http_rev3_probe.py needs Python 3.10 or newer and was started with Python %d.%d.%d (%s). '
                     'Nothing was carried out. Run it with Python 3.10 or newer.\n'
                     % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
    sys.exit(2)
import argparse
import base64
import http.client
import json
import os
import secrets
import shutil
import threading
from pathlib import Path

ADMIN = 'root-admin'
ADMIN_PASSWORD = 'correct-horse-battery-staple'
COMMIT = 'a' * 40
BASE = 'b' * 40
BUNDLE = 'c' * 64
STRICT = str(Path(__file__).resolve().parent / 'strict_canonical_endpoint.py')


class Harness:
    """One disposable service with an explicit backend factory."""

    def __init__(self, tmp, module, service_class, backend_factory):
        self.tmp = Path(tmp)
        self.module = module
        self.service_class = service_class
        self.store = module.Store(self.tmp / 'state.json')
        module.Service.bootstrap_superuser(self.store, ADMIN, ADMIN_PASSWORD)
        self.service = service_class(self.store)
        self.backend = backend_factory(self)
        self.counter = 0
        self._start()

    def _start(self):
        self.httpd = self.module.create_server(self.service, self.backend, host='127.0.0.1',
                                               port=0, trusted_proxies=('127.0.0.1',))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def restart(self, service, backend):
        self._stop()
        self.service, self.backend, self.store = service, backend, service.store
        self._start()

    def _stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=5)
        except Exception:  # noqa: BLE001 - cleanup is best effort
            pass

    def close(self):
        self._stop()

    # -- fixtures --------------------------------------------------------------
    def name(self, stem):
        self.counter += 1
        return '%s-%s-%d' % (stem, secrets.token_hex(4), self.counter)

    def request(self, method, path, body=None, token=None, key=None, headers=None):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        request_headers = {}
        if body is not None:
            request_headers['Content-Type'] = 'application/json'
        if token:
            request_headers['Authorization'] = 'Bearer ' + token
        if key:
            request_headers['Idempotency-Key'] = key
        request_headers.update(headers or {})
        payload = None if body is None else (body if isinstance(body, str) else json.dumps(body))
        try:
            client.request(method, path, body=payload, headers=request_headers)
            response = client.getresponse()
            status, raw = response.status, response.read()
        finally:
            client.close()
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        return {'status': status, 'data': data}

    def login(self, username, password):
        response = self.request('POST', '/v1/sessions', {'username': username,
                                                         'password': password})
        assert response['status'] == 201, response
        return response['data']['session']['token']

    def admin_token(self):
        return self.login(ADMIN, ADMIN_PASSWORD)

    def make_user(self, username, password):
        admin = self.admin_token()
        created = self.request('POST', '/v1/accounts', {'username': username}, token=admin)
        assert created['status'] == 201, created
        user_id = created['data']['id']
        changed = self.request('POST', '/v1/accounts/%s/password' % user_id,
                               {'new_password': password}, token=admin)
        assert changed['status'] == 200, changed
        return user_id, self.login(username, password)

    def make_project(self, token, name, project_id=None):
        body = {'name': name}
        if project_id:
            body['project_id'] = project_id
        response = self.request('POST', '/v1/projects', body, token=token)
        assert response['status'] == 201, response
        return response['data']['id']

    def issue(self, token, project, **body):
        response = self.request('POST', '/v1/projects/%s/worker-credentials' % project,
                                body or {'label': 'worker'}, token=token)
        assert response['status'] == 201, response
        return response['data']['credential']

    def canonical_state(self):
        path = self.tmp / 'canonical' / 'canonical.json'
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding='utf-8'))

    def canonical_tasks(self):
        return self.canonical_state().get('rows', [])


def endpoint_backend(harness):
    return harness.module.EndpointBackend(sys.executable, STRICT,
                                          str(harness.tmp / 'canonical'),
                                          service=harness.service)


# ------------------------------------------------------------------ 1. protocol
def probe_canonical_protocol(harness):
    """Every call runs through the strict endpoint, i.e. the real canonical rules."""
    _, alex = harness.make_user(harness.name('alex'), 'alex-password-1')
    project = harness.make_project(alex, harness.name('Alpha'))
    with_description = harness.request(
        'POST', '/v1/projects/%s/tasks' % project,
        {'title': 'described task', 'description': 'evidence body'}, token=alex)
    plain = harness.request('POST', '/v1/projects/%s/tasks' % project,
                            {'title': 'plain task'}, token=alex)
    task_id = (plain['data'] or {}).get('id')
    review = {'status': None, 'data': None}
    if task_id:
        harness.request('POST', '/v1/projects/%s/tasks/%s/claim' % (project, task_id),
                        {}, token=alex)
        review = harness.request(
            'POST', '/v1/projects/%s/tasks/%s/reviews' % (project, task_id),
            {'operation': 'contribute', 'schema_version': 1,
             'operation_id': 'op-' + secrets.token_hex(6), 'previous': None,
             'repository': 'https://example.invalid/repo.git', 'commit': COMMIT,
             'base_commit': BASE, 'summary': 'delivered', 'supersedes': None,
             'delivery': {'kind': 'bundle', 'path': 'koopa:/tmp/x.bundle',
                          'sha256': BUNDLE}}, token=alex)
    history = harness.request('GET', '/v1/projects/%s/tasks/%s/history?limit=50'
                              % (project, task_id or 'missing'), token=alex)
    observations = {
        'create_with_description': with_description['status'],
        'create_description_error': ((with_description['data'] or {}).get('error') or
                                     {}).get('message'),
        'create_plain': plain['status'],
        'contribute': review['status'],
        'contribute_error': ((review['data'] or {}).get('error') or {}).get('message'),
        'history_limit_50': history['status'],
        'history_error': ((history['data'] or {}).get('error') or {}).get('message'),
    }
    return {'observations': observations,
            'closed': (with_description['status'] == 201 and plain['status'] == 201 and
                       review['status'] == 201 and history['status'] == 200)}


def probe_canonical_full_flow(harness):
    """create -> claim -> history cursor -> checkpoint -> contribute -> approve."""
    _, alex = harness.make_user(harness.name('alex'), 'alex-password-1')
    project = harness.make_project(alex, harness.name('Alpha'))
    created = harness.request('POST', '/v1/projects/%s/tasks' % project,
                              {'title': 'flow task'}, token=alex)
    task_id = (created['data'] or {}).get('id')
    steps = {'create': created['status']}
    if not task_id:
        return {'observations': steps, 'closed': False}
    steps['claim'] = harness.request('POST', '/v1/projects/%s/tasks/%s/claim'
                                     % (project, task_id), {}, token=alex)['status']
    history = harness.request('GET', '/v1/projects/%s/tasks/%s/history'
                              % (project, task_id), token=alex)
    steps['history'] = history['status']
    cursor = (history['data'] or {}).get('activity_cursor')
    checkpoint = harness.request(
        'POST', '/v1/projects/%s/tasks/%s/checkpoints' % (project, task_id),
        {'schema_version': 1, 'previous': None, 'activity_cursor': cursor,
         'source_commit': COMMIT, 'branch': 'contrib/probe',
         'intent': 'prove the canonical checkpoint contract',
         'acceptance': 'accepted by the real validator',
         'summary': 'checkpoint via the real endpoint',
         'next_action': 'contribute', 'open_items': [], 'resolved': []}, token=alex)
    steps['checkpoint'] = checkpoint['status']
    steps['checkpoint_error'] = ((checkpoint['data'] or {}).get('error') or {}).get('message')
    review = harness.request(
        'POST', '/v1/projects/%s/tasks/%s/reviews' % (project, task_id),
        {'operation': 'contribute', 'schema_version': 1,
         'operation_id': 'op-' + secrets.token_hex(6), 'previous': None,
         'repository': 'https://example.invalid/repo.git', 'commit': COMMIT,
         'base_commit': BASE, 'summary': 'delivered', 'supersedes': None,
         'delivery': {'kind': 'bundle', 'path': 'koopa:/tmp/x.bundle',
                      'sha256': BUNDLE}}, token=alex)
    steps['contribute'] = review['status']
    steps['contribute_error'] = ((review['data'] or {}).get('error') or {}).get('message')
    contribution_id = ((review['data'] or {}).get('comment_id') or
                       ((review['data'] or {}).get('contribution') or {}).get('comment_id'))
    steps['contribution_id'] = contribution_id
    if contribution_id:
        approve = harness.request(
            'POST', '/v1/projects/%s/tasks/%s/reviews' % (project, task_id),
            {'operation': 'approve', 'schema_version': 1,
             'operation_id': 'op-' + secrets.token_hex(6), 'previous': contribution_id,
             'contribution': contribution_id, 'summary': 'accepted'}, token=alex)
        steps['approve'] = approve['status']
        steps['approve_error'] = ((approve['data'] or {}).get('error') or {}).get('message')
    return {'observations': steps,
            'closed': (steps.get('create') == 201 and steps.get('claim') == 200 and
                       steps.get('history') == 200 and steps.get('checkpoint') == 201 and
                       steps.get('contribute') == 201 and steps.get('approve') == 201)}


# ------------------------------------------------------------- 2. lost result
def probe_lost_result(harness):
    _, alex = harness.make_user(harness.name('alex'), 'alex-password-1')
    project = harness.make_project(alex, harness.name('Alpha'))
    backend = harness.backend
    original = backend._endpoint

    def lossy(action, project_id, actor, args, attachments=None, **kwargs):
        original(action, project_id, actor, args, attachments, **kwargs)
        raise harness.module.UncertainOutcome('response lost after canonical commit')

    backend._endpoint = lossy
    first = harness.request('POST', '/v1/projects/%s/tasks' % project,
                            {'title': 'lost response'}, token=alex, key='lost-key-0001')
    backend._endpoint = original
    # Rebuild the service and backend from disk: the receipt is durable.
    store = harness.module.Store(harness.tmp / 'state.json')
    service = harness.module.Service(store)
    harness.restart(service, harness.module.EndpointBackend(sys.executable, STRICT,
                                                            str(harness.tmp / 'canonical'),
                                                            service=service))
    retry = harness.request('POST', '/v1/projects/%s/tasks' % project,
                            {'title': 'lost response'}, token=alex, key='lost-key-0001')
    tasks = harness.canonical_tasks()
    return {'observations': {'first_status': first['status'], 'retry_status': retry['status'],
                             'canonical_tasks': len(tasks)},
            'closed': len(tasks) == 1 and retry['status'] in (201, 200)}


# ------------------------------------------------------------ 3. revocation race
class PausingEndpoint:
    """Pause the endpoint between its authorization check and the canonical call.

    ``_run`` is patched on the backend instance, not wrapped, because ``invoke`` is
    an internal caller and would otherwise bind the wrapped object's ``self``.
    """

    def __init__(self, inner):
        self.inner = inner
        self.entered = threading.Event()
        self.proceed = threading.Event()
        self.pause = False
        self._original = inner._run
        inner._run = self._paused

    def _paused(self, *args, **kwargs):
        if self.pause:
            self.entered.set()
            self.proceed.wait(timeout=20)
        return self._original(*args, **kwargs)


def probe_revocation_race(harness):
    _, alex = harness.make_user(harness.name('alex'), 'alex-password-1')
    project = harness.make_project(alex, harness.name('Alpha'))
    credential = harness.issue(alex, project, label='worker',
                               scopes=['tasks', 'checkpoints', 'reviews', 'feedback'])
    secret, credential_id = credential['secret'], credential['id']
    pausing = PausingEndpoint(harness.backend)
    pausing.pause = True
    pausing.entered.clear()
    pausing.proceed.clear()
    outcome = {}

    def create():
        outcome['response'] = harness.request('POST', '/v1/projects/%s/tasks' % project,
                                              {'title': 'raced'}, token=secret,
                                              key='race-key-0001')

    worker = threading.Thread(target=create)
    worker.start()
    reached = pausing.entered.wait(timeout=20)
    revoke = harness.request('POST', '/v1/projects/%s/worker-credentials/%s/revoke'
                             % (project, credential_id), token=alex)
    pausing.proceed.set()
    worker.join(timeout=30)
    pausing.pause = False
    tasks = harness.canonical_tasks()
    return {'observations': {'reached_seam': reached, 'revoke': revoke['status'],
                             'create_after_revoke':
                                 (outcome.get('response') or {}).get('status'),
                             'canonical_tasks': len(tasks)},
            'closed': bool(reached) and revoke['status'] == 204 and len(tasks) == 0}


# ----------------------------------------------------------- 4. live membership
def probe_live_membership(harness):
    admin = harness.admin_token()
    alex_id, alex = harness.make_user(harness.name('alex'), 'alex-password-1')
    blair_id, _ = harness.make_user(harness.name('blair'), 'blair-password-1')
    project = harness.make_project(alex, harness.name('Alpha'))
    credential = harness.issue(alex, project, label='worker',
                               scopes=['tasks', 'checkpoints', 'reviews', 'feedback'])
    secret = credential['secret']
    harness.request('PUT', '/v1/projects/%s/members/%s' % (project, blair_id),
                    {'role': 'owner'}, token=admin)
    demoted = harness.request('PUT', '/v1/projects/%s/members/%s' % (project, alex_id),
                              {'role': 'viewer'}, token=admin)
    write_after_demotion = harness.request('POST', '/v1/projects/%s/tasks' % project,
                                           {'title': 'after demotion'}, token=secret)
    read_after_demotion = harness.request('GET', '/v1/projects/%s/tasks' % project,
                                          token=secret)
    harness.request('PUT', '/v1/projects/%s/members/%s' % (project, alex_id),
                    {'role': 'contributor'}, token=admin)
    removed = harness.request('DELETE', '/v1/projects/%s/members/%s' % (project, alex_id),
                              token=admin)
    write_after_removal = harness.request('POST', '/v1/projects/%s/tasks' % project,
                                          {'title': 'after removal'}, token=secret)
    read_after_removal = harness.request('GET', '/v1/projects/%s/tasks' % project,
                                         token=secret)
    return {'observations': {'demote': demoted['status'],
                             'write_after_demotion': write_after_demotion['status'],
                             'read_after_demotion': read_after_demotion['status'],
                             'remove': removed['status'],
                             'write_after_removal': write_after_removal['status'],
                             'read_after_removal': read_after_removal['status']},
            'closed': (write_after_demotion['status'] == 403 and
                       write_after_removal['status'] in (403, 404) and
                       read_after_removal['status'] in (403, 404))}


# ------------------------------------------------------- 5. idempotency atomicity
def probe_idempotency_atomicity(harness):
    _, alex = harness.make_user(harness.name('alex'), 'alex-password-1')
    project = harness.make_project(alex, harness.name('Alpha'))
    barrier = threading.Barrier(2)
    service = harness.service
    original = service.issue_credential

    def gated(*args, **kwargs):
        try:
            barrier.wait(timeout=3)
        except Exception:  # noqa: BLE001 - only one caller reaches here after the fix
            pass
        return original(*args, **kwargs)

    service.issue_credential = gated
    results = {}
    start = threading.Barrier(3)

    def issue(number):
        start.wait(timeout=5)
        results[number] = harness.request(
            'POST', '/v1/projects/%s/worker-credentials' % project, {'label': 'same'},
            token=alex, key='concurrent-key-0001')

    threads = [threading.Thread(target=issue, args=(n,)) for n in (0, 1)]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=30)
    service.issue_credential = original
    statuses = sorted((results.get(n) or {}).get('status') for n in (0, 1))
    credentials = len(harness.store.state['credentials'])

    admin = harness.admin_token()
    first = harness.request('POST', '/v1/projects',
                            {'name': 'Created one', 'project_id': 'proj-one'},
                            token=admin, key='project-key-0001')
    changed = harness.request('POST', '/v1/projects',
                              {'name': 'Created two', 'project_id': 'proj-two'},
                              token=admin, key='project-key-0001')
    return {'observations': {'concurrent_statuses': statuses,
                             'credentials_created': credentials,
                             'project_create': first['status'],
                             'project_create_changed_body': changed['status']},
            'closed': credentials == 1 and statuses.count(201) <= 1 and
                      changed['status'] == 409}


# ------------------------------------------------------------- 6. attachment loss
def probe_attachment_loss(harness):
    _, alex = harness.make_user(harness.name('alex'), 'alex-password-1')
    project = harness.make_project(alex, harness.name('Alpha'))
    marker = 'attachment-probe-payload-9f3a'
    content = base64.b64encode(marker.encode('ascii')).decode('ascii')
    response = harness.request(
        'POST', '/v1/projects/%s/tasks' % project,
        {'title': 'attachment probe', 'attachments': [
            {'name': 'note.txt', 'media_type': 'text/plain', 'content_base64': content}]},
        token=alex)
    stored = json.dumps(harness.canonical_state())
    return {'observations': {'status': response['status'],
                             'canonical_tasks': len(harness.canonical_tasks()),
                             'canonical_mentions_content': marker in stored or content in stored},
            'closed': response['status'] == 501}


PROBES = (
    ('canonical-protocol', probe_canonical_protocol, True),
    ('canonical-full-flow', probe_canonical_full_flow, True),
    ('canonical-lost-result', probe_lost_result, True),
    ('canonical-revocation', probe_revocation_race, True),
    ('live-membership', probe_live_membership, False),
    ('idempotency-atomicity', probe_idempotency_atomicity, False),
    ('attachment-loss', probe_attachment_loss, True),
)


def main():
    parser = argparse.ArgumentParser(description='Round-3 HTTP P1 probe')
    parser.add_argument('--code-dir', required=True)
    parser.add_argument('--label', default='checkout')
    parser.add_argument('--out', help='write the JSON report here')
    arguments = parser.parse_args()
    code_dir = Path(arguments.code_dir).resolve()
    os.environ['STRICT_ENDPOINT_CODE_DIR'] = str(code_dir)
    sys.path.insert(0, str(code_dir))
    module = __import__('http_service')
    service_class = __import__('http_auth').Service
    tmp_root = Path(os.environ.get('ORCHESTRA_TEST_TMP',
                                   str(Path.cwd() / '.runtime' / 'probe')))
    report = {'label': arguments.label, 'code_dir': str(code_dir), 'findings': {}}
    for name, probe, needs_endpoint in PROBES:
        tmp = tmp_root / ('rev3-%s-%s' % (arguments.label, secrets.token_hex(4)))
        tmp.mkdir(parents=True, exist_ok=True)
        harness = None
        try:
            if needs_endpoint:
                harness = Harness(tmp, module, service_class, endpoint_backend)
            else:
                harness = Harness(tmp, module, service_class,
                                  lambda h: module.InProcessBackend(h.service))
            report['findings'][name] = probe(harness)
        except Exception as error:  # noqa: BLE001 - a probe failure is itself evidence
            report['findings'][name] = {'error': '%s: %s' % (type(error).__name__, error),
                                        'closed': False}
        finally:
            if harness is not None:
                harness.close()
            shutil.rmtree(tmp, ignore_errors=True)
    text = json.dumps(report, indent=2)
    if arguments.out:
        Path(arguments.out).write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
