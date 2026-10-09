"""A full acceptance through the coordinator route on a real bd (review of 958e883, item 2).

The rev-1 tests built the request by hand in a shape the shipped client never sends, and none
of them used a real bd, which is how ``capability-retire`` through ``capability-apply`` and the
attachment shape both passed 18 green tests. This drives the real client's wire (one
``@attachment:N`` token) into ``endpoint.execute`` as the legitimate coordinator on a scratch
server-mode bd, accepts a reference entry as a direct accepted revision 1, and reads it back.

A real bd is used when one is available: set ``ORCHESTRA_BD_BIN`` to the bd binary (a sibling
``dolt`` is copied too), or have ``bd`` on PATH. Without one the class skips, like
``tests/test_bd_label_aliases.py``. POSIX-only because endpoint.py imports fcntl.
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

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
try:
    import admin
    import endpoint
except ImportError:                                   # fcntl: the endpoint is POSIX only
    admin = endpoint = None


def _bd_binary():
    configured = os.environ.get('ORCHESTRA_BD_BIN')
    if configured and Path(configured).is_file():
        return Path(configured)
    found = shutil.which('bd')
    return Path(found) if found else None


BD = _bd_binary()
PRINCIPAL = 'lane:lane-one'
ACTOR = 'coordinator'
PROJECT = 'pp'
KEY = 'calendar.trading'


def wire(project, actor, args, path):
    """The request the shipped client sends: ``--file FILE`` becomes one @attachment token."""
    import client
    return client._wire(project, actor, args, 'coordinator', path)


def payload(operation_id='coord-direct-1', operation='draft'):
    """A direct accepted revision 1: the form the host command writes with ``operation: draft``."""
    return {'schema_version': 1, 'operation_id': operation_id, 'operation': operation, 'key': KEY,
            'title': 'Trading calendar authority',
            'statement': 'Charts derive holidays from the pinned calendar module.',
            'authority': {'type': 'repo-path', 'path': 'src/example/calendar.py',
                          'commit': '0' * 40, 'anchor': 'HOLIDAYS'},
            'owner': 'account:u-0001', 'review_by': '2027-01-15', 'tags': ['calendar', 'data'],
            'decisions': [], 'revision': 1, 'expected_sha256': None,
            'acceptance_state': 'accepted',
            'acceptance': {'decision_id': 'decision-42', 'owners': ['account:u-0001'],
                           'approvers': ['account:u-0001'], 'policy': 'any-owner',
                           'evidence': 'decision decision-42'}}


@unittest.skipIf(endpoint is None or os.name != 'posix', 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class RealBdCoordinatorAcceptanceTests(unittest.TestCase):
    """One full acceptance through the route, with a real bd."""

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
        cls._stop_server()
        cls._tmp.cleanup()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    @classmethod
    def _watch_signals(cls):
        cls._signals = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                cls._signals[signum] = signal.signal(signum, cls._signalled)
            except ValueError:
                pass

    @classmethod
    def _await_server(cls):
        for _ in range(cls.READY_TRIES):
            try:
                admin.sql(cls.root, 'SELECT 1;', password='')
                return
            except (subprocess.CalledProcessError, OSError):
                time.sleep(cls.READY_PAUSE)
        raise unittest.SkipTest('the scratch Dolt server did not become ready')

    @classmethod
    def _set_root_password(cls):
        users = admin.sql(cls.root, 'SELECT User,Host FROM mysql.user;', password='')
        changes = ["ALTER USER '%s'@'%s' IDENTIFIED BY '%s';"
                   % (row['User'].replace("'", "''"), row['Host'].replace("'", "''"), cls.PASSWORD)
                   for row in csv.DictReader(io.StringIO(users)) if row.get('User') == 'root']
        if not changes:
            raise unittest.SkipTest('the scratch Dolt server has no root account')
        admin.sql(cls.root, '\n'.join(changes), password='')

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='coord-realbd-')
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
            json.dumps({'password': cls.PASSWORD, 'unit': 'none', 'port': str(cls.port),
                        'operators': [ACTOR]}), encoding='utf-8')
        cls.project = cls.root / 'projects' / PROJECT
        cls.project.mkdir(parents=True)
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
                 '--server-port', str(cls.port), '--server-user', 'root', '--prefix', PROJECT,
                 '--database', PROJECT, '--skip-agents', '--skip-hooks', '--non-interactive'],
                cwd=str(cls.project), env=admin.environment(cls.root), capture_output=True, text=True)
            if init.returncode:
                raise unittest.SkipTest('bd server-mode init failed: %s'
                                        % (init.stderr or init.stdout).strip()[:200])
        except BaseException:
            cls._stop_server()
            cls._tmp.cleanup()
            raise
        # The project's session registry: the actor belongs to the key's principal (rule 2).
        (cls.project / '.sessions.json').write_text(
            json.dumps({'schema_version': 1, 'records': {}, 'owners': {ACTOR: PRINCIPAL}}),
            encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        for signum, previous in getattr(cls, '_signals', {}).items():
            signal.signal(signum, previous)
        cls._signals = {}
        cls._stop_server()
        cls._tmp.cleanup()

    def ask(self, subcommand, body, actor=ACTOR):
        attachment = self.root / ('%s.json' % subcommand)
        attachment.write_text(body, encoding='utf-8')
        return endpoint.execute(
            self.root, json.loads(wire(PROJECT, actor, [subcommand, '--file', str(attachment)], None)),
            key_principal=PRINCIPAL, key_projects=[PROJECT])

    def read(self, subcommand, *args):
        return endpoint.execute(self.root, {'project': PROJECT, 'actor': ACTOR,
                                            'action': 'ref', 'args': [subcommand, *args]})

    def propose(self, key):
        """A contributor's draft, written through the endpoint as a lane would."""
        document = {'schema_version': 1, 'operation_id': 'prop-' + key,
                    'key': key, 'title': 'Holiday calendar authority',
                    'statement': 'Charts derive holidays from the pinned calendar module.',
                    'authority': {'type': 'repo-path', 'path': 'src/example/calendar.py',
                                  'commit': '0' * 40, 'anchor': 'HOLIDAYS'},
                    'owner': 'account:u-0001', 'review_by': '2027-01-15', 'tags': ['calendar'],
                    'decisions': [], 'revision': 1, 'expected_sha256': None}
        return endpoint.execute(self.root, {
            'project': PROJECT, 'actor': 'alice', 'action': 'ref',
            'args': ['propose', '@attachment:0'],
            'attachments': {'0': {'flag': '--file', 'text': json.dumps(document)}}})

    def test_a_direct_accepted_revision_is_accepted_through_the_route_on_a_real_bd(self):
        answer = self.ask('reference-apply', json.dumps(payload()))
        self.assertEqual(answer['returncode'], 0, answer['stderr'])
        result = json.loads(answer['stdout'])
        self.assertEqual((result['revision'], result['state']), (1, 'accepted'))
        # The entry reads accepted back, from the real tracker.
        got = self.read('get', KEY)
        self.assertEqual(got['returncode'], 0, got['stderr'])
        view = json.loads(got['stdout'])
        self.assertEqual(view['state'], 'accepted')
        self.assertEqual(view['record']['title']['text'], 'Trading calendar authority')

    def test_a_reviewed_draft_is_accepted_through_the_route_on_a_real_bd(self):
        # The ordinary acceptance: a contributor proposes, the coordinator accepts the
        # reviewed revision by its content hash.
        made = self.propose('calendar.holidays')
        self.assertEqual(made['returncode'], 0, made['stderr'])
        draft = json.loads(self.read('get', 'calendar.holidays')['stdout'])
        self.assertEqual(draft['state'], 'draft-only')
        digest = draft['proposed']['sha256']
        body = {'schema_version': 1, 'operation_id': 'coord-accept-1', 'operation': 'accept',
                'key': 'calendar.holidays', 'revision': 1, 'record_sha256': digest,
                'acceptance_state': 'accepted', 'acceptance': payload()['acceptance']}
        answer = self.ask('reference-apply', json.dumps(body))
        self.assertEqual(answer['returncode'], 0, answer['stderr'])
        self.assertEqual(json.loads(answer['stdout'])['state'], 'accepted')
        self.assertEqual(json.loads(self.read('get', 'calendar.holidays')['stdout'])['state'], 'accepted')

    def test_the_route_refuses_retire_on_a_real_bd(self):
        # The reproduction of finding 1 (out/smuggle-FG-s1.log): `operation: retire` must be
        # refused before the library, with nothing written, on a real bd too.
        with self.assertRaises(ValueError) as refusal:
            self.ask('reference-apply', json.dumps(
                payload(operation_id='coord-direct-retire', operation='retire')))
        self.assertIn('accept, draft', str(refusal.exception))


if __name__ == '__main__':
    unittest.main()
