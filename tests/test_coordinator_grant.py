"""The coordinator role and the per-project grant that lets an agent approve (kittrial-5bb.209).

Slice 1b of docs/WEB_COORDINATOR_DESIGN.md. A project role ``coordinator`` between contributor
and owner; and a grant, kept with the project, by which an owner of the project (or a
superuser) lets one personal agent approve there. An agent that approves is judged by party
whatever the slice-1a setting says.

Through the real routes of the web service (the in-process backend); the endpoint backend
with a real bd is in tests/test_coordinator_grant_real.py.
"""
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_authority
import http_service
import test_http_proposals
import test_own_party

OWN_PARTY_AGENT = http_service.ApiHandler.OWN_PARTY_AGENT[0] + http_service.ApiHandler.OWN_PARTY_AGENT[3]


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
                ('the account leaves the project',
                 lambda: self.request('DELETE', '/v1/projects/%s/members/%s' % (self.project, self.ids['cora']), token=self.tokens['olive']),
                 lambda: self.member('cora', 'coordinator')),
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

    def test_a_disabled_agent_or_account_approves_nothing(self):
        for label, path in (('the agent', '/v1/agents/%s/disable' % self.agent_id), ('its account', '/v1/accounts/%s/disable' % self.ids['cora'])):
            with self.subTest(disabled=label):
                agent, agent_id = self.agent_with_id('cora', 'Heron ' + label[:3]) if label == 'its account' else (self.agent, self.agent_id)
                self.assertEqual(200, self.grant(agent_id).status)
                task, contribution = self.deliver(self.tokens['carl'])
                self.assertEqual(200, self.request('POST', path, {}, token=self.admin).status)
                self.assertIn(self.approve(agent, task, contribution).status, (401, 403))
                self.assertEqual(self.state_of(task), 'awaiting-review')
                self.assertFalse(self.listed()[agent_id]['coordinator']['effective'])

    def audited(self, project=None):
        with self.service.store.lock:
            return [(event['action'], event['outcome'], event['reason']) for event in self.service.state['audit']
                    if event['action'].startswith('agents.coordinator.') and event['project_id'] == (project or self.project)]

    def test_the_grant_ends_when_the_agent_leaves_the_project_by_any_route(self):
        """The review's blocking finding: PATCH /v1/agents/{id} took the agent out of the project and left the
        grant in the state, unseen by the owner; putting the project back made it effective again with no
        owner's decision. By the account and by a superuser."""
        beta = self.create_project(self.admin, 'Beta')
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s' % (beta, self.ids['cora']), {'role': 'owner'},
                                           token=self.admin).status)
        for label, token in (('its own account', self.tokens['cora']), ('a superuser', self.admin)):
            with self.subTest(taken_out_by=label):
                agent, agent_id = self.agent_with_id('cora', 'Heron ' + label[:5], projects=[self.project, beta])
                self.assertEqual(200, self.grant(agent_id).status)
                self.assertEqual(200, self.grant(agent_id, token=self.tokens['cora'], project=beta).status)
                task, contribution = self.deliver(self.tokens['carl'])
                # Out of Alpha, still in Beta: the grant in Alpha ends, the one in Beta stays.
                patched = self.request('PATCH', '/v1/agents/%s' % agent_id, {'projects': [beta]}, token=token)
                self.assertEqual(200, patched.status, patched.data)
                with self.service.store.lock:
                    grants = self.service.state.get('coordinator_grants') or {}
                    self.assertNotIn(agent_id, grants.get(self.project) or {})
                    self.assertIn(agent_id, grants[beta])
                self.assertIn(('agents.coordinator.revoke', 'committed',
                               "agent %s: the agent's projects were changed (PATCH /v1/agents)" % agent_id), self.audited())
                # Put back by its own account: no grant, and it approves nothing until an owner decides again.
                back = self.request('PATCH', '/v1/agents/%s' % agent_id, {'projects': [self.project, beta]}, token=self.tokens['cora'])
                self.assertEqual(200, back.status, back.data)
                self.assertIsNone(self.listed()[agent_id]['coordinator'])
                self.assertEqual(403, self.approve(agent, task, contribution).status)
                self.assertEqual(self.state_of(task), 'awaiting-review')
                self.assertEqual(200, self.grant(agent_id).status)
                self.assertEqual(201, self.approve(agent, task, contribution).status)
                # Every project at once, and a change that takes it out of none.
                self.assertEqual(200, self.request('PATCH', '/v1/agents/%s' % agent_id, {'notes': 'x'}, token=token).status)
                self.assertIsNotNone(self.listed()[agent_id]['coordinator'])
                self.assertEqual(200, self.request('PATCH', '/v1/agents/%s' % agent_id, {'projects': []}, token=token).status)
                with self.service.store.lock:
                    self.assertNotIn(agent_id, json.dumps(self.service.state.get('coordinator_grants') or {}))
        # The owner's own route says why in the audit too.
        self.assertEqual(200, self.grant().status)
        self.assertEqual(200, self.request('DELETE', '/v1/projects/%s/agents/%s' % (self.project, self.agent_id),
                                           token=self.tokens['olive']).status)
        self.assertIn(('agents.coordinator.revoke', 'committed',
                       'agent %s: a project owner took the agent out of the project' % self.agent_id), self.audited())

    def test_a_grant_ended_by_patch_is_audited_with_the_id_of_that_request(self):
        """kittrial-5bb.261: the revoke written when PATCH /v1/agents/{id} takes the agent out of the project
        carried `request_id: null`; it carries the id of the PATCH, as the owner's DELETE route does."""
        def events(action):
            with self.service.store.lock:
                return [event for event in self.service.state['audit'] if event['action'] == action]
        self.assertEqual(200, self.grant().status)
        self.assertEqual(200, self.request('PATCH', '/v1/agents/%s' % self.agent_id, {'projects': []},
                                           token=self.tokens['cora']).status)
        revoked, updated = events('agents.coordinator.revoke'), events('agents.update')
        self.assertEqual([self.project], [event['project_id'] for event in revoked])
        self.assertIsInstance(revoked[0]['request_id'], str)
        self.assertTrue(revoked[0]['request_id'])
        self.assertEqual(updated[-1]['request_id'], revoked[0]['request_id'])
        # The owner's route, for comparison: its revoke carries its request's id too.
        self.assertEqual(200, self.request('PATCH', '/v1/agents/%s' % self.agent_id, {'projects': [self.project]},
                                           token=self.tokens['cora']).status)
        self.assertEqual(200, self.grant().status)
        self.assertEqual(200, self.request('DELETE', '/v1/projects/%s/agents/%s' % (self.project, self.agent_id),
                                           token=self.tokens['olive']).status)
        self.assertTrue(all(isinstance(event['request_id'], str) and event['request_id']
                            for event in events('agents.coordinator.revoke')))
        self.assertEqual(2, len({event['request_id'] for event in events('agents.coordinator.revoke')}))

    def test_a_task_held_by_the_agents_own_account_is_its_own_partys(self):
        """The assignee half of the rule, for an agent: carl delivered, the task was then given to cora."""
        self.assertEqual(200, self.grant().status)
        task, contribution = self.deliver(self.tokens['carl'])
        self.backend._task(self.project, task)['assignee'] = self.ids['cora']
        refused = self.approve(self.agent, task, contribution)
        api = http_service.ApiHandler
        self.assertEqual((403, api.OWN_PARTY_AGENT[2] + api.OWN_PARTY_AGENT[3]),
                         (refused.status, refused.data['error']['message']), refused.data)
        self.assertEqual(self.state_of(task), 'awaiting-review')
        self.backend._task(self.project, task)['assignee'] = self.ids['carl']
        self.assertEqual(201, self.approve(self.agent, task, contribution).status)

    def test_a_disabled_agent_is_not_granted_and_a_damaged_record_is_said(self):
        self.assertEqual(200, self.request('POST', '/v1/agents/%s/disable' % self.agent_id, {}, token=self.tokens['cora']).status)
        refused = self.grant()
        self.assertEqual((409, 'A disabled agent cannot be granted'), (refused.status, refused.data['error']['message']))
        self.assertNotIn('coordinator_grants', self.service.state)
        self.assertEqual(200, self.request('POST', '/v1/agents/%s/enable' % self.agent_id, {}, token=self.tokens['cora']).status)
        # A record of grants that is not an object: a sentence and no write, where it was a 500.
        for damaged in ([], 'x', {self.project: []}, {self.project: 'x'}):
            with self.subTest(stored=damaged):
                with self.service.store.lock:
                    self.service.state['coordinator_grants'] = damaged
                refused = self.grant()
                self.assertEqual(503, refused.status, refused.data)
                self.assertIn('is damaged (it is not an object), so no grant was written', refused.data['error']['message'])
                with self.service.store.lock:
                    self.assertEqual(self.service.state['coordinator_grants'], damaged)
        with self.service.store.lock:
            del self.service.state['coordinator_grants']
        self.assertEqual(200, self.grant().status)

    def test_a_null_record_of_grants_is_damage_and_an_absent_one_is_not(self):
        """kittrial-5bb.261: `coordinator_grants: null` passed the damaged-record check and answered 500. It is
        answered like every other damaged shape; a state file with NO such key is every installation before
        its first grant and must keep working."""
        with self.service.store.lock:
            self.assertNotIn('coordinator_grants', self.service.state)
        self.assertEqual(200, self.grant().status)                      # absent: the first grant creates it
        self.assertEqual(200, self.grant(method='DELETE').status)
        for damaged in (None, 7, {self.project: None}, {self.project: 7}):
            with self.subTest(stored=damaged):
                with self.service.store.lock:
                    self.service.state['coordinator_grants'] = damaged
                before = self.audited()
                refused = self.grant()
                self.assertEqual(503, refused.status, refused.data)
                self.assertEqual('The record of coordinator grants in the service state is damaged (it is not an object), '
                                 'so no grant was written. An operator must repair the state file',
                                 refused.data['error']['message'])
                with self.service.store.lock:
                    self.assertIn('coordinator_grants', self.service.state)
                    self.assertEqual(self.service.state['coordinator_grants'], damaged)
                added = self.audited()[len(before):]
                self.assertEqual([('agents.coordinator.grant', 'rejected')], [event[:2] for event in added], added)
                self.assertIsNone(self.listed()[self.agent_id]['coordinator'])
        with self.service.store.lock:
            del self.service.state['coordinator_grants']
        self.assertEqual(200, self.grant().status)
        self.assertTrue(self.listed()[self.agent_id]['coordinator']['effective'])

    def test_why_a_grant_on_record_has_no_effect(self):
        self.assertEqual(200, self.grant().status)
        view = lambda: self.listed()[self.agent_id]['coordinator']
        self.assertEqual((view()['effective'], view()['no_effect_because']), (True, None))
        self.assertEqual(200, self.member('cora', 'contributor').status)
        self.assertEqual((view()['effective'], view()['no_effect_because']), (False, 'account-role'))
        self.assertEqual(200, self.member('cora', 'coordinator').status)
        self.assertEqual(200, self.request('POST', '/v1/agents/%s/disable' % self.agent_id, {}, token=self.tokens['cora']).status)
        self.assertEqual((view()['effective'], view()['no_effect_because']), (False, 'agent-disabled'))
        self.assertEqual(200, self.request('POST', '/v1/agents/%s/enable' % self.agent_id, {}, token=self.tokens['cora']).status)
        self.assertEqual(200, self.request('POST', '/v1/accounts/%s/disable' % self.ids['cora'], {}, token=self.admin).status)
        self.assertEqual((view()['effective'], view()['no_effect_because']), (False, 'account-disabled'))
        page = (KIT / 'web' / 'js' / 'views' / 'project.js').read_text(encoding='utf-8')
        for word in ('agent-disabled', 'account-disabled', 'account-role'):
            self.assertIn("'%s': " % word, page)
        self.assertIn("NO_EFFECT[grant.no_effect_because] || NO_EFFECT['account-role']", page)

    def test_the_endpoints_own_check_refuses_a_block_that_claims_the_approval(self):
        """The second authorization: what the endpoint is handed is read again from the live state. An agent
        WITHOUT the grant whose block claims `reviews.approve` is refused there; with the grant it passes."""
        principal = self.service.authenticate(self.agent)
        block = http_authority.authority_request(principal, self.project, 'reviews.approve')
        for claimed in ('reviews.approve', 'coordinate'):
            with self.subTest(capability=claimed), self.assertRaises(http_authority.AuthorityDenied) as denied:
                http_authority.decide(self.service.state, dict(block, capability=claimed))
            self.assertEqual(denied.exception.status, 403)
        self.assertEqual(200, self.grant().status)
        for claimed in ('reviews.approve', 'coordinate'):
            http_authority.decide(self.service.state, dict(block, capability=claimed))          # no refusal now
        with self.assertRaises(http_authority.AuthorityDenied):
            http_authority.decide(self.service.state, dict(block, capability='project.admin'))
        # In another project of the same agent the grant is nothing.
        beta = self.create_project(self.admin, 'Beta')
        with self.assertRaises(http_authority.AuthorityDenied):
            http_authority.decide(self.service.state, dict(block, project=beta))

    def test_who_may_grant_and_what_may_be_granted(self):
        for label, token, status in (('a coordinator', self.tokens['cora'], 403), ('a contributor', self.tokens['carl'], 403),
                                     ('the agent itself', self.agent, 403)):
            with self.subTest(by=label):
                self.assertEqual(status, self.grant(token=token).status)
        self.assertIsNone(self.listed()[self.agent_id]['coordinator'])
        self.assertEqual(200, self.grant(token=self.admin).status)                # a superuser
        first = self.listed()[self.agent_id]['coordinator']
        self.assertNotIn(first['granted_by'], self.ids.values())                  # the superuser, none of the members
        self.assertTrue(first['granted_at'])
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
        again = self.grant()                                                      # by olive this time
        self.assertEqual((again.status, again.data['coordinator']), (200, first))
        # Taking it away is the owner's too: not a coordinator's, a contributor's or the agent's own.
        for label, token in (('a coordinator', self.tokens['cora']), ('a contributor', self.tokens['carl']), ('the agent itself', self.agent)):
            with self.subTest(removed_by=label):
                self.assertEqual(403, self.grant(token=token, method='DELETE').status)
        self.assertEqual(self.listed()[self.agent_id]['coordinator'], first)

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

    def test_an_agent_is_traced_on_both_sides_whatever_the_setting(self):
        """With the setting off a name is its person and nothing more. For an agent that approves, both sides
        are traced in full: the work's (a name under a worker credential its account issued) and the
        approver's own (a label under the agent's name is the agent's account)."""
        self.assertFalse(self.service.approval_by_another_party)
        handler = http_service.ApiHandler.__new__(http_service.ApiHandler)
        handler.service = self.service
        cora, carl = self.ids['cora'], self.ids['carl']
        label = self.agent_id + '/night'
        # By person (a signed-in approver, setting off): the label is nobody the service knows.
        self.assertEqual(handler._independent([label], [cora], self.project), [label])
        # In full (an agent approves): the label is cora's, so it is not independent of cora's work.
        self.assertEqual(handler._independent([label], [cora], self.project, True), [])
        self.assertEqual(handler._independent([label], [carl], self.project, True), [label])
        # And the work's side: a label under ANOTHER agent of cora's is cora's too.
        other, other_id = self.agent_with_id('cora', 'Swift')
        self.assertEqual(handler._independent([self.agent_id], [other_id + '/x'], self.project, True), [])
        self.assertEqual(handler._independent([self.agent_id], [other_id + '/x'], self.project), [self.agent_id])

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
        # Work under a worker credential its account issued is nobody's by person (the setting is off), so
        # only the party rule keeps it from the agent that would be refused the approval.
        swift, swift_id = self.agent_with_id('olive', 'Swift')
        self.assertEqual(200, self.grant(swift_id).status)
        worker, _ = self.worker_of('olive', 'lane-a')
        under, _ = self.deliver(worker)
        shown = {item['task']: item for item in self.request('GET', '/v1/agents/me/next', token=swift).data['next_actions']
                 if item.get('kind') in ('to-review', 'review-recommended')}
        self.assertIn(task, shown)
        self.assertIn('You hold the coordinator grant', shown[task]['reason'])
        self.assertNotIn(under, shown)


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


