"""HTTP-boundary regression tests for the six P1 findings on kittrial-5bb.19.

Each test maps to one requested change and exercises the real HTTP surface (or the
real subprocess canonical binding), including the negative cases:

1. canonical-backend      -> ``CanonicalEndpointCase`` (real subprocess seam)
2. credential-authority   -> ``CredentialAuthorityCase``
3. idempotency-target     -> ``IdempotencyTargetCase``
4. final-owner            -> ``FinalOwnerCase``
5. revocation-transaction -> ``RevocationRaceCase``, ``StoreDurabilityCase``
6. browser-deployment     -> ``DeploymentContractCase``
"""
import argparse
import http.client
import json
import os
import secrets
import shutil
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from http_auth import HttpError, Service, Store
from http_service import (EndpointBackend, InProcessBackend, build_backend, create_server,
                          MAX_BODY_BYTES)

TMP_ROOT = Path(os.environ.get('ORCHESTRA_TEST_TMP', str(ROOT / '.runtime' / 'test-tmp')))
ADMIN = 'root-admin'
ADMIN_PASSWORD = 'correct-horse-battery-staple'
COMMIT = 'a' * 40
BASE = 'b' * 40
BUNDLE = 'c' * 64
STUB = Path(__file__).resolve().parent / 'canonical_endpoint_stub.py'


def unique_dir(prefix):
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    while True:
        candidate = TMP_ROOT / (prefix + secrets.token_hex(6))
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue


class Response:
    def __init__(self, status, header_list, body):
        self.status = status
        self.header_list = header_list
        self.headers = {}
        for key, value in header_list:
            self.headers.setdefault(key.lower(), value)
        self.body = body
        try:
            self.data = json.loads(body) if body else None
        except ValueError:
            self.data = None

    def set_cookies(self):
        return [value for key, value in self.header_list if key.lower() == 'set-cookie']


class Harness(unittest.TestCase):
    trusted_proxies = ()

    def setUp(self):
        self.tmp = unique_dir('rev-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = Store(self.tmp / 'state.json')
        self.admin_user = Service.bootstrap_superuser(self.store, ADMIN, ADMIN_PASSWORD)
        self.service = Service(self.store)
        self.backend = self.make_backend()
        self._start_server(self.service, self.backend)

    def make_backend(self):
        return InProcessBackend(self.service)

    def _start_server(self, service, backend):
        self.httpd = create_server(service, backend, host='127.0.0.1', port=0,
                                   trusted_proxies=self.trusted_proxies,
                                   max_body=MAX_BODY_BYTES)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _stop_server(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=5)
        except Exception:  # noqa: BLE001 - cleanup must stay idempotent
            pass

    def restart(self, service, backend):
        self._stop_server()
        self.service, self.backend, self.store = service, backend, service.store
        self._start_server(self.service, self.backend)

    def request(self, method, path, body=None, token=None, cookie=None, csrf=None,
                key=None, headers=None, content_type='application/json'):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=20)
        request_headers = {}
        if body is not None:
            request_headers['Content-Type'] = content_type
        if token:
            request_headers['Authorization'] = 'Bearer ' + token
        if cookie:
            request_headers['Cookie'] = cookie
        if csrf:
            request_headers['X-CSRF-Token'] = csrf
        if key:
            request_headers['Idempotency-Key'] = key
        request_headers.update(headers or {})
        if body is None:
            payload = None
        elif isinstance(body, (bytes, str)):
            payload = body
        else:
            payload = json.dumps(body)
        try:
            client.request(method, path, body=payload, headers=request_headers)
            response = client.getresponse()
            status = response.status
            header_list = response.getheaders()
            data = response.read()
        finally:
            client.close()
        return Response(status, header_list, data)

    # -- fixtures --------------------------------------------------------------
    def login(self, username, password):
        response = self.request('POST', '/v1/sessions',
                                {'username': username, 'password': password})
        self.assertEqual(201, response.status, response.data)
        return (response.data['session']['token'], response.data['session']['csrf_token'],
                response.data['user'])

    def admin_token(self):
        return self.login(ADMIN, ADMIN_PASSWORD)[0]

    def create_account(self, admin, username, password):
        response = self.request('POST', '/v1/accounts', {'username': username}, token=admin)
        self.assertEqual(201, response.status, response.data)
        user_id = response.data['id']
        response = self.request('POST', '/v1/accounts/%s/password' % user_id,
                                {'new_password': password}, token=admin)
        self.assertEqual(200, response.status, response.data)
        return user_id

    def create_project(self, token, name, project_id=None):
        body = {'name': name}
        if project_id:
            body['project_id'] = project_id
        response = self.request('POST', '/v1/projects', body, token=token)
        self.assertEqual(201, response.status, response.data)
        return response.data['id']

    def create_task(self, token, project, title, key=None):
        return self.request('POST', '/v1/projects/%s/tasks' % project, {'title': title},
                            token=token, key=key)

    def issue_credential(self, token, project, **body):
        response = self.request('POST', '/v1/projects/%s/worker-credentials' % project,
                                body or {'label': 'worker'}, token=token)
        self.assertEqual(201, response.status, response.data)
        return response.data['credential']


