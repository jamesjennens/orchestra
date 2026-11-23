"""Stopped-office restore-new tests and named refusal mutants (kittrial-5bb.520).

Verifies that on a stopped foreground-office runtime (unit == 'office-foreground'),
`admin.py restore-new` starts a temporary Dolt using the same shared rules as
`office_service.py` (via `office_dolt.py`), stops it before restoring HTTP state and on
every exit/failure/signal, never bootstraps credentials, refuses non-Dolt port holders,
honors configurable `dolt_startup_seconds` with bounded timeout diagnostics, cleans up
on `SIGKILL` via `PR_SET_PDEATHSIG`, and passes named mutant checks for every refusal.
"""
import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))

import admin
import http_auth
import office_dolt

try:
    import fcntl
    import office_service
except ImportError:  # pragma: no cover - POSIX-only
    fcntl = None
    office_service = None


FAKE_DOLT_SCRIPT = '''#!__PYTHON__
import json, os, signal, socket, sys, time

ROOT = os.getcwd()
BEHAVIOR_PATH = os.path.join(ROOT, 'fake-dolt-behavior.json')
STARTS_LOG = os.path.join(ROOT, 'fake-dolt-starts.jsonl')
PROBES_LOG = os.path.join(ROOT, 'fake-dolt-probes.jsonl')
BOOTSTRAP_LOG = os.path.join(ROOT, 'fake-dolt-bootstrap.jsonl')
PID_FILE = os.path.join(ROOT, 'fake-dolt.pid')
PIDS_LOG = os.path.join(ROOT, 'fake-dolt-pids.txt')
SIGTERM_FLAG = os.path.join(ROOT, 'fake-dolt-sigterm.flag')
CLEAN_EXIT_FLAG = os.path.join(ROOT, 'fake-dolt-clean-exit.flag')


def load_behavior():
    if os.path.isfile(BEHAVIOR_PATH):
        with open(BEHAVIOR_PATH, encoding='utf-8') as f:
            return json.load(f)
    return {}


def append_jsonl(path, obj):
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(obj) + '\\n')


args = sys.argv[1:]
behavior = load_behavior()

if 'sql-server' in args:
    ignore_sigterm = float(behavior.get('ignore_sigterm_seconds', 0))
    if ignore_sigterm > 0:
        stopping = {'active': False}
        def _slow_term(_signum, _frame):
            if stopping['active']:
                return
            stopping['active'] = True
            with open(SIGTERM_FLAG, 'w', encoding='utf-8') as f:
                f.write(str(os.getpid()))
            time.sleep(ignore_sigterm)
            with open(CLEAN_EXIT_FLAG, 'w', encoding='utf-8') as f:
                f.write(str(os.getpid()))
            os._exit(0)
        signal.signal(signal.SIGTERM, _slow_term)
    has_cli_pw = 'DOLT_CLI_PASSWORD' in os.environ
    has_beads_pw = 'BEADS_DOLT_PASSWORD' in os.environ
    append_jsonl(STARTS_LOG, {
        'pid': os.getpid(),
        'args': args,
        'has_cli_pw': has_cli_pw,
        'has_beads_pw': has_beads_pw,
    })
    with open(PID_FILE, 'w', encoding='utf-8') as f:
        f.write(str(os.getpid()))
    with open(PIDS_LOG, 'a', encoding='utf-8') as f:
        f.write(str(os.getpid()) + '\\n')
    if has_cli_pw or has_beads_pw:
        sys.stderr.write('a password is provided, a user must also be provided\\n')
        sys.stderr.flush()
        sys.exit(17)
    mode = behavior.get('server_mode', 'normal')
    if mode == 'exit_early':
        msg = behavior.get('stderr_message', 'fatal: corrupted data dir\\n')
        sys.stderr.write(msg)
        sys.stderr.flush()
        sys.exit(int(behavior.get('exit_code', 2)))
    if mode == 'hang':
        msg = behavior.get('stderr_message', 'still recovering journal...\\n')
        sys.stderr.write(msg)
        sys.stderr.flush()
        while True:
            time.sleep(0.5)
    delay = float(behavior.get('ready_delay', 0))
    if delay > 0:
        time.sleep(delay)
    cfg = json.load(open(args[args.index('--config') + 1], encoding='utf-8'))
    port = int(cfg['listener']['port'])
    expected_pw = behavior.get('expected_password', 'disposable-only')
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', port))
    sock.listen(16)
    while True:
        conn, _ = sock.accept()
        try:
            conn.settimeout(2.0)
            data = b''
            while b'\\n' not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            line = data.decode('utf-8', errors='replace').strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except Exception:
                continue
            pw = req.get('password')
            query = req.get('query', '')
            if mode == 'auth_refusal' or (expected_pw is not None and pw != expected_pw):
                # Real Dolt 2.2.0 prints only "Access denied for user 'root'" (no "(using password: YES)").
                resp = {'ok': False, 'error': "Access denied for user 'root'"}
            else:
                if 'ALTER USER' in query.upper():
                    append_jsonl(BOOTSTRAP_LOG, {'query': query, 'password': pw})
                    if behavior.get('bootstrap_fail_echo_query'):
                        resp = {
                            'ok': False,
                            'error': 'error on line 1 for query %s\\nError parsing SQL: syntax error' % query.strip(),
                        }
                    else:
                        resp = {'ok': True, 'out': ''}
                elif 'mysql.user' in query:
                    resp = {'ok': True, 'out': 'User,Host\\nroot,localhost\\n'}
                else:
                    resp = {'ok': True, 'out': '1\\n1\\n'}
            conn.sendall((json.dumps(resp) + '\\n').encode('utf-8'))
        except Exception:
            pass
        finally:
            conn.close()
elif 'sql' in args:
    port = int(args[args.index('--port') + 1])
    query = sys.stdin.read()
    pw = os.environ.get('DOLT_CLI_PASSWORD', '')
    append_jsonl(PROBES_LOG, {'port': port, 'query': query, 'has_pw': bool(pw)})
    if 'ALTER USER' in query.upper():
        append_jsonl(BOOTSTRAP_LOG, {'query': query, 'password': pw})
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1.0) as conn:
            conn.settimeout(1.0)
            conn.sendall((json.dumps({'password': pw, 'query': query}) + '\\n').encode('utf-8'))
            raw = b''
            while b'\\n' not in raw:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                raw += chunk
    except OSError as exc:
        sys.stderr.write('error connecting to server: %s\\n' % exc)
        sys.exit(1)
    try:
        resp = json.loads(raw.decode('utf-8', errors='replace'))
    except Exception:
        sys.stderr.write('invalid mysql handshake from 127.0.0.1:%d\\n' % port)
        sys.exit(1)
    if not resp.get('ok'):
        sys.stderr.write(resp.get('error', 'sql failed') + '\\n')
        sys.exit(1)
    sys.stdout.write(resp.get('out', ''))
    sys.exit(0)
else:
    sys.exit(2)
'''


