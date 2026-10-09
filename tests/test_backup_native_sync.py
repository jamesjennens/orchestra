"""Timeout-safe native sync, the durable last-complete pair and the reference copy.

kittrial-5bb.39 rev3/rev4. ``bd backup sync`` carries a fixed client read timeout of about
ten seconds, which a large database cannot meet on a stalled server, and a failed run
used to leave only a ``pending`` sidecar behind. These tests cover the Dolt SQL-client
sync path, the own-session process group that lets an interrupted run stop the client,
the native-backup manifest a restore checks before pairing a previous generation, the
last-complete copy a failed or interrupted run cannot degrade, the operation-journal
snapshot that must stay in the same generation as that pair, and the gated reference
off-machine copy helper (which stages, locks and replaces a destination instead of
merging into it).
"""
import contextlib
import io
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
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


def write_live_journal(path, operation_ids):
    """A minimal live operation journal (the schema ``_check_journal_database`` wants)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            'CREATE TABLE operations(operation_id TEXT PRIMARY KEY, state TEXT)')
        connection.execute('CREATE TABLE meta(key TEXT)')
        connection.executemany('INSERT INTO operations VALUES(?,?)',
                               [(item, 'committed') for item in operation_ids])
        connection.commit()
    finally:
        connection.close()


def journal_ids(path):
    if not Path(path).is_file():
        return None
    connection = sqlite3.connect(str(path))
    try:
        return {row[0] for row in connection.execute('SELECT operation_id FROM operations')}
    finally:
        connection.close()


def root_spellings(path):
    """Two spellings of the same directory, or None when the platform has only one.

    A Windows runner's temp directory has an 8.3 short name (``C:/Users/RUNNER~1``)
    as well as its long form (``C:/Users/runneradmin``). A containment check that
    compares literal paths accepts a destination inside the runtime when the root is
    passed with one spelling and the destination resolves to the other - which is
    how a copy destination inside the runtime reached the copy on CI. POSIX has no
    short names, so a directory symlink alias supplies the second spelling. Returns
    ``(short, long)`` or None where the platform offers only one spelling.
    """
    path = Path(path)
    if os.name == 'nt':
        import ctypes

        def resolved(function):
            buffer = ctypes.create_unicode_buffer(32768)
            if not function(str(path), buffer, len(buffer)):
                return None
            return Path(buffer.value)

        short = resolved(ctypes.windll.kernel32.GetShortPathNameW)
        long = resolved(ctypes.windll.kernel32.GetLongPathNameW)
        if short is None or long is None:
            return None
        if os.path.normcase(str(short)) == os.path.normcase(str(long)):
            return None
        return short, long
    alias = path.parent / (path.name + '-alias')
    if not alias.is_symlink():
        try:
            alias.symlink_to(path, target_is_directory=True)
        except (OSError, NotImplementedError):
            return None
    return alias, path.resolve()


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
        # ``restore-new`` runs ``add-project``, which takes the creation lock too
        # (kittrial-5bb.176), so the stand-in needs the whole fcntl surface
        # ``http_authority.file_lock`` uses.
        fake = types.SimpleNamespace(flock=Mock(), LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        self.fcntl = fake
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

        class FakeProcess:
            pid = 4242
            returncode = 0
            stdout = None
            stderr = None

            def communicate(self, timeout=None):
                seen['timeout'] = timeout
                return 'Backup synced\n', ''

            def wait(self, timeout=None):
                return 0

        def fake_popen(command, **kwargs):
            seen['command'] = [str(item) for item in command]
            seen['kwargs'] = kwargs
            return FakeProcess()

        handle = admin.SyncClientHandle()
        with patch.object(admin.subprocess, 'Popen', side_effect=fake_popen):
            output = admin.native_backup_sync(self.root, 'alpha', client=handle)
        self.assertEqual(output, 'Backup synced\n')
        self.assertIn("CALL DOLT_BACKUP('sync', 'default')", seen['command'])
        self.assertIn('--use-db', seen['command'])
        self.assertIn('alpha', seen['command'])
        # The ceiling is the checked()-style communicate() timeout, not a Popen kwarg.
        self.assertEqual(seen['timeout'], admin.BACKUP_SYNC_TIMEOUT)
        self.assertNotIn('timeout', seen['kwargs'])
        # The credential is supplied through the environment, never the command line.
        self.assertNotIn('test-only-password', ' '.join(seen['command']))
        self.assertEqual(seen['kwargs']['env']['DOLT_CLI_PASSWORD'], 'test-only-password')
        # The client owns its own session and its pid is recorded for the caller, which is
        # what lets an interrupted run kill the whole group instead of only one process.
        self.assertIs(seen['kwargs']['start_new_session'], True)
        self.assertEqual(handle.pid, 4242)
        self.assertTrue(handle.finished)

    def test_a_project_without_server_metadata_keeps_the_existing_native_path(self):
        make_project(self.root, 'alpha', server=False)
        with patch.object(admin, 'run_bd', return_value='synced') as native:
            self.assertEqual(admin.native_backup_sync(self.root, 'alpha'), 'synced')
        native.assert_called_once_with(self.root, 'alpha', ['backup', 'sync'])

    def test_an_unusable_backup_name_is_refused_before_any_command(self):
        make_project(self.root, 'alpha')
        (self.root / 'projects' / 'alpha' / '.beads' / 'dolt-backup.json').write_text(
            json.dumps({'backup_name': "x'; DROP TABLE issues; --"}), encoding='utf-8')
        with patch.object(admin, 'spawn_sync_client') as spawn:
            with self.assertRaises(ValueError):
                admin.native_backup_sync(self.root, 'alpha')
        spawn.assert_not_called()

    def test_a_clone_recording_another_projects_backup_target_is_refused(self):
        # kittrial-5bb.49: `restore-new source clone` leaves the clone's
        # .beads/dolt-backup.json naming backups/source, so a backup of the clone would
        # sync it into the SOURCE project's directory. Refuse before any native command.
        make_project(self.root, 'alphan')
        (self.root / 'projects' / 'alphan' / '.beads' / 'dolt-backup.json').write_text(
            json.dumps({'backup_name': 'default',
                        'backup_url': (self.root / 'backups' / 'alpha').resolve().as_uri()}),
            encoding='utf-8')
        with patch.object(admin, 'spawn_sync_client') as spawn, \
                patch.object(admin, 'run_bd') as native:
            with self.assertRaisesRegex(
                    ValueError, re.escape(str((self.root / 'backups' / 'alphan').resolve()))):
                admin.native_backup_sync(self.root, 'alphan')
        spawn.assert_not_called()
        native.assert_not_called()

    def test_a_remote_or_malformed_backup_url_is_refused_too(self):
        make_project(self.root, 'alpha')
        for url in ('https://doltremoteapi.dolthub.com/user/repo',
                    'file://elsewhere/backups/alpha',
                    'backups/not-alpha'):
            with self.subTest(url=url):
                (self.root / 'projects' / 'alpha' / '.beads' / 'dolt-backup.json').write_text(
                    json.dumps({'backup_name': 'default', 'backup_url': url}), encoding='utf-8')
                with self.assertRaisesRegex(
                        ValueError, re.escape(str((self.root / 'backups' / 'alpha').resolve()))):
                    admin.native_backup_sync(self.root, 'alpha')

    def test_backup_refuses_a_foreign_target_before_the_pending_marker(self):
        make_project(self.root, 'alphan')
        (self.root / 'projects' / 'alphan' / '.beads' / 'dolt-backup.json').write_text(
            json.dumps({'backup_name': 'default',
                        'backup_url': (self.root / 'backups' / 'alpha').resolve().as_uri()}),
            encoding='utf-8')
        with patch.object(admin, 'native_backup_sync') as sync:
            with self.assertRaisesRegex(
                    ValueError, re.escape(str((self.root / 'backups' / 'alphan').resolve()))):
                admin.backup_project(self.root, 'alphan')
        sync.assert_not_called()
        self.assertFalse((self.root / 'backups' / 'alphan.coordination.json').exists())

    def test_a_project_recording_its_own_target_still_backs_up(self):
        make_project(self.root, 'alpha')
        (self.root / 'projects' / 'alpha' / '.beads' / 'dolt-backup.json').write_text(
            json.dumps({'backup_name': 'default',
                        'backup_url': (self.root / 'backups' / 'alpha').resolve().as_uri()}),
            encoding='utf-8')
        with patch.object(admin, 'native_backup_sync', return_value='synced') as sync:
            self.assertEqual(admin.backup_project(self.root, 'alpha'), 'synced')
        sync.assert_called_once()

    def test_a_project_with_no_recorded_target_keeps_the_pre_existing_path(self):
        # A project that records no backup_url (legacy, or the make_project fixture every
        # other test uses) is not refused: the guard only refuses a recorded mismatch.
        make_project(self.root, 'alpha')
        self.assertIsNone(admin.project_backup_record(self.root, 'alpha').get('backup_url'))
        admin.validate_backup_target(self.root, 'alpha')

    def test_the_native_command_records_its_own_stderr_in_the_failure_reason(self):
        failure = subprocess.CalledProcessError(1, ['dolt', 'sql'],
                                                stderr='Error 1105: backup target is full')
        reason = admin.failure_reason(failure)
        self.assertIn('backup target is full', reason)
        self.assertNotIn('\n', reason)
        self.assertLessEqual(len(reason), 400)
        self.assertEqual(admin.failure_reason(RuntimeError('refused')), 'refused')

    def test_the_client_is_not_killed_again_once_it_has_exited(self):
        # A successful run must not signal a pid the OS may have recycled: the handle is
        # marked finished by spawn_sync_client once the child has been waited for.
        handle = admin.SyncClientHandle()
        handle.pid = os.getpid()
        handle.finished = True
        admin.terminate_process_group(handle)
        self.assertEqual(handle.pid, os.getpid())

    @unittest.skipUnless(hasattr(os, 'getpgrp') and hasattr(os, 'getpgid'),
                         'POSIX process groups are required')
    def test_a_client_still_in_our_own_group_is_never_signalled(self):
        # Fail-safe: if the own-session start had not taken effect, killing "the group"
        # would kill the kit itself, so the helper refuses a group it shares.
        handle = admin.SyncClientHandle()
        handle.pid = os.getpid()
        handle.finished = False
        admin.terminate_process_group(handle)
        self.assertIsNone(handle.pid)


class BackupRepointCase(RuntimeCase):
    """kittrial-5bb.51: repair a mis-pointed clone, and make URL resolution cwd-independent."""

    def own_url(self, name):
        return (self.root / 'backups' / name).resolve().as_uri()

    def test_a_relative_bare_url_resolves_against_the_project_not_the_cwd(self):
        make_project(self.root, 'alpha')
        (self.root / 'backups' / 'alpha').mkdir(exist_ok=True)
        record = self.root / 'projects' / 'alpha' / '.beads' / 'dolt-backup.json'
        elsewhere = Path(self.temp.name) / 'elsewhere'
        elsewhere.mkdir()
        previous = os.getcwd()
        os.chdir(elsewhere)
        try:
            # Against the project directory this is the project's own target; against the
            # cwd it would be <elsewhere>/backups/alpha, so the old code refused here.
            record.write_text(json.dumps({'backup_name': 'default',
                                          'backup_url': '../../backups/alpha'}), encoding='utf-8')
            admin.validate_backup_target(self.root, 'alpha')
            # A relative path that points inside the project rather than at its backup is
            # refused no matter where the command runs.
            record.write_text(json.dumps({'backup_name': 'default',
                                          'backup_url': 'backups/alpha'}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'backups/alpha'):
                admin.validate_backup_target(self.root, 'alpha')
        finally:
            os.chdir(previous)

    def test_the_refusal_names_the_backup_repoint_command(self):
        # The plain `bd backup init DIR` advice fails without the kit environment (the
        # reviewer's Error 1045), so the message must name the operator command instead.
        make_project(self.root, 'alpha')
        (self.root / 'projects' / 'alpha' / '.beads' / 'dolt-backup.json').write_text(
            json.dumps({'backup_name': 'default',
                        'backup_url': (self.root / 'backups' / 'pilotone').as_uri()}),
            encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'admin.py backup-repoint alpha'):
            admin.validate_backup_target(self.root, 'alpha')

    def test_backup_repoint_runs_init_under_the_kit_env_and_verifies_both_records(self):
        make_project(self.root, 'alpha')
        record = self.root / 'projects' / 'alpha' / '.beads' / 'dolt-backup.json'

        def native(root, name, args):
            record.write_text(json.dumps({'backup_name': 'default',
                                          'backup_url': self.own_url(name)}), encoding='utf-8')
            return 'Backup destination configured'

        with patch.object(admin, 'run_bd', side_effect=native) as run, \
                patch.object(admin, 'sql',
                             return_value='name,url\ndefault,%s\n' % self.own_url('alpha')):
            report = admin.repoint_backup(self.root, 'alpha')
        run.assert_called_once_with(self.root, 'alpha',
                                    ['backup', 'init',
                                     str((self.root / 'backups' / 'alpha').resolve())])
        self.assertEqual(report['row_checked'], True)
        self.assertEqual(report['backup_url'], self.own_url('alpha'))
        self.assertEqual(report['row_url'], self.own_url('alpha'))

    def test_backup_repoint_refuses_when_the_db_row_still_names_the_source(self):
        make_project(self.root, 'alpha')
        record = self.root / 'projects' / 'alpha' / '.beads' / 'dolt-backup.json'

        def native(root, name, args):
            record.write_text(json.dumps({'backup_name': 'default',
                                          'backup_url': self.own_url(name)}), encoding='utf-8')
            return 'configured'

        with patch.object(admin, 'run_bd', side_effect=native), \
                patch.object(admin, 'sql', return_value=(
                    'name,url\ndefault,%s\n' % (self.root / 'backups' / 'pilotone').as_uri())):
            with self.assertRaisesRegex(ValueError, 'dolt_backups row'):
                admin.repoint_backup(self.root, 'alpha')

    def test_backup_repoint_refuses_when_the_file_still_names_the_source(self):
        make_project(self.root, 'alpha')
        record = self.root / 'projects' / 'alpha' / '.beads' / 'dolt-backup.json'

        def native(root, name, args):
            record.write_text(json.dumps({
                'backup_name': 'default',
                'backup_url': (self.root / 'backups' / 'pilotone').as_uri()}), encoding='utf-8')
            return 'configured'

        with patch.object(admin, 'run_bd', side_effect=native), \
                patch.object(admin, 'sql') as query:
            with self.assertRaisesRegex(ValueError, 'dolt-backup.json'):
                admin.repoint_backup(self.root, 'alpha')
        query.assert_not_called()

    def test_backup_repoint_is_honest_when_the_row_cannot_be_read(self):
        # No loopback coordinates: `bd backup init` still runs, but the row cannot be
        # checked and the report says so instead of claiming a verification.
        make_project(self.root, 'alpha', server=False)
        record = self.root / 'projects' / 'alpha' / '.beads' / 'dolt-backup.json'

        def native(root, name, args):
            record.write_text(json.dumps({'backup_name': 'default',
                                          'backup_url': self.own_url(name)}), encoding='utf-8')
            return 'configured'

        with patch.object(admin, 'run_bd', side_effect=native), \
                patch.object(admin, 'sql') as query:
            report = admin.repoint_backup(self.root, 'alpha')
        query.assert_not_called()
        self.assertEqual(report['row_checked'], False)
        self.assertIsNone(report['row_url'])

    def test_backup_repoint_refuses_an_uninitialized_project(self):
        with patch.object(admin, 'run_bd') as run:
            with self.assertRaisesRegex(ValueError, 'Unknown/uninitialized'):
                admin.repoint_backup(self.root, 'alpha')
        run.assert_not_called()

    def test_the_backup_repoint_command_prints_the_verified_report(self):
        make_project(self.root, 'alpha')
        record = self.root / 'projects' / 'alpha' / '.beads' / 'dolt-backup.json'

        def native(root, name, args):
            record.write_text(json.dumps({'backup_name': 'default',
                                          'backup_url': self.own_url(name)}), encoding='utf-8')
            return 'configured'

        with patch.object(admin, 'run_bd', side_effect=native), \
                patch.object(admin, 'sql',
                             return_value='name,url\ndefault,%s\n' % self.own_url('alpha')):
            stdout, stderr, code = self.run_admin('backup-repoint', 'alpha')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)['row_checked'], True)


class SyncClientHandleCase(RuntimeCase):
    def test_spawn_records_the_pid_and_returns_the_captured_stdout(self):
        handle = admin.SyncClientHandle()
        output = admin.spawn_sync_client(
            [sys.executable, '-c', 'print("sync output")'], handle)
        self.assertIn('sync output', output)
        self.assertIsNotNone(handle.pid)
        self.assertTrue(handle.finished)
        self.assertEqual(handle.process.returncode, 0)

    def test_a_nonzero_client_is_reported_like_checked_would(self):
        handle = admin.SyncClientHandle()
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            admin.spawn_sync_client(
                [sys.executable, '-c', 'import sys; sys.stderr.write("nope"); sys.exit(3)'], handle)
        self.assertEqual(caught.exception.returncode, 3)
        self.assertIn('nope', caught.exception.stderr)
        self.assertTrue(handle.finished)

    @unittest.skipUnless(os.name == 'posix',
                         'POSIX signal delivery is required: on Windows hasattr(signal, "SIGTERM") '
                         'is true but os.kill(pid, SIGTERM) is TerminateProcess and kills the run')
    def test_sigterm_raises_inside_the_guard_and_the_previous_handler_is_restored(self):
        previous = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(admin.TerminatedBySignal) as caught:
            with admin.signal_termination_guard():
                os.kill(os.getpid(), signal.SIGTERM)
        self.assertEqual(caught.exception.signum, signal.SIGTERM)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    @unittest.skipUnless(hasattr(os, 'killpg'), 'POSIX process groups are required')
    def test_a_sigterm_during_the_native_sync_kills_the_client_group_and_runs_the_guard(self):
        # P2: the reviewer reproduced SIGTERM of `admin.py backup` leaving the `dolt ...
        # CALL DOLT_BACKUP` client writing the native backup after the backup lock was
        # released. The kit must stop the client's whole process group and still run the
        # guard that keeps the previous complete pair.
        make_project(self.root, 'alpha')
        (self.root / 'backups' / 'alpha').mkdir(parents=True, exist_ok=True)
        (self.root / 'backups' / 'alpha' / 'older-chunk').write_text('older generation',
                                                                    encoding='utf-8')
        write_pair(self.root, 'alpha', files={'.merge-context.json': {'holder': 'prior'}})
        canonical = self.root / 'backups' / 'alpha.coordination.json'
        self.assertEqual(admin.complete_sidecar(canonical)['files'],
                         {'.merge-context.json': {'holder': 'prior'}})
        # This run's native step is a fake client that spawns a child and waits, so the
        # whole process group can be observed.
        pids = self.root / 'sync.pids'
        script = self.root / 'bin' / 'dolt'
        script.parent.mkdir()
        script.write_text('#!/bin/sh\nsleep 30 &\necho "$$ $!" > %s\nwait\n' % pids,
                          encoding='utf-8')
        script.chmod(0o755)

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
            with self.assertRaises(admin.TerminatedBySignal):
                admin.backup_project(self.root, 'alpha')
        finally:
            killer.join(40)
        self.assertEqual(never_started, [], 'the fake client did not start within 30 s')
        self.assertTrue(pids.is_file(), 'the fake sync client never started')
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
        self.assertFalse(alive(client_pid), 'the sync client outlived the backup lock')
        self.assertFalse(alive(child_pid), 'a child of the sync client outlived the backup lock')
        # The guard restored the previous complete sidecar...
        restored = json.loads(canonical.read_text(encoding='utf-8'))
        self.assertEqual(restored['status'], 'complete')
        self.assertEqual(restored['files'], {'.merge-context.json': {'holder': 'prior'}})
        self.assertEqual(admin.backup_pair_state(self.root, 'alpha'), (True, None))
        # ...the staged snapshot did not survive, and the handler was restored.
        self.assertFalse(admin.staged_journal_snapshot_path(self.root, 'alpha').exists())
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    @unittest.skipUnless(hasattr(signal, 'pthread_sigmask'), 'POSIX signal masks are required')
    def test_sigterm_is_held_across_a_critical_window_and_the_mask_is_restored(self):
        # P3: a stop landing between promote_journal_snapshot() and the guard's promoted flag
        # restores the previous sidecar beside an already-promoted journal, and one landing
        # between Popen() and the pid record orphans the client. Those windows hold SIGTERM.
        before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        with admin.sigterm_blocked():
            held = signal.pthread_sigmask(signal.SIG_BLOCK, set())
            self.assertIn(signal.SIGTERM, held)
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), before)

    @unittest.skipUnless(hasattr(signal, 'pthread_sigmask'), 'POSIX signal masks are required')
    def test_a_stop_held_out_of_a_window_is_delivered_when_the_window_closes(self):
        # A held stop is deferred, never dropped: the guard still sees it as soon as the
        # mask is restored, so the normal cleanup runs.
        previous = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(admin.TerminatedBySignal):
            with admin.signal_termination_guard():
                with admin.sigterm_blocked():
                    # Directed at this thread, so it stays pending until the window closes,
                    # whichever thread the process signal would otherwise reach and
                    # whenever the interpreter then runs the handler (kittrial-5bb.122).
                    signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
                    self.assertIn(signal.SIGTERM,
                                  signal.pthread_sigmask(signal.SIG_BLOCK, set()))
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    @unittest.skipUnless(hasattr(os, 'killpg'), 'POSIX signal delivery is required')
    def test_a_second_sigterm_cannot_interrupt_the_cleanup_the_first_started(self):
        # P3: terminate_process_group used to run after the guard restored the previous
        # handler, so a second SIGTERM killed the interpreter before killpg. Later stops are
        # now ignored for the rest of the guard, and the group kill runs inside it.
        marks = []
        with self.assertRaises(admin.TerminatedBySignal):
            with admin.signal_termination_guard():
                try:
                    os.kill(os.getpid(), signal.SIGTERM)
                finally:
                    marks.append(signal.getsignal(signal.SIGTERM))
                    os.kill(os.getpid(), signal.SIGTERM)
                    marks.append('survived')
        # The handler stays installed and ignores the second stop itself (kittrial-5bb.122
        # review: it no longer switches to SIG_IGN, which a stop in the guard's exit could
        # leave behind); the guard puts the previous handler back.
        self.assertEqual(marks, [admin.raise_termination, 'survived'])



@unittest.skipUnless(hasattr(signal, 'pthread_sigmask') and hasattr(signal, 'pthread_kill'),
                     'POSIX signal masks are required')
class TerminationGuardExitCase(unittest.TestCase):
    """A stop delivered at each point of signal_termination_guard's exit (kittrial-5bb.122).

    Python runs a caught signal's handler at a later bytecode boundary, and
    `signal.signal` runs pending handlers before it swaps. So a stop can reach
    `raise_termination` while the guard is restoring the previous handler; it used to
    set SIG_IGN there and raise, and nothing restored the handler again (seen once in CI:
    "SIG_IGN is not SIG_DFL"). Each point is driven two ways: `handler`, the interpreter
    running the pending handler right there, and `kernel`, a real SIGTERM directed at this
    thread. Whatever the point, the stop must be raised as TerminatedBySignal or reach the
    previous handler, exactly once, and the previous handler and mask must be back.
    """
    POINTS = ('end-of-block', 'after-block', 'in-restore', 'after-restore', 'after-unmask')
    STYLES = ('handler', 'kernel')

    def setUp(self):
        self.received = []
        self.previous = lambda signum, frame: self.received.append(signum)
        old = signal.signal(signal.SIGTERM, self.previous)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        self.mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, self.mask)

    def deliver(self, style, frame):
        """`handler`: what the interpreter does with a pending stop at this boundary - call
        the installed handler with the running frame. `kernel`: a real SIGTERM."""
        if style == 'kernel':
            signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
            return
        handler = signal.getsignal(signal.SIGTERM)
        # CPython drops a pending signal whose handler is no longer callable.
        self.assertTrue(callable(handler) or handler == signal.SIG_IGN, handler)
        if callable(handler):
            handler(signal.SIGTERM, frame)

    def exit_with_stop(self, point, style, stopped_first=False):
        """Run the guard, delivering one stop at `point` of its exit. Returns the exception."""
        real_mask, real_signal = signal.pthread_sigmask, signal.signal
        fired = []

        def fire(at):
            if at == point and not fired:
                fired.append(at)
                self.deliver(style, sys._getframe(2))   # the frame that made the call

        def mask(how, signals):
            old = real_mask(how, signals)
            if how == signal.SIG_BLOCK and set(signals) == {signal.SIGTERM}:
                fire('after-block')
            if how == signal.SIG_SETMASK:
                fire('after-unmask')
            return old

        def swap(signum, handler):
            restoring = handler is not admin.raise_termination and handler != signal.SIG_IGN
            if restoring:
                fire('in-restore')
            old = real_signal(signum, handler)
            if restoring:
                fire('after-restore')
            return old

        caught = None
        try:
            with patch.object(signal, 'pthread_sigmask', mask), patch.object(signal, 'signal', swap):
                with admin.signal_termination_guard():
                    try:
                        if stopped_first:
                            self.deliver('handler', sys._getframe())
                    finally:
                        if point == 'end-of-block' and not fired:
                            fired.append(point)
                            self.deliver(style, sys._getframe())
        except admin.TerminatedBySignal as error:
            caught = error
        for _ in range(3):
            pass   # bytecode boundaries: a stop now pending at the previous handler runs here
        self.assertEqual(fired, [point])
        self.assertIs(signal.getsignal(signal.SIGTERM), self.previous)
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), self.mask)
        return caught

    # Who owns a stop: one the guard can see before it hands the signal back is raised
    # as TerminatedBySignal, so the cleanup runs (in production the previous handler is
    # usually SIG_DFL, which would kill the process with no cleanup). Only a stop that
    # is handled after the previous handler is installed and unmasked - or that the
    # interpreter runs once the previous handler is back - belongs to that handler.
    PREVIOUS_OWNS = {('after-restore', 'handler'), ('after-unmask', 'handler'), ('after-unmask', 'kernel')}

    def test_every_exit_point_restores_the_handler_and_keeps_the_stop(self):
        for point in self.POINTS:
            for style in self.STYLES:
                with self.subTest(point=point, style=style):
                    del self.received[:]
                    caught = self.exit_with_stop(point, style)
                    self.assertEqual((caught is not None) + len(self.received), 1,
                                     'the stop was dropped or delivered twice')
                    self.assertEqual(caught is None, (point, style) in self.PREVIOUS_OWNS)

    def test_a_stop_during_the_exit_cannot_interrupt_the_cleanup_of_an_earlier_one(self):
        for point in self.POINTS:
            for style in self.STYLES:
                with self.subTest(point=point, style=style):
                    del self.received[:]
                    caught = self.exit_with_stop(point, style, stopped_first=True)
                    self.assertIsNotNone(caught)
                    self.assertIsNone(caught.__context__, 'a second stop interrupted the first')

    def test_a_stop_already_pending_when_the_first_one_raised_is_ignored(self):
        # The first stop sets SIG_IGN, but a second one the interpreter had already caught
        # still runs the handler; it must not raise inside the cleanup the first started.
        cleanup = []
        with self.assertRaises(admin.TerminatedBySignal) as raised:
            with admin.signal_termination_guard():
                try:
                    admin.raise_termination(signal.SIGTERM, sys._getframe())
                finally:
                    admin.raise_termination(signal.SIGTERM, sys._getframe())
                    cleanup.append('ran')
        self.assertEqual(cleanup, ['ran'])
        self.assertIsNone(raised.exception.__context__)
        self.assertIs(signal.getsignal(signal.SIGTERM), self.previous)

    def test_a_stop_during_the_exit_after_an_error_is_raised_not_dropped(self):
        real_signal = signal.signal

        def swap(signum, handler):
            if handler is self.previous:
                # the pending handler runs here, in the guard's frame
                admin.raise_termination(signal.SIGTERM, sys._getframe(1))
            return real_signal(signum, handler)

        with self.assertRaises(admin.TerminatedBySignal) as raised:
            with patch.object(signal, 'signal', swap):
                with admin.signal_termination_guard():
                    raise ValueError('the block failed')
        self.assertIsInstance(raised.exception.__context__, ValueError)
        self.assertIs(signal.getsignal(signal.SIGTERM), self.previous)


@unittest.skipUnless(hasattr(signal, 'pthread_sigmask') and hasattr(signal, 'pthread_kill'),
                     'POSIX signal masks are required')
class TerminationGuardRealSignalCase(unittest.TestCase):
    """Real SIGTERMs, nothing patched (kittrial-5bb.122 review).

    `signal.signal` and `signal.pthread_sigmask` are Python functions in Lib/signal.py, so
    while the guard is inside them the frame Python hands the handler is theirs, not the
    guard's. The earlier tests called the handler with the guard's own frame and patched
    those functions, and missed it: a stop there set SIG_IGN and escaped the restore.
    Here a profile hook only chooses the moment; the signal is a real one sent to this
    thread, and the interpreter runs the handler wherever it next checks.
    """

    def setUp(self):
        self.received = []
        self.previous = lambda signum, frame: self.received.append(signum)
        old = signal.signal(signal.SIGTERM, self.previous)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        self.mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, self.mask)
        self.addCleanup(sys.setprofile, None)

    @staticmethod
    def in_guard(frame):
        while frame is not None:
            if frame.f_code in admin._GUARD_CODES:
                return True
            frame = frame.f_back
        return False

    def wrapper_calls(self, action, at=None, guard_filter=None):
        """Profile calls into Lib/signal.py made from the guard's own code; send a real
        SIGTERM at the call numbered `at`. Returns how many such calls there were."""
        seen = []

        def profile(frame, event, arg):
            if (event == 'call' and frame.f_code.co_filename == signal.__file__ and self.in_guard(frame.f_back)
                    and (guard_filter is None or guard_filter(frame))):
                if len(seen) == at:
                    signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
                seen.append(frame.f_code.co_name)

        sys.setprofile(profile)
        try:
            return action(), seen
        finally:
            sys.setprofile(None)

    def settle(self):
        for _ in range(3):
            pass
        sum(range(10))   # a call: a stop now pending at the previous handler runs here

    def assert_restored(self):
        self.assertIs(signal.getsignal(signal.SIGTERM), self.previous)
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), self.mask)
        self.assertEqual(admin._termination_guards, [])

    def guarded(self):
        try:
            with admin.signal_termination_guard():
                sum(range(10))
        except admin.TerminatedBySignal:
            return 'raised'
        return 'completed'

    def test_a_real_stop_inside_the_signal_wrappers_is_raised_or_reaches_the_previous_handler(self):
        _, calls = self.wrapper_calls(self.guarded)
        # setup: getsignal, signal; exit: pthread_sigmask (read), pthread_sigmask (block),
        # signal, sigtimedwait or sigpending, pthread_sigmask (restore)
        self.assertGreaterEqual(len(calls), 6, calls)
        self.assertIn('pthread_sigmask', calls)
        for index, name in enumerate(calls):
            with self.subTest(call=index, wrapper=name):
                del self.received[:]
                outcome, _ = self.wrapper_calls(self.guarded, at=index)
                self.settle()
                self.assert_restored()
                self.assertEqual((outcome == 'raised') + len(self.received), 1,
                                 'the stop was dropped or delivered twice: %s, %r' % (outcome, self.received))

    def test_a_stop_recorded_during_setup_is_raised_before_the_block(self):
        ran = []

        def profile(frame, event, arg):
            # What the interpreter does with a stop caught as the install returns: run the
            # handler with the frame then executing, the wrapper's (whose caller is the guard).
            if (event == 'return' and frame.f_code.co_name == 'signal' and frame.f_code.co_filename == signal.__file__
                    and arg is not admin.raise_termination and self.in_guard(frame.f_back)
                    and signal.getsignal(signal.SIGTERM) is admin.raise_termination):
                sys.setprofile(None)
                admin.raise_termination(signal.SIGTERM, frame)

        sys.setprofile(profile)
        with self.assertRaises(admin.TerminatedBySignal):
            with admin.signal_termination_guard():
                ran.append('block')
        sys.setprofile(None)
        self.assertEqual(ran, [])
        self.assert_restored()
        self.assertEqual(self.received, [])

    def test_nested_guards_raise_at_the_inner_exit_and_protect_the_outer_cleanup(self):
        # restore-new wraps native_restore: a stop taken at the INNER guard's exit is raised
        # there, and a second stop must not interrupt the outer block's cleanup.
        for index in range(4):
            with self.subTest(call=index):
                del self.received[:]
                trace = []
                inner = admin.signal_termination_guard()

                def run():
                    try:
                        with admin.signal_termination_guard():
                            try:
                                with inner:
                                    trace.append('inner block')
                                trace.append('after inner')
                            finally:
                                os.kill(os.getpid(), signal.SIGTERM)    # a second stop
                                sum(range(10))
                                trace.append('outer cleanup')
                    except admin.TerminatedBySignal as error:
                        return error
                    return None

                error, calls = self.wrapper_calls(
                    run, at=index, guard_filter=lambda frame: self.in_inner_exit(frame, inner))
                self.settle()
                self.assertGreater(len(calls), index)
                self.assertIsNotNone(error)
                self.assertIsNone(error.__context__, 'a second stop interrupted the cleanup')
                self.assertEqual(trace, ['inner block', 'outer cleanup'])
                self.assert_restored()
                self.assertEqual(self.received, [])

    def test_a_stop_in_the_inner_block_protects_the_outer_cleanup(self):
        # The first stop lands in the INNER block: every guard is marked stopped, so a
        # second stop in the outer block's cleanup, after the inner guard is gone, is
        # ignored too.
        trace = []
        with self.assertRaises(admin.TerminatedBySignal) as raised:
            with admin.signal_termination_guard():
                try:
                    with admin.signal_termination_guard():
                        os.kill(os.getpid(), signal.SIGTERM)
                        sum(range(10))
                        trace.append('inner block went on')
                finally:
                    os.kill(os.getpid(), signal.SIGTERM)
                    sum(range(10))
                    trace.append('outer cleanup')
        self.assertIsNone(raised.exception.__context__)
        self.assertEqual(trace, ['outer cleanup'])
        self.settle()
        self.assert_restored()
        self.assertEqual(self.received, [])

    def test_a_stop_run_as_the_inner_guard_unmasks_cannot_leave_the_mask_held(self):
        # The inner guard stays listed until its mask is back: a handler the interpreter
        # runs inside that last pthread_sigmask call (a stop another thread caught while
        # this one was masked) is recorded on the inner guard, not raised before the
        # restore. The handler is run here as the interpreter would, with the wrapper's frame.
        inner = admin.signal_termination_guard()

        def profile(frame, event, arg):
            if (event == 'call' and frame.f_code.co_name == 'pthread_sigmask'
                    and frame.f_code.co_filename == signal.__file__ and self.in_inner_exit(frame, inner)
                    and frame.f_locals.get('how') == signal.SIG_SETMASK):
                sys.setprofile(None)
                admin.raise_termination(signal.SIGTERM, frame)

        with self.assertRaises(admin.TerminatedBySignal):
            with admin.signal_termination_guard():
                sys.setprofile(profile)
                with inner:
                    pass
        sys.setprofile(None)
        self.settle()
        self.assert_restored()
        self.assertEqual(self.received, [])

    def test_a_stop_at_the_end_of_enter_is_raised_before_the_block(self):
        # kittrial-5bb.124: a stop handled in __enter__ after its held check used to be
        # recorded and raised only at the exit, after the whole block ran. Now the handler
        # releases the guard and raises from __enter__, as for a stop during setup. The
        # handler is run where the interpreter would run it: in __enter__'s last line.
        ran = []

        def profile(frame, event, arg):
            if event == 'return' and frame.f_code is admin.signal_termination_guard.__enter__.__code__:
                sys.setprofile(None)
                admin.raise_termination(signal.SIGTERM, frame)

        with self.assertRaises(admin.TerminatedBySignal):
            sys.setprofile(profile)
            with admin.signal_termination_guard():
                sum(range(3))                 # a call: the interpreter's first check
                for _ in range(3):
                    pass                      # backward jumps: more checks
                ran.append('rest of the block')
        sys.setprofile(None)
        self.assertEqual(ran, [])
        self.settle()
        self.assert_restored()
        self.assertEqual(self.received, [])

    def test_a_stop_at_any_line_of_enter_after_the_install_never_lets_the_block_run_first(self):
        # The guard is armed BEFORE its held check, so every line after the install either
        # records the stop for that check or releases and raises; none leaves it for the
        # exit. The handler is run, as the interpreter would, at each line in turn.
        enter = admin.signal_termination_guard.__enter__.__code__
        count = 0
        while True:
            lines = []

            def tracer(frame, event, arg):
                if frame.f_code is not enter:
                    return None
                if event == 'line' and signal.getsignal(signal.SIGTERM) is admin.raise_termination:
                    if len(lines) == count:
                        lines.append(frame.f_lineno)
                        sys.settrace(None)
                        frame.f_trace = None
                        admin.raise_termination(signal.SIGTERM, frame)
                    else:
                        lines.append(frame.f_lineno)
                return tracer

            ran = []
            sys.settrace(tracer)
            try:
                with admin.signal_termination_guard():
                    ran.append('block')
            except admin.TerminatedBySignal:
                pass
            finally:
                sys.settrace(None)
            if len(lines) <= count:
                break                         # every line after the install was tried
            with self.subTest(line=lines[count]):
                self.assertEqual(ran, [])
                self.settle()
                self.assert_restored()
            count += 1
        self.assertGreaterEqual(count, 3)

    def test_a_guard_whose_exit_never_runs_is_released_when_collected(self):
        # kittrial-5bb.124: through contextlib.ExitStack a stop in contextlib's own code
        # between the block and the guard's exit skipped the exit, and the guard stayed
        # installed for good (handler raise_termination, a record marked stopped, later
        # stops ignored). Here the exit is skipped the same way - the stack's callbacks are
        # moved out and dropped - with a stop already recorded.
        import contextlib
        import gc
        with contextlib.ExitStack() as stack:
            guard = stack.enter_context(admin.signal_termination_guard())
            self.assertIs(signal.getsignal(signal.SIGTERM), admin.raise_termination)
            guard.held = True                 # a stop it recorded, never raised
            stack.pop_all()                   # the exit will not run
        del guard
        gc.collect()
        self.settle()
        self.assert_restored()
        self.assertEqual(self.received, [signal.SIGTERM])   # handed to the previous handler
        # A stopped record no longer silences later stops.
        with self.assertRaises(admin.TerminatedBySignal):
            with admin.signal_termination_guard():
                os.kill(os.getpid(), signal.SIGTERM)
                sum(range(10))
        self.assert_restored()

    def test_a_stop_in_an_unlisted_guards_last_lines_belongs_to_the_guards_still_listed(self):
        # kittrial-5bb.124 mutation: the stack walk must only match a guard that is still
        # listed. Once the inner guard's exit has unlisted it, a stop handled in its
        # _raise_held is the outer guard's to raise; recorded on the inner guard it was lost.
        inner = admin.signal_termination_guard()
        outcome = []

        def profile(frame, event, arg):
            if (event == 'return' and frame.f_code is admin.signal_termination_guard._raise_held.__code__
                    and frame.f_locals.get('self') is inner):
                sys.setprofile(None)
                try:
                    admin.raise_termination(signal.SIGTERM, frame)
                    outcome.append('recorded')
                except admin.TerminatedBySignal:
                    outcome.append('raised')

        with admin.signal_termination_guard():
            sys.setprofile(profile)
            with inner:
                pass
            sys.setprofile(None)
        self.assertEqual(outcome, ['raised'])
        self.assertFalse(inner.held)
        self.assert_restored()

    def abandon(self, held=False, cycle=False):
        """A guard entered through an ExitStack whose callbacks are then dropped, so its
        exit never runs; returned in a one-item list the caller empties."""
        import contextlib
        stack = contextlib.ExitStack()
        guard = stack.enter_context(admin.signal_termination_guard())
        stack.pop_all()
        guard.held = held
        if cycle:
            guard.cycle = guard               # only the cycle collector frees it
        return [guard]

    def drop_on_another_thread(self, box):
        errors = []
        old_hook = threading.excepthook
        threading.excepthook = errors.append
        try:
            worker = threading.Thread(target=box.pop)
            worker.start()
            worker.join()
        finally:
            threading.excepthook = old_hook
        self.assertEqual(errors, [])

    def test_a_guard_dropped_on_another_thread_leaves_a_record_the_next_stop_releases(self):
        # kittrial-5bb.125: its finaliser ran off the main thread, could not restore the
        # handler, and the next stop raised TerminatedBySignal outside any guard and left
        # SIG_IGN. Now the finaliser does nothing there; the dead record it leaves is
        # released by the next stop, which then reaches the previous handler, once.
        box = self.abandon()
        unraisable = []
        old_hook = sys.unraisablehook
        sys.unraisablehook = unraisable.append
        try:
            self.drop_on_another_thread(box)
        finally:
            sys.unraisablehook = old_hook
        self.assertEqual(unraisable, [])
        self.assertIs(signal.getsignal(signal.SIGTERM), admin.raise_termination)
        self.assertEqual([record.ref() for record in admin._termination_guards], [None])
        signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)   # a real stop, no guard around it
        self.settle()
        self.assertEqual(self.received, [signal.SIGTERM])
        self.assert_restored()

    def test_a_stop_owed_by_a_guard_dropped_on_another_thread_reaches_the_previous_handler_at_the_next_guard(self):
        box = self.abandon(held=True)
        self.drop_on_another_thread(box)
        self.assertEqual(self.received, [])           # nothing was sent from that thread
        with admin.signal_termination_guard():
            self.assertEqual(self.received, [signal.SIGTERM])   # sent on as the next guard begins
            self.assertEqual(len(admin._termination_guards), 1)
        self.settle()
        self.assertEqual(self.received, [signal.SIGTERM])
        self.assert_restored()

    def test_a_stop_owed_by_a_guard_dropped_on_another_thread_goes_before_the_next_stop(self):
        box = self.abandon(held=True)
        self.drop_on_another_thread(box)
        signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
        self.settle()
        self.assertEqual(self.received, [signal.SIGTERM, signal.SIGTERM])   # the owed stop, then this one
        self.assert_restored()

    def test_a_previous_handler_that_raises_does_not_escape_the_finaliser(self):
        # The finaliser sends an owed stop on to the previous handler; a handler that raises
        # (here a BaseException, as KeyboardInterrupt or SystemExit would be) cannot raise
        # out of a finaliser usefully, and must not print "Exception ignored".
        class Stopped(BaseException):
            pass

        def previous(signum, frame):
            self.received.append(signum)
            raise Stopped()

        signal.signal(signal.SIGTERM, previous)
        unraisable = []
        old_hook = sys.unraisablehook
        sys.unraisablehook = unraisable.append
        try:
            box = self.abandon(held=True)
            box.pop()                         # collected here, on the main thread
        finally:
            sys.unraisablehook = old_hook
        self.assertEqual(unraisable, [])
        self.assertEqual(self.received, [signal.SIGTERM])
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        self.assertEqual(admin._termination_guards, [])
        signal.signal(signal.SIGTERM, self.previous)

    def test_nested_guards_dropped_on_another_thread_give_back_the_outermost_previous_handler(self):
        outer = self.abandon()
        inner = self.abandon()                # its previous handler is raise_termination
        self.drop_on_another_thread(inner)
        self.drop_on_another_thread(outer)
        self.assertEqual(len(admin._termination_guards), 2)
        signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
        self.settle()
        self.assertEqual(self.received, [signal.SIGTERM])
        self.assert_restored()

    def test_a_dead_record_outside_a_live_guard_waits_for_that_guard(self):
        # Only dead records inside every live one are released: an abandoned OUTER guard's
        # record stays while a guard it encloses is active, so the live guard keeps its
        # rules, and is released once that guard has exited.
        outer = self.abandon()
        with self.assertRaises(admin.TerminatedBySignal):
            with admin.signal_termination_guard():
                self.drop_on_another_thread(outer)
                signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
                sum(range(10))
        self.assertEqual(len(admin._termination_guards), 1)
        self.assertIs(signal.getsignal(signal.SIGTERM), admin.raise_termination)
        signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
        self.settle()
        self.assertEqual(self.received, [signal.SIGTERM])
        self.assert_restored()

    def test_a_stop_owed_by_a_guard_collected_inside_a_live_one_is_raised_by_that_one(self):
        # Sent on from the finaliser, it would be raised by the live guard's handler inside
        # the finaliser and swallowed there; the live guard raises it at its exit instead.
        unraisable = []
        old_hook = sys.unraisablehook
        sys.unraisablehook = unraisable.append
        try:
            with self.assertRaises(admin.TerminatedBySignal):
                with admin.signal_termination_guard():
                    box = self.abandon(held=True)
                    box.pop()                 # collected here, on the main thread
                    self.assertEqual(len(admin._termination_guards), 1)
        finally:
            sys.unraisablehook = old_hook
        self.assertEqual(unraisable, [])
        self.settle()
        self.assertEqual(self.received, [])
        self.assert_restored()

    def test_a_real_stop_while_the_collector_releases_a_guard_is_not_lost(self):
        # kittrial-5bb.125: the finaliser of a cycle-collected guard runs after its weak
        # reference is cleared; a stop during its release printed "Exception ignored in
        # __del__", was lost and left SIG_IGN and a dead record. A real SIGTERM is sent at
        # each call into Lib/signal.py the finaliser makes, one per trial.
        import gc
        index = 0
        while True:
            del self.received[:]
            box = self.abandon(cycle=True)
            box.pop()
            seen = []

            def profile(frame, event, arg):
                if (event == 'call' and frame.f_code.co_filename == signal.__file__
                        and self.under(frame, admin.signal_termination_guard.__del__.__code__)):
                    if len(seen) == index:
                        signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
                    seen.append(frame.f_code.co_name)

            unraisable = []
            old_hook = sys.unraisablehook
            sys.unraisablehook = unraisable.append
            sys.setprofile(profile)
            try:
                gc.collect()
            finally:
                sys.setprofile(None)
                sys.unraisablehook = old_hook
            self.settle()
            if len(seen) <= index:
                del self.received[:]
                break
            with self.subTest(call=index, wrapper=seen[index]):
                self.assertEqual(unraisable, [])
                self.assertEqual(self.received, [signal.SIGTERM])
                self.assert_restored()
            index += 1
        self.assertGreaterEqual(index, 3)

    @staticmethod
    def under(frame, code):
        while frame is not None:
            if frame.f_code is code:
                return True
            frame = frame.f_back
        return False

    def test_at_interpreter_exit_an_abandoned_guard_keeps_the_exit_status(self):
        # kittrial-5bb.125: with a stop recorded and SIG_DFL before it, the finaliser at
        # exit sent the stop on and the process died by SIGTERM (-15) instead of exiting
        # with the status it was asked for. The process is already ending; the status is
        # kept, with or without a recorded stop.
        script = (
            'import contextlib, signal, sys\n'
            'sys.path.insert(0, %r)\n'
            'import admin\n'
            'signal.signal(signal.SIGTERM, signal.SIG_DFL)\n'
            'stack = contextlib.ExitStack()\n'
            'GUARD = stack.enter_context(admin.signal_termination_guard())\n'
            'stack.pop_all()\n'
            'GUARD.held = %r\n'
            'sys.exit(3)\n')
        repo = str(Path(admin.__file__).resolve().parent)
        for held in (False, True):
            with self.subTest(held=held):
                result = subprocess.run([sys.executable, '-c', script % (repo, held)],
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertNotIn('Exception ignored', result.stderr)

    @staticmethod
    def in_inner_exit(frame, inner):
        while frame is not None:
            if frame.f_code is admin.signal_termination_guard._release.__code__:
                return frame.f_locals.get('self') is inner
            frame = frame.f_back
        return False

    def test_an_abandoned_guard_hands_a_recorded_stop_to_the_previous_handler(self):
        # A generator suspended inside the block is closed (here explicitly, as the
        # collector would): the stop recorded during that exit cannot be raised into the
        # closer, so it must reach the previous handler rather than be lost.
        def suspended():
            with admin.signal_termination_guard():
                yield 'inside'

        generator = suspended()
        self.assertEqual(next(generator), 'inside')
        unraisable = []
        old_hook = sys.unraisablehook
        sys.unraisablehook = unraisable.append
        try:
            _, calls = self.wrapper_calls(generator.close, at=1)
        finally:
            sys.unraisablehook = old_hook
        self.settle()
        self.assertTrue(calls)
        self.assertEqual(unraisable, [])
        self.assertEqual(self.received, [signal.SIGTERM])
        self.assert_restored()


@unittest.skipUnless(sys.platform.startswith('linux'), 'timer_create and real signal timing are Linux-only')
class TerminationGuardStressCase(unittest.TestCase):
    """Real SIGTERMs from a kernel timer at random delays around a guarded block's end,
    in child processes, for each kind of previous handler (kittrial-5bb.122 review).
    Bounded to about a second per handler; tests/sigterm_stress_child.py runs longer."""
    CHILD = Path(__file__).resolve().parent / 'sigterm_stress_child.py'
    REPO = str(Path(__file__).resolve().parent.parent)

    SKIP = 77

    def run_child(self, previous, seconds):
        result = subprocess.run([sys.executable, str(self.CHILD), self.REPO, previous, str(seconds)],
                                capture_output=True, text=True, timeout=60)
        if result.returncode == self.SKIP:
            self.skipTest(result.stdout.strip())     # no kernel timer on this machine
        return result

    def test_custom_and_ignored_previous_handlers(self):
        for previous in ('custom', 'ignore'):
            with self.subTest(previous=previous):
                result = self.run_child(previous, 1)
                # A wrong state after any trial is exit 1: that is the guard failing.
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                counts = json.loads(result.stdout.splitlines()[-1])
                # Coverage, not correctness: the child keeps going (up to 5 s) until it has
                # 20 trials with stops on both sides of the guard's end. A machine too slow
                # or too loaded for that has not exercised the guard, which is a skip, not
                # a failure (kittrial-5bb.124).
                if not (counts['trials'] >= 20 and 0 < counts['raised'] < counts['trials']):
                    self.skipTest('the guard was not exercised on both sides here: %r' % counts)

    def test_a_machine_without_a_kernel_timer_skips(self):
        # kittrial-5bb.124: a refused or missing timer_create used to exit 1 and fail the test.
        with tempfile.TemporaryDirectory() as folder:
            child = Path(folder) / 'child.py'
            child.write_text("print('SKIP: timer_create was refused (errno 1)'); raise SystemExit(77)\n",
                             encoding='utf-8')
            with patch.object(self, 'CHILD', child):
                with self.assertRaisesRegex(unittest.SkipTest, 'timer_create was refused'):
                    self.run_child('custom', 1)

    def test_default_previous_handler(self):
        # A stop the previous SIG_DFL owns ends the child with SIGTERM: allowed. Any wrong
        # state after a trial makes the child exit 1 instead.
        deadline, trials, ended = time.monotonic() + 1.5, 0, 0
        while time.monotonic() < deadline:
            result = self.run_child('default', 0.5)
            trials += result.stderr.count('t\n')
            if result.returncode == -signal.SIGTERM:
                ended += 1
                continue
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # About half the stops belong to SIG_DFL and end the child, so process start-up
        # bounds the count; each ended child is one trial too. The count measures how fast
        # the machine starts processes, not the guard: on one core shared with eleven busy
        # processes it was 2 to 6 (kittrial-5bb.122 review), so only require that a trial ran.
        self.assertGreater(trials + ended, 0)


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

    def test_a_failed_run_keeps_the_journal_snapshot_of_the_last_complete_pair(self):
        # P1(b): the journal snapshot must belong to the same generation as the native
        # backup and the complete sidecar. A keyed write is acknowledged, the synced run
        # fails, and restore-new must not see that un-synced operation in the journal it
        # restores with the previous Dolt state.
        live = self.root / 'projects' / 'alpha' / admin.JOURNAL_STORE_NAME
        write_live_journal(live, ['op-old'])
        with patch.object(admin, 'native_backup_sync', return_value='Backup synced'):
            stdout, stderr, code = self.run_admin('backup', 'alpha')
        self.assertEqual(code, 0, stderr)
        snapshot = admin.journal_snapshot_path(self.root, 'alpha')
        self.assertEqual(journal_ids(snapshot), {'op-old'})
        # A keyed write lands in the live journal, then the native sync fails.
        write_live_journal(live, ['op-old', 'op-unsynced'])
        failure = subprocess.CalledProcessError(1, ['dolt', 'sql'], stderr='backup target is full')
        with patch.object(admin, 'native_backup_sync', side_effect=failure):
            stdout, stderr, code = self.run_admin('backup', 'alpha')
        self.assertNotEqual(code, 0)
        # The complete sidecar (and the durable last-complete copy) is restored...
        self.assertEqual(admin.backup_pair_state(self.root, 'alpha'), (True, None))
        aside = admin.last_complete_sidecar_path(self.root, 'alpha')
        self.assertEqual(json.loads(aside.read_text(encoding='utf-8'))['status'], 'complete')
        # ...and the journal snapshot restore-new uses is the one that pairs with it,
        # with no un-synced operation and no staging file left behind.
        self.assertEqual(journal_ids(snapshot), {'op-old'})
        self.assertNotIn('op-unsynced', journal_ids(snapshot))
        self.assertFalse(admin.staged_journal_snapshot_path(self.root, 'alpha').exists())
        # A later successful run promotes the journal of the new generation.
        with patch.object(admin, 'native_backup_sync', return_value='Backup synced'):
            stdout, stderr, code = self.run_admin('backup', 'alpha')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(journal_ids(snapshot), {'op-old', 'op-unsynced'})

    def test_restore_reports_when_it_falls_back_to_the_last_complete_copy(self):
        write_pair(self.root, 'alpha', status='pending')
        aside = admin.last_complete_sidecar_path(self.root, 'alpha')
        aside.write_text(json.dumps(
            {'schema_version': 1, 'status': 'complete', 'files': {}, 'operators': []}),
            encoding='utf-8')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            admin.restore_coordination(self.root, 'alpha', 'beta')
        # The operator is told the canonical sidecar was not complete and what the
        # restore actually used, instead of the fallback happening silently.
        self.assertIn('last-complete copy', output.getvalue())

    def test_a_journal_snapshot_is_promoted_only_after_a_successful_sync(self):
        live = self.root / 'projects' / 'alpha' / admin.JOURNAL_STORE_NAME
        write_live_journal(live, ['op-one'])
        with patch.object(admin, 'native_backup_sync', side_effect=RuntimeError('sync failed')):
            with self.assertRaises(RuntimeError):
                admin.backup_project(self.root, 'alpha')
        # No previous snapshot existed, so a failed first run publishes none.
        self.assertIsNone(journal_ids(admin.journal_snapshot_path(self.root, 'alpha')))
        with patch.object(admin, 'native_backup_sync', return_value='Backup synced'):
            admin.backup_project(self.root, 'alpha')
        self.assertEqual(journal_ids(admin.journal_snapshot_path(self.root, 'alpha')), {'op-one'})

    def test_a_failed_last_complete_refresh_does_not_roll_back_the_new_sidecar(self):
        # P3 ordering: if refreshing the last-complete copy fails after a successful
        # sync, the guard must NOT put the old sidecar back beside the new native
        # directory and the newly promoted journal.
        write_pair(self.root, 'alpha', files={'.merge-context.json': {'holder': 'prior'}})
        real_copy = admin._atomic_copy
        calls = []

        def flaky(source, destination):
            calls.append((Path(source).name, Path(destination).name))
            if len(calls) == 2:  # guard save-aside first, post-sync refresh second
                raise OSError('disk full')
            return real_copy(source, destination)

        with patch.object(admin, 'native_backup_sync', return_value='Backup synced'), \
                patch.object(admin, '_atomic_copy', side_effect=flaky):
            stdout, stderr, code = self.run_admin('backup', 'alpha')
        self.assertNotEqual(code, 0)
        canonical = json.loads(
            (self.root / 'backups' / 'alpha.coordination.json').read_text(encoding='utf-8'))
        self.assertEqual(canonical['status'], 'complete')
        # The new generation's (empty) coordination files, not the prior holder.
        self.assertEqual(canonical['files'], {})


class NativeBackupManifestCase(RuntimeCase):
    """P2: a restore must notice a native directory that changed after its generation."""

    def setUp(self):
        super().setUp()
        for name in ('alpha', 'beta'):
            make_project(self.root, name)
            write_pair(self.root, name, files={'.merge-context.json': {'holder': 'prior'}})

    def complete_run(self, name='alpha'):
        with patch.object(admin, 'native_backup_sync', return_value='Backup synced'):
            admin.backup_project(self.root, name)

    def restore_output(self, source='alpha', destination='beta'):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            admin.restore_coordination(self.root, source, destination)
        return output.getvalue()

    def test_the_complete_sidecar_records_a_manifest_of_the_native_directory(self):
        self.complete_run()
        record = json.loads(
            (self.root / 'backups' / 'alpha.coordination.json').read_text(encoding='utf-8'))
        # Additive on schema 1: the readers keep accepting the record.
        self.assertEqual(record['schema_version'], 1)
        self.assertEqual(admin.complete_sidecar(
            self.root / 'backups' / 'alpha.coordination.json')['status'], 'complete')
        self.assertEqual(admin.backup_pair_state(self.root, 'alpha'), (True, None))
        self.assertEqual(admin.coordination_backup(self.root, 'alpha'), record['files'])
        manifest = record[admin.NATIVE_MANIFEST_KEY]
        self.assertEqual(manifest['schema_version'], 1)
        self.assertEqual(len(manifest['digest']), 64)
        current, problem = admin.native_backup_manifest(self.root / 'backups' / 'alpha')
        self.assertIsNone(problem)
        self.assertEqual(current['digest'], manifest['digest'])
        self.assertEqual(admin.native_backup_change(self.root, 'alpha', record)[0], 'unchanged')
        self.assertEqual(admin.coordination_operators(self.root, 'alpha'), [])

    def test_an_unchanged_pair_restores_without_a_warning(self):
        self.complete_run()
        text = self.restore_output()
        self.assertNotIn('WARNING', text)
        self.assertNotIn('could not be checked', text)

    def test_restore_warns_when_the_native_directory_changed_after_the_complete_run(self):
        self.complete_run()
        # A run that was interrupted (or killed) can leave the native directory partly
        # rewritten while the canonical sidecar is the restored previous generation.
        (self.root / 'backups' / 'alpha' / 'half-written-chunk').write_text(
            'a newer, partial sync', encoding='utf-8')
        text = self.restore_output()
        self.assertIn('WARNING', text)
        self.assertIn('NO receipt in the restored journal', text)
        self.assertIn('backups/alpha', text)

    def test_the_last_complete_fallback_and_the_change_warning_are_both_reported(self):
        # A kill -9 leaves the canonical marker `pending`; the durable copy is complete.
        self.complete_run()
        (self.root / 'backups' / 'alpha.coordination.json').write_text(
            json.dumps({'schema_version': 1, 'status': 'pending'}), encoding='utf-8')
        self.assertTrue(admin.using_last_complete_sidecar(self.root, 'alpha'))
        (self.root / 'backups' / 'alpha' / 'orphan-client-chunk').write_text(
            'written after the lock was released', encoding='utf-8')
        text = self.restore_output()
        self.assertIn('last-complete copy', text)
        self.assertIn('WARNING', text)
        self.assertIn('NO receipt in the restored journal', text)

    def test_a_sidecar_without_a_manifest_is_reported_as_not_checked(self):
        # A sidecar written before the manifest existed must not be called clean.
        text = self.restore_output()
        self.assertIn('could not be checked', text)
        self.assertNotIn('WARNING', text)

    def test_a_missing_or_odd_directory_is_reported_and_never_raises(self):
        manifest, problem = admin.native_backup_manifest(self.root / 'backups' / 'absent')
        self.assertIsNone(manifest)
        self.assertIn('missing', problem)
        state, detail = admin.native_backup_change(
            self.root, 'absent', {admin.NATIVE_MANIFEST_KEY: {'digest': '0' * 64}})
        self.assertEqual(state, 'unknown')
        self.assertIn('missing', detail)

    def test_a_rewritten_or_added_file_changes_the_digest(self):
        chunk = self.root / 'backups' / 'alpha' / 'chunk'
        chunk.write_text('one', encoding='utf-8')
        first, _ = admin.native_backup_manifest(self.root / 'backups' / 'alpha')
        chunk.write_text('two-longer', encoding='utf-8')
        second, _ = admin.native_backup_manifest(self.root / 'backups' / 'alpha')
        self.assertNotEqual(first['digest'], second['digest'])
        # A pure timestamp change is visible too (the stat walk records mtime_ns), which is
        # what catches a same-size rewrite on a filesystem with a fine-grained clock.
        info = chunk.stat()
        os.utime(chunk, ns=(info.st_atime_ns, info.st_mtime_ns + 10 ** 9))
        third, _ = admin.native_backup_manifest(self.root / 'backups' / 'alpha')
        self.assertNotEqual(second['digest'], third['digest'])
        (self.root / 'backups' / 'alpha' / 'added').write_text('more', encoding='utf-8')
        fourth, _ = admin.native_backup_manifest(self.root / 'backups' / 'alpha')
        self.assertNotEqual(third['digest'], fourth['digest'])
        # Reading the same directory twice is deterministic.
        again, _ = admin.native_backup_manifest(self.root / 'backups' / 'alpha')
        self.assertEqual(fourth['digest'], again['digest'])


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

    def degraded(self):
        self.record()
        record = admin.read_backup_status(self.root)
        record['projects'][1]['degraded'] = 'guidance is degraded: the pair was left out; repair it'
        admin.write_backup_status(self.root, record)

    def test_the_copy_names_a_degraded_project_and_copies_it(self):
        # kittrial-5bb.105 item 1.
        self.degraded()
        stdout, stderr, code = self.run_admin('backup-copy', str(self.destination))
        self.assertEqual(code, 0, stderr)
        self.assertIn('Degraded, copied as it is: beta: guidance is degraded', stdout)
        self.assertNotIn('alpha: guidance', stdout)
        self.assertTrue((self.destination / 'beta').is_dir())
        self.assertTrue((self.destination / 'beta.coordination.json').is_file())

    def test_the_copy_with_require_clean_refuses_a_degraded_project_and_copies_nothing(self):
        self.degraded()
        stdout, stderr, code = self.run_admin('backup-copy', str(self.destination), '--require-clean')
        self.assertNotEqual(code, 0)
        self.assertIn('backup-copy refused (--require-clean): 1 degraded (beta)', stderr)
        self.assertFalse(self.destination.exists())
        # With nothing degraded the option changes nothing.
        self.record()
        stdout, stderr, code = self.run_admin('backup-copy', str(self.destination), '--require-clean')
        self.assertEqual(code, 0, stderr)
        self.assertNotIn('egraded', stdout)
        self.assertTrue((self.destination / 'alpha').is_dir())

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

    def test_the_copy_refuses_an_inside_destination_through_an_alternate_root_spelling(self):
        # The containment refusal must compare resolved paths: with the root spelled
        # one way and the destination resolving to the other spelling of the same
        # directory (8.3 short name on a Windows runner, symlink alias on POSIX), the
        # literal-parents check let a copy destination inside the runtime through.
        self.record()
        spellings = root_spellings(self.root)
        if spellings is None:
            self.skipTest('No short/long spelling pair of the runtime root on this platform')
        short, long = spellings
        with self.assertRaises(SystemExit) as caught:
            admin.backup_copy(short, str(long / 'offmachine'))
        self.assertIn('inside the runtime', str(caught.exception))
        self.assertIn(os.path.normcase(str(long.resolve())), os.path.normcase(str(caught.exception)))
        self.assertFalse((long / 'offmachine').exists())

    def test_the_copy_refuses_a_relative_destination(self):
        self.record()
        stdout, stderr, code = self.run_admin('backup-copy', 'relative-copy')
        self.assertNotEqual(code, 0)
        self.assertIn('absolute', stderr)

    def test_the_copy_holds_each_project_lock_while_copying_its_pair(self):
        self.record()
        locked = []
        real_lock = admin.backup_lock

        @contextlib.contextmanager
        def recording_lock(root, name):
            locked.append(name)
            with real_lock(root, name):
                yield

        with patch.object(admin, 'backup_lock', side_effect=recording_lock):
            stdout, stderr, code = self.run_admin('backup-copy', str(self.destination))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(locked, ['alpha', 'beta'])
        # The project coordination lock is taken too (the runtime `fcntl` is a mock).
        self.assertGreaterEqual(self.fcntl.flock.call_count, 2)

    def test_the_copy_replaces_a_stale_destination_instead_of_merging(self):
        self.record()
        (self.root / 'backups' / 'alpha' / 'chunk').write_text('native bytes', encoding='utf-8')
        (self.destination / 'alpha').mkdir(parents=True)
        (self.destination / 'alpha' / 'STALE-junk-file').write_text('x', encoding='utf-8')
        stdout, stderr, code = self.run_admin('backup-copy', str(self.destination))
        self.assertEqual(code, 0, stderr)
        self.assertFalse((self.destination / 'alpha' / 'STALE-junk-file').exists())
        self.assertEqual((self.destination / 'alpha' / 'chunk').read_text(encoding='utf-8'),
                         'native bytes')
        # The staging directory and the previous-generation swap name are cleaned up.
        self.assertEqual(list(self.destination.glob('.backup-copy-staging-*')), [])
        self.assertEqual(list(self.destination.glob('*.previous')), [])

    def test_the_copy_includes_each_project_journal_snapshot(self):
        self.record()
        (self.root / 'backups' / 'alpha.http-operations.sqlite3').write_bytes(b'journal-bytes')
        stdout, stderr, code = self.run_admin('backup-copy', str(self.destination))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            (self.destination / 'alpha.http-operations.sqlite3').read_bytes(), b'journal-bytes')
        # A project without a journal snapshot is copied without one.
        self.assertFalse((self.destination / 'beta.http-operations.sqlite3').exists())
        self.assertIn('1 operation-journal snapshot(s)', stdout)

    def test_a_partial_failure_leaves_no_completeness_record_and_a_clear_error(self):
        self.record()
        (self.root / 'backups' / 'alpha' / 'chunk').write_text('native bytes', encoding='utf-8')
        real_copytree = shutil.copytree

        def flaky(source, target, **kwargs):
            if Path(source).name == 'beta':
                raise PermissionError('destination is not writable')
            return real_copytree(source, target, **kwargs)

        with patch('shutil.copytree', side_effect=flaky):
            stdout, stderr, code = self.run_admin('backup-copy', str(self.destination))
        self.assertNotEqual(code, 0)
        self.assertIn('backup-copy failed', stderr)
        self.assertIn('does not look complete', stderr)
        self.assertNotIn('Traceback', stderr)
        # The record that would make the destination look complete is not published.
        self.assertFalse((self.destination / admin.BACKUP_STATUS_NAME).exists())
        self.assertEqual(list(self.destination.glob('.backup-copy-staging-*')), [])

    def test_the_copy_rechecks_the_pair_under_the_lock(self):
        self.record()
        (self.root / 'backups' / 'beta').rmdir()
        with patch.object(admin, 'require_complete_problems', return_value=[]):
            stdout, stderr, code = self.run_admin('backup-copy', str(self.destination))
        self.assertNotEqual(code, 0)
        self.assertIn('no longer a complete pair', stderr)
        self.assertFalse((self.destination / admin.BACKUP_STATUS_NAME).exists())

    def _pair_with_manifest(self, name='alpha'):
        """Write the sidecar's manifest for the current native directory."""
        manifest, problem = admin.native_backup_manifest(self.root / 'backups' / name)
        self.assertIsNone(problem)
        sidecar = self.root / 'backups' / (name + '.coordination.json')
        complete = json.loads(sidecar.read_text(encoding='utf-8'))
        complete[admin.NATIVE_MANIFEST_KEY] = manifest
        sidecar.write_text(json.dumps(complete), encoding='utf-8')

    def test_the_copy_refuses_a_native_directory_that_changed_after_its_generation(self):
        # P3: a killed or interrupted run can leave the native directory partly rewritten
        # while its sidecar still records the previous generation. Copying that pair would
        # propagate a Dolt directory whose journal belongs to a different generation.
        self.record()
        (self.root / 'backups' / 'alpha' / 'chunk').write_text('one', encoding='utf-8')
        self._pair_with_manifest()
        (self.root / 'backups' / 'alpha' / 'half-written-chunk').write_text(
            'a newer, partial sync', encoding='utf-8')
        stdout, stderr, code = self.run_admin('backup-copy', str(self.destination))
        self.assertNotEqual(code, 0)
        self.assertIn('no longer matches the manifest', stderr)
        self.assertIn('alpha', stderr)
        self.assertFalse((self.destination / admin.BACKUP_STATUS_NAME).exists())
        self.assertEqual(list(self.destination.glob('.backup-copy-staging-*')), [])
        # Nothing was moved into place for the other project either.
        self.assertFalse((self.destination / 'beta').exists())

    def test_the_copy_refuses_a_native_directory_that_changed_while_it_was_copied(self):
        # A direct native writer bypasses the project's backup lock, so the directory can
        # still change during the long copytree; the after-check refuses the mixed copy.
        self.record()
        (self.root / 'backups' / 'alpha' / 'chunk').write_text('one', encoding='utf-8')
        self._pair_with_manifest()
        real_copytree = shutil.copytree

        def racing(source, target, **kwargs):
            result = real_copytree(source, target, **kwargs)
            if Path(source).name == 'alpha':
                (Path(source) / 'written-mid-copy').write_text('direct write', encoding='utf-8')
            return result

        with patch('shutil.copytree', side_effect=racing):
            stdout, stderr, code = self.run_admin('backup-copy', str(self.destination))
        self.assertNotEqual(code, 0)
        self.assertIn('changed while it was being copied', stderr)
        self.assertFalse((self.destination / admin.BACKUP_STATUS_NAME).exists())
        self.assertEqual(list(self.destination.glob('.backup-copy-staging-*')), [])

    def test_the_copy_reports_an_unverifiable_manifest_instead_of_refusing(self):
        # A sidecar written before the manifest existed must not be called clean, but it
        # must not block an off-machine copy either.
        self.record()
        stdout, stderr, code = self.run_admin('backup-copy', str(self.destination))
        self.assertEqual(code, 0, stderr)
        self.assertIn('could not be checked', stdout)
        self.assertTrue((self.destination / admin.BACKUP_STATUS_NAME).is_file())


if __name__ == '__main__':
    unittest.main()
