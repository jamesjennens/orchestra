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
from http_auth import AGENT_SECRET_ENV, Service, Store
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
    #: Some suites count backend reads; the default is the untraced in-process backend.
    backend_class = InProcessBackend

    def setUp(self):
        self.tmp_path = unique_dir('agent-')
        self.addCleanup(shutil.rmtree, self.tmp_path, ignore_errors=True)
        self.store = Store(self.tmp_path / 'state.json')
        self.admin_user = Service.bootstrap_superuser(self.store, ADMIN, ADMIN_PASSWORD)
        self.service = Service(self.store, public_url='https://office.example.invalid')
        self.backend = self.backend_class(self.service)
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


class AgentSecretGuidanceTests(AgentHarness):
    """Owner decision 9: recommend a secret store; an env var is only a fallback.

    The one-time secret must never be shown being assigned on a command line, and
    ``.orchestra/agent.json`` must stay secretless while the secret itself is still
    returned exactly once.
    """

    DOCS = ('docs/HTTP_DEPLOYMENT.md', 'docs/HTTP_TRANSPORT_DESIGN.md')

    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.alex = self.login('alex', 'alex-password-1')[0]

    def test_setup_snippet_leads_with_a_secret_store_and_no_literal_export(self):
        created = self.create_agent(self.alex)
        self.assertEqual(201, created.status, created.data)
        setup = created.data['setup']
        snippet = setup['setup_snippet']
        secret = created.data['credential']['secret']
        self.assertTrue(secret)
        # No command ever carries the literal secret, and no shell export at all.
        self.assertNotIn(secret, json.dumps(setup))
        self.assertNotRegex(snippet, r'export\s+\w+=')
        self.assertNotIn('%s=' % AGENT_SECRET_ENV, snippet)
        # The store is named first; the environment variable is only the fallback.
        self.assertIn('VS Code secret storage', snippet)
        self.assertIn('credential store', snippet)
        self.assertIn('environment variable', snippet)
        self.assertLess(snippet.index('VS Code secret storage'),
                        snippet.index(AGENT_SECRET_ENV))
        self.assertIn('VS Code secret storage', setup['guidance'])
        self.assertLess(setup['guidance'].index('VS Code secret storage'),
                        setup['guidance'].index(AGENT_SECRET_ENV))
        # The config file stays secretless and the secret is still shown once.
        self.assertFalse(setup['config_contains_secret'])
        self.assertNotIn('secret', setup['config'])
        self.assertTrue(created.data['secret_available'])

    def test_secret_is_returned_exactly_once(self):
        body = {'name': 'Kestrel', 'working_directory': '/home/priya/kestrel'}
        first = self.request('POST', '/v1/agents', body, token=self.alex,
                             key='guidance-key-0001')
        self.assertEqual(201, first.status, first.data)
        secret = first.data['credential']['secret']
        self.assertTrue(secret)
        replay = self.request('POST', '/v1/agents', body, token=self.alex,
                              key='guidance-key-0001')
        self.assertEqual(200, replay.status, replay.data)
        self.assertNotIn('secret', replay.data['credential'])
        self.assertFalse(replay.data['credential']['secret_available'])
        self.assertFalse(replay.data['setup']['config_contains_secret'])
        self.assertNotIn(secret, json.dumps(replay.data))
        listing = self.request('GET', '/v1/agents', token=self.alex)
        self.assertEqual(200, listing.status, listing.data)
        self.assertNotIn(secret, json.dumps(listing.data))

    def test_docs_recommend_the_secret_store_before_the_environment_fallback(self):
        phrase = ('VS Code secret storage or the OS credential store; an environment '
                  'variable is only a documented fallback')
        for name in self.DOCS:
            flat = ' '.join((ROOT / name).read_text(encoding='utf-8').split())
            self.assertIn(phrase, flat, name)
            self.assertNotIn('export %s' % AGENT_SECRET_ENV, flat, name)
            self.assertNotIn("setup snippet's export", flat, name)


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


