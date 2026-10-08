"""The coordinator role and the per-project grant that lets an agent approve (kittrial-5bb.209).

Slice 1b of docs/WEB_COORDINATOR_DESIGN.md. A project role ``coordinator`` between contributor
and owner; and a grant, kept with the project, by which an owner of the project (or a
superuser) lets one personal agent approve there. An agent that approves is judged by party
whatever the slice-1a setting says.

Through the real routes of the web service (the in-process backend); the endpoint backend
with a real bd is in tests/test_coordinator_grant_real.py.
"""
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_authority
import test_own_party

OWN_PARTY_AGENT = ('This contribution was delivered by this agent\'s own account (its owner, one of that account\'s '
                   'agents, or a worker credential it issued). An agent never approves work of its own account: another '
                   'coordinator or owner of this project, or a superuser who did not deliver it, must approve it.')


class Grants(test_own_party.Party):
    """olive and oscar own Alpha, carl contributes; ``cora`` is made a coordinator and has an agent."""

    SETTING = False

    def setUp(self):
        super().setUp()
        self.ids['cora'] = self.create_account(self.admin, 'cora', 'cora-password-1')
        self.tokens['cora'] = self.login('cora', 'cora-password-1')[0]
        self.assertEqual(200, self.member('cora', 'coordinator').status)
        self.agent, self.agent_id = self.agent_with_id('cora', 'Heron')

    def member(self, name, role, token=None, project=None):
        return self.request('PUT', '/v1/projects/%s/members/%s' % (project or self.project, self.ids[name]),
                            {'role': role}, token=token or self.tokens['olive'])

    def agent_with_id(self, name, label, projects=None):
        made = self.request('POST', '/v1/agents', {'name': label, 'working_directory': '/home/x/' + label,
                                                   'projects': projects or [self.project]}, token=self.tokens[name])
        self.assertEqual(201, made.status, made.data)
        return made.data['credential']['secret'], made.data['agent']['id']

    def grant(self, agent_id=None, token=None, project=None, method='PUT'):
        return self.request(method, '/v1/projects/%s/agents/%s/coordinator' % (project or self.project, agent_id or self.agent_id),
                            {} if method == 'PUT' else None, token=token or self.tokens['olive'])

    def listed(self, token=None):
        return {item['id']: item for item in self.request('GET', '/v1/projects/%s/agents' % self.project,
                                                          token=token or self.tokens['olive']).data['items']}


class RoleTests(Grants):

    def test_the_role_sits_between_contributor_and_owner(self):
        self.assertEqual(http_authority.ROLES, ('viewer', 'contributor', 'coordinator', 'owner'))
        rank = http_authority.RANK
        self.assertTrue(rank['contributor'] < rank['coordinator'] < rank['owner'])
        caps = http_authority.ROLE_CAPABILITIES
        self.assertEqual(caps['coordinator'] - caps['contributor'], {'reviews.approve', 'coordinate'})
        self.assertEqual(caps['owner'] - caps['coordinator'], {'project.admin'})
        self.assertEqual(caps['contributor'] - caps['coordinator'], frozenset())

    def test_a_person_who_is_a_coordinator_approves_and_does_not_administer(self):
        task, contribution = self.deliver(self.tokens['carl'])
        self.assertEqual(201, self.approve(self.tokens['cora'], task, contribution).status)
        self.assertEqual(self.state_of(task), 'approved')
        # Not the owner's: members, worker credentials, the audit, the set-up page, archiving.
        for method, path, body in (('PUT', '/members/%s' % self.ids['carl'], {'role': 'viewer'}),
                                   ('POST', '/worker-credentials', {'label': 'x'}), ('GET', '/audit', None),
                                   ('GET', '/setup', None), ('POST', '/archive', {}), ('GET', '/agents', None)):
            with self.subTest(path=path):
                self.assertEqual(403, self.request(method, '/v1/projects/%s%s' % (self.project, path), body,
                                                   token=self.tokens['cora']).status)

    def test_an_owner_gives_and_takes_the_role_and_only_a_superuser_makes_an_owner(self):
        self.assertEqual(200, self.member('carl', 'coordinator').status)
        self.assertEqual(200, self.member('carl', 'contributor').status)
        self.assertEqual(403, self.member('carl', 'owner').status)                  # as before
        self.assertEqual(403, self.member('carl', 'coordinator', token=self.tokens['cora']).status)   # a coordinator names nobody
        self.assertEqual(422, self.member('carl', 'conductor').status)
        members = {item['user_id']: item['role'] for item in
                   self.request('GET', '/v1/projects/%s/members' % self.project, token=self.tokens['olive']).data['items']}
        self.assertEqual(members[self.ids['cora']], 'coordinator')


