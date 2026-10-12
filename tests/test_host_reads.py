"""The host-reading parts of kittrial-5bb.198: two read-only endpoint actions and what the web shows of them.

``host-coordination`` (the operator list, the project's session names and principals) and
``host-projects`` (the runtime's projects) are web-service-only reads, sent without an authority
block. The web service shows the operators on the "Who coordinates" panel, a session actor by its
name and principal, and, to a superuser, the initialized projects it has not registered that have
no creation record.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import test_bd_label_aliases as rb
import test_claim_held as held
import test_coordinator_grant
from http_auth import HttpError

try:
    import endpoint
except ImportError:          # fcntl: POSIX only
    endpoint = None

SESSION = 'session-0f0e0d0c-0b0a-4908-8706-050403020100'
OTHER = 'session-11111111-2222-4333-8444-555555555555'
NAMELESS = 'session-99999999-8888-4777-8666-555555555555'


def registry(owners=None):
    def record(request, actor, name):
        return {'request_id': request, 'actor': actor, 'name': name, 'created_at': '2026-10-12T09:00:00Z'}
    first, second = 'a0000000-0000-4000-8000-000000000001', 'a0000000-0000-4000-8000-000000000002'
    data = {'schema_version': 1, 'records': {first: record(first, SESSION, 'Cloud lane'),
                                             second: record(second, OTHER, 'Review desk')}}
    if owners is not None:
        data['owners'] = owners
    return data


class LabelTests(unittest.TestCase):
    def test_how_a_session_is_shown(self):
        label = http_service.session_label
        self.assertEqual(label({'name': 'Cloud lane', 'principal': 'person:james'}), 'Cloud lane (person james)')
        self.assertEqual(label({'name': 'Cloud lane', 'principal': 'lane:cloud'}), 'Cloud lane (lane cloud)')
        self.assertEqual(label({'name': 'Cloud lane', 'principal': None}), 'Cloud lane')
        self.assertEqual(label({'name': None, 'principal': 'person:james'}), 'a session of person james')
        for nothing in ({'name': None, 'principal': None}, {'name': '  ', 'principal': None}, None, 'Cloud lane'):
            self.assertIsNone(label(nothing))


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for name in ('alpha', 'beta'):
            (self.root / 'projects' / name / '.beads').mkdir(parents=True)
            (self.root / 'projects' / name / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        self.project = self.root / 'projects' / 'alpha'
        operators = mock.patch.object(endpoint, 'configured_operators', return_value={'james', 'ops-2'})
        operators.start(); self.addCleanup(operators.stop)

    def tree(self):
        return {str(path): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}

    def ask(self, action, project='alpha', **more):
        answer = endpoint.execute(self.root, dict({'project': project, 'actor': 'http/read', 'action': action, 'args': []}, **more))
        self.assertEqual(answer['returncode'], 0, answer)
        return json.loads(answer['stdout'])

    def test_host_coordination_answers_the_operators_and_each_sessions_name_and_principal(self):
        (self.project / '.sessions.json').write_text(json.dumps(registry({SESSION: 'person:james', 'legacy': 'lane:old'})),
                                                    encoding='utf-8')
        before = self.tree()
        body = self.ask('host-coordination')
        self.assertEqual(body['operators'], ['james', 'ops-2'])
        self.assertEqual(body['sessions'], [
            {'actor': SESSION, 'name': 'Cloud lane', 'principal': 'person:james'},
            {'actor': OTHER, 'name': 'Review desk', 'principal': None}])
        self.assertIsNone(body['sessions_note'])
        self.assertEqual(self.tree(), before)            # no lock file, no write

    def test_no_registry_is_no_sessions_and_a_damaged_one_is_said(self):
        self.assertEqual(self.ask('host-coordination')['sessions'], [])
        (self.project / '.sessions.json').write_text('{"schema_version": 1, "records": []}', encoding='utf-8')
        body = self.ask('host-coordination')
        self.assertEqual((body['operators'], body['sessions']), (['james', 'ops-2'], None))
        self.assertIn('cannot be read', body['sessions_note'])

    def test_host_projects_lists_the_projects_and_not_an_unfinished_creation(self):
        import project_creation as pc
        pc.write_record(self.root, 'beta', {'project': 'beta', 'by': pc.HOST, 'state': 'started', 'stage': 'configure',
                                            'started_at': '2026-10-12T09:00:00Z'})
        (self.root / 'projects' / 'stray').mkdir()                      # no metadata: not a project
        before = self.tree()
        self.assertEqual(self.ask('host-projects', project=None)['projects'], ['alpha'])
        self.assertEqual(self.tree(), before)

    def test_both_take_no_arguments_and_host_projects_names_no_project(self):
        for action, project in (('host-coordination', 'alpha'), ('host-projects', None)):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'without arguments'):
                endpoint.execute(self.root, {'project': project, 'actor': 'http/read', 'action': action, 'args': ['x']})
        with self.assertRaisesRegex(ValueError, 'names no project'):
            endpoint.execute(self.root, {'project': 'alpha', 'actor': 'http/read', 'action': 'host-projects', 'args': []})

    def test_a_bound_key_is_refused_both_before_anything_is_read(self):
        (self.project / '.sessions.json').write_text(json.dumps(registry()), encoding='utf-8')
        reached = AssertionError('the request reached a read')
        with mock.patch('host_reads.coordination', side_effect=reached), mock.patch('host_reads.projects', side_effect=reached):
            for action in ('host-coordination', 'host-projects'):
                for bound in ({'key_projects': ['alpha']}, {'key_principal': 'person:james'}):
                    with self.subTest(action=action, bound=bound), \
                            self.assertRaisesRegex(ValueError, 'available only to the web service'):
                        endpoint.execute(self.root, {'project': 'alpha', 'actor': 'alex/s1', 'action': action, 'args': []},
                                         **bound)

    def test_they_are_not_in_the_capability_table_so_a_block_is_never_sent(self):
        import http_authority
        for action in ('host-coordination', 'host-projects'):
            with self.subTest(action=action):
                self.assertIsNone(http_authority.required_capability({'project': 'alpha', 'action': action, 'args': []}))
        source = (KIT / 'http_service.py').read_text(encoding='utf-8')
        for action in ('host-coordination', 'host-projects'):
            sent = source[source.index("self._run('%s'" % action):]
            self.assertNotIn('authority', sent[:sent.index('\n')])


class CacheTests(unittest.TestCase):
    """The session names of a project are reused for a short while; the panel's read is fresh."""

    def test_reused_for_its_seconds_per_project_and_fresh_when_asked(self):
        backend = http_service.EndpointBackend.__new__(http_service.EndpointBackend)
        backend.actor_namespace = 'http'
        sent = []

        def run(action, project, actor, args, **more):
            sent.append((action, project, actor, args, more))
            return {'schema_version': 1, 'operators': [], 'sessions': [], 'sessions_note': None, 'n': len(sent)}
        backend._run = run
        clock = [1000.0]
        with mock.patch.object(http_service.time, 'monotonic', side_effect=lambda: clock[0]):
            self.assertEqual(backend.host_coordination('alpha')['n'], 1)
            clock[0] += backend.HOST_NAMES_SECONDS - 1
            self.assertEqual(backend.host_coordination('alpha')['n'], 1)          # reused
            self.assertEqual(backend.host_coordination('beta')['n'], 2)           # another project: asked
            self.assertEqual(backend.host_coordination('alpha', fresh=True)['n'], 3)
            clock[0] += backend.HOST_NAMES_SECONDS
            self.assertEqual(backend.host_coordination('alpha')['n'], 4)          # too old: asked again
        # Sent as a read, with no authority block.
        self.assertEqual({(action, actor, tuple(args), tuple(more)) for action, _, actor, args, more in sent},
                         {('host-coordination', 'http/read', (), ())})


