import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin
import project_creation
import requirement_governance as governance
from requirements import content_hash

OWNER = 'usr_' + 'a' * 16


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'projects' / 'alpha'
        self.project.mkdir(parents=True)

    def initialize(self):
        return governance.initialize(self.project, 'alpha', OWNER, 'creation-alpha')

    def set_mode(self, mode, operation='mode-alpha'):
        current = governance.current(self.project, 'alpha')
        return governance.set_mode(self.project, 'alpha', mode, OWNER, operation,
                                   current['revision'], current['sha256'])

    def test_existing_missing_is_governed_and_read_does_not_initialize(self):
        self.assertEqual(governance.current(self.project, 'alpha'),
                         {'revision': 0, 'sha256': None, 'mode': 'governed'})
        self.assertEqual(list(self.project.iterdir()), [])
        current = self.set_mode('simple')
        self.assertEqual((current['revision'], current['mode']), (1, 'simple'))

    def test_present_null_is_invalid_history_or_restore_binding_not_absence(self):
        (self.project / governance.FILE).write_text('null', encoding='utf-8')
        before = (self.project / governance.FILE).read_bytes()
        with self.assertRaisesRegex(ValueError, 'Invalid requirements governance record'):
            governance.current(self.project, 'alpha')
        self.assertEqual((self.project / governance.FILE).read_bytes(), before)
        (self.project / governance.FILE).unlink()
        self.initialize()
        (self.project / governance.SOURCE_FILE).write_text('null', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Invalid requirements governance restore binding'):
            governance.current(self.project, 'alpha')

    def test_future_clock_warning_boundary_does_not_order_or_rewrite_history(self):
        import requirement_http
        from http_authority import JOURNAL_MAX_SKEW_SECONDS
        timestamp = '2026-10-10T07:00:00Z'
        instant = datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=timezone.utc).timestamp()
        first = governance.entry(1, None, 'simple', OWNER, 'session', 'first', timestamp)
        # A later revision can have an earlier timestamp; hashes/revisions govern.
        second = governance.entry(2, first['sha256'], 'governed', OWNER, 'session',
                                  'second', '2000-01-01T00:00:00Z')
        record = dict(schema_version=1, project='alpha', revisions=[first, second])
        path = self.project / governance.FILE
        path.write_text(json.dumps(record), encoding='utf-8')
        before = path.read_bytes()
        authority = governance.current(self.project, 'alpha')
        for difference, warned in ((JOURNAL_MAX_SKEW_SECONDS - 1, False),
                                   (JOURNAL_MAX_SKEW_SECONDS, False),
                                   (JOURNAL_MAX_SKEW_SECONDS + 1, True)):
            with self.subTest(difference=difference), patch.object(
                    governance.time, 'time', return_value=instant - difference):
                result = requirement_http.read(self.project, 'alpha', ['governance'],
                                              lambda args: self.fail('Unexpected native read'))
                self.assertEqual({k: result[k] for k in authority}, authority)
                self.assertEqual(bool(result.get('warnings')), warned)
                if warned:
                    self.assertEqual(result['warnings'][0]['revision'], 1)
                    self.assertIn('host operator', result['warnings'][0]['message'])
                    self.assertIn('clock and history', result['warnings'][0]['message'])
                self.assertEqual(path.read_bytes(), before)
        # A write on the behind-clock host still uses the exact revision/hash CAS.
        changed = self.set_mode('simple', 'after-clock-warning')
        self.assertEqual(changed['revision'], 3)
        self.assertEqual(governance.snapshot(self.project, 'alpha')[governance.FILE]
                         ['revisions'][:2], record['revisions'])

    def test_restored_future_history_warns_boundedly_without_locking_owner_out(self):
        entries = []
        for number in range(1, 13):
            entries.append(governance.entry(number, entries[-1]['sha256'] if entries else None,
                'simple', OWNER, 'session', 'future-' + str(number), '2999-01-01T00:00:00Z'))
        original = {governance.FILE: dict(schema_version=1, project='alpha', revisions=entries)}
        restored = governance.restored_files(original, 'alpha', 'beta')
        beta = self.root / 'projects' / 'beta'; beta.mkdir()
        for name, value in restored.items():
            (beta / name).write_text(json.dumps(value), encoding='utf-8')
        before = {p.name: p.read_bytes() for p in beta.iterdir()}
        result = governance.read_state(beta, 'beta', comparison_time=0)
        self.assertEqual([w['revision'] for w in result['warnings']], list(range(1, 9)))
        self.assertTrue(result['warnings_truncated'])
        self.assertEqual({p.name: p.read_bytes() for p in beta.iterdir()}, before)
        current = governance.current(beta, 'beta')
        changed = governance.set_mode(beta, 'beta', 'governed', OWNER, 'restored-write',
                                      current['revision'], current['sha256'])
        self.assertEqual((changed['revision'], changed['mode']), (13, 'governed'))
        governance.validate_evidence(beta, 'beta', {'project': 'alpha',
            'governance': {k: entries[-1][k] for k in ('revision', 'sha256', 'mode')}})

    def test_default_creation_retry_and_both_mode_switches_preserve_history(self):
        first = self.initialize()
        self.assertEqual(first['mode'], 'simple')
        original = (self.project / governance.FILE).read_bytes()
        self.assertEqual(self.initialize(), first)
        self.assertEqual((self.project / governance.FILE).read_bytes(), original)
        second = self.set_mode('governed')
        third = self.set_mode('simple', 'back-to-simple')
        record = governance.snapshot(self.project, 'alpha')[governance.FILE]
        self.assertEqual([r['mode'] for r in record['revisions']], ['simple', 'governed', 'simple'])
        self.assertEqual(record['revisions'][1]['previous_sha256'], first['sha256'])
        self.assertEqual(record['revisions'][2]['previous_sha256'], second['sha256'])
        self.assertEqual(third['revision'], 3)
        with self.assertRaisesRegex(ValueError, 'this creation'):
            governance.initialize(self.project, 'alpha', OWNER, 'another-creation')

    def test_mode_cas_and_retry_do_not_rewrite_history(self):
        first = self.initialize()
        second = self.set_mode('governed')
        before = (self.project / governance.FILE).read_bytes()
        self.assertEqual(governance.set_mode(self.project, 'alpha', 'governed', OWNER,
                         'mode-alpha', first['revision'], first['sha256']), second)
        for args in [('simple', 'stale', 1, first['sha256']),
                     ('simple', 'mode-alpha', 1, first['sha256']),
                     ('simple', 'fresh', True, first['sha256'])]:
            with self.assertRaises(ValueError):
                governance.set_mode(self.project, 'alpha', args[0], OWNER, *args[1:])
        self.assertEqual((self.project / governance.FILE).read_bytes(), before)

    def test_duplicate_keys_broken_chain_unknown_shape_and_wrong_project_refuse(self):
        self.initialize()
        self.set_mode('governed')
        valid = governance.snapshot(self.project, 'alpha')[governance.FILE]
        bads = []
        bad = copy.deepcopy(valid); bad['revisions'][1]['previous_sha256'] = '0' * 64
        bad['revisions'][1]['sha256'] = content_hash(bad['revisions'][1]); bads.append(bad)
        bad = copy.deepcopy(valid); bad['revisions'][1]['revision'] = True; bads.append(bad)
        bad = copy.deepcopy(valid); bad['unknown'] = None; bads.append(bad)
        bad = copy.deepcopy(valid); bad['revisions'][1]['operation_id'] = 'creation-alpha'
        bad['revisions'][1]['sha256'] = content_hash(bad['revisions'][1]); bads.append(bad)
        for bad in bads:
            with self.subTest(record=bad), self.assertRaises(ValueError):
                governance.validate(bad)
        with self.assertRaisesRegex(ValueError, 'different project'):
            governance.snapshot(self.project, 'beta')
        (self.project / governance.FILE).write_text('{"project":"alpha","project":"beta"}', encoding='utf-8')
        with self.assertRaises(ValueError):
            governance.current(self.project, 'alpha')

    def test_restore_new_keeps_history_and_validates_source_before_any_write(self):
        self.initialize(); self.set_mode('governed')
        original = governance.snapshot(self.project, 'alpha')
        restored = governance.restored_files(original, 'alpha', 'beta')
        self.assertEqual(restored[governance.FILE], original[governance.FILE])
        beta = self.root / 'projects' / 'beta'; beta.mkdir()
        with patch.object(admin, 'coordination_backup', return_value=original), \
                patch.object(admin, 'using_last_complete_sidecar', return_value=False), \
                patch.object(admin, 'report_native_backup_change'):
            self.assertTrue(admin.restore_coordination(self.root, 'alpha', 'beta', authority=False))
        self.assertEqual(governance.current(beta, 'beta')['mode'], 'governed')
        self.assertEqual(governance.snapshot(beta, 'beta')[governance.FILE], original[governance.FILE])
        state = governance.current(beta, 'beta')
        governance.set_mode(beta, 'beta', 'simple', OWNER, 'beta-simple', state['revision'], state['sha256'])
        second_restore = governance.restored_files(governance.snapshot(beta, 'beta'), 'beta', 'gamma')
        governance.validate_files(second_restore, 'gamma')
        self.assertEqual(second_restore[governance.FILE]['project'], 'alpha')
        gamma = self.root / 'projects' / 'gamma'; gamma.mkdir()
        for name, record in second_restore.items():
            (gamma / name).write_text(json.dumps(record), encoding='utf-8')
        for revision, project in ((1, 'alpha'), (3, 'beta')):
            stored = second_restore[governance.FILE]['revisions'][revision - 1]
            governance.validate_evidence(gamma, 'gamma', {'project': project,
                'governance': {k: stored[k] for k in ('revision', 'sha256', 'mode')}})
            governance.validate_evidence(gamma, 'gamma', {'project': 'gamma',
                'governance': {k: stored[k] for k in ('revision', 'sha256', 'mode')}})
            with self.assertRaisesRegex(ValueError, 'different project'):
                governance.validate_evidence(gamma, 'gamma', {'project': 'unrelated',
                    'governance': {k: stored[k] for k in ('revision', 'sha256', 'mode')}})
        with self.assertRaisesRegex(ValueError, 'different project'):
            governance.restored_files(original, 'other', 'beta')
        damaged = copy.deepcopy(second_restore)
        damaged[governance.SOURCE_FILE]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'restore hash'):
            admin.validate_coordination_files(damaged)

    def test_finish_uses_the_existing_creation_and_rejects_legacy_initialization(self):
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            admin.initialize_requirements_governance(self.root, 'alpha')
        project_creation.write_record(self.root, 'alpha', {
            'project': 'alpha', 'by': OWNER, 'operation_id': 'creation-alpha',
            'state': 'incomplete', 'stage': 'configure', 'started_at': '2030-01-01T00:00:00Z',
            'requirements_governance': 'simple'})
        self.assertEqual(admin.initialize_requirements_governance(self.root, 'alpha')['mode'], 'simple')
        self.assertEqual(admin.initialize_requirements_governance(self.root, 'alpha')['revision'], 1)

    def test_legacy_creation_and_restore_intent_never_install_simple_default(self):
        legacy = {'project': 'alpha', 'by': OWNER, 'operation_id': 'creation-alpha',
                  'state': 'incomplete', 'stage': 'configure', 'started_at': '2030-01-01T00:00:00Z'}
        project_creation.write_record(self.root, 'alpha', legacy)
        self.assertEqual(admin.initialize_requirements_governance(self.root, 'alpha')['mode'], 'governed')
        self.assertFalse((self.project/governance.FILE).exists())
        made = []
        def initialize(root, name, stage):
            directory = root/'projects'/name; directory.mkdir(parents=True)
            made.append(admin.initialize_requirements_governance(root, name))
        project_creation.host_create(self.root, 'beta', initialize, lambda *args: None,
                                     requirements_default=False)
        self.assertEqual(made[0], {'revision': 0, 'sha256': None, 'mode': 'governed'})
        self.assertNotIn('requirements_governance', project_creation.read_record(self.root, 'beta'))

    def test_same_mode_is_noop_but_stale_hash_is_refused(self):
        first = self.initialize(); before = (self.project/governance.FILE).read_bytes()
        self.assertEqual(self.set_mode('simple'), first)
        self.assertEqual((self.project/governance.FILE).read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'changed'):
            governance.set_mode(self.project, 'alpha', 'simple', OWNER, 'stale', 1, '0'*64)

    def test_invalid_time_and_damaged_file_do_not_disclose_server_path(self):
        self.initialize(); record = governance.snapshot(self.project, 'alpha')[governance.FILE]
        for at in ('yesterday', '2030-02-30T00:00:00Z', '2030-01-01T25:00:00Z'):
            bad = copy.deepcopy(record); bad['revisions'][0]['at'] = at
            bad['revisions'][0]['sha256'] = content_hash(bad['revisions'][0])
            with self.subTest(at=at), self.assertRaisesRegex(ValueError, 'valid UTC'):
                governance.validate(bad)
        (self.project/governance.FILE).write_text('{broken')
        with self.assertRaises(ValueError) as refused:
            governance.current(self.project, 'alpha')
        self.assertNotIn(str(self.project), str(refused.exception))

    def test_symlink_refusal_does_not_modify_target(self):
        outside = self.root / 'outside'; outside.write_text('unchanged')
        try:
            (self.project / governance.FILE).symlink_to(outside)
        except OSError:
            self.skipTest('Creating symlinks is not available')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.initialize()
        self.assertEqual(outside.read_text(), 'unchanged')


if __name__ == '__main__':
    unittest.main()
