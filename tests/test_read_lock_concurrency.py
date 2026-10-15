"""Reads hold the locks SHARED; writes keep them exclusive (kittrial-5bb.281).

The measured defect: concurrent web requests were largely serialized because every read
took the project's ``.coordination.lock`` with ``flock(LOCK_EX)`` and every guarded read
also took the installation-wide authority lock. These tests pin the seams the review asked
for: the ONE read-only-action table that decides the project lock's shared/exclusive flag,
the ``bd`` read path choosing it from ``is_mutating_invocation``, an operation identity
staying exclusive (as on main), ``run_guarded`` not taking the authority lock for a request
that carries no descriptor and no operation identity but still taking it for one that does,
and the concurrency of the first history-snapshot write.
"""
import contextlib
import inspect
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import coordination
import http_authority

try:
    import endpoint
except ImportError:                                   # fcntl: the endpoint is POSIX only
    endpoint = None

POSIX = unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')


def envelope(stdout='[]'):
    return {'returncode': 0, 'stdout': stdout, 'stderr': ''}


@POSIX
class CoordinationLockTests(unittest.TestCase):
    """The one helper every endpoint action now takes its project lock through."""

    def test_a_read_takes_the_lock_shared(self):
        with tempfile.TemporaryDirectory() as base:
            with mock.patch.object(endpoint.fcntl, 'flock') as flock:
                with endpoint.held_coordination_lock(Path(base), shared=True):
                    pass
            self.assertEqual(flock.call_count, 1)
            self.assertEqual(flock.call_args.args[1], endpoint.fcntl.LOCK_SH)

    def test_a_write_takes_the_lock_exclusive(self):
        with tempfile.TemporaryDirectory() as base:
            with mock.patch.object(endpoint.fcntl, 'flock') as flock:
                with endpoint.held_coordination_lock(Path(base)):
                    pass
            self.assertEqual(flock.call_count, 1)
            self.assertEqual(flock.call_args.args[1], endpoint.fcntl.LOCK_EX)


@POSIX
class EndpointReadLockTests(unittest.TestCase):
    """A client/web read reaches the lock as a shared hold, a write as an exclusive one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'rt'
        project = self.root / 'projects' / 'alpha'
        (project / '.beads').mkdir(parents=True)
        (project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'operators': [], 'verifiers': [], 'password': 'x'}), encoding='utf-8')

    def locked_flags(self, args, **extra):
        request = {'project': 'alpha', 'actor': 'reader', 'action': 'bd', 'args': args}
        request.update(extra)
        completed = subprocess.CompletedProcess(['bd'], 0, '[]', '')
        with mock.patch.object(subprocess, 'run', return_value=completed), \
                mock.patch.object(endpoint.fcntl, 'flock') as flock:
            endpoint.execute(self.root, request)
        return [call.args[1] for call in flock.call_args_list]

    def test_a_bd_read_takes_the_shared_lock(self):
        # The web tasks page and every list reader go through exactly this invocation
        # (http_service EndpointBackend.read_tasks: `bd list --all --limit 0 --json`).
        self.assertEqual(self.locked_flags(['list', '--all', '--limit', '0', '--json']),
                         [endpoint.fcntl.LOCK_SH])

    def test_a_bd_write_keeps_the_exclusive_lock(self):
        self.assertEqual(self.locked_flags(['create', 'a task']),
                         [endpoint.fcntl.LOCK_EX])

    def test_a_bd_read_with_an_operation_identity_keeps_the_exclusive_lock(self):
        # Review item 2(2): on main the exclusive project lock serialized one
        # operation_id, so concurrent reads sharing it replayed or were refused, never
        # answered "Operation identity reserved; outcome unknown". A read that carries
        # an identity therefore stays exclusive too.
        self.assertEqual(
            self.locked_flags(['list', '--all', '--limit', '0', '--json'],
                              operation_id='op-00000001'),
            [endpoint.fcntl.LOCK_EX])


class SharedFileLockBoundTests(unittest.TestCase):
    """kittrial-5bb.291 item 5: the shared wait has the same bound as the exclusive one.

    The roster read takes the authority lock SHARED (two reads at once, a writer
    still excluded) but used to wait forever. file_lock(shared=True) now uses the
    same deadline as the exclusive lock.
    """

    def test_a_shared_hold_allows_a_second_shared_hold(self):
        import threading
        from http_authority import file_lock
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'authority.lock'
            done = threading.Event(); error = []
            def second():
                try:
                    with file_lock(lock, timeout=5, shared=True):
                        done.set()
                except BaseException as caught:
                    error.append(caught); done.set()
            with file_lock(lock, timeout=5, shared=True):
                thread = threading.Thread(target=second, daemon=True)
                thread.start()
                self.assertTrue(done.wait(5), 'a second shared hold queued behind a shared hold')
            thread.join(5)
            self.assertFalse(error, error)

    def test_a_shared_wait_behind_an_exclusive_holder_refuses_after_the_bound(self):
        import threading
        from http_authority import file_lock
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'authority.lock'
            ready = threading.Event(); outcome = []
            def shared_wait():
                ready.set()
                try:
                    with file_lock(lock, timeout=0.2, poll=0.01, shared=True):
                        outcome.append('acquired')
                except TimeoutError as waited:
                    outcome.append('timeout')
                except BaseException as caught:
                    outcome.append(type(caught).__name__)
            holder = threading.Thread(target=lambda: (
                ready.set(),
                __import__('time').sleep(1.0)), daemon=True)
            with file_lock(lock, timeout=5):
                thread = threading.Thread(target=shared_wait, daemon=True)
                thread.start()
                ready.wait(5)
                thread.join(3)
            self.assertEqual(outcome, ['timeout'],
                             'a shared wait behind an exclusive holder must refuse after the bound')


class GuardedReadLockTests(unittest.TestCase):
    """run_guarded takes the installation-wide authority lock only when it is needed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = http_authority.AuthorityConfig(str(self.root / 'authority.json'))

    def locks_taken(self, request, **options):
        seen = []

        def fake_lock(path, *args, **kwargs):
            seen.append(path)
            return contextlib.nullcontext()

        with mock.patch.object(http_authority, 'file_lock', side_effect=fake_lock):
            http_authority.run_guarded(request, str(self.root / 'journal.sqlite3'),
                                       lambda: envelope(), authority_config=self.config, **options)
        return seen

    def test_a_pure_read_does_not_take_the_authority_lock(self):
        # No descriptor and no operation identity: the web service's shape for every read.
        self.assertEqual(self.locks_taken({'actor': 'reader', 'args': []}), [])

    def test_a_descriptor_carrying_request_takes_the_authority_lock(self):
        # Review item 2(1)/mutant M4: a request WITH a descriptor must still take the
        # lock, so an HTTP-side revocation that committed first is visible before the
        # effect. Only the no-descriptor/no-identity shape may skip it.
        self.assertEqual(
            self.locks_taken({'actor': 'writer', 'authority': {'project': 'alpha',
                                                               'capability': 'read'}}),
            [self.config.lock])

    def test_a_reserved_operation_identity_takes_the_authority_lock(self):
        self.assertEqual(self.locks_taken({'actor': 'writer', 'operation_id': 'op-00000001'}), [self.config.lock])

    def test_an_identity_check_takes_the_authority_lock(self):
        self.assertEqual(
            self.locks_taken({'actor': 'writer', 'args': []}, identity_check=lambda: None),
            [self.config.lock])