class ProjectOwnerAgentTests(AgentHarness):
    """Owner decision 4: a project owner/admin sees and revokes agents in it.

    The boundary is the project's own administration capability, so a project owner
    governs which agents may work in their project without gaining any control over
    an agent they do not own - and never sees its ``working_directory``.
    """

    #: The only fields a project owner may receive about an agent in their project.
    SAFE_FIELDS = {'id', 'name', 'owner', 'owner_display_name', 'actor', 'enabled',
                   'last_seen_at'}

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.create_account(self.admin, 'alex', 'alex-password-1')
        self.create_account(self.admin, 'blair', 'blair-password-1')
        self.create_account(self.admin, 'carol', 'carol-password-1')
        self.alex, self.alex_user = self.login('alex', 'alex-password-1')
        self.blair, self.blair_user = self.login('blair', 'blair-password-1')
        self.carol = self.login('carol', 'carol-password-1')[0]
        self.alpha = self.create_project(self.alex, 'Alpha')
        self.beta = self.create_project(self.alex, 'Beta')
        # Blair is an ordinary contributor on Alpha: a member, but not its owner.
        member = self.request('PUT', '/v1/projects/%s/members/%s'
                              % (self.alpha, self.blair_user['id']),
                              {'role': 'contributor'}, token=self.alex)
        self.assertEqual(200, member.status, member.data)
        self.agent_id, self.secret, _ = self.agent_secret(
            self.alex, name='Kestrel', projects=[self.alpha, self.beta])
        for project, title in ((self.alpha, 'ALPHA-WORK'), (self.beta, 'BETA-WORK')):
            created = self.request('POST', '/v1/projects/%s/tasks' % project,
                                   {'title': title}, token=self.alex)
            self.assertEqual(201, created.status, created.data)

    def assert_safe_view(self, agent):
        self.assertEqual(self.SAFE_FIELDS, set(agent))
        self.assertNotIn('working_directory', agent)
        # "hidden" is still a disclosure that a path exists.
        self.assertNotIn('working_directory_hidden', agent)

    def test_project_owner_lists_agents_without_the_working_directory(self):
        listed = self.request('GET', '/v1/projects/%s/agents' % self.alpha, token=self.alex)
        self.assertEqual(200, listed.status, listed.data)
        self.assertEqual(self.alpha, listed.data['project'])
        self.assertEqual(1, listed.data['total'])
        item = listed.data['items'][0]
        self.assert_safe_view(item)
        self.assertEqual(self.agent_id, item['id'])
        self.assertEqual('Kestrel', item['name'])
        self.assertEqual(self.alex_user['id'], item['owner'])
        self.assertEqual('alex', item['owner_display_name'])
        self.assertEqual(self.agent_id, item['actor'])
        self.assertTrue(item['enabled'])
        self.assertIn('last_seen_at', item)
        self.assertNotIn('/home/priya', listed.body.decode('utf-8'))
        self.assertNotIn('working_directory', listed.body.decode('utf-8'))
        # A single-agent project view is just as narrow.
        single = self.request('GET', '/v1/projects/%s/agents/%s'
                              % (self.alpha, self.agent_id), token=self.alex)
        self.assertEqual(200, single.status, single.data)
        self.assert_safe_view(single.data['agent'])
        self.assertNotIn('/home/priya', single.body.decode('utf-8'))
        self.assertNotIn('working_directory', single.body.decode('utf-8'))
        # A superuser holds the same project administration boundary.
        as_admin = self.request('GET', '/v1/projects/%s/agents' % self.alpha, token=self.admin)
        self.assertEqual(200, as_admin.status, as_admin.data)

    def test_listing_is_scoped_to_the_project_grant(self):
        other = self.create_agent(self.alex, name='Merlin', projects=[self.beta])
        self.assertEqual(201, other.status, other.data)
        alpha = self.request('GET', '/v1/projects/%s/agents' % self.alpha, token=self.alex)
        self.assertEqual([self.agent_id], [a['id'] for a in alpha.data['items']])
        beta = self.request('GET', '/v1/projects/%s/agents' % self.beta, token=self.alex)
        self.assertEqual({self.agent_id, other.data['agent']['id']},
                         {a['id'] for a in beta.data['items']})
        # An agent that is not in the project is reported exactly like a missing one.
        self.assertEqual(404, self.request('GET', '/v1/projects/%s/agents/%s'
                                           % (self.alpha, other.data['agent']['id']),
                                           token=self.alex).status)

    def test_non_owner_member_cannot_list_or_revoke(self):
        self.assertEqual(403, self.request('GET', '/v1/projects/%s/agents' % self.alpha,
                                           token=self.blair).status)
        self.assertEqual(403, self.request('GET', '/v1/projects/%s/agents/%s'
                                           % (self.alpha, self.agent_id),
                                           token=self.blair).status)
        refused = self.request('DELETE', '/v1/projects/%s/agents/%s'
                               % (self.alpha, self.agent_id), token=self.blair)
        self.assertEqual(403, refused.status, refused.data)
        # A non-member learns nothing at all (no existence probe).
        self.assertEqual(404, self.request('GET', '/v1/projects/%s/agents' % self.alpha,
                                           token=self.carol).status)
        self.assertEqual(404, self.request('DELETE', '/v1/projects/%s/agents/%s'
                                           % (self.alpha, self.agent_id),
                                           token=self.carol).status)
        # Nothing changed, and an agent credential never holds project admin.
        detail = self.request('GET', '/v1/agents/%s' % self.agent_id, token=self.alex)
        self.assertEqual([self.alpha, self.beta], detail.data['projects'])
        self.assertEqual(403, self.request('GET', '/v1/projects/%s/agents' % self.alpha,
                                           token=self.secret).status)
        self.assertEqual(403, self.request('DELETE', '/v1/projects/%s/agents/%s'
                                           % (self.alpha, self.agent_id),
                                           token=self.secret).status)

    def test_project_owner_revokes_one_project_live_with_audit(self):
        revoked = self.request('DELETE', '/v1/projects/%s/agents/%s'
                               % (self.alpha, self.agent_id), token=self.alex)
        self.assertEqual(200, revoked.status, revoked.data)
        self.assertEqual(self.alpha, revoked.data['project'])
        self.assertEqual('agents.project.revoke', revoked.data['operation'])
        self.assert_safe_view(revoked.data['agent'])
        # Only the grant narrowed: Beta stays, the agent stays enabled, and its own
        # owner keeps every other control.
        detail = self.request('GET', '/v1/agents/%s' % self.agent_id, token=self.alex)
        self.assertEqual([self.beta], detail.data['projects'])
        self.assertTrue(detail.data['enabled'])
        renamed = self.request('PATCH', '/v1/agents/%s' % self.agent_id,
                               {'name': 'Kestrel-2'}, token=self.alex)
        self.assertEqual(200, renamed.status, renamed.data)
        self.assertEqual('Kestrel-2', renamed.data['name'])
        # Live authorization: the next request on Alpha is refused...
        self.assertEqual(404, self.request('GET', '/v1/projects/%s/tasks' % self.alpha,
                                           token=self.secret).status)
        self.assertEqual(404, self.request('POST', '/v1/projects/%s/tasks' % self.alpha,
                                           {'title': 'nope'}, token=self.secret).status)
        # ...Beta still works...
        self.assertEqual(200, self.request('GET', '/v1/projects/%s/tasks' % self.beta,
                                           token=self.secret).status)
        # ...and /next drops Alpha from the grant, the projects list and attention.
        nxt = self.request('GET', '/v1/agents/me/next', token=self.secret)
        self.assertEqual(200, nxt.status, nxt.data)
        self.assertEqual([self.beta], [p['id'] for p in nxt.data['projects']])
        self.assertEqual([self.beta], nxt.data['agent']['projects'])
        self.assertEqual(1, nxt.data['attention']['counts']['claimable'])
        self.assertNotIn('ALPHA-WORK', nxt.body.decode('utf-8'))
        self.assertNotIn(self.alpha, json.dumps(nxt.data['attention']))
        # The project-scoped view drops it too.
        self.assertEqual(404, self.request('GET', '/v1/projects/%s/agents/%s'
                                           % (self.alpha, self.agent_id),
                                           token=self.alex).status)
        self.assertEqual(0, self.request('GET', '/v1/projects/%s/agents' % self.alpha,
                                         token=self.alex).data['total'])
        # The audit names the project and the acting user.
        audit = self.request('GET', '/v1/projects/%s/audit' % self.alpha, token=self.alex)
        self.assertEqual(200, audit.status, audit.data)
        events = [e for e in audit.data['items']
                  if e.get('action') == 'agents.project.revoke'
                  and e.get('outcome') == 'committed']
        self.assertEqual(1, len(events), audit.data)
        self.assertEqual(self.alpha, events[0]['project_id'])
        self.assertEqual(self.alex_user['id'], events[0]['user_id'])