class GrantTests(Grants):

    def test_the_test_that_matters(self):
        """A granted agent approves another party's contribution in the project it was granted; is refused its own
        party's whatever the setting; is refused where it has no grant; and again the moment the grant is removed."""
        task, contribution = self.deliver(self.tokens['carl'])
        # No grant yet: an agent cannot approve, as today.
        refused = self.approve(self.agent, task, contribution)
        self.assertEqual(403, refused.status, refused.data)
        self.assertEqual(self.state_of(task), 'awaiting-review')
        granted = self.grant()
        self.assertEqual(200, granted.status, granted.data)
        self.assertEqual(granted.data['coordinator']['granted_by'], self.ids['olive'])
        self.assertIs(granted.data['coordinator']['effective'], True)
        approved = self.approve(self.agent, task, contribution)
        self.assertEqual(201, approved.status, approved.data)
        self.assertEqual(self.state_of(task), 'approved')
        # Its own party's work, with the installation's setting OFF and with it ON.
        for setting in (False, True):
            self.service.approval_by_another_party = setting
            for label, token in (('the agent itself', self.agent), ('its owner', self.tokens['cora']),
                                 ('another agent of its account', self.agent_with_id('cora', 'Egret-%s' % setting)[0])):
                with self.subTest(setting=setting, delivered_by=label):
                    own, mine = self.deliver(token)
                    answer = self.approve(self.agent, own, mine)
                    self.assertEqual(403, answer.status, answer.data)
                    self.assertEqual(answer.data['error']['message'], OWN_PARTY_AGENT)
                    self.assertEqual(self.state_of(own), 'awaiting-review')
        self.service.approval_by_another_party = False
        # Removed: refused again at the very next request.
        other, second = self.deliver(self.tokens['carl'])
        self.assertEqual(200, self.grant(method='DELETE').status)
        self.assertEqual(403, self.approve(self.agent, other, second).status)
        self.assertEqual(self.state_of(other), 'awaiting-review')
        self.assertEqual(200, self.grant().status)
        self.assertEqual(201, self.approve(self.agent, other, second).status)

    def test_a_project_where_it_has_no_grant(self):
        """Beta: the agent works there and its account is a coordinator there, but nobody granted it. And Gamma, where it is nobody."""
        beta = self.create_project(self.admin, 'Beta')
        gamma = self.create_project(self.admin, 'Gamma')
        for name, role in (('cora', 'coordinator'), ('carl', 'contributor')):
            self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s' % (beta, self.ids[name]), {'role': role},
                                               token=self.admin).status)
        both, both_id = self.agent_with_id('cora', 'Stork', projects=[self.project, beta])
        self.assertEqual(200, self.grant(both_id).status)                         # in Alpha only
        first, self.project = self.project, beta
        try:
            task, contribution = self.deliver(self.tokens['carl'])
            self.assertEqual(403, self.approve(both, task, contribution).status)
            self.assertEqual(self.state_of(task), 'awaiting-review')
        finally:
            self.project = first
        task, contribution = self.deliver(self.tokens['carl'])
        self.assertEqual(201, self.approve(both, task, contribution).status)      # where it was granted
        answer = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (gamma, task),
                              {'operation': 'approve', 'contribution': contribution, 'summary': 'x'}, token=both)
        self.assertEqual(404, answer.status, answer.data)

    def test_what_ends_the_grant_at_the_next_request(self):
        self.assertEqual(200, self.grant().status)
        ways = (('the account is made a contributor', lambda: self.member('cora', 'contributor'), lambda: self.member('cora', 'coordinator')),
                ('the agent is taken out of the project',
                 lambda: self.request('DELETE', '/v1/projects/%s/agents/%s' % (self.project, self.agent_id), token=self.tokens['olive']),
                 None))
        for label, end, restore in ways:
            with self.subTest(ended_by=label):
                task, contribution = self.deliver(self.tokens['carl'])
                self.assertEqual(201, self.approve(self.agent, task, contribution).status)
                task, contribution = self.deliver(self.tokens['carl'])
                self.assertIn(end().status, (200, 204))
                self.assertIn(self.approve(self.agent, task, contribution).status, (403, 404))
                self.assertEqual(self.state_of(task), 'awaiting-review')
                if restore:
                    self.assertEqual(200, restore().status)
                    self.assertEqual(201, self.approve(self.agent, task, contribution).status)
        # Taken out of the project, its grant went with it: added back by its own account, it does not approve.
        self.assertEqual(200, self.request('PATCH', '/v1/agents/%s' % self.agent_id, {'projects': [self.project]},
                                           token=self.tokens['cora']).status)
        self.assertIsNone(self.listed()[self.agent_id]['coordinator'])
        task, contribution = self.deliver(self.tokens['carl'])
        self.assertEqual(403, self.approve(self.agent, task, contribution).status)

    def test_who_may_grant_and_what_may_be_granted(self):
        for label, token, status in (('a coordinator', self.tokens['cora'], 403), ('a contributor', self.tokens['carl'], 403),
                                     ('the agent itself', self.agent, 403)):
            with self.subTest(by=label):
                self.assertEqual(status, self.grant(token=token).status)
        self.assertIsNone(self.listed()[self.agent_id]['coordinator'])
        self.assertEqual(200, self.grant(token=self.admin).status)                # a superuser
        self.assertEqual(self.listed()[self.agent_id]['coordinator']['granted_by'], self.admin_user['id']
                         if isinstance(self.admin_user, dict) else self.listed()[self.agent_id]['coordinator']['granted_by'])
        # An agent whose account is only a contributor: the grant would have no effect and is not written.
        worker, worker_id = self.agent_with_id('carl', 'Wren')
        refused = self.grant(worker_id)
        self.assertEqual(409, refused.status, refused.data)
        self.assertIn('is not a coordinator or an owner of this project', refused.data['error']['message'])
        self.assertIsNone(self.listed()[worker_id]['coordinator'])
        # An agent that is not in this project, an id that is none, and a body with a field: nothing is written.
        beta = self.create_project(self.admin, 'Beta')
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s' % (beta, self.ids['cora']), {'role': 'owner'},
                                           token=self.admin).status)
        elsewhere, elsewhere_id = self.agent_with_id('cora', 'Crane', projects=[beta])
        self.assertEqual(404, self.grant(elsewhere_id).status)
        self.assertEqual(404, self.grant('agent_0000000000000000').status)
        self.assertEqual(422, self.request('PUT', '/v1/projects/%s/agents/%s/coordinator' % (self.project, worker_id),
                                           {'scopes': ['reviews']}, token=self.tokens['olive']).status)
        # Removing a grant that is not there says so; giving one twice changes nothing.
        self.assertEqual(404, self.grant(worker_id, method='DELETE').status)
        again = self.grant()
        self.assertEqual((again.status, again.data['coordinator']['granted_by']), (200, self.listed()[self.agent_id]['coordinator']['granted_by']))

    def test_an_owners_own_agent_is_granted_by_that_owner(self):
        """The agent's own account may grant only where that account is an owner of the project."""
        owned, owned_id = self.agent_with_id('olive', 'Swift')
        self.assertEqual(200, self.grant(owned_id).status)
        task, contribution = self.deliver(self.tokens['carl'])
        self.assertEqual(201, self.approve(owned, task, contribution).status)

    def test_the_grant_gives_approval_and_nothing_else_of_what_a_credential_is_denied(self):
        self.assertEqual(200, self.grant().status)
        for method, path, body in (('PUT', '/members/%s' % self.ids['carl'], {'role': 'viewer'}),
                                   ('POST', '/worker-credentials', {'label': 'x'}), ('GET', '/audit', None), ('POST', '/archive', {}),
                                   ('PUT', '/agents/%s/coordinator' % self.agent_id, {})):
            with self.subTest(path=path):
                self.assertEqual(403, self.request(method, '/v1/projects/%s%s' % (self.project, path), body, token=self.agent).status)
        self.assertEqual(403, self.request('POST', '/v1/projects', {'name': 'Mine'}, token=self.agent).status)
        # A worker credential is never an agent: it cannot be named in a grant and does not approve.
        worker, credential_id = self.worker_of('olive', 'lane-a')
        self.assertEqual(404, self.grant(credential_id).status)
        task, contribution = self.deliver(self.tokens['carl'])
        self.assertEqual(403, self.approve(worker, task, contribution).status)
        # And a state document that carries something that is not a grant grants nothing.
        with self.service.store.lock:
            self.service.state['coordinator_grants'][self.project][self.agent_id] = True
        self.assertEqual(403, self.approve(self.agent, task, contribution).status)

    def test_work_under_a_worker_credential_its_account_issued_is_its_own_partys(self):
        """Judged in full for an agent, with the setting off: the hole of slice 1a is not open to a granted agent."""
        self.assertEqual(200, self.member('cora', 'owner', token=self.admin).status)     # only an owner issues a worker credential
        self.assertEqual(200, self.grant().status)
        worker, _ = self.worker_of('cora', 'lane-c')
        task, contribution = self.deliver(worker)
        answer = self.approve(self.agent, task, contribution)
        self.assertEqual((answer.status, answer.data['error']['message']), (403, OWN_PARTY_AGENT))
        self.assertIs(self.service.approval_by_another_party, False)
        # The person, with the setting off, still may: that is slice 1a's setting, and it is off.
        self.assertEqual(201, self.approve(self.tokens['cora'], task, contribution).status)

    def test_the_audit_names_who_gave_and_took_the_grant(self):
        self.assertEqual(200, self.grant().status)
        self.assertEqual(200, self.grant(method='DELETE').status)
        events = self.request('GET', '/v1/projects/%s/audit' % self.project, token=self.tokens['olive']).data['items']
        seen = [(event['action'], event['outcome'], event['user_id'], event['reason']) for event in events
                if event['action'].startswith('agents.coordinator.')]
        self.assertEqual(seen, [('agents.coordinator.grant', 'committed', self.ids['olive'], 'agent %s' % self.agent_id),
                                ('agents.coordinator.revoke', 'committed', self.ids['olive'], 'agent %s' % self.agent_id)])

    def test_what_a_granted_agent_is_told_to_do(self):
        task, contribution = self.deliver(self.tokens['carl'])
        own, mine = self.deliver(self.tokens['cora'])

        def actions():
            return {item['task']: item for item in self.request('GET', '/v1/agents/me/next', token=self.agent).data['next_actions']
                    if item.get('kind') in ('to-review', 'review-recommended')}
        before = actions()
        self.assertIn(task, before)
        self.assertNotIn('coordinator grant', before[task]['reason'])
        self.assertEqual(200, self.grant().status)
        after = actions()
        self.assertEqual(after[task]['kind'], 'to-review')
        self.assertIn('You hold the coordinator grant in this project: review it, then approve it or request changes.', after[task]['reason'])
        self.assertNotIn(own, after)                                              # never its own party's