#: Collects every action name ``endpoint.execute`` dispatches on, from its source, so a
#: NEW action cannot escape the classification test without failing it.
_DISPATCH = re.compile(r"(?<![\w.'\"])action\s*(?:==|!=|in)\s*(\([^)]*\)|'[^']*')")


def dispatched_actions():
    source = inspect.getsource(endpoint.execute).replace("request.get('action')", 'action')
    names = set()
    for match in _DISPATCH.finditer(source):
        names.update(re.findall(r"'([^']+)'", match.group(1)))
    return names


#: The actions this test knows can write. Each must stay OUT of the read-only table and
#: take the exclusive lock (kills the review's M1: checkpoint shared; M3: review/handoff
#: shared with work), whatever the table's predicates say.
WRITE_ACTIONS = ('checkpoint', 'review', 'handoff', 'coordinate', 'lifecycle', 'requirement',
                 'owner-requirements', 'feedback', 'set-onboarding', 'session', 'refresh',
                 'create-project', 'project-creations', 'creation-standing')
#: The actions that are read-only always.
READ_ACTIONS = ('brief', 'history', 'requirements', 'work')
#: The actions whose shared/exclusive choice is the payload operation.
READ_OPERATIONS = (('web-lifecycle', 'read'), ('web-merge-slot', 'check'))


