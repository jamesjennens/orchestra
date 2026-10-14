"""The shared party ledger must preserve every account behind old work."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from http_authority import actor_parties, actor_person, namespace_holds


class SharedPartyTests(unittest.TestCase):
    def setUp(self):
        self.state = dict(users={'usr_a': {}, 'usr_b': {}},
                          agents={'agent_a': {'owner': 'usr_a', 'enabled': False}},
                          credentials={
                              'old': dict(user_id='usr_a', project_id='p', actor='Lane', revoked=True),
                              'new': dict(user_id='usr_b', project_id='p', actor='lane', expires=0),
                              'none': dict(user_id='usr_b', project_id='p', actor=None),
                              'other-project': dict(user_id='usr_b', project_id='q', actor='elsewhere')})

    def test_account_and_agent_namespaces_share_the_account(self):
        for actor in ('usr_a', 'usr_a/worker', 'agent_a', 'agent_a/worker'):
            with self.subTest(actor=actor):
                self.assertEqual(actor_parties(self.state, actor, 'p'), {'usr_a'})
        self.assertEqual(actor_person(self.state, 'agent_a'), 'usr_a')
        self.assertEqual(actor_person(self.state, 'agent_a/worker'), 'agent_a/worker')

    def test_all_historical_issuers_count_even_after_revoke_or_expiry(self):
        for actor in ('Lane', 'LANE/worker', 'lane/sub/worker'):
            with self.subTest(actor=actor):
                self.assertEqual(actor_parties(self.state, actor, 'p'), {'usr_a', 'usr_b'})
        self.assertEqual(actor_parties(self.state, 'lane/worker', 'q'), {'lane/worker'})
        self.assertEqual(actor_parties(self.state, 'elsewhere', 'p'), {'elsewhere'})

    def test_unknown_host_actor_stays_its_own_party_and_none_never_holds_it(self):
        self.assertEqual(actor_parties(self.state, 'host-coordinator', 'p'), {'host-coordinator'})
        self.assertFalse(namespace_holds(None, 'host-coordinator'))
        self.assertTrue(namespace_holds('Lane/', 'LANE/sub'))
        self.assertFalse(namespace_holds('Lane', 'LaneElse'))

    def test_agent_identity_wins_over_a_misleading_worker_namespace(self):
        self.state['credentials']['forged'] = dict(user_id='usr_b', project_id='p', actor='agent_a')
        self.assertEqual(actor_parties(self.state, 'agent_a/worker', 'p'), {'usr_a'})

    def test_read_does_not_rewrite_credentials_roles_or_ownership(self):
        before = copy.deepcopy(self.state)
        actor_parties(self.state, 'lane/worker', 'p')
        self.assertEqual(self.state, before)


if __name__ == '__main__':
    unittest.main()