class WebViewTests(test_coordinator_grant.Grants):
    """The web side, on the in-process backend with the host's two answers given."""

    def host(self, sessions=None, operators=('james',), error=None):
        asked = []

        def host_coordination(project_id, fresh=False):
            asked.append((project_id, fresh))
            if error is not None:
                raise error
            return {'schema_version': 1, 'operators': list(operators), 'sessions': sessions, 'sessions_note': None}
        self.backend.host_coordination = host_coordination
        return asked

    def coordination(self):
        answer = self.request('GET', '/v1/projects/%s/coordination' % self.project, token=self.tokens['carl'])
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    def test_the_panel_shows_the_operators_read_fresh(self):
        asked = self.host([])
        self.assertEqual(self.coordination()['host_operators'], {'state': 'available', 'operators': ['james']})
        self.assertEqual(asked, [(self.project, True)])
        for error, state in ((HttpError(502, 'bad_gateway', 'x', detail='ValueError: Unknown action'), 'unavailable'),
                             (HttpError(503, 'unavailable', 'busy'), 'unknown')):
            self.host(error=error)
            with self.subTest(state=state):
                self.assertEqual(self.coordination()['host_operators'], {'state': state, 'operators': []})
        del self.backend.host_coordination
        self.assertEqual(self.coordination()['host_operators'], {'state': 'not-applicable', 'operators': []})

    def names(self, actors, sessions, project=None):
        self.host(sessions)
        handler = http_service.ApiHandler.__new__(http_service.ApiHandler)
        handler.service, handler.backend = self.service, self.backend
        return handler._actor_names(actors, self.project if project is None else project)

    def test_a_session_actor_is_shown_by_its_name_and_principal_and_nothing_else_changes(self):
        sessions = [{'actor': SESSION, 'name': 'Cloud lane', 'principal': 'person:james'},
                    {'actor': NAMELESS, 'name': None, 'principal': None}]
        shown = self.names([SESSION, NAMELESS, self.ids['carl'], 'legacy-name', None], sessions)
        self.assertEqual(shown[SESSION], 'Cloud lane (person james)')
        self.assertEqual(shown[NAMELESS], NAMELESS)              # the host knows nothing more: the id
        self.assertEqual(shown[self.ids['carl']], 'carl')        # an account keeps its own name
        self.assertEqual(shown['legacy-name'], 'legacy-name')

    def test_the_host_is_asked_only_for_a_session_actor_and_only_with_a_project(self):
        asked = self.host([])
        handler = http_service.ApiHandler.__new__(http_service.ApiHandler)
        handler.service, handler.backend = self.service, self.backend
        handler._actor_names([self.ids['carl'], 'legacy-name'], self.project)
        handler._actor_names([SESSION], None)
        self.assertEqual(asked, [])
        handler._actor_names([SESSION], self.project)
        self.assertEqual(asked, [(self.project, False)])

    def test_a_host_that_cannot_answer_leaves_the_id(self):
        self.host(error=HttpError(503, 'unavailable', 'busy'))
        handler = http_service.ApiHandler.__new__(http_service.ApiHandler)
        handler.service, handler.backend = self.service, self.backend
        self.assertEqual(handler._actor_names([SESSION], self.project)[SESSION], SESSION)

    def test_the_merge_slot_says_its_holder_by_name_and_keeps_the_exact_one(self):
        self.host([{'actor': SESSION, 'name': 'Cloud lane', 'principal': 'lane:cloud'}])
        self.backend.merge_slot_read = lambda principal, project_id: {
            'available': False, 'holder': SESSION, 'context': None, 'missing': False, 'damaged': False}
        with mock.patch.object(http_service.ApiHandler, '_merge_slot_backend', lambda handler: None):
            answer = self.request('GET', '/v1/projects/%s/merge-slot' % self.project, token=self.tokens['carl'])
        self.assertEqual(200, answer.status, answer.data)
        self.assertEqual((answer.data['holder'], answer.data['holder_name']), (SESSION, 'Cloud lane (lane cloud)'))