class ProposalTests(test_http_proposals.ProposalHarness):
    """The grant is about contributions. Triage of requirement proposals stays what it was for a
    credential: refused, and the reads do not tell a granted agent otherwise."""

    def test_a_granted_agent_triages_no_proposal(self):
        made = self.request('POST', '/v1/agents', {'name': 'Heron', 'working_directory': '/home/x/heron',
                                                   'projects': [self.project]}, token=self.token('blair'))
        self.assertEqual(201, made.status, made.data)
        agent, agent_id = made.data['credential']['secret'], made.data['agent']['id']
        key = self.submit().data['key']

        def seen():
            listed = self.request('GET', self.base(), token=agent)
            one = self.request('GET', self.base() + '/' + key, token=agent)
            refused = self.request('POST', self.base() + '/' + key + '/dispositions', {'to_state': 'triaged'},
                                   token=agent, key='agent-triage-0001')
            return (listed.status, listed.data['can_triage'], one.status, one.data['can_triage'], refused.status,
                    (refused.data.get('error') or {}).get('message'))
        self.assertEqual(seen(), (200, False, 200, False, 403, 'Credential scope does not permit this operation'))
        granted = self.request('PUT', '/v1/projects/%s/agents/%s/coordinator' % (self.project, agent_id), {},
                               token=self.token('dana'))
        self.assertEqual((200, True), (granted.status, granted.data['coordinator']['effective']))
        # The grant lifts the scope refusal and meets the rule of the route: no credential triages.
        self.assertEqual(seen(), (200, False, 200, False, 403, 'A credential cannot record a proposal disposition'))
        # Its owner, signed in, triages as before.
        self.assertTrue(self.request('GET', self.base(), token=self.token('blair')).data['can_triage'])

    def test_a_coordinator_triages_and_the_decision_stays_an_owners(self):
        """`reviews.approve` alone carried both: a coordinator could record the owner's decision."""
        admin = self.admin
        cora = self.create_account(admin, 'cora', 'cora-password-1')
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, cora), {'role': 'coordinator'},
                                           token=admin).status)
        token = self.login('cora', 'cora-password-1')[0]
        key = self.submit().data['key']
        path = self.base() + '/' + key + '/dispositions'
        self.assertTrue(self.request('GET', self.base(), token=token).data['can_triage'])
        decided = self.request('POST', path, {'operation': 'decide', 'to_state': 'accepted'}, token=token, key='coord-decide-0001')
        self.assertEqual(403, decided.status, decided.data)
        self.assertEqual(decided.data['error']['message'],
                         'The decision on a proposal is an owner\'s. A coordinator triages a proposal and may escalate it to an '
                         'owner; it does not record the owner\'s decision')
        # Triage is not refused for want of authority (the body here is incomplete on purpose: 422, not 403).
        triaged = self.request('POST', path, {'operation': 'review', 'to_state': 'under-review'}, token=token, key='coord-review-0001')
        self.assertNotEqual(403, triaged.status, triaged.data)
        # An owner's decide is not refused by this rule (whatever the endpoint then says of the transition).
        owned = self.request('POST', path, {'operation': 'decide', 'to_state': 'accepted'}, token=self.token('blair'), key='owner-decide-0001')
        self.assertNotEqual(403, owned.status, owned.data)


