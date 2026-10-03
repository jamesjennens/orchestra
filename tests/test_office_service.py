"""Disposable Linux checks for the foreground office supervisor."""
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux supervisor only')
class OfficeServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = Path(__file__).resolve().parents[1] / 'office_service.py'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base/'runtime'
        self.logs = self.base/'logs'
        self.root.mkdir(mode=0o700)
        (self.root/'bin').mkdir()
        (self.root/'data').mkdir()
        self.db_port = self.free_port()
        self.web_port = self.free_port()
        (self.root/'deployment.private.json').write_text(json.dumps({
            'schema': 1, 'port': self.db_port, 'unit': 'office-foreground',
            'password': 'disposable-only'}), encoding='utf-8')
        (self.root/'server.json').write_text(json.dumps({
            'listener': {'host': '127.0.0.1', 'port': self.db_port}}), encoding='utf-8')
        self.config = self.base/'office.json'
        self.config.write_text('{"schema_version":1}', encoding='utf-8')
        self.fake_dolt()

    @staticmethod
    def free_port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    def fake_dolt(self):
        script = self.root/'bin'/'dolt'
        script.write_text('''#!%s
import json, os, socket, sys, time
args=sys.argv[1:]
if 'sql-server' in args:
    if 'DOLT_CLI_PASSWORD' in os.environ or 'BEADS_DOLT_PASSWORD' in os.environ:
        sys.exit(17)  # server must not inherit client credentials
    time.sleep(float(os.environ.get('FAKE_DOLT_READY_DELAY', '0')))
    cfg=json.load(open(args[args.index('--config')+1]))
    port=cfg['listener']['port']
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        s.bind(('127.0.0.1',port));s.listen()
        open(os.path.join(os.getcwd(),'db.pid'),'w').write(str(os.getpid()))
        while True:
            conn,_=s.accept();conn.close()
elif 'sql' in args:
    port=int(args[args.index('--port')+1])
    with socket.create_connection(('127.0.0.1',port),timeout=1): pass
    query=sys.stdin.read()
    print('User,Host\\nroot,localhost' if 'mysql.user' in query else 'result\\n1')
else:
    sys.exit(2)
''' % sys.executable, encoding='utf-8')
        script.chmod(0o755)

    def command(self, action, *extra):
        return [sys.executable, str(self.script), action, '--root', str(self.root),
                '--port', str(self.web_port), *extra]

    def wait_for(self, predicate, timeout=15):
        until = time.monotonic()+timeout
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.1)
        self.fail('Timed out waiting for service state')

    def port_up(self, port):
        try:
            with socket.create_connection(('127.0.0.1',port), timeout=.2):
                return True
        except OSError:
            return False

    def start(self, script=None, python=None, env=None, ready=True):
        command = self.command('run', '--config', str(self.config), '--logs', str(self.logs))
        if script is not None:
            command[1] = str(script)
        if python is not None:
            command[0] = str(python)
        proc = subprocess.Popen(command,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True, env=env)
        self.addCleanup(lambda: self.stop_if_running(proc))
        if ready:
            try:
                self.wait_for(lambda: self.port_up(self.db_port) and self.port_up(self.web_port))
            except AssertionError:
                error = proc.stderr.read() if proc.poll() is not None else '<still running>'
                logs = {name: (self.logs/name).read_text(errors='replace')
                        for name in ('dolt.log', 'http.log') if (self.logs/name).exists()}
                self.fail('service did not become ready: stderr=%r logs=%r' % (error, logs))
        return proc

    @staticmethod
    def children(proc):
        output = subprocess.check_output(['ps', '--ppid', str(proc.pid), '-o', 'pid=,args='], text=True)
        return [(int(line.strip().split(None, 1)[0]), line.strip().split(None, 1)[1])
                for line in output.splitlines() if line.strip()]

    @staticmethod
    def stop_if_running(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=7)
            except subprocess.TimeoutExpired:
                proc.kill();proc.wait()
        if proc.stdout: proc.stdout.close()
        if proc.stderr: proc.stderr.close()

    def test_supervision_health_single_instance_and_sigterm(self):
        proc = self.start()
        health = subprocess.run(self.command('health'), capture_output=True, text=True)
        self.assertIn('db=up web=up backup=unknown', health.stdout)
        self.assertEqual(health.returncode, 1)  # no completed backup yet
        duplicate = subprocess.run(self.command('run', '--config', str(self.config),
                                                '--logs', str(self.logs)),
                                   capture_output=True, text=True, timeout=5)
        self.assertEqual(duplicate.returncode, 1)
        self.assertIn('already running', duplicate.stderr)
        start = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=7), 0, proc.stderr.read())
        self.assertLess(time.monotonic()-start, 7)
        self.wait_for(lambda: not self.port_up(self.db_port) and not self.port_up(self.web_port))

    def test_sigkill_parent_releases_children_and_allows_restart(self):
        proc = self.start()
        proc.kill();proc.wait(timeout=5)
        self.wait_for(lambda: not self.port_up(self.db_port) and not self.port_up(self.web_port))
        next_proc = self.start()
        self.assertIsNone(next_proc.poll())

    def test_reject_non_loopback_and_unknown_configuration(self):
        from office_service import service_config
        self.config.write_text('{"schema_version":1,"http_host":"0.0.0.0"}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'loopback'):
            service_config(self.config)
        self.config.write_text('{"schema_version":1,"extra":true}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            service_config(self.config)

    def test_prepare_keeps_bd_config_inside_the_runtime(self):
        """prepare must not write under the login user's HOME.

        bd 1.2.2 resolves its user config as ``$HOME/.config/bd/config.yaml`` and
        creates it on every command, consulting ``XDG_CONFIG_HOME`` only when that
        path already holds a file (``internal/config/yaml_config.go``
        ``UserConfigYamlPath``). The fake bd below mirrors that write, so this
        fails if prepare lets bd see the login user's HOME.
        """
        from office_service import prepare
        runtime = self.base/'prepare-runtime'
        user_home = self.base/'user-home'
        user_home.mkdir()
        calls = runtime/'bd-calls.log'

        def fake_install(root, asset_dir=None):
            binaries = root/'bin'
            binaries.mkdir(parents=True, exist_ok=True)
            bd = binaries/'bd'
            bd.write_text('''#!__PYTHON__
import os, sys
from pathlib import Path
with open(os.environ['FAKE_BD_LOG'], 'a') as stream:
    stream.write('HOME=%s ARGS=%s\\n' % (os.environ.get('HOME'), ' '.join(sys.argv[1:])))
home = os.environ.get('HOME')
target = Path(home) / '.config' / 'bd' / 'config.yaml'
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text('metrics:\\n  disabled: true\\n')
'''.replace('__PYTHON__', sys.executable), encoding='utf-8')
            bd.chmod(0o755)
            dolt = binaries/'dolt'
            dolt.write_text('#!__PYTHON__\nimport sys\nsys.exit(0)\n'.replace(
                '__PYTHON__', sys.executable), encoding='utf-8')
            dolt.chmod(0o755)

        def snapshot():
            return {str(path.relative_to(self.base)) for path in self.base.rglob('*')
                    if path.is_file()}

        with mock.patch.dict(os.environ, {'HOME': str(user_home),
                                          'FAKE_BD_LOG': str(calls)}), \
                mock.patch('office_service.admin.install_binaries', side_effect=fake_install):
            before = snapshot()
            prepare(runtime, self.free_port())
            outside = [name for name in snapshot()-before
                       if not name.startswith('prepare-runtime/')]

        self.assertEqual(outside, [], 'prepare wrote outside the runtime: %s' % outside)
        self.assertTrue((runtime/'home'/'.config'/'bd'/'config.yaml').is_file())
        log = calls.read_text(encoding='utf-8')
        self.assertIn('HOME=%s' % (runtime/'home'), log)
        self.assertNotIn(str(user_home), log)

    def test_existing_deployment_upgrade_keeps_bd_metrics_disabled(self):
        """An upgraded runtime must stay metrics-off without a runtime HOME config.

        A runtime prepared the old way keeps its metrics-off setting only in the
        login user's ``~/.config/bd/config.yaml`` and has no ``<root>/home``.
        ``prepare`` takes the existing-deployment early-return path, so it never
        runs ``bd metrics off`` and the runtime HOME has no config at all. The
        fake bd below mirrors bd 1.2.2 enabling metrics (writing
        ``disabled: false`` under ``$HOME``) unless ``BD_DISABLE_METRICS`` is set,
        so this fails if the environment handed to every bd child omits the flag.
        """
        from office_service import prepare
        from admin import environment, run_bd
        runtime = self.base/'upgrade-runtime'
        runtime.mkdir()
        db_port = self.free_port()
        (runtime/'deployment.private.json').write_text(json.dumps({
            'schema': 1, 'port': db_port, 'unit': 'office-foreground',
            'password': 'disposable-only'}), encoding='utf-8')
        (runtime/'server.json').write_text(json.dumps({
            'listener': {'host': '127.0.0.1', 'port': db_port}}), encoding='utf-8')
        (runtime/'projects'/'demo').mkdir(parents=True)
        user_home = self.base/'user-home'
        (user_home/'.config'/'bd').mkdir(parents=True)
        old_config = user_home/'.config'/'bd'/'config.yaml'
        old_config.write_text('metrics:\n  disabled: true\n', encoding='utf-8')
        calls = runtime/'bd-calls.log'

        def fake_install(root, asset_dir=None):
            binaries = root/'bin'
            binaries.mkdir(parents=True, exist_ok=True)
            bd = binaries/'bd'
            bd.write_text('''#!__PYTHON__
import os, sys
from pathlib import Path
with open(os.environ['FAKE_BD_LOG'], 'a') as stream:
    stream.write('HOME=%s BD_DISABLE_METRICS=%s ARGS=%s\\n' % (
        os.environ.get('HOME'), os.environ.get('BD_DISABLE_METRICS'),
        ' '.join(sys.argv[1:])))
if os.environ.get('BD_DISABLE_METRICS') != '1':
    target = Path(os.environ['HOME']) / '.config' / 'bd' / 'config.yaml'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('metrics:\\n  disabled: false\\n')
'''.replace('__PYTHON__', sys.executable), encoding='utf-8')
            bd.chmod(0o755)

        with mock.patch.dict(os.environ, {'HOME': str(user_home),
                                          'FAKE_BD_LOG': str(calls)}), \
                mock.patch('office_service.admin.install_binaries', side_effect=fake_install):
            prepare(runtime, db_port)  # existing deployment: early return
            run_bd(runtime, 'demo', ['metrics', 'status'])

        # The behavioural checks come first so a regression fails on what bd did,
        # not only on the missing key.
        log = calls.read_text(encoding='utf-8')
        self.assertIn('HOME=%s' % (runtime/'home'), log)
        self.assertIn('BD_DISABLE_METRICS=1', log)
        self.assertNotIn(str(user_home), log)
        self.assertEqual(old_config.read_text(encoding='utf-8'),
                         'metrics:\n  disabled: true\n')
        self.assertFalse((runtime/'home'/'.config'/'bd'/'config.yaml').exists())
        for candidate in (user_home, runtime/'home'):
            config = candidate/'.config'/'bd'/'config.yaml'
            if config.exists():
                self.assertNotIn('disabled: false', config.read_text(encoding='utf-8'))
        self.assertEqual(environment(runtime)['BD_DISABLE_METRICS'], '1')

    def test_existing_log_directory_is_left_undisturbed(self):
        self.logs.mkdir(mode=0o700)
        os.chmod(self.logs, 0o755)
        proc = self.start()
        self.assertIsNone(proc.poll())
        self.assertEqual(stat.S_IMODE(self.logs.stat().st_mode), 0o755)

    def test_newly_created_log_directory_is_tightened(self):
        self.assertFalse(self.logs.exists())
        self.start()
        self.assertEqual(stat.S_IMODE(self.logs.stat().st_mode), 0o700)

    def test_log_directory_ownership_and_denied_chmod_never_abort(self):
        # A directory genuinely owned by another account needs root or a second
        # account, so the ownership branch and the chmod failure are exercised
        # directly instead of through a foreign-owned fixture.
        from office_service import log_directory
        existing = self.base/'existing-logs'
        existing.mkdir(mode=0o700)
        os.chmod(existing, 0o755)
        with mock.patch.object(Path, 'chmod') as chmod:
            log_directory(existing)
        chmod.assert_not_called()
        self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o755)
        foreign = self.base/'fresh-foreign-logs'
        with mock.patch.object(Path, 'chmod') as chmod, \
                mock.patch('office_service.os.geteuid', return_value=os.geteuid()+1):
            log_directory(foreign)
        chmod.assert_not_called()
        self.assertTrue(foreign.is_dir())
        denied = self.base/'denied-chmod-logs'
        with mock.patch.object(Path, 'chmod', side_effect=OSError(13, 'Permission denied')):
            log_directory(denied)
        self.assertTrue(denied.is_dir())

    def test_children_use_pinned_release_paths(self):
        alias = self.base/'current'
        alias.symlink_to(self.script.parent, target_is_directory=True)
        python_alias = self.base/'python-current'
        python_alias.symlink_to(Path(sys.executable).resolve())
        proc = self.start(script=alias/'office_service.py', python=python_alias)
        children = self.children(proc)
        web = [args for _, args in children if 'http_service.py' in args]
        self.assertEqual(len(web), 1, children)
        self.assertIn(str(self.script.parent/'http_service.py'), web[0])
        self.assertIn(str(self.script.parent/'endpoint.py'), web[0])
        self.assertIn(os.path.realpath(sys.executable), web[0])
        self.assertNotIn(str(alias/'http_service.py'), web[0])
        self.assertNotIn(str(python_alias), web[0])

    def test_slow_web_child_gets_bounded_grace_and_nonzero_exit(self):
        proc = self.start()
        web = [pid for pid, args in self.children(proc) if 'http_service.py' in args]
        self.assertEqual(len(web), 1)
        os.kill(web[0], signal.SIGSTOP)
        start = time.monotonic()
        proc.terminate()
        self.assertEqual(proc.wait(timeout=7), 1)
        self.assertLess(time.monotonic()-start, 7)
        self.wait_for(lambda: not self.port_up(self.db_port) and not self.port_up(self.web_port))

    def test_sigterm_during_database_startup_exits_cleanly(self):
        env = os.environ.copy()
        env['FAKE_DOLT_READY_DELAY'] = '10'
        proc = self.start(env=env, ready=False)
        self.wait_for(lambda: (self.logs/'dolt.log').exists())
        proc.terminate()
        self.assertEqual(proc.wait(timeout=7), 0)


if __name__ == '__main__':
    unittest.main()
