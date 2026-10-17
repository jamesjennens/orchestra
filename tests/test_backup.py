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
        # These cases isolate coordination sidecars from the tracker. The
        # native VOID pair is exercised with a real tracker in native tests.
        for name,value in [('backup_lifecycle_void_family',{}),('restore_lifecycle_void_family',False)]:
            patcher=patch.object(admin,name,return_value=value);patcher.start();self.addCleanup(patcher.stop)

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

    def test_owner_requirement_history_governance_receipts_and_request_journal_restore(self):
        import requirement_governance as gov
        import requirement_http as owner_http
        import requirement_owner_records as owner_records
        import requirement_records as records
        from http_authority import OperationJournal, journal_path
        from test_requirement_owner_records import OwnerNative, ACCOUNT
        native = OwnerNative(); native.actor = ACCOUNT
        native.row('job-1')['issue_type'] = 'epic'
        gov.initialize(self.source, 'source', 'host', 'new-project-source')
        context = owner_http.OwnerContext('source', ACCOUNT, gov.current(self.source, 'source'))
        draft = owner_http.apply(self.source, context, 'create', {
            'kind': 'requirement', 'parent': 'job-1', 'title': 'Durable content',
            'description': 'Retain this exact history.'}, 'restore-create', native)
        accepted = owner_http.apply(self.source, context, 'accept', {
            'expected_revision': draft['revision'], 'expected_sha256': draft['sha256']},
            'restore-accept', native, draft['id'])
        row = native.row(draft['id'])
        original = json.dumps(row, sort_keys=True)
        governance_bytes = (self.source/gov.FILE).read_bytes()
        receipt_bytes = {p.name: p.read_bytes() for p in (self.source/owner_http.JOURNAL).glob('*.json')}
        journal = OperationJournal(journal_path(self.source))
        journal.reserve('restore-accept', 'a'*64, ACCOUNT)
        journal.complete('restore-accept', {'returncode': 0, 'stdout': json.dumps(accepted),
                                         'stderr': '', 'server_time': '2030-01-01T00:00:00+00:00'},
                         'a'*64, ACCOUNT)
        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'source')
        admin.restore_coordination(self.root, 'source', 'destination')
        self.assertEqual((self.destination/gov.FILE).read_bytes(), governance_bytes)
        admin.restore_journal(admin.journal_snapshot_path(self.root, 'source'),
                              journal_path(self.destination))
        self.assertEqual({p.name: p.read_bytes() for p in (self.destination/owner_http.JOURNAL).glob('*.json')},
                         receipt_bytes)
        view = owner_http.read(self.destination, 'destination', ['get', draft['id']], native)
        self.assertEqual(view['current']['sha256'], accepted['sha256'])
        self.assertEqual(json.dumps(row, sort_keys=True), original)
        self.assertEqual(owner_records.existing_acceptances(row)[2]['project'], 'source')
        self.assertTrue(records.resolved_acceptance(row, records.existing_revisions(row)[2]))
        restored = OperationJournal(journal_path(self.destination)).lookup('restore-accept')
        self.assertEqual(restored['state'], 'committed')
        self.assertEqual(restored['envelope']['stdout'], json.dumps(accepted))

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
        create.assert_called_once_with(self.root, 'destination', requirements_default=False)
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

    def restore_with_a_failing_provisioning(self, slot_answer, empty_destination=False, sidecar_restored=True,
                                            journal_restored=False, argv_extra=(), **bundle):
        """``restore-new`` whose merge-slot provisioning step fails; returns (order, notice).

        The step itself is what fails (``provision_merge_slot`` is patched to raise), so the
        order the other steps ran in can be read from ``order`` and the notice's claims can be
        checked against what the clone's own ``merge-slot check`` answers (``slot_answer``,
        fed to the read ``restore_failure_notice`` makes through ``project_merge_slot_state``).
        ``empty_destination`` makes ``restore_destination_state``'s read answer the destination's
        own merge slot alone: its "empty, working project" shape, which a clone from an empty
        slotless source has. ``sidecar_restored`` and ``journal_restored`` say what the two steps
        before the failed one did; ``bundle`` is written into the sidecar as it stands
        (``operators``/``verifiers`` make it record names this host does not list, ``status``
        makes it unusable), and ``argv_extra`` adds restore-new flags.
        """
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'operators': ['operator']}), encoding='utf-8')
        self.save_bundle(**bundle)
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
                # ROWS, not one JSON object: ``restore_destination_state`` reads a JSON list, so a
                # JSON object made the destination "partial" and the empty-destination test never
                # reached the "empty, working project" shape it names (kittrial-5bb.240 F2).
                return json.dumps([{'id': 'destination-merge-slot'}]) if empty_destination else 'not rows'
            return 'restored'

        def sidecar(*args, **kwargs):
            order.append('sidecar')
            return sidecar_restored

        def journal(*args, **kwargs):
            order.append('journal')
            return {'operation_ids': ['op-restore-1']} if journal_restored else None

        def provision(*args, **kwargs):
            order.append('provision')
            raise admin.subprocess.CalledProcessError(1, 'bd', stderr='create refused')

        argv = ['admin.py', '--root', str(self.root), 'restore-new', 'source', 'destination', *argv_extra]
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
        self.assertIn('its data and its re-pointed backup target are in place', notice)
        self.assertIn('its operation journal starts empty because the backup carried no journal snapshot', notice)
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
        self.assertIn('its data and its re-pointed backup target are in place', notice)
        self.assertIn('its operation journal starts empty because the backup carried no journal snapshot', notice)
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
        self.assertIn('its data and its re-pointed backup target are in place', notice)
        self.assertIn('its operation journal starts empty because the backup carried no journal snapshot', notice)
        self.assertNotIn('nothing was restored', notice)
        self.assertNotIn('NOT restored', notice)

    def test_a_failed_provisioning_that_cannot_read_the_slot_does_not_guess(self):
        """The same fault that stopped the step can stop the re-read: the notice says so plainly
        rather than claiming the slot is missing (F1)."""
        _, notice = self.restore_with_a_failing_provisioning('not json at all')
        self.assertIn('Whether it has a usable merge slot could not be read', notice)
        self.assertIn('its data and its re-pointed backup target are in place', notice)
        self.assertIn('its operation journal starts empty because the backup carried no journal snapshot', notice)
        self.assertNotIn('Its merge slot is still missing', notice)
        self.assertNotIn('was not provisioned', notice)
        self.assertNotIn('its backup fails', notice)

    def test_the_provisioning_notice_says_the_authority_step_did_not_run(self):
        """kittrial-5bb.240 F1: ``restore_authority`` runs LAST, so a failed provisioning step
        skipped it without a word - the "NOT restored: ..." lines a complete restore prints were
        never printed and the two restore flags re-granted nothing. The notice says so, names what
        the backup records, and gives the comparison and the commands. It never re-grants."""
        _, notice = self.restore_with_a_failing_provisioning(
            json.dumps({'available': False, 'error': 'not found', 'id': 'destination-merge-slot'}),
            operators=['alice'], verifiers=['carol'])
        self.assertIn('the operators and verifiers the backup records were NOT compared', notice)
        self.assertIn('NOT re-granted', notice)
        self.assertIn('--restore-operators/--restore-verifiers re-granted nothing', notice)
        self.assertIn('operator allowlist entries this host does not list: alice', notice)
        self.assertIn('capability verifiers this host does not list: carol', notice)
        self.assertIn('backup-authority source', notice)
        self.assertIn('operators add alice', notice)
        self.assertIn('verifiers add carol', notice)
        self.assertIn('--actor OPERATOR --reason TEXT', notice)      # the placeholder an add refuses
        self.assertIn('Re-run restore-new into another unused destination name', notice)

    def test_the_notice_is_silent_about_authority_when_no_recorded_name_is_missing(self):
        """The skipped step would have printed nothing and re-granted nothing: the notice must not
        claim it failed to compare names this host already lists (kittrial-5bb.240 F1)."""
        _, notice = self.restore_with_a_failing_provisioning(
            json.dumps({'available': False, 'error': 'not found', 'id': 'destination-merge-slot'}),
            operators=['operator'])
        self.assertIn('the merge-slot provisioning step failed', notice)
        self.assertNotIn('NOT compared', notice)
        self.assertNotIn('were NOT re-granted', notice)
        self.assertNotIn('backup-authority', notice)

    def test_a_backup_with_no_sidecar_says_what_that_means_for_authority(self):
        """A legacy backup records no deployment authority, and the note that says so is printed
        after the step that failed: the notice carries its own true sentence (kittrial-5bb.240 F1)."""
        _, notice = self.restore_with_a_failing_provisioning(
            json.dumps({'available': False, 'error': 'not found', 'id': 'destination-merge-slot'}),
            sidecar_restored=False)
        self.assertIn('This backup has no coordination sidecar, so it records no operators or verifiers: nothing '
                      'was compared and nothing was re-granted.', notice)

    def test_a_without_coordination_restore_says_nothing_was_compared(self):
        """``--without-coordination`` restores no sidecar on purpose, and its closing notes also
        come after the step that failed: it gets its own true sentence (kittrial-5bb.240 F1)."""
        _, notice = self.restore_with_a_failing_provisioning(
            json.dumps({'available': False, 'error': 'not found', 'id': 'destination-merge-slot'}),
            argv_extra=['--without-coordination'], status='pending')
        self.assertIn('This was a --without-coordination restore, so no coordination sidecar and no deployment '
                      'authority were restored, and none was compared or re-granted', notice)
        self.assertNotIn('backup-authority', notice)

    def test_the_notice_claims_the_journals_only_when_one_was_restored(self):
        """kittrial-5bb.240 F2: "its journals are in place" was said when the backup had no journal
        snapshot to restore; with one restored the claim is true and kept."""
        _, notice = self.restore_with_a_failing_provisioning(
            json.dumps({'available': False, 'error': 'not found', 'id': 'destination-merge-slot'}),
            journal_restored=True)
        self.assertIn('its data, its re-pointed backup target and its journals are in place', notice)
        self.assertNotIn('starts empty', notice)

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
        def create(*args, **kwargs): locked('create-destination')
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
        for name,value in [('backup_lifecycle_void_family',{}),('restore_lifecycle_void_family',False)]:
            patcher=patch.object(admin,name,return_value=value);patcher.start();self.addCleanup(patcher.stop)
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


