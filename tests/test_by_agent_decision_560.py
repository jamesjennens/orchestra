"""kittrial-5bb.560 part A: the by-agent approval decision as pure functions over the service state.

``http_authority.by_agent_approval`` and the functions moved there from ``http_auth.Service``
(``agent_approval_mode``, ``agent_grant_effective``, ``other_agent_of_same_person``,
``worker_credential_of_same_person``) and ``independent_of`` (the service's
``_independent_all(names, [target], project, True)``). A hand-built state, no web service, no
tracker: one named test per refusal kind and per basis. ``NOW`` is a fixed number handed in
as the expiry clock; nothing here reads the real clock.
"""
import copy
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import http_authority as ha

NOW = 1_000_000.0
P = 'p1'
ROUTE_NAMES = ['heron', 'cora']          # the route judges [actor, account]


def base_state():
    """cora is a coordinator; her agent heron holds the coordinator grant olly (an owner) gave;
    egret is cora's second agent; kite is bob's agent. cora issued worker credentials ``builder``
    and ``shared``; dave once held ``shared`` too."""
    users = {name: {'id': name, 'display_name': name.title()} for name in ('olly', 'cora', 'bob', 'dave')}
    return {
        'users': users,
        'projects': {P: {'id': P}},
        'memberships': {P: {'olly': 'owner', 'cora': 'coordinator', 'bob': 'contributor', 'dave': 'contributor'}},
        'agents': {
            'heron': {'id': 'heron', 'owner': 'cora', 'enabled': True, 'projects': [P]},
            'egret': {'id': 'egret', 'owner': 'cora', 'enabled': True, 'projects': [P]},
            'kite': {'id': 'kite', 'owner': 'bob', 'enabled': True, 'projects': [P]},
        },
        'coordinator_grants': {P: {'heron': {'granted_by': 'olly', 'granted_at': 'then'}}},
        'credentials': {
            'c_heron': {'id': 'c_heron', 'user_id': 'cora', 'agent_id': 'heron', 'project_id': None,
                        'scopes': ['reviews'], 'actor': 'heron', 'expires_at': NOW + 3600},
            'w_builder': {'id': 'w_builder', 'user_id': 'cora', 'agent_id': None, 'project_id': P,
                          'scopes': ['tasks'], 'actor': 'builder', 'expires_at': NOW + 3600},
            'w_shared_cora': {'id': 'w_shared_cora', 'user_id': 'cora', 'agent_id': None, 'project_id': P,
                              'scopes': ['tasks'], 'actor': 'shared', 'expires_at': NOW + 3600},
            'w_shared_dave': {'id': 'w_shared_dave', 'user_id': 'dave', 'agent_id': None, 'project_id': P,
                              'scopes': ['tasks'], 'actor': 'shared', 'expires_at': NOW - 3600, 'revoked': True},
        },
    }


def decide(state, author, assignee='kite', names=ROUTE_NAMES, approver='heron'):
    return ha.by_agent_approval(state, P, approver, author, assignee, NOW, names=names)


