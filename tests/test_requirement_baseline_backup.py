"""Explicit backup admission and immutable, resumable restore installation."""
import copy
import contextlib
import os
import sys
import types
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import admin
import requirement_baseline_records as schema
import requirement_baselines as store
import requirement_governance as governance
import requirement_http as http
from test_requirement_baseline_records import CompleteNative
from test_requirement_owner_records import ACCOUNT


class BaselineBackupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.path = self.root / 'projects' / 'alpha'; self.path.mkdir(parents=True)
        governance.initialize(self.path, 'alpha', ACCOUNT, 'creation-alpha')
        self.context = http.OwnerContext('alpha', ACCOUNT, governance.current(self.path, 'alpha'))
        self.native = CompleteNative()
        http.apply(self.path, self.context, 'create', dict(kind='requirement', parent='job-1',
            title='A requirement', description='Original frozen wording'), 'create-one', self.native)
        store.cut(self.path, self.context, 'review-1', 'cut-one', self.native, '2026-01-01T00:00:00Z')
        self.files = dict(governance.snapshot(self.path, 'alpha'), **store.backup_files(self.path, 'alpha'))
        self.target = self.root / 'projects' / 'beta'; self.target.mkdir()
        self.restored = store.restored_files(self.files, 'alpha', 'beta')
        self.restored = governance.restored_files(self.restored, 'alpha', 'beta')

    def test_admin_admits_only_complete_family_with_current_governance_and_exact_markdown(self):
        admin.validate_coordination_files(self.files)
        for path in (store.PREFIX + 'review-1/BRD.md', store.PREFIX + 'review-1/receipt.json', governance.FILE):
            bad = copy.deepcopy(self.files); del bad[path]
            with self.subTest(path=path), self.assertRaises(ValueError):
                admin.validate_coordination_files(bad)
        for name in (store.PREFIX + '../escape', store.PREFIX + 'review-1/unknown.json', store.PREFIX + 'CON/snapshot.json'):
            bad = copy.deepcopy(self.files); bad[name] = {}
            with self.subTest(path=name), self.assertRaises(ValueError):
                admin.validate_coordination_files(bad)
        bad = copy.deepcopy(self.files); bad[store.PREFIX + 'review-1/BRD.md']['text'] += 'Changed'
        with self.assertRaises(ValueError):
            admin.validate_coordination_files(bad)

    def test_actual_admin_restore_dispatch_keeps_all_three_named_files_exact_and_lineage_separate(self):
        windows_lock = patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(LOCK_EX=2, flock=lambda *args: None, LOCK_NB=4, LOCK_UN=8, LOCK_SH=1)}) if os.name == 'nt' else contextlib.nullcontext()
        with windows_lock, patch.object(admin, 'coordination_backup', return_value=self.files), \
                patch.object(admin, 'using_last_complete_sidecar', return_value=False), \
                patch.object(admin, 'report_native_backup_change'), \
                patch.object(admin, 'restore_lifecycle_void_family', return_value=False), \
                patch.object(admin, 'restore_web_lifecycle_mark_family', return_value=False):
            self.assertTrue(admin.restore_coordination(self.root, 'alpha', 'beta', authority=False))
        for name in store.FILES:
            self.assertEqual((self.path / store.DIRECTORY / 'review-1' / name).read_bytes(),
                             (self.target / store.DIRECTORY / 'review-1' / name).read_bytes())
        self.assertEqual(store.get(self.target, 'beta', 'review-1')['snapshot']['project'], 'alpha')
        self.assertEqual(governance.current(self.target, 'beta')['mode'], 'simple')
        self.assertFalse((self.target / store.RESTORE_MARKER).exists())
        admin.validate_coordination_files(dict(governance.snapshot(self.target, 'beta'), **store.backup_files(self.target, 'beta')))

    def test_interrupted_restore_blocks_reads_and_exact_retry_preserves_files(self):
        real = store._write_once
        def interrupt(path, raw):
            if path.name == 'BRD.md':
                raise OSError('injected restore failure')
            return real(path, raw)
        with patch.object(store, '_write_once', interrupt), self.assertRaises(OSError):
            store.restore_files(self.target, self.restored, 'beta')
        marker = self.target / store.RESTORE_MARKER
        saved = marker.read_bytes(); self.assertTrue(saved)
        for reader in (lambda: store.current(self.target, 'beta'), lambda: store.list_baselines(self.target, 'beta'),
                       lambda: store.get(self.target, 'beta', 'review-1'), lambda: store.backup_files(self.target, 'beta')):
            with self.assertRaisesRegex(ValueError, 'restore is incomplete'):
                reader()
        changed = copy.deepcopy(self.restored); changed[store.PREFIX + 'review-1/BRD.md']['text'] += 'changed'
        with self.assertRaises(ValueError):
            store.restore_files(self.target, changed, 'beta')
        self.assertEqual(marker.read_bytes(), saved)
        self.assertTrue(store.restore_files(self.target, self.restored, 'beta'))
        self.assertFalse(marker.exists())
        for name in store.FILES:
            self.assertEqual((self.path / store.DIRECTORY / 'review-1' / name).read_bytes(),
                             (self.target / store.DIRECTORY / 'review-1' / name).read_bytes())
        self.assertTrue(store.restore_files(self.target, self.restored, 'beta'))

    def test_restore_never_overwrites_other_immutable_history_or_drops_existing_family(self):
        store.restore_files(self.target, self.restored, 'beta')
        target = self.target / store.DIRECTORY / 'review-1' / 'BRD.md'
        target.write_bytes(b'other bytes')
        with self.assertRaisesRegex(ValueError, 'different immutable'):
            store.restore_files(self.target, self.restored, 'beta')
        self.assertEqual(target.read_bytes(), b'other bytes')
        self.assertFalse((self.target / store.RESTORE_MARKER).exists())
        with self.assertRaisesRegex(ValueError, 'already exists'):
            store.restore_files(self.target, {}, 'beta')
        fresh = self.root / 'projects' / 'fresh'; fresh.mkdir()
        self.assertFalse(store.restore_files(fresh, {}, 'fresh'))
        self.assertEqual(list(fresh.iterdir()), [])
