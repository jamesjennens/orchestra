#!/usr/bin/env python3
"""Reproduce (or falsify) the six P1 findings against an HTTP service checkout.

This is a generic diagnostic for the authenticated office HTTP service, not a
project-specific script. Point it at a source tree and it starts a disposable
loopback service and probes each finding, printing a JSON report. Run it against
the reviewed revision to reproduce a finding, and against a fixed revision to show
it is closed.

    python tools/http_security_probe.py --code-dir <checkout>
"""
import sys
if sys.version_info < (3, 10):
    # Before every other import, and in syntax Python 3.6 reads: an older interpreter failed in
    # an import further down, with a traceback that hid the cause (kittrial-5bb.191).
    sys.stderr.write('http_security_probe.py needs Python 3.10 or newer and was started with Python %d.%d.%d (%s). '
                     'Nothing was carried out. Run it with Python 3.10 or newer.\n'
                     % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
    sys.exit(2)
import argparse
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


class PausingBackend:
    """Pause one backend route at its call boundary, before any lock is taken."""

    def __init__(self, inner):
        self.inner = inner
        self.entered = threading.Event()
        self.proceed = threading.Event()
        self.route = None

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def invoke(self, route, *args, **kwargs):
        if route == self.route:
            self.entered.set()
            if not self.proceed.wait(timeout=15):
                raise RuntimeError('probe was never resumed')
        return self.inner.invoke(route, *args, **kwargs)


class Probe:
    def __init__(self, code_dir, tmp):
        sys.path.insert(0, str(code_dir))
        from http_auth import Service, Store
        import http_service
        self.http_service = http_service
        self.store = Store(tmp / 'state.json')
        Service.bootstrap_superuser(self.store, ADMIN, ADMIN_PASSWORD)
        self.service = Service(self.store)
        self.unique = secrets.token_hex(4)
        self._counter = 0
        self.inner = http_service.InProcessBackend(self.service)
        self.backend = PausingBackend(self.inner)
        try:
            self.httpd = http_service.create_server(
                self.service, self.backend, host='127.0.0.1', port=0,
                trusted_proxies=('127.0.0.1',))
        except TypeError:
            # The reviewed revision predates configurable trusted proxies.
            self.httpd = http_service.create_server(
                self.service, self.backend, host='127.0.0.1', port=0, trust_proxy=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, token=None, key=None, headers=None):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=20)
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
            status, header_list, data = response.status, response.getheaders(), response.read()
        finally:
            client.close()
        try:
            parsed = json.loads(data) if data else None
        except ValueError:
            parsed = None
        cookies = [value for name, value in header_list if name.lower() == 'set-cookie']
        return {'status': status, 'data': parsed, 'cookies': cookies}

    # -- fixtures --------------------------------------------------------------
    def name(self, stem):
        self._counter += 1
        return '%s-%s-%d' % (stem, self.unique, self._counter)

    def login(self, username, password):
        response = self.request('POST', '/v1/sessions',
                                {'username': username, 'password': password})
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

    # -- probes ----------------------------------------------------------------
    def probe_canonical_backend(self):
        endpoint = self.http_service.EndpointBackend
        needed = ('invoke', 'list_tasks', 'get_task', 'task_history', 'list_feedback')
        missing = [name for name in needed if not hasattr(endpoint, name)]
        return {'missing_endpoint_methods': missing,
                'selectable_backend_option': hasattr(self.http_service, 'build_backend')}

    def probe_credential_authority(self):
        admin = self.admin_token()
        _, alex = self.make_user(self.name('alex'), 'alex-password-1')
        project = self.make_project(alex, self.name('Alpha'))
        credential = self.issue(admin, project, label='read-only', scopes=['read'])
        secret = credential['secret']
        account = self.request('POST', '/v1/accounts', {'username': self.name('mallory')},
                               token=secret)
        task = self.request('POST', '/v1/projects/%s/tasks' % project,
                            {'title': 'sneaky'}, token=secret)
        return {'accounts_create_as_read_only_credential': account['status'],
                'task_create_as_read_only_credential': task['status']}

    def probe_idempotency_target(self):
        admin = self.admin_token()
        alex_id, _ = self.make_user(self.name('alex'), 'alex-password-1')
        blair_id, _ = self.make_user(self.name('blair'), 'blair-password-1')
        first = self.request('POST', '/v1/accounts/%s/disable' % alex_id, None,
                             token=admin, key='disable-key-0001')
        second = self.request('POST', '/v1/accounts/%s/disable' % blair_id, None,
                              token=admin, key='disable-key-0001')
        return {'first_disable': first['status'], 'second_disable': second['status'],
                'second_account_disabled': bool(self.store.state['users'][blair_id]['disabled'])}

    def probe_final_owner(self):
        admin = self.admin_token()
        alex_id, alex = self.make_user(self.name('alex'), 'alex-password-1')
        project = self.make_project(alex, self.name('Alpha'))
        demote = self.request('PUT', '/v1/projects/%s/members/%s' % (project, alex_id),
                              {'role': 'viewer'}, token=admin)
        owners = sum(1 for role in self.store.state['memberships'][project].values()
                     if role == 'owner')
        disable = self.request('POST', '/v1/accounts/%s/disable' % alex_id, None, token=admin)
        return {'superuser_demote_final_owner': demote['status'], 'owners_after_demote': owners,
                'superuser_disable_final_owner': disable['status']}

    def probe_revocation_transaction(self):
        admin = self.admin_token()
        _, alex = self.make_user(self.name('alex'), 'alex-password-1')
        project = self.make_project(alex, self.name('Alpha'))
        credential = self.issue(alex, project, label='worker',
                                scopes=['tasks', 'checkpoints', 'reviews', 'feedback'])
        secret, credential_id = credential['secret'], credential['id']
        self.backend.route = 'tasks.create'
        self.backend.entered.clear()
        self.backend.proceed.clear()
        outcome = {}

        def create():
            outcome['response'] = self.request(
                'POST', '/v1/projects/%s/tasks' % project,
                {'title': 'raced task'}, token=secret, key='race-key-0001')

        worker = threading.Thread(target=create)
        worker.start()
        reached = self.backend.entered.wait(timeout=15)
        revoked = self.request('POST', '/v1/projects/%s/worker-credentials/%s/revoke'
                               % (project, credential_id), token=alex)
        self.backend.proceed.set()
        worker.join(timeout=20)
        listed = self.request('GET', '/v1/projects/%s/tasks' % project, token=alex)
        return {'create_reached_seam': reached, 'revoke_status': revoked['status'],
                'create_after_revoke': outcome.get('response', {}).get('status'),
                'tasks_after': listed['data']['total'] if listed['data'] else None}

    def probe_browser_deployment(self):
        admin = self.admin_token()
        alex_name = self.name('alex')
        _, alex = self.make_user(alex_name, 'alex-password-1')
        cookie_login = self.request('POST', '/v1/sessions',
                                    {'username': alex_name, 'password': 'alex-password-1'},
                                    headers={'X-Forwarded-Proto': 'https'})
        secure = any('Secure' in cookie for cookie in cookie_login['cookies'])

        blair_id, blair = self.make_user(self.name('blair'), 'blair-password-1')
        project = self.make_project(alex, self.name('Alpha'))
        self.request('PUT', '/v1/projects/%s/members/%s' % (project, blair_id),
                     {'role': 'contributor'}, token=alex)
        task = self.request('POST', '/v1/projects/%s/tasks' % project, {'title': 'delivery'},
                            token=alex)
        task_id = task['data']['id']
        self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (project, task_id),
                     {'operation': 'contribute', 'commit': COMMIT, 'base_commit': BASE,
                      'bundle_sha256': BUNDLE, 'summary': 'delivered'}, token=blair)
        approve = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (project, task_id),
                               {'operation': 'approve', 'summary': 'lgtm'}, token=blair)

        named = self.request('POST', '/v1/projects',
                             {'name': self.name('Beta'), 'project_id': 'alpha-5bb.19'},
                             token=alex)
        dotted = self.request('GET', '/v1/projects/alpha-5bb.19', token=alex)
        return {'secure_cookie_behind_trusted_proxy': secure,
                'contributor_approve_status': approve['status'],
                'create_dotted_project_id': named['status'],
                'get_dotted_project_id': dotted['status']}


def main():
    parser = argparse.ArgumentParser(description='Probe the six HTTP P1 findings')
    parser.add_argument('--code-dir', default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    # Keep disposable state inside a writable tree; some sandboxes deny the OS temp dir.
    tmp_root = Path(os.environ.get('ORCHESTRA_TEST_TMP',
                                   str(Path(args.code_dir) / '.runtime' / 'probe-tmp')))
    tmp = tmp_root / ('run-' + secrets.token_hex(4))
    tmp.mkdir(parents=True, exist_ok=True)
    report = {}
    probe = None
    try:
        probe = Probe(Path(args.code_dir), tmp)
        for name in ('canonical_backend', 'credential_authority', 'idempotency_target',
                     'final_owner', 'revocation_transaction', 'browser_deployment'):
            report[name] = getattr(probe, 'probe_' + name)()
    finally:
        if probe is not None:
            probe.close()
        shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps({'code_dir': str(args.code_dir), 'findings': report}, indent=2))


if __name__ == '__main__':
    main()
