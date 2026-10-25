"""Who reads every lane on the web route (kittrial-5bb.405): an authorization rule, one test each.

``GET /v1/projects/<pid>/lanes`` answers scope ``all`` to exactly three principals and ``own`` to
everyone else: an installation operator (as before), a person whose role IN THAT PROJECT is owner
or coordinator, and an agent with an EFFECTIVE coordinator grant in that project. The decision is
one live ``check_authority(CAP_COORDINATE)`` per read, so a role change, a revoked grant or an
expired credential takes effect on the very next read. Every case below names one principal.
"""
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))

from http_auth import HttpError
import test_coordinator_grant as grants

try:
    import endpoint
except ImportError:               # endpoint.py imports fcntl; POSIX only
    endpoint = None


class ScopeCase(grants.Grants):
    """olive and oscar own Alpha, cora coordinates it (and has agent Heron), carl contributes."""

    def setUp(self):
        super().setUp()
        for name in ('vera', 'otto', 'dave'):
            self.ids[name] = self.create_account(self.admin, name, name + '-password-1')
            self.tokens[name] = self.login(name, name + '-password-1')[0]
        self.assertEqual(200, self.member('vera', 'viewer').status)
        self.assertEqual(200, self.member('dave', 'contributor').status)
        # A task each for carl and dave, so there are two lanes to see or not see.
        self.carl_task = self.claimed('carl')
        self.dave_task = self.claimed('dave')

    def claimed(self, name):
        task = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': name + ' work'},
                            token=self.admin).data['id']
        self.assertEqual(200, self.request('POST', self.base(task) + '/claim', {}, token=self.tokens[name]).status)
        return task

    def lanes(self, token, project=None, query=''):
        return self.request('GET', '/v1/projects/%s/lanes%s' % (project or self.project, query), token=token)

    def actors(self, answer):
        return {row['actor'] for row in answer.data['items']}

    def plant_coordination(self, operators=()):
        document = {'schema_version': 1, 'operators': sorted(operators), 'sessions': [], 'sessions_note': None}
        self.backend.host_coordination = lambda project_id, fresh=False: dict(document)

    def both(self):
        return {self.ids['carl'], self.ids['dave']}

    def assertAll(self, answer, basis, role=None):
        self.assertEqual(200, answer.status, answer.data)
        self.assertEqual(('all', basis, role), (answer.data['scope'], answer.data['scope_basis'],
                                                answer.data['scope_role']), answer.data)
        self.assertEqual(self.both(), self.actors(answer))

    def assertOwn(self, answer, actor):
        self.assertEqual(200, answer.status, answer.data)
        self.assertEqual(('own', 'own'), (answer.data['scope'], answer.data['scope_basis']), answer.data)
        self.assertLessEqual(self.actors(answer), {actor})
        self.assertNotIn(self.ids['carl'] if actor != self.ids['carl'] else self.ids['dave'],
                         json.dumps(answer.data['items']))

    def watch_reads(self):
        """Record each canonical lanes read, so a refusal can be shown to have read nothing."""
        planted, asked = self.backend.lanes_read, []

        def watch(*args, **kwargs):
            asked.append(args)
            return planted(*args, **kwargs)
        self.backend.lanes_read = watch
        return asked