class PageTests(Grants):
    """The settings page under Node, with a small DOM, against this real service."""

    def test_the_settings_page_gives_and_removes_the_grant(self):
        import json
        import shutil
        from test_http_web import run_node_module
        node = shutil.which('node')
        if not node:
            print('NOTE: PageTests.test_the_settings_page_gives_and_removes_the_grant was SKIPPED: node is not installed, '
                  'so the settings page of web/js/views/project.js was not run on this platform.', file=sys.stderr)
            self.skipTest('node is not installed; the coordinator panel is not run here')
        _, worker_id = self.agent_with_id('carl', 'Wren')
        web = KIT / 'web' / 'js'
        done = run_node_module(self, node, 'await import(process.argv[1])',
                               (KIT / 'tests' / 'web_coordinator_screen.mjs').as_uri(),
                               (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(), (web / 'api.js').as_uri(),
                               (web / 'views' / 'project.js').as_uri(), 'http://127.0.0.1:%d' % self.port, self.project,
                               'owner=' + self.tokens['olive'], 'coordinator=' + self.tokens['cora'])
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(seen['roles'], ['Viewer', 'Contributor', 'Coordinator', 'Owner'])
        self.assertIn('Agents in this project 2', seen['head'])
        self.assertEqual(sorted((row['coordinator'], row['buttons']) for row in seen['before']),
                         [('none', ['Make coordinator']), ('none', ['Make coordinator'])])
        self.assertEqual((seen['granted']['agent'], seen['granted']['coordinator'], seen['granted']['buttons']),
                         (self.agent_id, 'true', ['Remove grant']))
        self.assertIn('Coordinates', seen['granted']['text'])
        self.assertEqual((seen['refused']['agent'], seen['refused']['coordinator']), (worker_id, 'none'))
        self.assertEqual((seen['removed']['coordinator'], seen['removed']['buttons']), ('none', ['Make coordinator']))
        self.assertIs(seen['coordinatorSeesPanel'], False)
        self.assertEqual(seen['can'], {'approveOwner': True, 'approveCoordinator': True, 'approveContributor': False,
                                       'ownerCoordinator': False, 'writeCoordinator': True})
        # What the page did is what the server holds: given and removed, so none now.
        self.assertIsNone(self.listed()[self.agent_id]['coordinator'])


if __name__ == '__main__':
    unittest.main()
