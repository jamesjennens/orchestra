"""HTTP contract tests for personal (owner-bound) agents - kittrial-5bb.22.

Every test runs a real loopback ``ThreadingHTTPServer`` over a throwaway store; no
live coordination data is touched. Coverage follows the task acceptance criteria:
owner-bound registry with a working-directory hint, one-time credential setup,
REST next-action for the agent, owner-visible attention with directory and resume
prompt, path privacy, revocation and owner-disable, the live-authority cap,
cross-project denial, and the absence of polling/scheduled work.
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

import http_service
from http_auth import Service, Store
from http_service import MAX_BODY_BYTES, InProcessBackend, create_server

TMP_ROOT = Path(os.environ.get('ORCHESTRA_TEST_TMP', str(ROOT / '.runtime' / 'test-tmp')))
ADMIN = 'root-admin'
ADMIN_PASSWORD = 'correct-horse-battery-staple'
COMMIT = 'a' * 40
BASE = 'b' * 40
BUNDLE = 'c' * 64


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


class AgentHarness(unittest.TestCase):
    max_body = MAX_BODY_BYTES

    def setUp(self):
        self.tmp_path = unique_dir('agent-')
        self.addCleanup(shutil.rmtree, self.tmp_path, ignore_errors=True)
        self.store = Store(self.tmp_path / 'state.json')
        self.admin_user = Service.bootstrap_superuser(self.store, ADMIN, ADMIN_PASSWORD)
        self.service = Service(self.store, public_url='https://office.example.invalid')
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

    def request(self, method, path, body=None, token=None, key=None):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        headers = {}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if key:
            headers['Idempotency-Key'] = key
        payload = None if body is None else json.dumps(body)
        try:
            client.request(method, path, body=payload, headers=headers)
            response = client.getresponse()
            return Response(response.status, response.getheaders(), response.read())
        finally:
            client.close()

    # -- fixtures --------------------------------------------------------------
    def login(self, username, password):
        response = self.request('POST', '/v1/sessions',
                                {'username': username, 'password': password})
        self.assertEqual(201, response.status, response.data)
        return response.data['session']['token'], response.data['user']

    def admin_token(self):
        return self.login(ADMIN, ADMIN_PASSWORD)[0]

    def create_account(self, admin, username, password):
        created = self.request('POST', '/v1/accounts', {'username': username}, token=admin)
        self.assertEqual(201, created.status, created.data)
        user_id = created.data['id']
        changed = self.request('POST', '/v1/accounts/%s/password' % user_id,
                               {'new_password': password}, token=admin)
        self.assertEqual(200, changed.status, changed.data)
        return user_id

    def create_project(self, token, name):
        response = self.request('POST', '/v1/projects', {'name': name}, token=token)
        self.assertEqual(201, response.status, response.data)
        return response.data['id']

    def create_agent(self, token, **payload):
        payload.setdefault('name', 'Kestrel')
        payload.setdefault('working_directory', '/home/priya/work/kestrel')
        return self.request('POST', '/v1/agents', payload, token=token)

    def agent_secret(self, token, **payload):
        created = self.create_agent(token, **payload)
        self.assertEqual(201, created.status, created.data)
        return created.data['agent']['id'], created.data['credential']['secret'], created.data


class AgentRegistryTests(AgentHarness):
    def test_create_returns_one_time_secret_and_secretless_setup(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        agent_id, secret, data = self.agent_secret(alex, tool='GitHub Copilot in VS Code',
                                                   machine='desk-01', notes='pilot agent')
        self.assertTrue(agent_id.startswith('agent_'))
        self.assertTrue(secret)
        self.assertTrue(data['secret_available'])
        self.assertEqual('/home/priya/work/kestrel', data['agent']['working_directory'])
        self.assertEqual('GitHub Copilot in VS Code', data['agent']['tool'])
        setup = data['setup']
        self.assertEqual('.orchestra/agent.json', setup['config_path'])
        self.assertFalse(setup['config_contains_secret'])
        blob = json.dumps(setup)
        self.assertNotIn(secret, blob)
        self.assertEqual(agent_id, setup['config']['agent_id'])
        self.assertEqual('https://office.example.invalid', setup['config']['server_url'])
        self.assertNotIn('secret', setup['config'])
        # The credential metadata is returned, but the secret is not persisted in a
        # readable form in the public agent record.
        listing = self.request('GET', '/v1/agents', token=alex)
        self.assertEqual(200, listing.status, listing.data)
        self.assertEqual(1, listing.data['total'])
        self.assertNotIn(secret, json.dumps(listing.data))

    def test_create_replay_never_redelivers_the_secret(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        body = {'name': 'Kestrel', 'working_directory': '/home/priya/kestrel'}
        first = self.request('POST', '/v1/agents', body, token=alex, key='agent-key-0001')
        self.assertEqual(201, first.status, first.data)
        self.assertTrue(first.data['credential']['secret'])
        replay = self.request('POST', '/v1/agents', body, token=alex, key='agent-key-0001')
        self.assertEqual(200, replay.status, replay.data)
        self.assertNotIn('secret', replay.data['credential'])
        self.assertFalse(replay.data['credential']['secret_available'])
        self.assertEqual(first.data['agent']['id'], replay.data['agent']['id'])
        self.assertEqual(1, self.request('GET', '/v1/agents', token=alex).data['total'])

    def test_issue_credential_route_is_one_time(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        agent_id, secret, _ = self.agent_secret(alex)
        issued = self.request('POST', '/v1/agents/%s/credentials' % agent_id,
                              {'label': 'second'}, token=alex, key='agent-cred-0001')
        self.assertEqual(201, issued.status, issued.data)
        second = issued.data['credential']['secret']
        self.assertTrue(second and second != secret)
        replay = self.request('POST', '/v1/agents/%s/credentials' % agent_id,
                              {'label': 'second'}, token=alex, key='agent-cred-0001')
        self.assertEqual(200, replay.status, replay.data)
        self.assertNotIn('secret', replay.data['credential'])
        # Both credentials authenticate independently.
        for token in (secret, second):
            self.assertEqual(200, self.request('GET', '/v1/agents/me', token=token).status)


class AgentRestContractTests(AgentHarness):
    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.create_account(self.admin, 'alex', 'alex-password-1')
        self.alex = self.login('alex', 'alex-password-1')[0]
        self.project = self.create_project(self.alex, 'Alpha')
        self.agent_id, self.secret, _ = self.agent_secret(self.alex, projects=[self.project])

    def create_task(self, token, project, title):
        return self.request('POST', '/v1/projects/%s/tasks' % project, {'title': title},
                            token=token)

    def test_agent_reads_itself_and_next_action(self):
        me = self.request('GET', '/v1/agents/me', token=self.secret)
        self.assertEqual(200, me.status, me.data)
        self.assertEqual(self.agent_id, me.data['agent']['id'])
        self.assertEqual('/home/priya/work/kestrel', me.data['agent']['working_directory'])
        self.assertIn('attention', me.data)
        # API use stamps last_seen_at and persists it.
        self.assertIsNotNone(me.data['agent']['last_seen_at'])
        owner_view = self.request('GET', '/v1/agents/%s' % self.agent_id, token=self.alex)
        self.assertEqual(200, owner_view.status, owner_view.data)
        self.assertIsNotNone(owner_view.data['last_seen_at'])

    def test_next_action_is_stable_json_and_computed_at_read_time(self):
        self.assertEqual(201, self.create_task(self.alex, self.project, 'first').status)
        first = self.request('GET', '/v1/agents/me/next', token=self.secret)
        self.assertEqual(200, first.status, first.data)
        self.assertEqual('idle', first.data['attention']['state'])
        self.assertEqual(1, first.data['attention']['counts']['claimable'])
        self.assertEqual(1, len(first.data['next_actions']))
        action = first.data['next_actions'][0]
        self.assertEqual('claimable-task', action['kind'])
        self.assertEqual(self.project, action['project'])
        self.assertIn('/v1/projects/%s/tasks/' % self.project, action['links']['task'])
        self.assertEqual(action['links']['task'], action['links']['brief'])
        self.assertEqual([self.project], [p['id'] for p in first.data['projects']])
        # A second task appears on the very next read: attention is computed at read
        # time, not by a cached/scheduled job.
        self.assertEqual(201, self.create_task(self.alex, self.project, 'second').status)
        second = self.request('GET', '/v1/agents/me/next', token=self.secret)
        self.assertEqual(2, second.data['attention']['counts']['claimable'])
        self.assertIsNotNone(second.data['generated_at'])

    def test_agent_claims_and_owner_sees_changes_requested_with_prompt(self):
        task = self.create_task(self.alex, self.project, 'first').data
        claimed = self.request('POST', '/v1/projects/%s/tasks/%s/claim'
                               % (self.project, task['id']), token=self.secret)
        self.assertEqual(200, claimed.status, claimed.data)
        self.assertEqual(self.agent_id, claimed.data['assignee'])
        contributed = self.request(
            'POST', '/v1/projects/%s/tasks/%s/reviews' % (self.project, task['id']),
            {'operation': 'contribute', 'commit': COMMIT, 'base_commit': BASE,
             'bundle_sha256': BUNDLE, 'summary': 'agent delivered a slice'}, token=self.secret)
        self.assertEqual(201, contributed.status, contributed.data)
        waiting = self.request('GET', '/v1/agents/me/next', token=self.secret).data
        self.assertEqual('waiting-review', waiting['attention']['state'])
        changed = self.request(
            'POST', '/v1/projects/%s/tasks/%s/reviews' % (self.project, task['id']),
            {'operation': 'request-changes', 'summary': 'please revise'}, token=self.alex)
        self.assertEqual(201, changed.status, changed.data)
        agent = self.request('GET', '/v1/agents/me/next', token=self.secret).data
        self.assertEqual('changes-requested', agent['attention']['state'])
        self.assertEqual('changes-requested', agent['next_actions'][0]['kind'])
        # The owner's "Your agents" payload carries attention, directory and prompt.
        owner = self.request('GET', '/v1/agents', token=self.alex)
        self.assertEqual(200, owner.status, owner.data)
        item = owner.data['items'][0]
        self.assertEqual('changes-requested', item['attention']['state'])
        self.assertEqual('/home/priya/work/kestrel', item['working_directory'])
        self.assertIn(self.agent_id, item['resume_prompt'])
        self.assertIn('/home/priya/work/kestrel', item['resume_prompt'])
        self.assertIn('ORCHESTRA_AGENT_SECRET', item['resume_prompt'])
        self.assertNotIn(self.secret, json.dumps(owner.data))

    def test_agent_scope_and_role_cap(self):
        read_only = self.request('POST', '/v1/agents/%s/credentials' % self.agent_id,
                                 {'scopes': ['read']}, token=self.alex)
        self.assertEqual(201, read_only.status, read_only.data)
        limited = read_only.data['credential']['secret']
        self.assertEqual(200, self.request('GET', '/v1/projects/%s/tasks' % self.project,
                                           token=limited).status)
        denied = self.request('POST', '/v1/projects/%s/feedback' % self.project,
                              {'text': 'should be denied'}, token=limited)
        self.assertEqual(403, denied.status, denied.data)

    def test_agent_credential_cannot_manage_agents(self):
        self.assertEqual(403, self.request('POST', '/v1/agents', {'name': 'X'},
                                           token=self.secret).status)
        self.assertEqual(403, self.request('GET', '/v1/agents', token=self.secret).status)
        self.assertEqual(403, self.request('GET', '/v1/agents/%s' % self.agent_id,
                                           token=self.secret).status)

    def test_session_cannot_use_the_agent_self_route(self):
        self.assertEqual(403, self.request('GET', '/v1/agents/me', token=self.alex).status)
        self.assertEqual(403, self.request('GET', '/v1/agents/me/next',
                                           token=self.alex).status)

    def test_cross_project_access_is_denied(self):
        beta = self.create_project(self.alex, 'Beta')
        self.assertEqual(404, self.request('GET', '/v1/projects/%s/tasks' % beta,
                                           token=self.secret).status)
        # Widening then narrowing the grant takes effect on the next request.
        widened = self.request('PATCH', '/v1/agents/%s' % self.agent_id,
                               {'projects': [self.project, beta]}, token=self.alex)
        self.assertEqual(200, widened.status, widened.data)
        self.assertEqual(200, self.request('GET', '/v1/projects/%s/tasks' % beta,
                                           token=self.secret).status)
        narrowed = self.request('PATCH', '/v1/agents/%s' % self.agent_id,
                                {'projects': [self.project]}, token=self.alex)
        self.assertEqual(200, narrowed.status, narrowed.data)
        self.assertEqual(404, self.request('GET', '/v1/projects/%s/tasks' % beta,
                                           token=self.secret).status)

    def test_owner_cannot_grant_a_project_they_do_not_belong_to(self):
        self.create_account(self.admin, 'blair', 'blair-password-1')
        blair_token, _ = self.login('blair', 'blair-password-1')
        blair_project = self.create_project(blair_token, 'Beta')
        refused = self.request('POST', '/v1/agents',
                               {'name': 'Intruder', 'projects': [blair_project]},
                               token=self.alex)
        self.assertEqual(404, refused.status, refused.data)

    def test_grant_of_unknown_project_is_refused(self):
        unknown = self.request('POST', '/v1/agents',
                               {'name': 'Ghost', 'projects': ['proj_missing']}, token=self.alex)
        self.assertEqual(404, unknown.status, unknown.data)


class AgentRevocationTests(AgentHarness):
    def test_revoke_credential_and_disable_agent_stop_it(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        agent_id, secret, data = self.agent_secret(alex, projects=[project])
        credential_id = data['credential']['id']
        self.assertEqual(200, self.request('GET', '/v1/agents/me', token=secret).status)
        revoked = self.request('POST', '/v1/agents/%s/credentials/%s/revoke'
                               % (agent_id, credential_id), token=alex)
        self.assertEqual(204, revoked.status, revoked.body)
        self.assertEqual(401, self.request('GET', '/v1/agents/me', token=secret).status)
        # A disabled agent stops even a fresh credential.
        fresh = self.request('POST', '/v1/agents/%s/credentials' % agent_id, {}, token=alex)
        self.assertEqual(201, fresh.status, fresh.data)
        secret = fresh.data['credential']['secret']
        disabled = self.request('POST', '/v1/agents/%s/disable' % agent_id, token=alex)
        self.assertEqual(200, disabled.status, disabled.data)
        self.assertFalse(disabled.data['enabled'])
        self.assertEqual(401, self.request('GET', '/v1/agents/me', token=secret).status)
        self.assertEqual(401, self.request('GET', '/v1/projects/%s/tasks' % project,
                                           token=secret).status)
        # Re-enabling does not resurrect the revoked credentials.
        self.assertEqual(200, self.request('POST', '/v1/agents/%s/enable' % agent_id,
                                           token=alex).status)
        self.assertEqual(401, self.request('GET', '/v1/agents/me', token=secret).status)
        self.assertTrue(self.request('GET', '/v1/agents/%s' % agent_id,
                                     token=alex).data['enabled'])

    def test_disabling_the_owner_stops_the_agent(self):
        admin = self.admin_token()
        user_id = self.create_account(admin, 'alex', 'alex-password-1')
        self.create_account(admin, 'blair', 'blair-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        _, blair_user = self.login('blair', 'blair-password-1')
        project = self.create_project(alex, 'Alpha')
        agent_id, secret, _ = self.agent_secret(alex, projects=[project])
        self.assertEqual(200, self.request('GET', '/v1/agents/me', token=secret).status)
        # Alex must not be the final active owner before the account can be disabled.
        promoted = self.request('PUT', '/v1/projects/%s/members/%s'
                                % (project, blair_user['id']), {'role': 'owner'}, token=admin)
        self.assertEqual(200, promoted.status, promoted.data)
        disabled = self.request('POST', '/v1/accounts/%s/disable' % user_id, token=admin)
        self.assertEqual(200, disabled.status, disabled.data)
        self.assertEqual(401, self.request('GET', '/v1/agents/me', token=secret).status)
        self.assertEqual(401, self.request('GET', '/v1/projects/%s/tasks' % project,
                                           token=secret).status)

    def test_removing_the_owner_membership_stops_the_agent(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.create_account(admin, 'blair', 'blair-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        _, alex_user = self.login('alex', 'alex-password-1')
        _, blair_user = self.login('blair', 'blair-password-1')
        project = self.create_project(alex, 'Alpha')
        agent_id, secret, _ = self.agent_secret(alex, projects=[project])
        promoted = self.request('PUT', '/v1/projects/%s/members/%s'
                                % (project, blair_user['id']), {'role': 'owner'}, token=admin)
        self.assertEqual(200, promoted.status, promoted.data)
        removed = self.request('DELETE', '/v1/projects/%s/members/%s'
                               % (project, alex_user['id']), token=admin)
        self.assertEqual(200, removed.status, removed.data)
        self.assertEqual(403, self.request('GET', '/v1/projects/%s/tasks' % project,
                                           token=secret).status)

    def test_live_role_cap_follows_a_demotion(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.create_account(admin, 'blair', 'blair-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        blair_token, blair_user = self.login('blair', 'blair-password-1')
        project = self.create_project(alex, 'Alpha')
        self.request('PUT', '/v1/projects/%s/members/%s' % (project, blair_user['id']),
                     {'role': 'viewer'}, token=alex)
        granted = self.request('POST', '/v1/agents',
                               {'name': 'Blair-agent', 'projects': [project]},
                               token=blair_token)
        self.assertEqual(201, granted.status, granted.data)
        secret = granted.data['credential']['secret']
        task = self.request('POST', '/v1/projects/%s/tasks' % project,
                            {'title': 'viewer may not claim'}, token=alex).data
        self.assertEqual(200, self.request('GET', '/v1/projects/%s/tasks' % project,
                                           token=secret).status)
        denied = self.request('POST', '/v1/projects/%s/tasks/%s/claim'
                              % (project, task['id']), token=secret)
        self.assertEqual(403, denied.status, denied.data)


class AgentPrivacyTests(AgentHarness):
    def test_path_is_private_to_owner_and_superuser(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.create_account(admin, 'blair', 'blair-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        blair = self.login('blair', 'blair-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        agent_id, _, _ = self.agent_secret(alex, projects=[project])
        # Another ordinary user sees nothing at all, and cannot probe the id.
        other = self.request('GET', '/v1/agents', token=blair)
        self.assertEqual(200, other.status)
        self.assertEqual([], other.data['items'])
        self.assertNotIn('kestrel', json.dumps(other.data))
        self.assertEqual(404, self.request('GET', '/v1/agents/%s' % agent_id,
                                           token=blair).status)
        # The superuser may see every agent, including the directory hint.
        superuser = self.request('GET', '/v1/agents', token=admin)
        self.assertEqual(200, superuser.status)
        self.assertEqual(1, superuser.data['total'])
        self.assertEqual('/home/priya/work/kestrel',
                         superuser.data['items'][0]['working_directory'])
        # No agent response body ever carries the secret.
        self.assertNotIn('secret"', json.dumps(superuser.data))


class NoScheduledWorkTests(AgentHarness):
    def test_agent_attention_uses_no_scheduler_or_poller(self):
        # The service module starts no scheduler, timer or polling loop: attention is
        # a plain computation at request time.
        source = (ROOT / 'http_service.py').read_text(encoding='utf-8')
        for forbidden in ('threading.Timer', 'import sched', 'sched.scheduler',
                          'while True', 'asyncio.sleep'):
            self.assertNotIn(forbidden, source)
        self.assertFalse(hasattr(http_service, 'sched'))
        # And the contract is read-time: a mutation is reflected on the next read.
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        agent_id, secret, _ = self.agent_secret(alex, projects=[project])
        before = self.request('GET', '/v1/agents/me/next', token=secret).data
        self.assertEqual(0, before['attention']['counts']['claimable'])
        self.request('POST', '/v1/projects/%s/tasks' % project, {'title': 'new work'},
                     token=alex)
        after = self.request('GET', '/v1/agents/me/next', token=secret).data
        self.assertEqual(1, after['attention']['counts']['claimable'])


if __name__ == '__main__':
    unittest.main()
