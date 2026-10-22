"""Release grant and release-route authority over the disposable HTTP service.

The host writer is exercised in ``test_web_release_native.py``; this module pins the
grant storage, the route gates and the body shape that need no native bd.
"""
import sys
from pathlib import Path
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from test_http_agents import AgentHarness


class ReleaseHttpTests(AgentHarness):
    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.project = self.create_project(self.admin, 'Releases')
        self.releases = '/v1/projects/' + self.project + '/releases'
        self.query = self.releases + '/query'

    # -- helpers ---------------------------------------------------------------
    def owner(self, name='release-owner'):
        user = self.create_account(self.admin, name, name + '-password-1')
        result = self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, user),
                              {'role': 'owner'}, token=self.admin)
        self.assertEqual(result.status, 200, result.data)
        return user, self.login(name, name + '-password-1')[0]

    def member(self, name, role):
        user = self.create_account(self.admin, name, name + '-password-1')
        result = self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, user),
                              {'role': role}, token=self.admin)
        self.assertEqual(result.status, 200, result.data)
        return user, self.login(name, name + '-password-1')[0]

    def make_agent(self, token, name='Release agent'):
        result = self.request('POST', '/v1/agents',
                              dict(name=name, working_directory='/tmp/synthetic-release-agent',
                                   projects=[self.project]), token=token)
        self.assertEqual(result.status, 201, result.data)
        return result.data['agent']['id'], result.data['credential']['secret']

    def deploy_body(self, **changes):
        body = dict(scope=dict(source_commit='', integration_commit='c' * 40,
                               release_id='synthetic-release', environment='synthetic-env'),
                    evidence=['build:synthetic'], provenance='performed', live_verified=False,
                    targets=[dict(task='synthetic-task', source_commit='a' * 40,
                                  integration_commit='b' * 40)])
        body.update(changes)
        return body

    def post(self, url, body, token, key='release-route-key'):
        return self.request('POST', url, body, token=token, key=key)

    def grant(self, agent_id, method='PUT', token=None):
        return self.request(method, '/v1/projects/%s/agents/%s/release' % (self.project, agent_id),
                            {} if method == 'PUT' else None, token=token or self.admin)

    # -- who may release -------------------------------------------------------
    def test_coordinator_and_viewer_are_refused_before_the_body_is_read(self):
        _, coordinator = self.member('release-coordinator', 'coordinator')
        _, viewer = self.member('release-viewer', 'viewer')
        for token in (coordinator, viewer):
            refused = self.post(self.releases, self.deploy_body(), token)
            self.assertEqual(refused.status, 403, refused.data)
        # A query is a coordinator read, so the viewer is refused there too.
        self.assertEqual(self.post(self.query, {'scope': self.deploy_body()['scope']},
                                   viewer, key=None).status, 403)

    def test_coordinator_alone_cannot_record_a_release(self):
        _, coordinator = self.member('release-coordinator-2', 'coordinator')
        refused = self.post(self.releases, self.deploy_body(), coordinator)
        self.assertEqual(refused.status, 403, refused.data)

    def test_owner_reaches_the_host_route_and_a_missing_host_answers_501(self):
        # The disposable backend has no host release writer: the owner passes the route's
        # authority gate and the route answers 501 rather than pretending to release.
        _, token = self.owner()
        result = self.post(self.releases, self.deploy_body(), token)
        self.assertEqual(result.status, 501, result.data)
        self.assertEqual(self.post(self.query, {'scope': self.deploy_body()['scope']},
                                   token, key=None).status, 501)

    def test_another_projects_owner_cannot_reach_this_project(self):
        other = self.create_project(self.admin, 'Other release project')
        user = self.create_account(self.admin, 'other-release-owner', 'other-release-password-1')
        self.assertEqual(self.request('PUT', '/v1/projects/%s/members/%s' % (other, user),
                                      {'role': 'owner'}, token=self.admin).status, 200)
        token = self.login('other-release-owner', 'other-release-password-1')[0]
        refused = self.post(self.releases, self.deploy_body(), token)
        self.assertEqual(refused.status, 404, refused.data)

    # -- the grant -------------------------------------------------------------
    def test_release_grant_is_stored_shown_and_checked_live(self):
        owner_id, owner = self.owner()
        agent_id, secret = self.make_agent(owner)
        before = self.request('GET', '/v1/projects/%s/agents' % self.project, token=owner)
        self.assertEqual(before.status, 200, before.data)
        self.assertIsNone(next(row for row in before.data['items']
                               if row['id'] == agent_id)['release'])
        # Without the grant the agent is refused even though it holds a credential.
        self.assertEqual(self.post(self.releases, self.deploy_body(), secret).status, 403)
        made = self.grant(agent_id, token=owner)
        self.assertEqual(made.status, 200, made.data)
        self.assertTrue(made.data['release']['effective'])
        self.assertEqual(made.data['release']['granted_by'], owner_id)
        # With it, the agent reaches the host writer (501 on this backend, never 403).
        self.assertEqual(self.post(self.releases, self.deploy_body(), secret).status, 501)
        # The view the Members page reads carries the same row.
        listed = self.request('GET', '/v1/projects/%s/agents' % self.project, token=owner)
        row = next(row for row in listed.data['items'] if row['id'] == agent_id)
        self.assertEqual(row['release']['granted_by'], owner_id)
        self.assertTrue(row['release']['effective'])
        # Revoked: the very next request is refused.
        revoked = self.grant(agent_id, 'DELETE', token=owner)
        self.assertEqual(revoked.status, 200, revoked.data)
        self.assertIsNone(revoked.data['release'])
        self.assertEqual(self.post(self.releases, self.deploy_body(), secret).status, 403)

    def test_a_granted_agent_whose_account_loses_the_owner_role_is_refused(self):
        owner_id, owner = self.owner('release-demoted-owner')
        agent_id, secret = self.make_agent(owner, 'Demoted owner agent')
        self.assertEqual(self.grant(agent_id).status, 200)
        self.assertEqual(self.post(self.releases, self.deploy_body(), secret).status, 501)
        demoted = self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, owner_id),
                               {'role': 'coordinator'}, token=self.admin)
        self.assertEqual(demoted.status, 200, demoted.data)
        self.assertEqual(self.post(self.releases, self.deploy_body(), secret).status, 403)
        # The grant is still on record but reads as ineffective; a coordinator session
        # cannot give it, and the account that lost the owner role no longer can either.
        listed = self.request('GET', '/v1/projects/%s/agents' % self.project, token=self.admin)
        row = next(row for row in listed.data['items'] if row['id'] == agent_id)
        self.assertFalse(row['release']['effective'])
        self.assertEqual(self.grant(agent_id).status, 409)
        other_id, _ = self.make_agent(owner, 'Coordinator grant attempt')
        _, coordinator_token = self.member('release-grant-giver-2', 'coordinator')
        self.assertEqual(self.request('PUT', '/v1/projects/%s/agents/%s/release'
                                      % (self.project, other_id), {}, token=coordinator_token).status, 403)

    def test_a_grant_to_an_account_that_is_not_an_owner_is_refused(self):
        _, coordinator = self.member('release-grant-coordinator', 'coordinator')
        agent_id, _ = self.make_agent(coordinator, 'Coordinator agent')
        refused = self.grant(agent_id)
        self.assertEqual(refused.status, 409, refused.data)
        self.assertIn('owner', refused.data['error']['message'])

    def test_removing_an_agent_from_the_project_ends_its_release_grant(self):
        _, owner = self.owner('release-cleanup-owner')
        agent_id, secret = self.make_agent(owner, 'Cleanup agent')
        self.assertEqual(self.grant(agent_id, token=owner).status, 200)
        self.assertIn(agent_id, self.service.state['release_grants'][self.project])
        removed = self.request('DELETE', '/v1/projects/%s/agents/%s' % (self.project, agent_id),
                               token=owner)
        self.assertEqual(removed.status, 200, removed.data)
        self.assertNotIn(self.project, self.service.state.get('release_grants') or {})
        # The credential no longer reaches this project at all.
        self.assertIn(self.post(self.releases, self.deploy_body(), secret).status, (403, 404))

    # -- body shape ------------------------------------------------------------
    def test_more_than_two_hundred_targets_is_422_for_an_owner(self):
        _, token = self.owner('release-body-owner')
        targets = [dict(task='t%d' % index, source_commit='a' * 40, integration_commit='b' * 40)
                   for index in range(201)]
        refused = self.post(self.releases, self.deploy_body(targets=targets), token)
        self.assertEqual(refused.status, 422, refused.data)

    def test_server_supplied_fields_are_refused(self):
        _, token = self.owner('release-field-owner')
        for field in ('actor', 'operation_id', 'schema_version', 'dimension', 'value'):
            refused = self.post(self.releases, self.deploy_body(**{field: True if field != 'actor'
                                                                  else 'someone'}), token)
            self.assertEqual(refused.status, 422, refused.data)
            self.assertIn(field, refused.data['error']['message'])

    def test_the_query_accepts_only_its_scope(self):
        _, token = self.owner('release-query-owner')
        refused = self.request('POST', self.query, dict(scope=self.deploy_body()['scope'],
                                                        targets=[]), token=token, key=None)
        self.assertEqual(refused.status, 422, refused.data)


if __name__ == '__main__':
    unittest.main()
