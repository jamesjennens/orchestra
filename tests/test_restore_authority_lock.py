"""A refused authority merge never leaves restore-new half-way (kittrial-5bb.142 item 1).

``restore-new --restore-operators`` with the deployment lock held by another process used
to stop with rc 1 after the destination existed: the operator was not re-granted, and the
verifier merge and the operation-journal restore were skipped, while a second restore-new
was refused because the destination now existed. The merges now run last and a refused
one is reported as a completed restore with the exact commands to re-grant.

The lock is held by a real second process. The wait budget is patched to zero, so the
refusal comes on the first attempt and no test waits on the wall clock.
"""
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

    def regrant_commands(self, stderr):
        return [shlex.split(line.strip()) for line in stderr.splitlines()
                if line.strip().startswith('admin.py --root ')]

    def test_a_held_lock_leaves_a_complete_restore_and_the_commands_to_regrant(self):
        write_live_journal(admin.journal_snapshot_path(self.root, 'alpha'), ['op-1', 'op-2'])
        holder, stdout, stderr, code = self.restore_while_held('--restore-operators', '--restore-verifiers')
        self.assertEqual(code, 0, stderr)
        destination = self.root / 'projects' / 'beta'
        # Everything but the authority is restored: the coordination files and the journal.
        self.assertEqual(json.loads((destination / '.merge-context.json').read_text(encoding='utf-8')),
                         {'holder': 'prior'})
        self.assertEqual(journal_ids(destination / admin.JOURNAL_STORE_NAME), {'op-1', 'op-2'})
        self.assertIn('Restored only into the newly created project', stdout)
        # A refused operator merge does not skip the verifiers: both are reported.
        self.assertIn('the restore is complete, but operators from the backup were NOT re-granted '
                      '(--restore-operators): ops-recorded, ops-second', stderr)
        self.assertIn('the restore is complete, but verifiers from the backup were NOT re-granted '
                      '(--restore-verifiers): verifier-recorded', stderr)
        self.assertIn('Do not repeat the restore', stderr)
        self.assertNotIn('Run the command again', stderr)
        commands = self.regrant_commands(stderr)
        self.assertEqual(commands, [
            ['admin.py', '--root', str(self.root), 'operators', 'add', 'ops-recorded'],
            ['admin.py', '--root', str(self.root), 'operators', 'add', 'ops-second'],
            ['admin.py', '--root', str(self.root), 'verifiers', 'add', 'verifier-recorded']])
        # The printed commands are the whole remedy once the holder is gone.
        self.release(holder)
        for command in commands:
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
                self.assertEqual(code, 0, stderr)
                self.assertIn('%s from the backup were NOT re-granted (%s)' % (kind, flag), stderr)
                self.assertIn('Backup has no operation-journal snapshot', stdout)
                self.assertEqual({command[3] for command in self.regrant_commands(stderr)}, {kind})

    def test_a_free_lock_still_regrants_after_the_journal(self):
        write_live_journal(admin.journal_snapshot_path(self.root, 'alpha'), ['op-1'])
        order = []
        real_restore_journal, real_merge = admin.restore_journal, admin.merge_operators
        with patch.object(admin, 'restore_journal',
                          side_effect=lambda *a: order.append('journal') or real_restore_journal(*a)), \
                patch.object(admin, 'merge_operators',
                             side_effect=lambda *a: order.append('operators') or real_merge(*a)):
            stdout, stderr, code = self.run_restore('--restore-operators')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(order, ['journal', 'operators'])
        self.assertIn('Re-granted operator allowlist entries from the backup (--restore-operators): '
                      'ops-recorded, ops-second', stdout)
        self.assertNotIn('NOT re-granted', stderr)


if __name__ == '__main__':
    unittest.main()
