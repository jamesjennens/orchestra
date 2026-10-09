"""Native backup/coordination sidecar ordering and recovery validation."""
import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin
import coordination
import handoff
import feedback


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
        # ``add-project`` takes the creation lock too (kittrial-5bb.176), so the stand-in
        # needs the whole fcntl surface ``http_authority.file_lock`` uses.
        self.fake_fcntl = types.SimpleNamespace(flock=self.flock, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        self.patcher = patch.dict(sys.modules, {'fcntl': self.fake_fcntl})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def allow_operator(self, actor='operator-1'):
        """The reconcile host commands check the deployment operator allowlist (kittrial-5bb.85)."""
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'unit': 'none', 'operators': [actor]}), encoding='utf-8')
        environment = patch.dict(os.environ, {'ORCHESTRA_OPERATORS': ''})
        environment.start()
        self.addCleanup(environment.stop)

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
            self.assertEqual(json.loads(self.bundle.read_text(encoding='utf-8'))['status'], 'pending')
            return 'native synced'
        with patch.object(admin, 'run_bd', side_effect=native):
            self.assertEqual(admin.backup_project(self.root, 'source'), 'native synced')
        data = json.loads(self.bundle.read_text(encoding='utf-8'))
        self.assertEqual(data['status'], 'complete')
        self.assertEqual(data['files'], {self.request_name: self.receipt, '.merge-context.json': self.context})

    def test_native_failure_keeps_the_previous_complete_pair_restorable(self):
        # kittrial-5bb.39 rev2: a failed run must not leave only a `pending` marker
        # behind, so the complete sidecar saved aside before the marker is put back and
        # restore can still use the previous complete pair.
        self.save_bundle()
        with patch.object(admin, 'run_bd', side_effect=RuntimeError('sync failed')):
            with self.assertRaises(RuntimeError): admin.backup_project(self.root, 'source')
        self.assertEqual(json.loads(self.bundle.read_text(encoding='utf-8'))['status'], 'complete')
        self.assertEqual(admin.coordination_backup(self.root, 'source'),
                         {self.request_name: self.receipt, '.merge-context.json': self.context})
        aside = admin.last_complete_sidecar_path(self.root, 'source')
        self.assertEqual(json.loads(aside.read_text(encoding='utf-8'))['status'], 'complete')

    def test_onboarding_is_captured_in_backup_and_restored_as_text(self):
        (self.source/'ONBOARDING.md').write_text('Private project instructions 漢',encoding='utf-8')
        with patch.object(admin,'run_bd',return_value='synced'):
            admin.backup_project(self.root,'source')
        data=json.loads(self.bundle.read_text(encoding='utf-8'))
        self.assertEqual(data['files']['ONBOARDING.md'],{'text':'Private project instructions 漢'})
        admin.restore_coordination(self.root,'source','destination')
        self.assertEqual((self.destination/'ONBOARDING.md').read_text(encoding='utf-8'),'Private project instructions 漢')

    def test_session_registry_is_in_native_backup_sidecar(self):
        registry={'schema_version':1,'records':{}}
        (self.source/'.sessions.json').write_text(json.dumps(registry))
        with patch.object(admin,'run_bd',return_value='synced'):admin.backup_project(self.root,'source')
        self.assertEqual(json.loads(self.bundle.read_text(encoding='utf-8'))['files']['.sessions.json'],registry)
        admin.restore_coordination(self.root,'source','destination')
        self.assertEqual(json.loads((self.destination/'.sessions.json').read_text()),registry)

    def test_feedback_feed_is_backed_up_and_restored_as_private_text(self):
        feed_path = self.source/'.feedback.jsonl'
        payload = {
            'operation_id': 'backup-op',
            'created_at': '2026-09-19T23:00:00+00:00',
            'body': 'private \u0085 \u2028 \u2029 text',
            'source': {'task': 'kittrial-5bb.13', 'version': 'base-1'},
            'evidence': ['test://backup'],
            'triage': {'task': 'kittrial-5bb.13', 'label': 'review'},
            'reminder': {'kind': 'none', 'text': ''},
            'supersedes': None,
        }
        feedback.add(feed_path, 'session-one', payload)
        self.assertEqual(feedback.list_entries(feed_path)['entries'][0]['body'], payload['body'])
        feed = feed_path.read_text(encoding='utf-8')
        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'source')
        data = json.loads(self.bundle.read_text(encoding='utf-8'))
        self.assertEqual(data['files']['.feedback.jsonl'], {'text': feed})
        admin.restore_coordination(self.root, 'source', 'destination')
        restored_path = self.destination/'.feedback.jsonl'
        self.assertEqual(restored_path.read_text(encoding='utf-8'), feed)
        restored = feedback.list_entries(restored_path)['entries'][0]
        self.assertEqual(restored['body'], payload['body'])
        self.assertTrue(feedback.add(restored_path, 'session-one', payload)['reconciled'])

    def test_feedback_quarantine_evidence_is_backed_up_and_restored_as_bytes(self):
        feed_path = self.source / feedback.FEED_NAME
        feedback.add(feed_path, 'session-one', {
            'operation_id': 'quarantine-backup-op',
            'created_at': '2026-09-19T23:00:00+00:00',
            'body': 'valid prefix',
            'source': {'task': 'kittrial-5bb.13', 'version': 'base-1'},
            'evidence': ['test://backup'],
            'triage': {'task': 'kittrial-5bb.13', 'label': 'review'},
            'reminder': {'kind': 'none', 'text': ''},
            'supersedes': None,
        })
        tail = b'\xfftruncated json'
        with feed_path.open('ab') as stream:
            stream.write(tail)

        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'source')
        files = json.loads(self.bundle.read_text(encoding='utf-8'))['files']
        quarantine_name = next(name for name in files if name.endswith(feedback.QUARANTINE_SUFFIX))
        self.assertEqual(base64.b64decode(files[quarantine_name]['base64']), tail)

        admin.restore_coordination(self.root, 'source', 'destination')
        restored = self.destination / quarantine_name
        self.assertEqual(restored.read_bytes(), tail)
        self.assertEqual(len(feedback.list_entries(self.destination / feedback.FEED_NAME)['entries']), 1)

        files[quarantine_name]['base64'] = base64.b64encode(b'different bytes').decode('ascii')
        with self.assertRaisesRegex(ValueError, 'digest does not match'):
            admin.validate_coordination_files(files)

    def test_sidecar_completion_failure_leaves_pending_after_native_success(self):
        real_atomic = coordination.atomic
        def interrupted(path, data):
            if data.get('status') == 'complete':
                raise OSError('completion interrupted')
            real_atomic(path, data)
        with patch.object(admin, 'run_bd', return_value='synced') as native, patch.object(coordination, 'atomic', side_effect=interrupted):
            with self.assertRaises(OSError): admin.backup_project(self.root, 'source')
        native.assert_called_once()
        self.assertEqual(json.loads(self.bundle.read_text(encoding='utf-8'))['status'], 'pending')

    def test_corrupt_source_journal_prevents_native_sync(self):
        self.source_files()
        (self.source / self.request_name).write_text('{bad', encoding='utf-8')
        with patch.object(admin, 'run_bd') as native:
            with self.assertRaises(ValueError): admin.backup_project(self.root, 'source')
        native.assert_not_called()
        self.assertEqual(json.loads(self.bundle.read_text(encoding='utf-8'))['status'], 'pending')

    def test_handoff_request_records_are_validated_in_backup(self):
        payload={'schema_version':1,'operation':'request',
                 'request_id':'00000000-0000-0000-0000-000000000007',
                 'task':'source-1.1','from_actor':'alice','to_actor':'bob',
                 'reason':'Take over'}
        handoff.request(self.source,'bob',payload)
        record=next((self.source/'.handoff-requests').glob('*.json'))
        files={'.handoff-requests/'+record.name:json.loads(record.read_text(encoding='utf-8'))}
        admin.validate_coordination_files(files)
        files['.handoff-requests/'+record.name]['status']='accepted'
        with self.assertRaises(ValueError):
            admin.validate_coordination_files(files)

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
                    self.assertEqual(json.loads(self.bundle.read_text(encoding='utf-8'))['status'], 'pending')
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

    def test_restore_new_refuses_corrupt_operator_allowlist_before_any_write(self):
        self.save_bundle()
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'operators': {'bad': 1}}), encoding='utf-8')
        fresh = self.root / 'projects' / 'fresh'
        argv = ['admin.py', '--root', str(self.root), 'restore-new', 'source', 'fresh']
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'add_project') as create, patch.object(admin, 'run_bd') as native:
            with self.assertRaisesRegex(ValueError, 'operators must be a list'):
                admin.main()
        create.assert_not_called()
        native.assert_not_called()
        self.assertFalse(fresh.exists())

    def test_restore_operators_requires_the_deployment_config_before_any_write(self):
        self.save_bundle()
        self.assertFalse((self.root / 'deployment.private.json').exists())
        fresh = self.root / 'projects' / 'fresh'
        argv = ['admin.py', '--root', str(self.root), 'restore-new', 'source', 'fresh', '--restore-operators']
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'add_project') as create, patch.object(admin, 'run_bd') as native:
            with self.assertRaisesRegex(ValueError, 'Deployment is not installed; run install first'):
                admin.main()
        create.assert_not_called()
        native.assert_not_called()
        self.assertFalse(fresh.exists())

    def test_restore_new_with_a_valid_allowlist_still_restores(self):
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'operators': ['operator']}), encoding='utf-8')
        self.save_bundle()
        argv = ['admin.py', '--root', str(self.root), 'restore-new', 'source', 'destination']
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'add_project') as create, \
                patch.object(admin, 'run_bd', return_value='restored') as native, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        create.assert_called_once_with(self.root, 'destination')
        # `bd backup restore` and then the kittrial-5bb.49 re-point of the clone's own
        # native backup target (a restored clone otherwise keeps the source's target).
        self.assertEqual([item.args[2] for item in native.call_args_list],
                         [['backup', 'restore', str(self.root / 'backups' / 'source'), '--force'],
                          ['backup', 'init', str(self.root / 'backups' / 'destination')],
                          # kittrial-5bb.202 item 3: a source made before the slot was provisioned
                          # would leave the clone without one, so restore-new provisions it too.
                          ['merge-slot', 'check', '--json'],
                          ['merge-slot', 'create', '--json']])
        self.assertIn('Restored only into the newly created project', out.getvalue())
        self.assertEqual(json.loads((self.destination / self.request_name).read_text()), self.receipt)

    def restore_with_a_failing_provisioning(self, slot_answer, empty_destination=False):
        """``restore-new`` whose merge-slot provisioning step fails; returns (order, notice).

        The step itself is what fails (``provision_merge_slot`` is patched to raise), so the
        order the other steps ran in can be read from ``order`` and the notice's claims can be
        checked against what the clone's own ``merge-slot check`` answers (``slot_answer``,
        fed to the read ``restore_failure_notice`` makes through ``project_merge_slot_state``).
        ``empty_destination`` makes ``restore_destination_state``'s read answer the destination's
        own merge slot alone: its "empty, working project" shape, which a clone from an empty
        slotless source has.
        """
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'operators': ['operator']}), encoding='utf-8')
        self.save_bundle()
        (self.destination / '.beads').mkdir()
        (self.destination / '.beads' / 'metadata.json').write_text(
            json.dumps({'dolt_server_host': '127.0.0.1', 'dolt_server_port': 13317,
                        'dolt_server_user': 'root', 'dolt_database': 'destination'}), encoding='utf-8')
        order = []

        def native(root, name, args):
            order.append(list(args))
            if list(args)[:2] == ['merge-slot', 'check']:
                return slot_answer
            if list(args)[:1] == ['list']:
                return json.dumps({'id': 'destination-merge-slot'}) if empty_destination else 'not rows'
            return 'restored'

        def sidecar(*args, **kwargs):
            order.append('sidecar')
            return True

        def journal(*args, **kwargs):
            order.append('journal')
            return None

        def provision(*args, **kwargs):
            order.append('provision')
            raise admin.subprocess.CalledProcessError(1, 'bd', stderr='create refused')

        argv = ['admin.py', '--root', str(self.root), 'restore-new', 'source', 'destination']
        stderr = io.StringIO()
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'add_project'), \
                patch.object(admin, 'native_restore', return_value='restored (stubbed native restore)'), \
                patch.object(admin, 'run_bd', side_effect=native), \
                patch.object(admin, 'restore_coordination', side_effect=sidecar), \
                patch.object(admin, 'restore_journal', side_effect=journal), \
                patch.object(admin, 'provision_merge_slot', side_effect=provision), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr), \
                self.assertRaises(admin.subprocess.CalledProcessError):
            admin.main()
        return order, stderr.getvalue()

    def test_a_failed_provisioning_leaves_the_sidecar_and_journals_restored(self):
        """kittrial-5bb.202 rev-3 item 1 (F1): the provisioning step runs AFTER the re-point,
        the coordination sidecar and the journals (it ran before them in revision 2, and the
        reviewer's `g3` measured a failed clone with no `.http-operations.sqlite3`)."""
        order, notice = self.restore_with_a_failing_provisioning(json.dumps(
            {'available': False, 'error': 'not found', 'id': 'destination-merge-slot'}))
        self.assertLess(order.index('sidecar'), order.index('provision'), order)
        self.assertLess(order.index('journal'), order.index('provision'), order)
        self.assertIn('the merge-slot provisioning step failed', notice)
        self.assertIn('its data, its re-pointed backup target and its journals are in place', notice)
        # A genuinely slotless clone: the slot is the one thing left, and merge-create is named.
        self.assertIn('Its merge slot is still missing', notice)
        self.assertIn('merge-create', notice)
        self.assertIn('retire-project destination', notice)
        self.assertNotIn('its backup fails', notice)
        self.assertNotIn('the re-point and coordination step failed', notice)

    def test_a_failed_provisioning_does_not_call_a_slot_missing_that_is_there(self):
        """The reviewer's `failcheck` shape: the step fails on its own check while the slot came
        with the source (merge-check rc 0, report healthy). Saying "was not provisioned" or
        "the report names it missing" would be untrue (F1)."""
        _, notice = self.restore_with_a_failing_provisioning(json.dumps(
            {'available': True, 'holder': None, 'id': 'destination-merge-slot', 'waiters': None}))
        self.assertIn('Its merge slot reads healthy', notice)
        self.assertIn('nothing is left to mend', notice)
        self.assertIn('its data, its re-pointed backup target and its journals are in place', notice)
        self.assertNotIn('was not provisioned', notice)
        self.assertNotIn('names it missing', notice)
        self.assertNotIn('Its merge slot is still missing', notice)
        self.assertNotIn('its backup fails', notice)

    def test_a_failed_provisioning_of_an_empty_clone_is_not_said_as_nothing_restored(self):
        """A clone from an empty slotless source reads as the "empty, working project" shape (bd
        lists no rows at all), but after the reorder its coordination sidecar and journals WERE
        restored: the notice must be the provisioning one, not the "nothing was restored" one."""
        _, notice = self.restore_with_a_failing_provisioning(json.dumps(
            {'available': False, 'error': 'not found', 'id': 'destination-merge-slot'}),
            empty_destination=True)
        self.assertIn('the merge-slot provisioning step failed', notice)
        self.assertIn('Its merge slot is still missing', notice)
        self.assertIn('its data, its re-pointed backup target and its journals are in place', notice)
        self.assertNotIn('nothing was restored', notice)
        self.assertNotIn('NOT restored', notice)

    def test_a_failed_provisioning_that_cannot_read_the_slot_does_not_guess(self):
        """The same fault that stopped the step can stop the re-read: the notice says so plainly
        rather than claiming the slot is missing (F1)."""
        _, notice = self.restore_with_a_failing_provisioning('not json at all')
        self.assertIn('Whether it has a usable merge slot could not be read', notice)
        self.assertIn('its data, its re-pointed backup target and its journals are in place', notice)
        self.assertNotIn('Its merge slot is still missing', notice)
        self.assertNotIn('was not provisioned', notice)
        self.assertNotIn('its backup fails', notice)

    def restore_new_output(self):
        argv = ['admin.py', '--root', str(self.root), 'restore-new', 'source', 'destination']
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'add_project'), patch.object(admin, 'run_bd', return_value='restored'), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        return out.getvalue()

    def test_restore_new_prints_what_a_degraded_backup_did_not_carry(self):
        # kittrial-5bb.105 (review of f2d6050: the note had no test through the command).
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'operators': ['operator']}), encoding='utf-8')
        self.save_bundle()
        stamp = admin.utc_stamp()
        entry = {'name': 'source', 'status': 'complete', 'completed_at': stamp,
                 'pair': {'native': 'backups/source', 'coordination': 'backups/source.coordination.json'}}
        record = {'schema_version': 1, 'scope': 'all', 'generated_at': stamp, 'status': 'complete',
                  'projects': [dict(entry, degraded='guidance is degraded: the pair was left out; repair it')]}
        admin.write_backup_status(self.root, record)
        output = self.restore_new_output()
        self.assertIn('Restored only into the newly created project', output)
        note = [line for line in output.splitlines() if line.startswith('Note: the last backup run recorded')]
        self.assertEqual(len(note), 1, output)
        self.assertIn('recorded source degraded', note[0])
        self.assertIn('was not restored into destination: guidance is degraded: the pair was left out', note[0])

    def test_restore_new_prints_no_degraded_note_for_a_clean_or_missing_run_record(self):
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'operators': ['operator']}), encoding='utf-8')
        self.save_bundle()
        self.assertNotIn('degraded', self.restore_new_output())               # no run record at all
        import shutil
        shutil.rmtree(self.destination); self.destination.mkdir()
        stamp = admin.utc_stamp()
        admin.write_backup_status(self.root, {
            'schema_version': 1, 'scope': 'all', 'generated_at': stamp, 'status': 'complete',
            'projects': [{'name': 'source', 'status': 'complete', 'completed_at': stamp,
                          'pair': {'native': 'backups/source', 'coordination': 'backups/source.coordination.json'}}]})
        self.assertNotIn('degraded', self.restore_new_output())

    def test_restore_new_repoints_the_clone_at_its_own_native_backup_target(self):
        # kittrial-5bb.49: `bd backup restore` carries the SOURCE project's backup target
        # into the clone, so without re-pointing, `backup destination` syncs the clone into
        # backups/source and backups/destination goes stale.
        self.save_bundle()
        calls = []

        def native(root, name, args):
            calls.append((name, list(args)))
            return 'restored'

        argv = ['admin.py', '--root', str(self.root), 'restore-new', 'source', 'destination']
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'add_project'), patch.object(admin, 'run_bd', side_effect=native), \
                contextlib.redirect_stdout(io.StringIO()):
            admin.main()
        self.assertEqual([name for name, _ in calls], ['destination', 'destination', 'destination', 'destination'])
        self.assertEqual(calls[0][1],
                         ['backup', 'restore', str(self.root / 'backups' / 'source'), '--force'])
        self.assertEqual(calls[1][1],
                         ['backup', 'init', str(self.root / 'backups' / 'destination')])
        # kittrial-5bb.202 item 3: the restore provisions the clone's merge slot (idempotent).
        self.assertEqual(calls[2][1], ['merge-slot', 'check', '--json'])
        self.assertEqual(calls[3][1], ['merge-slot', 'create', '--json'])

    def test_restore_holds_source_backup_lock_across_entire_pair(self):
        self.save_bundle()
        phases = []
        def locked(phase):
            # The source's backup lock is taken first and held to the end. From the moment
            # the destination starts to exist, its own restore lock is held too, so
            # retire-project cannot pull it away mid-restore (kittrial-5bb.85).
            handles = [call.args[0] for call in self.flock.call_args_list]
            self.assertEqual([Path(handle.name).name for handle in handles],
                             ['source.lock', 'destination.restore.lock'][:1 if phase == 'validate-sidecar' else 2])
            self.assertFalse(any(handle.closed for handle in handles), phase)
            phases.append(phase)
        real_read = admin.coordination_backup
        def read(root, source):
            locked('validate-sidecar')
            return real_read(root, source)
        def create(*args): locked('create-destination')
        def native(*args):
            # The native restore, then the kittrial-5bb.49 re-point of the clone's target.
            locked('repoint-native' if args[2][:2] == ['backup', 'init'] else 'restore-native')
            return 'restored'
        def restore(*args, **kwargs): locked('restore-sidecar')
        argv = ['admin.py', '--root', str(self.root), 'restore-new', 'source', 'destination']
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), patch.object(admin, 'coordination_backup', side_effect=read), patch.object(admin, 'add_project', side_effect=create), patch.object(admin, 'run_bd', side_effect=native), patch.object(admin, 'restore_coordination', side_effect=restore), contextlib.redirect_stdout(io.StringIO()):
            admin.main()
        self.assertEqual(phases, ['validate-sidecar', 'create-destination', 'restore-native',
                                  'repoint-native', 'restore-sidecar', 'restore-native', 'restore-native'])
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

        def native(root, name, args):
            if args[:2] == ['merge-slot', 'check']:
                # EXACT real bd 1.2.2 missing-slot JSON: `available` IS present
                # (false) alongside `error`, and the command exits 0.
                return json.dumps({'available': False, 'error': 'not found',
                                   'id': 'newproject-merge-slot'})
            return ''

        with patch.object(admin, 'config', return_value={'port': 13317}), patch.object(admin, 'run_bd', side_effect=native) as run_bd, patch.object(admin, 'backup_project') as backup, contextlib.redirect_stdout(io.StringIO()):
            admin.add_project(self.root, 'newproject')
        self.assertTrue(target.is_dir())
        calls = [c.args for c in run_bd.call_args_list]
        self.assertIn((self.root, 'newproject', ['backup', 'init', str(self.root / 'backups' / 'newproject')]), calls)
        self.assertIn((self.root, 'newproject', ['merge-slot', 'check', '--json']), calls)
        # The real missing shape must still cause add-project to create the slot.
        self.assertIn((self.root, 'newproject', ['merge-slot', 'create', '--json']), calls)
        backup.assert_called_once_with(self.root, 'newproject')

    def test_add_project_does_not_recreate_an_existing_merge_slot(self):
        target = self.root / 'projects' / 'newproject'

        def native(root, name, args):
            if args[:2] == ['merge-slot', 'check']:
                # Real bd 1.2.2 existing-slot JSON carries holder AND waiters.
                return json.dumps({'available': True, 'holder': None,
                                   'id': 'newproject-merge-slot', 'waiters': None})
            return ''

        with patch.object(admin, 'config', return_value={'port': 13317}), patch.object(admin, 'run_bd', side_effect=native) as run_bd, patch.object(admin, 'backup_project'), contextlib.redirect_stdout(io.StringIO()):
            admin.add_project(self.root, 'newproject')
        self.assertTrue(target.is_dir())
        calls = [c.args[2] for c in run_bd.call_args_list]
        self.assertIn(['merge-slot', 'check', '--json'], calls)
        self.assertNotIn(['merge-slot', 'create', '--json'], calls)

    def test_add_project_does_not_recreate_a_held_merge_slot(self):
        def native(root, name, args):
            if args[:2] == ['merge-slot', 'check']:
                # A held slot is available:false WITH a holder - not missing.
                return json.dumps({'available': False, 'holder': 'someone',
                                   'id': 'newproject-merge-slot', 'waiters': None})
            return ''

        with patch.object(admin, 'config', return_value={'port': 13317}), patch.object(admin, 'run_bd', side_effect=native) as run_bd, patch.object(admin, 'backup_project'), contextlib.redirect_stdout(io.StringIO()):
            admin.add_project(self.root, 'newproject')
        calls = [c.args[2] for c in run_bd.call_args_list]
        self.assertIn(['merge-slot', 'check', '--json'], calls)
        self.assertNotIn(['merge-slot', 'create', '--json'], calls)

    def test_provision_merge_slot_tolerates_an_existing_slot_refusal(self):
        def native(root, name, args):
            if args[:2] == ['merge-slot', 'check']:
                return json.dumps({'available': False, 'error': 'not found',
                                   'id': 'newproject-merge-slot'})
            if args[:2] == ['merge-slot', 'create']:
                raise subprocess.CalledProcessError(1, args, stderr='merge slot already exists')
            return ''

        with patch.object(admin, 'run_bd', side_effect=native) as run_bd:
            admin.provision_merge_slot(self.root, 'newproject')
        calls = [c.args[2] for c in run_bd.call_args_list]
        self.assertIn(['merge-slot', 'check', '--json'], calls)
        self.assertIn(['merge-slot', 'create', '--json'], calls)

    def test_provision_merge_slot_reraises_a_real_create_failure(self):
        def native(root, name, args):
            if args[:2] == ['merge-slot', 'check']:
                return json.dumps({'available': False, 'error': 'not found',
                                   'id': 'newproject-merge-slot'})
            raise subprocess.CalledProcessError(1, args, stderr='connection refused')

        with patch.object(admin, 'run_bd', side_effect=native):
            with self.assertRaises(subprocess.CalledProcessError):
                admin.provision_merge_slot(self.root, 'newproject')

    def test_reconcile_request_command_releases_under_lock_and_prints_audit(self):
        self.allow_operator()
        (self.source / '.beads').mkdir()
        (self.source / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        journal = self.source / '.coordination-requests'
        journal.mkdir()
        identity = coordination.content_hash({'request_id': 'alice/child-001'})
        record = {'sha256': 'b' * 64, 'status': 'pending', 'actor': 'alice'}
        (journal / (identity + '.json')).write_text(json.dumps(record), encoding='utf-8')
        argv = ['admin.py', '--root', str(self.root), 'reconcile-request', 'source',
                '--request-id', 'alice/child-001', '--actor', 'operator-1',
                '--reason', 'native validation refused before the fix', '--disposition', 'released']
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'run_bd', return_value='[]') as native, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        self.flock.assert_called_once()
        native.assert_called_once()
        self.assertEqual(native.call_args.args[2][:4], ['list', '--all', '--limit', '0'])
        stored = json.loads((journal / (identity + '.json')).read_text(encoding='utf-8'))
        self.assertEqual(stored['status'], 'released')
        self.assertEqual(stored['reconciliation']['actor'], 'operator-1')
        self.assertEqual(stored['reconciliation']['reason'], 'native validation refused before the fix')
        self.assertIn('operator-1', out.getvalue())

    def test_reconcile_request_command_opens_the_id_with_any_actor_release(self):
        self.allow_operator()
        (self.source / '.beads').mkdir()
        (self.source / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        journal = self.source / '.coordination-requests'
        journal.mkdir()
        identity = coordination.content_hash({'request_id': 'alice/child-001'})
        record = {'sha256': 'b' * 64, 'status': 'pending', 'actor': 'alice'}
        (journal / (identity + '.json')).write_text(json.dumps(record), encoding='utf-8')
        argv = ['admin.py', '--root', str(self.root), 'reconcile-request', 'source',
                '--request-id', 'alice/child-001', '--actor', 'operator-1',
                '--reason', 'handover to bob', '--disposition', 'released', '--any-actor']
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'run_bd', return_value='[]'), contextlib.redirect_stdout(io.StringIO()):
            admin.main()
        stored = json.loads((journal / (identity + '.json')).read_text(encoding='utf-8'))
        self.assertEqual(stored['status'], 'released')
        self.assertEqual(stored['actor'], 'alice')
        self.assertTrue(stored['reconciliation']['any_actor'])

    def test_reconcile_request_command_completes_from_a_labelled_native_issue(self):
        self.allow_operator()
        (self.source / '.beads').mkdir()
        (self.source / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        journal = self.source / '.coordination-requests'
        journal.mkdir()
        identity = coordination.content_hash({'request_id': 'alice/child-001'})
        record = {'sha256': 'b' * 64, 'status': 'pending', 'actor': 'alice'}
        (journal / (identity + '.json')).write_text(json.dumps(record), encoding='utf-8')
        issue = json.dumps([{'id': 'sample-job.7', 'title': 'Child', 'created_by': 'alice',
                             'parent': 'sample-job',
                             'labels': ['request:' + identity, 'request-content:' + 'b' * 64]}])
        argv = ['admin.py', '--root', str(self.root), 'reconcile-request', 'source',
                '--request-id', 'alice/child-001', '--actor', 'operator-1',
                '--reason', 'issue exists, original content unknown', '--disposition', 'complete',
                '--issue-id', 'sample-job.7']
        with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'run_bd', return_value=issue), contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        stored = json.loads((journal / (identity + '.json')).read_text(encoding='utf-8'))
        self.assertEqual(stored['status'], 'complete')
        self.assertEqual(stored['id'], 'sample-job.7')
        self.assertEqual(stored['reconciliation']['disposition'], 'complete')
        self.assertEqual(stored['reconciliation']['completed_from'], 'native')
        self.assertEqual(stored['reconciliation']['issue']['creator'], 'alice')
        self.assertIn('sample-job.7', out.getvalue())


