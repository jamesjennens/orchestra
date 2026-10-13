"""An agent with the coordinator grant, and a worker credential, against the web refusal.

Item 5 of the kittrial-5bb.10 review: `attest`, `hold` and `release-hold` are host-only over
the web route, and no web authority reaches them. The review did not run this; these tests
show it through the real in-process web routes, so a granted coordinator agent and a worker
credential are both refused and nothing is written.
"""
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import test_coordinator_grant


class HostOnlyWebTests(test_coordinator_grant.Grants):
    def reviews(self, token, task, operation, **extra):
        body = {'operation': operation, 'schema_version': 1, 'operation_id': 'op-' + operation,
                'contribution': '1', 'reason': 'because'}
        body.update(extra)
        return self.request('POST', self.base(task) + '/reviews', body, token=token)

    def test_a_granted_coordinator_agent_cannot_record_any_of_the_three(self):
        task, contribution = self.deliver(self.tokens['carl'])
        self.assertEqual(200, self.grant().status, 'the test needs the grant to be effective')
        for operation in ('attest', 'hold', 'release-hold'):
            with self.subTest(operation=operation):
                answer = self.reviews(self.agent, task, operation)
                # The route refuses the operation outright: the coordinator grant does not
                # reach it (the host-only sentence itself is asserted in
                # test_review_verification_holds.WebRouteHostOnlyTests over the bridge).
                self.assertEqual(422, answer.status, answer.data)
                self.assertEqual(self.state_of(task), 'awaiting-review')

    def test_a_worker_credential_cannot_reach_them_either(self):
        task, contribution = self.deliver(self.tokens['carl'])
        secret, _ = self.worker_of('olive')
        for operation in ('attest', 'hold', 'release-hold'):
            with self.subTest(operation=operation):
                answer = self.reviews(secret, task, operation)
                self.assertNotEqual(201, answer.status, answer.data)
                self.assertEqual(self.state_of(task), 'awaiting-review')


if __name__ == '__main__':
    unittest.main()
