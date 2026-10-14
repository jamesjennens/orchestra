"""Reads hold the locks SHARED; writes keep them exclusive (kittrial-5bb.281).

The measured defect: concurrent web requests were largely serialized because every read
took the project's ``.coordination.lock`` with ``flock(LOCK_EX)`` and every guarded read
also took the installation-wide authority lock. These tests pin the two seams: the
``held_coordination_lock`` helper's flag, the ``bd`` read path choosing it from
``is_mutating_invocation``, and ``run_guarded`` not taking the authority lock for a
request that carries no descriptor and no operation identity.
"""
import contextlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
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

    def locked_flags(self, args):
        completed = subprocess.CompletedProcess(['bd'], 0, '[]', '')
        with mock.patch.object(subprocess, 'run', return_value=completed), \
                mock.patch.object(endpoint.fcntl, 'flock') as flock:
            endpoint.execute(self.root, {'project': 'alpha', 'actor': 'reader',
                                         'action': 'bd', 'args': args})
        return [call.args[1] for call in flock.call_args_list]

    def test_a_bd_read_takes_the_shared_lock(self):
        # The web tasks page and every list reader go through exactly this invocation
        # (http_service EndpointBackend.read_tasks: `bd list --all --limit 0 --json`).
        self.assertEqual(self.locked_flags(['list', '--all', '--limit', '0', '--json']),
                         [endpoint.fcntl.LOCK_SH])

    def test_a_bd_write_keeps_the_exclusive_lock(self):
        self.assertEqual(self.locked_flags(['create', 'a task']),
                         [endpoint.fcntl.LOCK_EX])


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

    def test_a_reserved_operation_identity_takes_the_authority_lock(self):
        self.assertEqual(self.locks_taken({'actor': 'writer', 'operation_id': 'op-00000001'}), [self.config.lock])

    def test_an_identity_check_takes_the_authority_lock(self):
        self.assertEqual(
            self.locks_taken({'actor': 'writer', 'args': []}, identity_check=lambda: None),
            [self.config.lock])


if __name__ == '__main__':
    unittest.main()
