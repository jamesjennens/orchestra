"""kittrial-5bb.202 rev-2: the merge-slot reads on a REAL tracker.

The rev-1 tests mock bd for ``admin.project_merge_slot_state``, the merge-slot report and
restore-new's provisioning, and the reviewer's item `one-test-through-the-real-route` asks
for one that runs all three on a real tracker ("today all three are mocked"). These do,
against the same pinned, server-mode real-bd fixture the other real-bd classes use: set
``ORCHESTRA_BD_BIN`` to the bd binary (a sibling ``dolt`` is copied too) or have ``bd`` on
PATH; without one the class skips and says so. POSIX-only, because admin.py imports fcntl.

Every test makes its project with a plain ``bd init --server`` (the way the fixture makes
its own), so it starts slotless, as a real project that predates the slot does.
"""
import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import admin  # noqa: E402
import test_bd_label_aliases as rb  # noqa: E402


def without_inherited_tests(cls):
    """The real-bd fixture is a TestCase with tests of its own; borrowing its runtime must
    not run them a second time (as tests/test_claim_held.py does)."""
    for name in dir(rb.RealBdLabelAliasTests):
        if name.startswith('test_') and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


@unittest.skipIf(rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@without_inherited_tests
class MergeSlotRealBdTests(rb.RealBdLabelAliasTests):
    """A real slotless tracker: the host's state, the report, and restore-new's provisioning."""

    def slotless_project(self, name):
        """A project made the way the fixture makes its own: a plain ``bd init --server``.

        It has no merge slot, the shape a project that predates the slot has (kittrial-5bb.202).
        """
        path = self.root / 'projects' / name
        path.mkdir(parents=True, exist_ok=True)
        init = subprocess.run(
            [str(self.bd_path), 'init', '--server', '--external', '--server-host', '127.0.0.1',
             '--server-port', str(self.port), '--server-user', 'root', '--prefix', name,
             '--database', name, '--skip-agents', '--skip-hooks', '--non-interactive'],
            cwd=str(path), env=admin.environment(self.root), capture_output=True, text=True)
        self.assertEqual(init.returncode, 0, init.stderr or init.stdout)
        return path

    def bd_in(self, name, *args):
        return subprocess.run(
            [str(self.bd_path), '--directory', str(self.root / 'projects' / name),
             '--sandbox', '--actor', 'op', *args],
            env=admin.environment(self.root), capture_output=True, text=True)

    def test_project_merge_slot_state_and_the_report_on_a_real_tracker(self):
        """The host's read and the report, both against real bd, before and after the repair.

        Before: a real project with no slot is ``missing``, and the report lists it missing and
        not healthy. The repair is exactly the call ``finish_restore`` makes for restore-new
        (``admin.provision_merge_slot``); after it, both read healthy, with no mock anywhere.
        """
        self.slotless_project('rs1')
        state = admin.project_merge_slot_state(self.root, 'rs1')
        self.assertEqual(state['state'], 'missing', state)
        self.assertIn('merge-create', state['detail'])
        report, healthy = admin.merge_slot_report(self.root, ['rs1'])
        self.assertEqual((report['missing'], report['healthy'], report['not_healthy'], healthy),
                         (['rs1'], [], ['rs1'], False), report)
        # The real bd's own answer, not only the kit's reading of it.
        checked = json.loads(self.bd_in('rs1', 'merge-slot', 'check', '--json').stdout)
        self.assertEqual((checked.get('available'), checked.get('error')), (False, 'not found'), checked)
        # restore-new's provisioning call, on the real tracker: the slot is made and both reads
        # turn healthy.
        admin.provision_merge_slot(self.root, 'rs1')
        after = admin.project_merge_slot_state(self.root, 'rs1')
        self.assertEqual(after, {'state': 'healthy', 'detail': None}, after)
        report, healthy = admin.merge_slot_report(self.root, ['rs1'])
        self.assertEqual((report['healthy'], report['missing'], healthy), (['rs1'], [], True), report)
        self.assertEqual(json.loads(self.bd_in('rs1', 'merge-slot', 'check', '--json').stdout).get('available'),
                         True)

    def test_restore_new_provisions_the_slot_of_a_real_slotless_clone(self):
        """``restore-new`` end to end on real bd: a slotless source backup, restored into a
        fresh project, comes out with a healthy slot.

        The native restore replaces the clone's rows with the source's (no slot), and the
        provisioning step then creates it. Nothing here is mocked: bd, the Dolt server and the
        SQL restore path are the fixture's real ones.
        """
        self.slotless_project('rsrc')
        # The backup target the kit's own add-project gives a project, but without its merge
        # slot: this source is a real project from before the slot was provisioned.
        (self.root / 'backups').mkdir(exist_ok=True)
        admin.run_bd(self.root, 'rsrc', ['backup', 'init', str(self.root / 'backups' / 'rsrc')])
        with contextlib.redirect_stdout(io.StringIO()):
            admin.backup_projects(self.root, ['rsrc'])
        self.assertTrue((self.root / 'backups' / 'rsrc').is_dir())
        self.assertEqual(admin.project_merge_slot_state(self.root, 'rsrc')['state'], 'missing')
        out, err = io.StringIO(), io.StringIO()
        argv = ['admin.py', '--root', str(self.root), 'restore-new', 'rsrc', 'rdst']
        code = 0
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(admin, 'root_path', return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                admin.main()
            except SystemExit as stopped:
                code = stopped.code
        self.assertEqual(code, 0, err.getvalue() or out.getvalue())
        self.assertIn('Restored only into the newly created project', out.getvalue())
        destination = self.root / 'projects' / 'rdst'
        self.assertTrue((destination / '.beads' / 'metadata.json').is_file())
        clone = admin.project_merge_slot_state(self.root, 'rdst')
        self.assertEqual(clone, {'state': 'healthy', 'detail': None}, (clone, out.getvalue(), err.getvalue()))
        # The restore copied the source's rows, so the clone's slot came from the provisioning
        # step, and the source is untouched and still slotless.
        self.assertEqual(admin.project_merge_slot_state(self.root, 'rsrc')['state'], 'missing')


if __name__ == '__main__':
    unittest.main()
