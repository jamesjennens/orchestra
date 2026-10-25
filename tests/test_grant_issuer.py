"""A coordinator or release grant counts only while its ISSUER may still give it (kittrial-5bb.410).

The authority is rooted in a person and checked at use: ``granted_by`` must be an enabled
account that is a superuser or an owner of the project, read live on every decision. Each
case below is separate and goes through the route, the pure functions, or both.
"""
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_authority
import test_coordinator_grant as grants


class IssuerCase(grants.Grants):
    """olive and oscar own Alpha; cora coordinates it and has agent Heron; carl contributes."""

    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.grant().status)          # olive gives Heron the coordinator grant
        self.board = '/v1/projects/%s/lanes' % self.project

    def credential(self, agent_id=None):
        return next(c for c in self.service.state['credentials'].values()
                    if c.get('agent_id') == (agent_id or self.agent_id))

    def approved(self):
        task, contribution = self.deliver(self.tokens['carl'])
        return self.approve(self.agent, task, contribution).status

    def view(self):
        return self.listed(self.tokens['oscar'])[self.agent_id]['coordinator']

    def remove_member(self, name):
        return self.request('DELETE', '/v1/projects/%s/members/%s' % (self.project, self.ids[name]),
                            token=self.admin)

    def requirement_editor(self):
        return http_authority.requirement_editor(
            self.service.state, self.project,
            http_authority.authority_request(self.principal(), self.project, http_authority.CAP_COORDINATE))

    def principal(self):
        class Credential:
            via, user_id, session_hash = 'credential', self.ids['cora'], None
            credential_id = self.credential()['id']
        return Credential

    def demote_olive(self, role='coordinator'):
        # Only a superuser changes an owner's role; oscar still owns the project.
        self.assertEqual(200, self.member('olive', role, token=self.admin).status)


class CoordinatorGrantIssuerTests(IssuerCase):

    def test_the_grant_works_while_its_issuer_owns_the_project(self):
        self.assertEqual(201, self.approved())
        self.assertIs(True, self.view()['effective'])

    def test_a_demoted_issuer_ends_the_grant_at_the_next_request(self):
        self.demote_olive()
        self.assertIn(self.approved(), (401, 403))
        self.assertEqual((False, 'issuer-role'), (self.view()['effective'], self.view()['no_effect_because']))

    def test_a_removed_issuer_ends_the_grant(self):
        self.assertEqual(200, self.remove_member('olive').status)
        self.assertIn(self.approved(), (401, 403))

    def test_a_disabled_issuer_ends_the_grant(self):
        self.assertEqual(200, self.request('POST', '/v1/accounts/%s/disable' % self.ids['olive'], {},
                                           token=self.admin).status)
        self.assertIn(self.approved(), (401, 403))

    def test_a_re_promoted_issuer_restores_the_same_record(self):
        before = dict(self.service.state['coordinator_grants'][self.project][self.agent_id])
        self.demote_olive()
        self.assertIn(self.approved(), (401, 403))
        self.assertEqual(200, self.member('olive', 'owner', token=self.admin).status)
        self.assertEqual(201, self.approved())
        self.assertEqual(before, self.service.state['coordinator_grants'][self.project][self.agent_id])

    def superuser_outside(self):
        """A superuser who is no member of Alpha (the bootstrap admin owns what it creates)."""
        self.ids['sue'] = self.create_account(self.admin, 'sue', 'sue-password-1')
        self.service.state['users'][self.ids['sue']]['superuser'] = True
        self.tokens['sue'] = self.login('sue', 'sue-password-1')[0]
        self.assertNotIn(self.ids['sue'], self.service.state['memberships'][self.project])

    def test_a_grant_given_by_a_superuser_needs_no_membership(self):
        self.superuser_outside()
        second, second_id = self.agent_with_id('cora', 'Egret')
        self.assertEqual(200, self.grant(second_id, token=self.tokens['sue']).status)
        task, contribution = self.deliver(self.tokens['carl'])
        self.assertEqual(201, self.approve(second, task, contribution).status)

    def test_a_superuser_issuer_who_loses_the_flag_ends_it(self):
        self.superuser_outside()
        second, second_id = self.agent_with_id('cora', 'Egret')
        self.assertEqual(200, self.grant(second_id, token=self.tokens['sue']).status)
        self.service.state['users'][self.ids['sue']]['superuser'] = False
        task, contribution = self.deliver(self.tokens['carl'])
        self.assertIn(self.approve(second, task, contribution).status, (401, 403))

    def test_a_record_with_no_readable_issuer_is_no_grant(self):
        for broken in ('no-such-user', None, 7, ''):
            with self.subTest(granted_by=broken):
                self.service.state['coordinator_grants'][self.project][self.agent_id]['granted_by'] = broken
                self.assertIn(self.approved(), (401, 403))

    def test_the_pure_function_adds_nothing_for_a_demoted_issuer(self):
        state = self.service.state
        issuer = state['users'][self.ids['cora']]
        caps = http_authority.credential_capabilities(state, self.credential(), issuer, self.project)
        self.assertIn('coordinate', caps)
        self.demote_olive()
        caps = http_authority.credential_capabilities(state, self.credential(), issuer, self.project)
        self.assertNotIn('coordinate', caps)
        self.assertNotIn('reviews.approve', caps)

    def test_a_current_owner_gives_a_dormant_grant_afresh(self):
        self.demote_olive()
        self.assertEqual(200, self.grant(token=self.tokens['oscar']).status)
        self.assertEqual(self.ids['oscar'], self.view()['granted_by'])
        self.assertEqual(201, self.approved())

    def test_the_issuer_rule_reaches_the_lanes_read_and_the_coordinator_list(self):
        self.assertEqual(['Heron'], [a['name'] for a in self.request(
            'GET', '/v1/projects/%s/coordination' % self.project, token=self.tokens['carl']).data['agents']])
        self.assertEqual('all', self.request('GET', self.board, token=self.agent).data['scope'])
        self.demote_olive()
        self.assertEqual([], self.request('GET', '/v1/projects/%s/coordination' % self.project,
                                          token=self.tokens['carl']).data['agents'])
        self.assertEqual('own', self.request('GET', self.board, token=self.agent).data['scope'])

    def test_a_demoted_issuer_ends_the_requirement_editor_too(self):
        self.assertEqual('coordinator', self.requirement_editor()['role'])
        self.demote_olive()
        with self.assertRaises(http_authority.AuthorityDenied) as raised:
            self.requirement_editor()
        self.assertEqual(403, raised.exception.status)