class RequirementJournalBackupTests(unittest.TestCase):
    """The requirement journals ride the native backup/restore sidecar.

    kittrial-pth.26 item 3: the F3 acceptance evidence and the pending/released
    operation IDs live in `.requirement-requests`/`.requirement-backfills`, so a
    backup that omitted them lost the operator's acceptance on restore.
    """

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
        self.request_name = '.requirement-requests/' + 'a' * 64 + '.json'
        self.backfill_name = '.requirement-backfills/' + 'b' * 64 + '.json'
        self.receipt = {'sha256': 'c' * 64, 'status': 'complete', 'actor': 'operator',
                        'id': 'source-1.1', 'operation': 'revise', 'revision': 2,
                        'acceptance': {'owners': ['owner-a'], 'approvers': ['owner-a'],
                                       'policy': 'any-owner', 'decision_id': 'dec-1',
                                       'evidence': 'review-1', 'record_sha256': 'd' * 64}}
        self.backfill = {'sha256': 'e' * 64, 'status': 'complete', 'actor': 'operator',
                         'records': ['source-1.1'],
                         'evidence': {'source-1.1': 'decision-bf'}}
        self.flock = Mock()
        # As above: the creation lock ``add-project`` now takes uses the same surface.
        self.fake_fcntl = types.SimpleNamespace(flock=self.flock, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        self.patcher = patch.dict(sys.modules, {'fcntl': self.fake_fcntl})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def write_journals(self):
        for name, record in ((self.request_name, self.receipt),
                             (self.backfill_name, self.backfill)):
            path = self.source / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(record), encoding='utf-8')

    def test_requirement_journals_round_trip_through_backup_and_restore(self):
        self.write_journals()
        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'source')
        files = json.loads(self.bundle.read_text(encoding='utf-8'))['files']
        self.assertEqual(files[self.request_name], self.receipt)
        self.assertEqual(files[self.backfill_name], self.backfill)
        admin.restore_coordination(self.root, 'source', 'destination')
        self.assertEqual(json.loads((self.destination / self.request_name).read_text()),
                         self.receipt)
        self.assertEqual(json.loads((self.destination / self.backfill_name).read_text()),
                         self.backfill)

    def test_malformed_requirement_receipt_is_refused_before_native_sync(self):
        self.write_journals()
        (self.source / self.request_name).write_text(
            json.dumps({'status': 'complete', 'actor': 'operator'}), encoding='utf-8')
        with patch.object(admin, 'run_bd') as native:
            with self.assertRaises(ValueError):
                admin.backup_project(self.root, 'source')
        native.assert_not_called()
        self.assertEqual(json.loads(self.bundle.read_text(encoding='utf-8'))['status'],
                         'pending')

    def test_restore_validates_requirement_receipts_before_any_write(self):
        for name, record in ((self.request_name,
                              {'sha256': 'nothex', 'status': 'complete'}),
                             (self.backfill_name,
                              {'sha256': 'e' * 64, 'status': 'complete',
                               'actor': 'operator', 'records': []})):
            with self.subTest(name=name):
                with patch.object(admin, 'coordination_backup',
                                  return_value={name: record}):
                    with self.assertRaises(ValueError):
                        admin.restore_coordination(self.root, 'source', 'destination')
                self.assertFalse(list(self.destination.iterdir()))

    def test_requirement_receipt_paths_must_stay_in_their_journal(self):
        for name in ('.requirement-requests/../../escape.json',
                     '.requirement-requests/notahash.json',
                     '.requirement-backfills/' + 'b' * 63 + '.json'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    admin.validate_coordination_files({name: self.receipt})


if __name__ == '__main__':
    unittest.main()