class HttpStateBackupTests(unittest.TestCase):
    """kittrial-5bb.331: the web-service state joins every backup, 0600, consistent."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'backups').mkdir()

    def state(self, with_records=True):
        live = self.root / 'http-state.json'
        live.write_text(json.dumps({'schema_version': 1, 'users': [{'id': 'usr_x',
                                    'password': 'hash-of-x'}]}), encoding='utf-8')
        if with_records:
            import sqlite3
            records = Path(str(live) + '.records.sqlite3')
            connection = sqlite3.connect(str(records))
            connection.execute('CREATE TABLE sessions (token TEXT, user TEXT)')
            connection.execute("INSERT INTO sessions VALUES ('tok-1', 'usr_x')")
            connection.commit()
            connection.close()
        return live

    def entry(self, **more):
        return dict({'status': 'complete', 'path': '/x', 'files': [{'name': 'a'}]}, **more)

    def test_the_path_resolves_flag_deployment_default_then_none(self):
        self.assertEqual(admin.resolve_http_state_path(self.root, '/flag/state.json'),
                         (Path('/flag/state.json'), 'flag'))
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'http_state': str(self.root / 'configured.json')}),
            encoding='utf-8')
        self.assertEqual(admin.resolve_http_state_path(self.root),
                         (Path(str(self.root / 'configured.json')), 'deployment'))
        (self.root / 'deployment.private.json').unlink()
        self.state(with_records=False)
        self.assertEqual(admin.resolve_http_state_path(self.root),
                         (self.root / 'http-state.json', 'default'))
        other = Path(self.temp.name)  # same root; remove the file to answer none
        (self.root / 'http-state.json').unlink()
        self.assertEqual(admin.resolve_http_state_path(self.root), (None, None))

    def test_a_present_state_is_copied_0600_and_the_records_query(self):
        live = self.state()
        entry = admin.backup_http_state(self.root)
        self.assertEqual(entry['status'], 'complete')
        self.assertEqual(entry['path'], str(live))
        copied_json = self.root / 'backups' / admin.HTTP_STATE_JSON
        copied_records = self.root / 'backups' / admin.HTTP_STATE_RECORDS
        self.assertEqual(copied_json.read_text(encoding='utf-8'), live.read_text(encoding='utf-8'))
        # kittrial-5bb.331 review item 'posix-mode-asserts': Windows has no POSIX
        # modes (0600 reads as 0666 there); the mode assertions run on POSIX only,
        # everything else in this test runs everywhere.
        if os.name != 'nt':
            self.assertEqual(oct(copied_json.stat().st_mode & 0o777), '0o600')
            self.assertEqual(oct(copied_records.stat().st_mode & 0o777), '0o600')
        # The reader is closed (review item 'windows-sqlite-handle'): an open handle
        # locks the file on Windows and fails the fixture clean-up.
        import sqlite3
        reader = sqlite3.connect(str(copied_records))
        try:
            rows = reader.execute('SELECT token, user FROM sessions').fetchall()
        finally:
            reader.close()
        self.assertEqual(rows, [('tok-1', 'usr_x')])

    def test_an_absent_state_records_absent(self):
        entry = admin.backup_http_state(self.root)
        self.assertEqual(entry['status'], 'absent')

    def test_a_running_service_still_gets_a_committed_snapshot(self):
        import sqlite3
        live = self.state()
        records = Path(str(live) + '.records.sqlite3')
        writer = sqlite3.connect(str(records))     # the "running service" connection
        try:
            writer.execute("INSERT INTO sessions VALUES ('tok-2', 'usr_y')")
            writer.commit()
            entry = admin.backup_http_state(self.root)   # copied while the writer is open
        finally:
            writer.close()
        self.assertEqual(entry['status'], 'complete')
        # Closed explicitly (review item 'windows-sqlite-handle'): an open handle
        # locks the copy on Windows and fails the fixture clean-up with WinError 32.
        copied = sqlite3.connect(str(self.root / 'backups' / admin.HTTP_STATE_RECORDS))
        try:
            tokens = sorted(row[0] for row in copied.execute('SELECT token FROM sessions'))
            self.assertEqual(tokens, ['tok-1', 'tok-2'])
            self.assertTrue(copied.execute('PRAGMA integrity_check').fetchone()[0])
        finally:
            copied.close()

    def test_a_degraded_copy_makes_the_whole_run_incomplete(self):
        degraded = {'status': 'degraded', 'reason': 'no consistent SQLite copy'}
        record = admin.backup_status_record(
            [{'name': 'example', 'status': 'complete', 'completed_at': '2026-10-17T00:00:00Z'}],
            'all', '2026-10-17T00:00:00Z', http_state=degraded)
        self.assertEqual(record['status'], 'incomplete')
        admin.validate_backup_status(record)          # the entry validates
        complete = admin.backup_status_record(
            [{'name': 'example', 'status': 'complete', 'completed_at': '2026-10-17T00:00:00Z'}],
            'all', '2026-10-17T00:00:00Z',
            http_state={'status': 'complete', 'path': '/x',
                        'files': [{'name': admin.HTTP_STATE_JSON, 'bytes': 10}]})
        self.assertEqual(complete['status'], 'complete')
        admin.validate_backup_status(complete)
        absent = admin.backup_status_record(
            [{'name': 'example', 'status': 'complete', 'completed_at': '2026-10-17T00:00:00Z'}],
            'all', '2026-10-17T00:00:00Z', http_state={'status': 'absent'})
        self.assertEqual(absent['status'], 'complete')
        admin.validate_backup_status(absent)

    def test_restore_returns_the_pair_0600_and_names_the_invalidation(self):
        live = self.state()
        admin.backup_http_state(self.root)
        live.unlink()
        Path(str(live) + '.records.sqlite3').unlink()
        sentence = admin.restore_http_state(self.root)
        self.assertIn('INVALIDATED', sentence)
        self.assertEqual(live.read_text(encoding='utf-8'),
                         (self.root / 'backups' / admin.HTTP_STATE_JSON).read_text(encoding='utf-8'))
        # POSIX-only mode assertions (review item 'posix-mode-asserts').
        if os.name != 'nt':
            self.assertEqual(oct(live.stat().st_mode & 0o777), '0o600')
            self.assertEqual(oct(Path(str(live) + '.records.sqlite3').stat().st_mode & 0o777), '0o600')

    def test_restore_refuses_without_a_pair_and_targets_the_default(self):
        with self.assertRaisesRegex(ValueError, 'no HTTP state pair'):
            admin.restore_http_state(self.root)
        # Back up the default-path state FIRST (no deployment config yet) ...
        self.state()                                 # the live files exist at the default
        admin.backup_http_state(self.root)            # the pair lands in backups/
        (self.root / 'http-state.json').unlink()      # the disaster: the live file is gone
        Path(str(self.root / 'http-state.json') + '.records.sqlite3').unlink()
        # ... then configure a different live path: the restore targets IT.
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'http_state': str(self.root / 'configured.json')}),
            encoding='utf-8')
        sentence = admin.restore_http_state(self.root)
        self.assertIn('INVALIDATED', sentence)
        self.assertTrue((self.root / 'configured.json').is_file())
        self.assertTrue(Path(str(self.root / 'configured.json') + '.records.sqlite3').is_file())

    def test_restore_new_touches_the_state_only_through_the_flag(self):
        # Source pin: the dispatch calls restore_http_state only under the flag, and
        # the no-flag path prints the one-line refusal that names it.
        source = (Path(admin.__file__).read_text(encoding='utf-8'))
        start = source.index("elif args.command=='restore-new'")
        body = source[start:]
        # the block runs to the next elif at the same indent, or to the end of main
        nxt = body.find('\n    elif ')
        if nxt != -1:
            body = body[:nxt]
        self.assertIn("if getattr(args,'restore_http_state',False):", body)
        self.assertIn('print(restore_http_state(root))', body)
        self.assertIn('was left ', body)
        self.assertEqual(body.count('restore_http_state(root)'), 1)
