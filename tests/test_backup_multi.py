"""Multi-project scheduled backup: status file, coverage guidance and compatibility.

kittrial-5bb.39: a project added after installation silently fell outside the
installation's single-project schedule. These tests cover the multi-project /
``--all`` invocation, the honest per-project status file, the read/verify path,
the unchanged single-project form, the exact schedule guidance ``add-project``
prints, the stale staged-journal cleanup an interrupted run needs and the lock
scope of the reference off-machine copy.
"""
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin


def make_project(root, name):
    """Create the native marker a project must carry to be an initialized target."""
    path = root / 'projects' / name
    (path / '.beads').mkdir(parents=True, exist_ok=True)
    (path / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
    return path


def write_pair(root, name, status='complete'):
    (root / 'backups' / name).mkdir(parents=True, exist_ok=True)
    (root / 'backups' / (name + '.coordination.json')).write_text(
        json.dumps({'schema_version': 1, 'status': status}), encoding='utf-8')


class BackupCommandCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'backups').mkdir()
        for name in ('alpha', 'beta'):
            make_project(self.root, name)
        self.calls = []

    def fake_backup(self, fail=(), pending=(), no_native=()):
        def backup(root, name):
            self.calls.append(name)
            if name in fail:
                raise RuntimeError('sync refused for ' + name)
            if name not in no_native:
                (root / 'backups' / name).mkdir(parents=True, exist_ok=True)
            (root / 'backups' / (name + '.coordination.json')).write_text(
                json.dumps({'schema_version': 1, 'status': 'pending' if name in pending else 'complete'}),
                encoding='utf-8')
            return 'native output for ' + name
        return backup

    def run_admin(self, *argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = 0
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                patch.object(admin, 'root_path', return_value=self.root), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                admin.main()
            except SystemExit as exit:
                code = exit.code if isinstance(exit.code, int) else 1
                if exit.code is not None and not isinstance(exit.code, int):
                    stderr.write(str(exit.code))
        return stdout.getvalue(), stderr.getvalue(), code

    def status_text(self):
        return (self.root / 'backups' / admin.BACKUP_STATUS_NAME).read_text(encoding='utf-8')

    def test_all_projects_backs_up_every_initialized_project(self):
        with patch.object(admin, 'backup_project', side_effect=self.fake_backup()):
            stdout, stderr, code = self.run_admin('backup', '--all')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.calls, ['alpha', 'beta'])
        self.assertIn('native output for alpha', stdout)
        self.assertIn('native output for beta', stdout)
        self.assertIn('Backed up 2 of 2 project(s)', stdout)
        record = admin.read_backup_status(self.root)
        self.assertEqual(record['schema_version'], 1)
        self.assertEqual(record['scope'], 'all')
        self.assertEqual(record['status'], 'complete')
        self.assertEqual([entry['name'] for entry in record['projects']], ['alpha', 'beta'])
        for entry in record['projects']:
            self.assertEqual(entry['status'], 'complete')
            self.assertTrue(admin.utc_timestamp(entry['completed_at']))
            self.assertEqual(entry['pair'], {'native': 'backups/' + entry['name'],
                                             'coordination': 'backups/' + entry['name'] + '.coordination.json'})
        self.assertTrue(admin.utc_timestamp(record['generated_at']))
        # Deterministic JSON: the file is exactly the sorted-key dump of its record.
        self.assertEqual(self.status_text(), json.dumps(record, ensure_ascii=False, sort_keys=True))

    def test_several_named_projects_run_in_one_invocation(self):
        with patch.object(admin, 'backup_project', side_effect=self.fake_backup()):
            stdout, stderr, code = self.run_admin('backup', 'beta', 'alpha')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.calls, ['alpha', 'beta'])
        record = admin.read_backup_status(self.root)
        self.assertEqual(record['scope'], 'named')
        self.assertEqual(record['status'], 'complete')

    def test_single_project_form_keeps_its_stdout_and_exit(self):
        with patch.object(admin, 'backup_project', side_effect=self.fake_backup()) as backup:
            stdout, stderr, code = self.run_admin('backup', 'alpha')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, 'native output for alpha\n')
        self.assertEqual(stderr, '')
        backup.assert_called_once_with(self.root, 'alpha')
        record = admin.read_backup_status(self.root)
        self.assertEqual(record['scope'], 'named')
        self.assertEqual([entry['name'] for entry in record['projects']], ['alpha'])

    def test_one_failed_project_does_not_stop_the_others_and_is_recorded(self):
        with patch.object(admin, 'backup_project', side_effect=self.fake_backup(fail=('alpha',))):
            stdout, stderr, code = self.run_admin('backup', '--all')
        self.assertNotEqual(code, 0)
        self.assertEqual(self.calls, ['alpha', 'beta'])
        self.assertIn('native output for beta', stdout)
        self.assertIn('backup failed for alpha', stderr)
        self.assertIn('sync refused for alpha', stderr)
        record = admin.read_backup_status(self.root)
        self.assertEqual(record['status'], 'incomplete')
        entries = {entry['name']: entry for entry in record['projects']}
        self.assertEqual(entries['alpha']['status'], 'failed')
        self.assertIn('sync refused for alpha', entries['alpha']['reason'])
        self.assertNotIn('completed_at', entries['alpha'])
        self.assertEqual(entries['beta']['status'], 'complete')

    def test_a_pending_sidecar_is_never_recorded_complete(self):
        with patch.object(admin, 'backup_project', side_effect=self.fake_backup(pending=('beta',))):
            stdout, stderr, code = self.run_admin('backup', '--all')
        self.assertNotEqual(code, 0)
        entries = {entry['name']: entry for entry in admin.read_backup_status(self.root)['projects']}
        self.assertEqual(entries['beta']['status'], 'failed')
        self.assertIn('not complete', entries['beta']['reason'])
        self.assertEqual(entries['alpha']['status'], 'complete')

    def test_a_missing_native_directory_is_never_recorded_complete(self):
        with patch.object(admin, 'backup_project', side_effect=self.fake_backup(no_native=('beta',))):
            stdout, stderr, code = self.run_admin('backup', '--all')
        self.assertNotEqual(code, 0)
        entries = {entry['name']: entry for entry in admin.read_backup_status(self.root)['projects']}
        self.assertEqual(entries['beta']['reason'], 'native backup directory is missing')

    def test_an_unknown_named_project_is_refused_and_never_recorded(self):
        # kittrial-5bb.85 review 01a1026a: `backup NOSUCHPROJECT` recorded a skipped entry
        # that every later `backup --all` carried forward, so the gate failed for good.
        with patch.object(admin, 'backup_project', side_effect=self.fake_backup()) as backup:
            with self.assertRaisesRegex(ValueError, 'Not an initialized project in this runtime: gamma. Nothing '
                                                    'was backed up or recorded'):
                self.run_admin('backup', 'gamma')
            with self.assertRaisesRegex(ValueError, 'Not an initialized project in this runtime: gamma, zeta'):
                self.run_admin('backup', 'alpha', 'zeta', 'gamma')       # the known one is not backed up either
            backup.assert_not_called()
            self.assertFalse((self.root / 'backups' / admin.BACKUP_STATUS_NAME).exists())
            stdout, stderr, code = self.run_admin('backup', '--all')
        self.assertEqual(code, 0, stderr)
        record = admin.read_backup_status(self.root)
        self.assertEqual((record['status'], [entry['name'] for entry in record['projects']]),
                         ('complete', ['alpha', 'beta']))

    def test_an_entry_for_a_name_that_is_not_a_project_is_not_carried_forward(self):
        # What an older kit recorded for a mistyped name is dropped by the next run.
        with patch.object(admin, 'backup_project', side_effect=self.fake_backup()):
            self.run_admin('backup', '--all')
            record = admin.read_backup_status(self.root)
            record['status'] = 'incomplete'
            record['projects'].append({'name': 'nosuchproject', 'status': 'skipped',
                                       'reason': 'not an initialized project in this runtime'})
            admin.write_backup_status(self.root, record)
            gate = self.run_admin('backup-status', '--require-complete')
            self.assertNotEqual(gate[2], 0)
            for argv in (('backup', 'alpha'), ('backup', '--all')):
                stdout, stderr, code = self.run_admin(*argv)
                self.assertEqual(code, 0, stderr)
                record = admin.read_backup_status(self.root)
                self.assertEqual((record['status'], [entry['name'] for entry in record['projects']]),
                                 ('complete', ['alpha', 'beta']), argv)
            self.assertEqual(self.run_admin('backup-status', '--require-complete')[2], 0)

    def test_a_run_without_targets_is_refused_rather_than_recorded_empty(self):
        with self.assertRaises(ValueError):
            admin.backup_projects(self.root, [], False)
        empty = Path(self.temp.name) / 'emptyruntime'
        (empty / 'backups').mkdir(parents=True)
        with patch.object(admin, 'backup_project') as backup:
            with self.assertRaises(ValueError):
                admin.backup_projects(empty, [], True)
        backup.assert_not_called()
        self.assertFalse((empty / 'backups' / admin.BACKUP_STATUS_NAME).exists())

    def test_all_with_named_projects_is_refused_rather_than_ignored(self):
        with self.assertRaisesRegex(ValueError, 'do not also name projects'):
            admin.backup_projects(self.root, ['alpha'], True)
        self.assertFalse((self.root / 'backups' / admin.BACKUP_STATUS_NAME).exists())

    def test_initialized_projects_ignores_stray_and_unusable_directories(self):
        (self.root / 'projects' / 'stray').mkdir()
        (self.root / 'projects' / 'bad_name').mkdir()
        (self.root / 'projects' / 'bad_name' / '.beads').mkdir()
        (self.root / 'projects' / 'bad_name' / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        self.assertEqual(admin.initialized_projects(self.root), ['alpha', 'beta'])
        try:
            (self.root / 'projects' / 'linked').symlink_to(self.root / 'projects' / 'alpha',
                                                           target_is_directory=True)
        except OSError:
            return
        self.assertEqual(admin.initialized_projects(self.root), ['alpha', 'beta'])

    def test_a_named_run_keeps_the_last_known_state_of_untouched_projects(self):
        with patch.object(admin, 'backup_project', side_effect=self.fake_backup()):
            self.run_admin('backup', '--all')
        first = {entry['name']: entry for entry in admin.read_backup_status(self.root)['projects']}
        with patch.object(admin, 'backup_project', side_effect=self.fake_backup()):
            stdout, stderr, code = self.run_admin('backup', 'alpha')
        self.assertEqual(code, 0, stderr)
        record = admin.read_backup_status(self.root)
        # The run's own scope and stamp are truthful, and beta's entry survives intact.
        self.assertEqual(record['scope'], 'named')
        self.assertTrue(admin.utc_timestamp(record['generated_at']))
        entries = {entry['name']: entry for entry in record['projects']}
        self.assertEqual(sorted(entries), ['alpha', 'beta'])
        self.assertEqual(entries['beta'], first['beta'])
        self.assertEqual(entries['alpha']['status'], 'complete')

    def test_a_merge_never_drops_a_failed_entry_or_claims_the_run_covered_it(self):
        def failing(root, name):
            raise RuntimeError('sync refused for ' + name)
        with patch.object(admin, 'backup_project', side_effect=failing):
            self.run_admin('backup', '--all')
        failed = {entry['name']: entry for entry in admin.read_backup_status(self.root)['projects']}
        self.assertEqual(failed['beta']['status'], 'failed')
        with patch.object(admin, 'backup_project', side_effect=self.fake_backup()):
            self.run_admin('backup', 'alpha')
        record = admin.read_backup_status(self.root)
        entries = {entry['name']: entry for entry in record['projects']}
        self.assertEqual(entries['beta'], failed['beta'])
        self.assertEqual(record['status'], 'incomplete')

    def test_a_native_failure_reason_keeps_the_bd_stderr(self):
        def failing(root, name):
            raise subprocess.CalledProcessError(1, ['dolt', 'sql'], stderr='Error 1105: backup target is full')
        with patch.object(admin, 'backup_project', side_effect=failing):
            stdout, stderr, code = self.run_admin('backup', 'alpha')
        self.assertNotEqual(code, 0)
        entry = admin.read_backup_status(self.root)['projects'][0]
        self.assertEqual(entry['status'], 'failed')
        self.assertIn('backup target is full', entry['reason'])
        self.assertIn('backup target is full', stderr)
        self.assertNotIn('\n', entry['reason'])


class BackupStatusReadCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'backups').mkdir()
        for name in ('alpha', 'beta'):
            make_project(self.root, name)
            write_pair(self.root, name)
        self.record = {'schema_version': 1, 'scope': 'all', 'generated_at': '2026-09-27T16:00:00Z',
                       'status': 'complete',
                       'projects': [{'name': name, 'status': 'complete',
                                     'completed_at': '2026-09-27T16:00:00Z',
                                     'pair': {'native': 'backups/' + name,
                                              'coordination': 'backups/' + name + '.coordination.json'}}
                                    for name in ('alpha', 'beta')]}

    def run_status(self, *argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = 0
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                patch.object(admin, 'root_path', return_value=self.root), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                admin.main()
            except SystemExit as exit:
                code = exit.code if isinstance(exit.code, int) else 1
                if exit.code is not None and not isinstance(exit.code, int):
                    stderr.write(str(exit.code))
        return stdout.getvalue(), stderr.getvalue(), code

    def test_status_file_is_published_only_after_validation(self):
        admin.write_backup_status(self.root, self.record)
        self.assertEqual(admin.read_backup_status(self.root), self.record)
        bad = json.loads(json.dumps(self.record))
        bad['projects'][0]['status'] = 'failed'
        bad['projects'][0]['reason'] = 'sync failed'
        with self.assertRaises(ValueError):
            admin.write_backup_status(self.root, bad)
        # The refused record did not replace the valid one.
        self.assertEqual(admin.read_backup_status(self.root), self.record)

    def test_require_complete_accepts_a_complete_all_run(self):
        admin.write_backup_status(self.root, self.record)
        stdout, stderr, code = self.run_status('backup-status', '--require-complete')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), self.record)

    def test_require_complete_refuses_a_named_run_that_cannot_cover_every_project(self):
        named = json.loads(json.dumps(self.record))
        named['scope'] = 'named'
        named['projects'] = named['projects'][:1]
        admin.write_backup_status(self.root, named)
        stdout, stderr, code = self.run_status('backup-status', '--require-complete')
        self.assertNotEqual(code, 0)
        self.assertIn('named run', stderr)

    def test_require_complete_detects_a_pair_removed_after_the_run(self):
        admin.write_backup_status(self.root, self.record)
        (self.root / 'backups' / 'beta').rmdir()
        stdout, stderr, code = self.run_status('backup-status', '--require-complete')
        self.assertNotEqual(code, 0)
        self.assertIn('beta: native backup directory is missing', stderr)

    def test_require_complete_detects_a_sidecar_that_became_pending(self):
        admin.write_backup_status(self.root, self.record)
        write_pair(self.root, 'alpha', 'pending')
        stdout, stderr, code = self.run_status('backup-status', '--require-complete')
        self.assertNotEqual(code, 0)
        self.assertIn('alpha', stderr)
        self.assertIn('not complete', stderr)

    def test_require_complete_reports_a_project_initialized_after_the_run(self):
        admin.write_backup_status(self.root, self.record)
        make_project(self.root, 'gamma')
        stdout, stderr, code = self.run_status('backup-status', '--require-complete')
        self.assertNotEqual(code, 0)
        self.assertIn('gamma', stderr)
        self.assertIn('absent from the last run record', stderr)
        # alpha and beta are complete and covered, so only gamma is a problem.
        self.assertNotIn('alpha:', stderr)

    def test_require_complete_reports_a_recorded_project_that_is_not_complete(self):
        record = json.loads(json.dumps(self.record))
        record['status'] = 'incomplete'
        record['projects'][1] = {'name': 'beta', 'status': 'skipped', 'reason': 'not initialized'}
        admin.write_backup_status(self.root, record)
        stdout, stderr, code = self.run_status('backup-status', '--require-complete')
        self.assertNotEqual(code, 0)
        self.assertIn('beta', stderr)
        self.assertIn('recorded skipped', stderr)

    def test_missing_or_malformed_status_file_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'No backup status file'):
            admin.read_backup_status(self.root)
        path = self.root / 'backups' / admin.BACKUP_STATUS_NAME
        path.write_text('{bad', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'not readable JSON'):
            admin.read_backup_status(self.root)

    def test_status_file_symlink_is_refused(self):
        path = self.root / 'backups' / admin.BACKUP_STATUS_NAME
        try:
            path.symlink_to(self.root / 'backups' / 'alpha.coordination.json')
        except OSError as exc:
            self.skipTest('Symlink privilege unavailable: ' + str(exc))
        with self.assertRaisesRegex(ValueError, 'must not be a symlink'):
            admin.read_backup_status(self.root)