class ReleaseGrantIssuerTests(IssuerCase):

    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.member('cora', 'owner', token=self.admin).status)
        self.assertEqual(200, self.request(
            'PUT', '/v1/projects/%s/agents/%s/release' % (self.project, self.agent_id), {},
            token=self.tokens['olive']).status)

    def release_caps(self):
        return http_authority.credential_capabilities(
            self.service.state, self.credential(), self.service.state['users'][self.ids['cora']], self.project)

    def test_the_release_grant_works_while_its_issuer_owns_the_project(self):
        self.assertIn('release', self.release_caps())

    def test_a_demoted_issuer_ends_the_release_grant_only(self):
        self.demote_olive()
        caps = self.release_caps()
        self.assertNotIn('release', caps)
        self.assertNotIn('coordinate', caps)
        view = self.listed(self.tokens['oscar'])[self.agent_id]['release']
        self.assertEqual((False, 'issuer-role'), (view['effective'], view['no_effect_because']))

    def test_a_re_promoted_issuer_restores_the_release_grant(self):
        self.demote_olive()
        self.assertEqual(200, self.member('olive', 'owner', token=self.admin).status)
        self.assertIn('release', self.release_caps())

    def test_a_current_owner_gives_a_dormant_release_grant_afresh(self):
        self.demote_olive()
        self.assertEqual(200, self.request(
            'PUT', '/v1/projects/%s/agents/%s/release' % (self.project, self.agent_id), {},
            token=self.tokens['oscar']).status)
        self.assertEqual(self.ids['oscar'], self.listed(self.tokens['oscar'])[self.agent_id]['release']['granted_by'])
        self.assertIn('release', self.release_caps())

    def test_a_record_with_no_readable_issuer_is_no_release_grant(self):
        self.service.state['release_grants'][self.project][self.agent_id]['granted_by'] = 'no-such-user'
        self.assertNotIn('release', self.release_caps())


class DormantGrantListTests(IssuerCase):
    """The read-only pre-deploy check: grants on record that no longer have an issuer."""

    def listed_dormant(self):
        import contextlib
        import io
        import json
        import http_service
        with self.service.store.lock:
            self.service.store.save()
        before = Path(self.service.store.path).read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, http_service.main(['--state', str(self.service.store.path), '--list-dormant-grants']))
        self.assertEqual(before, Path(self.service.store.path).read_bytes())
        return [(i['kind'], i['agent'], i['granted_by'], i['why']) for i in json.loads(out.getvalue())['items']]

    def test_it_lists_exactly_the_grants_whose_issuer_no_longer_qualifies_and_writes_nothing(self):
        self.assertEqual([], self.listed_dormant())
        self.assertEqual(200, self.member('olive', 'coordinator', token=self.admin).status)
        self.assertEqual([('coordinator', self.agent_id, self.ids['olive'], 'issuer-not-owner')], self.listed_dormant())
        self.service.state['coordinator_grants'][self.project][self.agent_id]['granted_by'] = 'gone'
        self.assertEqual([('coordinator', self.agent_id, 'gone', 'issuer-missing')], self.listed_dormant())
        self.service.state['coordinator_grants'][self.project][self.agent_id]['granted_by'] = self.ids['oscar']
        self.assertEqual([], self.listed_dormant())
        self.assertEqual(200, self.request('POST', '/v1/accounts/%s/disable' % self.ids['oscar'], {},
                                           token=self.admin).status)
        self.assertEqual([('coordinator', self.agent_id, self.ids['oscar'], 'issuer-disabled')], self.listed_dormant())


class SuperuserAgentRuleTests(IssuerCase):
    """kittrial-5bb.410 item 4, documented not widened: the two rules differ on purpose.

    ``decide()`` reads a superuser as an owner of every project, so a superuser's agent with
    a grant reads every lane in a project the superuser is not a member of. The requirement
    editor needs the membership (kittrial-5bb.318 item 3) and refuses the same agent, as it
    refuses the superuser's own session. Both answers are pinned here.
    """

    def test_a_superuser_agent_reads_all_lanes_yet_is_no_requirement_editor(self):
        self.service.state['users'][self.ids['cora']]['superuser'] = True
        self.assertEqual(200, self.member('cora', 'contributor', token=self.admin).status)
        # cora is now a contributor member who is also a superuser; the grant sits in the record.
        self.assertEqual('all', self.request('GET', self.board, token=self.agent).data['scope'])
        with self.assertRaises(http_authority.AuthorityDenied) as raised:
            self.requirement_editor()
        self.assertEqual(403, raised.exception.status)


if __name__ == '__main__':
    unittest.main()
