"""A refused authority merge never leaves restore-new half-way (kittrial-5bb.142 item 1).

``restore-new --restore-operators`` with the deployment lock held by another process used
to stop with rc 1 after the destination existed: the operator was not re-granted, and the
verifier merge and the operation-journal restore were skipped, while a second restore-new
was refused because the destination now existed. The merges now run last and a refused
one is reported as a completed restore with the exact commands to re-grant.

kittrial-5bb.144: that report is the last thing the restore prints, one wait covers both
lists, the exit status is 3 (restore complete, authority not re-granted), and
``backup-authority`` shows afterwards what a backup records against this installation.

The lock is held by a real second process. The wait budget is patched to zero, so the
refusal comes on the first attempt and no test waits on the wall clock.
"""
import contextlib
import io
import json
import shlex
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import admin
from test_backup_native_sync import journal_ids, write_live_journal, write_pair
from test_backup_native_sync import RuntimeCase
import test_restore_native_sql as native

try:
    import fcntl as real_fcntl
except ImportError:   # pragma: no cover - Windows
    real_fcntl = None


@unittest.skipIf(real_fcntl is None, 'flock is POSIX-only')
class RefusedAuthorityMergeTests(RuntimeCase):
    # The restore-new harness of test_restore_native_sql, without re-running its tests.
    fake_add_project = native.RestoreNewCommandCase.fake_add_project
    fake_run_bd = native.RestoreNewCommandCase.fake_run_bd
    fake_spawn = native.RestoreNewCommandCase.fake_spawn
    run_restore = native.RestoreNewCommandCase.run_restore

    def setUp(self):
        super().setUp()
        write_pair(self.root, 'alpha', files={'.merge-context.json': {'holder': 'prior'}})
        self.calls = []
        # RuntimeCase installs a fake fcntl; the lock here must be the real one.
        patcher = patch.dict(sys.modules, {'fcntl': real_fcntl})
        patcher.start()
        self.addCleanup(patcher.stop)
        bundle = self.root / 'backups' / 'alpha.coordination.json'
        record = json.loads(bundle.read_text(encoding='utf-8'))
        record.update({'operators': ['ops-recorded', 'ops-second'], 'verifiers': ['verifier-recorded']})
        bundle.write_text(json.dumps(record), encoding='utf-8')
        self.marker = self.root / 'deployment.private.json'

    def hold_lock(self):
        holder = subprocess.Popen([sys.executable, '-c', textwrap.dedent('''
            import fcntl, sys, time
            handle = open(sys.argv[1], 'a')
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)   # never wait: a leak fails fast
            except BlockingIOError:
                print('busy', flush=True)
                sys.exit(1)
            print('held', flush=True)
            sys.stdin.read()
        '''), str(self.root / admin.REVIEW_WRITES_LOCK)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait, 30)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        return holder

    def release(self, holder):
        holder.stdin.close()      # the holder reads to end of input, then exits
        holder.wait(30)

    def restore_while_held(self, *flags):
        holder = self.hold_lock()
        before = self.marker.read_bytes()
        try:
            with patch.object(admin, 'DEPLOYMENT_LOCK_WAIT_SECONDS', 0):
                stdout, stderr, code = self.run_restore(*flags)
        except BaseException:
            self.release(holder)
            raise
        self.assertEqual(self.marker.read_bytes(), before)       # nothing was re-granted
        return holder, stdout, stderr, code

    def run_restore_one_stream(self, *flags):
        """restore-new with stdout and stderr in ONE stream, in the order they were written."""
        both = io.StringIO()
        code = 0
        with patch.object(admin, 'add_project', side_effect=self.fake_add_project), \
                patch.object(admin, 'run_bd', side_effect=self.fake_run_bd), \
                patch.object(admin, 'spawn_sync_client', side_effect=self.fake_spawn), \
                patch.object(admin, 'sql', side_effect=native.identity_sql(native.SOURCE_ID)), \
                patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'restore-new', 'alpha', 'beta', *flags]), \
                patch.object(admin, 'root_path', return_value=self.root), \
                contextlib.redirect_stdout(both), contextlib.redirect_stderr(both):
            try:
                admin.main()
            except SystemExit as exit:
                code = exit.code
        return both.getvalue(), code

    def regrant_commands(self, stderr):
        return [command for command in (shlex.split(line.strip()) for line in stderr.splitlines()
                                        if line.strip().startswith('admin.py --root '))
                if command[4:5] == ['add']]

    def test_a_held_lock_leaves_a_complete_restore_and_the_commands_to_regrant(self):
        write_live_journal(admin.journal_snapshot_path(self.root, 'alpha'), ['op-1', 'op-2'])
        holder, stdout, stderr, code = self.restore_while_held('--restore-operators', '--restore-verifiers')
        self.assertEqual(code, admin.RESTORE_AUTHORITY_NOT_REGRANTED, stderr)
        self.assertEqual(admin.RESTORE_AUTHORITY_NOT_REGRANTED, 3)
        destination = self.root / 'projects' / 'beta'
        # Everything but the authority is restored: the coordination files and the journal.
        self.assertEqual(json.loads((destination / '.merge-context.json').read_text(encoding='utf-8')),
                         {'holder': 'prior'})
        self.assertEqual(journal_ids(destination / admin.JOURNAL_STORE_NAME), {'op-1', 'op-2'})
        self.assertIn('Restored only into the newly created project', stdout)
        # One warning names both lists: one wait, one refusal, nothing skipped.
        self.assertEqual(stderr.count('WARNING:'), 1)
        self.assertIn('the restore is complete, but deployment authority the backup records was NOT '
                      're-granted: operators (--restore-operators): ops-recorded, ops-second; verifiers '
                      '(--restore-verifiers): verifier-recorded.', stderr)
        self.assertIn('backup-authority alpha', stderr)
        self.assertIn('Do not repeat the restore', stderr)
        self.assertNotIn('Run the command again', stderr)
        commands = self.regrant_commands(stderr)
        flags = ['--actor', 'OPERATOR', '--reason', 'TEXT']
        self.assertEqual(commands, [
            ['admin.py', '--root', str(self.root), 'operators', 'add', 'ops-recorded'] + flags,
            ['admin.py', '--root', str(self.root), 'operators', 'add', 'ops-second'] + flags,
            ['admin.py', '--root', str(self.root), 'verifiers', 'add', 'verifier-recorded'] + flags])
        self.assertIn('Replace OPERATOR in --actor and TEXT in --reason', stderr)
        self.release(holder)
        # kittrial-5bb.229 finding 3: a command copied as printed is REFUSED - the placeholders
        # must be replaced, so the audit never records an operator named OPERATOR with the entry
        # counted as attributed.
        with self.assertRaisesRegex(ValueError, 'placeholder'):
            self.run_admin(*commands[0][3:])
        replaced = [command[:6] + ['--actor', 'james', '--reason', 'after a rollback']
                    for command in commands]
        for command in replaced:
            _, err, rc = self.run_admin(*command[3:])
            self.assertEqual(rc, 0, err)
        self.assertEqual(admin.operators(self.root), frozenset({'ops-recorded', 'ops-second'}))
        self.assertEqual(admin.verifiers(self.root), frozenset({'verifier-recorded'}))

    def test_each_flag_alone_with_a_held_lock(self):
        for flag, kind in (('--restore-operators', 'operators'), ('--restore-verifiers', 'verifiers')):
            with self.subTest(flag=flag):
                destination = self.root / 'projects' / 'beta'
                if destination.exists():
                    import shutil
                    shutil.rmtree(destination)
                holder, stdout, stderr, code = self.restore_while_held(flag)
                self.release(holder)
                self.assertEqual(code, 3, stderr)
                self.assertIn('was NOT re-granted: %s (%s): ' % (kind, flag), stderr)
                self.assertIn('Backup has no operation-journal snapshot', stdout)
                self.assertEqual({command[3] for command in self.regrant_commands(stderr)}, {kind})

    def test_a_free_lock_still_regrants_after_the_journal(self):
        write_live_journal(admin.journal_snapshot_path(self.root, 'alpha'), ['op-1'])
        order = []
        real_restore_journal, real_merge = admin.restore_journal, admin.merge_authority
        with patch.object(admin, 'restore_journal',
                          side_effect=lambda *a: order.append('journal') or real_restore_journal(*a)), \
                patch.object(admin, 'merge_authority',
                             side_effect=lambda *a: order.append('operators') or real_merge(*a)):
            stdout, stderr, code = self.run_restore('--restore-operators')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(order, ['journal', 'operators'])
        self.assertIn('Re-granted operator allowlist entries from the backup (--restore-operators): '
                      'ops-recorded, ops-second', stdout)
        self.assertNotIn('NOT re-granted', stderr)

    def test_the_warning_is_the_last_thing_the_restore_prints(self):
        holder = self.hold_lock()
        try:
            with patch.object(admin, 'DEPLOYMENT_LOCK_WAIT_SECONDS', 0):
                output, code = self.run_restore_one_stream('--restore-operators', '--restore-verifiers')
        finally:
            self.release(holder)
        self.assertEqual(code, 3, output)
        lines = output.rstrip('\n').splitlines()
        warning = next(index for index, line in enumerate(lines) if line.startswith('WARNING:'))
        # Everything else, the success line included, comes before it.
        self.assertLess(next(index for index, line in enumerate(lines)
                             if line.startswith('Restored only into the newly created project')), warning)
        self.assertEqual(lines[-1], 'restore-new exits 3: the restore is complete, but the authority above was '
                                    'NOT re-granted.')
        self.assertTrue(all(line.startswith('  admin.py --root ') for line in lines[warning + 1:warning + 4]))
        self.assertTrue(lines[-2].startswith('  admin.py --root ') and lines[-2].endswith(' backup-authority alpha'))

    def test_both_lists_cost_one_wait(self):
        # A fake clock: the full 10 s budget, measured, with no wall-clock wait.
        holder = self.hold_lock()
        now = [500.0]

        def sleep(seconds):
            now[0] += seconds

        try:
            with patch.object(admin.time, 'monotonic', side_effect=lambda: now[0]), \
                    patch.object(admin.time, 'sleep', side_effect=sleep):
                stdout, stderr, code = self.run_restore('--restore-operators', '--restore-verifiers')
        finally:
            self.release(holder)
        self.assertEqual(code, 3, stderr)
        waited = now[0] - 500.0
        self.assertGreaterEqual(waited, admin.DEPLOYMENT_LOCK_WAIT_SECONDS)
        self.assertLess(waited, admin.DEPLOYMENT_LOCK_WAIT_SECONDS + 2 * admin.DEPLOYMENT_LOCK_POLL_SECONDS)

    def test_backup_authority_shows_what_a_backup_records_against_this_installation(self):
        self.marker.write_text(json.dumps(dict(json.loads(self.marker.read_text(encoding='utf-8')),
                                               operators=['ops-second', 'ops-here'])), encoding='utf-8')
        before = self.marker.read_bytes()
        stdout, stderr, code = self.run_admin('backup-authority', 'alpha')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), {
            'project': 'alpha', 'sidecar': 'backups/alpha.coordination.json',
            'operators': {'recorded': ['ops-recorded', 'ops-second'], 'listed_here': ['ops-here', 'ops-second'],
                          'not_listed_here': ['ops-recorded']},
            'verifiers': {'recorded': ['verifier-recorded'], 'listed_here': [],
                          'not_listed_here': ['verifier-recorded']}})
        self.assertEqual(self.marker.read_bytes(), before)                      # read only
        self.assertFalse((self.root / admin.REVIEW_WRITES_LOCK).exists())       # takes no lock

    def cli(self, *argv):
        """admin.py as the command line runs it (run_main): a refusal is rc 1 and one line."""
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                patch.object(admin, 'root_path', return_value=self.root), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                admin.run_main()
            except SystemExit as exit:
                code = exit.code if isinstance(exit.code, int) else 1
                if isinstance(exit.code, str):
                    stderr.write(exit.code)
        return stdout.getvalue(), stderr.getvalue(), code

    def test_backup_authority_tells_no_backup_damage_and_legacy_apart(self):
        # kittrial-5bb.145: these three all read `sidecar: null`, empty lists, rc 0.
        bundle = self.root / 'backups' / 'alpha.coordination.json'
        fallback = self.root / 'backups' / 'alpha.coordination.last-complete.json'
        # 1. A name with no backup: refused, rc 1, nothing on stdout.
        stdout, stderr, code = self.cli('backup-authority', 'gamma')
        self.assertEqual((code, stdout), (1, ''), stderr)
        self.assertIn('No such backup: backups/gamma does not exist', stderr)
        # 2. Both copies damaged: refused, each copy named with why, and the lists untrusted.
        record = bundle.read_text(encoding='utf-8')
        bundle.write_text('{"schema_version": 1, "status": "complete", "files"', encoding='utf-8')
        fallback.write_text(json.dumps({'schema_version': 1, 'status': 'pending', 'files': {}}), encoding='utf-8')
        stdout, stderr, code = self.cli('backup-authority', 'alpha')
        self.assertEqual((code, stdout), (1, ''), stderr)
        self.assertIn('The coordination sidecar of backup alpha is damaged: '
                      'backups/alpha.coordination.json: it is not valid JSON; '
                      'backups/alpha.coordination.last-complete.json: its status is "pending", not complete. '
                      'What this backup records cannot be read, so no operator or verifier list from it can '
                      'be trusted.', stderr)
        # Only one copy, damaged: the same refusal, naming that copy alone.
        fallback.unlink()
        bundle.write_text(json.dumps(['not', 'a', 'sidecar']), encoding='utf-8')
        _, stderr, code = self.cli('backup-authority', 'alpha')
        self.assertEqual(code, 1)
        self.assertIn('damaged: backups/alpha.coordination.json: it is not a coordination sidecar of schema 1. ', stderr)
        # A damaged canonical copy with a usable fallback: the fallback's lists, and the
        # passed-over copy named.
        fallback.write_text(record, encoding='utf-8')
        stdout, stderr, code = self.cli('backup-authority', 'alpha')
        self.assertEqual(code, 0, stderr)
        result = json.loads(stdout)
        self.assertEqual(result['sidecar'], 'backups/alpha.coordination.last-complete.json')
        self.assertEqual(result['operators']['recorded'], ['ops-recorded', 'ops-second'])
        self.assertEqual(result['unusable'], [{'copy': 'backups/alpha.coordination.json',
                                               'problem': 'it is not a coordination sidecar of schema 1'}])
        self.assertNotIn('note', result)
        # 3. No sidecar at all: a legacy backup, rc 0, and said so.
        bundle.unlink()
        fallback.unlink()
        stdout, stderr, code = self.cli('backup-authority', 'alpha')
        self.assertEqual(code, 0, stderr)
        result = json.loads(stdout)
        self.assertEqual((result['sidecar'], result['operators']['recorded'], result['verifiers']['recorded']),
                         (None, [], []))
        self.assertIn('legacy backup', result['note'])
        self.assertNotIn('unusable', result)

    def test_backup_authority_checks_the_name_before_anything_else(self):
        # An invalid name is refused as a name, not reported as a missing backup.
        for name in ('Alpha', '../projects', 'a'):
            with self.subTest(name=name):
                _, stderr, code = self.cli('backup-authority', name)
                self.assertEqual(code, 1)
                self.assertIn('Project: 2-24 lowercase letters/digits', stderr)
                self.assertNotIn('No such backup', stderr)

    def test_merge_authority_refuses_an_invalid_name_with_nothing_changed(self):
        before = self.marker.read_bytes()
        for operators, verifiers, label in ((['ok-op', 'bad name'], [], 'Invalid operator identity'),
                                            ([], ['ok-ver', 'bad;name'], 'Invalid verifier identity'),
                                            (['-leading'], ['ok'], 'Invalid operator identity')):
            with self.subTest(operators=operators, verifiers=verifiers):
                with self.assertRaisesRegex(ValueError, label):
                    admin.merge_authority(self.root, operators, verifiers)
        self.assertEqual(self.marker.read_bytes(), before)

    def test_restore_coordination_called_directly_prints_the_warning(self):
        (self.root / 'projects' / 'beta').mkdir()
        holder = self.hold_lock()
        err = io.StringIO()
        try:
            with patch.object(admin, 'DEPLOYMENT_LOCK_WAIT_SECONDS', 0), contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(admin.restore_coordination(self.root, 'alpha', 'beta', restore_operators=True,
                                                           restore_verifiers=True))
        finally:
            self.release(holder)
        self.assertIn('WARNING: the restore is complete, but deployment authority the backup records was NOT '
                      're-granted: operators (--restore-operators): ops-recorded, ops-second; verifiers '
                      '(--restore-verifiers): verifier-recorded.', err.getvalue())

    def test_a_legacy_backup_does_not_run_the_authority_step(self):
        (self.root / 'backups' / 'alpha.coordination.json').unlink()
        with patch.object(admin, 'restore_authority', side_effect=AssertionError('authority step ran')), \
                patch.object(admin, 'merge_authority', side_effect=AssertionError('merge ran')):
            stdout, stderr, code = self.run_restore('--restore-operators', '--restore-verifiers')
        self.assertEqual(code, 0, stderr)
        self.assertIn('Legacy backup has no coordination journal', stdout)
        self.assertIn('This backup has no coordination sidecar, so it records no operators or verifiers', stdout)
        stdout, stderr, code = self.run_restore()
        self.assertNotIn('records no operators or verifiers', stdout)    # said only when a flag asked


    def test_a_sidecar_with_session_and_request_files_is_restored_whole_with_the_lock_held(self):
        request = '.coordination-requests/' + 'a' * 64 + '.json'
        receipt = {'sha256': 'b' * 64, 'status': 'complete', 'id': 'source-1.1'}
        sessions = {'schema_version': 1, 'records': {}}
        bundle = self.root / 'backups' / 'alpha.coordination.json'
        record = json.loads(bundle.read_text(encoding='utf-8'))
        record['files'].update({'.sessions.json': sessions, request: receipt})
        bundle.write_text(json.dumps(record), encoding='utf-8')
        holder, stdout, stderr, code = self.restore_while_held('--restore-operators')
        self.release(holder)
        self.assertEqual(code, 3, stderr)
        destination = self.root / 'projects' / 'beta'
        self.assertEqual(json.loads((destination / '.sessions.json').read_text(encoding='utf-8')), sessions)
        self.assertEqual(json.loads((destination / request).read_text(encoding='utf-8')), receipt)
        self.assertEqual(json.loads((destination / '.merge-context.json').read_text(encoding='utf-8')),
                         {'holder': 'prior'})

    def test_a_journal_step_failing_after_the_destination_exists_grants_nothing(self):
        # The authority step comes after the journal: a journal failure ends the restore with
        # the failure notice (rc 1, as for every other step after the destination exists) and
        # re-grants nothing, so nothing deployment-wide is left from a failed restore.
        write_live_journal(admin.journal_snapshot_path(self.root, 'alpha'), ['op-1'])
        before = self.marker.read_bytes()
        with patch.object(admin, 'restore_journal', side_effect=OSError('disk full for the test')), \
                patch.object(admin, 'merge_authority', side_effect=AssertionError('authority merged')):
            with self.assertRaises(OSError):
                self.run_restore('--restore-operators', '--restore-verifiers')
        self.assertEqual(self.marker.read_bytes(), before)
        self.assertTrue((self.root / 'projects' / 'beta').is_dir())