class NothingGrantedTests(Grants):
    """An installation that grants nothing: every answer is today's."""

    def test_no_grant_no_change(self):
        self.assertNotIn('coordinator_grants', self.service.state)
        task, contribution = self.deliver(self.tokens['carl'])
        for label, token in (('an agent of a coordinator', self.agent), ('an agent of an owner', self.agent_with_id('olive', 'Swift')[0]),
                             ('an agent of the superuser', self.request('POST', '/v1/agents', {
                                 'name': 'Root', 'working_directory': '/home/x/root', 'projects': [self.project]},
                                 token=self.admin).data['credential']['secret'])):
            with self.subTest(agent=label):
                answer = self.approve(token, task, contribution)
                self.assertEqual(403, answer.status, answer.data)
                self.assertEqual(answer.data['error']['message'], 'Credential scope does not permit this operation')
        self.assertEqual(201, self.approve(self.tokens['olive'], task, contribution).status)
        self.assertNotIn('coordinator_grants', self.service.state)
        self.assertIsNone(self.listed()[self.agent_id]['coordinator'])

    def test_the_capability_function_adds_nothing_without_a_grant(self):
        state = self.service.state
        credential = next(c for c in state['credentials'].values() if c.get('agent_id') == self.agent_id)
        issuer = state['users'][self.ids['cora']]
        caps = http_authority.credential_capabilities(state, credential, issuer, self.project)
        self.assertEqual(caps, {'read', 'tasks.write', 'checkpoints.write', 'reviews.write', 'feedback.write'})
        self.assertEqual(200, self.grant().status)
        self.assertEqual(http_authority.credential_capabilities(state, credential, issuer, self.project) - caps,
                         {'reviews.approve', 'coordinate'})
        self.assertEqual(http_authority.credential_capabilities(state, credential, dict(issuer, superuser=True), self.project) - caps,
                         {'reviews.approve', 'coordinate'})


if __name__ == '__main__':
    unittest.main()