class PersonRoleTests(ScopeCase):
    def test_a_project_owner_who_is_no_operator_reads_every_lane(self):
        self.assertAll(self.lanes(self.tokens['olive']), 'project-role', 'owner')

    def test_a_project_coordinator_reads_every_lane(self):
        self.assertAll(self.lanes(self.tokens['cora']), 'project-role', 'coordinator')

    def test_a_superuser_reads_every_lane_as_on_every_coordinator_route(self):
        # decide() treats a superuser as an owner of every project: one rule, so also here.
        self.assertAll(self.lanes(self.admin), 'project-role', 'owner')

    def test_a_contributor_sees_only_their_own_lane(self):
        self.assertOwn(self.lanes(self.tokens['carl']), self.ids['carl'])

    def test_a_viewer_sees_no_other_lane(self):
        answer = self.lanes(self.tokens['vera'])
        self.assertOwn(answer, self.ids['vera'])
        self.assertEqual([], answer.data['items'])

    def test_a_non_member_is_refused_before_anything_is_read(self):
        asked = self.watch_reads()
        answer = self.lanes(self.tokens['otto'])
        self.assertEqual(404, answer.status, answer.data)
        self.assertEqual([], asked)

    def test_the_owner_of_another_project_gets_the_refusal_here_never_all(self):
        elsewhere = self.create_project(self.tokens['otto'], 'Elsewhere')
        self.assertEqual(200, self.lanes(self.tokens['otto'], elsewhere).status)
        asked = self.watch_reads()
        answer = self.lanes(self.tokens['otto'])
        self.assertEqual(404, answer.status, answer.data)
        self.assertNotIn(self.ids['carl'], json.dumps(answer.data))
        self.assertEqual([], asked)

    def test_an_owner_here_who_only_contributes_in_another_project_gets_own_there(self):
        other = self.create_project(self.admin, 'Beta')
        for name, role in (('olive', 'contributor'), ('carl', 'contributor')):
            self.assertEqual(200, self.member(name, role, token=self.admin, project=other).status)
        task = self.request('POST', '/v1/projects/%s/tasks' % other, {'title': 'beta work'}, token=self.admin).data['id']
        self.assertEqual(200, self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (other, task), {},
                                           token=self.tokens['carl']).status)
        answer = self.lanes(self.tokens['olive'], other)
        self.assertEqual(200, answer.status, answer.data)
        self.assertEqual(('own', 'own'), (answer.data['scope'], answer.data['scope_basis']))
        self.assertNotIn(self.ids['carl'], json.dumps(answer.data['items']))
        self.assertEqual('project-role', self.lanes(self.tokens['olive']).data['scope_basis'])

    def test_a_demoted_owner_gets_own_on_the_next_read(self):
        self.assertAll(self.lanes(self.tokens['oscar']), 'project-role', 'owner')
        self.assertEqual(200, self.member('oscar', 'contributor', token=self.admin).status)
        self.assertOwn(self.lanes(self.tokens['oscar']), self.ids['oscar'])

    def test_a_demoted_coordinator_gets_own_on_the_next_read(self):
        self.assertAll(self.lanes(self.tokens['cora']), 'project-role', 'coordinator')
        self.assertEqual(200, self.member('cora', 'viewer').status)
        self.assertOwn(self.lanes(self.tokens['cora']), self.ids['cora'])

    def test_a_promoted_contributor_gets_every_lane_on_the_next_read(self):
        self.assertOwn(self.lanes(self.tokens['dave']), self.ids['dave'])
        self.assertEqual(200, self.member('dave', 'coordinator').status)
        self.assertAll(self.lanes(self.tokens['dave']), 'project-role', 'coordinator')

    def test_a_removed_member_is_refused(self):
        self.assertEqual(200, self.request('DELETE', '/v1/projects/%s/members/%s' % (self.project, self.ids['cora']),
                                           token=self.tokens['olive']).status)
        self.assertEqual(404, self.lanes(self.tokens['cora']).status)

    def test_a_query_cannot_ask_for_a_wider_scope(self):
        for query in ('?scope=all', '?lanes_scope=all', '?scope_basis=project-role'):
            with self.subTest(query=query):
                answer = self.lanes(self.tokens['carl'], query=query)
                self.assertOwn(answer, self.ids['carl'])

    def test_a_cursor_from_an_all_read_does_not_carry_the_scope_after_a_demotion(self):
        first = self.lanes(self.tokens['oscar'], query='?limit=1')
        self.assertEqual('all', first.data['scope'])
        self.assertTrue(first.data['next_cursor'])
        self.assertEqual(200, self.member('oscar', 'contributor', token=self.admin).status)
        again = self.lanes(self.tokens['oscar'], query='?limit=1&cursor=' + first.data['next_cursor'])
        if again.status == 200:
            self.assertEqual('own', again.data['scope'])
            self.assertNotIn(self.ids['carl'], json.dumps(again.data['items']))
            self.assertNotIn(self.ids['dave'], json.dumps(again.data['items']))


class OperatorTests(ScopeCase):
    def test_an_installation_operator_reads_every_lane_even_as_a_contributor(self):
        self.plant_coordination(operators=[self.ids['carl']])
        answer = self.lanes(self.tokens['carl'])
        self.assertAll(answer, 'operator')

    def test_an_owner_is_all_whether_or_not_the_host_can_name_operators(self):
        self.assertFalse(hasattr(self.backend, 'host_coordination'))
        self.assertAll(self.lanes(self.tokens['olive']), 'project-role', 'owner')

        def broken(project_id, fresh=False):
            raise HttpError(503, 'unavailable', 'the host cannot answer')
        self.backend.host_coordination = broken
        self.assertAll(self.lanes(self.tokens['olive']), 'project-role', 'owner')

    def test_a_contributor_stays_own_when_the_host_cannot_name_operators(self):
        def broken(project_id, fresh=False):
            raise HttpError(503, 'unavailable', 'the host cannot answer')
        self.backend.host_coordination = broken
        self.assertOwn(self.lanes(self.tokens['carl']), self.ids['carl'])

    def test_a_listed_operator_who_is_a_non_member_is_still_refused(self):
        self.plant_coordination(operators=[self.ids['otto']])
        asked = self.watch_reads()
        self.assertEqual(404, self.lanes(self.tokens['otto']).status)
        self.assertEqual([], asked)