# ---------------------------------------------------------------- 2. credential-authority
class CredentialAuthorityCase(Harness):
    def test_read_only_credential_never_inherits_issuer_authority(self):
        admin = self.admin_token()
        alex_id = self.create_account(admin, 'alex', 'alex-password-1')
        blair_id = self.create_account(admin, 'blair', 'blair-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        blair = self.login('blair', 'blair-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        beta = self.create_project(blair, 'Beta')

        # Issued by a superuser, explicitly read-only. It must not gain global
        # administration from the issuer, and must not write anywhere.
        issued = self.issue_credential(admin, project, label='ro', scopes=['read'])
        secret, credential_id = issued['secret'], issued['id']

        denied = [
            ('POST', '/v1/accounts', {'username': 'mallory'}),
            ('GET', '/v1/accounts', None),
            ('POST', '/v1/accounts/%s/password' % alex_id, {'new_password': 'x' * 12}),
            ('POST', '/v1/accounts/%s/reset' % alex_id, None),
            ('POST', '/v1/accounts/%s/disable' % alex_id, None),
            ('POST', '/v1/projects', {'name': 'Gamma'}),
            ('POST', '/v1/projects/%s/archive' % project, None),
            ('PUT', '/v1/projects/%s/members/%s' % (project, blair_id), {'role': 'viewer'}),
            ('DELETE', '/v1/projects/%s/members/%s' % (project, blair_id), None),
            ('POST', '/v1/projects/%s/worker-credentials' % project, {'label': 'x'}),
            ('POST', '/v1/projects/%s/worker-credentials/%s/revoke'
             % (project, credential_id), None),
            ('GET', '/v1/projects/%s/audit' % project, None),
            ('POST', '/v1/projects/%s/tasks' % project, {'title': 'sneaky'}),
            ('POST', '/v1/projects/%s/feedback' % project, {'text': 'sneaky'}),
        ]
        for method, path, body in denied:
            response = self.request(method, path, body, token=secret)
            self.assertEqual(403, response.status, (method, path, response.data))

        # A read-only credential can read its own project, but not learn another one.
        self.assertEqual(200, self.request('GET', '/v1/projects/%s/tasks' % project,
                                           token=secret).status)
        self.assertEqual(404, self.request('GET', '/v1/projects/%s/tasks' % beta,
                                           token=secret).status)
        self.assertEqual(0, self.request('GET', '/v1/projects/%s/tasks' % project,
                                         token=secret).data['total'])

        # Revoking the credential takes immediate effect.
        self.assertEqual(204, self.request('POST', '/v1/projects/%s/worker-credentials/%s/revoke'
                                           % (project, credential_id), token=admin).status)
        self.assertEqual(401, self.request('GET', '/v1/projects/%s/tasks' % project,
                                           token=secret).status)

    def test_project_scoped_credential_cannot_cross_projects(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.create_account(admin, 'blair', 'blair-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        blair = self.login('blair', 'blair-password-1')[0]
        alpha = self.create_project(alex, 'Alpha')
        beta = self.create_project(blair, 'Beta')
        issued = self.issue_credential(alex, alpha, label='writer',
                                       scopes=['tasks', 'checkpoints', 'reviews', 'feedback'])
        secret = issued['secret']
        # A credential scoped to another project must not even learn it exists.
        self.assertEqual(404, self.request('POST', '/v1/projects/%s/tasks' % beta,
                                           {'title': 'x'}, token=secret).status)
        self.assertEqual(404, self.request('GET', '/v1/projects/%s/tasks' % beta,
                                           token=secret).status)
        self.assertEqual(201, self.request('POST', '/v1/projects/%s/tasks' % alpha,
                                           {'title': 'ok'}, token=secret).status)


# ----------------------------------------------------------------- 3. idempotency-target
class IdempotencyTargetCase(Harness):
    def test_same_key_on_two_account_targets_does_not_collide(self):
        admin = self.admin_token()
        alex_id = self.create_account(admin, 'alex', 'alex-password-1')
        blair_id = self.create_account(admin, 'blair', 'blair-password-1')
        first = self.request('POST', '/v1/accounts/%s/disable' % alex_id, None,
                             token=admin, key='disable-key-0001')
        self.assertEqual(200, first.status, first.data)
        second = self.request('POST', '/v1/accounts/%s/disable' % blair_id, None,
                              token=admin, key='disable-key-0001')
        self.assertEqual(200, second.status, second.data)
        self.assertTrue(self.store.state['users'][blair_id]['disabled'])
        # An exact retry of the first target replays without error.
        retry = self.request('POST', '/v1/accounts/%s/disable' % alex_id, None,
                             token=admin, key='disable-key-0001')
        self.assertEqual(200, retry.status, retry.data)

    def test_same_key_on_two_member_targets_does_not_collide(self):
        admin = self.admin_token()
        alex_id = self.create_account(admin, 'alex', 'alex-password-1')
        blair_id = self.create_account(admin, 'blair', 'blair-password-1')
        project = self.create_project(admin, 'Alpha')
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s'
                                           % (project, alex_id), {'role': 'viewer'},
                                           token=admin, key='member-key-0001').status)
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s'
                                           % (project, blair_id), {'role': 'contributor'},
                                           token=admin, key='member-key-0001').status)
        members = self.store.state['memberships'][project]
        self.assertEqual('viewer', members[alex_id])
        self.assertEqual('contributor', members[blair_id])

    def test_conflicting_body_on_same_target_is_rejected(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        self.assertEqual(201, self.create_task(alex, project, 'one',
                                               key='task-key-0009').status)
        conflict = self.create_task(alex, project, 'two', key='task-key-0009')
        self.assertEqual(409, conflict.status)
        self.assertEqual('conflict', conflict.data['error']['code'])


# ---------------------------------------------------------------------- 4. final-owner
class FinalOwnerCase(Harness):
    def test_final_owner_invariant_includes_superuser(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.create_account(admin, 'blair', 'blair-password-1')
        alex, _, alex_user = self.login('alex', 'alex-password-1')
        _, _, blair_user = self.login('blair', 'blair-password-1')
        project = self.create_project(alex, 'Alpha')

        demote = self.request('PUT', '/v1/projects/%s/members/%s'
                              % (project, alex_user['id']), {'role': 'viewer'}, token=admin)
        self.assertEqual(409, demote.status, demote.data)
        disable = self.request('POST', '/v1/accounts/%s/disable' % alex_user['id'], None,
                               token=admin)
        self.assertEqual(409, disable.status, disable.data)
        remove = self.request('DELETE', '/v1/projects/%s/members/%s'
                              % (project, alex_user['id']), token=admin)
        self.assertEqual(409, remove.status, remove.data)
        # The project still has exactly one owner.
        self.assertEqual(1, sum(1 for role in self.store.state['memberships'][project].values()
                                if role == 'owner'))

        # Accepted recovery: install a second owner, then demote the first.
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s'
                                           % (project, blair_user['id']), {'role': 'owner'},
                                           token=admin).status)
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s'
                                           % (project, alex_user['id']), {'role': 'viewer'},
                                           token=admin).status)
        self.assertEqual(1, sum(1 for role in self.store.state['memberships'][project].values()
                                if role == 'owner'))


