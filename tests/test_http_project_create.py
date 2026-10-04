"""Creating a project from the web interface: the grant, the route, the host action
(kittrial-5bb.118 part 2). Endpoint backend over the strict stub, which runs the kit's own
``project_creation`` rules and emulates only the work of initializing a project."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import project_creation as pc
import test_http_review_fixes as fixes

STUB = KIT / 'tools' / 'strict_canonical_endpoint.py'
REFUSED = 'Only a superuser registers a project on this server.'


class Case(fixes.EndpointCase):
    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.ids, self.tokens = {}, {}
        for name in ('olive', 'carl', 'vera'):
            self.ids[name] = self.create_account(self.admin, name, name + '-password-1')
            self.tokens[name] = self.login(name, name + '-password-1')[0]
        self.olive, self.carl = self.tokens['olive'], self.tokens['carl']

    def grant(self, who='olive', limit=None, token=None):
        body = {} if limit is None else {'limit': limit}
        return self.request('PUT', '/v1/accounts/%s/project-grant' % self.ids[who], body, token=token or self.admin)

    def create(self, token, project_id, name=None, key=None, **extra):
        body = dict({'project_id': project_id, 'name': name or project_id.title(), 'create': True}, **extra)
        return self.request('POST', '/v1/projects', body, token=token, key=key)

    def visible(self, token):
        return sorted(p['id'] for p in self.request('GET', '/v1/projects', token=token).data['items'])

    def on_host(self, project_id):
        return (self.canonical_root / 'projects' / project_id).exists()

    def record(self, project_id):
        return pc.read_record(self.canonical_root, project_id)

    def audit(self, action):
        return [event for event in self.service.state['audit'] if event['action'] == action]

    def stop_at(self, stage):
        stopped = patch.dict(os.environ, {'STRICT_ENDPOINT_CREATE_STOP': stage})
        stopped.start()
        self.addCleanup(stopped.stop)
        return stopped


class GrantTests(Case):
    def test_a_superuser_grants_changes_and_revokes(self):
        made = self.grant()
        self.assertEqual(200, made.status, made.data)
        self.assertEqual((made.data['project_grant']['limit'], made.data['project_grant']['granted_by'],
                          made.data['projects_created']), (5, self.admin_user['id'], 0))
        self.assertEqual(self.grant(limit=2).data['project_grant']['limit'], 2)
        listed = {u['username']: u for u in self.request('GET', '/v1/accounts', token=self.admin).data['items']}
        self.assertEqual(listed['olive']['project_grant']['limit'], 2)
        self.assertIsNone(listed['carl']['project_grant'])
        gone = self.request('DELETE', '/v1/accounts/%s/project-grant' % self.ids['olive'], token=self.admin)
        self.assertEqual((200, None), (gone.status, gone.data['project_grant']))
        reasons = [event['reason'] for event in self.audit('accounts.project-grant')]
        self.assertEqual(reasons, ['account=%s granted limit=5' % self.ids['olive'],
                                   'account=%s changed limit=2 (was 5)' % self.ids['olive'],
                                   'account=%s revoked (limit was 2)' % self.ids['olive']])
        self.assertTrue(all(event['user_id'] == self.admin_user['id'] for event in self.audit('accounts.project-grant')))

    def test_who_may_not_grant_and_what_a_limit_may_be(self):
        self.assertEqual(403, self.grant(token=self.olive).status)                 # not even to oneself
        self.assertEqual(403, self.grant(who='carl', token=self.olive).status)
        for bad in (0, -1, 101, '5', 2.5, True, None.__class__):
            if bad is None.__class__:
                continue
            with self.subTest(limit=bad):
                self.assertEqual(422, self.grant(limit=bad).status)
        self.assertEqual(422, self.request('PUT', '/v1/accounts/%s/project-grant' % self.ids['olive'],
                                           {'limit': 3, 'projects': ['x']}, token=self.admin).status)
        self.assertEqual(404, self.request('PUT', '/v1/accounts/usr_0000000000000000/project-grant', {},
                                           token=self.admin).status)
        self.assertEqual(self.audit('accounts.project-grant'), [])

    def test_the_session_says_whether_this_account_may_create(self):
        def mine(token):
            return self.request('GET', '/v1/sessions/current', token=token).data['project_host_create']
        self.assertEqual(mine(self.olive), {'allowed': False, 'limit': None, 'used': 0, 'reason': 'no-grant'})
        self.grant(limit=1)
        self.assertEqual(mine(self.olive), {'allowed': True, 'limit': 1, 'used': 0, 'reason': None})
        self.assertEqual(mine(self.admin), {'allowed': True, 'limit': None, 'used': 0, 'reason': None})
        self.assertEqual(201, self.create(self.olive, 'alpha').status)
        self.assertEqual(mine(self.olive), {'allowed': False, 'limit': 1, 'used': 1, 'reason': 'limit'})


class RefusalTests(Case):
    def test_everyone_without_the_grant_gets_one_answer_whatever_they_send(self):
        self.grant('carl')
        self.assertEqual(201, self.create(self.carl, 'taken').status)
        (self.canonical_root / 'retired' / 'gone-20260101T000000Z').mkdir(parents=True)
        answers = set()
        for project_id in ('taken', 'gone', 'fresh', 'Not A Name', '', None):
            with self.subTest(project_id=project_id):
                answer = self.request('POST', '/v1/projects', {'project_id': project_id, 'name': 'X', 'create': True},
                                      token=self.olive)
                self.assertEqual(403, answer.status, answer.data)
                answers.add(json.dumps(answer.data['error'], sort_keys=True).replace(
                    answer.data.get('request_id', '-'), ''))
        self.assertEqual(len(answers), 1)
        self.assertIn(REFUSED, answers.pop())
        # The register route (no "create") says the same to the same person.
        self.assertIn(REFUSED, self.request('POST', '/v1/projects', {'project_id': 'fresh'},
                                            token=self.olive).data['error']['message'])
        self.assertFalse(self.on_host('fresh'))
        self.assertIsNone(self.record('fresh'))

    def test_an_agent_credential_never_creates_whoever_owns_it(self):
        self.grant()
        made = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'working_directory': '/home/olive/k',
                                                   'projects': []}, token=self.olive)
        self.assertEqual(201, made.status, made.data)
        for owner_agent in (made.data['credential']['secret'],):
            answer = self.create(owner_agent, 'alpha')
            self.assertEqual(403, answer.status, answer.data)
        admin_agent = self.request('POST', '/v1/agents', {'name': 'Root', 'working_directory': '/root/a',
                                                          'projects': []}, token=self.admin)
        self.assertEqual(403, self.create(admin_agent.data['credential']['secret'], 'alpha').status)
        self.assertFalse(self.on_host('alpha'))

    def test_a_taken_and_a_retired_name_read_the_same_to_a_creator(self):
        self.grant()
        self.initialize_canonical('operator')                       # made by an operator, not registered
        (self.canonical_root / 'retired' / 'gone-20260101T000000Z').mkdir(parents=True)
        self.assertEqual(201, self.create(self.olive, 'mine').status)
        said = {}
        for project_id in ('operator', 'gone', 'mine'):
            answer = self.create(self.olive, project_id)
            self.assertEqual(409, answer.status, answer.data)
            said[project_id] = answer.data['error']['message'].replace(project_id, 'NAME')
        self.assertEqual(len(set(said.values())), 1, said)
        self.assertEqual(said['gone'], 'Project name NAME is not available: choose another name')
        self.assertIsNone(self.record('operator'))
        self.assertIsNone(self.record('gone'))

    def test_bad_requests_make_nothing(self):
        self.grant()
        for body in ({'project_id': 'Alpha', 'create': True}, {'project_id': 'a', 'create': True},
                     {'project_id': '../x', 'create': True}, {'project_id': 'alpha', 'create': True, 'owner': 'x'},
                     {'project_id': 'alpha', 'name': 'x' * 80, 'create': True}):
            with self.subTest(body=body):
                self.assertEqual(422, self.request('POST', '/v1/projects', body, token=self.olive).status)
        self.assertEqual(list((self.canonical_root / 'projects').glob('*')) if (self.canonical_root / 'projects').exists()
                         else [], [])
        self.assertEqual(self.visible(self.olive), [])


class CreateTests(Case):
    def test_a_grantee_creates_a_project_and_is_its_only_member(self):
        self.grant(limit=3)
        made = self.create(self.olive, 'alpha', 'Alpha project')
        self.assertEqual(201, made.status, made.data)
        self.assertEqual((made.data['id'], made.data['name'], made.data['role'], made.data['usable']),
                         ('alpha', 'Alpha project', 'owner', True))
        self.assertIn(made.data['backup'], ('covered', 'not-covered', 'unknown'))
        self.assertTrue((self.canonical_root / 'projects' / 'alpha' / '.beads' / 'metadata.json').is_file())
        record = self.record('alpha')
        self.assertEqual((record['state'], record['by']), ('created', self.ids['olive']))
        self.assertEqual(self.visible(self.olive), ['alpha'])
        self.assertEqual(self.visible(self.carl), [])
        members = self.request('GET', '/v1/projects/alpha/members', token=self.olive).data['items']
        self.assertEqual([(m['user_id'], m['role']) for m in members], [(self.ids['olive'], 'owner')])
        # It is a working project at once: a task, and the setup page.
        self.assertEqual(201, self.request('POST', '/v1/projects/alpha/tasks', {'title': 'first'}, token=self.olive).status)
        self.assertEqual(200, self.request('GET', '/v1/projects/alpha/setup', token=self.olive).status)
        stored = self.service.state['projects']['alpha']
        self.assertEqual((stored['created_by'], stored['registered_by'], stored['host_created']['grant_limit'],
                          stored['host_created']['granted_by']),
                         (self.ids['olive'], self.ids['olive'], 3, self.admin_user['id']))
        self.assertIsNone(http_service.project_unusable(self.service, 'alpha'))
        # The audit record names the account, the grant and the count.
        event = self.audit('projects.host-create')[-1]
        self.assertEqual((event['outcome'], event['user_id']), ('committed', self.ids['olive']))
        self.assertEqual(event['reason'], 'create alpha account=%s limit=3 count=1' % self.ids['olive'])

    def test_a_superuser_creates_without_a_grant_or_a_limit(self):
        for index in range(3):
            self.assertEqual(201, self.create(self.admin, 'p%d' % index).status)
        self.assertIn('limit=none (superuser) count=3', self.audit('projects.host-create')[-1]['reason'])
        self.assertIsNone(self.service.state['projects']['p0']['host_created']['grant_limit'])

    def test_the_same_request_sent_again_replays_and_creates_once(self):
        self.grant()
        first = self.create(self.olive, 'alpha', key='create-key-0001')
        again = self.create(self.olive, 'alpha', key='create-key-0001')
        self.assertEqual((201, 201, first.data['id']), (first.status, again.status, again.data['id']))
        self.assertEqual(len([e for e in self.audit('projects.host-create') if e['outcome'] == 'committed']), 1)
        # The same key with another name is a conflict, never a second project.
        self.assertEqual(409, self.create(self.olive, 'beta', key='create-key-0001').status)
        self.assertFalse(self.on_host('beta'))

    def test_the_limit_counts_what_the_account_created_until_it_is_archived(self):
        self.grant(limit=2)
        self.assertEqual(201, self.create(self.olive, 'one').status)
        self.assertEqual(201, self.create(self.olive, 'two').status)
        full = self.create(self.olive, 'three')
        self.assertEqual(403, full.status, full.data)
        self.assertIn('The limit of 2 project(s) for this account is reached', full.data['error']['message'])
        self.assertFalse(self.on_host('three'))
        # Handing a project to someone else does not free a place.
        self.assertIn(self.request('PUT', '/v1/projects/one/members/%s' % self.ids['carl'], {'role': 'owner'},
                                   token=self.admin).status, (200, 201))
        self.request('DELETE', '/v1/projects/one/members/%s' % self.ids['olive'], token=self.admin)
        self.assertEqual(403, self.create(self.olive, 'three').status)
        # Archiving one does.
        self.assertEqual(200, self.request('POST', '/v1/projects/two/archive', {}, token=self.olive).status)
        self.assertEqual(201, self.create(self.olive, 'three').status)
        # A lowered limit is seen at once.
        self.grant(limit=1)
        self.assertEqual(403, self.create(self.olive, 'four').status)

    def test_a_revoked_grant_stops_the_next_creation_and_keeps_the_projects(self):
        self.grant()
        self.assertEqual(201, self.create(self.olive, 'alpha').status)
        self.request('DELETE', '/v1/accounts/%s/project-grant' % self.ids['olive'], token=self.admin)
        refused = self.create(self.olive, 'beta')
        self.assertEqual(403, refused.status)
        self.assertIn(REFUSED, refused.data['error']['message'])
        self.assertEqual(self.visible(self.olive), ['alpha'])
        self.assertFalse(self.on_host('beta'))


class StopTests(Case):
    def test_a_stop_before_anything_is_made_leaves_nothing_and_the_next_request_works(self):
        self.grant()
        stopped = self.stop_at('init')
        answer = self.create(self.olive, 'alpha')
        self.assertEqual(409, answer.status, answer.data)
        self.assertIn('nothing was made', answer.data['error']['message'])
        self.assertEqual((self.on_host('alpha'), self.record('alpha'), self.visible(self.olive)), (False, None, []))
        stopped.stop()
        self.assertEqual(201, self.create(self.olive, 'alpha').status)

    def test_a_stop_half_way_registers_nothing_names_the_project_and_holds_the_name(self):
        self.grant(limit=1)
        stopped = self.stop_at('merge-slot')
        answer = self.create(self.olive, 'alpha')
        self.assertEqual(409, answer.status, answer.data)
        message = answer.data['error']['message']
        for words in ('Project alpha was started on the server and did not finish', 'An operator must finish it',
                      'admin.py finish-project alpha', 'admin.py retire-project alpha'):
            self.assertIn(words, message)
        # Nothing half-made is visible or usable in the web interface.
        self.assertEqual(self.visible(self.olive), [])
        self.assertEqual(self.visible(self.admin), [])
        self.assertNotIn('alpha', self.service.state['projects'])
        self.assertEqual(404, self.request('GET', '/v1/projects/alpha', token=self.olive).status)
        self.assertEqual(404, self.request('GET', '/v1/projects/alpha/tasks', token=self.olive).status)
        stopped.stop()
        # The name is held: by the same account (same answer), against another account, and in the limit.
        self.assertEqual(409, self.create(self.olive, 'alpha').status)
        self.assertEqual(self.record('alpha')['state'], 'incomplete')
        held = self.create(self.olive, 'beta')
        self.assertEqual(409, held.status, held.data)
        self.assertIn('The limit of 1 project(s) for this account is reached', held.data['error']['message'])
        self.assertFalse(self.on_host('beta'))
        self.grant('carl')
        other = self.create(self.carl, 'alpha')
        self.assertEqual((409, 'Project name alpha is not available: choose another name'),
                         (other.status, other.data['error']['message']))
        # A superuser sees it listed, with who started it and the two commands.
        listed = self.request('GET', '/v1/project-creations', token=self.admin)
        self.assertEqual(200, listed.status, listed.data)
        self.assertEqual([(i['project'], i['state'], i['by'], i['stage'], i['finish'], i['remove'])
                          for i in listed.data['items']],
                         [('alpha', 'incomplete', self.ids['olive'], 'merge-slot', 'admin.py finish-project alpha',
                           'admin.py retire-project alpha --actor OPERATOR --reason REASON --force')])
        self.assertEqual(listed.data['items'][0]['by_name'], 'olive')
        self.assertEqual(403, self.request('GET', '/v1/project-creations', token=self.olive).status)

    def test_after_an_operator_finishes_it_the_creator_creates_it_again_and_nothing_is_redone(self):
        self.grant(limit=1)
        stopped = self.stop_at('first-backup')
        self.assertEqual(409, self.create(self.olive, 'alpha').status)
        stopped.stop()
        finished = []
        pc.finish(self.canonical_root, 'alpha', finish_steps=lambda root, name: finished.append(name))
        self.assertEqual(finished, ['alpha'])
        made = self.create(self.olive, 'alpha')
        self.assertEqual(201, made.status, made.data)
        self.assertTrue(self.service.state['projects']['alpha']['host_created']['adopted'])
        self.assertEqual(self.request('GET', '/v1/project-creations', token=self.admin).data['items'], [])
        self.assertEqual(self.visible(self.olive), ['alpha'])

    def test_after_an_operator_removes_it_the_place_is_free_and_the_name_is_not(self):
        self.grant(limit=1)
        stopped = self.stop_at('configure')
        self.assertEqual(409, self.create(self.olive, 'alpha').status)
        stopped.stop()
        # What retire-project does: the directory moves to retired/, the record is marked.
        (self.canonical_root / 'retired').mkdir()
        (self.canonical_root / 'projects' / 'alpha').rename(self.canonical_root / 'retired' / 'alpha-20260101T000000Z')
        pc.mark_removed(self.canonical_root, 'alpha', 'ops')
        self.assertEqual(self.request('GET', '/v1/project-creations', token=self.admin).data['items'], [])
        again = self.create(self.olive, 'alpha')
        self.assertEqual((409, 'Project name alpha is not available: choose another name'),
                         (again.status, again.data['error']['message']))
        self.assertEqual(201, self.create(self.olive, 'beta').status)


class EndpointGuardTests(Case):
    """The action itself, called as the endpoint is called."""

    def call(self, request, authority=True):
        argv = [sys.executable, str(STUB), '--root', str(self.canonical_root)]
        if authority:
            argv += ['--authority-store', str(self.store.path), '--authority-lock', str(self.store.path) + '.lock']
        done = subprocess.run(argv, input=json.dumps(request), text=True, encoding='utf-8', capture_output=True,
                              timeout=120)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def descriptor(self, token, capability='project.host-create'):
        principal = self.service.authenticate(token)
        return http_service.authority_request(principal, None, capability, now=self.service._expiry_now())

    def request_for(self, account, token, **changes):
        request = {'project': 'alpha', 'actor': self.ids[account], 'action': 'create-project', 'args': [],
                   'operation_id': 'op-guard-1', 'authority': self.descriptor(token)}
        request.update(changes)
        return request

    def test_without_the_services_launch_arguments_the_action_is_refused(self):
        self.grant()
        answer = self.call(self.request_for('olive', self.olive), authority=False)
        self.assertEqual(answer['returncode'], 2)
        self.assertIn('create-project is available only to the web service', answer['stderr'])
        answer = self.call({'project': 'alpha', 'actor': 'mallory', 'action': 'project-creations', 'args': []},
                           authority=False)
        self.assertIn('project-creations is available only to the web service', answer['stderr'])
        self.assertFalse(self.on_host('alpha'))

    def test_the_descriptor_must_be_a_session_for_this_capability_and_this_account(self):
        self.grant()
        good = self.request_for('olive', self.olive)
        for label, request in (
                ('no descriptor', dict(good, authority=None)),
                ('another capability', dict(good, authority=self.descriptor(self.olive, 'project.create'))),
                ('another account as the actor', dict(good, actor=self.ids['carl'])),
                ('a label under the account', dict(good, actor=self.ids['olive'] + '/x')),
                ('an SSH-style actor', dict(good, actor='alice')),
                ('no operation id', dict(good, operation_id=None)),
                ('arguments', dict(good, args=['--force']))):
            with self.subTest(refused=label):
                answer = self.call(request)
                self.assertEqual(answer['returncode'], 2, answer)
        self.assertFalse(self.on_host('alpha'))
        self.assertIsNone(self.record('alpha'))

    def test_the_grant_and_the_limit_are_checked_in_the_endpoint_against_the_live_store(self):
        # No grant: the web route would have refused; a caller that skipped it is refused here.
        answer = self.call(self.request_for('olive', self.olive))
        self.assertEqual((answer['returncode'], answer.get('authority_status')), (126, 403))
        self.assertFalse(self.on_host('alpha'))
        self.grant(limit=1)
        self.assertEqual(self.call(self.request_for('olive', self.olive))['returncode'], 0)
        self.assertEqual(self.record('alpha')['state'], 'created')
        # The project is on the host and not registered: the name is held and counts.
        answer = self.call(self.request_for('olive', self.olive, project='beta', operation_id='op-guard-2'))
        self.assertEqual(answer['returncode'], 2)
        self.assertIn('The limit of 1 project(s) for this account is reached', answer['stderr'])
        self.assertFalse(self.on_host('beta'))
        # Revoked between the route's check and the endpoint's: refused here.
        self.request('DELETE', '/v1/accounts/%s/project-grant' % self.ids['olive'], token=self.admin)
        answer = self.call(self.request_for('olive', self.olive, project='gamma', operation_id='op-guard-3'))
        self.assertEqual((answer['returncode'], answer.get('authority_status')), (126, 403))
        self.assertFalse(self.on_host('gamma'))

    def test_the_list_is_for_superusers_only(self):
        self.grant()
        request = {'project': None, 'actor': self.ids['olive'], 'action': 'project-creations', 'args': [],
                   'authority': self.descriptor(self.olive, 'accounts.admin')}
        self.assertEqual(self.call(request)['returncode'], 126)
        request = {'project': None, 'actor': self.admin_user['id'], 'action': 'project-creations', 'args': [],
                   'authority': self.descriptor(self.admin, 'accounts.admin')}
        answer = self.call(request)
        self.assertEqual((answer['returncode'], json.loads(answer['stdout'])['items']), (0, []))


class OnboardingTests(Case):
    """An owner sets the project's onboarding text from the web interface; guidance stays with the operator."""

    def setUp(self):
        super().setUp()
        self.grant()
        self.assertEqual(201, self.create(self.olive, 'alpha').status)
        for name, role in (('carl', 'contributor'), ('vera', 'viewer')):
            self.request('PUT', '/v1/projects/alpha/members/%s' % self.ids[name], {'role': role}, token=self.admin)
        # The stub keeps a project's files in <root>/NAME (the real endpoint: <root>/projects/NAME).
        self.file = self.canonical_root / 'alpha' / 'ONBOARDING.md'
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.url = '/v1/projects/alpha/onboarding'

    def put(self, text, token=None):
        return self.request('PUT', self.url, {'text': text}, token=token or self.olive)

    def test_an_owner_sets_reads_and_clears_it(self):
        import onboarding
        self.assertEqual(self.request('GET', self.url, token=self.olive).data,
                         {'state': 'not-set', 'source': None, 'text': None, 'limit': 8000})
        made = self.put('Start with docs/README.md.\nAsk Olive about access.')
        self.assertEqual(200, made.status, made.data)
        self.assertEqual((made.data['state'], made.data['source']), ('set', 'web'))
        stored = self.file.read_text(encoding='utf-8')
        # The stored document leads with the line that says who wrote it and what it is.
        self.assertIn('not an instruction from the operator', onboarding.WEB_HEADER)
        self.assertEqual(stored, onboarding.WEB_HEADER + '\n\nStart with docs/README.md.\nAsk Olive about access.\n')
        read = self.request('GET', self.url, token=self.olive).data
        self.assertEqual((read['state'], read['source'], read['text']),
                         ('set', 'web', 'Start with docs/README.md.\nAsk Olive about access.\n'))
        # What a worker is given by `onboard` is the whole stored document, so the line is in it.
        shown = onboarding.read_document(self.canonical_root / 'alpha', 'ONBOARDING.md', onboarding.PROJECT_LIMIT)
        self.assertTrue(shown.startswith(onboarding.WEB_HEADER))
        steps = {s['id']: s for s in self.request('GET', '/v1/projects/alpha/setup', token=self.olive).data['steps']}
        self.assertEqual((steps['onboarding']['state'], steps['onboarding']['who']), ('done', 'owner-or-operator'))
        self.assertEqual(steps['guidance']['who'], 'operator')
        # Audited with the account and the size, never the text.
        event = self.audit('projects.onboarding')[-1]
        self.assertEqual((event['outcome'], event['project_id'], event['reason']),
                         ('committed', 'alpha', 'account=%s set 50 bytes' % self.ids['olive']))
        self.assertNotIn('README', json.dumps(self.service.state['audit']))
        cleared = self.request('DELETE', self.url, token=self.olive)
        self.assertEqual((200, 'not-set'), (cleared.status, cleared.data['state']))
        self.assertFalse(self.file.exists())
        self.assertIn('cleared', self.audit('projects.onboarding')[-1]['reason'])

    def test_who_may_not_set_it(self):
        agent = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'working_directory': '/home/olive/k',
                                                    'projects': ['alpha']}, token=self.olive)
        self.create_account(self.admin, 'nell', 'nell-password-1')
        nell = self.login('nell', 'nell-password-1')[0]
        for label, token, status in (('a contributor', self.carl, 403), ('a viewer', self.tokens['vera'], 403),
                                     ("the owner's agent", agent.data['credential']['secret'], 403),
                                     ('someone outside the project', nell, 404)):
            for method, body in (('PUT', {'text': 'x'}), ('DELETE', None), ('GET', None)):
                with self.subTest(who=label, method=method):
                    self.assertEqual(status, self.request(method, self.url, body, token=token).status)
        self.assertFalse(self.file.exists())
        self.assertEqual(200, self.put('by a superuser', token=self.admin).status)

    def test_the_same_rules_as_the_operator_command_plus_plain_text(self):
        import onboarding
        room = onboarding.PROJECT_LIMIT - len((onboarding.WEB_HEADER + '\n\n\n').encode('utf-8'))
        self.assertEqual(200, self.put('x' * room).status)
        before = self.file.read_bytes()
        self.assertEqual(len(before), onboarding.PROJECT_LIMIT)
        for label, text, words in (
                ('empty', '   ', 'nonempty'), ('one byte too long', 'x' * (room + 1), 'at most 8000 bytes'),
                ('a bidi override', 'read this‮', 'plain text'), ('a control character', 'a\x1b[31m', 'plain text'),
                ('a zero-width space', 'a​b', 'plain text')):
            with self.subTest(refused=label):
                answer = self.put(text)
                self.assertEqual(422, answer.status, answer.data)
                self.assertIn(words, answer.data['error']['message'])
                self.assertIn('Project onboarding', answer.data['error']['message'])
        for body in ({'text': 7}, {'text': 'x', 'guidance': 'y'}, {}):
            self.assertEqual(422, self.request('PUT', self.url, body, token=self.olive).status)
        self.assertEqual(self.file.read_bytes(), before)

    def test_the_operators_own_text_is_reported_not_returned_and_not_removable_here(self):
        self.file.write_text('Operator text: read the RUNBOOK.\n', encoding='utf-8')
        read = self.request('GET', self.url, token=self.olive).data
        self.assertEqual((read['state'], read['source'], read['text']), ('set', 'operator', None))
        refused = self.request('DELETE', self.url, token=self.olive)
        self.assertEqual(422, refused.status, refused.data)
        self.assertIn('set by an operator on the server', refused.data['error']['message'])
        self.assertTrue(self.file.exists())
        # An owner may replace it with their own; it then reads as owner-written.
        self.assertEqual(200, self.put('Owner text.').status)
        self.assertEqual(self.request('GET', self.url, token=self.olive).data['source'], 'web')

    def test_guidance_cannot_be_set_through_this_or_any_web_route(self):
        self.assertEqual(404, self.request('PUT', '/v1/projects/alpha/guidance', {'text': 'x'}, token=self.olive).status)
        self.put('Owner text.')
        self.assertFalse((self.canonical_root / 'alpha' / 'GUIDANCE.md').exists())

    def test_the_endpoint_action_is_for_the_web_service_and_this_project_only(self):
        principal = self.service.authenticate(self.olive)

        def call(authority=True, **changes):
            descriptor = http_service.authority_request(principal, changes.pop('for_project', 'alpha'),
                                                        changes.pop('capability', 'project.admin'),
                                                        now=self.service._expiry_now())
            request = dict({'project': 'alpha', 'actor': self.ids['olive'], 'action': 'set-onboarding',
                            'args': ['set'], 'attachments': {'text': {'flag': '--file', 'text': 'x'}},
                            'operation_id': 'op-onb-1', 'authority': descriptor}, **changes)
            argv = [sys.executable, str(STUB), '--root', str(self.canonical_root)]
            if authority:
                argv += ['--authority-store', str(self.store.path), '--authority-lock', str(self.store.path) + '.lock']
            done = subprocess.run(argv, input=json.dumps(request), text=True, encoding='utf-8', capture_output=True,
                                  timeout=120)
            return json.loads(done.stdout.strip().splitlines()[-1])
        for label, answer in (('no launch arguments', call(authority=False)),
                              ('a descriptor for another capability', call(capability='read')),
                              ('a descriptor for another project', call(for_project='beta')),
                              ('an SSH-style actor', call(actor='alice')),
                              ('other arguments', call(args=['set', '--force']))):
            with self.subTest(refused=label):
                self.assertNotEqual(answer['returncode'], 0, answer)
        self.assertFalse(self.file.exists())
        self.assertEqual(call()['returncode'], 0)
        self.assertTrue(self.file.exists())


