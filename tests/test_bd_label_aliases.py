"""Endpoint-level reserved-label guard against bd's hidden label aliases.

kittrial-pth.26 rev3 review item hidden-label-alias: bd 1.2.2 accepts
`create --label X` as an UNDOCUMENTED alias of `--labels` (it is hidden from
`bd create --help`), so a guard table built from --help let a plain create mint
`requirement,requirement:accepted`, `brd-section` and `request:<64hex>`.

These tests drive endpoint.execute against a REAL pinned bd, not a stub, so the
flag surface is the binary's own. A real bd is used when one is available:
set `ORCHESTRA_BD_BIN` to the bd binary (a sibling `dolt` is copied too), or
have `bd` on PATH. Without one the class skips and the stub-backed
endpoint/reserved_comments tests still cover the same spellings. The suite is
POSIX-only because endpoint.py imports fcntl.

The tracker is made in SERVER mode (kittrial-5bb.166). A bare `bd init` is
embedded mode, which the static bd does not have, so these three tests skipped
whenever `ORCHESTRA_BD_BIN` was the static bd. The fixture now starts a scratch
Dolt SQL server from the `dolt` binary beside bd and initializes the tracker
against it, in the mode the kit itself uses; with no `dolt` beside bd the class
skips and says so. The server runs with HOME, DOLT_ROOT_PATH and XDG_CONFIG_HOME
inside the fixture, so its writes stay there and never touch the runner's own
`~/.dolt`, and the class stops it on SIGTERM/SIGINT as well as at the end
(kittrial-5bb.166 item 2).
"""
import csv
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import admin
    import endpoint
except ImportError:  # pragma: no cover - endpoint needs fcntl (POSIX)
    admin = endpoint = None


def _bd_binary():
    configured = os.environ.get('ORCHESTRA_BD_BIN')
    if configured and Path(configured).is_file():
        return Path(configured)
    found = shutil.which('bd')
    return Path(found) if found else None


BD = _bd_binary()

