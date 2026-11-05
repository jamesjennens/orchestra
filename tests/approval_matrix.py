"""The approval matrix of kittrial-5bb.518: what a coordinator agent's approve POST answers, per cell.

Written to run on c01d067 as well as on the changed tree (it names nothing the change adds), so the answers of
c01d067 could be recorded and stored as literal expectations in tests/test_approval_permission.py.
"""
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import test_coordinator_grant

CASES = ('another-agent', 'own-work', 'own-person', 'own-worker-credential', 'independent')
MODES = ('by-agent', 'by-person', 'damaged')
INSTALLATION = (False, True)


class MatrixFixture(test_coordinator_grant.Grants):
    """cora is an owner here (so her worker credential can deliver); her agent Heron holds the coordinator grant."""

    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.grant().status)
        self.other, self.other_id = self.agent_with_id('cora', 'Egret')
        self.assertEqual(200, self.member('cora', 'owner', token=self.admin).status)
        self.worker, _ = self.worker_of('cora', 'lane-c')

    def set_mode(self, mode):
        if mode == 'damaged':
            with self.service.store.lock:
                self.service.state['agent_approval'] = {self.project: 'garbage'}
            return
        with self.service.store.lock:
            self.service.state.pop('agent_approval', None)
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/agent-approval' % self.project,
                                           {'agent_approval': mode}, token=self.tokens['olive']).status)

    def deliverer(self, case):
        return {'another-agent': self.other, 'own-work': self.agent, 'own-person': self.tokens['cora'],
                'own-worker-credential': self.worker, 'independent': self.tokens['carl']}[case]

    def cells(self):
        """Yield ``(case, mode, installation, task, contribution)`` for every cell, a fresh delivery each."""
        for mode in MODES:
            self.set_mode(mode)
            for installation in INSTALLATION:
                self.service.approval_by_another_party = installation
                for case in CASES:
                    task, contribution = self.deliver(self.deliverer(case))
                    yield case, mode, installation, task, contribution
        self.service.approval_by_another_party = False