class CreationsTests(test_coordinator_grant.Grants):
    """The superuser's list on a backend that registers host projects, with the host's answers given."""

    def setUp(self):
        super().setUp()
        self.backend.PROJECT_CREATE = 'register'
        self.backend.host_creations = lambda principal: {
            'items': [{'project': 'beta', 'state': 'incomplete', 'by': 'host'}],
            'created': [{'project': 'gamma', 'state': 'created', 'by': 'host'}], 'server': None, 'server_readable': True}

    def listed(self):
        answer = self.request('GET', '/v1/project-creations', token=self.admin)
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    def test_projects_with_no_record_and_no_registration_are_listed(self):
        self.backend.host_projects = lambda: [self.project, 'beta', 'gamma', 'delta', 'omega']
        found = self.listed()
        self.assertEqual([item['project'] for item in found['unrecorded']], ['delta', 'omega'])
        self.assertEqual({item['state'] for item in found['unrecorded']}, {'unrecorded-unregistered'})
        self.assertIn('no creation record', found['unrecorded'][0]['what'])
        self.assertIn('(Register a project, with this name)', found['unrecorded'][0]['what'])
        self.assertEqual(found['host_projects'], 'available')
        # The finished record stays where it was, once.
        self.assertEqual([item['project'] for item in found['unregistered']], ['gamma'])

    def test_what_cannot_be_listed_is_said(self):
        for error, state in ((HttpError(502, 'bad_gateway', 'x', detail='ValueError: Unknown action'), 'unavailable'),
                             (HttpError(503, 'unavailable', 'busy'), 'unknown')):
            def refuse(error=error):
                raise error
            self.backend.host_projects = refuse
            with self.subTest(state=state):
                found = self.listed()
                self.assertEqual((found['unrecorded'], found['host_projects']), ([], state))
        del self.backend.host_projects
        self.assertEqual(self.listed()['host_projects'], 'unavailable')

    def test_only_a_superuser_sees_it(self):
        self.backend.host_projects = lambda: ['delta']
        self.assertEqual(403, self.request('GET', '/v1/project-creations', token=self.tokens['olive']).status)


