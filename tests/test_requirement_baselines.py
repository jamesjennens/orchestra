"""Immutable installation, interruption recovery and restored source identities."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requirement_baseline_records as schema
import requirement_baselines as store
import requirement_governance as governance
import requirement_http as http
from requirements import content_hash
from test_requirement_baseline_records import CompleteNative
from test_requirement_owner_records import ACCOUNT


class StoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.parent = Path(temp.name)
        self.path = self.parent / 'alpha'; self.path.mkdir()
        governance.initialize(self.path, 'alpha', ACCOUNT, 'provision-alpha')
        self.context = http.OwnerContext('alpha', ACCOUNT, governance.current(self.path, 'alpha'))
        self.native = CompleteNative()
        self.created = http.apply(self.path, self.context, 'create', dict(kind='requirement', parent='job-1',
            title='A requirement', description='Original wording'), 'create-one', self.native)

    def cut(self, name='Review-1', operation='cut-one', at='2099-01-01T00:00:00Z'):
        return store.cut(self.path, self.context, name, operation, self.native, at)

    def frozen_bytes(self, name='review-1'):
        directory = self.path / store.DIRECTORY / name
        return {filename: (directory / filename).read_bytes() for filename in store.FILES}

    def no_native(self, args):
        self.fail('A saved cut replay must not recapture native state: ' + repr(args))

    def test_installation_and_exact_replay_preserve_every_frozen_byte_and_time(self):
        result = self.cut(); frozen = self.frozen_bytes()
        self.assertTrue(result['created'])
        artifact = store.get(self.path, 'alpha', 'REVIEW-1')
        self.assertEqual(artifact['receipt_sha256'], result['receipt_sha256'])
        answer = store.cut(self.path, self.context, 'review-1', 'cut-one', self.no_native, '1999-01-01T00:00:00Z')
        self.assertFalse(answer['created'])
        self.assertEqual(frozen, self.frozen_bytes())
        self.assertEqual(store.current(self.path, 'alpha')['name'], 'review-1')
        self.assertEqual(store.list_baselines(self.path, 'alpha')['total'], 1)

    def test_replaying_old_cut_never_rewinds_current_and_clocks_do_not_order(self):
        first = self.cut()
        self.cut('second', 'cut-two', '1999-01-01T00:00:00Z')
        self.assertEqual(store.current(self.path, 'alpha')['name'], 'second')
        replay = store.cut(self.path, self.context, 'review-1', 'cut-one', self.no_native, '2100-01-01T00:00:00Z')
        self.assertEqual(first['receipt_sha256'], replay['receipt_sha256'])
        self.assertEqual(store.current(self.path, 'alpha')['name'], 'second')

    def test_case_conflict_and_operation_reuse_do_not_capture_or_overwrite(self):
        self.cut(); frozen = self.frozen_bytes()
        for name, operation in (('REVIEW-1', 'different-operation'), ('different-name', 'cut-one')):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'different cut'):
                store.cut(self.path, self.context, name, operation, self.no_native, '2026-01-01T00:00:00Z')
        self.assertEqual(frozen, self.frozen_bytes())
        self.assertEqual(store.list_baselines(self.path, 'alpha')['total'], 1)

    def test_stage_retry_uses_saved_capture_after_live_source_has_changed(self):
        real = store._write_once
        def interrupt(path, raw):
            if path.name == 'BRD.md':
                raise OSError('injected failure after complete snapshot')
            return real(path, raw)
        with patch.object(store, '_write_once', interrupt), self.assertRaisesRegex(OSError, 'injected'):
            self.cut()
        self.assertIsNone(store.current(self.path, 'alpha'))
        baseline_dir = self.path / store.DIRECTORY
        stage = next(baseline_dir.glob('.pending-*'))
        captured = (stage / 'snapshot.json').read_bytes()
        http.apply(self.path, self.context, 'revise', dict(expected_revision=self.created['revision'],
            expected_sha256=self.created['sha256'], description='Later wording'), 'later-edit', self.native, self.created['id'])
        result = store.cut(self.path, self.context, 'Review-1', 'cut-one', self.no_native, '1999-01-01T00:00:00Z')
        self.assertTrue(result['created'])
        self.assertEqual(self.frozen_bytes()['snapshot.json'], captured)
        self.assertIn(b'Original wording', self.frozen_bytes()['BRD.md'])
        self.assertNotIn(b'Later wording', self.frozen_bytes()['BRD.md'])
        self.assertFalse(stage.exists())

    def test_lost_pointer_write_reconciles_installed_artifacts_without_recapture(self):
        with patch.object(store, '_publish_pointer', side_effect=OSError('injected pointer failure')):
            with self.assertRaisesRegex(OSError, 'injected'):
                self.cut()
        frozen = self.frozen_bytes()
        self.assertIsNone(store.current(self.path, 'alpha'))
        result = store.cut(self.path, self.context, 'review-1', 'cut-one', self.no_native, '1999-01-01T00:00:00Z')
        self.assertFalse(result['created'])
        self.assertEqual(store.current(self.path, 'alpha')['name'], 'review-1')
        self.assertEqual(frozen, self.frozen_bytes())

    def test_partial_capture_is_retained_and_never_replaced_with_today(self):
        with patch.object(store, '_write_once', side_effect=OSError('injected first-file failure')):
            with self.assertRaises(OSError):
                self.cut()
        stage = next((self.path / store.DIRECTORY).glob('.pending-*'))
        (stage / 'snapshot.json').write_bytes(b'{"cut":')
        with self.assertRaisesRegex(ValueError, 'cannot be verified'):
            store.cut(self.path, self.context, 'review-1', 'cut-one', self.no_native, '2026-01-01T00:00:00Z')
        self.assertEqual((stage / 'snapshot.json').read_bytes(), b'{"cut":')
        self.assertIsNone(store.current(self.path, 'alpha'))
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            store.backup_files(self.path, 'alpha')

    def test_stale_or_governed_context_refuses_even_an_identical_replay(self):
        self.cut(); frozen = self.frozen_bytes()
        prior = governance.current(self.path, 'alpha')
        governance.set_mode(self.path, 'alpha', 'governed', ACCOUNT, 'switch-mode', prior['revision'], prior['sha256'])
        with self.assertRaisesRegex(ValueError, 'stale or unavailable'):
            store.cut(self.path, self.context, 'review-1', 'cut-one', self.no_native, '2026-01-01T00:00:00Z')
        self.assertEqual(frozen, self.frozen_bytes())

    def test_tampered_selected_baseline_refuses_healthy_baseline_stays_readable(self):
        self.cut(); self.cut('second', 'cut-two')
        (self.path / store.DIRECTORY / 'second' / 'BRD.md').write_text('rewritten', encoding='utf-8')
        self.assertEqual(store.get(self.path, 'alpha', 'review-1')['snapshot']['name'], 'review-1')
        with self.assertRaisesRegex(ValueError, 'cannot be verified'):
            store.get(self.path, 'alpha', 'second')
        listing = store.list_baselines(self.path, 'alpha')
        self.assertEqual(listing['total'], 2)
        self.assertEqual([item['name'] for item in listing['items'] if not item['readable']], ['second'])
        self.assertIsNone(listing['current'])
        self.assertIsNotNone(listing['current_error'])

    def test_damaged_old_receipt_is_isolated_only_with_proven_unrelated_name(self):
        self.cut(); self.cut('healthy', 'cut-healthy')
        path = self.path / store.DIRECTORY / 'review-1' / 'receipt.json'
        original = schema.decode_snapshot((path.parent / 'snapshot.json').read_bytes())
        receipt = schema.receipt(original)
        receipt['sha256'] = '0' * 64
        path.write_bytes(schema.json_bytes(receipt))
        damaged = self.frozen_bytes()
        self.assertTrue(self.cut('new', 'cut-new')['created'])
        self.assertEqual(damaged, self.frozen_bytes())
        self.assertFalse(store.list_baselines(self.path, 'alpha')['items'][-1]['readable'])
        for broken in (dict(receipt, name='new-target'),
                       {key: value for key, value in receipt.items() if key != 'name'},
                       b'{"name":'):
            with self.subTest(broken=broken):
                path.write_bytes(broken if isinstance(broken, bytes) else schema.json_bytes(broken))
                before = {p.name for p in path.parent.parent.iterdir()}
                with self.assertRaisesRegex(ValueError, 'cannot be verified'):
                    store.cut(self.path, self.context, 'new-target', 'new-target', self.no_native,
                              '2026-01-01T00:00:00Z')
                self.assertEqual(before, {p.name for p in path.parent.parent.iterdir()})

    def _assert_damaged_receipt_cannot_reuse_operation_under_another_name(self, extra_field):
        self.cut(); self.cut('healthy', 'cut-healthy')
        path = self.path / store.DIRECTORY / 'review-1' / 'receipt.json'
        receipt = json.loads(path.read_text(encoding='utf-8'))
        if extra_field:
            receipt['extra'] = 'unexpected'
            receipt['sha256'] = content_hash(receipt)  # valid hash, closed-field failure
        else:
            receipt['at'] = '2098-01-01T00:00:00Z'  # readable identity, stale hash
        path.write_bytes(schema.json_bytes(receipt))
        frozen = self.frozen_bytes()
        pointer = (path.parent.parent / 'current.json').read_bytes()
        calls = len(self.native.calls)
        with self.assertRaisesRegex(ValueError, 'different cut|cannot be verified'):
            self.cut('new', 'cut-one')
        self.assertEqual(len(self.native.calls), calls)
        self.assertFalse((path.parent.parent / 'new').exists())
        self.assertEqual((path.parent.parent / 'current.json').read_bytes(), pointer)
        self.assertEqual(self.frozen_bytes(), frozen)

    def test_same_operation_other_name_refuses_a_receipt_with_hash_damage(self):
        self._assert_damaged_receipt_cannot_reuse_operation_under_another_name(False)

    def test_same_operation_other_name_refuses_a_receipt_with_extra_field(self):
        self._assert_damaged_receipt_cannot_reuse_operation_under_another_name(True)

    def test_damaged_selected_receipt_and_current_pointer_still_refuse(self):
        self.cut(); self.cut('healthy', 'cut-healthy')
        receipt = self.path / store.DIRECTORY / 'review-1' / 'receipt.json'
        receipt.write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'cannot be verified'):
            store.cut(self.path, self.context, 'review-1', 'cut-one', self.no_native,
                      '2026-01-01T00:00:00Z')
        pointer = self.path / store.DIRECTORY / 'current.json'
        pointer.write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'cannot be verified'):
            store.cut(self.path, self.context, 'new', 'new', self.no_native,
                      '2026-01-01T00:00:00Z')

    def test_legacy_multiline_reason_artifact_replay_backup_and_restore_keep_bytes(self):
        from requirement_brd import render
        accepted = http.apply(self.path, self.context, 'accept', dict(
            expected_revision=self.created['revision'], expected_sha256=self.created['sha256']),
            'accept-one', self.native, self.created['id'])
        http.apply(self.path, self.context, 'withdraw', dict(expected_revision=accepted['revision'],
            expected_sha256=accepted['sha256'], expected_state_sha256=None, reason='Old reason\n==='),
            'withdraw-one', self.native, self.created['id'])
        self.cut()
        folder = self.path / store.DIRECTORY / 'review-1'
        snapshot = schema.decode_snapshot((folder / 'snapshot.json').read_bytes())
        legacy = render(snapshot['view'], _legacy_reason=True)
        self.assertIn(b'Reason: Old reason\n===', legacy)
        receipt = schema.receipt(snapshot, markdown=legacy)
        (folder / 'BRD.md').write_bytes(legacy)
        (folder / 'receipt.json').write_bytes(schema.json_bytes(receipt))
        pointer = dict(schema_version=1, name='review-1', receipt_sha256=schema.byte_hash(schema.json_bytes(receipt)))
        (folder.parent / 'current.json').write_bytes(schema.json_bytes(pointer))
        frozen = self.frozen_bytes()
        self.assertEqual(store.get(self.path, 'alpha', 'review-1')['markdown'], legacy)
        store.cut(self.path, self.context, 'review-1', 'cut-one', self.no_native, '2026-01-01T00:00:00Z')
        restored = self.install_sidecar('beta', store.restored_files(store.backup_files(self.path, 'alpha'), 'alpha', 'beta'))
        self.assertEqual(store.get(restored, 'beta', 'review-1')['markdown'], legacy)
        self.assertEqual(frozen, self.frozen_bytes())
        stage = folder.parent / ('.pending-' + content_hash(dict(project='alpha', account_id=ACCOUNT,
                                                               operation_id='cut-one')))
        (folder.parent / 'current.json').unlink()
        folder.rename(stage)
        store.cut(self.path, self.context, 'review-1', 'cut-one', self.no_native, '2026-01-01T00:00:00Z')
        self.assertFalse(stage.exists())
        self.assertEqual(frozen, self.frozen_bytes())
        (folder / 'BRD.md').write_bytes(render(snapshot['view']))
        with self.assertRaisesRegex(ValueError, 'cannot be verified'):
            store.get(self.path, 'alpha', 'review-1')

    def test_sixty_four_baselines_is_the_actual_boundary_and_refusal_has_no_effect(self):
        self.cut()
        for index in range(1, 64):
            self.cut('baseline-%d' % index, 'cut-%d' % index)
        before = {path.name for path in (self.path / store.DIRECTORY).iterdir()}
        with self.assertRaisesRegex(ValueError, 'limit of 64'):
            store.cut(self.path, self.context, 'sixty-fifth', 'cut-65', self.no_native, '2026-01-01T00:00:00Z')
        self.assertEqual(before, {path.name for path in (self.path / store.DIRECTORY).iterdir()})
        self.assertEqual(store.list_baselines(self.path, 'alpha')['total'], 64)

    def install_sidecar(self, destination, files):
        path = self.parent / destination; path.mkdir()
        for relative, value in files.items():
            target = path / relative; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value['text'].encode('utf-8') if relative.endswith('/BRD.md') else schema.json_bytes(value))
        return path

    def test_two_restores_preserve_frozen_bytes_and_bind_new_destination_cuts(self):
        self.cut(); original = self.frozen_bytes()
        files = store.backup_files(self.path, 'alpha')
        restored = store.restored_files(files, 'alpha', 'beta')
        beta = self.install_sidecar('beta', restored)
        self.assertEqual(store.get(beta, 'beta', 'review-1')['snapshot']['project'], 'alpha')
        self.assertEqual(original, {filename: (beta / store.DIRECTORY / 'review-1' / filename).read_bytes() for filename in store.FILES})
        governance.initialize(beta, 'beta', ACCOUNT, 'provision-beta')
        context = http.OwnerContext('beta', ACCOUNT, governance.current(beta, 'beta'))
        store.cut(beta, context, 'beta-cut', 'beta-cut', CompleteNative(), '1999-01-01T00:00:00Z')
        second = store.restored_files(store.backup_files(beta, 'beta'), 'beta', 'gamma')
        gamma = self.install_sidecar('gamma', second)
        self.assertEqual(store.get(gamma, 'gamma', 'review-1')['snapshot']['project'], 'alpha')
        self.assertEqual(store.get(gamma, 'gamma', 'beta-cut')['snapshot']['project'], 'beta')
        self.assertEqual(store.current(gamma, 'gamma')['name'], 'beta-cut')
        forged = copy.deepcopy(second)
        binding = forged[store.PREFIX + 'source.json']
        binding['baselines']['review-1']['receipt_sha256'] = '0' * 64
        binding['sha256'] = content_hash(binding)
        with self.assertRaises(ValueError):
            store.validate_files(forged, 'gamma')
        with self.assertRaises(ValueError):
            store.validate_files(files, 'unrelated')

    def test_empty_legacy_project_has_no_new_sidecar_and_restore_is_noop(self):
        self.assertEqual(store.backup_files(self.path, 'alpha'), {})
        self.assertEqual(store.restored_files({}, 'alpha', 'beta'), {})
        self.assertFalse((self.path / store.DIRECTORY).exists())
        self.assertEqual(store.list_baselines(self.path, 'alpha')['items'], [])
        with self.assertRaisesRegex(ValueError, 'not found'):
            store.get(self.path, 'alpha', 'missing')

    def test_backup_missing_pair_unsafe_path_and_pointer_mismatch_refuse(self):
        self.cut(); files = store.backup_files(self.path, 'alpha')
        for modified in ('missing', 'unsafe', 'pointer'):
            bad = copy.deepcopy(files)
            if modified == 'missing':
                del bad[store.PREFIX + 'review-1/BRD.md']
            elif modified == 'unsafe':
                bad[store.PREFIX + '../escape/receipt.json'] = {}
            else:
                bad[store.PREFIX + 'current.json']['receipt_sha256'] = '0' * 64
            with self.subTest(kind=modified), self.assertRaises(ValueError):
                store.validate_files(bad, 'alpha')

    def test_empty_or_null_restore_binding_is_never_treated_as_absent(self):
        self.cut(); files = store.backup_files(self.path, 'alpha')
        for binding in ({}, None):
            bad = dict(files, **{store.PREFIX + 'source.json': binding})
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                store.validate_files(bad, 'alpha')
        (self.path / store.DIRECTORY / 'source.json').write_bytes(b'{}\n')
        with self.assertRaises(ValueError):
            store.get(self.path, 'alpha', 'review-1')

    def test_directory_io_errors_are_bounded_and_do_not_echo_host_paths(self):
        with patch.object(Path, 'lstat', side_effect=PermissionError('/private/host/path')):
            with self.assertRaisesRegex(ValueError, '^' + store.REPAIR.replace('.', r'\.') + '$'):
                store.list_baselines(self.path, 'alpha')

    def test_symlink_selected_file_is_refused_without_reading_or_changing_target(self):
        self.cut(); secret = self.parent / 'target.txt'; secret.write_text('untouched')
        selected = self.path / store.DIRECTORY / 'review-1' / 'BRD.md'
        selected.unlink()
        try:
            selected.symlink_to(secret)
        except OSError as error:
            self.skipTest('Creating symlinks is unavailable on this platform: ' + type(error).__name__)
        with self.assertRaisesRegex(ValueError, 'cannot be verified'):
            store.get(self.path, 'alpha', 'review-1')
        self.assertEqual(secret.read_text(), 'untouched')

    def test_root_directory_symlink_refuses_reads_and_cuts_without_touching_target(self):
        outside = self.parent / 'outside'; outside.mkdir(); (outside / 'sentinel').write_text('untouched')
        base = self.path / store.DIRECTORY
        try:
            base.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest('Creating directory symlinks is unavailable: ' + type(error).__name__)
        calls = list(self.native.calls)
        for read in (lambda: store.list_baselines(self.path, 'alpha'),
                     lambda: store.current(self.path, 'alpha'),
                     lambda: store.get(self.path, 'alpha', 'review-1'),
                     lambda: store.backup_files(self.path, 'alpha'), lambda: self.cut()):
            with self.assertRaisesRegex(ValueError, 'cannot be verified'):
                read()
        self.assertEqual(self.native.calls, calls)
        self.assertEqual({path.name: path.read_bytes() for path in outside.iterdir()}, {'sentinel': b'untouched'})

    def test_selected_directory_symlink_refuses_before_any_target_file_is_opened(self):
        outside = self.parent / 'outside'; outside.mkdir(); (outside / 'sentinel').write_text('untouched')
        base = self.path / store.DIRECTORY; base.mkdir()
        try:
            (base / 'review-1').symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest('Creating directory symlinks is unavailable: ' + type(error).__name__)
        with patch.object(store, '_read', side_effect=AssertionError('A selected symlink target must never be read')):
            with self.assertRaisesRegex(ValueError, 'cannot be verified'):
                store.get(self.path, 'alpha', 'review-1')
        self.assertEqual((outside / 'sentinel').read_text(), 'untouched')

    def test_file_replacement_between_lstat_and_open_refuses_the_new_descriptor(self):
        self.cut(); selected = self.path / store.DIRECTORY / 'review-1' / 'snapshot.json'
        replacement = self.parent / 'replacement.dat'; replacement.write_bytes(b'competing file')
        real_open = store.os.open; switched = []
        def replace_then_open(path, flags, *args, **kwargs):
            if Path(path) == selected and not switched:
                os.replace(replacement, selected); switched.append(True)
            return real_open(path, flags, *args, **kwargs)
        with patch.object(store.os, 'open', replace_then_open), self.assertRaisesRegex(ValueError, 'cannot be verified'):
            store._read(selected, 1024)
        self.assertEqual(switched, [True])
        self.assertEqual(selected.read_bytes(), b'competing file')

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFOs require POSIX; actual Linux proof remains required')
    def test_selected_fifo_refuses_without_waiting_for_a_writer(self):
        self.cut(); selected = self.path / store.DIRECTORY / 'review-1' / 'BRD.md'; selected.unlink()
        os.mkfifo(selected)
        with self.assertRaisesRegex(ValueError, 'cannot be verified'):
            store.get(self.path, 'alpha', 'review-1')


if __name__ == '__main__':
    unittest.main()