class RegrantCommandQuotingTests(unittest.TestCase):
    """Every printed command is shell-quoted. Identities and root paths cannot hold a space or
    a quote today (recovery.identity, admin.root_path), so this pins the notice itself. Since
    kittrial-5bb.192 revision 3 the commands carry --actor/--reason, with the placeholders the
    list commands' own refusals use when the restore named nobody: following a printed command
    must not leave the unattributed entry the notice exists to avoid."""

    def test_an_actor_with_a_space_and_a_quote_round_trips_through_the_shell(self):
        root = '/srv/run time'            # a string: a Path would print with backslashes on Windows
        warning = admin.authority_not_regranted(root, 'alpha', ["o'brien x"], ['v "q"'])
        commands = [shlex.split(line.strip()) for line in warning.splitlines()
                    if line.strip().startswith('admin.py --root ')]
        flags = ['--actor', 'OPERATOR', '--reason', 'TEXT']
        self.assertEqual(commands, [['admin.py', '--root', '/srv/run time', 'operators', 'add', "o'brien x"] + flags,
                                    ['admin.py', '--root', '/srv/run time', 'verifiers', 'add', 'v "q"'] + flags,
                                    ['admin.py', '--root', '/srv/run time', 'backup-authority', 'alpha']])
        # What the restore was GIVEN is used where it had it, shell-quoted like the rest.
        warning = admin.authority_not_regranted(root, 'alpha', ["o'brien x"], [], actor='james',
                                                reason='after a rollback')
        commands = [shlex.split(line.strip()) for line in warning.splitlines()
                    if line.strip().startswith('admin.py --root ')]
        self.assertEqual(commands, [['admin.py', '--root', '/srv/run time', 'operators', 'add', "o'brien x",
                                     '--actor', 'james', '--reason', 'after a rollback'],
                                    ['admin.py', '--root', '/srv/run time', 'backup-authority', 'alpha']])


if __name__ == '__main__':
    unittest.main()