@POSIX
class ReadOnlyActionTableTests(unittest.TestCase):
    """ONE table decides the project lock, and nothing outside it may be shared."""

    def test_the_table_names_no_write_action(self):
        for action in WRITE_ACTIONS:
            with self.subTest(action=action):
                self.assertNotIn(action, endpoint.READ_ONLY_ACTIONS)
                self.assertFalse(endpoint.coordination_lock_shared(
                    {'actor': 'a'}, action, args=['create', 'x'], operation='write'))

    def test_the_read_only_actions_are_shared(self):
        for action in READ_ACTIONS:
            with self.subTest(action=action):
                self.assertIn(action, endpoint.READ_ONLY_ACTIONS)
                self.assertTrue(endpoint.coordination_lock_shared({'actor': 'a'}, action, args=[]))

    def test_the_dynamic_actions_classify_from_their_arguments_or_operation(self):
        shared = endpoint.coordination_lock_shared
        self.assertTrue(shared({'actor': 'a'}, 'bd', args=['list', '--all', '--json']))
        self.assertFalse(shared({'actor': 'a'}, 'bd', args=['create', 'a task']))
        for action, operation in READ_OPERATIONS:
            with self.subTest(action=action):
                self.assertTrue(shared({'actor': 'a'}, action, args=[], operation=operation))
                self.assertFalse(shared({'actor': 'a'}, action, args=[], operation='apply'))

    def test_an_operation_identity_is_exclusive(self):
        # Review item 2(2): the identity reservation must be serialized as on main.
        self.assertFalse(endpoint.coordination_lock_shared(
            {'actor': 'a', 'operation_id': 'op-00000001'}, 'brief', args=['t', '--json']))
        self.assertFalse(endpoint.coordination_lock_shared(
            {'actor': 'a', 'operation_id': 'op-00000001'}, 'bd', args=['list', '--json']))

    def test_every_dispatched_action_outside_the_table_is_exclusive(self):
        actions = dispatched_actions()
        # The scan really reads the dispatcher; a miss here would make the walk vacuous.
        for expected in ('bd', 'brief', 'history', 'checkpoint', 'work', 'handoff', 'review',
                         'web-lifecycle', 'web-merge-slot'):
            self.assertIn(expected, actions)
        for action in sorted(actions):
            if action in endpoint.READ_ONLY_ACTIONS:
                continue
            with self.subTest(action=action):
                self.assertFalse(endpoint.coordination_lock_shared(
                    {'actor': 'a'}, action, args=['create', 'x'], operation='read'))


class AtomicWriteTests(unittest.TestCase):
    """The shared temporary is unique per writer (review item 1, coordinator half)."""

    def test_temporary_names_are_unique_and_recognized(self):
        first = coordination.temporary_name('abc.json')
        second = coordination.temporary_name('abc.json')
        self.assertNotEqual(first, second)
        self.assertTrue(coordination.is_temporary(first))
        self.assertTrue(coordination.is_temporary('abc.json.tmp-1-0123456789abcdef'))
        self.assertFalse(coordination.is_temporary('abc.json'))
        self.assertFalse(coordination.is_temporary('abc.json.tmp'))

    def test_atomic_writes_the_target_and_leaves_no_temporary(self):
        with tempfile.TemporaryDirectory() as base:
            target = Path(base) / 'snap.json'
            coordination.atomic(target, {'b': 1, 'a': [2, 3]})
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')),
                             {'b': 1, 'a': [2, 3]})
            self.assertEqual(list(Path(base).glob('*.tmp-*')), [])


#: One racer of the snapshot write. It waits for every sibling to be alive, then writes the
#: same content-addressed bytes to the same target through ``coordination.atomic``.
RACER = r'''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import coordination
target = Path(sys.argv[2]); ready = Path(sys.argv[3]); barrier = Path(sys.argv[4])
size = int(sys.argv[5])
ready.write_text('ready')
while not barrier.exists():
    time.sleep(0.001)
coordination.atomic(target, {'token': 'identical', 'payload': 'x' * size})
'''


@POSIX
class HistorySnapshotConcurrencyTests(unittest.TestCase):
    """The first history read of one task creates ``.history-snapshots/<digest>.json``.

    ``briefing.py`` writes it through ``coordination.atomic`` while the endpoint holds the
    project lock SHARED, so several first readers of one task write one target at once. The
    reviewed tip used one temporary name (``<digest>.tmp``) for all of them: they truncated
    each other's file and one rename failed with FileNotFoundError (review
    ev/RC2-atomic-race.txt, 7 of 8 racers). Every racer here must succeed.
    """

    def race(self, racers=8, rounds=4, size=64 * 1024):
        base = Path(tempfile.mkdtemp(prefix='history-snapshot-race-'))
        self.addCleanup(shutil.rmtree, str(base), True)
        failures = []
        for round_number in range(rounds):
            target = base / ('%d.json' % round_number)
            barrier = base / ('go-%d' % round_number)
            processes = []
            for racer in range(racers):
                ready = base / ('ready-%d-%d' % (round_number, racer))
                processes.append(subprocess.Popen(
                    [sys.executable, '-c', RACER, str(KIT), str(target), str(ready),
                     str(barrier), str(size)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            deadline = time.monotonic() + 60
            while (len(list(base.glob('ready-%d-*' % round_number))) < racers
                   and time.monotonic() < deadline):
                time.sleep(0.01)
            barrier.write_text('go', encoding='utf-8')
            for racer, process in enumerate(processes):
                out, err = process.communicate(timeout=120)
                if process.returncode != 0:
                    failures.append('round %d racer %d rc %d: %s'
                                    % (round_number, racer, process.returncode,
                                       err.decode('utf-8', 'replace').strip()[-300:]))
            if not failures:
                saved = json.loads(target.read_text(encoding='utf-8'))
                if saved.get('token') != 'identical':
                    failures.append('round %d wrote a damaged target' % round_number)
        return failures

    def test_concurrent_first_time_snapshot_writers_all_succeed(self):
        self.assertEqual(self.race(), [])


if __name__ == '__main__':
    unittest.main()
