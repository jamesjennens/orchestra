"""The approval matrix of kittrial-5bb.518: what a coordinator agent's approve POST answers, per cell.

Six cases x the project's setting (by-agent, by-person, damaged) x the installation's approval_by_another_party
(off, on) = 36 cells, each a fresh delivery approved by the granted agent Heron. The shapes are .519's
(tests/test_agent_approval_credentials.py ShapeSetup): lane-c is a name only cora has held, lane-s a name
another account held first, the agent's person is cora.
"""
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import test_agent_approval_credentials as credentials

CASES = ('another-agent', 'worker-credential', 'own-work', 'own-person', 'own-worker-credential', 'independent')
MODES = ('by-agent', 'by-person', 'damaged')
INSTALLATION = (False, True)


class MatrixFixture(credentials.ShapeSetup):
    """cora (an owner here, so her worker credentials deliver) has Heron (granted) and Egret."""

    def deliverer(self, case):
        return {'another-agent': lambda: self.deliver(self.other),
                'worker-credential': lambda: self.deliver(self.lane_c),
                'own-work': lambda: self.deliver(self.agent),
                'own-person': lambda: self.deliver(self.tokens['cora']),
                'own-worker-credential': lambda: self.deliver(self.lane_s),
                'independent': lambda: self.deliver(self.tokens['carl'])}[case]

    def cells(self):
        """Yield ``(case, mode, installation, task, contribution)`` for every cell, a fresh delivery each."""
        for mode in MODES:
            self.set_project_setting(mode)
            for installation in INSTALLATION:
                self.service.approval_by_another_party = installation
                for case in CASES:
                    task, contribution = self.deliverer(case)()
                    yield case, mode, installation, task, contribution
        self.service.approval_by_another_party = False