def only_its_own_tests(cls):
    for base in (rb.RealBdLabelAliasTests, held.RealStackTests):
        for name in dir(base):
            if name.startswith('test_') and name not in cls.__dict__:
                setattr(cls, name, None)
    return cls


@unittest.skipIf(rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@only_its_own_tests
class RealStackTests(held.RealStackTests):
    """The web service, the real endpoint.py and a real bd."""

    def put_registry(self):
        path = self.project / '.sessions.json'
        self.assertFalse(path.exists())
        path.write_text(json.dumps(registry({SESSION: 'person:james'})), encoding='utf-8')
        self.addCleanup(path.unlink)

    def test_a_task_held_by_a_session_names_the_session(self):
        self.put_registry()
        task = self.new('held by a session')
        updated = self.bd('update', task, '--assignee', SESSION)
        self.assertEqual(updated.returncode, 0, updated.stderr)
        one = self.request('GET', '%s/%s' % (self.tasks, task), token=self.tokens['casey'])
        self.assertEqual(200, one.status, one.data)
        self.assertEqual((one.data['assignee'], one.data['assignee_name']), (SESSION, 'Cloud lane (person james)'))
        listed = self.request('GET', self.tasks + '?status=', token=self.tokens['casey'])
        row = [item for item in listed.data['items'] if item['id'] == task][0]
        self.assertEqual(row['assignee_name'], 'Cloud lane (person james)')

    def test_the_panel_and_the_superusers_list_read_the_host(self):
        import admin
        coordination = self.request('GET', '/v1/projects/pp/coordination', token=self.tokens['casey'])
        self.assertEqual(200, coordination.status, coordination.data)
        self.assertEqual(coordination.data['host_operators'],
                         {'state': 'available', 'operators': sorted(admin.operators(self.root))})
        self.assertEqual(coordination.data['host_rules']['state'], 'available')
        # An initialized project with no creation record, not registered here.
        extra = self.root / 'projects' / 'qq'
        (extra / '.beads').mkdir(parents=True)
        (extra / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        self.addCleanup(lambda: __import__('shutil').rmtree(extra, ignore_errors=True))
        found = self.request('GET', '/v1/project-creations', token=self.admin_token())
        self.assertEqual(200, found.status, found.data)
        self.assertEqual((found.data['host_projects'], [item['project'] for item in found.data['unrecorded']]),
                         ('available', ['qq']))


if __name__ == '__main__':
    unittest.main()
