"""Timeout-safe native sync, the durable last-complete pair and the reference copy.

kittrial-5bb.39 rev2. ``bd backup sync`` carries a fixed client read timeout of about
ten seconds, which a large database cannot meet on a stalled server, and a failed run
used to leave only a ``pending`` sidecar behind. These tests cover the Dolt SQL-client
sync path, the last-complete copy that a failed or interrupted run cannot degrade, and
the gated reference off-machine copy helper.
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin


def make_project(root, name, server=True):
    """A project directory that carries the native marker (and server metadata)."""
    path = root / 'projects' / name
    (path / '.beads').mkdir(parents=True, exist_ok=True)
    metadata = ({'dolt_server_host': '127.0.0.1', 'dolt_server_port': 13317,
                 'dolt_server_user': 'root', 'dolt_database': name} if server else {})
    (path / '.beads' / 'metadata.json').write_text(json.dumps(metadata), encoding='utf-8')
    if server:
        (path / '.beads' / 'dolt-backup.json').write_text(
            json.dumps({'backup_name': 'default'}), encoding='utf-8')
    return path


def write_pair(root, name, status='complete', files=None):
    (root / 'backups' / name).mkdir(parents=True, exist_ok=True)
    (root / 'backups' / (name + '.coordination.json')).write_text(json.dumps(
        {'schema_version': 1, 'status': status, 'files': files or {}, 'operators': []}),
        encoding='utf-8')


class RuntimeCase(unittest.TestCase):
    """A disposable runtime root with the fake ``fcntl`` the POSIX lock code needs."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'runtime'
        (self.root / 'backups').mkdir(parents=True)
        (self.root / 'projects').mkdir()
        (self.root / 'deployment.private.json').write_text(json.dumps(
            {'port': 13317, 'unit': 'beads-example.service', 'password': 'test-only-password',
             'schema': 1}), encoding='utf-8')
        fake = types.SimpleNamespace(flock=Mock(), LOCK_EX=2)
        patcher = patch.dict(sys.modules, {'fcntl': fake})
        patcher.start()
        self.addCleanup(patcher.stop)

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


class NativeSyncCase(RuntimeCase):
    def test_the_sql_client_runs_the_sync_with_a_generous_explicit_timeout(self):
        make_project(self.root, 'alpha')
        seen = {}

        def fake_checked(command, **kwargs):
            seen['command'] = [str(item) for item in command]
            seen['kwargs'] = kwargs
            return subprocess.CompletedProcess(command, 0, stdout='Backup synced\n', stderr='')

        with patch.object(admin, 'checked', side_effect=fake_checked):
            output = admin.native_backup_sync(self.root, 'alpha')
        self.assertEqual(output, 'Backup synced\n')
        self.assertIn("CALL DOLT_BACKUP('sync', 'default')", seen['command'])
        self.assertIn('--use-db', seen['command'])
        self.assertIn('alpha', seen['command'])
        self.assertEqual(seen['kwargs']['timeout'], admin.BACKUP_SYNC_TIMEOUT)
        # The credential is supplied through the environment, never the command line.
        self.assertNotIn('test-only-password', ' '.join(seen['command']))
        self.assertEqual(seen['kwargs']['env']['DOLT_CLI_PASSWORD'], 'test-only-password')

    def test_a_project_without_server_metadata_keeps_the_existing_native_path(self):
        make_project(self.root, 'alpha', server=False)
        with patch.object(admin, 'run_bd', return_value='synced') as native:
            self.assertEqual(admin.native_backup_sync(self.root, 'alpha'), 'synced')
        native.assert_called_once_with(self.root, 'alpha', ['backup', 'sync'])

    def test_an_unusable_backup_name_is_refused_before_any_command(self):
        make_project(self.root, 'alpha')
        (self.root / 'projects' / 'alpha' / '.beads' / 'dolt-backup.json').write_text(
            json.dumps({'backup_name': "x'; DROP TABLE issues; --"}), encoding='utf-8')
        with patch.object(admin, 'checked') as checked:
            with self.assertRaises(ValueError):
                admin.native_backup_sync(self.root, 'alpha')
        checked.assert_not_called()

    def test_the_native_command_records_its_own_stderr_in_the_failure_reason(self):
        failure = subprocess.CalledProcessError(1, ['dolt', 'sql'],
                                                stderr='Error 1105: backup target is full')
        reason = admin.failure_reason(failure)
        self.assertIn('backup target is full', reason)
        self.assertNotIn('\n', reason)
        self.assertLessEqual(len(reason), 400)
        self.assertEqual(admin.failure_reason(RuntimeError('refused')), 'refused')


