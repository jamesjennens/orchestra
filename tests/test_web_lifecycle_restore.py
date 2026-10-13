"""Mark restore cuts, exact native generations and the operator switch."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import admin
import coordination
import web_lifecycle_marks as marks
from test_lifecycle_voids import rows


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source';self.source.mkdir()
        self.destination = self.root / 'destination';self.destination.mkdir()
        self.rows = rows()
        self.entry = marks.issue(self.source, 'p', self.rows[-1],
                                 dict(route=marks.route_for('p', 'p-task'), project='p', capability='coordinate'),
                                 enabled=True)
        self.files = marks.backup_files(self.source, self.rows)

    def test_exact_round_trip_keeps_original_authority_and_foreign_project_confers_none(self):
        self.assertTrue(marks.restore_files(self.destination, self.files, self.rows))
        self.assertEqual(marks.backup_files(self.destination, self.rows), self.files)
        journal = marks.read_journal(self.destination)
        self.assertTrue(marks.recorder_eligible(self.rows[-1], 'p', [], {}, journal))
        self.assertFalse(marks.recorder_eligible(self.rows[-1], 'foreign', [], {}, journal))
        self.assertEqual(journal['entries']['p-task.2'], self.entry)
        self.assertFalse(self.destination.joinpath(marks.RESTORE_MARKER).exists())

    def test_unmatched_native_generation_leaves_explicit_uncertainty(self):
        changed = copy.deepcopy(self.rows);changed[-1]['created_by'] = 'another-author'
        with self.assertRaisesRegex(ValueError, 'native event'):
            marks.restore_files(self.destination, self.files, changed)
        journal = marks.read_journal(self.destination)
        self.assertIn('web-lifecycle-mark-restore-incomplete', journal['invalid'])
        self.assertFalse(marks.recorder_eligible(changed[-1], 'p', [], {}, journal))
        with self.assertRaises(ValueError):marks.backup_files(self.destination, changed)
        self.assertTrue(marks.restore_files(self.destination, self.files, self.rows))

    def test_member_cut_retries_same_manifest_and_cannot_be_replaced_by_legacy_restore(self):
        atomic = coordination.atomic
        def cut(path, value):
            if path.parent.name == marks.DIRECTORY:
                raise OSError('synthetic member cut')
            return atomic(path, value)
        with patch.object(coordination, 'atomic', side_effect=cut), self.assertRaises(OSError):
            marks.restore_files(self.destination, self.files, self.rows)
        before = self.destination.joinpath(marks.RESTORE_MARKER).read_bytes()
        with self.assertRaisesRegex(ValueError, 'already incomplete'):
            marks.restore_files(self.destination, {}, self.rows)
        self.assertEqual(self.destination.joinpath(marks.RESTORE_MARKER).read_bytes(), before)
        self.assertTrue(marks.restore_files(self.destination, self.files, self.rows))
        self.assertEqual(marks.backup_files(self.destination, self.rows), self.files)

    def test_missing_manifest_preserves_existing_family_and_disables_its_mark_authority(self):
        marks.restore_files(self.destination, self.files, self.rows)
        before = {p.name: p.read_bytes() for p in self.destination.joinpath(marks.DIRECTORY).iterdir()}
        with self.assertRaisesRegex(ValueError, 'no web lifecycle mark manifest'):
            marks.restore_files(self.destination, {}, self.rows)
        self.assertEqual({p.name: p.read_bytes() for p in self.destination.joinpath(marks.DIRECTORY).iterdir()}, before)
        journal = marks.read_journal(self.destination)
        self.assertFalse(marks.recorder_eligible(self.rows[-1], 'p', [], {}, journal))
        self.assertTrue(marks.recorder_eligible(self.rows[-1], 'p', ['op'], {}, journal))

    def test_missing_expected_member_and_unknown_backup_path_are_refused(self):
        broken = copy.deepcopy(self.files)
        del broken[next(name for name in broken if name.startswith(marks.DIRECTORY + '/'))]
        with self.assertRaises(ValueError):admin.validate_coordination_files(broken)
        with self.assertRaises(ValueError):marks.restore_files(self.destination, broken, self.rows)
        self.assertEqual(list(self.destination.iterdir()), [])
        admin.validate_coordination_files(self.files)
        with self.assertRaises(ValueError):admin.validate_coordination_files(dict(self.files, **{'../marks.json': {}}))

    def test_absent_legacy_family_is_byte_preserving_and_needs_no_tracker_read(self):
        self.assertFalse(marks.restore_files(self.destination, {}, self.rows))
        self.assertEqual(list(self.destination.iterdir()), [])
        with patch.object(admin, 'project_dir', return_value=self.destination), \
             patch.object(admin, 'run_bd', side_effect=AssertionError('legacy absent family exported tracker')):
            self.assertFalse(admin.restore_web_lifecycle_mark_family(self.root, 'destination', {}))

    def test_operator_gate_is_independent_audited_and_status_preserves_config(self):
        config = self.root / 'deployment.private.json'
        config.write_text(json.dumps(dict(operators=['op'], lifecycle_void_writes=True,
                                         coordinator_writes=True, unrelated={'kept': True})), encoding='utf-8')
        before = config.read_bytes()
        with patch.dict(os.environ, {'ORCHESTRA_OPERATORS': ''}):
            status = admin.web_lifecycle_mark_switch(self.root, 'status', 'op')
            self.assertFalse(status['web_lifecycle_mark_writes'])
            self.assertEqual(config.read_bytes(), before)
            with self.assertRaisesRegex(ValueError, 'operator'):
                admin.web_lifecycle_mark_switch(self.root, 'on', 'worker')
            self.assertEqual(config.read_bytes(), before)
            self.assertTrue(admin.web_lifecycle_mark_switch(self.root, 'on', 'op')['changed'])
            self.assertFalse(admin.web_lifecycle_mark_switch(self.root, 'on', 'op')['changed'])
            off = admin.web_lifecycle_mark_switch(self.root, 'off', 'op')
            self.assertEqual([entry['enabled'] for entry in off['audit_history']], [True, False])
            self.assertTrue(off['audit_agrees'])
        final = json.loads(config.read_text())
        self.assertTrue(final['lifecycle_void_writes']);self.assertTrue(final['coordinator_writes'])
        self.assertEqual(final['unrelated'], {'kept': True})
        self.assertFalse(self.root.joinpath(marks.DIRECTORY).exists())


if __name__ == '__main__':
    unittest.main()
