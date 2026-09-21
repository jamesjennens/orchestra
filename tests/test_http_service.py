"""Disposable HTTP contract tests for the authenticated office service.

Every test starts a real ``ThreadingHTTPServer`` on loopback with a synthetic
superuser and a temp-file store; nothing touches live coordination data. Coverage
follows section 11 of ``docs/HTTP_TRANSPORT_DESIGN.md``: unauthenticated access,
cross-project reads/writes, role escalation, final-owner/superuser safeguards,
CSRF/cookie behaviour, stale cursors, replay, conflicting idempotency keys,
pagination, bounded requests, clean JSON errors and a full worker coordination
flow over HTTP only.
"""
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

from http_auth import Service, Store
from http_service import MAX_BODY_BYTES, InProcessBackend, create_server

TMP_ROOT = Path(os.environ.get('ORCHESTRA_TEST_TMP', str(ROOT / '.runtime' / 'test-tmp')))
ADMIN = 'root-admin'
ADMIN_PASSWORD = 'correct-horse-battery-staple'
COMMIT = 'a' * 40
BASE = 'b' * 40
BUNDLE = 'c' * 64


def unique_dir(prefix):
    """Create a uniquely named directory with default ACLs (see test_http_auth)."""
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


class ServerHarness(unittest.TestCase):
    max_body = MAX_BODY_BYTES

    def setUp(self):
        self.tmp_path = unique_dir('svc-')
        self.addCleanup(shutil.rmtree, self.tmp_path, ignore_errors=True)
        self.store = Store(self.tmp_path / 'state.json')
        self.admin_user = Service.bootstrap_superuser(self.store, ADMIN, ADMIN_PASSWORD)
        self.service = Service(self.store)
        self.backend = InProcessBackend(self.service)
        self.httpd = create_server(self.service, self.backend, host='127.0.0.1', port=0,
                                   max_body=self.max_body)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    # -- transport -------------------------------------------------------------
    def request(self, method, path, body=None, token=None, cookie=None, csrf=None,
                key=None, headers=None, content_type='application/json'):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
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

    def create_project(self, token, name):
        response = self.request('POST', '/v1/projects', {'name': name}, token=token)
        self.assertEqual(201, response.status, response.data)
        return response.data['id']

    def create_task(self, token, project, title, key=None):
        return self.request('POST', '/v1/projects/%s/tasks' % project, {'title': title},
                            token=token, key=key)


