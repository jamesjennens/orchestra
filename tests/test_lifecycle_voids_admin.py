"""Host gate, closed backup generation and interrupted restore behavior."""
import contextlib
import copy
import json
import os
from pathlib import Path
import unittest
from unittest import mock

import admin
import lifecycle_voids as voids
from requirements import canonical_bytes, content_hash
import test_lifecycle_voids as core


class VoidAdministrationTests(unittest.TestCase):
    setUp = core.HostCorrectionTests.setUp
    payload = core.HostCorrectionTests.payload
    native_run = core.HostCorrectionTests.native_run
    apply = core.HostCorrectionTests.apply
    reading = core.HostCorrectionTests.reading

    def config(self, **values):
        document = dict(operators=['op'], **values)
        (self.project / 'deployment.private.json').write_text(json.dumps(document))
        return document

    def switch(self, action, actor='op'):
        with mock.patch.object(admin, 'deployment_config_lock', return_value=contextlib.nullcontext()):
            return admin.lifecycle_void_switch(self.project, action, actor)

    def test_flag_absence_and_status_are_off_with_identical_config_bytes(self):
        self.config()
        before = (self.project / 'deployment.private.json').read_bytes()
        self.assertFalse(admin.lifecycle_void_writes(self.project))
        status = self.switch('status')
        self.assertFalse(status['changed']); self.assertIsNone(status['audit_agrees'])
        self.assertEqual(before, (self.project / 'deployment.private.json').read_bytes())
        self.assertFalse((self.project / voids.DIRECTORY).exists())

    def test_flag_flips_preserve_other_configuration_and_audit_only_changes(self):
        self.config(unrelated={'kept': True})
        self.assertTrue(self.switch('on')['changed'])
        self.assertFalse(self.switch('on')['changed'])
        status = self.switch('off')
        self.assertTrue(status['changed']); self.assertTrue(status['audit_agrees'])
        self.assertEqual(len(status['audit_history']), 2)
        self.assertEqual([entry['enabled'] for entry in status['audit_history']], [True, False])
        self.assertEqual(admin.config(self.project)['unrelated'], {'kept': True})

    def test_gate_rejects_unlisted_actor_and_damaged_audit_without_writing(self):
        self.config(lifecycle_void_writes_audit=[{'bogus': True}])
        before = (self.project / 'deployment.private.json').read_bytes()
        for actor in ('worker', 'op'):
            with self.subTest(actor=actor), self.assertRaises(ValueError):
                self.switch('on', actor)
        self.assertEqual(before, (self.project / 'deployment.private.json').read_bytes())

    def test_damaged_flag_is_unknown_then_explicit_off_repairs_and_retains_it(self):
        self.config(lifecycle_void_writes='not a boolean')
        with self.assertRaises(ValueError):
            admin.lifecycle_void_writes(self.project)
        self.assertIsNone(self.switch('status')['lifecycle_void_writes'])
        with self.assertRaises(ValueError):
            self.switch('on')
        self.assertTrue(self.switch('off')['changed'])
        cfg = admin.config(self.project)
        self.assertFalse(cfg['lifecycle_void_writes'])
        self.assertEqual([v for k,v in cfg.items() if k.startswith('lifecycle_void_writes_damaged_')], ['not a boolean'])

    def test_empty_absent_family_preserves_legacy_backup_and_read_bytes(self):
        self.assertEqual(voids.backup_files(self.project, self.rows), {})
        self.assertFalse(voids.restore_files(self.project, {}, self.rows))
        self.assertEqual(list(self.project.iterdir()), [])

    def test_family_manifest_round_trip_preserves_durable_void_and_retraction(self):
        self.apply(self.payload())
        files = voids.backup_files(self.project, self.rows)
        old_native = copy.deepcopy(self.rows)
        self.assertEqual(files[voids.MANIFEST]['count'], 1)
        self.apply(self.payload('retract', 'retract-one', 'void-one'))
        complete = voids.backup_files(self.project, self.rows)
        destination = self.project / 'restored'; destination.mkdir()
        self.assertTrue(voids.restore_files(destination, complete, self.rows))
        self.assertEqual(voids.backup_files(destination, self.rows), complete)
        self.assertEqual(voids.context(self.rows, voids.read_journal(destination))['reasons'], {})
        # Restoring the earlier generation removes later retraction files; its
        # effective value agrees with the earlier native snapshot's named pair.
        with self.assertRaisesRegex(ValueError, 'native correction comment generation'):
            voids.restore_files(destination, files, self.rows)
        self.assertTrue(voids.context(self.rows, voids.read_journal(destination))['global_reason'])
        self.assertTrue(voids.restore_files(destination, files, old_native))
        self.assertEqual(voids.context(old_native, voids.read_journal(destination))['reasons'], {'p-task.2':'voided'})
        self.assertFalse((destination / voids.RESTORE_MARKER).exists())

    def test_manifest_hash_count_path_and_graph_damage_refuse_before_restore_writes(self):
        self.apply(self.payload())
        files = voids.backup_files(self.project, self.rows)
        corruptions = []
        for field, value in (('count', 2), ('bytes', 0), ('files', {})):
            broken = copy.deepcopy(files); broken[voids.MANIFEST][field] = value; corruptions.append(broken)
        broken = copy.deepcopy(files); broken.pop(voids.MANIFEST); corruptions.append(broken)
        broken = copy.deepcopy(files); name = next(n for n in broken if n != voids.MANIFEST)
        broken[name]['payload']['actor'] = 'forged'; corruptions.append(broken)
        destination = self.project / 'empty'; destination.mkdir()
        for broken in corruptions:
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                voids.restore_files(destination, broken, self.rows)
            self.assertEqual(list(destination.iterdir()), [])

    def test_native_generation_mismatch_keeps_restore_marker_and_reads_unknown(self):
        self.apply(self.payload())
        files = voids.backup_files(self.project, self.rows)
        destination = self.project / 'mismatch'; destination.mkdir()
        changed = copy.deepcopy(self.rows); changed[2]['created_by'] = 'forged'
        with self.assertRaisesRegex(ValueError, 'native target/comment generation'):
            voids.restore_files(destination, files, changed)
        state = voids.context(changed, voids.read_journal(destination))
        self.assertTrue(state['global_reason'])
        self.assertTrue(voids.restore_files(destination, files, self.rows))
        self.assertEqual(voids.context(self.rows, voids.read_journal(destination))['reasons'], {'p-task.2':'voided'})

    def test_hard_cut_before_first_member_cannot_expose_old_pass_and_exact_retry_repairs(self):
        import coordination
        self.apply(self.payload())
        files = voids.backup_files(self.project, self.rows)
        destination = self.project / 'cut'; destination.mkdir()
        real_atomic = coordination.atomic
        def cut(path, value):
            if Path(path).parent.name == voids.DIRECTORY:
                raise OSError('synthetic restore cut')
            return real_atomic(path, value)
        with mock.patch.object(coordination, 'atomic', side_effect=cut), self.assertRaises(OSError):
            voids.restore_files(destination, files, self.rows)
        self.assertTrue(voids.context(self.rows, voids.read_journal(destination))['global_reason'])
        with self.assertRaises(ValueError):
            voids.backup_files(destination, self.rows)
        self.assertTrue(voids.restore_files(destination, files, self.rows))
        self.assertFalse((destination / voids.RESTORE_MARKER).exists())

    def test_legacy_restore_refuses_to_drop_existing_void_or_incomplete_marker(self):
        self.apply(self.payload())
        before = voids.backup_files(self.project, self.rows)
        with self.assertRaisesRegex(ValueError, 'Legacy backup'):
            voids.restore_files(self.project, {}, self.rows)
        self.assertEqual(before, voids.backup_files(self.project, self.rows))

    def test_backup_coordination_whitelist_accepts_only_complete_closed_void_family(self):
        self.apply(self.payload())
        files = voids.backup_files(self.project, self.rows)
        admin.validate_coordination_files(files)
        malformed = copy.deepcopy(files); malformed[voids.RESTORE_MARKER] = {}
        with self.assertRaisesRegex(ValueError, 'Invalid coordination backup path'):
            admin.validate_coordination_files(malformed)

    @unittest.skipIf(os.name == 'nt', 'host restore coordination lock uses POSIX flock')
    def test_admin_coordination_restore_invokes_native_bound_family_generation(self):
        self.apply(self.payload())
        files = voids.backup_files(self.project, self.rows)
        backups = self.project / 'backups'; backups.mkdir()
        (backups / 'pp.coordination.json').write_text(json.dumps(dict(
            schema_version=1, status='complete', files=files, operators=['old-op'], verifiers=[])))
        destination = self.project / 'rr'; destination.mkdir()
        exported = '\n'.join(json.dumps(row) for row in self.rows)
        with mock.patch.object(admin, 'project_dir', return_value=destination), \
                mock.patch.object(admin, 'run_bd', return_value=exported) as native, \
                mock.patch.object(admin, 'report_native_backup_change'):
            self.assertTrue(admin.restore_coordination(self.project, 'pp', 'rr', authority=False))
        native.assert_called_once_with(self.project, 'rr', ['export', '--all'])
        self.assertEqual(voids.backup_files(destination, self.rows), files)
        self.assertEqual(voids.context(self.rows, voids.read_journal(destination))['reasons'], {'p-task.2':'voided'})
        self.assertFalse((self.project / 'deployment.private.json').exists())

    def test_web_queue_preserves_conditional_void_reasons_and_unchanged_normal_shape(self):
        from http_service import own_queue_tasks
        row = dict(id='p-task', title='synthetic', status='open', assignee='worker', review_state='awaiting-integration')
        queue = dict(items=[row], complete=True)
        before = own_queue_tasks(queue, 'worker')
        self.assertNotIn('lifecycle_reasons', before['tasks'][0])
        row['lifecycle_reasons'] = {'integrated':'voided'}
        affected = own_queue_tasks(queue, 'worker')
        self.assertEqual(affected['tasks'][0]['lifecycle_reasons'], {'integrated':'voided'})
        affected['tasks'][0].pop('lifecycle_reasons')
        self.assertEqual(before, affected)

    def test_entry_bound_includes_the_actual_atomic_json_separator_bytes(self):
        self.apply(self.payload())
        entry = next(iter(voids.read_journal(self.project)['entries'].values()))
        compact = len(canonical_bytes(entry))
        self.assertGreater(len(voids.entry_bytes(entry)), compact)
        with mock.patch.object(voids, 'MAX_RECORD_BYTES', compact), self.assertRaisesRegex(ValueError, 'byte bound'):
            voids.validate_entry(entry)

    def test_known_nonregular_publication_temp_refuses_before_native_effect(self):
        payload = self.payload()
        directory = self.project / voids.DIRECTORY; directory.mkdir()
        temporary = directory / (content_hash(payload) + '.tmp'); temporary.mkdir()
        before = copy.deepcopy(self.rows)
        with self.assertRaisesRegex(ValueError, 'no native write attempted'):
            self.apply(payload)
        self.assertEqual(self.rows, before); self.assertEqual(self.native_writes, 0)
        self.assertTrue(temporary.is_dir())


if __name__ == '__main__':
    unittest.main()
