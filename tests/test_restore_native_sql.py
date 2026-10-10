"""restore-new restores through the Dolt SQL client (kittrial-5bb.82).

The restore-side twin of kittrial-5bb.39. ``bd backup restore`` carries the same fixed client
read timeout of about ten seconds as ``bd backup sync``: a restore drill on 2026-10-03 cut a
588 MB backup at exactly 10 s (``i/o timeout``, ``invalid connection``) while the same restore
through ``CALL DOLT_BACKUP('restore', ...)`` took under a minute. These tests cover the SQL
path, its explicit ceiling, its own-session process group and signal handling, the adoption of
the restored project identity bd would otherwise refuse with ``PROJECT IDENTITY MISMATCH``, the
bd path kept for a destination without server metadata, and that every other restore-new step
is unchanged.
"""
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import admin
from test_backup_native_sync import RuntimeCase, make_project, write_pair

SOURCE_ID = '04a84984-f0f6-41df-ba11-b7f8417cb912'
FRESH_ID = '11111111-2222-3333-4444-555555555555'


def make_destination(root, name, project_id=FRESH_ID, server=True):
    """A destination as ``add_project`` leaves it: initialized, own target, fresh identity."""
    path = make_project(root, name, server=server)
    metadata = json.loads((path / '.beads' / 'metadata.json').read_text(encoding='utf-8'))
    metadata.update({'database': 'dolt', 'backend': 'dolt', 'project_id': project_id})
    (path / '.beads' / 'metadata.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    if server:
        (path / '.beads' / 'dolt-backup.json').write_text(json.dumps(
            {'backup_name': 'default', 'backup_url': (root / 'backups' / name).resolve().as_uri()}),
            encoding='utf-8')
    return path


def identity_sql(project_id):
    """A fake ``admin.sql`` answering the ``_project_id`` read in CSV, as the client does."""
    def fake(root, query, password=None):
        if '_project_id' in query:
            return 'value\n' + (project_id + '\n' if project_id else '')
        raise AssertionError('unexpected statement: %s' % query)
    return fake


class FakeProcess:
    pid = 5151
    returncode = 0
    stdout = None
    stderr = None

    def __init__(self, seen):
        self.seen = seen

    def communicate(self, timeout=None):
        self.seen['timeout'] = timeout
        return '+--------+\n| status |\n+--------+\n| 0      |\n+--------+\n', ''

    def wait(self, timeout=None):
        return 0


class NativeRestoreCase(RuntimeCase):
    def setUp(self):
        super().setUp()
        (self.root / 'backups' / 'alpha').mkdir()

    def restore(self, seen, project_id=SOURCE_ID, handle=None):
        def fake_popen(command, **kwargs):
            seen['command'] = [str(item) for item in command]
            seen['kwargs'] = kwargs
            return FakeProcess(seen)
        with patch.object(admin.subprocess, 'Popen', side_effect=fake_popen), \
                patch.object(admin, 'sql', side_effect=identity_sql(project_id)), \
                patch.object(admin, 'run_bd') as native:
            report = admin.native_restore(self.root, 'alpha', 'beta', client=handle)
        native.assert_not_called()
        return report

    def test_the_sql_client_runs_the_restore_with_a_generous_explicit_timeout(self):
        make_destination(self.root, 'beta')
        seen = {}
        handle = admin.SyncClientHandle()
        report = self.restore(seen, handle=handle)
        url = admin.native_restore_url(self.root / 'backups' / 'alpha')
        if os.name == 'posix':
            # Exactly the URL `bd backup restore` builds: "file://" + absolute path.
            self.assertEqual(url, 'file://' + str((self.root / 'backups' / 'alpha').resolve()))
        self.assertIn("CALL DOLT_BACKUP('restore', '--force', '%s', 'beta')" % url, seen['command'])
        # Connected to the server, not to the database it replaces.
        self.assertNotIn('--use-db', seen['command'])
        self.assertEqual(seen['command'][0], str(self.root / 'bin' / 'dolt'))
        self.assertEqual(seen['timeout'], admin.RESTORE_TIMEOUT)
        self.assertGreaterEqual(admin.RESTORE_TIMEOUT, 1800)
        self.assertNotIn('timeout', seen['kwargs'])
        # The credential travels in the environment, never on the command line.
        self.assertNotIn('test-only-password', ' '.join(seen['command']))
        self.assertEqual(seen['kwargs']['env']['DOLT_CLI_PASSWORD'], 'test-only-password')
        # Own session and a recorded pid: an interrupted restore can stop the whole group.
        self.assertIs(seen['kwargs']['start_new_session'], True)
        self.assertEqual(handle.pid, 5151)
        self.assertTrue(handle.finished)
        self.assertIn('through the Dolt SQL client', report)
        self.assertNotIn('test-only-password', report)

    def test_the_restored_project_identity_is_adopted_into_the_metadata(self):
        path = make_destination(self.root, 'beta')
        metadata = path / '.beads' / 'metadata.json'
        before = json.loads(metadata.read_text(encoding='utf-8'))
        report = self.restore({})
        after = json.loads(metadata.read_text(encoding='utf-8'))
        self.assertEqual(after['project_id'], SOURCE_ID)
        # Every other key is kept exactly, in order.
        self.assertEqual(list(after), list(before))
        self.assertEqual({k: v for k, v in after.items() if k != 'project_id'},
                         {k: v for k, v in before.items() if k != 'project_id'})
        self.assertIn(SOURCE_ID, report)
        self.assertEqual([item.name for item in (path / '.beads').iterdir()
                          if item.name.endswith('.tmp')], [])

    def test_an_identity_already_in_sync_or_absent_leaves_the_metadata_untouched(self):
        for project_id, recorded in ((SOURCE_ID, SOURCE_ID), (None, FRESH_ID)):
            with self.subTest(project_id=project_id):
                path = make_destination(self.root, 'beta', project_id=recorded)
                metadata = path / '.beads' / 'metadata.json'
                before = metadata.read_bytes()
                report = self.restore({}, project_id=project_id)
                self.assertEqual(metadata.read_bytes(), before)
                self.assertNotIn('Adopted', report)

    @unittest.skipUnless(os.name == 'posix', 'POSIX file modes are required')
    def test_the_metadata_rewrite_keeps_the_file_mode(self):
        path = make_destination(self.root, 'beta')
        metadata = path / '.beads' / 'metadata.json'
        os.chmod(metadata, 0o640)
        self.restore({})
        self.assertEqual(metadata.stat().st_mode & 0o777, 0o640)

    def test_a_destination_without_server_metadata_keeps_the_bd_path(self):
        make_destination(self.root, 'beta', server=False)
        with patch.object(admin, 'run_bd', return_value='restored') as native, \
                patch.object(admin, 'spawn_sync_client') as spawn, \
                patch.object(admin, 'sql') as statement:
            self.assertEqual(admin.native_restore(self.root, 'alpha', 'beta'), 'restored')
        native.assert_called_once_with(
            self.root, 'beta', ['backup', 'restore', str(self.root / 'backups' / 'alpha'), '--force'])
        spawn.assert_not_called()
        statement.assert_not_called()

    def test_an_unusable_database_name_is_refused_before_any_command(self):
        path = make_destination(self.root, 'beta')
        metadata = json.loads((path / '.beads' / 'metadata.json').read_text(encoding='utf-8'))
        metadata['dolt_database'] = "beta'; DROP DATABASE alpha; --"
        (path / '.beads' / 'metadata.json').write_text(json.dumps(metadata), encoding='utf-8')
        with patch.object(admin, 'spawn_sync_client') as spawn, patch.object(admin, 'run_bd') as native:
            with self.assertRaisesRegex(ValueError, 'unusable Dolt database name'):
                admin.native_restore(self.root, 'alpha', 'beta')
        spawn.assert_not_called()
        native.assert_not_called()

    def test_a_backup_path_that_needs_sql_quoting_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'cannot be named'):
            admin.native_restore_url(self.root / "it's")
        self.assertTrue(admin.native_restore_url(self.root / 'backups' / 'alpha').startswith('file:'))

    def test_a_failed_client_is_reported_and_the_identity_is_not_adopted(self):
        path = make_destination(self.root, 'beta')
        before = (path / '.beads' / 'metadata.json').read_bytes()
        failure = subprocess.CalledProcessError(1, ['dolt'], stderr='Error 1105: no such backup')
        with patch.object(admin, 'spawn_sync_client', side_effect=failure), \
                patch.object(admin, 'sql') as statement:
            with self.assertRaises(subprocess.CalledProcessError):
                admin.native_restore(self.root, 'alpha', 'beta')
        statement.assert_not_called()
        self.assertEqual((path / '.beads' / 'metadata.json').read_bytes(), before)

    def fake_client(self, body):
        """A fake ``bin/dolt`` that spawns a child and waits, so the group can be observed."""
        pids = self.root / 'restore.pids'
        script = self.root / 'bin' / 'dolt'
        script.parent.mkdir(exist_ok=True)
        script.write_text('#!/bin/sh\n%s &\necho "$$ $!" > %s\nwait\n' % (body, pids),
                          encoding='utf-8')
        script.chmod(0o755)
        return pids

    def assert_group_stopped(self, pids):
        self.assertTrue(pids.is_file(), 'the fake restore client never started')
        client_pid, child_pid = (int(value) for value in pids.read_text().split())
        self.assertNotEqual(client_pid, os.getpid())

        def alive(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            return True

        for _ in range(100):
            if not alive(client_pid) and not alive(child_pid):
                break
            time.sleep(0.05)
        self.assertFalse(alive(client_pid), 'the restore client outlived the restore')
        self.assertFalse(alive(child_pid), 'a child of the restore client outlived the restore')

    @unittest.skipUnless(hasattr(os, 'killpg'), 'POSIX process groups are required')
    def test_a_sigterm_during_the_restore_kills_the_client_group(self):
        make_destination(self.root, 'beta')
        pids = self.fake_client('sleep 30')

        never_started = []

        def terminate_when_running():
            for _ in range(600):
                if pids.is_file() and pids.read_text().strip():
                    break
                time.sleep(0.05)
            else:
                # Stop anyway, so the test fails at once and says why, rather than waiting
                # for the client's 30 s sleep (kittrial-5bb.132).
                never_started.append(True)
            time.sleep(0.3)
            os.kill(os.getpid(), signal.SIGTERM)

        previous = signal.getsignal(signal.SIGTERM)
        killer = threading.Thread(target=terminate_when_running)
        killer.start()
        try:
            with patch.object(admin, 'sql') as statement:
                with self.assertRaises(admin.TerminatedBySignal):
                    admin.native_restore(self.root, 'alpha', 'beta')
        finally:
            killer.join(40)
        self.assertEqual(never_started, [], 'the fake client did not start within 30 s')
        statement.assert_not_called()
        self.assert_group_stopped(pids)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    @unittest.skipUnless(hasattr(os, 'killpg'), 'POSIX process groups are required')
    def test_the_explicit_ceiling_stops_the_client_group(self):
        make_destination(self.root, 'beta')
        pids = self.fake_client('sleep 30')
        # 5 s, not 1: the fake client must fork and record its pids before the ceiling, which
        # a loaded runner can take more than a second to do (kittrial-5bb.132). Its child
        # sleeps 30 s, so the ceiling is still what stops it.
        with patch.object(admin, 'RESTORE_TIMEOUT', 5), patch.object(admin, 'sql') as statement:
            with self.assertRaises(subprocess.TimeoutExpired):
                admin.native_restore(self.root, 'alpha', 'beta')
        statement.assert_not_called()
        self.assert_group_stopped(pids)


class RestoreNewCommandCase(RuntimeCase):
    """The command keeps every other step and uses the SQL path for a server project."""

    def setUp(self):
        super().setUp()
        write_pair(self.root, 'alpha', files={'.merge-context.json': {'holder': 'prior'},
                                              '.sessions.json': {'schema_version': 1, 'records': {}}})
        self.calls = []
        self.statements = []

    def fake_add_project(self, root, name, requirements_default=True):
        self.calls.append(('add_project', name))
        make_destination(root, name)

    def fake_run_bd(self, root, name, args):
        self.calls.append(('bd', name, list(args)))
        return 'ok'

    def fake_spawn(self, command, handle, **kwargs):
        self.calls.append(('sql-restore', [str(item) for item in command][-1]))
        handle.pid = 77
        handle.finished = True
        return ''

    def run_restore(self, *extra, spawn=None):
        with patch.object(admin, 'add_project', side_effect=self.fake_add_project), \
                patch.object(admin, 'run_bd', side_effect=self.fake_run_bd), \
                patch.object(admin, 'spawn_sync_client', side_effect=spawn or self.fake_spawn), \
                patch.object(admin, 'sql', side_effect=identity_sql(SOURCE_ID)):
            return self.run_admin('restore-new', 'alpha', 'beta', *extra)

    def test_every_way_restore_new_stops_prints_the_notice(self):
        # kittrial-5bb.85 review 01a10219: Ctrl-C in the add-project step, and a destination
        # that disappears after the native restore, each end with the notice.
        def interrupted(root, name, requirements_default=True):
            make_destination(root, name)
            raise KeyboardInterrupt()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(KeyboardInterrupt), \
                patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'restore-new', 'alpha', 'beta']), \
                patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'add_project', side_effect=interrupted), \
                patch.object(admin, 'run_bd', side_effect=self.fake_run_bd), \
                contextlib.redirect_stdout(io.StringIO()):
            admin.main()
        notice = stderr.getvalue()
        self.assertIn('restore-new did not complete: the restore was interrupted during the add-project step',
                      notice)
        self.assertIn('retire-project beta', notice)

        import shutil
        shutil.rmtree(self.root / 'projects' / 'beta')

        def vanished(root, name, args):
            if args[:2] == ['backup', 'init']:
                shutil.rmtree(self.root / 'projects' / 'beta')         # moved away under the restore
                raise FileNotFoundError(2, 'No such file or directory')
            return 'ok'
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(FileNotFoundError), \
                patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'restore-new', 'alpha', 'beta']), \
                patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'add_project', side_effect=self.fake_add_project), \
                patch.object(admin, 'run_bd', side_effect=vanished), \
                patch.object(admin, 'spawn_sync_client', side_effect=self.fake_spawn), \
                patch.object(admin, 'sql', side_effect=identity_sql(SOURCE_ID)), \
                contextlib.redirect_stdout(io.StringIO()):
            admin.main()
        notice = stderr.getvalue()
        self.assertIn('the re-point and coordination step failed', notice)
        self.assertIn('The directory of project beta is no longer there', notice)

    def test_restore_new_uses_the_sql_path_then_every_existing_step(self):
        stdout, stderr, code = self.run_restore()
        self.assertEqual(code, 0, stderr)
        self.assertEqual([call[0] for call in self.calls], ['add_project', 'sql-restore', 'bd', 'bd', 'bd'])
        self.assertIn("CALL DOLT_BACKUP('restore', '--force',", self.calls[1][1])
        # No `bd backup restore`; the re-point of the clone's own target is kept, then the
        # merge slot is provisioned (kittrial-5bb.202 item 3: a legacy source may lack it).
        self.assertEqual(self.calls[2], ('bd', 'beta', ['backup', 'init', str(self.root / 'backups' / 'beta')]))
        self.assertEqual(self.calls[3], ('bd', 'beta', ['merge-slot', 'check', '--json']))
        self.assertEqual(self.calls[4], ('bd', 'beta', ['merge-slot', 'create', '--json']))
        destination = self.root / 'projects' / 'beta'
        self.assertEqual(json.loads((destination / '.beads' / 'metadata.json').read_text(
            encoding='utf-8'))['project_id'], SOURCE_ID)
        # The sidecar (including .sessions.json) and the journal notice are restored as before.
        self.assertEqual(json.loads((destination / '.merge-context.json').read_text(encoding='utf-8')),
                         {'holder': 'prior'})
        self.assertEqual(json.loads((destination / '.sessions.json').read_text(encoding='utf-8')),
                         {'schema_version': 1, 'records': {}})
        self.assertIn('through the Dolt SQL client', stdout)
        self.assertIn('Backup has no operation-journal snapshot', stdout)
        self.assertIn('Restored only into the newly created project', stdout)
        self.assertNotIn('test-only-password', stdout + stderr)

    def test_operators_not_restored_notice_and_restore_verifiers_are_kept(self):
        write_pair(self.root, 'alpha')
        bundle = self.root / 'backups' / 'alpha.coordination.json'
        record = json.loads(bundle.read_text(encoding='utf-8'))
        record.update({'operators': ['ops-recorded'], 'verifiers': ['verifier-recorded']})
        bundle.write_text(json.dumps(record), encoding='utf-8')
        stdout, stderr, code = self.run_restore('--restore-verifiers')
        self.assertEqual(code, 0, stderr)
        self.assertIn('NOT restored', stdout)
        self.assertIn('ops-recorded', stdout)
        self.assertIn('Re-granted capability verifiers from the backup (--restore-verifiers): verifier-recorded',
                      stdout)
        self.assertEqual(admin.verifiers(self.root), frozenset({'verifier-recorded'}))

    def test_a_manifest_mismatch_still_prints_the_restore_warning(self):
        with patch.object(admin, 'native_backup_change', return_value=('changed', 'changed for the test')):
            stdout, stderr, code = self.run_restore()
        self.assertEqual(code, 0, stderr)
        self.assertIn('WARNING: changed for the test', stdout)
        self.assertIn('sql-restore', [call[0] for call in self.calls])

    def test_a_populated_destination_is_refused_before_any_native_step(self):
        (self.root / 'projects' / 'beta').mkdir()
        (self.root / 'projects' / 'beta' / 'keep.txt').write_text('mine', encoding='utf-8')
        with patch.object(admin, 'run_bd') as native, patch.object(admin, 'spawn_sync_client') as spawn:
            stdout, stderr, code = self.run_admin_unpatched_add_project()
        native.assert_not_called()
        spawn.assert_not_called()
        self.assertIsInstance(self.raised, ValueError)
        self.assertIn('Project already exists', str(self.raised))
        self.assertEqual((self.root / 'projects' / 'beta' / 'keep.txt').read_text(encoding='utf-8'), 'mine')

    def run_admin_unpatched_add_project(self):
        self.raised = None
        try:
            return self.run_admin('restore-new', 'alpha', 'beta')
        except ValueError as error:
            self.raised = error
            return '', '', 1

    def test_an_interrupted_restore_leaves_a_clear_error_and_writes_nothing_else(self):
        failures = (subprocess.TimeoutExpired(['dolt'], admin.RESTORE_TIMEOUT),
                    admin.TerminatedBySignal(15),
                    subprocess.CalledProcessError(1, ['dolt'], stderr='invalid connection'))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.calls = []
                destination = self.root / 'projects' / 'beta'
                if destination.exists():
                    import shutil
                    shutil.rmtree(destination)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(type(failure)):
                        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root),
                                                        'restore-new', 'alpha', 'beta']), \
                                patch.object(admin, 'root_path', return_value=self.root), \
                                patch.object(admin, 'add_project', side_effect=self.fake_add_project), \
                                patch.object(admin, 'run_bd', side_effect=self.fake_run_bd), \
                                patch.object(admin, 'spawn_sync_client', side_effect=failure), \
                                patch.object(admin, 'sql', side_effect=identity_sql(SOURCE_ID)), \
                                contextlib.redirect_stdout(io.StringIO()):
                            admin.main()
                notice = stderr.getvalue()
                self.assertIn('restore-new did not complete', notice)
                self.assertIn('partial restore', notice)
                self.assertIn('another unused destination', notice)
                # No re-point, no sidecar, no identity adoption after a failed native step:
                # only the read that tells an empty destination from a partial one.
                self.assertEqual([call[0] for call in self.calls], ['add_project', 'bd'])
                self.assertEqual(self.calls[1][2][:2], ['list', '--all'])
                self.assertFalse((destination / '.merge-context.json').exists())
                self.assertEqual(json.loads((destination / '.beads' / 'metadata.json').read_text(
                    encoding='utf-8'))['project_id'], FRESH_ID)

    def test_the_failure_notice_names_the_cause(self):
        self.assertIn('ceiling', admin.restore_failure_notice(
            'beta', subprocess.TimeoutExpired(['dolt'], 1)))
        self.assertIn('interrupted', admin.restore_failure_notice('beta', admin.TerminatedBySignal(15)))
        self.assertIn('interrupted', admin.restore_failure_notice('beta', KeyboardInterrupt()))
        self.assertIn('failed', admin.restore_failure_notice('beta', ValueError('x')))


if __name__ == '__main__':
    unittest.main()