class ScreenTests(Case):
    """The pages run under Node, with a small DOM, against this real service on the endpoint backend."""

    def run_page(self, node, phase):
        from test_http_web import run_node_module
        web = KIT / 'web' / 'js'
        people = [('admin', self.admin, self.admin_user['id']), ('olive', self.olive, self.ids['olive']),
                  ('carl', self.carl, self.ids['carl'])]
        done = run_node_module(self, node, 'await import(process.argv[1])',
                               (KIT / 'tests' / 'web_project_create_screen.mjs').as_uri(),
                               (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(), (web / 'api.js').as_uri(),
                               (web / 'views' / 'work.js').as_uri(), (web / 'views' / 'admin.js').as_uri(),
                               (web / 'views' / 'setup.js').as_uri(), 'http://127.0.0.1:%d' % self.port, phase,
                               *['%s=%s=%s' % item for item in people])
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_the_pages_under_node_against_the_real_service(self):
        import shutil
        node = shutil.which('node')
        if not node:
            print('NOTE: ScreenTests.test_the_pages_under_node_against_the_real_service was SKIPPED: node is not '
                  'installed, so the project-creation pages were not run on this platform.', file=sys.stderr)
            self.skipTest('node is not installed; the project-creation pages are not run here')
        seen = self.run_page(node, 'create')
        # The superuser allows olive two projects.
        self.assertTrue(seen['before'])
        self.assertEqual(seen['choices'], ['carl (@carl)', 'olive (@olive)', 'vera (@vera)'])
        self.assertEqual(seen['badLimit'], 'Enter a whole number from 1 to 100.')
        self.assertEqual(len(seen['holders']), 1, seen.get('debug'))
        self.assertIn('olive (@olive): 0 of 2 in use', seen['holders'][0])
        self.assertEqual(self.service.state['users'][self.ids['olive']]['project_grant']['limit'], 2)
        # Olive creates a project and lands on its setup page; carl is shown nothing.
        self.assertEqual(seen['session'], {'allowed': True, 'limit': 2, 'used': 0, 'reason': None})
        self.assertIsNone(seen['carlPanel'])
        self.assertIn('You have created 0 of the 2 projects you may have at one time', seen['panelText'])
        self.assertIn('lowercase letters or digits', seen['badName'])
        self.assertEqual(seen['went'], ['/p/alpha/setup'])
        self.assertEqual(seen['projects'], [['alpha', 'Alpha project', 'owner']])
        self.assertEqual(self.record('alpha')['state'], 'created')
        # The owner writes the onboarding text on the setup page; guidance has no form.
        self.assertEqual(seen['onboardingBefore'], {'state': 'todo', 'who': True})
        self.assertEqual(seen['emptyText'], 'Enter the onboarding text.')
        self.assertIn('plain text', seen['refusedText'])
        self.assertEqual(seen['onboardingAfter']['state'], 'done')
        self.assertEqual(seen['onboardingAfter']['value'], 'Start with docs/README.md.\n')
        self.assertEqual(seen['onboardingAfter']['buttons'], ['Save', 'Remove'])
        self.assertTrue(seen['guidanceHasNoForm'])
        # The server stops half way through the next one.
        self.stop_at('merge-slot')
        seen = self.run_page(node, 'stopped')
        self.assertTrue(seen['stopped']['shown'])
        self.assertIn('Project beta was started on the server and did not finish', seen['stopped']['text'])
        self.assertIn('An operator must finish it', seen['stopped']['text'])
        self.assertEqual((seen['stopped']['went'], seen['projects']), ([], ['alpha']))
        self.assertEqual(seen['list']['cards'], ['beta'])
        self.assertEqual(seen['list']['commands'], ['admin.py finish-project beta',
                                                    'admin.py retire-project beta --actor OPERATOR --reason REASON --force'])
        self.assertIn('Started by olive', seen['list']['text'])
        self.assertIn('this page does not run anything there', seen['list']['text'])
        self.assertIsNone(seen['oliveList'])
        self.assertIn('The limit of 2 project(s) for this account is reached', seen['limit'])
        self.assertFalse(self.on_host('gamma'))


if __name__ == '__main__':
    unittest.main()
