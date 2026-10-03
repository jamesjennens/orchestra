"""`POST /v1/projects` on the endpoint backend registers an existing canonical project
(kittrial-5bb.80).

On the endpoint backend a project's tasks live in a canonical Beads project that only an
operator creates on the host (`admin.py add-project NAME`). The HTTP route therefore only
registers one that exists: superuser only, with a refusal for everyone else that never
depends on whether the name exists; the id is the canonical name; one canonical project
is registered once; only the registering superuser becomes a member. A project record
from an older kit (a `proj_...` id) is listed as not usable and its task routes answer a
clear 409. The in-process backend keeps its service-local create unchanged.
"""
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
from test_http_review_fixes import STUB, EndpointCase, Harness


class CountingBackend(http_service.EndpointBackend):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.projects = []

    def _endpoint(self, action, project, *args, **kwargs):
        # Recorded only once the endpoint process really ran (the base class refuses an
        # unusable project before it starts one).
        reply = super()._endpoint(action, project, *args, **kwargs)
        self.projects.append(project)
        return reply


class RegisterHarness(EndpointCase):
    """The endpoint backend over the strict stub, counting real endpoint runs."""

    def make_backend(self):
        self.canonical_root = self.tmp / 'canonical'
        return CountingBackend(sys.executable, str(STUB), str(self.canonical_root), service=self.service)

    def alex(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        return admin, self.login('alex', 'alex-password-1')[0]

    def register(self, token, body):
        return self.request('POST', '/v1/projects', body, token=token)


class RegisterCase(RegisterHarness):
    def test_the_session_says_how_projects_are_made(self):
        admin, alex = self.alex()
        self.assertEqual(self.request('GET', '/v1/sessions/current', token=admin).data['project_create'], 'register')
        self.assertEqual(self.request('GET', '/v1/sessions/current', token=alex).data['project_create'],
                         'operator-only')

    def test_only_a_superuser_registers_and_the_refusal_never_reveals_a_name(self):
        admin, alex = self.alex()
        self.initialize_canonical('real')
        self.backend.projects = []
        refusals = [self.register(alex, body) for body in ({'project_id': 'real'}, {'project_id': 'nosuch'},
                                                           {'name': 'Anything'}, {'project_id': 'Bad_Name'})]
        self.assertEqual({response.status for response in refusals}, {403})
        self.assertEqual(len({json.dumps(response.data['error'], sort_keys=True) for response in refusals}), 1)
        self.assertIn('admin.py add-project', refusals[0].data['error']['message'])
        self.assertEqual(self.backend.projects, [])            # no existence check ran for them
        self.assertEqual(self.request('GET', '/v1/projects', token=admin).data['items'], [])

    def test_a_superuser_registers_an_existing_canonical_project_only(self):
        admin, alex = self.alex()
        for body, message in (({'name': 'Alpha'}, 'project_id must be the canonical project name'),
                              ({'project_id': 'Alpha'}, 'project_id must be the canonical project name'),
                              ({'project_id': 'a'}, 'project_id must be the canonical project name'),
                              ({'project_id': 'proj_1234'}, 'project_id must be the canonical project name'),
                              ({'project_id': 'nosuch'}, 'No canonical project nosuch exists')):
            response = self.register(admin, body)
            self.assertEqual(422, response.status, response.data)
            self.assertIn(message, response.data['error']['message'])
        self.assertEqual(self.request('GET', '/v1/projects', token=admin).data['items'], [])   # nothing stored
        self.initialize_canonical('alpha')
        made = self.register(admin, {'project_id': 'alpha', 'name': 'Alpha project'})
        self.assertEqual(201, made.status, made.data)
        self.assertEqual((made.data['id'], made.data['name'], made.data['role'], made.data['members']),
                         ('alpha', 'Alpha project', 'owner', [self.admin_user['id']]))
        listed = self.request('GET', '/v1/projects', token=admin).data['items']
        self.assertEqual([(item['id'], item['usable']) for item in listed], [('alpha', True)])
        # Registration gives nobody else access.
        self.assertEqual(self.request('GET', '/v1/projects', token=alex).data['items'], [])
        self.assertEqual(404, self.request('GET', '/v1/projects/alpha/tasks', token=alex).status)
        # The project works: its tasks reach the canonical project.
        created = self.create_task(admin, 'alpha', 'first canonical task')
        self.assertEqual(201, created.status, created.data)
        self.assertEqual(1, self.request('GET', '/v1/projects/alpha/tasks', token=admin).data['total'])
        # The name defaults to the canonical id.
        self.initialize_canonical('beta')
        self.assertEqual(self.register(admin, {'project_id': 'beta'}).data['name'], 'beta')

    def test_one_canonical_project_is_registered_once(self):
        admin, _ = self.alex()
        self.initialize_canonical('alpha')
        self.assertEqual(201, self.register(admin, {'project_id': 'alpha', 'name': 'Alpha'}).status)
        again = self.register(admin, {'project_id': 'alpha', 'name': 'Another name'})
        self.assertEqual(409, again.status, again.data)
        self.assertIn("already registered as project 'Alpha'", again.data['error']['message'])
        self.assertEqual(200, self.request('POST', '/v1/projects/alpha/archive', {}, token=admin).status)
        archived = self.register(admin, {'project_id': 'alpha'})
        self.assertEqual(409, archived.status, archived.data)
        self.assertIn('(archived)', archived.data['error']['message'])

    def test_an_older_record_without_a_canonical_project_is_marked_and_refused_clearly(self):
        admin, _ = self.alex()
        # What an older kit created from the web UI: an HTTP record with a proj_ id.
        with self.service.store.lock:
            self.service.state['projects']['proj_0123456789abcdef'] = {
                'id': 'proj_0123456789abcdef', 'name': 'Made in the web UI', 'created_by': self.admin_user['id'],
                'created_at': '2026-10-02T00:00:00Z', 'archived': False}
            self.service.state['memberships']['proj_0123456789abcdef'] = {self.admin_user['id']: 'owner'}
            self.service.store.save()
        listed = self.request('GET', '/v1/projects', token=admin).data['items']
        self.assertEqual([(item['id'], item['usable']) for item in listed], [('proj_0123456789abcdef', False)])
        self.assertIn('archive it', listed[0]['unusable_reason'])
        self.assertFalse(self.request('GET', '/v1/projects/proj_0123456789abcdef', token=admin).data['usable'])
        self.backend.projects = []
        for method, path, body in (('GET', '/v1/projects/proj_0123456789abcdef/tasks', None),
                                   ('POST', '/v1/projects/proj_0123456789abcdef/tasks', {'title': 'x'})):
            response = self.request(method, path, body, token=admin)
            self.assertEqual(409, response.status, response.data)
            self.assertIn('no canonical Beads project', response.data['error']['message'])
        self.assertEqual(self.backend.projects, [])            # refused before any endpoint call
        self.assertEqual(200, self.request('POST', '/v1/projects/proj_0123456789abcdef/archive', {},
                                           token=admin).status)


class InProcessCreateCase(Harness):
    """The service-local backend keeps its create route unchanged."""

    def test_create_is_unchanged_and_the_session_says_so(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        self.assertEqual(self.request('GET', '/v1/sessions/current', token=alex).data['project_create'], 'create')
        project = self.create_project(alex, 'Alpha')
        self.assertTrue(project.startswith('proj_'))
        listed = self.request('GET', '/v1/projects', token=alex).data['items']
        self.assertEqual([(item['id'], item['usable']) for item in listed], [(project, True)])
        self.assertEqual(201, self.create_task(alex, project, 'works in process').status)


class FollowUpCase(RegisterHarness):
    """kittrial-5bb.84: legacy records with a canonical id, the idempotent retry, and
    what stays possible on a record the backend will not serve."""

    def legacy(self, project_id, creator, initialized=True, **extra):
        """A record as the kit before kittrial-5bb.80 left it: any account could POST
        {project_id: <canonical name>} and become owner of that canonical project."""
        if initialized:
            self.initialize_canonical(project_id)
        with self.service.store.lock:
            self.service.state['projects'][project_id] = dict({
                'id': project_id, 'name': 'Legacy ' + project_id, 'created_by': creator,
                'created_at': '2026-09-01T00:00:00Z', 'archived': False}, **extra)
            self.service.state['memberships'][project_id] = {creator: 'owner'}
            self.service.store.save()

    def user_id(self, token):
        return self.request('GET', '/v1/sessions/current', token=token).data['user']['id']

    def test_a_legacy_canonical_record_fails_closed_until_a_superuser_confirms_it(self):
        admin, alex = self.alex()
        alex_id = self.user_id(alex)
        self.legacy('legacy', alex_id)
        self.legacy('byadmin', self.admin_user['id'])
        self.legacy('proj_0123456789abcdef', alex_id, initialized=False)
        self.backend.projects = []
        listed = {item['id']: item for item in self.request('GET', '/v1/projects', token=alex).data['items']}
        self.assertEqual((listed['legacy']['usable'], listed['legacy']['needs_confirmation']), (False, True))
        self.assertIn('no superuser has confirmed it', listed['legacy']['unusable_reason'])
        self.assertEqual(listed['proj_0123456789abcdef']['needs_confirmation'], False)
        for method, path, body in (('GET', '/v1/projects/legacy/tasks', None),
                                   ('POST', '/v1/projects/legacy/tasks', {'title': 'x'})):
            response = self.request(method, path, body, token=alex)
            self.assertEqual(409, response.status, response.data)
            self.assertIn('no superuser has confirmed it', response.data['error']['message'])
        self.assertEqual(self.backend.projects, [])                       # refused before any endpoint call
        # A record a superuser created under the old kit needs nothing.
        mine = self.request('GET', '/v1/projects', token=admin).data['items']
        self.assertEqual(sorted((item['id'], item['usable']) for item in mine),
                         [('byadmin', True), ('legacy', False), ('proj_0123456789abcdef', False)])

        # The review list and the confirmation are superuser-only, with one refusal.
        refusals = [self.request('GET', '/v1/projects/unconfirmed', token=alex)] + [
            self.request('POST', '/v1/projects/%s/confirm' % name, {}, token=alex) for name in ('legacy', 'nosuch')]
        self.assertEqual({response.status for response in refusals}, {403})
        self.assertEqual(json.dumps(refusals[1].data['error'], sort_keys=True),
                         json.dumps(refusals[2].data['error'], sort_keys=True))
        self.assertEqual(self.backend.projects, [])
        review = self.request('GET', '/v1/projects/unconfirmed', token=admin).data
        self.assertEqual([(item['id'], item['kind']) for item in review['items']],
                         [('legacy', 'unconfirmed'), ('proj_0123456789abcdef', 'no-canonical')])
        self.assertEqual((review['items'][0]['created_by'], review['items'][0]['members']),
                         ({'id': alex_id, 'username': 'alex'}, [{'id': alex_id, 'username': 'alex', 'role': 'owner'}]))

        self.assertEqual(404, self.request('POST', '/v1/projects/nosuch/confirm', {}, token=admin).status)
        self.assertEqual(409, self.request('POST', '/v1/projects/byadmin/confirm', {}, token=admin).status)
        self.assertEqual(409, self.request('POST', '/v1/projects/proj_0123456789abcdef/confirm', {},
                                           token=admin).status)
        self.legacy('ghost', alex_id, initialized=False)                  # no canonical project behind it
        gone = self.request('POST', '/v1/projects/ghost/confirm', {}, token=admin)
        self.assertEqual(422, gone.status, gone.data)
        self.assertIn('Archive it', gone.data['error']['message'])

        done = self.request('POST', '/v1/projects/legacy/confirm', {}, token=admin, key='confirm-key-0001')
        self.assertEqual(200, done.status, done.data)
        self.assertEqual((done.data['usable'], done.data['confirmed_by'], done.data['created_by']['username'],
                          [member['username'] for member in done.data['members']]),
                         (True, self.admin_user['id'], 'alex', ['alex']))
        again = self.request('POST', '/v1/projects/legacy/confirm', {}, token=admin, key='confirm-key-0001')
        self.assertEqual((again.status, again.data), (200, done.data))    # an exact retry replays
        self.assertEqual(409, self.request('POST', '/v1/projects/legacy/confirm', {}, token=admin).status)
        record = self.service.state['projects']['legacy']
        self.assertEqual((record['confirmed_by'], bool(record['confirmed_at'])), (self.admin_user['id'], True))
        audit = [event for event in self.service.state['audit'] if event['action'] == 'projects.confirm']
        self.assertEqual([(event['user_id'], event['outcome'], event['reason']) for event in audit
                          if event['outcome'] == 'committed'], [(self.admin_user['id'], 'committed', 'confirm legacy')])
        # Members were not changed, and the project works again.
        self.assertEqual(self.service.state['memberships']['legacy'], {alex_id: 'owner'})
        self.assertTrue(self.request('GET', '/v1/projects/legacy', token=alex).data['usable'])
        self.assertEqual(201, self.create_task(alex, 'legacy', 'works after confirmation').status)

    def test_a_registration_is_marked_and_stays_usable_whoever_its_creator_becomes(self):
        admin, _ = self.alex()
        self.initialize_canonical('alpha')
        self.assertEqual(201, self.request('POST', '/v1/projects', {'project_id': 'alpha'}, token=admin).status)
        record = self.service.state['projects']['alpha']
        self.assertEqual(record['registered_by'], self.admin_user['id'])
        with self.service.store.lock:                                     # the registrar is demoted later
            self.service.state['users'][self.admin_user['id']]['superuser'] = False
        self.assertIsNone(http_service.project_unusable(self.service, 'alpha'))
        del record['registered_by']                                       # a record without the mark would not be
        self.assertEqual(http_service.project_unusable(self.service, 'alpha')[0], 'unconfirmed')

    def test_an_exact_retry_of_a_registration_replays_its_201(self):
        admin, _ = self.alex()
        self.initialize_canonical('alpha')
        body = {'project_id': 'alpha', 'name': 'Alpha'}
        first = self.request('POST', '/v1/projects', body, token=admin, key='register-key-0001')
        self.assertEqual(201, first.status, first.data)
        again = self.request('POST', '/v1/projects', body, token=admin, key='register-key-0001')
        self.assertEqual((again.status, again.data), (201, first.data))
        # Anything that is not that exact retry is still the conflict, and stores nothing.
        for key, changed in (('register-key-0001', {'project_id': 'alpha', 'name': 'Other'}),
                             ('register-key-0002', body), (None, body)):
            response = self.request('POST', '/v1/projects', changed, token=admin, key=key)
            self.assertEqual(409, response.status, response.data)
        self.assertEqual(len(self.service.state['projects']), 1)
        # A refused retry holds no reservation: the same key works for the next registration.
        self.initialize_canonical('beta')
        self.assertEqual(201, self.request('POST', '/v1/projects', {'project_id': 'beta'}, token=admin,
                                           key='register-key-0002').status)

    def test_an_unusable_record_refuses_what_grants_access_and_allows_what_removes_it(self):
        admin, alex = self.alex()
        alex_id = self.user_id(alex)
        blair_id = self.create_account(admin, 'blair', 'blair-password-1')
        # Usable first (confirmed), so it can be given a member, a credential and an agent grant.
        self.legacy('legacy', alex_id, confirmed_by=self.admin_user['id'])
        self.assertEqual(200, self.request('PUT', '/v1/projects/legacy/members/%s' % blair_id, {'role': 'viewer'},
                                           token=alex).status)
        credential = self.issue_credential(alex, 'legacy')
        agent = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'working_directory': '/home/alex/k',
                                                    'projects': ['legacy']}, token=alex)
        self.assertEqual(201, agent.status, agent.data)
        agent_id = agent.data['agent']['id']
        with self.service.store.lock:                                     # ... then as the upgrade finds it
            del self.service.state['projects']['legacy']['confirmed_by']
        self.legacy('proj_0123456789abcdef', alex_id, initialized=False)

        for project in ('legacy', 'proj_0123456789abcdef'):
            for label, response in (
                    ('member set', self.request('PUT', '/v1/projects/%s/members/%s' % (project, blair_id),
                                                {'role': 'owner'}, token=alex)),
                    ('credential issue', self.request('POST', '/v1/projects/%s/worker-credentials' % project,
                                                      {'label': 'worker'}, token=alex)),
                    ('agent create', self.request('POST', '/v1/agents', {
                        'name': 'Merlin ' + project[:4], 'working_directory': '/home/alex/m',
                        'projects': [project]}, token=alex))):
                with self.subTest(project=project, route=label):
                    self.assertEqual(409, response.status, response.data)
                    self.assertIn('nothing that grants access is accepted', response.data['error']['message'])
                    self.assertIn('removing a member, revoking a credential', response.data['error']['message'])
        second = self.request('POST', '/v1/agents', {'name': 'Osprey', 'working_directory': '/home/alex/o'},
                              token=alex).data['agent']['id']
        granted = self.request('PATCH', '/v1/agents/%s' % second, {'projects': ['legacy']}, token=alex)
        self.assertEqual(409, granted.status, granted.data)               # a NEW grant by update
        kept = self.request('PATCH', '/v1/agents/%s' % agent_id, {'projects': ['legacy'], 'notes': 'still editable'},
                            token=alex)
        self.assertEqual(200, kept.status, kept.data)                     # a grant already held is left alone
        self.assertEqual(self.service.state['memberships']['legacy'][blair_id], 'viewer')

        # Reads still work, and so does everything that removes access.
        self.assertEqual(200, self.request('GET', '/v1/projects/legacy/members', token=alex).status)
        self.assertEqual(200, self.request('DELETE', '/v1/projects/legacy/agents/%s' % agent_id, token=alex).status)
        self.assertEqual(204, self.request('POST', '/v1/projects/legacy/worker-credentials/%s/revoke'
                                           % credential['id'], {}, token=alex).status)
        self.assertEqual(200, self.request('DELETE', '/v1/projects/legacy/members/%s' % blair_id,
                                           token=alex).status)
        # A superuser retires such a record without being a member of it.
        self.assertNotIn(self.admin_user['id'], self.service.state['memberships']['legacy'])
        self.assertEqual(200, self.request('POST', '/v1/projects/legacy/archive', {}, token=admin).status)
        self.assertTrue(self.service.state['projects']['legacy']['archived'])

    def test_the_host_check_lists_the_records_without_opening_the_store(self):
        import contextlib
        import io
        admin, alex = self.alex()
        self.legacy('legacy', self.user_id(alex))
        state = self.service.store.path
        before = {path.name: path.stat().st_mtime_ns for path in state.parent.iterdir()}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, http_service.main(['--state', str(state), '--list-unconfirmed-projects']))
        listed = json.loads(out.getvalue())
        self.assertEqual([(item['id'], item['kind'], item['created_by']['username']) for item in listed['items']],
                         [('legacy', 'unconfirmed', 'alex')])
        self.assertEqual(before, {path.name: path.stat().st_mtime_ns for path in state.parent.iterdir()})
        missing = state.parent / 'typo.json'
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            http_service.main(['--state', str(missing), '--list-unconfirmed-projects'])
        self.assertFalse(missing.exists())