class AgentGrantTests(ScopeCase):
    def test_an_agent_with_an_effective_grant_reads_every_lane(self):
        self.assertEqual(200, self.grant().status)
        self.assertAll(self.lanes(self.agent), 'coordinator-grant')

    def test_an_agent_without_a_grant_reads_only_its_own_lane(self):
        answer = self.lanes(self.agent)
        self.assertEqual(200, answer.status, answer.data)
        self.assertEqual(('own', 'own'), (answer.data['scope'], answer.data['scope_basis']))
        self.assertEqual([], answer.data['items'])

    def test_a_revoked_grant_gets_own_on_the_next_read(self):
        self.assertEqual(200, self.grant().status)
        self.assertAll(self.lanes(self.agent), 'coordinator-grant')
        self.assertEqual(200, self.grant(method='DELETE').status)
        answer = self.lanes(self.agent)
        self.assertEqual(('own', 'own'), (answer.data['scope'], answer.data['scope_basis']))
        self.assertEqual([], answer.data['items'])

    def test_a_grant_whose_account_was_demoted_is_not_effective(self):
        self.assertEqual(200, self.grant().status)
        self.assertAll(self.lanes(self.agent), 'coordinator-grant')
        self.assertEqual(200, self.member('cora', 'contributor').status)
        answer = self.lanes(self.agent)
        self.assertEqual(('own', 'own'), (answer.data['scope'], answer.data['scope_basis']))
        self.assertEqual([], answer.data['items'])
        # Raised again, the recorded grant is effective again, with no new grant written.
        self.assertEqual(200, self.member('cora', 'coordinator').status)
        self.assertAll(self.lanes(self.agent), 'coordinator-grant')

    def test_an_expired_credential_of_a_granted_agent_reads_nothing(self):
        self.assertEqual(200, self.grant().status)
        credential = next(c for c in self.service.state['credentials'].values() if c.get('agent_id') == self.agent_id)
        credential['expires_at'] = 1
        asked = self.watch_reads()
        answer = self.lanes(self.agent)
        self.assertEqual(401, answer.status, answer.data)
        self.assertEqual([], asked)

    def test_a_revoked_credential_of_a_granted_agent_reads_nothing(self):
        self.assertEqual(200, self.grant().status)
        credential = next(c for c in self.service.state['credentials'].values() if c.get('agent_id') == self.agent_id)
        credential['revoked'] = True
        asked = self.watch_reads()
        self.assertEqual(401, self.lanes(self.agent).status)
        self.assertEqual([], asked)

    def test_a_grant_in_another_project_does_not_widen_this_one(self):
        other = self.create_project(self.admin, 'Beta')
        self.assertEqual(200, self.member('cora', 'coordinator', token=self.admin, project=other).status)
        patched = self.request('PATCH', '/v1/agents/' + self.agent_id, {'projects': [self.project, other]},
                               token=self.tokens['cora'])
        self.assertEqual(200, patched.status, patched.data)
        self.assertEqual(200, self.grant(project=other, token=self.admin).status)
        here = self.lanes(self.agent)
        self.assertEqual(('own', 'own'), (here.data['scope'], here.data['scope_basis']))
        there = self.lanes(self.agent, other)
        self.assertEqual(('all', 'coordinator-grant'), (there.data['scope'], there.data['scope_basis']))

    def test_a_worker_credential_never_reads_every_lane(self):
        worker, _credential = self.worker_of('olive', 'lane-w')
        answer = self.lanes(worker)
        self.assertEqual(200, answer.status, answer.data)
        self.assertEqual(('own', 'own'), (answer.data['scope'], answer.data['scope_basis']))
        self.assertNotIn(self.ids['carl'], json.dumps(answer.data['items']))


class ParityTests(ScopeCase):
    """The route's scope in, the canonical read's rows out; the SSH bound-key rule is unchanged."""

    def rows(self, scope):
        return self.backend.lanes_read(self.project, None, ['--idle-minutes', '15', '--limit', '50', '--offset', '0'],
                                       self.ids['olive'], scope)

    def test_the_routes_rows_are_the_canonical_reads_rows_for_the_scope_it_chose(self):
        every = self.lanes(self.tokens['olive']).data
        self.assertEqual('all', every['scope'])
        # display_name is a route-only display field (kittrial-5bb.416,
        # http_service.py project_lanes): strip it before comparing, keeping
        # every canonical field in the comparison.
        route_rows = [{key: value for key, value in row.items() if key != 'display_name'}
                      for row in every['items']]
        self.assertEqual(self.rows('all')['items'], route_rows)
        # ... and assert it is present on the route's rows: known accounts read
        # as their display names, never as another name.
        names = {row['actor']: row.get('display_name') for row in every['items']}
        self.assertEqual('carl', names[self.ids['carl']])
        self.assertEqual('dave', names[self.ids['dave']])
        self.assertEqual(self.both(), {row['actor'] for row in self.rows('all')['items']})

    def test_the_bound_key_endpoint_rule_stays_operator_only(self):
        if endpoint is None:
            self.skipTest('endpoint.py needs a POSIX interpreter')
        self.assertEqual('own', endpoint.lanes_scope_for(self.ids['olive'], [], 'a-bound-key'))
        self.assertEqual('all', endpoint.lanes_scope_for(self.ids['olive'], [self.ids['olive']], 'a-bound-key'))
        self.assertEqual('own', endpoint.lanes_scope_requested('all', 'own'))
        self.assertEqual('own', endpoint.lanes_scope_requested('own', 'all'))


if __name__ == '__main__':
    unittest.main()