class HttpContractCase(ServerHarness):
    # -- baseline --------------------------------------------------------------
    def test_healthz_is_anonymous_and_no_store(self):
        response = self.request('GET', '/healthz')
        self.assertEqual(200, response.status)
        self.assertEqual({'status': 'ok'}, response.data)
        self.assertEqual('no-store', response.headers.get('cache-control'))
        self.assertEqual('nosniff', response.headers.get('x-content-type-options'))
        self.assertTrue(response.headers.get('x-request-id'))

    def test_unauthenticated_is_rejected_with_clean_json(self):
        response = self.request('GET', '/v1/projects')
        self.assertEqual(401, response.status)
        self.assertEqual('unauthenticated', response.data['error']['code'])
        self.assertEqual(response.data['request_id'], response.headers.get('x-request-id'))

    def test_unknown_route_and_method_are_404(self):
        self.assertEqual(404, self.request('GET', '/v1/nope').status)
        self.assertEqual(404, self.request('DELETE', '/v1/projects').status)

    def test_login_failure_is_uniform(self):
        self.create_account(self.admin_token(), 'alex', 'alex-password-1')
        missing = self.request('POST', '/v1/sessions',
                               {'username': 'nobody', 'password': 'whatever-password'})
        wrong = self.request('POST', '/v1/sessions',
                             {'username': 'alex', 'password': 'whatever-password'})
        self.assertEqual(401, missing.status)
        self.assertEqual(401, wrong.status)
        self.assertEqual(missing.data['error'], wrong.data['error'])

    def test_cookie_session_requires_csrf_and_sets_safe_cookie(self):
        self.create_account(self.admin_token(), 'alex', 'alex-password-1')
        response = self.request('POST', '/v1/sessions',
                                {'username': 'alex', 'password': 'alex-password-1'})
        cookies = response.set_cookies()
        self.assertEqual(1, len(cookies), cookies)
        self.assertIn('HttpOnly', cookies[0])
        self.assertIn('SameSite=Strict', cookies[0])
        cookie = cookies[0].split(';')[0]
        csrf = response.data['session']['csrf_token']
        self.assertEqual(200, self.request('GET', '/v1/projects', cookie=cookie).status)
        denied = self.request('POST', '/v1/projects', {'name': 'Alpha'}, cookie=cookie)
        self.assertEqual(403, denied.status)
        allowed = self.request('POST', '/v1/projects', {'name': 'Alpha'}, cookie=cookie,
                               csrf=csrf)
        self.assertEqual(201, allowed.status, allowed.data)

    def test_request_id_propagation_and_replacement(self):
        supplied = self.request('GET', '/healthz', headers={'X-Request-Id': 'req_client_01'})
        self.assertEqual('req_client_01', supplied.headers.get('x-request-id'))
        generated = self.request('GET', '/healthz',
                                 headers={'X-Request-Id': 'bad id with spaces'})
        self.assertTrue(generated.headers.get('x-request-id').startswith('req_'))

    # -- isolation and authority ----------------------------------------------
    def test_project_isolation_hides_other_projects(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.create_account(admin, 'blair', 'blair-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        blair = self.login('blair', 'blair-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        self.assertEqual(200, self.request('GET', '/v1/projects/%s' % project, token=alex).status)
        self.assertEqual(404, self.request('GET', '/v1/projects/%s' % project,
                                           token=blair).status)
        self.assertEqual(404, self.request('GET', '/v1/projects/%s/tasks' % project,
                                           token=blair).status)
        self.assertEqual([], self.request('GET', '/v1/projects', token=blair).data['items'])

    def test_actor_spoofing_is_rejected(self):
        alex_id = self.create_account(self.admin_token(), 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        task = self.create_task(alex, project, 'first task').data
        spoofed = self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (project, task['id']),
                               {'actor': 'someone-else'}, token=alex)
        self.assertEqual(403, spoofed.status)
        claimed = self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (project, task['id']),
                               {'actor': alex_id}, token=alex)
        self.assertEqual(200, claimed.status, claimed.data)
        self.assertEqual(alex_id, claimed.data['assignee'])

    def test_credential_scope_namespace_and_revocation(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        alpha = self.create_project(alex, 'Alpha')
        beta = self.create_project(alex, 'Beta')
        task = self.create_task(alex, alpha, 'first task').data
        issued = self.request('POST', '/v1/projects/%s/worker-credentials' % alpha,
                              {'label': 'worker', 'actor': 'worker'}, token=alex)
        self.assertEqual(201, issued.status, issued.data)
        secret = issued.data['credential']['secret']
        self.assertEqual(1, self.request('GET', '/v1/projects/%s/tasks' % alpha,
                                         token=secret).data['total'])
        self.assertEqual(404, self.request('GET', '/v1/projects/%s/tasks' % beta,
                                           token=secret).status)
        denied = self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (alpha, task['id']),
                              {'actor': 'other/run-1'}, token=secret)
        self.assertEqual(403, denied.status)
        claimed = self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (alpha, task['id']),
                               {'actor': 'worker/run-1'}, token=secret)
        self.assertEqual(200, claimed.status, claimed.data)
        revoked = self.request('POST', '/v1/projects/%s/worker-credentials/%s/revoke'
                               % (alpha, issued.data['credential']['id']), token=alex)
        self.assertEqual(204, revoked.status, revoked.body)
        self.assertEqual(401, self.request('GET', '/v1/projects/%s/tasks' % alpha,
                                           token=secret).status)

    def test_expired_session_and_logout(self):
        self.create_account(self.admin_token(), 'alex', 'alex-password-1')
        token, _, _ = self.login('alex', 'alex-password-1')
        self.assertEqual(200, self.request('GET', '/v1/projects', token=token).status)
        for session in self.store.state['sessions'].values():
            session['idle_expires'] = 0
        self.store.save()
        self.assertEqual(401, self.request('GET', '/v1/projects', token=token).status)

    def test_logout_returns_204_without_a_body(self):
        self.create_account(self.admin_token(), 'alex', 'alex-password-1')
        token, _, _ = self.login('alex', 'alex-password-1')
        response = self.request('DELETE', '/v1/sessions/current', token=token)
        self.assertEqual(204, response.status)
        self.assertEqual(b'', response.body)
        self.assertEqual(401, self.request('GET', '/v1/projects', token=token).status)

    def test_final_owner_safeguard_over_http(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.create_account(admin, 'blair', 'blair-password-1')
        alex, _, alex_user = self.login('alex', 'alex-password-1')
        _, _, blair_user = self.login('blair', 'blair-password-1')
        project = self.create_project(alex, 'Alpha')
        self.assertEqual(403, self.request('DELETE', '/v1/projects/%s/members/%s'
                                           % (project, alex_user['id']), token=alex).status)
        self.assertEqual(403, self.request('PUT', '/v1/projects/%s/members/%s'
                                           % (project, blair_user['id']), {'role': 'owner'},
                                           token=alex).status)
        # Even a superuser cannot remove the final active owner.
        self.assertEqual(409, self.request('DELETE', '/v1/projects/%s/members/%s'
                                           % (project, alex_user['id']), token=admin).status)

    def test_audit_read_is_owner_only(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.create_account(admin, 'blair', 'blair-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        blair, _, blair_user = self.login('blair', 'blair-password-1')
        project = self.create_project(alex, 'Alpha')
        self.request('PUT', '/v1/projects/%s/members/%s' % (project, blair_user['id']),
                     {'role': 'contributor'}, token=alex)
        self.assertEqual(200, self.request('GET', '/v1/projects/%s/audit' % project,
                                           token=alex).status)
        self.assertEqual(403, self.request('GET', '/v1/projects/%s/audit' % project,
                                           token=blair).status)

    # -- idempotency and uncertainty ------------------------------------------
    def test_idempotent_retry_does_not_duplicate(self):
        self.create_account(self.admin_token(), 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        first = self.create_task(alex, project, 'first task', key='task-key-0001')
        self.assertEqual(201, first.status)
        second = self.create_task(alex, project, 'first task', key='task-key-0001')
        self.assertEqual(201, second.status)
        self.assertEqual(first.data['id'], second.data['id'])
        listing = self.request('GET', '/v1/projects/%s/tasks' % project, token=alex).data
        self.assertEqual(1, listing['total'])

    def test_idempotency_key_conflict_on_different_payload(self):
        self.create_account(self.admin_token(), 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        self.create_task(alex, project, 'first task', key='task-key-0002')
        conflict = self.create_task(alex, project, 'different task', key='task-key-0002')
        self.assertEqual(409, conflict.status)
        self.assertEqual('conflict', conflict.data['error']['code'])

    def test_uncertain_write_reconciles_on_exact_retry(self):
        self.create_account(self.admin_token(), 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        self.backend.fail_next('tasks.create')
        uncertain = self.create_task(alex, project, 'first task', key='task-key-0003')
        self.assertEqual(503, uncertain.status)
        self.assertEqual('uncertain', uncertain.data['error']['code'])
        retry = self.create_task(alex, project, 'first task', key='task-key-0003')
        self.assertEqual(201, retry.status, retry.data)
        listing = self.request('GET', '/v1/projects/%s/tasks' % project, token=alex).data
        self.assertEqual(1, listing['total'])
        self.assertEqual(retry.data['id'], listing['items'][0]['id'])

    def test_one_time_secrets_are_not_replayed(self):
        admin = self.admin_token()
        created = self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        issued = self.request('POST', '/v1/projects/%s/worker-credentials' % project,
                              {'label': 'worker'}, token=alex, key='cred-key-0001')
        self.assertEqual(201, issued.status)
        self.assertTrue(issued.data['credential']['secret'])
        replay = self.request('POST', '/v1/projects/%s/worker-credentials' % project,
                              {'label': 'worker'}, token=alex, key='cred-key-0001')
        self.assertEqual(200, replay.status, replay.data)
        self.assertNotIn('secret', replay.data['credential'])
        self.assertFalse(replay.data['secret_available'])
        self.assertEqual(1, len(self.store.state['credentials']))
        reset = self.request('POST', '/v1/accounts/%s/reset' % created, token=admin,
                             key='reset-key-0001')
        self.assertEqual(201, reset.status)
        self.assertTrue(reset.data['reset_value'])
        reset_replay = self.request('POST', '/v1/accounts/%s/reset' % created, token=admin,
                                    key='reset-key-0001')
        self.assertEqual(200, reset_replay.status)
        self.assertNotIn('reset_value', reset_replay.data)
        self.assertFalse(reset_replay.data['reset_value_available'])
        self.assertEqual(1, len(self.store.state['reset_tokens']))

    # -- bounded requests and clean errors ------------------------------------
    def test_unbounded_and_malformed_requests_are_rejected(self):
        self.create_account(self.admin_token(), 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        bad_json = self.request('POST', '/v1/projects', '{not json', token=alex)
        self.assertEqual(422, bad_json.status)
        self.assertEqual('invalid_payload', bad_json.data['error']['code'])
        wrong_type = self.request('POST', '/v1/projects', 'name=Alpha', token=alex,
                                  content_type='text/plain')
        self.assertEqual(415, wrong_type.status)
        not_object = self.request('POST', '/v1/projects', '[1, 2]', token=alex)
        self.assertEqual(422, not_object.status)
        too_many = self.request('POST', '/v1/projects/%s/tasks' % project,
                                {'title': 'x',
                                 'attachments': [{'name': 'a.txt', 'content_base64': 'eA=='}
                                                 for _ in range(9)]}, token=alex)
        self.assertEqual(413, too_many.status)
        traversal = self.request('POST', '/v1/projects/%s/tasks' % project,
                                 {'title': 'x', 'attachments': [
                                     {'name': '../escape.txt', 'content_base64': 'eA=='}]},
                                 token=alex)
        self.assertEqual(422, traversal.status)
        media = self.request('POST', '/v1/projects/%s/tasks' % project,
                             {'title': 'x', 'attachments': [
                                 {'name': 'a.bin', 'media_type': 'application/octet-stream',
                                  'content_base64': 'eA=='}]}, token=alex)
        self.assertEqual(422, media.status)

    def test_pagination_cursor_round_trip_and_staleness(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        for index in range(3):
            self.create_task(alex, project, 'task %d' % index)
        first = self.request('GET', '/v1/projects/%s/tasks?limit=2' % project, token=alex).data
        self.assertEqual(2, len(first['items']))
        self.assertEqual(3, first['total'])
        self.assertTrue(first['next_cursor'])
        second = self.request('GET', '/v1/projects/%s/tasks?limit=2&cursor=%s'
                              % (project, first['next_cursor']), token=alex).data
        self.assertEqual(1, len(second['items']))
        self.assertIsNone(second['next_cursor'])
        stale = self.request('GET', '/v1/projects/%s/tasks?limit=2&cursor=AAAA' % project,
                             token=alex)
        self.assertEqual(409, stale.status)
        # A cursor minted for one principal is not valid for another authorized one.
        foreign = self.request('GET', '/v1/projects/%s/tasks?limit=2&cursor=%s'
                               % (project, first['next_cursor']), token=admin)
        self.assertEqual(409, foreign.status)

class SmallBodyCase(ServerHarness):
    max_body = 1024

    def test_configured_body_limit_is_enforced_with_clean_json(self):
        response = self.request('POST', '/v1/sessions',
                                {'username': ADMIN, 'password': 'x' * 4000})
        self.assertEqual(413, response.status)
        self.assertEqual('payload_too_large', response.data['error']['code'])


class FullFlowCase(ServerHarness):
    def test_worker_coordination_flow_over_http_only(self):
        """A fresh user completes the documented walkthrough without SSH or file access."""
        admin = self.admin_token()
        alex_id = self.create_account(admin, 'alex', 'alex-password-1')
        blair_id = self.create_account(admin, 'blair', 'blair-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        blair = self.login('blair', 'blair-password-1')[0]

        project = self.create_project(alex, 'Alpha')
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s'
                                           % (project, blair_id), {'role': 'contributor'},
                                           token=alex).status)
        self.assertEqual('contributor',
                         self.request('GET', '/v1/projects/%s' % project,
                                      token=blair).data['role'])

        task = self.create_task(alex, project, 'office API delivery').data
        issued = self.request('POST', '/v1/projects/%s/worker-credentials' % project,
                              {'label': 'ds-sub', 'actor': 'worker'}, token=alex).data
        secret = issued['credential']['secret']

        claimed = self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (project, task['id']),
                               {'actor': 'worker/run-7'}, token=secret)
        self.assertEqual(200, claimed.status, claimed.data)
        self.assertEqual('worker/run-7', claimed.data['assignee'])

        checkpoint = self.request('POST', '/v1/projects/%s/tasks/%s/checkpoints'
                                  % (project, task['id']),
                                  {'previous': None, 'summary': 'implementation done',
                                   'open_items': []}, token=secret)
        self.assertEqual(201, checkpoint.status, checkpoint.data)

        contribution = self.request('POST', '/v1/projects/%s/tasks/%s/reviews'
                                    % (project, task['id']),
                                    {'operation': 'contribute', 'commit': COMMIT,
                                     'base_commit': BASE, 'bundle_sha256': BUNDLE,
                                     'summary': 'delivered on a branch plus bundle',
                                     'actor': 'worker/run-7'}, token=secret)
        self.assertEqual(201, contribution.status, contribution.data)

        brief = self.request('GET', '/v1/projects/%s/tasks/%s' % (project, task['id']),
                             token=alex).data
        self.assertEqual('awaiting-review', brief['review_state'])
        history = self.request('GET', '/v1/projects/%s/tasks/%s/history'
                               % (project, task['id']), token=blair).data
        self.assertTrue(history['items'])
        self.assertGreaterEqual(history['total'], 4)

        self.assertEqual(201, self.request('POST', '/v1/projects/%s/feedback' % project,
                                           {'text': 'reviewer note'}, token=alex).status)
        self.assertEqual(201, self.request('POST', '/v1/projects/%s/feedback' % project,
                                           {'text': 'worker note'}, token=secret).status)
        self.assertEqual(2, self.request('GET', '/v1/projects/%s/feedback' % project,
                                         token=blair).data['total'])

        approved = self.request('POST', '/v1/projects/%s/tasks/%s/reviews'
                                % (project, task['id']),
                                {'operation': 'approve', 'summary': 'accepted'}, token=alex)
        self.assertEqual(201, approved.status, approved.data)
        self.assertEqual('approved',
                         self.request('GET', '/v1/projects/%s/tasks/%s' % (project, task['id']),
                                      token=blair).data['review_state'])

        # Contributor cannot read audit or issue credentials; the owner can.
        self.assertEqual(403, self.request('GET', '/v1/projects/%s/audit' % project,
                                           token=blair).status)
        self.assertEqual(200, self.request('GET', '/v1/projects/%s/audit' % project,
                                           token=alex).status)
        self.assertEqual(403, self.request('POST', '/v1/projects/%s/worker-credentials'
                                           % project, {'label': 'x'}, token=blair).status)
        # Stable human identity and the credential's run attribution stay distinct.
        self.assertNotEqual(alex_id, blair_id)
        self.assertEqual(alex_id, self.request('GET', '/v1/projects/%s/tasks/%s'
                                               % (project, task['id']),
                                               token=alex).data['created_by'])


class TlsPolicyCase(unittest.TestCase):
    def test_plaintext_non_loopback_listener_is_refused(self):
        tmp = unique_dir('tls-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = Store(tmp / 'state.json')
        Service.bootstrap_superuser(store, ADMIN, ADMIN_PASSWORD)
        service = Service(store)
        backend = InProcessBackend(service)
        with self.assertRaises(ValueError):
            create_server(service, backend, host='0.0.0.0', port=0)
        server = create_server(service, backend, host='0.0.0.0', port=0,
                               allow_plaintext_non_loopback=True)
        server.server_close()


if __name__ == '__main__':
    unittest.main()