class ValidateBackupStatusCase(unittest.TestCase):
    def record(self, **overrides):
        data = {'schema_version': 1, 'scope': 'all', 'generated_at': '2026-09-27T16:00:00Z',
                'status': 'complete',
                'projects': [{'name': 'alpha', 'status': 'complete',
                              'completed_at': '2026-09-27T16:00:00Z',
                              'pair': {'native': 'backups/alpha',
                                       'coordination': 'backups/alpha.coordination.json'}}]}
        data.update(overrides)
        return data

    def test_a_supported_record_validates(self):
        self.assertIsNone(admin.validate_backup_status(self.record()))
        self.assertIsNone(admin.validate_backup_status(self.record(
            status='incomplete',
            projects=[{'name': 'alpha', 'status': 'failed', 'reason': 'sync failed'},
                      {'name': 'beta', 'status': 'skipped', 'reason': 'not initialized'}])))

    def test_records_that_could_overclaim_completeness_are_refused(self):
        cases = {
            'not an object': [],
            'schema': self.record(schema_version=2),
            'scope': self.record(scope='partial'),
            'generated_at': self.record(generated_at='yesterday'),
            'empty projects': self.record(projects=[]),
            'run status disagrees': self.record(
                status='complete',
                projects=[{'name': 'alpha', 'status': 'failed', 'reason': 'sync failed'}]),
            'unknown status': self.record(
                status='incomplete',
                projects=[{'name': 'alpha', 'status': 'ok'}]),
            'complete without timestamp': self.record(
                projects=[{'name': 'alpha', 'status': 'complete',
                           'pair': {'native': 'backups/alpha',
                                    'coordination': 'backups/alpha.coordination.json'}}]),
            'complete with reason': self.record(
                projects=[{'name': 'alpha', 'status': 'complete',
                           'completed_at': '2026-09-27T16:00:00Z', 'reason': 'maybe',
                           'pair': {'native': 'backups/alpha',
                                    'coordination': 'backups/alpha.coordination.json'}}]),
            'complete with another project pair': self.record(
                projects=[{'name': 'alpha', 'status': 'complete',
                           'completed_at': '2026-09-27T16:00:00Z',
                           'pair': {'native': 'backups/beta',
                                    'coordination': 'backups/beta.coordination.json'}}]),
            'incomplete without reason': self.record(
                status='incomplete', projects=[{'name': 'alpha', 'status': 'failed'}]),
            'incomplete with completion fields': self.record(
                status='incomplete',
                projects=[{'name': 'alpha', 'status': 'failed', 'reason': 'sync failed',
                           'completed_at': '2026-09-27T16:00:00Z'}]),
            'invalid name': self.record(
                projects=[{'name': 'Alpha', 'status': 'failed', 'reason': 'x'}]),
            'missing name': self.record(
                projects=[{'status': 'failed', 'reason': 'x'}]),
            'entry not an object': self.record(status='incomplete', projects=[[]]),
            'repeated project': self.record(
                status='incomplete',
                projects=[{'name': 'alpha', 'status': 'failed', 'reason': 'x'},
                          {'name': 'alpha', 'status': 'failed', 'reason': 'y'}]),
        }
        for label, record in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    admin.validate_backup_status(record)


class ScheduledCoverageGuidanceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'runtime'
        (self.root / 'backups').mkdir(parents=True)
        (self.root / 'projects').mkdir()
        self.root = self.root.resolve()
        # The report enumerates the account's unit DIRECTORY, so the tests point it at
        # their own directory and install `beads-*backup*.service` files there.
        self.units = Path(self.temp.name) / 'user-units'
        self.units.mkdir()
        patcher = patch.object(admin, 'scheduled_backup_unit_dir', return_value=self.units)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.unit = self.units / 'beads-backup.service'

    def write_unit(self, *execstarts, name='beads-backup.service'):
        path = self.units / name
        path.write_text('\n'.join(
            ['[Unit]', 'Description=Backup', '', '[Service]', 'Type=oneshot', *execstarts]) + '\n',
            encoding='utf-8')
        return path

    def admin_line(self, root, *projects):
        # A systemd ExecStart line carries a POSIX path, so render the root with
        # forward slashes even when the test runs on Windows: shlex parses backslashes
        # as escapes and would mangle a native Windows path.
        return ('ExecStart=/usr/bin/python3 /opt/orchestra/admin.py --root %s backup %s'
                % (Path(root).as_posix(), ' '.join(projects)))

    def line(self, *projects):
        return self.admin_line(self.root, *projects)

    def wrapper_line(self, root=None, script='/opt/orchestra/longsync-wrapper.py'):
        return ('ExecStart=/usr/bin/python3 %s --root %s --project second'
                % (script, Path(root or self.root).as_posix()))

    def test_no_unit_reports_no_coverage_and_the_exact_line(self):
        covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertFalse(covered)
        self.assertIn('was found', message)
        self.assertIn('second', message)
        self.assertIn('backup --all', message)
        self.assertIn('ExecStart=', message)

    def test_unit_listing_only_other_projects_reports_this_project_missing(self):
        self.write_unit(self.line('first'))
        covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertFalse(covered)
        self.assertIn('cover only first', message)
        self.assertIn('do not include second', message)
        self.assertIn('durable form', message)

    def test_unit_listing_this_project_is_reported_as_needing_the_durable_form(self):
        self.write_unit(self.line('first', 'second'))
        covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertFalse(covered)
        self.assertIn('already include second', message)
        self.assertIn('first, second', message)
        self.assertIn('backup --all', message)

    def test_several_execstart_lines_are_all_considered(self):
        self.write_unit(self.line('first'), self.line('second'))
        covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertFalse(covered)
        self.assertIn('already include second', message)
        self.assertIn('first, second', message)

    def test_unit_using_all_is_reported_as_already_covering_every_project(self):
        self.write_unit(self.line('--all'))
        covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertTrue(covered)
        self.assertIn('cover every project', message)
        self.assertIn('no change is needed', message)
        self.assertIn('second', message)
        # The answer is about the unit files: drop-ins are stated as not inspected.
        self.assertIn('drop-ins', message)
        self.assertIn('not inspected', message)

    def test_a_unit_for_another_runtime_is_not_coverage(self):
        self.write_unit(self.admin_line('/srv/other', '--all'))
        covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertFalse(covered)
        self.assertIn('do not cover second', message)
        self.assertIn('do not back up this runtime', message)
        self.assertIn('backup --all', message)

    def test_an_unreadable_unit_is_reported_unconfirmed_not_fine(self):
        self.unit.write_text('[Service]\n', encoding='utf-8')
        with patch.object(admin.Path, 'read_text', side_effect=OSError('permission denied')):
            covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertFalse(covered)
        self.assertIn('could not be read', message)
        self.assertIn('second', message)

    def test_bare_backup_with_no_project_is_reported_as_no_coverage(self):
        self.write_unit('ExecStart=/usr/bin/python3 /opt/orchestra/admin.py --root %s backup'
                        % self.root.as_posix())
        covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertFalse(covered)
        self.assertIn('cover only no project', message)
        self.assertIn('do not include second', message)

    def test_execstart_line_names_this_runtime_and_the_durable_form(self):
        line = admin.scheduled_backup_execstart(self.root)
        self.assertTrue(line.startswith('ExecStart='))
        self.assertIn('--root %s backup --all' % self.root, line)

    def test_a_differently_named_backup_unit_is_read(self):
        self.write_unit(self.line('--all'), name='beads-example-backup.service')
        covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertTrue(covered)
        self.assertIn('beads-example-backup.service', message)

    def test_a_longsync_wrapper_covers_only_the_projects_it_names(self):
        # The live installations moved to a long-sync wrapper naming one project per
        # line (see kittrial-5bb.28). The wrapper is the timeout-safe path and must not
        # be replaced with `backup --all`, but it is NOT full coverage: a project added
        # later needs another line, so it must never be reported as "no change needed".
        self.write_unit(self.wrapper_line(), name='beads-example-backup.service')
        covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertFalse(covered)
        self.assertIn('long-sync wrapper', message)
        self.assertIn('beads-example-backup.service', message)
        self.assertIn('already include second', message)
        self.assertIn('name projects individually', message)
        self.assertNotIn('cover every project', message)
        self.assertNotIn('no change is needed', message)
        self.assertNotIn('backup --all', message)

    def test_a_wrapper_with_one_line_per_project_covers_only_those_projects(self):
        self.write_unit(
            'ExecStart=/usr/bin/python3 /opt/wrapper.py --root %s --project first\n'
            'ExecStart=/usr/bin/python3 /opt/wrapper.py --root %s --project second'
            % (self.root.as_posix(), self.root.as_posix()),
            name='beads-example-backup.service')
        covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertFalse(covered)
        self.assertIn('first, second', message)
        # A project added after the unit was installed is reported NOT covered.
        covered, message = admin.scheduled_backup_coverage(self.root, 'third')
        self.assertFalse(covered)
        self.assertIn('cover only first, second', message)
        self.assertIn('do not include third', message)
        self.assertIn('long-sync wrapper', message)
        self.assertNotIn('no change is needed', message)
        self.assertNotIn('backup --all', message)

    def test_a_wrapper_with_no_project_names_is_not_full_coverage(self):
        self.write_unit('ExecStart=/usr/bin/python3 /opt/report.py --root %s' % self.root.as_posix())
        covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertFalse(covered)
        self.assertIn('names no project', message)
        self.assertIn('second', message)
        self.assertNotIn('cover every project', message)
        self.assertNotIn('no change is needed', message)
        self.assertNotIn('backup --all', message)

    def test_a_wrapper_project_equals_form_is_parsed(self):
        self.write_unit(
            'ExecStart=/usr/bin/python3 /opt/wrapper.py --root=%s --project=second' % self.root.as_posix())
        covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertFalse(covered)
        self.assertIn('already include second', message)
        self.assertNotIn('no change is needed', message)

    def test_a_wrapper_and_a_named_unit_together_never_steer_off_the_wrapper(self):
        self.write_unit(self.wrapper_line(), name='beads-example-backup.service')
        self.write_unit(self.line('first'), name='beads-backup.service')
        covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertFalse(covered)
        self.assertIn('long-sync wrapper', message)
        self.assertNotIn('no change is needed', message)
        self.assertNotIn('Replace that project list', message)
        self.assertNotIn('backup --all', message)

    def test_a_wrapper_for_another_runtime_is_not_coverage_of_this_one(self):
        self.write_unit(self.wrapper_line(root='/srv/other'))
        covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertFalse(covered)
        self.assertIn('do not cover second', message)

    def test_named_lines_never_suggest_combining_projects_with_all(self):
        self.write_unit(self.line('first'))
        covered, message = admin.scheduled_backup_coverage(self.root, 'second')
        self.assertFalse(covered)
        self.assertIn('do not combine named projects with --all in one command', message)
        for candidate in message.splitlines():
            if candidate.strip().startswith('ExecStart=') and '--all' in candidate:
                self.assertNotIn('first', candidate)

    def test_add_project_prints_the_guidance_without_touching_the_unit(self):
        self.write_unit(self.line('first'))
        before = self.unit.read_text(encoding='utf-8')
        output = io.StringIO()
        with patch.object(admin, 'config', return_value={'port': 13317}), \
                patch.object(admin, 'run_bd', return_value=''), \
                patch.object(admin, 'backup_project'), contextlib.redirect_stdout(output):
            admin.add_project(self.root, 'newproject')
        text = output.getvalue()
        self.assertIn('Created project newproject', text)
        self.assertIn('do not include newproject', text)
        self.assertIn('backup --all', text)
        self.assertEqual(self.unit.read_text(encoding='utf-8'), before)

    def test_add_project_states_when_the_schedule_already_covers_every_project(self):
        self.write_unit(self.line('--all'))
        output = io.StringIO()
        with patch.object(admin, 'config', return_value={'port': 13317}), \
                patch.object(admin, 'run_bd', return_value=''), \
                patch.object(admin, 'backup_project'), contextlib.redirect_stdout(output):
            admin.add_project(self.root, 'newproject')
        text = output.getvalue()
        self.assertIn('cover every project', text)
        self.assertIn('no change is needed', text)

    def test_add_project_does_not_steer_off_a_longsync_wrapper(self):
        self.write_unit(self.wrapper_line(), name='beads-example-backup.service')
        output = io.StringIO()
        with patch.object(admin, 'config', return_value={'port': 13317}), \
                patch.object(admin, 'run_bd', return_value=''), \
                patch.object(admin, 'backup_project'), contextlib.redirect_stdout(output):
            admin.add_project(self.root, 'newproject')
        text = output.getvalue()
        self.assertIn('long-sync wrapper', text)
        self.assertIn('do not include newproject', text)
        self.assertNotIn('no change is needed', text)
        self.assertNotIn('backup --all', text)


