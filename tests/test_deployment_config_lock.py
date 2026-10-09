"""Every writer of deployment.private.json holds one deployment lock (kittrial-5bb.136).

The switches (`review-writes`, `checkpoint-provenance-writes`), `operators add|remove`,
`verifiers add|remove` and the restore merges of operators and verifiers all rewrite the
same file. Each pair is run as two real processes: process A stops inside its critical
section, between its read and its write, until process B reports it has finished (at
most PAUSE seconds); B then makes its change. With the lock B cannot finish before A has
written, so both changes are in the file whatever the timing. Without it, B finishes
inside A's pause and A's write throws B's change away, which is what these tests catch.
The pause only bounds how long a lock-less run takes to show the race; it decides
nothing when the lock is there.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import admin
import project_creation

REPO = str(Path(__file__).resolve().parent.parent)
PAUSE = 0.5

CHILD = textwrap.dedent('''
    import json, os, sys, time
    from pathlib import Path
    sys.path.insert(0, {repo!r})
    import admin
    root, writer, role = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    real = admin.atomic_private_write
    def paused(path, text):
        if Path(path).name == 'deployment.private.json' and role == 'first':
            (root / 'first.ready').touch()
            deadline = time.monotonic() + {pause!r}
            while not (root / 'second.done').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
        return real(path, text)
    admin.atomic_private_write = paused
    def cli(*argv):
        sys.argv = ['admin.py', '--root', str(root), *argv]
        admin.main()
    WRITERS = {{
        'checkpoint-on': lambda: admin.checkpoint_provenance_switch(root, 'on', 'ops'),
        'review-on': lambda: admin.review_writes_command(root, 'ops', 'on'),
        'operators-add': lambda: cli('operators', 'add', 'added-op'),
        'operators-remove': lambda: cli('operators', 'remove', 'gone-op', '--confirm-revoke'),
        'verifiers-add': lambda: cli('verifiers', 'add', 'added-ver'),
        'verifiers-remove': lambda: cli('verifiers', 'remove', 'gone-ver', '--confirm-revoke'),
        'merge-operators': lambda: admin.merge_operators(root, ['restored-op']),
        'merge-verifiers': lambda: admin.merge_verifiers(root, ['restored-ver']),
        'server-limit': lambda: cli('project-creations', '--set-server-limit', '7', '--actor', 'ops'),
        'merge-authority': lambda: admin.merge_authority(root, ['both-op'], ['both-ver']),
    }}
    if role == 'second':
        (root / 'second.started').touch()
    WRITERS[writer]()
    if role == 'second':
        (root / 'second.done').touch()
''').format(repo=REPO, pause=PAUSE)

#: What each writer leaves in the file: a predicate on the final configuration.
EFFECTS = {
    'checkpoint-on': lambda cfg: cfg.get('checkpoint_provenance_writes') is True
    and [entry['action'] for entry in cfg.get('checkpoint_provenance_audit', [])] == ['on'],
    'review-on': lambda cfg: cfg.get('review_workflow_writes') is True,
    'operators-add': lambda cfg: 'added-op' in cfg.get('operators', []),
    'operators-remove': lambda cfg: 'gone-op' not in cfg.get('operators', []),
    'verifiers-add': lambda cfg: 'added-ver' in cfg.get('verifiers', []),
    'verifiers-remove': lambda cfg: 'gone-ver' not in cfg.get('verifiers', []),
    'merge-operators': lambda cfg: 'restored-op' in cfg.get('operators', []),
    'merge-verifiers': lambda cfg: 'restored-ver' in cfg.get('verifiers', []),
    # The limit of project databases (kittrial-5bb.118 part 2): the setting and its audit entry.
    # The one-wait merge of both lists restore-new uses (kittrial-5bb.144).
    'merge-authority': lambda cfg: 'both-op' in cfg.get('operators', []) and 'both-ver' in cfg.get('verifiers', []),
    'server-limit': lambda cfg: cfg.get('project_database_limit') == 7
    and [(entry['actor'], entry['to']) for entry in cfg.get('project_database_limit_audit', [])] == [('ops', 7)],
}

#: Each writer against a switch flip, in both orders, and the two switches against the
#: allowlists: every pair the review raced, and the verifier and restore merges it did not.
PAIRS = [(writer, 'checkpoint-on') for writer in EFFECTS if writer not in ('checkpoint-on', 'review-on')]
PAIRS += [(second, first) for first, second in PAIRS]
PAIRS += [('review-on', 'operators-add'), ('operators-add', 'review-on'), ('review-on', 'checkpoint-on')]
PAIRS += [('server-limit', 'operators-add'), ('operators-add', 'server-limit'),
          ('server-limit', 'review-on'), ('review-on', 'server-limit')]
# The project database limit against the restore merges (kittrial-5bb.144).
PAIRS += [('server-limit', 'merge-authority'), ('merge-authority', 'server-limit'),
          ('server-limit', 'merge-operators'), ('merge-verifiers', 'server-limit')]


@unittest.skipIf(sys.platform == 'win32', 'flock is POSIX-only')
class DeploymentWritersShareOneLockTests(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.child = self.temp / 'child.py'
        self.child.write_text(CHILD, encoding='utf-8')
        self.env = {key: value for key, value in os.environ.items() if key != 'ORCHESTRA_OPERATORS'}

    def deployment(self, name):
        root = self.temp / name
        root.mkdir()
        (root / 'deployment.private.json').write_text(json.dumps({
            'password': 'test-only', 'port': 1, 'unit': 'beads-test.service', 'unrelated': 'keep',
            'operators': ['ops', 'gone-op'], 'verifiers': ['ver', 'gone-ver']}), encoding='utf-8')
        return root

    def spawn(self, root, writer, role):
        return subprocess.Popen([sys.executable, str(self.child), str(root), writer, role],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)

    def wait_for(self, path, process):
        for _ in range(3000):                       # up to 30 s for the interpreter to start
            if path.exists() or process.poll() is not None:
                return
            time.sleep(0.01)
        self.fail('%s never appeared' % path.name)

    def test_two_processes_writing_at_once_keep_both_changes(self):
        for index, (first, second) in enumerate(PAIRS):
            with self.subTest(first=first, second=second):
                root = self.deployment('pair-%d' % index)
                a = self.spawn(root, first, 'first')
                self.wait_for(root / 'first.ready', a)     # A is inside its critical section
                b = self.spawn(root, second, 'second')
                out_a, err_a = a.communicate(timeout=60)
                out_b, err_b = b.communicate(timeout=60)
                self.assertEqual(a.returncode, 0, err_a)
                self.assertEqual(b.returncode, 0, err_b)
                cfg = json.loads((root / 'deployment.private.json').read_text(encoding='utf-8'))
                self.assertTrue(EFFECTS[first](cfg), '%s was lost: %r' % (first, cfg))
                self.assertTrue(EFFECTS[second](cfg), '%s was lost: %r' % (second, cfg))
                self.assertEqual(cfg['unrelated'], 'keep')
                self.assertEqual(cfg['password'], 'test-only')


@unittest.skipIf(sys.platform == 'win32', 'flock is POSIX-only')
class DeploymentLockWaitTests(unittest.TestCase):
    """kittrial-5bb.136 item 4: a live holder no longer blocks a change for good."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.marker = self.root / 'deployment.private.json'
        self.marker.write_text(json.dumps({'password': 'test-only', 'operators': ['ops']}), encoding='utf-8')

    def hold(self):
        import fcntl
        handle = (self.root / admin.REVIEW_WRITES_LOCK).open('a')
        fcntl.flock(handle, fcntl.LOCK_EX)
        self.addCleanup(handle.close)
        return handle

    def flip(self, action):
        with redirect_stderr(StringIO()):
            return admin.checkpoint_provenance_switch(self.root, action, 'ops')

    def bounded(self, change):
        """Run ``change`` in a daemon thread: a wait on the lock that never ends fails the
        test (the join gives up) instead of hanging the suite. Returns ``(result, error)``."""
        outcome = []

        def attempt():
            try:
                outcome.append((change(), None))
            except ValueError as error:
                outcome.append((None, str(error)))

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(20)   # the fixed code answers at once; only an unbounded wait reaches this
        self.assertFalse(worker.is_alive(), 'the change waited for the held lock without a bound')
        return outcome[0]

    def test_a_held_lock_refuses_the_change_with_nothing_changed_and_status_still_answers(self):
        self.hold()
        before = self.marker.read_bytes()
        # A budget of zero: one attempt, then the refusal; no wall-clock wait in the test.
        # Each change runs in a daemon thread, so a wait that never ends fails this test
        # (the join gives up) instead of hanging the suite.
        with patch.object(admin, 'DEPLOYMENT_LOCK_WAIT_SECONDS', 0):
            for change in (lambda: self.flip('on'),
                           lambda: admin.review_writes_command(self.root, 'ops', 'on'),
                           lambda: admin.merge_operators(self.root, ['another']),
                           lambda: admin.merge_verifiers(self.root, ['another']),
                           lambda: project_creation.set_server_limit(self.root, 7, 'ops')):
                _, error = self.bounded(change)
                self.assertRegex(error or '', r'^Nothing was changed: another change to '
                                              r'deployment\.private\.json still holds its lock')
        self.assertEqual(self.marker.read_bytes(), before)
        self.assertFalse(self.flip('status')['checkpoint_provenance_writes'])   # reads take no lock
        result, _ = admin.review_writes_command(self.root, 'ops', 'status')
        self.assertFalse(result['review_workflow_writes'])

    def test_the_command_that_sets_the_server_limit_exits_1_and_says_so(self):
        # kittrial-5bb.118 part 2: admin.py project-creations --set-server-limit under a held lock.
        self.hold()
        before = self.marker.read_bytes()
        done = subprocess.run([sys.executable, '-c', textwrap.dedent('''
            import sys
            sys.path.insert(0, sys.argv[1])
            import admin
            admin.DEPLOYMENT_LOCK_WAIT_SECONDS = 0
            sys.argv = ['admin.py', '--root', sys.argv[2], 'project-creations', '--set-server-limit', '7', '--actor', 'ops']
            admin.run_main()
        '''), REPO, str(self.root)], capture_output=True, text=True, timeout=60,
            env={key: value for key, value in os.environ.items() if key != 'ORCHESTRA_OPERATORS'})
        self.assertEqual((done.returncode, done.stdout), (1, ''), done.stderr)
        self.assertRegex(done.stderr.strip(), r'^ValueError: Nothing was changed: another change to '
                                              r'deployment\.private\.json still holds its lock \(\.review-writes\.lock\) '
                                              r'after 0 s\. Run the command again')
        self.assertEqual(self.marker.read_bytes(), before)
        self.assertEqual(project_creation.server_limit(self.root), project_creation.SERVER_LIMIT_DEFAULT)

    def test_a_lock_released_during_the_wait_is_taken(self):
        holder = self.hold()
        waits = []

        def sleep(seconds):                       # the wait's own sleep: release on the first one
            waits.append(seconds)
            holder.close()

        with patch.object(admin.time, 'sleep', side_effect=sleep):
            result, error = self.bounded(lambda: self.flip('on'))
        self.assertIsNone(error)
        self.assertTrue(result['checkpoint_provenance_writes'])
        self.assertEqual(waits, [admin.DEPLOYMENT_LOCK_POLL_SECONDS])

    def test_the_wait_lasts_ten_seconds_of_the_clock_it_reads(self):
        # A fake clock: each poll's sleep advances it, so the bound is measured exactly and
        # the test takes no wall-clock time (kittrial-5bb.142: a 100 s wait survived).
        self.hold()
        now = [1000.0]

        def sleep(seconds):
            now[0] += seconds

        with patch.object(admin.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(admin.time, 'sleep', side_effect=sleep):
            _, error = self.bounded(lambda: self.flip('on'))
        waited = now[0] - 1000.0
        self.assertRegex(error or '', r'still holds its lock \(\.review-writes\.lock\) after 10 s\.')
        self.assertGreaterEqual(waited, 10)
        self.assertLess(waited, 10 + 2 * admin.DEPLOYMENT_LOCK_POLL_SECONDS)

    def test_a_killed_holder_releases_the_lock(self):
        holder = subprocess.Popen([sys.executable, '-c', textwrap.dedent('''
            import fcntl, sys, time
            handle = open(sys.argv[1], 'a')
            fcntl.flock(handle, fcntl.LOCK_EX)
            print('held', flush=True)
            time.sleep(600)
        '''), str(self.root / admin.REVIEW_WRITES_LOCK)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        with patch.object(admin, 'DEPLOYMENT_LOCK_WAIT_SECONDS', 0):
            _, error = self.bounded(lambda: self.flip('on'))
        self.assertRegex(error or '', 'Nothing was changed')
        holder.send_signal(signal.SIGKILL)
        holder.wait(30)
        self.assertTrue(self.flip('on')['checkpoint_provenance_writes'])



@unittest.skipIf(sys.platform == 'win32', 'flock is POSIX-only')
class PrivateWriteLeftoverTests(unittest.TestCase):
    """kittrial-5bb.142 item 5: the temporary copy an interrupted write leaves is a full
    copy of the configuration, password included. It is created 0600 and the next locked
    write removes it."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.marker = self.root / 'deployment.private.json'
        self.marker.write_text(json.dumps({'password': 'test-only', 'operators': ['ops']}), encoding='utf-8')

    def flip(self, action):
        err = StringIO()
        with redirect_stderr(err):
            result = admin.checkpoint_provenance_switch(self.root, action, 'ops')
        return result, err.getvalue()

    def names(self):
        return sorted(path.name for path in self.root.iterdir())

    def test_a_writer_killed_mid_write_leaves_a_copy_the_next_locked_write_removes(self):
        killed = subprocess.run([sys.executable, '-c', textwrap.dedent('''
            import os, signal, sys
            sys.path.insert(0, sys.argv[2])
            import admin
            from pathlib import Path
            def die(*args):                       # killed between the copy and the rename
                os.kill(os.getpid(), signal.SIGKILL)
            admin.os.replace = die
            admin.review_writes_command(Path(sys.argv[1]), 'ops', 'on')
        '''), str(self.root), REPO], capture_output=True, text=True, timeout=60)
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stderr)
        leftovers = [name for name in self.names() if name.startswith('.deployment.private.json.')]
        self.assertEqual(len(leftovers), 1)
        leftover = self.root / leftovers[0]
        self.assertIn('test-only', leftover.read_text(encoding='utf-8'))     # the password
        self.assertEqual(leftover.stat().st_mode & 0o777, 0o600)
        # A read takes no lock and leaves it; the next locked write removes it.
        self.flip('status')
        self.assertTrue(leftover.exists())
        result, err = self.flip('on')
        self.assertTrue(result['checkpoint_provenance_writes'])
        self.assertFalse(leftover.exists())
        self.assertIn('Removed 1 temporary copy of deployment.private.json left by an interrupted write: '
                      + leftovers[0], err)
        self.assertEqual(json.loads(self.marker.read_text(encoding='utf-8'))['password'], 'test-only')

    def test_only_the_temporary_copies_are_removed_and_only_under_the_lock(self):
        leftovers = ['.deployment.private.json.abcd_123', '.deployment.private.json.zz99yy88']
        kept = ['.deployment.private.json.bak', 'deployment.private.json.abcd1234',
                '.deployment.private.json.abcd12345', '.deployment.private.json.ABCD-123']
        for name in leftovers + kept:
            (self.root / name).write_text('{"password": "test-only"}', encoding='utf-8')
        handle = (self.root / admin.REVIEW_WRITES_LOCK).open('a')
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX)
        with patch.object(admin, 'DEPLOYMENT_LOCK_WAIT_SECONDS', 0):
            with self.assertRaisesRegex(ValueError, 'Nothing was changed'):
                self.flip('on')
        handle.close()
        for name in leftovers:                  # a refused write removed nothing
            self.assertTrue((self.root / name).exists(), name)
        _, err = self.flip('on')
        self.assertIn('Removed 2 temporary copies', err)
        for name in leftovers:
            self.assertFalse((self.root / name).exists(), name)
        for name in kept:
            self.assertTrue((self.root / name).exists(), name)
        # Every writer that takes the lock does it, not only the switches.
        (self.root / leftovers[0]).write_text('{}', encoding='utf-8')
        with redirect_stderr(StringIO()):
            admin.merge_operators(self.root, ['another'])
        self.assertFalse((self.root / leftovers[0]).exists())

    def test_a_platform_without_non_blocking_flock_takes_the_lock_and_cleans_up(self):
        # The fallback for an fcntl with no LOCK_NB: one blocking flock, then the same work.
        import types
        from unittest.mock import Mock
        fake = types.SimpleNamespace(flock=Mock(), LOCK_EX=2)
        (self.root / '.deployment.private.json.abcd1234').write_text('{}', encoding='utf-8')
        with patch.dict(sys.modules, {'fcntl': fake}):
            result, err = self.flip('on')
        self.assertTrue(result['checkpoint_provenance_writes'])
        self.assertEqual(fake.flock.call_count, 1)
        self.assertEqual(fake.flock.call_args[0][1], 2)
        self.assertFalse((self.root / '.deployment.private.json.abcd1234').exists())
        self.assertIn('Removed 1 temporary copy', err)

    def test_without_fcntl_nothing_is_removed(self):
        # No lock to exclude another writer, so a copy may be one mid-write: kept.
        (self.root / '.deployment.private.json.abcd1234').write_text('{}', encoding='utf-8')
        with patch.dict(sys.modules, {'fcntl': None}):
            result, _ = self.flip('on')
        self.assertTrue(result['checkpoint_provenance_writes'])
        self.assertTrue((self.root / '.deployment.private.json.abcd1234').exists())

    def test_an_unlocked_older_kit_writer_loses_its_copy_and_fails_with_the_file_kept(self):
        # kittrial-5bb.144 item 4: a writer from a kit before kittrial-5bb.136 takes no lock.
        # Paused between its copy and its rename, its copy is removed by a newer writer's
        # cleanup; its rename then fails (FileNotFoundError, rc 1), the file stays valid and
        # the newer change is kept. The older change is lost, loudly.
        older = subprocess.Popen([sys.executable, '-c', textwrap.dedent('''
            import json, os, sys, time
            sys.path.insert(0, sys.argv[2])
            import admin
            from pathlib import Path
            root = Path(sys.argv[1])
            real = os.replace
            def paused(source, target):
                (root / 'older.ready').touch()
                deadline = time.monotonic() + 30
                while not (root / 'newer.done').exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                return real(source, target)
            admin.os.replace = paused
            cfg = json.loads((root / 'deployment.private.json').read_text(encoding='utf-8'))
            cfg['operators'] = cfg['operators'] + ['older-op']          # no lock, as before .136
            admin.atomic_private_write(root / 'deployment.private.json', json.dumps(cfg))
        '''), str(self.root), REPO], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(older.kill)
        for _ in range(3000):
            if (self.root / 'older.ready').exists() or older.poll() is not None:
                break
            time.sleep(0.01)
        self.assertTrue((self.root / 'older.ready').exists(), older.stderr.read() if older.poll() is not None else '')
        result, err = self.flip('on')                                    # the newer, locked writer
        self.assertIn('Removed 1 temporary copy', err)
        (self.root / 'newer.done').touch()
        _, older_err = older.communicate(timeout=60)
        self.assertEqual(older.returncode, 1)
        self.assertIn('FileNotFoundError', older_err)
        cfg = json.loads(self.marker.read_text(encoding='utf-8'))        # valid, newer change kept
        self.assertTrue(cfg['checkpoint_provenance_writes'])
        self.assertNotIn('older-op', cfg['operators'])
        self.assertEqual(cfg['password'], 'test-only')

    def test_the_temporary_copy_is_created_0600(self):
        modes = []
        real_fsync = os.fsync

        def fsync(fd):                       # the copy as it is written, before any chmod
            modes.append(os.fstat(fd).st_mode & 0o777)
            return real_fsync(fd)

        previous = os.umask(0)                # the mode must not depend on the umask
        try:
            with patch.object(admin.os, 'fsync', side_effect=fsync):
                admin.atomic_private_write(self.marker, '{"password": "test-only"}')
        finally:
            os.umask(previous)
        self.assertEqual(modes[0], 0o600)
        self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual([name for name in self.names() if name.startswith('.deployment')], [])


if __name__ == '__main__':
    unittest.main()
