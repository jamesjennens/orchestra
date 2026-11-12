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



class Recorded(unittest.TestCase):
    """kittrial-5bb.560 part B: the decision for an approval already recorded under a name."""

    def test_the_agent_is_the_record_of_the_first_segment_judged_with_its_owner(self):
        self.assertEqual(ha.ByAgentDecision(True, 'agent', None, None, None, False),
                         ha.recorded_by_agent_approval(base_state(), P, 'heron', 'egret', 'egret', NOW))
        self.assertEqual('worker-credential',
                         ha.recorded_by_agent_approval(base_state(), P, 'heron', 'builder', None, NOW).basis)
        # Under a sub-label of its own the agent is the same agent, judged as the route judges it.
        self.assertTrue(ha.recorded_by_agent_approval(base_state(), P, 'heron/x', 'egret', None, NOW).allowed)

    def test_each_refusal_reaches_the_recorded_approval(self):
        for author, assignee, kind in (('heron', None, 'same-agent'), ('heron/w1', None, 'same-agent'),
                                       ('cora', None, 'account-id'), ('shared', None, 'shared-name'),
                                       ('egret', 'cora', 'held-assignee')):
            self.assertEqual(kind, ha.recorded_by_agent_approval(base_state(), P, 'heron', author, assignee,
                                                                 NOW).refusal, author)

    def test_a_person_or_a_name_of_no_agent_is_no_grant(self):
        for approver in ('cora', 'builder', '', None, 'nobody/x'):
            self.assertEqual('no-grant', ha.recorded_by_agent_approval(base_state(), P, approver, 'egret', None,
                                                                       NOW).refusal, approver)

    def test_without_a_clock_it_is_strict(self):
        for now in (None, 'later', True, [NOW]):
            self.assertEqual(ha.ByAgentDecision(False, None, 'strict', None, None, False),
                             ha.recorded_by_agent_approval(base_state(), P, 'heron', 'egret', None, now), now)

    def test_the_setting_and_the_grant_are_read_now(self):
        state = base_state()
        state['agent_approval'] = {P: {'mode': 'by-person', 'set_by': 'olly', 'set_at': 'then'}}
        self.assertEqual('strict', ha.recorded_by_agent_approval(state, P, 'heron', 'egret', None, NOW).refusal)
        self.assertEqual('no-grant', ha.recorded_by_agent_approval(base_state(), P, 'heron', 'egret', None,
                                                                   NOW + 3600).refusal)



RAW = 2_000_000.0


class GrantJudgedAsTheService(unittest.TestCase):
    """kittrial-5bb.560 r2 item m1: each credential counts toward the grant only as the service
    would accept it. One shape per test, each through ``agent_grant_effective`` and through the
    recorded decision integration asks."""

    def assert_no_grant(self, state, now=NOW, raw_now=RAW):
        self.assertFalse(ha.agent_grant_effective(state, 'heron', P, now, raw_now))
        self.assertEqual('no-grant', ha.recorded_by_agent_approval(state, P, 'heron', 'egret', None, now,
                                                                   raw_now).refusal)

    def test_the_control_is_allowed(self):
        self.assertTrue(ha.agent_grant_effective(base_state(), 'heron', P, NOW, RAW))
        self.assertTrue(ha.recorded_by_agent_approval(base_state(), P, 'heron', 'egret', None, NOW, RAW).allowed)

    def test_a_credential_past_the_366_day_cap_does_not_count(self):
        state = base_state()
        # Stored expiry still ahead, but the last legitimate write is more than 366 days back.
        state['credentials']['c_heron'].update(agent_written_raw=NOW - ha.CREDENTIAL_MAX_AGE_SECONDS,
                                               expires_at=NOW + 10 * 365 * 86400)
        self.assert_no_grant(state)
        state['credentials']['c_heron']['agent_written_raw'] = NOW - ha.CREDENTIAL_MAX_AGE_SECONDS + 1
        self.assertTrue(ha.agent_grant_effective(state, 'heron', P, NOW, RAW))

    def test_an_unreadable_expires_at_does_not_count_and_raises_nothing(self):
        for value in (None, 'tomorrow', True, float('nan'), [NOW + 3600]):
            state = base_state()
            state['credentials']['c_heron']['expires_at'] = value
            self.assert_no_grant(state)
        state = base_state()
        del state['credentials']['c_heron']['expires_at']
        self.assert_no_grant(state)

    def test_a_credential_issued_to_another_account_does_not_count(self):
        for user_id in ('bob', 'olly', None):
            state = base_state()
            state['credentials']['c_heron']['user_id'] = user_id
            self.assert_no_grant(state)

    def test_an_agent_key_past_its_raw_deadline_does_not_count(self):
        state = base_state()
        state['credentials']['c_heron']['agent_raw_expires_at'] = RAW + 60
        self.assertTrue(ha.agent_grant_effective(state, 'heron', P, NOW, RAW + 59))
        self.assert_no_grant(state, raw_now=RAW + 60)
        # No raw clock given, or one that cannot be read: the key does not count.
        for raw_now in (None, float('nan'), True, 'now'):
            self.assert_no_grant(state, raw_now=raw_now)
        for value in (None, float('inf'), float('nan'), 'later', False):
            state['credentials']['c_heron']['agent_raw_expires_at'] = value
            self.assert_no_grant(state)

    def test_a_key_without_a_raw_deadline_needs_no_raw_clock(self):
        self.assertTrue(ha.agent_grant_effective(base_state(), 'heron', P, NOW, None))

    def test_a_non_finite_now_fails_closed(self):
        state = base_state()
        state['credentials']['c_heron']['expires_at'] = NOW - 1        # expired at any real clock
        for now in (float('nan'), float('inf'), float('-inf')):
            self.assertFalse(ha.agent_grant_effective(base_state(), 'heron', P, now, RAW), now)
            self.assertFalse(ha.agent_grant_effective(state, 'heron', P, now, RAW), now)
            self.assertEqual('strict', ha.recorded_by_agent_approval(state, P, 'heron', 'egret', None, now,
                                                                     RAW).refusal, now)

    def test_damaged_agents_users_or_credentials_answer_false(self):
        for key in ('agents', 'users', 'credentials'):
            for damage in (None, [], 'x'):
                state = base_state()
                state[key] = damage
                self.assertFalse(ha.agent_grant_effective(state, 'heron', P, NOW, RAW), (key, damage))
                self.assertFalse(ha.recorded_by_agent_approval(state, P, 'heron', 'egret', None, NOW,
                                                               RAW).allowed, (key, damage))

    def test_a_damaged_users_map_does_not_break_the_setting_read(self):
        state = base_state()
        state['agent_approval'] = {P: {'mode': 'by-agent', 'set_by': 'olly', 'set_at': 'then'}}
        state['users'] = None
        self.assertEqual('olly', ha.agent_approval_mode(state, P)['set_by_display_name'])


if __name__ == '__main__':
    unittest.main()