class InterruptedRunAndCopyLockCase(unittest.TestCase):
    """P3: the stale staged snapshot, and the coordination-lock scope of backup-copy."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'runtime'
        (self.root / 'backups').mkdir(parents=True)
        (self.root / 'projects').mkdir()
        (self.root / 'deployment.private.json').write_text(json.dumps(
            {'port': 13317, 'unit': 'beads-example.service', 'password': 'test-only-password',
             'schema': 1}), encoding='utf-8')
        for name in ('alpha', 'beta'):
            make_project(self.root, name)
        self.flock = Mock()
        patcher = patch.dict(sys.modules, {
            'fcntl': types.SimpleNamespace(flock=self.flock, LOCK_EX=2)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.destination = Path(self.temp.name) / 'off-machine'

    def run_admin(self, *argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = 0
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                patch.object(admin, 'root_path', return_value=self.root), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                admin.main()
            except SystemExit as exit:
                code = exit.code if isinstance(exit.code, int) else 1
                if exit.code is not None and not isinstance(exit.code, int):
                    stderr.write(str(exit.code))
        return stdout.getvalue(), stderr.getvalue(), code

    def record(self):
        for name in ('alpha', 'beta'):
            write_pair(self.root, name)
        admin.write_backup_status(self.root, {
            'schema_version': 1, 'scope': 'all', 'generated_at': '2026-09-27T16:00:00Z',
            'status': 'complete',
            'projects': [{'name': name, 'status': 'complete',
                          'completed_at': '2026-09-27T16:00:00Z',
                          'pair': {'native': 'backups/' + name,
                                   'coordination': 'backups/' + name + '.coordination.json'}}
                         for name in ('alpha', 'beta')]})

    def test_a_stale_staging_snapshot_from_a_hard_kill_is_removed_at_the_start_of_a_run(self):
        write_pair(self.root, 'alpha')
        staging = admin.staged_journal_snapshot_path(self.root, 'alpha')
        staging.write_bytes(b'left behind by kill -9')
        Path(str(staging) + '.tmp').write_bytes(b'left behind by kill -9')
        keep = self.root / 'backups' / 'alpha' / 'chunk'
        keep.write_text('native bytes', encoding='utf-8')
        with patch.object(admin, 'native_backup_sync', return_value='Backup synced'):
            admin.backup_project(self.root, 'alpha')
        self.assertFalse(staging.exists())
        self.assertFalse(Path(str(staging) + '.tmp').exists())
        self.assertEqual(keep.read_text(encoding='utf-8'), 'native bytes')

    def test_a_stale_staging_snapshot_is_removed_even_when_the_run_fails(self):
        write_pair(self.root, 'alpha')
        staging = admin.staged_journal_snapshot_path(self.root, 'alpha')
        staging.write_bytes(b'left behind by kill -9')
        with patch.object(admin, 'native_backup_sync', side_effect=RuntimeError('sync failed')):
            with self.assertRaises(RuntimeError):
                admin.backup_project(self.root, 'alpha')
        self.assertFalse(staging.exists())

    def coordination_handle_recorder(self, handles):
        def fake_flock(handle, operation):
            if str(getattr(handle, 'name', '')).endswith('.coordination.lock'):
                handles.append(handle)
        return types.SimpleNamespace(flock=fake_flock, LOCK_EX=2)

    def test_the_pair_is_rechecked_and_the_sidecar_copied_under_the_coordination_lock(self):
        self.record()
        handles = []
        copied_under_lock = []
        real_copy = admin._atomic_copy

        def recording_copy(source, destination):
            if str(destination).endswith('.coordination.json'):
                copied_under_lock.append(any(not handle.closed for handle in handles))
            return real_copy(source, destination)

        with patch.dict(sys.modules, {'fcntl': self.coordination_handle_recorder(handles)}), \
                patch.object(admin, '_atomic_copy', side_effect=recording_copy):
            stdout, stderr, code = self.run_admin('backup-copy', str(self.destination))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(copied_under_lock, [True, True])

    def test_the_coordination_lock_is_released_before_the_native_copy(self):
        self.record()
        (self.root / 'backups' / 'alpha' / 'chunk').write_text('native bytes', encoding='utf-8')
        handles = []
        held_during_copy = []
        real_copytree = shutil.copytree

        def slow_copytree(source, target, **kwargs):
            held_during_copy.append(any(not handle.closed for handle in handles))
            return real_copytree(source, target, **kwargs)

        with patch.dict(sys.modules, {'fcntl': self.coordination_handle_recorder(handles)}), \
                patch('shutil.copytree', side_effect=slow_copytree):
            stdout, stderr, code = self.run_admin('backup-copy', str(self.destination))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(held_during_copy), 2)
        self.assertFalse(any(held_during_copy),
                         'the coordination lock was still held during the native copy')


if __name__ == '__main__':
    unittest.main()