class AgentAttentionAuthorizationTests(AgentHarness):
    """Attention must apply the same authorization check as the task routes.

    A stored project grant is a ceiling, not a licence to read: the owner leaving the
    project, or a superuser storing a project outside the owner's membership, must
    never put that project's tasks into an attention or next-action response.
    """

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.create_account(self.admin, 'alex', 'alex-password-1')
        self.create_account(self.admin, 'blair', 'blair-password-1')
        self.alex = self.login('alex', 'alex-password-1')[0]
        _, self.alex_user = self.login('alex', 'alex-password-1')
        self.blair, self.blair_user = self.login('blair', 'blair-password-1')
        self.alpha = self.create_project(self.alex, 'Alpha')
        # Blair must be an owner before Alex can be removed from Alpha.
        promoted = self.request('PUT', '/v1/projects/%s/members/%s'
                                % (self.alpha, self.blair_user['id']), {'role': 'owner'},
                                token=self.admin)
        self.assertEqual(200, promoted.status, promoted.data)
        self.agent_id, self.secret, _ = self.agent_secret(self.alex, projects=[self.alpha])

    def test_attention_hides_a_project_the_owner_was_removed_from(self):
        removed = self.request('DELETE', '/v1/projects/%s/members/%s'
                               % (self.alpha, self.alex_user['id']), token=self.admin)
        self.assertEqual(200, removed.status, removed.data)
        task = self.request('POST', '/v1/projects/%s/tasks' % self.alpha,
                            {'title': 'CONFIDENTIAL-after-removal'}, token=self.blair)
        self.assertEqual(201, task.status, task.data)
        # The direct task route refuses the agent credential...
        self.assertEqual(403, self.request('GET', '/v1/projects/%s/tasks' % self.alpha,
                                           token=self.secret).status)
        # ...and the agent's own next-action read must not disclose that project.
        nxt = self.request('GET', '/v1/agents/me/next', token=self.secret)
        self.assertEqual(200, nxt.status, nxt.data)
        self.assertEqual([], nxt.data['projects'])
        self.assertEqual('idle', nxt.data['attention']['state'])
        self.assertEqual(0, nxt.data['attention']['counts']['claimable'])
        self.assertIsNone(nxt.data['next_action'])
        self.assertNotIn('CONFIDENTIAL-after-removal', nxt.body.decode('utf-8'))
        self.assertNotIn(task.data['id'], nxt.body.decode('utf-8'))
        self.assertNotIn(self.alpha, json.dumps(nxt.data['attention']))
        # The owner's own "Your agents" view is filtered the same way.
        owner = self.request('GET', '/v1/agents', token=self.alex)
        self.assertEqual(200, owner.status, owner.data)
        item = owner.data['items'][0]
        self.assertEqual('idle', item['attention']['state'])
        self.assertEqual(0, item['attention']['counts']['claimable'])
        self.assertNotIn('CONFIDENTIAL-after-removal', owner.body.decode('utf-8'))
        self.assertNotIn(' in project %s.' % self.alpha, item['resume_prompt'])
        detail = self.request('GET', '/v1/agents/%s' % self.agent_id, token=self.alex)
        self.assertEqual(200, detail.status, detail.data)
        self.assertEqual('idle', detail.data['attention']['state'])
        self.assertNotIn('CONFIDENTIAL-after-removal', detail.body.decode('utf-8'))

    def test_superuser_cannot_grant_a_project_outside_the_owners_membership(self):
        gamma = self.create_project(self.blair, 'Gamma')
        seeded = self.request('POST', '/v1/projects/%s/tasks' % gamma,
                              {'title': 'GAMMA-PRIVATE'}, token=self.blair)
        self.assertEqual(201, seeded.status, seeded.data)
        # A superuser may not store a project the agent's owner cannot open.
        refused = self.request('PATCH', '/v1/agents/%s' % self.agent_id,
                               {'projects': [self.alpha, gamma]}, token=self.admin)
        self.assertEqual(404, refused.status, refused.data)
        detail = self.request('GET', '/v1/agents/%s' % self.agent_id, token=self.alex)
        self.assertEqual([self.alpha], detail.data['projects'])
        nxt = self.request('GET', '/v1/agents/me/next', token=self.secret)
        self.assertEqual(200, nxt.status, nxt.data)
        self.assertNotIn('GAMMA-PRIVATE', nxt.body.decode('utf-8'))
        self.assertEqual([self.alpha], [p['id'] for p in nxt.data['projects']])
        self.assertEqual(0, nxt.data['attention']['counts']['claimable'])
        # The superuser path still works where the owner *is* a member: rename and
        # re-grant Alpha, and administer an agent whose owner belongs to Gamma.
        allowed = self.request('PATCH', '/v1/agents/%s' % self.agent_id,
                               {'projects': [self.alpha], 'notes': 'reviewed'},
                               token=self.admin)
        self.assertEqual(200, allowed.status, allowed.data)
        self.assertEqual('reviewed', allowed.data['notes'])
        blair_agent = self.create_agent(self.blair, name='Blair-agent', projects=[gamma])
        self.assertEqual(201, blair_agent.status, blair_agent.data)
        blair_grant = self.request('PATCH', '/v1/agents/%s' % blair_agent.data['agent']['id'],
                                   {'projects': [gamma]}, token=self.admin)
        self.assertEqual(200, blair_grant.status, blair_grant.data)

    def test_attention_follows_a_narrowed_grant(self):
        beta = self.create_project(self.alex, 'Beta')
        widened = self.request('PATCH', '/v1/agents/%s' % self.agent_id,
                               {'projects': [self.alpha, beta]}, token=self.alex)
        self.assertEqual(200, widened.status, widened.data)
        seeded = self.request('POST', '/v1/projects/%s/tasks' % beta, {'title': 'BETA-WORK'},
                              token=self.alex)
        self.assertEqual(201, seeded.status, seeded.data)
        before = self.request('GET', '/v1/agents/me/next', token=self.secret)
        self.assertEqual(1, before.data['attention']['counts']['claimable'])
        narrowed = self.request('PATCH', '/v1/agents/%s' % self.agent_id,
                                {'projects': [self.alpha]}, token=self.alex)
        self.assertEqual(200, narrowed.status, narrowed.data)
        after = self.request('GET', '/v1/agents/me/next', token=self.secret)
        self.assertEqual(0, after.data['attention']['counts']['claimable'])
        self.assertNotIn('BETA-WORK', after.body.decode('utf-8'))


