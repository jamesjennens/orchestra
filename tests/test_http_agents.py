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
import re
import secrets
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import http_auth
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
    """Owner decision 9 as updated on 2026-09-29: a per-agent curl config file.

    The secret lives in ``%USERPROFILE%\\.orchestra-agent-<slug>.curlrc`` (Windows) or
    ``~/.orchestra-agent-<slug>.curlrc`` and every call hands it to curl with ``-K``;
    an env var is only a secondary fallback.

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
        # The per-agent file is named first (Windows steps first); the environment
        # variable is only a secondary fallback.
        windows = '%USERPROFILE%\\.orchestra-agent-kestrel.curlrc'
        self.assertEqual({'name': '.orchestra-agent-kestrel.curlrc', 'windows': windows,
                          'windows_powershell': '$env:USERPROFILE\\.orchestra-agent-kestrel.curlrc',
                          'posix': '~/.orchestra-agent-kestrel.curlrc'}, setup['secret_file'])
        self.assertIn(windows, snippet)
        self.assertIn('All files (*.*)', snippet)
        self.assertIn('curl.exe -fsS -K "$env:USERPROFILE\\.orchestra-agent-kestrel.curlrc"',
                      snippet)
        self.assertLess(snippet.index(windows), snippet.index('~/.orchestra-agent-kestrel.curlrc'))
        self.assertIn('environment variable', snippet)
        self.assertLess(snippet.index(windows), snippet.index(AGENT_SECRET_ENV))
        self.assertIn(windows, setup['guidance'])
        self.assertLess(setup['guidance'].index(windows),
                        setup['guidance'].index(AGENT_SECRET_ENV))
        self.assertNotIn('VS Code secret storage', snippet)
        # The config file stays secretless and the secret is still shown once.
        self.assertFalse(setup['config_contains_secret'])
        self.assertNotIn('secret', setup['config'])
        self.assertTrue(created.data['secret_available'])
        # The fallback hands curl a config file (-K), never a command line that a
        # shell would expand into curl's argv and the process list.
        self.assertNotRegex(snippet, r'-H\s+["\']?Authorization:\s*Bearer')
        self.assertNotRegex(snippet, r'Authorization:\s*Bearer\s+\$')
        self.assertRegex(snippet, r'curl -fsS -K \S+')
        self.assertRegex(snippet, r'header = "Authorization: Bearer <[^>]+>"')
        self.assertIn('mode 600', snippet)

    def test_no_command_line_puts_the_agent_secret_in_argv(self):
        """Item 3: the flagged shape is a bearer header on a curl command line.

        ``curl -H "Authorization: Bearer ..."`` puts the value in curl's argv; with a
        shell variable the expanded *secret* lands there and in the process list, and a
        literal token is worse. The setup snippet must hand curl a config file instead,
        and neither the snippet nor the resume prompt may carry the issued secret or a
        bearer value that is not a shell variable.
        """
        created = self.create_agent(self.alex)
        self.assertEqual(201, created.status, created.data)
        secret = created.data['credential']['secret']
        snippet = created.data['setup']['setup_snippet']
        listed = self.request('GET', '/v1/agents', token=self.alex)
        self.assertEqual(200, listed.status, listed.data)
        prompt = listed.data['items'][0]['resume_prompt']
        literal_bearer = re.compile(
            r'(?:-H|--header)\s+["\']?Authorization:\s*Bearer\s+(?!\$)')
        for text in (snippet, prompt):
            self.assertNotIn(secret, text)
            self.assertNotRegex(text, literal_bearer)
            self.assertNotIn('export %s=' % AGENT_SECRET_ENV, text)
        # The setup fallback expands nothing at all.
        self.assertNotRegex(snippet, r'-H\s+["\']?Authorization:\s*Bearer')
        self.assertNotIn('$' + AGENT_SECRET_ENV, snippet)
        self.assertRegex(snippet, r'curl -fsS -K \S+')
        # The documented example matches the snippet.
        flat = ' '.join((ROOT / 'docs/HTTP_DEPLOYMENT.md').read_text(
            encoding='utf-8').split())
        self.assertIn('curl reads the header from it with `-K`', flat)
        self.assertNotIn('-H "Authorization: Bearer', flat)

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

    def test_docs_recommend_the_secret_file_before_the_environment_fallback(self):
        phrase = ('a per-agent curl config file in the user profile '
                  '(`%USERPROFILE%\\.orchestra-agent-<name>.curlrc` on Windows, '
                  '`~/.orchestra-agent-<name>.curlrc` on macOS/Linux, mode 600)')
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
        # kittrial-5bb.48: the prompt names the per-agent secret file and hands it to
        # curl with -K; no secret or variable ever lands on the command line.
        self.assertIn('.orchestra-agent-kestrel.curlrc', item['resume_prompt'])
        self.assertIn('curl.exe -fsS -K "$env:USERPROFILE\\.orchestra-agent-kestrel.curlrc"',
                      item['resume_prompt'])
        self.assertNotIn('Authorization', item['resume_prompt'])
        self.assertNotIn('$ORCHESTRA_AGENT_SECRET', item['resume_prompt'])
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


class AgentUpdateAtomicityTests(AgentHarness):
    """A refused PATCH leaves the agent record exactly as it was (slice 2a item 1).

    Fields used to be applied one by one, name first, so a later failure left a
    half-applied record behind - including a rename, which also renames the owner's
    secret file.
    """

    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.create_account(admin, 'blair', 'blair-password-1')
        self.alex = self.login('alex', 'alex-password-1')[0]
        self.blair = self.login('blair', 'blair-password-1')[0]
        self.project = self.create_project(self.alex, 'Alpha')
        self.ungranted = self.create_project(self.blair, 'Bravo')
        self.agent_id, self.secret, _ = self.agent_secret(
            self.alex, name='Kestrel', tool='Copilot', machine='desk-01', notes='pilot',
            projects=[self.project])
        self.agent_secret(self.alex, name='Osprey')

    def record(self):
        response = self.request('GET', '/v1/agents/%s' % self.agent_id, token=self.alex)
        self.assertEqual(200, response.status, response.data)
        return {k: response.data.get(k) for k in
                ('name', 'tool', 'machine', 'notes', 'working_directory', 'projects',
                 'enabled', 'setup_secret_file')}

    def assert_refused_unchanged(self, payload, status):
        before = self.record()
        stored_before = json.dumps(self.service.state['agents'][self.agent_id],
                                   sort_keys=True)
        response = self.request('PATCH', '/v1/agents/%s' % self.agent_id, payload,
                                token=self.alex)
        self.assertEqual(status, response.status, response.data)
        self.assertEqual(before, self.record())
        self.assertEqual(stored_before,
                         json.dumps(self.service.state['agents'][self.agent_id],
                                    sort_keys=True))
        # The persisted document agrees with the live record.
        on_disk = json.loads((self.tmp_path / 'state.json').read_text(encoding='utf-8'))
        self.assertEqual('Kestrel', on_disk['agents'][self.agent_id]['name'])
        # The agent still works with its credential.
        self.assertEqual(200, self.request('GET', '/v1/agents/me', token=self.secret).status)

    def test_rename_with_an_ungranted_project_is_not_half_applied(self):
        self.assert_refused_unchanged({'name': 'Kestrel Two',
                                       'projects': [self.ungranted]}, 404)

    def test_rename_with_an_overlong_working_directory_is_not_half_applied(self):
        self.assert_refused_unchanged({'name': 'Kestrel Two', 'tool': 'Cline',
                                       'working_directory': 'x' * 5000}, 422)

    def test_rename_with_an_invalid_later_field_is_not_half_applied(self):
        self.assert_refused_unchanged({'name': 'Kestrel Two', 'notes': 'bad\x01note'}, 422)

    def test_disable_with_an_invalid_grant_revokes_nothing(self):
        self.assert_refused_unchanged({'enabled': False, 'projects': 'not-a-list'}, 422)
        self.assertTrue(self.service.state['agents'][self.agent_id]['enabled'])

    def test_clashing_rename_changes_nothing(self):
        self.assert_refused_unchanged({'name': 'osprey', 'tool': 'Cline'}, 409)

    def test_invalid_name_changes_nothing(self):
        self.assert_refused_unchanged({'name': '!!!', 'tool': 'Cline',
                                       'projects': []}, 422)

    def test_a_valid_change_applies_every_field_together(self):
        response = self.request('PATCH', '/v1/agents/%s' % self.agent_id,
                                {'name': 'Kestrel Two', 'tool': 'Cline', 'projects': []},
                                token=self.alex)
        self.assertEqual(200, response.status, response.data)
        after = self.record()
        self.assertEqual('Kestrel Two', after['name'])
        self.assertEqual('Cline', after['tool'])
        self.assertEqual([], after['projects'])


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

    @staticmethod
    def _without_request_id(body):
        return {key: value for key, value in body.items() if key != 'request_id'}

    def test_not_granted_agent_is_indistinguishable_from_a_missing_id(self):
        """Item 1: one 404 message for both project-scoped agent routes.

        An agent that exists but is granted only to Beta, probed through Alpha, must
        answer exactly like an id that does not exist - same status, same error object,
        same body apart from the per-request id - while the granted case still works.
        """
        other = self.create_agent(self.alex, name='Merlin', projects=[self.beta])
        self.assertEqual(201, other.status, other.data)
        elsewhere = other.data['agent']['id']
        missing = 'agent_no_such_agent_at_all'
        for method in ('GET', 'DELETE'):
            path = '/v1/projects/%s/agents/%%s' % self.alpha
            not_granted = self.request(method, path % elsewhere, token=self.alex)
            unknown = self.request(method, path % missing, token=self.alex)
            self.assertEqual(404, unknown.status, unknown.data)
            self.assertEqual(404, not_granted.status, not_granted.data)
            self.assertEqual(unknown.data['error'], not_granted.data['error'])
            self.assertEqual('not_found', not_granted.data['error']['code'])
            self.assertEqual('Agent not found', not_granted.data['error']['message'])
            # Nothing but the unavoidable per-request id may differ.
            self.assertEqual(self._without_request_id(unknown.data),
                             self._without_request_id(not_granted.data))
            self.assertEqual(['error', 'request_id'], sorted(not_granted.data))
        # The refused DELETE changed neither agent's grant.
        detail = self.request('GET', '/v1/agents/%s' % elsewhere, token=self.alex)
        self.assertEqual([self.beta], detail.data['projects'])
        kept = self.request('GET', '/v1/agents/%s' % self.agent_id, token=self.alex)
        self.assertEqual([self.alpha, self.beta], kept.data['projects'])
        # A granted project owner still reads and revokes normally.
        read = self.request('GET', '/v1/projects/%s/agents/%s'
                            % (self.alpha, self.agent_id), token=self.alex)
        self.assertEqual(200, read.status, read.data)
        self.assertEqual(self.agent_id, read.data['agent']['id'])
        revoked = self.request('DELETE', '/v1/projects/%s/agents/%s'
                               % (self.alpha, self.agent_id), token=self.alex)
        self.assertEqual(200, revoked.status, revoked.data)
        self.assertEqual(self.alpha, revoked.data['project'])
        self.assertEqual(self.agent_id, revoked.data['agent']['id'])

    def test_a_project_revoke_is_not_sticky_and_its_owner_can_regrant(self):
        """Item 2: pin today's behaviour - a project revoke narrows one grant only.

        Blair owns the agent and is an ordinary contributor member of Alpha; Alex owns
        Alpha. Alex's revoke takes effect live, yet Blair can add Alpha straight back
        because the agent's own owner still administers the agent. Whether a project
        owner may block that re-grant is an owner decision that is still pending, so
        this test pins the existing behaviour only.
        """
        created = self.create_agent(self.blair, name='Sparrow', projects=[self.alpha])
        self.assertEqual(201, created.status, created.data)
        agent_id = created.data['agent']['id']
        secret = created.data['credential']['secret']
        self.assertEqual(200, self.request('GET', '/v1/projects/%s/tasks' % self.alpha,
                                           token=secret).status)
        # The project's owner revokes: live, and only for this project.
        revoked = self.request('DELETE', '/v1/projects/%s/agents/%s'
                               % (self.alpha, agent_id), token=self.alex)
        self.assertEqual(200, revoked.status, revoked.data)
        self.assertEqual(self.alpha, revoked.data['project'])
        self.assertEqual(404, self.request('GET', '/v1/projects/%s/tasks' % self.alpha,
                                           token=secret).status)
        detail = self.request('GET', '/v1/agents/%s' % agent_id, token=self.blair)
        self.assertEqual([], detail.data['projects'])
        self.assertTrue(detail.data['enabled'])
        # The agent's own owner adds the project straight back: the revoke is not
        # sticky, and nothing marks the agent as barred.
        regranted = self.request('PATCH', '/v1/agents/%s' % agent_id,
                                 {'projects': [self.alpha]}, token=self.blair)
        self.assertEqual(200, regranted.status, regranted.data)
        self.assertEqual([self.alpha], regranted.data['projects'])
        self.assertEqual(200, self.request('GET', '/v1/projects/%s/tasks' % self.alpha,
                                           token=secret).status)
        # The project owner sees the grant back and can revoke again.
        listed = self.request('GET', '/v1/projects/%s/agents' % self.alpha, token=self.alex)
        self.assertIn(agent_id, [a['id'] for a in listed.data['items']])
        again = self.request('DELETE', '/v1/projects/%s/agents/%s'
                             % (self.alpha, agent_id), token=self.alex)
        self.assertEqual(200, again.status, again.data)


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
        #: Own-task reads (``agent_tasks``): on the endpoint binding each is a ``work``
        #: read. ``None`` as the actor is the one unfiltered read the owners' list makes.
        self.own_calls = []

    def agent_tasks(self, project_id, actor=None, queue=None):
        self.own_calls.append((project_id, actor))
        return super().agent_tasks(project_id, actor, queue)

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
        # The same for the own-task read (kittrial-5bb.114): one unfiltered read per
        # project for all ten agents, never one per agent.
        self.assertEqual(sorted(self.backend.own_calls), sorted((project, None) for project in self.projects))
        # The cache never outlives the request: the next read re-reads every project.
        self.backend.list_calls = []
        self.backend.own_calls = []
        self.request('GET', '/v1/agents', token=self.alex)
        self.assertEqual(5, len(self.backend.list_calls))
        self.assertEqual(5, len(self.backend.own_calls))

    def test_agent_next_reads_every_granted_project_once(self):
        agent_id, secret, _ = self.agent_secret(self.alex, projects=list(self.projects))
        self.assertTrue(agent_id)
        self.backend.list_calls = []
        self.backend.own_calls = []
        nxt = self.request('GET', '/v1/agents/me/next', token=secret)
        self.assertEqual(200, nxt.status, nxt.data)
        self.assertEqual(5, len(self.backend.list_calls))
        # One owner-filtered own-task read per project, naming this agent's actor.
        actor = nxt.data['agent']['actor']
        self.assertEqual(sorted(self.backend.own_calls), sorted((project, actor) for project in self.projects))

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


class RenewalScopeTests(AgentHarness):
    """kittrial-5bb.208: a new credential for an agent carried the default four scopes whenever no list
    was sent, and the agents page sends none: a read-only agent became a writing one when its owner
    clicked "new secret", and an agent with all six lost one. Through the real route."""

    #: What web/js/api.js sends for "Issue a new secret".
    PAGE = {'label': 'web: new secret'}
    ALL = ['checkpoints', 'feedback', 'proposals', 'read', 'reviews', 'tasks']

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.alex_id = self.create_account(self.admin, 'alex', 'alex-password-1')
        self.alex = self.login('alex', 'alex-password-1')[0]
        self.project = self.create_project(self.admin, 'Alpha')
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, self.alex_id),
                                           {'role': 'contributor'}, token=self.admin).status)

    def agent(self, scopes=None, name='Kestrel'):
        body = {'projects': [self.project], 'name': name}
        if scopes is not None:
            body['scopes'] = scopes
        agent_id, secret, data = self.agent_secret(self.alex, **body)
        return agent_id, secret, data

    def renew(self, agent_id, body=None, token=None, key=None):
        return self.request('POST', '/v1/agents/%s/credentials' % agent_id, dict(self.PAGE if body is None else body),
                            token=token or self.alex, key=key)

    def writes(self, secret):
        return self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'a task'}, token=secret).status

    def read(self, agent_id, token=None):
        return self.request('GET', '/v1/agents/%s' % agent_id, token=token or self.alex).data

    def has(self, agent_id):
        return self.read(agent_id)['scopes']

    def revoke(self, agent_id, credential_id, token=None):
        return self.request('POST', '/v1/agents/%s/credentials/%s/revoke' % (agent_id, credential_id), {},
                            token=token or self.alex)

    def working(self, agent_id):
        return [credential for credential in self.read(agent_id)['credentials'] if credential['working']]

    def made_before(self, agent_id):
        """An agent as an earlier kit left it: its record holds no scopes."""
        with self.service.store.lock:
            agent = self.service.state['agents'][agent_id]
            agent.pop('scopes', None)
            agent.pop('scopes_source', None)
            self.service.store.save()

    def stored(self, agent_id):
        with self.service.store.lock:
            agent = self.service.state['agents'][agent_id]
            return agent.get('scopes'), agent.get('scopes_source')

    def later(self, seconds):
        """The host clock moves on; everybody signs in again (their sessions are over too)."""
        self.ahead = getattr(self, 'ahead', 0) + seconds
        ahead = self.ahead
        self.service.store.clock = lambda: time.time() + ahead
        self.admin = self.admin_token()
        self.alex = self.login('alex', 'alex-password-1')[0]

    def test_a_read_only_agent_renewed_from_the_page_stays_read_only(self):
        agent_id, first, made = self.agent(['read'])
        self.assertEqual((made['credential']['scopes'], self.writes(first)), (['read'], 403))
        renewed = self.renew(agent_id)
        self.assertEqual(201, renewed.status, renewed.data)
        self.assertEqual(renewed.data['credential']['scopes'], ['read'])           # it was the default four
        again = renewed.data['credential']['secret']
        self.assertEqual(200, self.request('GET', '/v1/projects/%s/tasks' % self.project, token=again).status)
        self.assertEqual(self.writes(again), 403)                                  # it wrote a task
        self.assertEqual(self.has(agent_id), ['read'])

    def test_an_agent_with_every_scope_keeps_every_scope(self):
        agent_id, _, made = self.agent(self.ALL)
        self.assertEqual(sorted(made['credential']['scopes']), self.ALL)
        renewed = self.renew(agent_id)
        self.assertEqual(sorted(renewed.data['credential']['scopes']), self.ALL)   # it lost proposals
        # With no label and no body either, as a client may send.
        self.assertEqual(sorted(self.renew(agent_id, {}).data['credential']['scopes']), self.ALL)

    def test_an_agent_made_with_the_default_keeps_the_default(self):
        agent_id, _, made = self.agent()
        self.assertEqual(made['credential']['scopes'], ['tasks', 'checkpoints', 'reviews', 'feedback'])
        self.assertEqual(self.renew(agent_id).data['credential']['scopes'], ['tasks', 'checkpoints', 'reviews', 'feedback'])
        self.assertEqual((self.has(agent_id), self.read(agent_id)['scopes_source']),
                         (['tasks', 'checkpoints', 'reviews', 'feedback'], 'set'))

    def test_a_list_that_asks_for_no_more_is_taken_and_decides_what_the_agent_has_next(self):
        agent_id, _, _ = self.agent(self.ALL)
        narrower = self.renew(agent_id, {'scopes': ['read', 'tasks']})
        self.assertEqual(201, narrower.status, narrower.data)
        self.assertEqual(narrower.data['credential']['scopes'], ['read', 'tasks'])
        self.assertEqual(self.has(agent_id), ['read', 'tasks'])                    # the list is the agent's now
        self.assertEqual(self.renew(agent_id).data['credential']['scopes'], ['read', 'tasks'])
        # The same names in another order and one of them twice: neither asks for more. The order
        # given is the order kept, and a plain renewal issues exactly that.
        again = self.renew(agent_id, {'scopes': ['tasks', 'read', 'tasks']})
        self.assertEqual((201, ['tasks', 'read']), (again.status, again.data['credential']['scopes']))
        self.assertEqual((self.has(agent_id), self.stored(agent_id)), (['tasks', 'read'], (['tasks', 'read'], 'set')))
        self.assertEqual(self.renew(agent_id).data['credential']['scopes'], ['tasks', 'read'])
        self.assertEqual(201, self.renew(agent_id, {'scopes': ['read']}).status)
        self.assertEqual(self.has(agent_id), ['read'])

    def test_no_credential_decides_anything_once_the_record_has_the_scopes(self):
        """The review's finding: with no working credential a revoked or expired one seeded the plain
        renewal and the widening check, so a superuser got round the 403 in two requests."""
        agent_id, _, _ = self.agent(['read'])
        # Its account widens it and narrows it again; the wide credential is revoked and stays on record.
        wide = self.renew(agent_id, {'scopes': self.ALL}).data['credential']
        narrow = self.renew(agent_id, {'scopes': ['read']}).data['credential']
        self.assertEqual(204, self.revoke(agent_id, wide['id']).status)
        self.assertEqual(self.has(agent_id), ['read'])
        refused = self.renew(agent_id, {'scopes': self.ALL}, token=self.admin)
        self.assertEqual(403, refused.status, refused.data)
        # The superuser revokes every credential that works and sends the page's plain body.
        for credential in self.working(agent_id):
            self.assertEqual(204, self.revoke(agent_id, credential['id'], token=self.admin).status)
        self.assertEqual(self.working(agent_id), [])
        self.assertEqual(self.has(agent_id), ['read'])                             # it was all six: the newest dead one
        again = self.renew(agent_id, token=self.admin)
        self.assertEqual((201, ['read']), (again.status, again.data['credential']['scopes']), again.data)
        self.assertEqual(self.writes(again.data['credential']['secret']), 403)
        refused = self.renew(agent_id, {'scopes': ['read', 'tasks']}, token=self.admin)
        self.assertEqual(403, refused.status, refused.data)
        # The same after the agent was disabled and enabled, which revokes them all.
        for action in ('disable', 'enable'):
            self.assertEqual(200, self.request('POST', '/v1/agents/%s/%s' % (agent_id, action), {}, token=self.alex).status)
        self.assertEqual(self.renew(agent_id).data['credential']['scopes'], ['read'])
        self.assertEqual(narrow['scopes'], ['read'])

    def test_the_same_when_they_stopped_working_by_time(self):
        agent_id, _, _ = self.agent(['read'])
        self.renew(agent_id, {'scopes': self.ALL})
        self.renew(agent_id, {'scopes': ['read']})
        self.later(self.service.credential_ttl + 60)                               # a month and more away
        self.assertEqual((self.working(agent_id), self.has(agent_id)), ([], ['read']))
        refused = self.renew(agent_id, {'scopes': self.ALL}, token=self.admin)
        self.assertEqual(403, refused.status, refused.data)
        again = self.renew(agent_id, token=self.admin)
        self.assertEqual((201, ['read']), (again.status, again.data['credential']['scopes']), again.data)

    def test_an_agent_made_before_the_record_kept_its_scopes(self):
        """One working credential, or several that allow the same: that is what the agent has. It is
        written to the record by the first renewal, not by a read."""
        agent_id, _, _ = self.agent(['read', 'tasks'])
        self.assertEqual(201, self.renew(agent_id).status)
        self.made_before(agent_id)
        seen = self.read(agent_id)
        self.assertEqual((seen['scopes'], seen['scopes_source']), (['read', 'tasks'], 'inferred'))
        self.assertEqual(self.stored(agent_id), (None, None))                      # a read writes nothing
        renewed = self.renew(agent_id)
        self.assertEqual((201, ['read', 'tasks']), (renewed.status, renewed.data['credential']['scopes']), renewed.data)
        self.assertEqual(self.stored(agent_id), (['read', 'tasks'], 'inferred'))
        self.assertEqual(self.read(agent_id)['scopes_source'], 'inferred')
        # From here on the record decides, as for any agent.
        for credential in self.working(agent_id):
            self.revoke(agent_id, credential['id'])
        self.assertEqual(self.renew(agent_id, token=self.admin).data['credential']['scopes'], ['read', 'tasks'])
        # A list from its account makes them set, not inferred.
        self.assertEqual(201, self.renew(agent_id, {'scopes': ['read']}).status)
        self.assertEqual(self.stored(agent_id), (['read'], 'set'))

    def test_nothing_is_guessed_for_one_with_no_working_credential(self):
        agent_id, _, made = self.agent(['read'])
        wide = self.renew(agent_id, {'scopes': self.ALL}).data['credential']       # the newest, and the widest
        for credential in self.working(agent_id):
            self.revoke(agent_id, credential['id'])
        self.made_before(agent_id)
        seen = self.read(agent_id)
        self.assertEqual((seen['scopes'], seen['scopes_source'], seen['scopes_differ']), (None, 'unknown', []))
        before = len(seen['credentials'])
        for token in (self.alex, self.admin):
            refused = self.renew(agent_id, token=token)
            self.assertEqual(409, refused.status, refused.data)
            self.assertEqual(refused.data['error']['message'], http_auth.Service.SCOPES_NEEDED)
            self.assertEqual(refused.data['error']['detail'], {'scopes_needed': True})
        # A superuser's list is a widening of nothing; the narrowest one too.
        refused = self.renew(agent_id, {'scopes': ['read']}, token=self.admin)
        self.assertEqual(403, refused.status, refused.data)
        self.assertEqual(refused.data['error']['message'],
                         'This agent has no scopes on record. Only its own account may give it more (asked for beyond that: read)')
        self.assertEqual((len(self.read(agent_id)['credentials']), self.stored(agent_id)), (before, (None, None)))
        # Its own account says what it may do.
        chosen = self.renew(agent_id, {'scopes': ['read']})
        self.assertEqual((201, ['read']), (chosen.status, chosen.data['credential']['scopes']), chosen.data)
        self.assertEqual(self.stored(agent_id), (['read'], 'set'))
        self.assertEqual(self.renew(agent_id, token=self.admin).data['credential']['scopes'], ['read'])
        self.assertEqual(wide['scopes'], self.ALL)

    def test_nothing_is_guessed_for_one_whose_working_credentials_differ(self):
        """What an agent looks like that was renewed from the page before the fix: the credential it was
        made with, and a newer one with the four writing scopes."""
        agent_id, _, made = self.agent(['read'])
        harmed = self.renew(agent_id, {'scopes': ['tasks', 'checkpoints', 'reviews', 'feedback']}).data['credential']
        self.made_before(agent_id)
        seen = self.read(agent_id)
        self.assertEqual((seen['scopes'], seen['scopes_source'], len(seen['scopes_differ'])), (None, 'unknown', 2))
        refused = self.renew(agent_id)
        self.assertEqual((409, {'scopes_needed': True}), (refused.status, refused.data['error']['detail']), refused.data)
        # Its owner revokes the credential it should not have and clicks "new secret": read-only again.
        self.assertEqual(204, self.revoke(agent_id, harmed['id']).status)
        self.assertEqual(self.read(agent_id)['scopes_source'], 'inferred')
        for action in ('disable', 'enable'):
            self.assertEqual(200, self.request('POST', '/v1/agents/%s/%s' % (agent_id, action), {}, token=self.alex).status)
        # Disabled and enabled, none works: nothing is guessed, and the dead ones do not decide.
        refused = self.renew(agent_id)
        self.assertEqual(409, refused.status, refused.data)
        chosen = self.renew(agent_id, {'scopes': ['read']})
        self.assertEqual((201, 403), (chosen.status, self.writes(chosen.data['credential']['secret'])))

    def test_a_superuser_may_narrow_and_not_widen_back(self):
        agent_id, _, _ = self.agent(['read', 'tasks'])
        narrowed = self.renew(agent_id, {'scopes': ['read']}, token=self.admin)
        self.assertEqual((201, ['read']), (narrowed.status, narrowed.data['credential']['scopes']), narrowed.data)
        self.assertEqual(self.stored(agent_id), (['read'], 'set'))
        self.assertEqual(403, self.renew(agent_id, {'scopes': ['read', 'tasks']}, token=self.admin).status)
        self.assertEqual(201, self.renew(agent_id, {'scopes': ['read', 'tasks']}).status)

    def test_only_the_agents_own_account_may_give_it_more(self):
        agent_id, _, _ = self.agent(['read'])
        refused = self.renew(agent_id, {'scopes': ['read', 'tasks', 'reviews']}, token=self.admin)
        self.assertEqual(403, refused.status, refused.data)
        self.assertEqual(refused.data['error']['message'],
                         'This agent has read. Only its own account may give it more (asked for beyond that: reviews, tasks)')
        self.assertEqual(self.has(agent_id), ['read'])
        self.assertEqual(len(self.request('GET', '/v1/agents/%s' % agent_id, token=self.alex).data['credentials']), 1)
        # A superuser may renew it as it is, with no list or with one that asks for no more.
        self.assertEqual(self.renew(agent_id, token=self.admin).data['credential']['scopes'], ['read'])
        self.assertEqual(201, self.renew(agent_id, {'scopes': ['read']}, token=self.admin).status)
        # Its own account could have made it with those scopes, and may widen it.
        widened = self.renew(agent_id, {'scopes': ['read', 'tasks']})
        self.assertEqual(201, widened.status, widened.data)
        self.assertEqual(self.writes(widened.data['credential']['secret']), 201)
        self.assertEqual(self.has(agent_id), ['read', 'tasks'])

    def test_a_scope_that_does_not_exist_is_refused_before_anything_else(self):
        agent_id, _, _ = self.agent(['read'])
        for token in (self.alex, self.admin):
            # For the superuser the list also widens (tasks): the name that does not exist answers first.
            refused = self.renew(agent_id, {'scopes': ['read', 'tasks', 'everything']}, token=token)
            self.assertEqual((422, "Unknown credential scope 'everything'"), (refused.status, refused.data['error']['message']))
        self.assertEqual(len(self.request('GET', '/v1/agents/%s' % agent_id, token=self.alex).data['credentials']), 1)

    def test_scopes_that_are_not_a_list_of_names_and_a_field_the_route_does_not_take(self):
        agent_id, _, _ = self.agent(['read'])
        agents = len(self.request('GET', '/v1/agents', token=self.alex).data['items'])
        for number, bad in enumerate((5, True, 'read', {'read': 1, 'tasks': 1}, [5], [['read']], [None])):
            with self.subTest(scopes=bad):
                renewed = self.renew(agent_id, {'scopes': bad})
                self.assertEqual(422, renewed.status, renewed.data)
                made = self.create_agent(self.alex, name='Bad %d' % number, scopes=bad)
                self.assertEqual(422, made.status, made.data)
        # Nothing was made and nothing was issued; an empty list is no list.
        self.assertEqual(len(self.request('GET', '/v1/agents', token=self.alex).data['items']), agents)
        self.assertEqual(len(self.read(agent_id)['credentials']), 1)
        self.assertEqual(self.renew(agent_id, {'scopes': []}).data['credential']['scopes'], ['read'])
        refused = self.renew(agent_id, {'label': 'x', 'scope': ['read', 'tasks']})
        self.assertEqual(422, refused.status, refused.data)
        self.assertIn('scope', refused.data['error']['message'])
        # Somebody who may not see the agent learns nothing from the field check.
        self.create_account(self.admin, 'blake', 'blake-password-1')
        blake = self.login('blake', 'blake-password-1')[0]
        self.assertEqual(404, self.renew(agent_id, {'scope': ['read']}, token=blake).status)

    def test_a_replay_returns_the_same_credential_and_changes_nothing(self):
        agent_id, _, _ = self.agent(['read'])
        first = self.renew(agent_id, key='renew-0001')
        replay = self.renew(agent_id, key='renew-0001')
        self.assertEqual((first.status, replay.status), (201, 200))
        self.assertEqual(replay.data['credential']['id'], first.data['credential']['id'])
        self.assertEqual(replay.data['credential']['scopes'], ['read'])
        self.assertNotIn('secret', replay.data['credential'])

    def test_twenty_that_still_work_and_dead_ones_are_not_counted(self):
        """The limit counted every credential the agent ever had: after nineteen renewals an agent could
        never be given another, however many of them were revoked or expired."""
        agent_id, _, _ = self.agent(['read'])
        for _ in range(19):
            self.assertEqual(201, self.renew(agent_id).status)
        full = self.renew(agent_id)
        self.assertEqual(409, full.status, full.data)
        self.assertEqual(full.data['error']['message'], 'An agent may hold at most 20 credentials that still work; revoke one first')
        # Refused with a list too, and a refused request has changed nothing of what the agent has.
        self.assertEqual(409, self.renew(agent_id, {'scopes': ['read', 'tasks']}).status)
        self.assertEqual((self.has(agent_id), self.stored(agent_id)), (['read'], (['read'], 'set')))
        credentials = self.request('GET', '/v1/agents/%s' % agent_id, token=self.alex).data['credentials']
        self.assertEqual(len(credentials), 20)
        # One revoked: one more may be issued, and then it is full again.
        self.assertEqual(204, self.request('POST', '/v1/agents/%s/credentials/%s/revoke' % (agent_id, credentials[0]['id']),
                                           {}, token=self.alex).status)
        after = self.renew(agent_id)
        self.assertEqual((after.status, after.data['credential']['scopes']), (201, ['read']))
        self.assertEqual(409, self.renew(agent_id).status)
        # All of them expired, as after a month away: the next renewal passes and carries what the agent had.
        with self.service.store.lock:
            for credential in self.service.state['credentials'].values():
                if credential.get('agent_id') == agent_id:
                    credential['expires_at'] = 0
            self.service.store.save()
        late = self.renew(agent_id)
        self.assertEqual((late.status, late.data['credential']['scopes']), (201, ['read']))
        # The one that works and the newest few that do not: the others are gone from the state.
        shown = self.read(agent_id)['credentials']
        self.assertEqual([c['working'] for c in shown], [False] * http_auth.AGENT_DEAD_CREDENTIALS_KEPT + [True])
        self.assertEqual(shown[-1]['id'], late.data['credential']['id'])

    def held(self, agent_id):
        with self.service.store.lock:
            ids = {key for key, credential in self.service.state['credentials'].items() if credential.get('agent_id') == agent_id}
            tokens = [value for value in self.service.state['credential_tokens'].values() if value in ids]
            return len(ids), len(tokens)

    def test_credentials_that_no_longer_work_are_kept_to_a_few(self):
        """The state grew by a record for every renewal, and every read of the agent carried them all."""
        kept = http_auth.AGENT_DEAD_CREDENTIALS_KEPT
        agent_id, first, made = self.agent(['read'])
        issued = [made['credential']['id']]
        secrets_seen = [first]
        for _ in range(kept + 7):
            renewed = self.renew(agent_id).data['credential']
            self.assertEqual(204, self.revoke(agent_id, issued[-1]).status)
            issued.append(renewed['id'])
            secrets_seen.append(renewed['secret'])
        self.assertEqual(self.held(agent_id), (kept + 1, kept + 1))                # the state, and its index of secrets
        shown = self.read(agent_id)['credentials']
        self.assertEqual([c['id'] for c in shown], issued[-(kept + 1):])           # the newest ones, oldest first
        self.assertEqual([c['working'] for c in shown], [False] * kept + [True])
        # A secret whose record is gone is refused like any other that does not work.
        self.assertEqual(401, self.request('GET', '/v1/agents/me', token=secrets_seen[0]).status)
        self.assertEqual(401, self.request('GET', '/v1/agents/me', token=secrets_seen[-2]).status)
        self.assertEqual(200, self.request('GET', '/v1/agents/me', token=secrets_seen[-1]).status)
        # Twelve that work, all revoked at once by disabling the agent: the same few remain.
        for _ in range(11):
            self.assertEqual(201, self.renew(agent_id).status)
        self.assertEqual(200, self.request('POST', '/v1/agents/%s/disable' % agent_id, {}, token=self.alex).status)
        self.assertEqual(self.held(agent_id), (kept, kept))
        # Stopped working by time, with no write since: the read is bounded all the same.
        self.assertEqual(200, self.request('POST', '/v1/agents/%s/enable' % agent_id, {}, token=self.alex).status)
        for _ in range(kept + 3):
            self.assertEqual(201, self.renew(agent_id).status)
        self.later(self.service.credential_ttl + 60)
        self.assertEqual(self.held(agent_id)[0], 2 * kept + 3)
        self.assertEqual([c['working'] for c in self.read(agent_id)['credentials']], [False] * kept)
        self.assertEqual(201, self.renew(agent_id).status)
        self.assertEqual(self.held(agent_id), (kept + 1, kept + 1))
        # Another agent's credentials are not touched by any of it.
        other, _, _ = self.agent(['read'], name='Other')
        self.assertEqual(self.held(other), (1, 1))
        self.assertEqual(204, self.revoke(agent_id, self.working(agent_id)[0]['id']).status)
        self.assertEqual(self.held(other), (1, 1))

    def test_working_credentials_that_differ_are_said_and_dead_ones_are_not(self):
        """A read for the agent's account and a superuser: what main left behind. It says that they differ, not
        which is right, and an agent whose only working credential is a renewed one cannot be told at all."""
        agent_id, _, _ = self.agent(self.ALL)

        def differ(token=None):
            return self.request('GET', '/v1/agents/%s' % agent_id, token=token or self.alex).data['scopes_differ']
        self.assertEqual(differ(), [])
        self.assertEqual(201, self.renew(agent_id).status)                         # the same scopes: nothing to say
        self.assertEqual(differ(), [])
        narrow = self.renew(agent_id, {'scopes': ['read', 'tasks']}).data['credential']
        expected = [['checkpoints', 'feedback', 'proposals', 'read', 'reviews', 'tasks'], ['read', 'tasks']]
        self.assertEqual([sorted(item) for item in differ()], expected)
        self.assertEqual([sorted(item) for item in differ(self.admin)], expected)  # a superuser sees it too
        listed = {item['id']: item for item in self.request('GET', '/v1/agents', token=self.admin).data['items']}
        self.assertEqual([sorted(item) for item in listed[agent_id]['scopes_differ']], expected)
        # Revoked, or expired: a credential that no longer works is not what the agent may do.
        self.assertEqual(204, self.request('POST', '/v1/agents/%s/credentials/%s/revoke' % (agent_id, narrow['id']),
                                           {}, token=self.alex).status)
        self.assertEqual(differ(), [])
        again = self.renew(agent_id, {'scopes': ['read']}).data['credential']
        self.assertEqual(len(differ()), 2)
        with self.service.store.lock:
            self.service.state['credentials'][again['id']]['expires_at'] = 0
            self.service.store.save()
        self.assertEqual(differ(), [])
        # What it cannot see: one working credential, whatever it carries.
        other, _, _ = self.agent(['read'], name='Lone')
        self.assertEqual(self.request('GET', '/v1/agents/%s' % other, token=self.alex).data['scopes_differ'], [])

    def test_making_an_agent_is_as_it_was(self):
        self.assertEqual(self.agent(name='One')[2]['credential']['scopes'], ['tasks', 'checkpoints', 'reviews', 'feedback'])
        two_id, _, two = self.agent(['read'], name='Two')
        self.assertEqual(two['credential']['scopes'], ['read'])
        self.assertEqual((two['agent']['scopes'], two['agent']['scopes_source'], self.stored(two_id)),
                         (['read'], 'set', (['read'], 'set')))
        self.assertEqual(422, self.create_agent(self.alex, name='Three', scopes=['nothing']).status)
        # The agent reads the same about itself.
        mine = self.request('GET', '/v1/agents/me', token=two['credential']['secret']).data['agent']
        self.assertEqual((mine['scopes'], mine['scopes_source'], mine['scopes_differ']), (['read'], 'set', []))



class AgentsPageTests(RenewalScopeTests):
    """web/js/views/agents.js run under Node, with a small DOM, against this real service: what the card
    says about an agent's scopes, and that its account can put them right from the page."""

    def test_the_agents_page_under_node_against_the_real_service(self):
        from test_http_web import run_node_module
        node = shutil.which('node')
        if not node:
            print('NOTE: AgentsPageTests.test_the_agents_page_under_node_against_the_real_service was SKIPPED: node is not '
                  'installed, so web/js/views/agents.js was not run on this platform.', file=sys.stderr)
            self.skipTest('node is not installed; the agents page is not run here')
        four = ['tasks', 'checkpoints', 'reviews', 'feedback']
        kestrel, _, _ = self.agent(['read'], name='Kestrel')
        wren, _, _ = self.agent(['read'], name='Wren')
        self.assertEqual(201, self.renew(wren, {'scopes': four}).status)
        self.made_before(wren)                                                     # harmed by a renewal of an earlier kit
        nothing = {}
        for name in ('Lone', 'Dove'):                                              # nothing on record, nothing that works
            nothing[name], _, _ = self.agent(self.ALL, name=name)
            for credential in self.working(nothing[name]):
                self.revoke(nothing[name], credential['id'])
            self.made_before(nothing[name])
        web = ROOT / 'web' / 'js'
        done = run_node_module(self, node, 'await import(process.argv[1])',
                               (ROOT / 'tests' / 'web_agents_screen.mjs').as_uri(),
                               (ROOT / 'tests' / 'web_dom_shim.mjs').as_uri(), (web / 'api.js').as_uri(),
                               (web / 'views' / 'agents.js').as_uri(), 'http://127.0.0.1:%d' % self.port, self.project,
                               'alex=%s=%s' % (self.alex_id, self.alex), 'admin=none=%s' % self.admin)
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        first = seen['first']
        # 1. What the card says, and what "What it may do" shows.
        self.assertEqual((first['Kestrel']['scopes'], first['Kestrel']['source'], first['Kestrel']['differ']), ('read', 'set', None))
        self.assertEqual(first['Kestrel']['line'], 'May: read.')
        self.assertEqual(first['Kestrel']['boxes'], {'read': [True, False], 'tasks': [False, False], 'checkpoints': [False, False],
                                                     'reviews': [False, False], 'feedback': [False, False], 'proposals': [False, False]})
        self.assertEqual([(row['working'], row['differs'], row['buttons']) for row in first['Kestrel']['rows']],
                         [('true', 'false', ['Revoke'])])
        self.assertEqual((first['Wren']['scopes'], first['Wren']['source'], first['Wren']['differ'], first['Wren']['summary']),
                         ('unknown', 'unknown', '2', 'What it may do (choose)'))
        self.assertIn('Nothing says what this agent may do.', first['Wren']['line'])
        self.assertIn('Its working credentials do not all allow the same.', first['Wren']['line'])
        self.assertNotIn('fixed', first['Wren']['line'])                           # nobody is blamed
        self.assertEqual(sorted((row['differs'], row['buttons'], 'allows read' in row['text']) for row in first['Wren']['rows']),
                         [('true', ['Revoke'], False), ('true', ['Revoke'], True)])
        self.assertEqual((first['Lone']['scopes'], first['Lone']['differ']), ('unknown', None))
        self.assertEqual({row['working'] for row in first['Lone']['rows']}, {'false'})
        self.assertEqual({tuple(row['buttons']) for row in first['Lone']['rows']}, {()})
        self.assertEqual({name for name, (ticked, _) in first['Lone']['boxes'].items() if ticked}, {'read'})
        # 2. The credential that allowed the four is revoked from the card; what is left says what Wren has.
        self.assertIn('Revoke this credential?', seen['revokeAsked'])
        after = seen['afterRevoke']
        self.assertEqual((after['scopes'], after['source'], after['differ']), ('read', 'inferred', None))
        self.assertIn('read from its working credentials', after['line'])
        self.assertEqual(self.read(wren)['scopes'], ['read'])
        self.assertEqual([c['scopes'] for c in self.working(wren)], [['read']])
        # 3. and 4. Nothing ticked sends nothing; the choice (reading alone, as offered) issues a secret.
        self.assertEqual(seen['noneTicked'], {'said': 'Tick at least one.', 'sent': 0, 'dialogs': 0})
        self.assertIn('Lone will have: read.', seen['loneAsked'])
        self.assertEqual(seen['loneDialog'], 1)
        self.assertEqual((seen['afterChoice']['scopes'], seen['afterChoice']['source']), ('read', 'set'))
        self.assertEqual(self.stored(nothing['Lone']), (['read'], 'set'))
        # 5. Its own account gives more; a cancelled confirmation sends nothing.
        self.assertEqual(seen['cancelSent'], 0)
        self.assertIn('Kestrel will have: read, tasks.', seen['widenAsked'])
        self.assertEqual((seen['afterWiden']['scopes'], seen['afterWiden']['differ']), ('read tasks', '1'))
        self.assertIn('1 working credential allows something else than that.', seen['afterWiden']['line'])
        # 6. A superuser: what the agent does not have cannot be ticked, and is not sent when it is.
        self.assertEqual({name: disabled for name, (_, disabled) in seen['admin']['boxes'].items()},
                         {'read': False, 'tasks': False, 'checkpoints': True, 'reviews': True, 'feedback': True, 'proposals': True})
        self.assertEqual(seen['afterNarrow']['scopes'], 'read')
        # 7. Set up folder.
        self.assertEqual((seen['setupKestrel']['needed'], seen['setupKestrel']['issue']), (0, True))
        self.assertIn('It carries what the agent has now: read.', seen['setupKestrel']['asked'])
        self.assertEqual(seen['setupKestrel']['carries'], [['read', 'This credential carries: read.']])
        self.assertGreaterEqual(seen['setupKestrel']['older'], 1)
        self.assertEqual(seen['setupDove'], {'needed': 1, 'issue': False})
        # 8. Nothing threw.
        self.assertEqual(seen['odd'], [[0, None], [0, 'unknown'], [0, 'read'], [2, 'read'], [2, 'unknown'], [0, 'unknown']])
        # What the page sent, in order: a list only from "What it may do".
        credentials = '/v1/agents/AGENT/credentials'
        self.assertEqual(seen['sent'], [
            ['alex', 'POST', credentials + '/CRED/revoke', {}],
            ['alex', 'POST', credentials, {'label': 'web: new secret', 'scopes': ['read']}],
            ['alex', 'POST', credentials, {'label': 'web: new secret', 'scopes': ['read', 'tasks']}],
            ['admin', 'POST', credentials, {'label': 'web: new secret', 'scopes': ['read']}],
            ['alex', 'POST', credentials, {'label': 'web: new secret'}]])
        self.assertEqual(self.stored(kestrel), (['read'], 'set'))
        self.assertEqual(self.stored(nothing['Dove']), (None, None))


for _name in dir(RenewalScopeTests):
    if _name.startswith('test_') and _name not in AgentsPageTests.__dict__:
        setattr(AgentsPageTests, _name, None)


if __name__ == '__main__':
    unittest.main()