class InProcessFollowUpCase(Harness):
    """The in-process backend has no such rule: nothing is unusable and nothing is listed."""

    def test_the_in_process_backend_is_unchanged(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        made = self.request('POST', '/v1/projects', {'name': 'Alpha', 'project_id': 'alpha'}, token=alex)
        self.assertEqual(201, made.status, made.data)
        self.assertEqual((made.data.get('usable'), 'registered_by' in self.service.state['projects']['alpha']),
                         (None, False))
        self.assertTrue(self.request('GET', '/v1/projects/alpha', token=alex).data['usable'])
        self.assertEqual(201, self.request('POST', '/v1/projects/alpha/worker-credentials', {'label': 'w'},
                                           token=alex).status)
        self.assertEqual(self.request('GET', '/v1/projects/unconfirmed', token=admin).data, {'items': [], 'total': 0})


class WebViewTests(unittest.TestCase):
    """The Projects page offers what the server supports (static checks; the browser
    behaviour runs under node where it is installed)."""

    def test_the_projects_page_follows_the_session_mode(self):
        view = (KIT / 'web' / 'js' / 'views' / 'work.js').read_text(encoding='utf-8')
        api = (KIT / 'web' / 'js' / 'api.js').read_text(encoding='utf-8')
        self.assertIn("registerProject: (projectId, name) => mutate('POST', '/v1/projects'", api)
        for text in ("session.project_create", "mode === 'register'", 'admin.py add-project NAME',
                     'Registering makes you its owner and gives nobody else access',
                     "p.usable === false", "'Not usable'", 'ctx.api.archiveProject(p.id)'):
            self.assertIn(text, view)

    def test_the_pages_follow_usable_and_the_session_mode(self):
        """kittrial-5bb.84: the welcome text, the confirm panel and the unusable project page."""
        web = KIT / 'web' / 'js'
        work = (web / 'views' / 'work.js').read_text(encoding='utf-8')
        project = (web / 'views' / 'project.js').read_text(encoding='utf-8')
        task = (web / 'views' / 'task.js').read_text(encoding='utf-8')
        api = (web / 'api.js').read_text(encoding='utf-8')
        for text in ("unconfirmedProjects: () => call('GET', '/v1/projects/unconfirmed')",
                     "confirmProject: (pid) => mutate('POST', `/v1/projects/${pid}/confirm`, {})"):
            self.assertIn(text, api)
        welcome = work[work.index('export async function welcome'):work.index('export async function directory')]
        for text in ("session.project_create", "'operator-only': [", 'Ask an operator or a superuser',
                     "create ? h('div', null, create) : null"):
            self.assertIn(text, welcome)
        # The old sentence is offered only in `create` mode now.
        self.assertEqual(work.count('You become its owner and can invite colleagues'), 1)
        for text in ('Projects to confirm or archive', 'Created by ', 'These members keep their access',
                     'Your confirmation is recorded', 'ctx.api.confirmProject(p.id)'):
            self.assertIn(text, work)
        overview = project[project.index('export async function overview'):project.index('export async function reviews')]
        self.assertLess(overview.index('project.usable === false'), overview.index("'New task'"))
        self.assertIn('unusableBanner(project)', project[project.index('export async function settings'):])
        self.assertIn('project.usable === false', task)


if __name__ == '__main__':
    unittest.main()
