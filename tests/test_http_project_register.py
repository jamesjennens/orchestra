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


class RegisterCase(EndpointCase):
    def make_backend(self):
        self.canonical_root = self.tmp / 'canonical'
        return CountingBackend(sys.executable, str(STUB), str(self.canonical_root), service=self.service)

    def alex(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        return admin, self.login('alex', 'alex-password-1')[0]

    def register(self, token, body):
        return self.request('POST', '/v1/projects', body, token=token)

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


if __name__ == '__main__':
    unittest.main()