BLOCKING_BD_SCRIPT = '''#!__PYTHON__
import os, sys, time
marker = os.environ.get('FAKE_BD_MARKER')
if marker:
    with open(marker, 'w', encoding='utf-8') as f:
        f.write(str(os.getpid()))
time.sleep(float(os.environ.get('FAKE_BD_SLEEP', '30')))
sys.exit(0)
'''


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def port_up(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.2):
            return True
    except OSError:
        return False


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_pid(pid):
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def wait_for(predicate, timeout=15.0, interval=0.05):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return True
        time.sleep(interval)
    return False


class NonDoltListener:
    """Loopback TCP listener that accepts connections and is NOT Dolt."""

    def __init__(self, port, banner=None):
        self.port = port
        self.banner = banner
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', port))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                if self.banner is not None:
                    conn.sendall(self.banner)
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


class OfficeRuntimeMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='office-restore-stop-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'runtime'
        self.logs = self.base / 'logs'
        self.root.mkdir(mode=0o700)
        (self.root / 'bin').mkdir()
        (self.root / 'data').mkdir()
        (self.root / 'projects').mkdir()
        (self.root / 'backups').mkdir()
        self.db_port = free_port()
        self.web_port = free_port()
        self.password = 'disposable-only'
        self.write_deployment()
        (self.root / 'data' / '.doltcfg').mkdir(parents=True, exist_ok=True)
        (self.root / 'data' / '.doltcfg' / 'privileges.db').write_bytes(b'initialized')
        (self.root / 'server.json').write_text(json.dumps({
            'listener': {'host': '127.0.0.1', 'port': self.db_port, 'socket': str(self.root / 'mysql.sock')},
            'data_dir': str(self.root / 'data'),
            'cfg_dir': str(self.root / 'data' / '.doltcfg'),
        }), encoding='utf-8')
        self.config = self.base / 'office.json'
        self.config.write_text('{"schema_version":1}', encoding='utf-8')
        self.install_fake_dolt()
        self.state_path = self.root / 'http-state.json'
        self.backup_dir = self.create_backup()
        self.live_http_state()

    def write_deployment(self, **extra):
        doc = {
            'schema': 1,
            'port': self.db_port,
            'unit': 'office-foreground',
            'password': self.password,
        }
        doc.update(extra)
        (self.root / 'deployment.private.json').write_text(
            json.dumps(doc), encoding='utf-8')

    def install_fake_dolt(self):
        script = self.root / 'bin' / 'dolt'
        script.write_text(FAKE_DOLT_SCRIPT.replace('__PYTHON__', sys.executable), encoding='utf-8')
        script.chmod(0o755)

    def install_blocking_bd(self):
        script = self.root / 'bin' / 'bd'
        script.write_text(BLOCKING_BD_SCRIPT.replace('__PYTHON__', sys.executable), encoding='utf-8')
        script.chmod(0o755)
        return script

    def set_fake_dolt_behavior(self, **behavior):
        (self.root / 'fake-dolt-behavior.json').write_text(
            json.dumps(behavior), encoding='utf-8')

    def read_starts(self):
        path = self.root / 'fake-dolt-starts.jsonl'
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def read_probes(self):
        path = self.root / 'fake-dolt-probes.jsonl'
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def read_bootstrap(self):
        path = self.root / 'fake-dolt-bootstrap.jsonl'
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def create_backup(self):
        backup_dir = self.root / 'backups' / 'src'
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / 'issues.jsonl').write_text('{}\n', encoding='utf-8')
        backup_state_path = self.root / 'backups' / admin.HTTP_STATE_JSON
        store = http_auth.Store(backup_state_path)
        http_auth.Service.bootstrap_superuser(store, 'owner', 'correct-horse-battery-staple')
        svc = http_auth.Service(store)
        logged = svc.login('owner', 'correct-horse-battery-staple')
        principal = svc.authenticate(logged['session_token'])
        svc.create_project(principal, 'src', project_id='src')
        svc.issue_credential(principal, 'src', label='agent-1')
        backup_state_path.chmod(0o600)
        return backup_dir

    def live_http_state(self):
        store = http_auth.Store(self.state_path)
        http_auth.Service.bootstrap_superuser(store, 'liveowner', 'live-secret-password-9999')
        return self.state_path

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root)] + list(argv)), \
                mock.patch.object(admin, 'root_path', return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                admin.run_main()
            except SystemExit as stopped:
                code = stopped.code if isinstance(stopped.code, int) else (1 if stopped.code else 0)
                if isinstance(stopped.code, str):
                    err.write(stopped.code)
        return code, out.getvalue(), err.getvalue()

    def start_office(self):
        proc = subprocess.Popen(
            [sys.executable, str(KIT / 'office_service.py'), 'run',
             '--root', str(self.root), '--port', str(self.web_port),
             '--logs', str(self.logs), '--config', str(self.config)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            encoding='utf-8')
        self.addCleanup(self.stop_proc, proc)
        ready = wait_for(lambda: port_up(self.db_port) and port_up(self.web_port), timeout=15)
        if not ready:
            err = ''
            if proc.poll() is not None and proc.stderr is not None:
                err = proc.stderr.read()
            self.fail('office_service did not open db and web ports: %s' % err)
        return proc

    @staticmethod
    def stop_proc(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()

    def make_fake_add(self, observed_ready=None):
        def fake_add(root, name, **kwargs):
            ready = office_service._database_ready(root)
            if observed_ready is not None:
                observed_ready.append(ready)
            if not ready:
                raise RuntimeError('Dolt server is not reachable on 127.0.0.1:%d' % self.db_port)
            (root / 'projects' / name).mkdir(parents=True, exist_ok=True)
            return {'project': name, 'database': name, 'prefix': name}
        return fake_add


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux foreground office supervisor only')
class OfficeRestoreAfterStopTests(OfficeRuntimeMixin, unittest.TestCase):

    def test_stopped_office_restore_new_with_http_state_starts_and_stops_temporary_dolt(self):
        """Running office refuses --restore-http-state; after stop it succeeds, stops Dolt before HTTP restore, and restarts."""
        proc = self.start_office()
        code, _, err = self.run_cli('restore-new', 'src', 'dst', '--restore-http-state', '--credentials', 'keep')
        self.assertEqual(code, 1)
        self.assertIn('office-service.lock', err)
        self.assertIn('Nothing was changed.', err)
        self.assertFalse((self.root / 'projects' / 'dst').exists())

        proc.terminate()
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertFalse(port_up(self.db_port))
        self.assertFalse(port_up(self.web_port))
        starts_before = len(self.read_starts())

        dolt_during_add = []
        dolt_during_http_restore = []
        orig_restore_http = admin.restore_http_state

        def spy_http_restore(*args, **kwargs):
            dolt_during_http_restore.append(port_up(self.db_port))
            return orig_restore_http(*args, **kwargs)

        with mock.patch.object(admin, 'add_project', side_effect=self.make_fake_add(dolt_during_add)), \
                mock.patch.object(admin, 'native_restore', return_value='restored'), \
                mock.patch.object(admin, 'finish_restore', return_value=None), \
                mock.patch.object(admin, 'restore_http_state', side_effect=spy_http_restore):
            code, out, err = self.run_cli(
                'restore-new', 'src', 'dst',
                '--restore-http-state', '--credentials', 'keep',
            )

        self.assertEqual(code, 0, err)
        self.assertEqual(dolt_during_add, [True], 'temporary Dolt must answer SELECT 1 during restore')
        self.assertEqual(dolt_during_http_restore, [False], 'temporary Dolt must stop before HTTP state restore')
        self.assertFalse(port_up(self.db_port), 'temporary Dolt must be stopped after restore-new')
        self.assertFalse(list(self.root.glob('.restore-dolt-*.log')))
        starts = self.read_starts()
        self.assertEqual(len(starts), starts_before + 1)
        self.assertFalse(starts[-1]['has_cli_pw'])
        self.assertFalse(starts[-1]['has_beads_pw'])
        self.assertFalse(pid_alive(starts[-1]['pid']))

        restored = json.loads(self.state_path.read_text(encoding='utf-8'))
        self.assertIn('owner', restored['usernames'])
        self.assertTrue(restored['sessions'])
        self.assertTrue(all(s.get('revoked') for s in restored['sessions'].values()))
        self.assertEqual(len(restored['credentials']), 1)
        self.assertTrue(all(not c.get('revoked') for c in restored['credentials'].values()))

        restarted = self.start_office()
        restarted.terminate()
        self.assertEqual(restarted.wait(timeout=10), 0)

    def test_stopped_office_restore_new_credentials_revoke(self):
        """--credentials revoke revokes both sessions and worker credentials on a stopped office runtime."""
        with mock.patch.object(admin, 'add_project', side_effect=self.make_fake_add()), \
                mock.patch.object(admin, 'native_restore', return_value='restored'), \
                mock.patch.object(admin, 'finish_restore', return_value=None):
            code, _, err = self.run_cli(
                'restore-new', 'src', 'dstrev',
                '--restore-http-state', '--credentials', 'revoke',
            )
        self.assertEqual(code, 0, err)
        self.assertFalse(port_up(self.db_port))
        restored = json.loads(self.state_path.read_text(encoding='utf-8'))
        self.assertTrue(all(s.get('revoked') for s in restored['sessions'].values()))
        self.assertTrue(all(c.get('revoked') for c in restored['credentials'].values()))

    def test_plain_restore_new_after_stop_takes_lock_and_stops_temporary_dolt(self):
        """Plain restore-new (without --restore-http-state) on a stopped office starts/stops temporary Dolt under office lock."""
        dolt_during_add = []
        lock_held_during_add = []

        def check_lock_and_add(root, name, **kwargs):
            fd = os.open(str(root / 'office-service.lock'), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_held_during_add.append(False)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    lock_held_during_add.append(True)
            finally:
                os.close(fd)
            return self.make_fake_add(dolt_during_add)(root, name, **kwargs)

        with mock.patch.object(admin, 'add_project', side_effect=check_lock_and_add), \
                mock.patch.object(admin, 'native_restore', return_value='restored'), \
                mock.patch.object(admin, 'finish_restore', return_value=None):
            code, _, err = self.run_cli('restore-new', 'src', 'dstplain')

        self.assertEqual(code, 0, err)
        self.assertEqual(dolt_during_add, [True])
        self.assertEqual(lock_held_during_add, [True], 'plain restore-new must hold office-service.lock while running temporary Dolt')
        self.assertFalse(port_up(self.db_port))
        fd = os.open(str(self.root / 'office-service.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def test_plain_restore_new_while_office_running_leaves_running_dolt_untouched(self):
        """When Dolt already answers SELECT 1, plain restore-new starts no second server and never stops the running one."""
        proc = self.start_office()
        starts_before = self.read_starts()
        running_pid = starts_before[-1]['pid']

        with mock.patch.object(admin, 'add_project', side_effect=self.make_fake_add()), \
                mock.patch.object(admin, 'native_restore', return_value='restored'), \
                mock.patch.object(admin, 'finish_restore', return_value=None):
            code, _, err = self.run_cli('restore-new', 'src', 'dstrunning')

        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.read_starts()), len(starts_before), 'must not spawn a second Dolt')
        self.assertTrue(pid_alive(running_pid), 'running Dolt must stay untouched')
        self.assertTrue(office_service._database_ready(self.root))
        proc.terminate()
        self.assertEqual(proc.wait(timeout=10), 0)

    def test_plain_restore_new_refused_when_office_lock_held_and_dolt_not_answering(self):
        """If office-service.lock is held while Dolt is down, plain restore-new refuses and spawns nothing."""
        fd = os.open(str(self.root / 'office-service.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)

        code, _, err = self.run_cli('restore-new', 'src', 'dstlocked')
        self.assertEqual(code, 1)
        self.assertIn('office-service.lock', err)
        self.assertIn('Nothing was changed.', err)
        self.assertEqual(self.read_starts(), [])
        self.assertFalse((self.root / 'projects' / 'dstlocked').exists())

    def test_mid_restore_failure_stops_temporary_dolt_and_cleans_temp_log(self):
        """A failure during native_restore still sees Dolt during failure reporting, then stops Dolt and cleans temp log."""
        dolt_during_state_check = []
        orig_dest_state = admin.restore_destination_state

        def spy_dest_state(*args, **kwargs):
            dolt_during_state_check.append(port_up(self.db_port))
            return orig_dest_state(*args, **kwargs)

        with mock.patch.object(admin, 'add_project', side_effect=self.make_fake_add()), \
                mock.patch.object(admin, 'native_restore', side_effect=RuntimeError('injected native failure')), \
                mock.patch.object(admin, 'restore_destination_state', side_effect=spy_dest_state):
            with self.assertRaises(RuntimeError) as exc:
                self.run_cli('restore-new', 'src', 'dstfail')

        self.assertIn('injected native failure', str(exc.exception))
        self.assertEqual(dolt_during_state_check, [True])
        self.assertFalse(port_up(self.db_port), 'temporary Dolt must be stopped after mid-restore failure')
        self.assertFalse(list(self.root.glob('.restore-dolt-*.log')))
        starts = self.read_starts()
        self.assertEqual(len(starts), 1)
        self.assertFalse(pid_alive(starts[0]['pid']))

    def test_preflight_failure_refuses_before_starting_temporary_dolt(self):
        """Missing backed-up HTTP state refuses before spawning any temporary Dolt."""
        (self.root / 'backups' / admin.HTTP_STATE_JSON).unlink()
        code, _, err = self.run_cli('restore-new', 'src', 'dstpreflight',
                                    '--restore-http-state', '--credentials', 'keep')
        self.assertEqual(code, 1)
        self.assertEqual(self.read_starts(), [], 'preflight refusal must happen before spawning Dolt')
        self.assertFalse((self.root / 'projects' / 'dstpreflight').exists())

    def test_sigterm_during_restore_stops_temporary_dolt_and_cleans_temp_log(self):
        """SIGTERM sent to restore-new subprocess stops temporary Dolt cleanly and leaves no temp log."""
        self.install_blocking_bd()
        marker = self.base / 'bd-running.pid'
        env = dict(os.environ, FAKE_BD_MARKER=str(marker), FAKE_BD_SLEEP='30')
        proc = subprocess.Popen(
            [sys.executable, str(KIT / 'admin.py'), '--root', str(self.root),
             'restore-new', 'src', 'dstterm', '--restore-http-state', '--credentials', 'keep'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', env=env)
        self.addCleanup(self.stop_proc, proc)
        self.assertTrue(wait_for(lambda: marker.is_file() and (self.root / 'fake-dolt.pid').is_file(), timeout=15),
                        'restore-new did not reach add-project with temporary Dolt running')
        dolt_pid = int((self.root / 'fake-dolt.pid').read_text(encoding='utf-8').strip())
        self.addCleanup(kill_pid, dolt_pid)
        self.assertTrue(pid_alive(dolt_pid))

        proc.terminate()
        proc.wait(timeout=15)
        self.assertTrue(wait_for(lambda: not pid_alive(dolt_pid), timeout=10),
                        'temporary Dolt survived SIGTERM of restore-new')
        self.assertFalse(port_up(self.db_port))
        self.assertFalse(list(self.root.glob('.restore-dolt-*.log')))

    def test_sigkill_of_restore_new_leaves_no_temporary_dolt_running(self):
        """Condition 6(a): SIGKILL of restore-new kills temporary Dolt via PR_SET_PDEATHSIG."""
        self.install_blocking_bd()
        marker = self.base / 'bd-sigkill.pid'
        env = dict(os.environ, FAKE_BD_MARKER=str(marker), FAKE_BD_SLEEP='30')
        proc = subprocess.Popen(
            [sys.executable, str(KIT / 'admin.py'), '--root', str(self.root),
             'restore-new', 'src', 'dstkill', '--restore-http-state', '--credentials', 'keep'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', env=env)
        self.addCleanup(self.stop_proc, proc)
        self.assertTrue(wait_for(lambda: marker.is_file() and (self.root / 'fake-dolt.pid').is_file(), timeout=15),
                        'restore-new did not start temporary Dolt before SIGKILL')
        dolt_pid = int((self.root / 'fake-dolt.pid').read_text(encoding='utf-8').strip())
        self.addCleanup(kill_pid, dolt_pid)
        self.assertTrue(pid_alive(dolt_pid))
        self.assertTrue(office_service._database_ready(self.root))

        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        self.assertTrue(wait_for(lambda: not pid_alive(dolt_pid), timeout=5),
                        'temporary Dolt PID %d survived SIGKILL of restore-new' % dolt_pid)
        self.assertFalse(port_up(self.db_port))

    def test_non_dolt_listener_on_db_port_is_refused_and_never_starts_second_server(self):
        """Condition 3 & 6(b): a non-Dolt TCP listener on the DB port is refused without spawning Dolt."""
        for label, banner in (('silent-tcp', None), ('http-banner', b'HTTP/1.1 400 Bad Request\r\n\r\n')):
            with self.subTest(listener=label):
                other = NonDoltListener(self.db_port, banner=banner)
                try:
                    code, _, err = self.run_cli('restore-new', 'src', 'dstnondolt')
                finally:
                    other.close()
                self.assertEqual(code, 1)
                self.assertIn('already in use', err)
                self.assertIn('SELECT 1', err)
                self.assertIn('a second Dolt server was not started', err)
                self.assertIn('Nothing was changed.', err)
                self.assertEqual(self.read_starts(), [], 'must never spawn a second Dolt when port is held')
                self.assertFalse((self.root / 'projects' / 'dstnondolt').exists())

    def test_auth_refusal_never_bootstraps_credentials_for_temporary_or_running_dolt(self):
        """Condition 2: root auth refusal refuses with 'Nothing was changed.' and never sets a password."""
        self.set_fake_dolt_behavior(server_mode='auth_refusal')
        code, _, err = self.run_cli('restore-new', 'src', 'dstauth')
        self.assertEqual(code, 1)
        self.assertIn('refused the root connection', err)
        self.assertIn('never sets or resets database credentials', err)
        self.assertIn('Nothing was changed.', err)
        self.assertFalse(port_up(self.db_port), 'temporary Dolt must be stopped on auth refusal')
        self.assertEqual(self.read_bootstrap(), [], 'restore must NEVER bootstrap credentials')
        probes = self.read_probes()
        self.assertTrue(probes)
        self.assertTrue(all(p['has_pw'] for p in probes), 'restore must never probe with an empty root password')
        self.assertFalse((self.root / 'projects' / 'dstauth').exists())

        self.set_fake_dolt_behavior(server_mode='normal', expected_password='some-other-password')
        db_env = office_dolt.db_server_env(admin.environment(self.root))
        external_log = self.base / 'external-dolt.log'
        external = office_dolt.spawn_process(office_dolt.dolt_command(self.root), self.root, external_log, db_env)
        self.addCleanup(office_dolt.stop_process, external, time.monotonic() + 5)
        self.assertTrue(wait_for(lambda: port_up(self.db_port), timeout=10))
        starts_before = len(self.read_starts())

        code, _, err = self.run_cli('restore-new', 'src', 'dstauthrunning')
        self.assertEqual(code, 1)
        self.assertIn('refused the root connection', err)
        self.assertIn('Nothing was changed.', err)
        self.assertEqual(len(self.read_starts()), starts_before, 'must not spawn a second Dolt on running auth refusal')
        self.assertEqual(self.read_bootstrap(), [], 'restore must NEVER bootstrap credentials')
        office_dolt.stop_process(external, time.monotonic() + 5)

    def test_configurable_startup_seconds_timeout_stops_child_and_prints_bounded_diagnostic(self):
        """Condition 5: dolt_startup_seconds bounds wait, stops child on timeout, and prints bounded single-line diagnostic."""
        self.assertEqual(office_dolt.dolt_startup_seconds({}), 30)
        self.write_deployment(dolt_startup_seconds=2)
        noisy = ('\x1b[31mRECOVERY_MARKER\x07\r\n' + ('x' * 1500) + '\nTAIL_MARKER\n')
        self.set_fake_dolt_behavior(server_mode='hang', stderr_message=noisy)

        started = time.monotonic()
        code, _, err = self.run_cli('restore-new', 'src', 'dsttimeout')
        elapsed = time.monotonic() - started

        self.assertEqual(code, 1)
        self.assertLess(elapsed, 10.0, 'must honor dolt_startup_seconds=2 rather than 30s default')
        self.assertIn('within 2 seconds', err)
        self.assertIn('Nothing was changed.', err)
        self.assertIn('TAIL_MARKER', err)
        self.assertNotIn('\x1b', err)
        self.assertNotIn('\x07', err)
        self.assertLessEqual(len(err), 900, 'timeout diagnostic must be bounded')
        starts = self.read_starts()
        self.assertEqual(len(starts), 1)
        self.assertFalse(pid_alive(starts[0]['pid']), 'timed-out temporary Dolt must be stopped')
        self.assertFalse(list(self.root.glob('.restore-dolt-*.log')))
        self.assertFalse((self.root / 'projects' / 'dsttimeout').exists())

    def test_early_exit_during_startup_reports_bounded_diagnostic_and_cleans_up(self):
        """If temporary Dolt exits during startup, restore-new reports status and bounded log tail."""
        self.set_fake_dolt_behavior(server_mode='exit_early', exit_code=9,
                                    stderr_message='fatal: cannot open data lock\n')
        code, _, err = self.run_cli('restore-new', 'src', 'dstearly')
        self.assertEqual(code, 1)
        self.assertIn('exited with status 9', err)
        self.assertIn('cannot open data lock', err)
        self.assertIn('Nothing was changed.', err)
        self.assertFalse(list(self.root.glob('.restore-dolt-*.log')))
        self.assertFalse((self.root / 'projects' / 'dstearly').exists())

    def test_invalid_dolt_startup_seconds_refused_in_restore_and_supervisor(self):
        """Invalid dolt_startup_seconds values are refused before starting Dolt in both admin and office_service."""
        for bad in (0, -1, 601, True, False, '5', 2.5, None):
            with self.subTest(dolt_startup_seconds=bad):
                self.write_deployment(dolt_startup_seconds=bad)
                code, _, err = self.run_cli('restore-new', 'src', 'dstbadcfg')
                self.assertEqual(code, 1)
                self.assertIn('dolt_startup_seconds', err)
                self.assertIn('Nothing was changed.', err)
                self.assertEqual(self.read_starts(), [])
                with self.assertRaises(ValueError) as exc:
                    office_service.run(self.root, self.logs, self.config, self.web_port, 5)
                self.assertIn('dolt_startup_seconds', str(exc.exception))

    def test_shared_helper_single_source_of_truth(self):
        """Condition 1: office_service and admin share office_dolt's spawn/stop/PDEATHSIG/env helpers."""
        self.assertIs(office_service._after_fork, office_dolt.after_fork)
        self.assertIs(office_service._stop, office_dolt.stop_process)
        cleaned = office_dolt.db_server_env({
            'PATH': '/usr/bin',
            'DOLT_CLI_PASSWORD': 'secret',
            'BEADS_DOLT_PASSWORD': 'secret',
        })
        self.assertEqual(cleaned, {'PATH': '/usr/bin'})


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux foreground office supervisor only')
class RefusalPinTests(OfficeRuntimeMixin, unittest.TestCase):
    """Condition 6(c) & r2 pins: every test exercises real code and fails when its source mutant is injected."""

    def test_pin_sigterm_during_temporary_dolt_stop_prints_http_notice_and_cleans_up(self):
        """SHOULD-1 (stop-guard): SIGTERM while stopping temporary Dolt exits 143 with the HTTP notice and cleans up."""
        self.set_fake_dolt_behavior(ignore_sigterm_seconds=3.5)
        before_http = self.state_path.read_bytes()
        runner = self.base / 'run_stop_guard.py'
        runner.write_text('''import sys
from unittest import mock
sys.path.insert(0, %r)
import admin
root = %r
def fake_add(r, name, **kw):
    (r / 'projects' / name).mkdir(parents=True, exist_ok=True)
    return {'project': name, 'database': name, 'prefix': name}
sys.argv = ['admin.py', '--root', root, 'restore-new', 'src', 'dststop',
            '--restore-http-state', '--credentials', 'keep']
with mock.patch.object(admin, 'add_project', side_effect=fake_add), \\
     mock.patch.object(admin, 'native_restore', return_value='restored'), \\
     mock.patch.object(admin, 'finish_restore', return_value=None):
    admin.run_main()
''' % (str(KIT), str(self.root)), encoding='utf-8')
        proc = subprocess.Popen(
            [sys.executable, str(runner)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8',
        )
        self.addCleanup(self.stop_proc, proc)
        sigterm_flag = self.root / 'fake-dolt-sigterm.flag'
        self.assertTrue(wait_for(lambda: sigterm_flag.is_file(), timeout=15),
                        'temporary Dolt did not enter its stop phase')
        dolt_pid = int(sigterm_flag.read_text(encoding='utf-8').strip())
        self.addCleanup(kill_pid, dolt_pid)
        time.sleep(0.2)
        proc.terminate()
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 143, 'expected guarded exit 143, got %r (stderr=%r)' % (proc.returncode, err))
        self.assertIn('The project dststop was restored', err)
        self.assertIn('the HTTP state was NOT restored', err)
        self.assertIn('restore-new src NEWNAME --restore-http-state --credentials keep into a NEW name', err)
        self.assertEqual(self.state_path.read_bytes(), before_http, 'live HTTP state must stay unchanged')
        self.assertTrue(wait_for(lambda: not pid_alive(dolt_pid), timeout=8),
                        'temporary Dolt survived interrupted stop')
        self.assertFalse(port_up(self.db_port))
        self.assertEqual(list(self.root.glob('.restore-dolt-*.log')), [])
        self.assertTrue((self.root / 'fake-dolt-clean-exit.flag').is_file(),
                        'restore-new must wait for temporary Dolt to finish stopping before exiting')

    def test_pin_bootstrap_database_escapes_backslashes_and_never_leaks_password(self):
        """SHOULD-2 (bootstrap-password): failing bootstrap escapes backslashes and never leaks password to error or logs."""
        secret = "Secr3t'Part\\"
        self.password = secret
        self.write_deployment(password=secret, dolt_startup_seconds=3)
        self.set_fake_dolt_behavior(server_mode='normal', expected_password='', bootstrap_fail_echo_query=True)

        db_env = office_dolt.db_server_env(admin.environment(self.root))
        child = office_dolt.spawn_process(
            office_dolt.dolt_command(self.root), self.root, self.base / 'boot-dolt.log', db_env)
        self.addCleanup(office_dolt.stop_process, child, time.monotonic() + 5)
        self.assertTrue(wait_for(lambda: port_up(self.db_port), timeout=10))
        try:
            with self.assertRaises(RuntimeError) as exc:
                office_dolt.bootstrap_database(self.root, admin.config(self.root), admin.environment(self.root))
            msg = str(exc.exception)
            self.assertNotIn(secret, msg)
            self.assertNotIn("Secr3t", msg)
            self.assertNotIn('ALTER USER', msg)
            boot_entries = self.read_bootstrap()
            self.assertTrue(boot_entries)
            self.assertIn("IDENTIFIED BY 'Secr3t''Part\\\\';", boot_entries[-1]['query'])
        finally:
            office_dolt.stop_process(child, time.monotonic() + 5)

        proc = subprocess.Popen(
            [sys.executable, str(KIT / 'office_service.py'), 'run',
             '--root', str(self.root), '--port', str(self.web_port),
             '--logs', str(self.logs), '--config', str(self.config)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8',
        )
        self.addCleanup(self.stop_proc, proc)
        _, stderr = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn(secret, stderr)
        self.assertNotIn('Secr3t', stderr)
        self.assertNotIn('ALTER USER', stderr)
        for log_file in self.logs.glob('*'):
            text = log_file.read_text(encoding='utf-8', errors='replace')
            self.assertNotIn(secret, text)
            self.assertNotIn('Secr3t', text)

    def test_pin_sigterm_during_startup_wait_stops_temporary_dolt_and_cleans_log(self):
        """SHOULD-3 (R1, CLI): SIGTERM during the startup wait exits 143, stops Dolt, and cleans temp log."""
        self.write_deployment(dolt_startup_seconds=30)
        self.set_fake_dolt_behavior(server_mode='hang')
        proc = subprocess.Popen(
            [sys.executable, str(KIT / 'admin.py'), '--root', str(self.root),
             'restore-new', 'src', 'dstwaitterm', '--restore-http-state', '--credentials', 'keep'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8',
        )
        self.addCleanup(self.stop_proc, proc)
        self.assertTrue(wait_for(lambda: (self.root / 'fake-dolt.pid').is_file()
                                 and bool(list(self.root.glob('.restore-dolt-*.log'))), timeout=15),
                        'restore-new did not enter startup wait')
        dolt_pid = int((self.root / 'fake-dolt.pid').read_text(encoding='utf-8').strip())
        self.addCleanup(kill_pid, dolt_pid)
        proc.terminate()
        proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 143)
        self.assertTrue(wait_for(lambda: not pid_alive(dolt_pid), timeout=8))
        self.assertEqual(list(self.root.glob('.restore-dolt-*.log')), [])

    def test_pin_direct_temporary_office_dolt_startup_wait_is_signal_guarded(self):
        """SHOULD-3 (R1, direct): calling temporary_office_dolt directly guards SIGTERM during startup wait."""
        self.write_deployment(dolt_startup_seconds=30)
        self.set_fake_dolt_behavior(server_mode='hang')
        runner = self.base / 'run_direct_wait.py'
        runner.write_text('''import sys
from pathlib import Path
sys.path.insert(0, %r)
import admin
try:
    with admin.temporary_office_dolt(Path(%r), holds_office_lock=False):
        pass
except admin.TerminatedBySignal as exc:
    sys.exit(128 + exc.signum)
''' % (str(KIT), str(self.root)), encoding='utf-8')
        proc = subprocess.Popen(
            [sys.executable, str(runner)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8',
        )
        self.addCleanup(self.stop_proc, proc)
        self.assertTrue(wait_for(lambda: (self.root / 'fake-dolt.pid').is_file()
                                 and bool(list(self.root.glob('.restore-dolt-*.log'))), timeout=15),
                        'direct temporary_office_dolt did not enter startup wait')
        dolt_pid = int((self.root / 'fake-dolt.pid').read_text(encoding='utf-8').strip())
        self.addCleanup(kill_pid, dolt_pid)
        proc.terminate()
        proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 143)
        self.assertTrue(wait_for(lambda: not pid_alive(dolt_pid), timeout=8))
        self.assertEqual(list(self.root.glob('.restore-dolt-*.log')), [])

    def test_pin_supervisor_honors_dolt_startup_seconds(self):
        """SHOULD-3 (R3): office_service.py run honors dolt_startup_seconds rather than hardcoded 30 s."""
        self.write_deployment(dolt_startup_seconds=2)
        self.set_fake_dolt_behavior(server_mode='hang')
        started = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, str(KIT / 'office_service.py'), 'run',
             '--root', str(self.root), '--port', str(self.web_port),
             '--logs', str(self.logs), '--config', str(self.config)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8',
        )
        self.addCleanup(self.stop_proc, proc)
        _, stderr = proc.communicate(timeout=15)
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 1)
        self.assertLess(elapsed, 10.0, 'supervisor did not honor dolt_startup_seconds=2 (elapsed=%.2fs)' % elapsed)
        self.assertIn('within 2 seconds', stderr)
        starts = self.read_starts()
        self.assertEqual(len(starts), 1)
        self.assertFalse(pid_alive(starts[0]['pid']))

    def test_pin_restore_http_state_reuses_hand_started_dolt_and_leaves_it_running(self):
        """NOTE-7 (R2b): --restore-http-state reuses an already-running hand-started Dolt without spawning another."""
        db_env = office_dolt.db_server_env(admin.environment(self.root))
        external_log = self.base / 'hand-dolt.log'
        external = office_dolt.spawn_process(
            office_dolt.dolt_command(self.root), self.root, external_log, db_env)
        self.addCleanup(office_dolt.stop_process, external, time.monotonic() + 5)
        self.assertTrue(wait_for(lambda: office_service._database_ready(self.root), timeout=10))
        starts_before = len(self.read_starts())

        with mock.patch.object(admin, 'add_project', side_effect=self.make_fake_add()), \
                mock.patch.object(admin, 'native_restore', return_value='restored'), \
                mock.patch.object(admin, 'finish_restore', return_value=None):
            code, _, err = self.run_cli(
                'restore-new', 'src', 'dstreuse',
                '--restore-http-state', '--credentials', 'keep',
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.read_starts()), starts_before, 'must reuse hand-started Dolt without spawning a second one')
        self.assertTrue(pid_alive(external.pid), 'hand-started Dolt must remain running after restore-new')
        self.assertTrue(office_service._database_ready(self.root))
        office_dolt.stop_process(external, time.monotonic() + 5)

    def test_pin_never_run_runtime_refused_before_spawn_without_touching_data(self):
        """NOTE-1: a prepared, never-run runtime (missing privileges.db) is refused before spawning Dolt."""
        priv = self.root / 'data' / '.doltcfg' / 'privileges.db'
        priv.unlink()
        (self.root / 'data' / '.doltcfg').rmdir()
        before_data = sorted(str(p.relative_to(self.root / 'data')) for p in (self.root / 'data').rglob('*'))

        code, _, err = self.run_cli('restore-new', 'src', 'dstneverrun',
                                    '--restore-http-state', '--credentials', 'keep')
        self.assertEqual(code, 1)
        self.assertIn('privileges.db', err)
        self.assertIn('Start the office service once, stop it, then run restore-new again.', err)
        self.assertIn('Nothing was changed.', err)
        self.assertEqual(self.read_starts(), [], 'must not spawn Dolt in a never-run runtime')
        after_data = sorted(str(p.relative_to(self.root / 'data')) for p in (self.root / 'data').rglob('*'))
        self.assertEqual(after_data, before_data)
        self.assertFalse((self.root / 'projects' / 'dstneverrun').exists())

    def test_pin_prctl_unavailable_gives_one_line_refusal_no_traceback(self):
        """NOTE-2 / r3 NOTE-F: missing or ENOSYS prctl gives exact parent-death refusal without 'last probe:' or '..'."""
        import errno
        expected = (
            'ValueError: Refusing restore-new: could not start temporary Dolt with parent-death protection '
            '(PR_SET_PDEATHSIG): Exception occurred in preexec_fn. Nothing was changed.'
        )
        for err_obj in (OSError(errno.ENOSYS, 'PR_SET_PDEATHSIG failed'), AttributeError('undefined symbol: prctl')):
            with self.subTest(error=type(err_obj).__name__):
                with mock.patch.object(office_dolt, 'after_fork', side_effect=err_obj):
                    code, _, err = self.run_cli('restore-new', 'src', 'dstnoprctl')
                self.assertEqual(code, 1)
                self.assertEqual(err.strip(), expected)
                self.assertNotIn('last probe:', err)
                self.assertNotIn('..', err)
                self.assertNotIn('Traceback', err)
                self.assertEqual(list(self.root.glob('.restore-dolt-*.log')), [])
                self.assertFalse((self.root / 'projects' / 'dstnoprctl').exists())

    def test_pin_spawn_oserror_gives_generic_temporary_dolt_refusal_exact(self):
        """r3 NOTE-F: non-prctl spawn OSError says 'could not start the temporary Dolt' without PR_SET_PDEATHSIG."""
        import errno
        cases = [
            ('missing_binary', FileNotFoundError(errno.ENOENT, 'No such file or directory', '/missing/dolt'),
             "ValueError: Refusing restore-new: could not start the temporary Dolt: "
             "[Errno 2] No such file or directory: '/missing/dolt'. Nothing was changed."),
            ('log_open_denied', PermissionError(errno.EACCES, 'Permission denied.', '/tmp/restore.log'),
             "ValueError: Refusing restore-new: could not start the temporary Dolt: "
             "[Errno 13] Permission denied.: '/tmp/restore.log'. Nothing was changed."),
            ('trailing_dot_message', OSError('exec failed.'),
             "ValueError: Refusing restore-new: could not start the temporary Dolt: exec failed. Nothing was changed."),
        ]
        for label, err_obj, expected in cases:
            with self.subTest(case=label):
                with mock.patch.object(office_dolt, 'spawn_process', side_effect=err_obj):
                    code, _, err = self.run_cli('restore-new', 'src', 'dstspawnerr')
                self.assertEqual(code, 1)
                self.assertEqual(err.strip(), expected)
                self.assertNotIn('PR_SET_PDEATHSIG', err)
                self.assertNotIn('last probe:', err)
                self.assertNotIn('..', err)
                self.assertNotIn('Traceback', err)
                self.assertEqual(list(self.root.glob('.restore-dolt-*.log')), [])
                self.assertFalse((self.root / 'projects' / 'dstspawnerr').exists())

    def test_pin_sigint_during_temporary_dolt_stop_names_sigint_and_keeps_warnings(self):
        """r3 NOTE-C & NOTE-G: Ctrl-C during temporary Dolt stop names (SIGINT), never (), and repeats end-of-run warnings."""
        self.set_fake_dolt_behavior(ignore_sigterm_seconds=3.5)
        before_http = self.state_path.read_bytes()
        runner = self.base / 'run_sigint_stop.py'
        runner.write_text('''import sys
from unittest import mock
sys.path.insert(0, %r)
import admin
root = %r
def fake_add(r, name, **kw):
    (r / 'projects' / name).mkdir(parents=True, exist_ok=True)
    return {'project': name, 'database': name, 'prefix': name}
def fake_finish(r, args, snapshot, step):
    args.task_files_warning = 'task files of dstsigint: mismatch warning'
    return 'Authority was NOT re-granted warning'
sys.argv = ['admin.py', '--root', root, 'restore-new', 'src', 'dstsigint',
            '--restore-http-state', '--credentials', 'keep']
with mock.patch.object(admin, 'add_project', side_effect=fake_add), \\
     mock.patch.object(admin, 'native_restore', return_value='restored'), \\
     mock.patch.object(admin, 'finish_restore', side_effect=fake_finish):
    admin.run_main()
''' % (str(KIT), str(self.root)), encoding='utf-8')
        proc = subprocess.Popen(
            [sys.executable, str(runner)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8',
        )
        self.addCleanup(self.stop_proc, proc)
        sigterm_flag = self.root / 'fake-dolt-sigterm.flag'
        self.assertTrue(wait_for(lambda: sigterm_flag.is_file(), timeout=15),
                        'temporary Dolt did not enter its stop phase')
        dolt_pid = int(sigterm_flag.read_text(encoding='utf-8').strip())
        self.addCleanup(kill_pid, dolt_pid)
        time.sleep(0.2)
        proc.send_signal(signal.SIGINT)
        _, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 130, 'expected SIGINT exit 130, got %r (stderr=%r)' % (proc.returncode, err))
        expected_notice = (
            'The project dstsigint was restored, but restore-new was stopped while its temporary Dolt '
            'was being stopped (SIGINT): the HTTP state was NOT restored. To restore it, run '
            'restore-new src NEWNAME --restore-http-state --credentials keep into a NEW name; '
            'the clone dstsigint can then be retired (admin.py retire-project dstsigint --actor OPERATOR '
            '--reason REASON, with --force when it holds issues), and its name stays reserved: '
            'a retired name is never reused.'
        )
        err_lines = err.strip().splitlines()
        self.assertEqual(err_lines[0], expected_notice)
        self.assertIn('Authority was NOT re-granted warning', err_lines)
        self.assertIn('task files of dstsigint: mismatch warning', err_lines)
        self.assertNotIn('()', err)
        self.assertEqual(self.state_path.read_bytes(), before_http)
        self.assertTrue(wait_for(lambda: not pid_alive(dolt_pid), timeout=8))
        self.assertEqual(list(self.root.glob('.restore-dolt-*.log')), [])

        # Plain restore-new (without --restore-http-state) on SIGINT during Dolt stop
        (self.root / 'fake-dolt-sigterm.flag').unlink()
        runner_plain = self.base / 'run_sigint_stop_plain.py'
        runner_plain.write_text('''import sys
from unittest import mock
sys.path.insert(0, %r)
import admin
root = %r
def fake_add(r, name, **kw):
    (r / 'projects' / name).mkdir(parents=True, exist_ok=True)
    return {'project': name, 'database': name, 'prefix': name}
sys.argv = ['admin.py', '--root', root, 'restore-new', 'src', 'dstsigintplain']
with mock.patch.object(admin, 'add_project', side_effect=fake_add), \\
     mock.patch.object(admin, 'native_restore', return_value='restored'), \\
     mock.patch.object(admin, 'finish_restore', return_value=None):
    admin.run_main()
''' % (str(KIT), str(self.root)), encoding='utf-8')
        proc2 = subprocess.Popen(
            [sys.executable, str(runner_plain)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8',
        )
        self.addCleanup(self.stop_proc, proc2)
        self.assertTrue(wait_for(lambda: sigterm_flag.is_file(), timeout=15))
        dolt_pid2 = int(sigterm_flag.read_text(encoding='utf-8').strip())
        self.addCleanup(kill_pid, dolt_pid2)
        time.sleep(0.2)
        proc2.send_signal(signal.SIGINT)
        _, err2 = proc2.communicate(timeout=15)
        self.assertEqual(proc2.returncode, 130, 'expected SIGINT exit 130, got %r (stderr=%r)' % (proc2.returncode, err2))
        self.assertEqual(
            err2.strip(),
            'The project dstsigintplain was restored, but restore-new was stopped while its temporary Dolt '
            'was being stopped (SIGINT).',
        )
        self.assertNotIn('()', err2)
        self.assertTrue(wait_for(lambda: not pid_alive(dolt_pid2), timeout=8))

    def test_pin_stale_restore_log_and_dead_socket_cleaned_on_next_run(self):
        """NOTE-3 / r3 SHOULD-1: stale .restore-dolt-*.log and dead mysql.sock are cleaned by both restore-new and office_service.py run."""
        stale_log = self.root / '.restore-dolt-stale123.log'
        stale_log.write_text('old crash log\n', encoding='utf-8')
        sock_path = self.root / 'mysql.sock'
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(sock_path))
        s.close()
        self.assertTrue(sock_path.exists())

        with mock.patch.object(admin, 'add_project', side_effect=self.make_fake_add()), \
                mock.patch.object(admin, 'native_restore', return_value='restored'), \
                mock.patch.object(admin, 'finish_restore', return_value=None):
            code, _, err = self.run_cli('restore-new', 'src', 'dstclean')
        self.assertEqual(code, 0, err)
        self.assertFalse(stale_log.exists(), 'stale .restore-dolt-*.log should be cleaned up by restore-new')
        self.assertFalse(sock_path.exists(), 'dead mysql.sock should be cleaned up by restore-new')

        # Now verify office_service.py run also cleans stale .restore-dolt-*.log and dead mysql.sock on start.
        stale_log2 = self.root / '.restore-dolt-stale456.log'
        stale_log2.write_text('old crash log 2\n', encoding='utf-8')
        s2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s2.bind(str(sock_path))
        s2.close()
        self.assertTrue(stale_log2.exists())
        self.assertTrue(sock_path.exists())
        proc = subprocess.Popen(
            [sys.executable, str(KIT / 'office_service.py'), 'run',
             '--root', str(self.root), '--port', str(self.web_port),
             '--logs', str(self.logs), '--config', str(self.config)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8',
        )
        self.addCleanup(self.stop_proc, proc)
        self.assertTrue(wait_for(lambda: not stale_log2.exists() and not sock_path.exists(), timeout=15),
                        'office_service.py run must remove stale .restore-dolt-*.log and dead mysql.sock on start')
        self.stop_proc(proc)

    def test_pin_existing_dolt_auth_refusal_refuses_without_spawning(self):
        """SHOULD-3 (R8): existing Dolt refusing root with real Dolt 2.2.0 text refuses without spawning."""
        self.set_fake_dolt_behavior(server_mode='normal', expected_password='other-root-password')
        db_env = office_dolt.db_server_env(admin.environment(self.root))
        external = office_dolt.spawn_process(
            office_dolt.dolt_command(self.root), self.root, self.base / 'ext-auth.log', db_env)
        self.addCleanup(office_dolt.stop_process, external, time.monotonic() + 5)
        self.assertTrue(wait_for(lambda: port_up(self.db_port), timeout=10))
        starts_before = len(self.read_starts())

        code, _, err = self.run_cli('restore-new', 'src', 'dstextauth')
        self.assertEqual(code, 1)
        self.assertIn('refused the root connection', err)
        self.assertIn('Nothing was changed.', err)
        self.assertEqual(len(self.read_starts()), starts_before)
        self.assertTrue(pid_alive(external.pid))
        office_dolt.stop_process(external, time.monotonic() + 5)

    def test_pin_temporary_dolt_early_exit_refuses_immediately_with_status_and_log(self):
        """SHOULD-3 (R14): temporary Dolt exiting during startup refuses immediately with status and log tail."""
        self.write_deployment(dolt_startup_seconds=15)
        self.set_fake_dolt_behavior(server_mode='exit_early', exit_code=11,
                                    stderr_message='fatal: database demo is locked by another dolt process\n')
        started = time.monotonic()
        code, _, err = self.run_cli('restore-new', 'src', 'dstearlypin')
        elapsed = time.monotonic() - started
        self.assertEqual(code, 1)
        self.assertLess(elapsed, 6.0, 'early exit must not wait out dolt_startup_seconds')
        self.assertIn('exited with status 11', err)
        self.assertIn('locked by another dolt process', err)
        self.assertIn('Nothing was changed.', err)
        self.assertEqual(list(self.root.glob('.restore-dolt-*.log')), [])

    def test_pin_temporary_dolt_not_ready_in_time_refuses_and_stops_child(self):
        """SHOULD-3 (R15): temporary Dolt that never becomes ready refuses after dolt_startup_seconds and stops child."""
        self.write_deployment(dolt_startup_seconds=1)
        self.set_fake_dolt_behavior(server_mode='hang', stderr_message='still opening table\n')
        code, _, err = self.run_cli('restore-new', 'src', 'dstnotreadypin')
        self.assertEqual(code, 1)
        self.assertIn('within 1 seconds', err)
        self.assertIn('still opening table', err)
        self.assertIn('Nothing was changed.', err)
        starts = self.read_starts()
        self.assertEqual(len(starts), 1)
        self.assertFalse(pid_alive(starts[0]['pid']))
        self.assertEqual(list(self.root.glob('.restore-dolt-*.log')), [])

    def test_pin_office_service_uses_shared_dolt_command(self):
        """NOTE-5: office_service.py uses office_dolt.dolt_command instead of duplicating sql-server argv."""
        src = (KIT / 'office_service.py').read_text(encoding='utf-8')
        self.assertIn('office_dolt.dolt_command(', src)
        self.assertNotIn("'sql-server', '--config'", src)


if __name__ == '__main__':
    unittest.main()