# ------------------------------------------------------------- 5. revocation-transaction
class PausingBackend(InProcessBackend):
    """Pause a canonical mutation at the call boundary, before any lock is taken."""

    def __init__(self, service):
        super().__init__(service)
        self.entered = threading.Event()
        self.proceed = threading.Event()
        self.pause_route = None

    def invoke(self, route, *args, **kwargs):
        if route == self.pause_route:
            self.entered.set()
            if not self.proceed.wait(timeout=20):
                raise RuntimeError('probe was never resumed')
        return super().invoke(route, *args, **kwargs)


class RevocationRaceCase(Harness):
    def make_backend(self):
        self.pausing = PausingBackend(self.service)
        return self.pausing

    def test_revocation_wins_over_an_in_flight_create(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        issued = self.issue_credential(alex, project, label='worker',
                                       scopes=['tasks', 'checkpoints', 'reviews', 'feedback'])
        secret, credential_id = issued['secret'], issued['id']

        self.pausing.pause_route = 'tasks.create'
        self.pausing.entered.clear()
        self.pausing.proceed.clear()
        outcome = {}

        def create():
            outcome['response'] = self.create_task(secret, project, 'raced task',
                                                   key='race-key-0001')

        worker = threading.Thread(target=create)
        worker.start()
        self.assertTrue(self.pausing.entered.wait(timeout=15), 'create never reached the seam')
        # Revocation completes on another request while the create is paused.
        revoked = self.request('POST', '/v1/projects/%s/worker-credentials/%s/revoke'
                               % (project, credential_id), token=alex)
        self.assertEqual(204, revoked.status, revoked.body)
        self.pausing.proceed.set()
        worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        # The create must not succeed once revocation has committed.
        self.assertEqual(401, outcome['response'].status, outcome['response'].data)
        self.assertEqual(0, self.request('GET', '/v1/projects/%s/tasks' % project,
                                         token=alex).data['total'])

    def test_uncertain_outcome_is_durable_and_reconcilable(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        self.backend.fail_next('tasks.create')
        first = self.create_task(alex, project, 'lost response', key='task-key-0010')
        self.assertEqual(503, first.status)
        retry = self.create_task(alex, project, 'lost response', key='task-key-0010')
        self.assertEqual(201, retry.status, retry.data)
        self.assertEqual(1, self.request('GET', '/v1/projects/%s/tasks' % project,
                                         token=alex).data['total'])


class StoreDurabilityCase(unittest.TestCase):
    def test_concurrent_saves_are_atomic_and_leave_no_temp(self):
        tmp = unique_dir('st-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = Store(tmp / 'state.json')
        Service.bootstrap_superuser(store, ADMIN, ADMIN_PASSWORD)
        errors = []

        def writer(number):
            try:
                for index in range(15):
                    with store.lock:
                        store.state['audit'].append({'writer': number, 'entry': index})
                        store.save()
            except Exception as error:  # noqa: BLE001 - surfaced by the assertion
                errors.append(error)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(errors, errors)
        reloaded = json.loads((tmp / 'state.json').read_text(encoding='utf-8'))
        self.assertGreaterEqual(len(reloaded['audit']), 1)
        self.assertEqual([], list(tmp.glob('*.tmp')))


# ------------------------------------------------------- 6. browser-deployment-contract
class DeploymentContractCase(Harness):
    trusted_proxies = ('127.0.0.1',)

    def test_trusted_proxy_https_sets_secure_cookie(self):
        self.create_account(self.admin_token(), 'alex', 'alex-password-1')
        response = self.request('POST', '/v1/sessions',
                                {'username': 'alex', 'password': 'alex-password-1'},
                                headers={'X-Forwarded-Proto': 'https'})
        self.assertEqual(201, response.status)
        cookies = response.set_cookies()
        self.assertTrue(cookies)
        self.assertIn('Secure', cookies[0])

    def test_owner_only_review_approval(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.create_account(admin, 'blair', 'blair-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        blair, _, blair_user = self.login('blair', 'blair-password-1')
        project = self.create_project(alex, 'Alpha')
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s'
                                           % (project, blair_user['id']),
                                           {'role': 'contributor'}, token=alex).status)
        task = self.create_task(alex, project, 'delivery').data
        contribution = self.request('POST', '/v1/projects/%s/tasks/%s/reviews'
                                    % (project, task['id']),
                                    {'operation': 'contribute', 'commit': COMMIT,
                                     'base_commit': BASE, 'bundle_sha256': BUNDLE,
                                     'summary': 'delivered'}, token=blair)
        self.assertEqual(201, contribution.status, contribution.data)
        denied = self.request('POST', '/v1/projects/%s/tasks/%s/reviews'
                              % (project, task['id']),
                              {'operation': 'approve', 'summary': 'lgtm'}, token=blair)
        self.assertEqual(403, denied.status, denied.data)
        allowed = self.request('POST', '/v1/projects/%s/tasks/%s/reviews'
                               % (project, task['id']),
                               {'operation': 'approve', 'summary': 'lgtm'}, token=alex)
        self.assertEqual(201, allowed.status, allowed.data)

    def test_credential_can_never_approve(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        task = self.create_task(alex, project, 'delivery').data
        issued = self.issue_credential(alex, project, label='worker',
                                       scopes=['tasks', 'checkpoints', 'reviews', 'feedback'])
        secret = issued['secret']
        self.assertEqual(201, self.request('POST', '/v1/projects/%s/tasks/%s/reviews'
                                           % (project, task['id']),
                                           {'operation': 'contribute', 'commit': COMMIT,
                                            'base_commit': BASE, 'bundle_sha256': BUNDLE,
                                            'summary': 'delivered'}, token=secret).status)
        denied = self.request('POST', '/v1/projects/%s/tasks/%s/reviews'
                              % (project, task['id']),
                              {'operation': 'approve', 'summary': 'lgtm'}, token=secret)
        self.assertEqual(403, denied.status, denied.data)

    def test_canonical_ids_with_hyphens_and_dots_route(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        response = self.request('POST', '/v1/projects',
                                {'name': 'Alpha', 'project_id': 'alpha-5bb.19'}, token=alex)
        self.assertEqual(201, response.status, response.data)
        self.assertEqual(200, self.request('GET', '/v1/projects/alpha-5bb.19',
                                           token=alex).status)
        task = self.create_task(alex, 'alpha-5bb.19', 'named task').data
        self.assertEqual(200, self.request('GET', '/v1/projects/alpha-5bb.19/tasks/%s'
                                           % task['id'], token=alex).status)


class UntrustedProxyCase(Harness):
    trusted_proxies = ('10.9.9.9',)

    def test_forwarded_proto_from_an_untrusted_peer_is_ignored(self):
        self.create_account(self.admin_token(), 'alex', 'alex-password-1')
        response = self.request('POST', '/v1/sessions',
                                {'username': 'alex', 'password': 'alex-password-1'},
                                headers={'X-Forwarded-Proto': 'https'})
        self.assertEqual(201, response.status)
        cookies = response.set_cookies()
        self.assertTrue(cookies)
        self.assertNotIn('Secure', cookies[0])


class PlaintextLoopbackCase(Harness):
    trusted_proxies = ()

    def test_plaintext_loopback_cookie_is_not_marked_secure(self):
        self.create_account(self.admin_token(), 'alex', 'alex-password-1')
        response = self.request('POST', '/v1/sessions',
                                {'username': 'alex', 'password': 'alex-password-1'},
                                headers={'X-Forwarded-Proto': 'https'})
        cookies = response.set_cookies()
        self.assertTrue(cookies)
        self.assertNotIn('Secure', cookies[0])


# ------------------------------------------------------------------ 1. canonical-backend
class CanonicalEndpointCase(Harness):
    def make_backend(self):
        self.canonical_root = self.tmp / 'canonical'
        return EndpointBackend(sys.executable, str(STUB), str(self.canonical_root),
                               service=self.service)

    def _setup_project(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        return alex, self.create_project(alex, 'Alpha')

    def test_http_flows_through_the_canonical_process(self):
        alex, project = self._setup_project()
        created = self.create_task(alex, project, 'canonical task', key='canon-key-0001')
        self.assertEqual(201, created.status, created.data)
        task_id = created.data['id']
        self.assertIn('.', task_id)  # canonical ids carry a dot
        listed = self.request('GET', '/v1/projects/%s/tasks' % project, token=alex).data
        self.assertEqual(1, listed['total'])
        self.assertEqual(200, self.request('GET', '/v1/projects/%s/tasks/%s'
                                           % (project, task_id), token=alex).status)
        # The canonical file, not service memory, holds the record.
        canonical = json.loads((self.canonical_root / 'canonical.json').read_text())
        self.assertEqual(1, len(canonical['tasks']))

    def test_uncertain_write_reconciles_after_a_restart(self):
        alex, project = self._setup_project()
        self.backend.fail_next('tasks.create')
        first = self.create_task(alex, project, 'canonical task', key='canon-key-0002')
        self.assertEqual(503, first.status)
        # Rebuild the service and backend from disk: the receipt is durable.
        store = Store(self.tmp / 'state.json')
        service = Service(store)
        backend = EndpointBackend(sys.executable, str(STUB), str(self.canonical_root),
                                  service=service)
        self.restart(service, backend)
        retry = self.create_task(alex, project, 'canonical task', key='canon-key-0002')
        self.assertEqual(201, retry.status, retry.data)
        canonical = json.loads((self.canonical_root / 'canonical.json').read_text())
        self.assertEqual(1, len(canonical['tasks']))

    def test_feedback_fails_closed_until_the_canonical_stream_exists(self):
        alex, project = self._setup_project()
        response = self.request('POST', '/v1/projects/%s/feedback' % project,
                                {'text': 'note'}, token=alex)
        self.assertEqual(501, response.status, response.data)
        self.assertEqual('not_implemented', response.data['error']['code'])


class BackendSelectionCase(unittest.TestCase):
    def test_build_backend_selects_endpoint_then_inprocess(self):
        tmp = unique_dir('sel-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = Store(tmp / 'state.json')
        Service.bootstrap_superuser(store, ADMIN, ADMIN_PASSWORD)
        service = Service(store)
        args = argparse.Namespace(backend='endpoint', endpoint_python=sys.executable,
                                  endpoint=str(STUB), root=str(tmp / 'root'),
                                  actor_namespace='http', endpoint_timeout=30)
        self.assertIsInstance(build_backend(service, args), EndpointBackend)
        args.backend = 'inprocess'
        self.assertIsInstance(build_backend(service, args), InProcessBackend)


if __name__ == '__main__':
    unittest.main()