class Refusals(unittest.TestCase):
    """One test per refusal kind."""

    def test_strict_under_by_person(self):
        state = base_state()
        state['agent_approval'] = {P: {'mode': 'by-person', 'set_by': 'olly', 'set_at': 'then'}}
        self.assertEqual(ha.ByAgentDecision(False, None, 'strict', None, None, False), decide(state, 'egret'))

    def test_strict_under_a_damaged_setting(self):
        for damaged in ('junk', {'mode': 'sometimes'}, None):
            state = base_state()
            state['agent_approval'] = {P: damaged}
            self.assertEqual('strict', decide(state, 'egret').refusal, damaged)

    def test_no_grant_without_the_coordinator_grant(self):
        state = base_state()
        del state['coordinator_grants'][P]['heron']
        self.assertEqual(ha.ByAgentDecision(False, None, 'no-grant', None, None, False), decide(state, 'egret'))

    def test_no_grant_when_the_credential_expired_at_now(self):
        state = base_state()
        state['credentials']['c_heron']['expires_at'] = NOW
        self.assertEqual('no-grant', decide(state, 'egret').refusal)

    def test_no_grant_for_an_approver_that_is_no_agent(self):
        self.assertEqual('no-grant', decide(base_state(), 'egret', approver='cora').refusal)
        self.assertEqual('no-grant', decide(base_state(), 'egret', approver=None).refusal)

    def test_same_agent_author(self):
        self.assertEqual(ha.ByAgentDecision(False, None, 'same-agent', 'heron', 'author', False),
                         decide(base_state(), 'heron'))

    def test_same_agent_sub_worker_author(self):
        self.assertEqual(ha.ByAgentDecision(False, None, 'same-agent', 'heron/w1', 'author', True),
                         decide(base_state(), 'heron/w1'))

    def test_same_agent_assignee(self):
        self.assertEqual(ha.ByAgentDecision(False, None, 'same-agent', 'heron', 'assignee', False),
                         decide(base_state(), 'kite', assignee='heron'))

    def test_held_assignee(self):
        self.assertEqual(ha.ByAgentDecision(False, None, 'held-assignee', 'cora', 'assignee', False),
                         decide(base_state(), 'egret', assignee='cora'))

    def test_shared_name(self):
        self.assertEqual(ha.ByAgentDecision(False, None, 'shared-name', 'shared', 'author', False),
                         decide(base_state(), 'shared'))

    def test_account_id(self):
        self.assertEqual(ha.ByAgentDecision(False, None, 'account-id', 'cora', 'author', False),
                         decide(base_state(), 'cora'))

    def test_own_party(self):
        # The fallback: of the approver's names' party, of one account, and none of the above.
        self.assertEqual(ha.ByAgentDecision(False, None, 'own-party', 'bob', 'author', False),
                         decide(base_state(), 'bob', names=['heron', 'cora', 'bob']))

    def test_author_is_judged_before_the_assignee(self):
        self.assertEqual('account-id', decide(base_state(), 'cora', assignee='heron').refusal)

    def test_every_kind_is_listed(self):
        self.assertEqual(('strict', 'no-grant', 'same-agent', 'held-assignee', 'shared-name', 'account-id',
                          'own-party'), ha.BY_AGENT_REFUSALS)


class Bases(unittest.TestCase):
    """One test per basis."""

    def test_basis_agent(self):
        self.assertEqual(ha.ByAgentDecision(True, 'agent', None, None, None, False), decide(base_state(), 'egret'))

    def test_basis_worker_credential(self):
        self.assertEqual(ha.ByAgentDecision(True, 'worker-credential', None, None, None, False),
                         decide(base_state(), 'builder'))
        self.assertEqual('worker-credential', decide(base_state(), 'cora/label').basis)

    def test_basis_none_when_independent(self):
        self.assertEqual(ha.ByAgentDecision(True, None, None, None, None, False), decide(base_state(), 'kite'))

    def test_worker_credential_outranks_agent_either_way(self):
        self.assertEqual('worker-credential', decide(base_state(), 'egret', assignee='builder').basis)
        self.assertEqual('worker-credential', decide(base_state(), 'builder', assignee='egret').basis)

    def test_names_default_to_the_approver(self):
        self.assertTrue(ha.by_agent_approval(base_state(), P, 'heron', 'egret', 'kite', NOW).allowed)
        self.assertEqual('agent', ha.by_agent_approval(base_state(), P, 'heron', 'egret', 'kite', NOW).basis)

    def test_absent_setting_is_by_agent(self):
        state = base_state()
        state['agent_approval'] = {'other-project': {'mode': 'by-person'}}
        self.assertTrue(decide(state, 'egret').allowed)

    def test_the_state_is_not_changed(self):
        state = base_state()
        before = copy.deepcopy(state)
        for author in ('egret', 'builder', 'heron', 'shared', 'cora'):
            decide(state, author)
        self.assertEqual(before, state)