RESERVED_VALUES = ('requirement', 'requirement:draft', 'requirement:accepted',
                   'brd-section', 'request:' + 'a' * 64,
                   'request-content:' + 'b' * 64)


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class RealBdLabelAliasTests(unittest.TestCase):
    """Every accepted create/update label spelling, driven through endpoint."""

    #: The scratch deployment password, matching the one the fixture puts on the Dolt root.
    PASSWORD = 'x'
    READY_TRIES, READY_PAUSE = 120, 0.5

    @classmethod
    def _free_port(cls):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            return probe.getsockname()[1]

    @classmethod
    def _stop_server(cls):
        server = getattr(cls, 'server', None)
        if server is None:
            return
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=15)

    @classmethod
    def _signalled(cls, signum, frame):
        """A test process stopped with SIGTERM must not leave the scratch server or its folder.

        kittrial-5bb.166 item 2: the fixture's Dolt server is a child of the test process, so
        stopping the runner used to leave both the server and its ``bd-alias-*`` folder behind.
        ``tearDownClass`` cannot run on a signal, so the handler does its work and re-raises.
        """
        cls._stop_server()
        cls._tmp.cleanup()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    @classmethod
    def _watch_signals(cls):
        """Stop the server on SIGTERM/SIGINT as well as at the end of the class.

        Only the main thread may set a handler; a run that is not in it simply keeps the
        normal cleanup path.
        """
        cls._signals = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                cls._signals[signum] = signal.signal(signum, cls._signalled)
            except ValueError:
                pass

    @classmethod
    def _await_server(cls):
        """Wait until the scratch Dolt server answers; connect with the fresh root's empty password."""
        for _ in range(cls.READY_TRIES):
            try:
                admin.sql(cls.root, 'SELECT 1;', password='')
                return
            except (subprocess.CalledProcessError, OSError):
                time.sleep(cls.READY_PAUSE)
        raise unittest.SkipTest('the scratch Dolt server did not become ready')

    @classmethod
    def _set_root_password(cls):
        """Give the scratch server the same root password `admin.environment` will send."""
        users = admin.sql(cls.root, 'SELECT User,Host FROM mysql.user;', password='')
        changes = ["ALTER USER '%s'@'%s' IDENTIFIED BY '%s';"
                   % (row['User'].replace("'", "''"), row['Host'].replace("'", "''"), cls.PASSWORD)
                   for row in csv.DictReader(io.StringIO(users)) if row.get('User') == 'root']
        if not changes:
            raise unittest.SkipTest('the scratch Dolt server has no root account')
        admin.sql(cls.root, '\n'.join(changes), password='')

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='bd-alias-')
        cls._watch_signals()
        cls.root = Path(cls._tmp.name)
        (cls.root / 'bin').mkdir()
        for name in ('home', 'config', 'dolt-home', 'data'):
            (cls.root / name).mkdir()
        cls.bd_path = cls.root / 'bin' / 'bd'
        shutil.copy(str(BD), str(cls.bd_path))
        sibling = BD.with_name('dolt')
        if not sibling.is_file():
            cls._tmp.cleanup()
            raise unittest.SkipTest('no dolt binary beside bd: a server-mode tracker needs one')
        shutil.copy(str(sibling), str(cls.root / 'bin' / 'dolt'))
        cls.port = cls._free_port()
        (cls.root / 'deployment.private.json').write_text(
            json.dumps({'password': cls.PASSWORD, 'unit': 'none', 'port': str(cls.port)}), encoding='utf-8')
        cls.project = cls.root / 'projects' / 'pp'
        cls.project.mkdir(parents=True)
        # A HOME, DOLT_ROOT_PATH and XDG_CONFIG_HOME inside the fixture: the scratch server keeps
        # its global config and event data under cls.root and never writes the runner's real
        # ~/.dolt (kittrial-5bb.166 item 2). The kit's client credential variables are
        # deliberately NOT passed: `dolt sql-server` reads DOLT_CLI_PASSWORD at startup and
        # refuses to run with "a password is provided, a user must also be provided".
        server_env = dict(os.environ, HOME=str(cls.root / 'home'),
                          DOLT_ROOT_PATH=str(cls.root / 'dolt-home'),
                          XDG_CONFIG_HOME=str(cls.root / 'config'))
        cls.server = subprocess.Popen(
            [str(cls.root / 'bin' / 'dolt'), 'sql-server', '-H', '127.0.0.1', '-P', str(cls.port),
             '--data-dir', str(cls.root / 'data')],
            cwd=str(cls.root), env=server_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            cls._await_server()
            cls._set_root_password()
            init = subprocess.run(
                [str(cls.bd_path), 'init', '--server', '--external', '--server-host', '127.0.0.1',
                 '--server-port', str(cls.port), '--server-user', 'root', '--prefix', 'pp',
                 '--database', 'pp', '--skip-agents', '--skip-hooks', '--non-interactive'],
                cwd=str(cls.project), env=admin.environment(cls.root), capture_output=True, text=True)
            if init.returncode:
                raise unittest.SkipTest('bd server-mode init failed: %s'
                                        % (init.stderr or init.stdout).strip()[:200])
        except BaseException:
            cls._stop_server()
            cls._tmp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        for signum, previous in getattr(cls, '_signals', {}).items():
            signal.signal(signum, previous)
        cls._signals = {}
        cls._stop_server()
        cls._tmp.cleanup()

    def bd(self, *args, actor='op'):
        return subprocess.run(
            [str(self.bd_path), '--directory', str(self.project), '--sandbox', '--actor', actor, *args],
            env=admin.environment(self.root), capture_output=True, text=True)

    def export_rows(self):
        result = self.bd('export', '--all')
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()
                if line.strip()]

    def reserved_labels(self):
        found = []
        for row in self.export_rows():
            for label in row.get('labels') or []:
                if label in ('requirement', 'brd-section') \
                        or label.startswith(('requirement:', 'request:',
                                             'request-content:')):
                    found.append((row['id'], label))
        return found

    def raw(self, args, actor='mallory'):
        return endpoint.execute(self.root, {'project': 'pp', 'actor': actor,
                                            'action': 'bd', 'args': args})

    CREATE_SPELLINGS = (['--label'], ['--labels'], ['-l'])

    def test_create_label_spellings_refuse_every_reserved_value(self):
        for flag in self.CREATE_SPELLINGS:
            for value in RESERVED_VALUES:
                args = ['create', 'mint %s %s' % (flag[0], value[:8]), flag[0],
                        value, '--json']
                with self.subTest(args=str(args)):
                    with self.assertRaisesRegex(
                            ValueError, 'Reserved coordination/requirement labels'):
                        self.raw(args)
        for flag in ('--label=', '--labels=', '-l='):
            value = 'requirement,requirement:accepted'
            args = ['create', 'mint eq %s' % flag[:6], flag + value, '--json']
            with self.subTest(args=str(args)):
                with self.assertRaisesRegex(
                        ValueError, 'Reserved coordination/requirement labels'):
                    self.raw(args)
        self.assertEqual(self.reserved_labels(), [])

    def test_update_label_spellings_refuse_every_reserved_value(self):
        target = json.loads(self.bd('create', 'victim', '--json').stdout)['id']
        for flag in ('--add-label', '--set-labels', '--remove-label'):
            for value in RESERVED_VALUES:
                args = ['update', target, flag, value, '--json']
                with self.subTest(args=str(args)):
                    with self.assertRaisesRegex(
                            ValueError, 'Reserved coordination/requirement labels'):
                        self.raw(args)
        self.assertEqual(self.reserved_labels(), [])

    def test_ordinary_labels_still_reach_real_bd(self):
        created = json.loads(self.bd('create', 'ordinary alias',
                                     '--label', 'frontend,bug', '--json').stdout)['id']
        row = next(r for r in self.export_rows() if r['id'] == created)
        self.assertEqual(sorted(row['labels']), ['bug', 'frontend'])
        short = json.loads(self.bd('create', 'ordinary short', '-l', 'docs',
                                   '--json').stdout)['id']
        row = next(r for r in self.export_rows() if r['id'] == short)
        self.assertEqual(sorted(row['labels']), ['docs'])
        result = endpoint.execute(self.root, {
            'project': 'pp', 'actor': 'mallory', 'action': 'bd',
            'args': ['update', created, '--add-label', 'reviewed', '--json']})
        self.assertEqual(result['returncode'], 0, result['stderr'])
        row = next(r for r in self.export_rows() if r['id'] == created)
        self.assertIn('reviewed', row['labels'])
        self.assertEqual(self.reserved_labels(), [])


if __name__ == '__main__':
    unittest.main()