class AgentPaginationTests(AgentHarness):
    """Attention reads every page; only the claimable suggestions are capped."""

    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.alex = self.login('alex', 'alex-password-1')[0]
        self.project = self.create_project(self.alex, 'Alpha')
        self.agent_id, self.secret, _ = self.agent_secret(self.alex, projects=[self.project])

    def create_task(self, title):
        response = self.request('POST', '/v1/projects/%s/tasks' % self.project,
                                {'title': title}, token=self.alex)
        self.assertEqual(201, response.status, response.data)
        return response.data['id']

    def review(self, task, operation, token, **extra):
        payload = {'operation': operation, **extra}
        response = self.request('POST', '/v1/projects/%s/tasks/%s/reviews'
                                % (self.project, task), payload, token=token)
        self.assertEqual(201, response.status, response.data)
        return response.data

    def test_own_changes_requested_task_beyond_the_first_page_is_found(self):
        ids = [self.create_task('t%03d' % index) for index in range(130)]
        own = max(ids)
        # The agent's own task is deliberately the one that sorts last by id, i.e.
        # beyond the first MAX_PAGE page of the project read.
        self.assertEqual(130, sorted(ids).index(own) + 1)
        claim = self.request('POST', '/v1/projects/%s/tasks/%s/claim'
                             % (self.project, own), token=self.secret)
        self.assertEqual(200, claim.status, claim.data)
        self.review(own, 'contribute', self.secret, commit=COMMIT, base_commit=BASE,
                    bundle_sha256=BUNDLE, summary='agent slice')
        self.review(own, 'request-changes', self.alex, summary='please revise')
        nxt = self.request('GET', '/v1/agents/me/next', token=self.secret)
        self.assertEqual(200, nxt.status, nxt.data)
        attention = nxt.data['attention']
        counts = attention['counts']
        self.assertEqual('changes-requested', attention['state'])
        self.assertEqual(1, counts['changes_requested'])
        self.assertEqual(1, counts['claimed'])
        self.assertEqual(129, counts['claimable'])
        self.assertEqual('changes-requested', nxt.data['next_action']['kind'])
        self.assertIn(own, [action['task'] for action in nxt.data['next_actions']])
        # ``truncated`` now describes the capped *claimable suggestion* list only: the
        # agent's own feedback is still first and complete.
        self.assertTrue(attention['truncated'])
        self.assertEqual(own, nxt.data['next_actions'][0]['task'])
        self.assertEqual('changes-requested', nxt.data['next_actions'][0]['kind'])
        owner = self.request('GET', '/v1/agents', token=self.alex)
        self.assertEqual('changes-requested', owner.data['items'][0]['attention']['state'])
        self.assertIn(own, owner.data['items'][0]['resume_prompt'])

    def test_every_page_is_read_not_just_the_first(self):
        # 130 open, unclaimed tasks: the exact count proves the whole project was read
        # even though only AGENT_CLAIMABLE_LIMIT suggestions are collected.
        for index in range(130):
            self.create_task('open-%03d' % index)
        nxt = self.request('GET', '/v1/agents/me/next', token=self.secret)
        self.assertEqual(130, nxt.data['attention']['counts']['claimable'])
        self.assertTrue(nxt.data['attention']['truncated'])
        self.assertEqual(http_service.AGENT_ACTION_LIMIT,
                         len([action for action in nxt.data['next_actions']
                              if action['kind'] == 'claimable-task']))


