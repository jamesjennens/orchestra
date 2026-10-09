"""Follow-ups to kittrial-5bb.152 (kittrial-5bb.157).

1. The refusal keeps the phrase "A coordination backup must not be a symlink", and
   backup-status keeps its own wording for a symlinked sidecar.
2. `restore-new --without-coordination` on a truly legacy backup says there was nothing to
   leave out.
3. backup-copy and retire-project with a FIFO as the canonical sidecar copy answer
   instead of hanging (POSIX, each bounded so a regression fails instead of hanging CI).
4. A directory where a sidecar copy belongs makes `backup` refuse before writing anything,
   naming it and what to do, instead of ending in a raw "Is a directory" error.
"""
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import admin
import test_backup_multi as multi
import test_backup_native_sync as native_sync
import test_retire_project as retire
import test_sidecar_agreement as agreement


def bounded(test, call, seconds=20):
    """``call()`` in a daemon thread; a blocked read fails ``test`` instead of hanging it."""
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(call()), daemon=True)
    worker.start()
    worker.join(seconds)
    test.assertFalse(worker.is_alive(), 'the command blocked')
    return outcome[0]


def harness(test, case_class):
    """An existing test case's fixture (its setUp and helpers) without running its tests."""
    case = case_class('setUp')            # any existing attribute names the instance
    case.setUp()
    test.addCleanup(case.doCleanups)
    return case


class SymlinkWordingTests(unittest.TestCase):

    def setUp(self):
        self.case = harness(self, agreement.RestoreNewOutcomeTests)

    def test_the_refusal_keeps_the_symlink_sentence(self):
        for index, (canonical, fallback) in enumerate((('symlink', 'absent'), ('absent', 'symlink'),
                                                       ('pending', 'symlink'), ('symlink', 'good'))):
            root = self.case.combination(800 + index, canonical, fallback)
            if root is None:
                self.skipTest('symlinks unavailable')
            with self.subTest(canonical=canonical, last_complete=fallback):
                code, output, calls = self.case.run_restore(root)
                self.assertEqual((code, calls), (1, []), output)
                self.assertIn('A coordination backup must not be a symlink.', output)

    def test_the_refusal_without_a_symlink_does_not_say_it(self):
        root = self.case.combination(810, 'absent', 'pending')
        code, output, _ = self.case.run_restore(root)
        self.assertEqual(code, 1, output)
        self.assertNotIn('must not be a symlink', output)

    def test_backup_status_says_a_symlinked_sidecar_must_not_be_one(self):
        root = self.case.deployment('status-symlink')
        if not self.case.make(root / 'backups' / 'alpha.coordination.json', 'symlink', 'canonical'):
            self.skipTest('symlinks unavailable')
        self.assertEqual(admin.backup_pair_state(root, 'alpha'),
                         (False, 'coordination sidecar must not be a symlink'))


@unittest.skipIf(sys.platform == 'win32', 'admin.py host commands take the POSIX lock')
class LegacyWithFlagTests(unittest.TestCase):

    def test_the_flag_on_a_legacy_backup_says_there_was_nothing_to_leave_out(self):
        case = harness(self, agreement.RestoreNewOutcomeTests)
        root = case.combination(900, 'absent', 'absent')
        code, output, calls = case.run_restore(root, '--without-coordination')
        self.assertEqual((code, calls), (0, ['beta']), output)
        self.assertIn('Legacy backup has no coordination journal', output)
        self.assertEqual(output.rstrip('\n').splitlines()[-1],
                         '--without-coordination: this backup has no coordination sidecar at all, so there was '
                         'nothing to leave out; it was restored as a legacy backup, exactly as without the flag.')
        # Without the flag, the legacy restore says nothing about it.
        root = case.combination(901, 'absent', 'absent')
        code, output, _ = case.run_restore(root)
        self.assertEqual(code, 0, output)
        self.assertNotIn('--without-coordination', output)


@unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFOs are POSIX-only')
class FifoSidecarCommandTests(unittest.TestCase):
    """Both commands read the pair through backup_pair_state, which used to open the sidecar
    with a plain read: a FIFO there made them wait for good (fixed in kittrial-5bb.152)."""

    def test_backup_copy_with_a_fifo_for_a_sidecar_refuses_instead_of_hanging(self):
        case = harness(self, multi.InterruptedRunAndCopyLockCase)
        case.record()
        sidecar = case.root / 'backups' / 'alpha.coordination.json'
        sidecar.unlink()
        os.mkfifo(str(sidecar))
        stdout, stderr, code = bounded(self, lambda: case.run_admin('backup-copy', str(case.destination)))
        self.assertNotEqual(code, 0)
        self.assertIn('alpha: coordination sidecar is not a regular file', stderr)
        self.assertFalse((case.destination / 'alpha.coordination.json').exists())

    def test_retire_project_with_a_fifo_for_a_sidecar_answers_instead_of_hanging(self):
        case = harness(self, retire.RetireCase)
        sidecar = case.root / 'backups' / 'gamma.coordination.json'
        sidecar.unlink()
        os.mkfifo(str(sidecar))
        stdout, stderr, code = bounded(self, lambda: case.retire('gamma'))
        self.assertIn('coordination sidecar is not a regular file', stdout + stderr)


class DirectoryInPlaceOfACopyTests(native_sync.RuntimeCase):
    """kittrial-5bb.157 item 4: refuse, naming it and what to do; never replace or delete it."""

    def setUp(self):
        super().setUp()
        native_sync.make_project(self.root, 'alpha')
        (self.root / 'backups' / 'alpha').mkdir(exist_ok=True)
        native_sync.write_pair(self.root, 'alpha', files={'.merge-context.json': {'holder': 'prior'}})

    def test_a_directory_in_place_of_either_copy_is_refused_and_left_alone(self):
        for copy in ('alpha.coordination.last-complete.json', 'alpha.coordination.json'):
            with self.subTest(copy=copy):
                path = self.root / 'backups' / copy
                kept = None
                if path.exists():
                    kept = path.read_bytes()
                    path.unlink()
                path.mkdir()
                (path / 'operator-notes.txt').write_text('not the kit\'s', encoding='utf-8')
                pair = {name: (self.root / 'backups' / name).read_bytes()
                        for name in ('alpha.coordination.json', 'alpha.coordination.last-complete.json')
                        if (self.root / 'backups' / name).is_file()}
                with patch.object(admin, 'native_backup_sync', side_effect=AssertionError('synced')):
                    stdout, stderr, code = self.run_admin('backup', 'alpha')
                self.assertEqual(code, 1, stderr)
                self.assertIn('backups/%s is a directory where the coordination sidecar copy belongs' % copy, stderr)
                self.assertIn('the pair was not touched', stderr)
                self.assertIn('move it out of backups/, then run backup again', stderr)
                self.assertNotIn('Errno', stderr)
                # Nothing was replaced or deleted: the directory and the files beside it are as they were.
                self.assertEqual((path / 'operator-notes.txt').read_text(encoding='utf-8'), 'not the kit\'s')
                for name, data in pair.items():
                    self.assertEqual((self.root / 'backups' / name).read_bytes(), data, name)
                # Moving it aside is all it takes.
                (path / 'operator-notes.txt').unlink()
                path.rmdir()
                if kept is not None:
                    path.write_bytes(kept)
                with patch.object(admin, 'native_backup_sync', return_value='Backup synced'):
                    stdout, stderr, code = self.run_admin('backup', 'alpha')
                self.assertEqual(code, 0, stderr)
                self.assertEqual(admin.backup_pair_state(self.root, 'alpha'), (True, None))


if __name__ == '__main__':
    unittest.main()
