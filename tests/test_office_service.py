"""Disposable Linux checks for the foreground office supervisor."""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


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

    def start(self, script=None, env=None, ready=True):
        command = self.command('run', '--config', str(self.config), '--logs', str(self.logs))
        if script is not None:
            command[1] = str(script)
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

    def test_children_use_pinned_release_paths(self):
        alias = self.base/'current'
        alias.symlink_to(self.script.parent, target_is_directory=True)
        proc = self.start(script=alias/'office_service.py')
        children = self.children(proc)
        web = [args for _, args in children if 'http_service.py' in args]
        self.assertEqual(len(web), 1, children)
        self.assertIn(str(self.script.parent/'http_service.py'), web[0])
        self.assertIn(str(self.script.parent/'endpoint.py'), web[0])
        self.assertNotIn(str(alias/'http_service.py'), web[0])

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