class CountingBackend(InProcessBackend):
    """In-process backend that counts canonical task reads, the cost proxy for the
    endpoint binding (which spawns ``endpoint.py`` and ``bd list --all`` per read)."""

    def __init__(self, service):
        super().__init__(service)
        #: Every canonical task read, paginated or full, in call order.
        self.list_calls = []
        #: Only full-snapshot reads (``read_tasks``): what attention must use once.
        self.read_calls = []

    def read_tasks(self, project_id):
        self.read_calls.append(project_id)
        self.list_calls.append((project_id, None, 0))
        return super().read_tasks(project_id)

    def list_tasks(self, project_id, limit, offset):
        self.list_calls.append((project_id, limit, offset))
        return super().list_tasks(project_id, limit, offset)


class AgentReadCostTests(AgentHarness):
    """One project read per project per request, never per agent or across requests."""

    backend_class = CountingBackend

    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.alex = self.login('alex', 'alex-password-1')[0]
        self.projects = [self.create_project(self.alex, 'P%d' % index) for index in range(5)]
        for index in range(10):
            created = self.create_agent(self.alex, name='A%d' % index,
                                        projects=list(self.projects))
            self.assertEqual(201, created.status, created.data)

    def test_owner_list_reads_each_project_once_per_request(self):
        self.backend.list_calls = []
        listing = self.request('GET', '/v1/agents', token=self.alex)
        self.assertEqual(200, listing.status, listing.data)
        self.assertEqual(10, listing.data['total'])
        # 10 agents x 5 projects would be 50 reads; the request performs 5.
        self.assertEqual(5, len(self.backend.list_calls))
        self.assertEqual(set(self.projects), {call[0] for call in self.backend.list_calls})
        # The cache never outlives the request: the next read re-reads every project.
        self.backend.list_calls = []
        self.request('GET', '/v1/agents', token=self.alex)
        self.assertEqual(5, len(self.backend.list_calls))

    def test_agent_next_reads_every_granted_project_once(self):
        agent_id, secret, _ = self.agent_secret(self.alex, projects=list(self.projects))
        self.assertTrue(agent_id)
        self.backend.list_calls = []
        nxt = self.request('GET', '/v1/agents/me/next', token=secret)
        self.assertEqual(200, nxt.status, nxt.data)
        self.assertEqual(5, len(self.backend.list_calls))

    def test_a_project_bigger_than_one_page_still_costs_one_read(self):
        # 130 tasks in one project: the page walk used to cost one full read per
        # page (2 for this project, so 6 reads for the request). One snapshot now
        # costs one read, and the exact count proves the whole snapshot was used.
        big = self.projects[0]
        seeded = http_service.MAX_PAGE + 30
        for index in range(seeded):
            created = self.request('POST', '/v1/projects/%s/tasks' % big,
                                   {'title': 'big-%03d' % index}, token=self.alex)
            self.assertEqual(201, created.status, created.data)
        for label, path, token in (('owner list', '/v1/agents', self.alex),
                                   ('agent next', None, None)):
            if path is None:
                agent_id, secret, _ = self.agent_secret(self.alex, projects=list(self.projects))
                path, token = '/v1/agents/me/next', secret
            self.backend.list_calls = []
            self.backend.read_calls = []
            response = self.request('GET', path, token=token)
            self.assertEqual(200, response.status, response.data)
            # Exactly one read per project, the big one included; never per page.
            self.assertEqual(5, len(self.backend.read_calls), label)
            self.assertEqual(5, len(self.backend.list_calls), label)
            self.assertEqual(1, self.backend.read_calls.count(big), label)
        # The page boundary is not a read boundary: the whole project was counted.
        attention = self.request('GET', '/v1/agents/me/next', token=secret).data['attention']
        self.assertEqual(seeded, attention['counts']['claimable'])