class Mode(unittest.TestCase):

    def test_absent_is_the_default(self):
        self.assertEqual({'mode': 'by-agent', 'set_by': None, 'set_at': None, 'set_by_display_name': None},
                         ha.agent_approval_mode(base_state(), P))

    def test_set_names_who_set_it(self):
        state = base_state()
        state['agent_approval'] = {P: {'mode': 'by-person', 'set_by': 'olly', 'set_at': 'then'}}
        self.assertEqual({'mode': 'by-person', 'set_by': 'olly', 'set_at': 'then', 'set_by_display_name': 'Olly'},
                         ha.agent_approval_mode(state, P))
        state['agent_approval'][P]['set_by'] = 'gone'
        self.assertEqual('gone', ha.agent_approval_mode(state, P)['set_by_display_name'])

    def test_damaged_reads_by_person(self):
        state = base_state()
        state['agent_approval'] = {P: {'mode': 'by-anyone'}}
        self.assertEqual({'mode': 'by-person', 'set_by': None, 'set_at': None, 'set_by_display_name': None,
                          'damaged': True}, ha.agent_approval_mode(state, P))


class Moved(unittest.TestCase):

    def test_other_agent_of_same_person(self):
        state = base_state()
        self.assertTrue(ha.other_agent_of_same_person(state, 'heron', 'egret'))
        self.assertTrue(ha.other_agent_of_same_person(state, 'heron', 'egret/sub'))
        for name in ('heron', 'HERON', 'heron/sub', 'kite', 'cora', 'builder', '', None):
            self.assertFalse(ha.other_agent_of_same_person(state, 'heron', name), name)

    def test_worker_credential_of_same_person(self):
        state = base_state()
        self.assertTrue(ha.worker_credential_of_same_person(state, 'heron', 'builder', P))
        self.assertTrue(ha.worker_credential_of_same_person(state, 'heron', 'cora/x', P))
        for name in ('cora', 'shared', 'heron', 'egret', 'bob/x', 'nobody', ''):
            self.assertFalse(ha.worker_credential_of_same_person(state, 'heron', name, P), name)
        self.assertFalse(ha.worker_credential_of_same_person(state, 'heron', 'builder', 'p2'))

    def test_agent_grant_effective(self):
        self.assertTrue(ha.agent_grant_effective(base_state(), 'heron', P, NOW))
        self.assertFalse(ha.agent_grant_effective(base_state(), 'egret', P, NOW))
        cases = {
            'revoked': lambda s: s['credentials']['c_heron'].update(revoked=True),
            'expired': lambda s: s['credentials']['c_heron'].update(expires_at=NOW - 1),
            'agent disabled': lambda s: s['agents']['heron'].update(enabled=False),
            'out of the project': lambda s: s['agents']['heron'].update(projects=[]),
            'owner disabled': lambda s: s['users']['cora'].update(disabled=True),
            'owner demoted': lambda s: s['memberships'][P].update(cora='contributor'),
            'issuer demoted': lambda s: s['memberships'][P].update(olly='contributor'),
        }
        for label, change in cases.items():
            state = base_state()
            change(state)
            self.assertFalse(ha.agent_grant_effective(state, 'heron', P, NOW), label)

    def test_independent_of(self):
        state = base_state()
        self.assertTrue(ha.independent_of(state, ROUTE_NAMES, 'kite', P))
        self.assertTrue(ha.independent_of(state, ROUTE_NAMES, 'stranger', P))
        self.assertTrue(ha.independent_of(state, ROUTE_NAMES, '', P))
        for target in ('egret', 'builder', 'cora', 'shared', 'heron/sub'):
            self.assertFalse(ha.independent_of(state, ROUTE_NAMES, target, P), target)
        self.assertTrue(ha.independent_of(state, [None, ''], 'cora', P))


if __name__ == '__main__':
    unittest.main()