class LastCompletePairCase(RuntimeCase):
    def setUp(self):
        super().setUp()
        for name in ('alpha', 'beta'):
            make_project(self.root, name)
            (self.root / 'backups' / name).mkdir(exist_ok=True)

    def test_a_failed_sync_keeps_a_restorable_pair_and_is_recorded_failed(self):
        write_pair(self.root, 'alpha', files={'.merge-context.json': {'holder': 'prior'}})
        failure = subprocess.CalledProcessError(1, ['dolt', 'sql'],
                                                stderr='Error 1105: backup target is full')
        with patch.object(admin, 'native_backup_sync', side_effect=failure):
            stdout, stderr, code = self.run_admin('backup', '--all')
        self.assertNotEqual(code, 0)
        # (i) the previous complete pair is intact and still restorable
        self.assertEqual(admin.coordination_backup(self.root, 'alpha'),
                         {'.merge-context.json': {'holder': 'prior'}})
        self.assertEqual(admin.backup_pair_state(self.root, 'alpha'), (True, None))
        # (ii) the run is recorded failed, with the real native reason
        entry = {item['name']: item for item in admin.read_backup_status(self.root)['projects']}['alpha']
        self.assertEqual(entry['status'], 'failed')
        self.assertIn('backup target is full', entry['reason'])
        # (iii) a subsequent successful run completes normally
        with patch.object(admin, 'native_backup_sync', return_value='Backup synced'):
            stdout, stderr, code = self.run_admin('backup', '--all')
        self.assertEqual(code, 0, stderr)
        for name in ('alpha', 'beta'):
            self.assertEqual(admin.backup_pair_state(self.root, name), (True, None))

    def test_a_hard_interruption_still_restores_the_last_complete_pair(self):
        write_pair(self.root, 'alpha', files={'.merge-context.json': {'holder': 'prior'}})
        with patch.object(admin, 'native_backup_sync', return_value='Backup synced'):
            stdout, stderr, code = self.run_admin('backup', 'alpha')
        self.assertEqual(code, 0, stderr)
        aside = admin.last_complete_sidecar_path(self.root, 'alpha')
        self.assertEqual(json.loads(aside.read_text(encoding='utf-8'))['status'], 'complete')
        # A kill between the `pending` marker and the sync cannot run the in-process
        # restore, so the durable copy is what the restore path falls back to.
        (self.root / 'backups' / 'alpha.coordination.json').write_text(
            json.dumps({'schema_version': 1, 'status': 'pending'}), encoding='utf-8')
        self.assertEqual(admin.backup_pair_state(self.root, 'alpha')[0], False)
        self.assertEqual(admin.coordination_backup(self.root, 'alpha'), {})
        self.assertEqual(admin.coordination_operators(self.root, 'alpha'), [])

    def test_a_pending_sidecar_without_a_last_complete_copy_is_still_refused(self):
        write_pair(self.root, 'alpha', status='pending')
        with self.assertRaisesRegex(ValueError, 'Incomplete coordination backup'):
            admin.coordination_backup(self.root, 'alpha')

    def test_a_first_run_failure_leaves_the_honest_pending_marker(self):
        # Nothing to preserve: there was no previous complete pair for this project.
        failure = subprocess.CalledProcessError(1, ['dolt', 'sql'], stderr='refused')
        with patch.object(admin, 'native_backup_sync', side_effect=failure):
            stdout, stderr, code = self.run_admin('backup', 'alpha')
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads(
            (self.root / 'backups' / 'alpha.coordination.json').read_text(encoding='utf-8'))['status'],
            'pending')
        self.assertEqual(admin.backup_pair_state(self.root, 'alpha')[0], False)


class BackupCopyCase(RuntimeCase):
    def setUp(self):
        super().setUp()
        for name in ('alpha', 'beta'):
            make_project(self.root, name)
            write_pair(self.root, name)
        self.destination = Path(self.temp.name) / 'off-machine'

    def record(self, scope='all', names=('alpha', 'beta')):
        admin.write_backup_status(self.root, {
            'schema_version': 1, 'scope': scope, 'generated_at': '2026-09-27T16:00:00Z',
            'status': 'complete',
            'projects': [{'name': name, 'status': 'complete',
                          'completed_at': '2026-09-27T16:00:00Z',
                          'pair': {'native': 'backups/' + name,
                                   'coordination': 'backups/' + name + '.coordination.json'}}
                         for name in names]})

    def test_the_copy_refuses_an_incomplete_runtime_and_copies_nothing(self):
        self.record()
        (self.root / 'backups' / 'beta').rmdir()
        stdout, stderr, code = self.run_admin('backup-copy', str(self.destination))
        self.assertNotEqual(code, 0)
        self.assertIn('beta', stderr)
        self.assertIn('native backup directory is missing', stderr)
        self.assertEqual(stdout, '')
        self.assertFalse(self.destination.exists())

    def test_the_copy_refuses_a_project_initialized_after_the_run(self):
        self.record()
        make_project(self.root, 'gamma')
        stdout, stderr, code = self.run_admin('backup-copy', str(self.destination))
        self.assertNotEqual(code, 0)
        self.assertIn('gamma', stderr)
        self.assertFalse(self.destination.exists())

    def test_the_copy_copies_every_complete_pair_when_the_gate_passes(self):
        self.record()
        (self.root / 'backups' / 'alpha' / 'chunk').write_text('native bytes', encoding='utf-8')
        stdout, stderr, code = self.run_admin('backup-copy', str(self.destination))
        self.assertEqual(code, 0, stderr)
        self.assertEqual((self.destination / 'alpha' / 'chunk').read_text(encoding='utf-8'),
                         'native bytes')
        self.assertTrue((self.destination / 'beta').is_dir())
        for name in ('alpha', 'beta'):
            copied = json.loads((self.destination / (name + '.coordination.json')).read_text(
                encoding='utf-8'))
            self.assertEqual(copied['status'], 'complete')
            self.assertIn('Copied %s' % name, stdout)
        self.assertTrue((self.destination / admin.BACKUP_STATUS_NAME).is_file())
        self.assertIn('Copied 2 complete project pair(s)', stdout)

    def test_the_copy_refuses_a_destination_inside_the_runtime(self):
        self.record()
        stdout, stderr, code = self.run_admin('backup-copy', str(self.root / 'offmachine'))
        self.assertNotEqual(code, 0)
        self.assertIn('inside the runtime', stderr)
        self.assertFalse((self.root / 'offmachine').exists())

    def test_the_copy_refuses_a_relative_destination(self):
        self.record()
        stdout, stderr, code = self.run_admin('backup-copy', 'relative-copy')
        self.assertNotEqual(code, 0)
        self.assertIn('absolute', stderr)


if __name__ == '__main__':
    unittest.main()