class AgentReadBoundTests(AgentHarness):
    """The in-memory page bound keeps rev2's ``truncated`` meaning at ONE read."""

    backend_class = CountingBackend

    def test_a_snapshot_beyond_the_page_bound_is_truncated_after_one_read(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        agent_id, secret, _ = self.agent_secret(alex, projects=[project])
        self.assertTrue(agent_id)
        bound = http_service.AGENT_MAX_PAGES * http_service.MAX_PAGE
        # 20,001 HTTP creates is not viable; the canonical rows are the same shape the
        # create route writes, so seed them directly and drive the real route below.
        seeded = self.backend.state['tasks']
        for index in range(bound + 1):
            task_id = 'task_%08d' % index
            seeded[task_id] = {'id': task_id, 'project_id': project,
                               'title': 'bulk-%08d' % index, 'description': '',
                               'status': 'open', 'assignee': None, 'version': 1,
                               'created_by': 'usr_seed', 'created_at': 'seed',
                               'attachments': []}
        self.backend.list_calls = []
        self.backend.read_calls = []
        nxt = self.request('GET', '/v1/agents/me/next', token=secret)
        self.assertEqual(200, nxt.status, nxt.data)
        self.assertEqual(1, len(self.backend.read_calls))
        attention = nxt.data['attention']
        # Exactly the bound is considered - not bound+1 - and the flag still reports
        # that the project was cut off, with no second read.
        self.assertEqual(bound, attention['counts']['claimable'])
        self.assertTrue(attention['truncated'])


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
