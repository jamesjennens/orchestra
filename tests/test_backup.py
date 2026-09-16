"""Native backup/coordination sidecar ordering and recovery validation."""
import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin
import coordination


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'projects' / 'source'
        self.destination = self.root / 'projects' / 'destination'
        self.source.mkdir(parents=True)
        self.destination.mkdir()
        (self.root / 'backups' / 'source').mkdir(parents=True)
        self.bundle = self.root / 'backups' / 'source.coordination.json'
        self.request_name = '.coordination-requests/' + 'a' * 64 + '.json'
        self.receipt = {'sha256': 'b' * 64, 'status': 'complete', 'id': 'source-1.1'}
        self.context = {'holder': 'alice/session', 'task': 'source-1.1', 'target': 'main'}
        self.flock = Mock()
        self.fake_fcntl = types.SimpleNamespace(flock=self.flock, LOCK_EX=2)
        self.patcher = patch.dict(sys.modules, {'fcntl': self.fake_fcntl})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def save_bundle(self, **overrides):
        data = {'schema_version': 1, 'status': 'complete',
                'files': {self.request_name: self.receipt, '.merge-context.json': self.context}}
        data.update(overrides)
        self.bundle.write_text(json.dumps(data), encoding='utf-8')

    def source_files(self):
        receipt_path = self.source / self.request_name
        receipt_path.parent.mkdir()
        receipt_path.write_text(json.dumps(self.receipt), encoding='utf-8')
        (self.source / '.merge-context.json').write_text(json.dumps(self.context), encoding='utf-8')

    def symlink_or_skip(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest('Symlink privilege unavailable: ' + str(exc))

    def test_native_sync_occurs_under_lock_with_pending_marker(self):
        self.source_files()
        def native(root, name, args):
            self.assertEqual(self.flock.call_count, 2)
            handles = [call.args[0] for call in self.flock.call_args_list]
            self.assertEqual({Path(h.name).name for h in handles}, {'source.lock', '.coordination.lock'})
            self.assertTrue(all(not h.closed for h in handles))
            self.assertEqual(name, 'source')
            self.assertEqual(args, ['backup', 'sync'])
            self.assertEqual(json.loads(self.bundle.read_text())['status'], 'pending')
            return 'native synced'
        with patch.object(admin, 'run_bd', side_effect=native):
            self.assertEqual(admin.backup_project(self.root, 'source'), 'native synced')
        data = json.loads(self.bundle.read_text())
        self.assertEqual(data['status'], 'complete')
        self.assertEqual(data['files'], {self.request_name: self.receipt, '.merge-context.json': self.context})

    def test_native_failure_replaces_old_complete_marker_with_pending(self):
        self.save_bundle()
        with patch.object(admin, 'run_bd', side_effect=RuntimeError('sync failed')):
            with self.assertRaises(RuntimeError): admin.backup_project(self.root, 'source')
        self.assertEqual(json.loads(self.bundle.read_text())['status'], 'pending')

    def test_onboarding_is_captured_in_backup_and_restored_as_text(self):
        (self.source/'ONBOARDING.md').write_text('Private project instructions 漢',encoding='utf-8')
        with patch.object(admin,'run_bd',return_value='synced'):
            admin.backup_project(self.root,'source')
        data=json.loads(self.bundle.read_text())
        self.assertEqual(data['files']['ONBOARDING.md'],{'text':'Private project instructions 漢'})
        admin.restore_coordination(self.root,'source','destination')
        self.assertEqual((self.destination/'ONBOARDING.md').read_text(encoding='utf-8'),'Private project instructions 漢')

    def test_sidecar_completion_failure_leaves_pending_after_native_success(self):
        real_atomic = coordination.atomic
        def interrupted(path, data):
            if data.get('status') == 'complete':
                raise OSError('completion interrupted')
            real_atomic(path, data)
        with patch.object(admin, 'run_bd', return_value='synced') as native, patch.object(coordination, 'atomic', side_effect=interrupted):
            with self.assertRaises(OSError): admin.backup_project(self.root, 'source')
        native.assert_called_once()
        self.assertEqual(json.loads(self.bundle.read_text())['status'], 'pending')

    def test_corrupt_source_journal_prevents_native_sync(self):
        self.source_files()
        (self.source / self.request_name).write_text('{bad', encoding='utf-8')
        with patch.object(admin, 'run_bd') as native:
            with self.assertRaises(ValueError): admin.backup_project(self.root, 'source')
        native.assert_not_called()
        self.assertEqual(json.loads(self.bundle.read_text())['status'], 'pending')

    def test_invalid_source_record_or_name_cannot_publish_complete_backup(self):
        journal = self.source / '.coordination-requests'
        journal.mkdir()
        for name, value in [('bad-name.json', self.receipt), ('a' * 64 + '.json', [])]:
            with self.subTest(name=name):
                record = journal / name
                record.write_text(json.dumps(value), encoding='utf-8')
                try:
                    with patch.object(admin, 'run_bd') as native:
                        with self.assertRaises(ValueError): admin.backup_project(self.root, 'source')
                    native.assert_not_called()
                    self.assertEqual(json.loads(self.bundle.read_text())['status'], 'pending')
                finally:
                    record.unlink()

    def test_complete_sidecar_restores_both_record_types(self):
        self.save_bundle()
        admin.restore_coordination(self.root, 'source', 'destination')
        self.assertEqual(json.loads((self.destination / self.request_name).read_text()), self.receipt)
        self.assertEqual(json.loads((self.destination / '.merge-context.json').read_text()), self.context)

    def test_legacy_missing_sidecar_emits_reconciliation_instruction(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            admin.restore_coordination(self.root, 'source', 'destination')
        self.assertIn('Reconcile', output.getvalue())
        self.assertFalse(list(self.destination.iterdir()))

    def test_pending_or_wrong_schema_refused_before_any_restore_write(self):
        for overrides in ({'status': 'pending'}, {'schema_version': 2}):
            with self.subTest(overrides=overrides):
                self.save_bundle(**overrides)
                with self.assertRaises(ValueError): admin.restore_coordination(self.root, 'source', 'destination')
                self.assertFalse(list(self.destination.iterdir()))

    def test_all_sidecar_paths_validated_before_first_write(self):
        self.save_bundle(files={self.request_name: self.receipt, '../escape.json': {}})
        with self.assertRaises(ValueError): admin.restore_coordination(self.root, 'source', 'destination')
        self.assertFalse(list(self.destination.iterdir()))
        self.assertFalse((self.destination.parent / 'escape.json').exists())

    def test_bad_container_or_record_is_rejected_before_writes(self):
        for files in ([], None, {self.request_name: []}, {self.request_name: self.receipt, '.merge-context.json': None}):
            with self.subTest(files=files):
                self.save_bundle(files=files)
                with self.assertRaises(ValueError): admin.restore_coordination(self.root, 'source', 'destination')
                self.assertFalse(list(self.destination.iterdir()))

    def test_restore_command_validates_entire_sidecar_before_project_creation(self):
        for overrides in ({'schema_version': 999}, {'files': {'../escape.json': {}}}, {'files': []}):
            with self.subTest(overrides=overrides):
                self.save_bundle(**overrides)
                argv = ['admin.py', '--root', str(self.root), 'restore-new', 'source', 'destination']
                with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), patch.object(admin, 'add_project') as create, patch.object(admin, 'run_bd') as native:
                    with self.assertRaises(ValueError): admin.main()
                create.assert_not_called()
                native.assert_not_called()

    def test_restore_holds_source_backup_lock_across_entire_pair(self):
        self.save_bundle()
        phases = []
        def locked(phase):
            self.flock.assert_called_once()
            handle = self.flock.call_args.args[0]
            self.assertEqual(Path(handle.name), self.root / 'backups' / 'source.lock')
            self.assertFalse(handle.closed, phase)
            phases.append(phase)
        real_read = admin.coordination_backup
        def read(root, source):
            locked('validate-sidecar')
            return real_read(root, source)
        def create(*args): locked('create-destination')
        def native(*args):
            locked('restore-native')
            return 'restored'
        def restore(*args): locked('restore-sidecar')
        argv = ['admin.py', '--root', str(self.root), 'restore-new', 'source', 'destination']
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), patch.object(admin, 'coordination_backup', side_effect=read), patch.object(admin, 'add_project', side_effect=create), patch.object(admin, 'run_bd', side_effect=native), patch.object(admin, 'restore_coordination', side_effect=restore), contextlib.redirect_stdout(io.StringIO()):
            admin.main()
        self.assertEqual(phases, ['validate-sidecar', 'create-destination', 'restore-native', 'restore-sidecar'])
        self.assertTrue(self.flock.call_args.args[0].closed)

    def test_backup_rejects_symlink_journal_directory(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / ('a' * 64 + '.json')).write_text(json.dumps(self.receipt))
        self.symlink_or_skip(self.source / '.coordination-requests', outside, directory=True)
        with patch.object(admin, 'run_bd') as native:
            with self.assertRaises(ValueError): admin.backup_project(self.root, 'source')
        native.assert_not_called()

    def test_restore_rejects_symlink_destination_parent(self):
        self.save_bundle()
        outside = self.root / 'outside'
        outside.mkdir()
        self.symlink_or_skip(self.destination / '.coordination-requests', outside, directory=True)
        with self.assertRaises(ValueError): admin.restore_coordination(self.root, 'source', 'destination')
        self.assertFalse(list(outside.iterdir()))

    def test_new_project_initializes_and_performs_backup(self):
        target = self.root / 'projects' / 'newproject'
        with patch.object(admin, 'config', return_value={'port': 13317}), patch.object(admin, 'run_bd', return_value='') as native, patch.object(admin, 'backup_project') as backup, contextlib.redirect_stdout(io.StringIO()):
            admin.add_project(self.root, 'newproject')
        self.assertTrue(target.is_dir())
        self.assertIn((self.root, 'newproject', ['backup', 'init', str(self.root / 'backups' / 'newproject')]), [c.args for c in native.call_args_list])
        backup.assert_called_once_with(self.root, 'newproject')


if __name__ == '__main__':
    unittest.main()
