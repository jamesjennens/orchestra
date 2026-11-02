"""kittrial-5bb.500: the backup and restore promises that the audit (kittrial-5bb.498) found untrue.

F1  ``restore-new --restore-http-state`` says every session is invalidated: now it really is, and
    ``--credentials keep|revoke`` (required, no default) says what happens to the credentials.
F2  a configured HTTP state path that is not a file is DEGRADED (incomplete, non-zero exit), not absent.
F3  ``--restore-http-state`` is refused while a web service runs: the office lock, and for a plain
    ``http_service.py`` (which holds no lock) a check of the running processes.
F4  a sidecar of another project is refused before anything is created; a half restore carries a marker,
    so ``backup --all`` records it incomplete and ``backup-status --require-complete`` fails.
F5  the JSON half alone is not called a pair.
F6  SIGTERM during the entry copy of the last-complete sidecar leaves no temporary.

The web service half runs a real Store, a real Service and a real HTTP server on loopback (and, for F3,
a real ``http_service.py`` process); the tracker half runs against the real pinned bd and a scratch Dolt
server (the harness of test_task_files_ops_real_bd). Nothing is mocked except the injected fault of F4/F6.
POSIX only where it needs fcntl, signals or /proc: no module-level POSIX-only import.
"""
import contextlib
import hashlib
import http.client
import io
import json
import os
import shutil
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
sys.path.insert(0, str(KIT / 'tests'))
import admin
import test_bd_label_aliases as rb
from test_merge_slot_real_bd import without_inherited_tests

ADMIN_NAME, ADMIN_PASSWORD = 'root-admin', 'correct-horse-battery-staple'
ALEX_PASSWORD, ALEX_NEW_PASSWORD = 'alex-password-1', 'alex-password-2'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Web:
    """A real Store + Service + loopback HTTP server over ``state_path`` (the in-process backend)."""

    def __init__(self, state_path):
        from http_auth import Service, Store
        from http_service import InProcessBackend, create_server
        self.store = Store(state_path)
        self.service = Service(self.store)
        self.httpd = create_server(self.service, InProcessBackend(self.service), host='127.0.0.1', port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, token=None):
        return http_request(self.port, method, path, body, token)


def http_request(port, method, path, body=None, token=None):
    client = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
    headers = {}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        client.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = client.getresponse()
        data = response.read()
    finally:
        client.close()
    try:
        parsed = json.loads(data) if data else None
    except ValueError:
        parsed = None
    return response.status, parsed


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def run_cli(root, *argv):
    """``admin.py --root ROOT ARGV`` in this process; (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, 'argv', ['admin.py', '--root', str(root)] + list(argv)), \
            mock.patch.object(admin, 'root_path', return_value=root), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            admin.run_main()
        except SystemExit as stopped:
            code = stopped.code if isinstance(stopped.code, int) else (1 if stopped.code else 0)
            if isinstance(stopped.code, str):
                err.write(stopped.code)
    return code, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------------------ the real-bd fixtures
@unittest.skipIf(rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@without_inherited_tests
class RealRuntimeCase(rb.RealBdLabelAliasTests):
    """A scratch runtime with a real Dolt server; every project it makes is backed up through backup init."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        config = cls.root / 'deployment.private.json'
        document = json.loads(config.read_text(encoding='utf-8'))
        document['operators'] = ['ops']
        config.write_text(json.dumps(document), encoding='utf-8')
        os.chmod(str(config), 0o600)
        (cls.root / 'backups').mkdir(exist_ok=True)
        admin.run_bd(cls.root, 'pp', ['backup', 'init', str(cls.root / 'backups' / 'pp')])
        # setUpClass is the one place the backup of the fixture's own project is taken
        with contextlib.redirect_stdout(io.StringIO()):
            admin.backup_projects(cls.root, ['pp'])

    def new_project(self, name):
        path = self.root / 'projects' / name
        if (path / '.beads' / 'metadata.json').is_file():
            return path                     # the runtime is shared by the class: made by an earlier test
        path.mkdir(parents=True, exist_ok=True)
        init = subprocess.run(
            [str(self.bd_path), 'init', '--server', '--external', '--server-host', '127.0.0.1',
             '--server-port', str(self.port), '--server-user', 'root', '--prefix', name,
             '--database', name, '--skip-agents', '--skip-hooks', '--non-interactive'],
            cwd=str(path), env=admin.environment(self.root), capture_output=True, text=True)
        self.assertEqual(init.returncode, 0, init.stderr or init.stdout)
        (self.root / 'backups').mkdir(exist_ok=True)
        admin.run_bd(self.root, name, ['backup', 'init', str(self.root / 'backups' / name)])
        return path

    def main(self, *argv):
        return run_cli(self.root, *argv)

    def backup(self, *names):
        code, out, err = self.main('backup', *names)
        self.assertEqual(0, code, err or out)
        return out

    def exists(self, name):
        return (self.root / 'projects' / name).exists()


class StateRestoreRealBdTests(RealRuntimeCase):
    """F1, F2, F3 and the verified.md items 1-3 against a real service and a real tracker."""

    def setUp(self):
        self.live = self.root / 'http-state.json'
        self.addCleanup(self.clean_state)
        self.clean_state()
        self.web = None
        self.addCleanup(self.stop_web)

    def clean_state(self):
        for name in ('http-state.json', 'http-state.json.records.sqlite3', 'http-state.json.lock',
                     'office-service.lock'):
            for folder in (self.root, self.root / 'backups'):
                with contextlib.suppress(FileNotFoundError):
                    (folder / name).unlink()
        with contextlib.suppress(FileNotFoundError):
            (self.root / 'backups' / admin.BACKUP_STATUS_NAME).unlink()
        for kept in self.root.glob('http-state.json.before-restore-*'):    # r2: the live pair a restore kept
            kept.unlink()
        config = self.root / 'deployment.private.json'
        document = json.loads(config.read_text(encoding='utf-8'))
        if document.pop('http_state', None) is not None:
            config.write_text(json.dumps(document), encoding='utf-8')

    def stop_web(self):
        if self.web is not None:
            self.web.stop()
            self.web = None

    # -- a signed-in office -----------------------------------------------------------------------
    def office(self):
        """Bootstrap, an account, a project; the live service. Returns (admin token, alex id)."""
        from http_auth import Service, Store
        Service.bootstrap_superuser(Store(self.live), ADMIN_NAME, ADMIN_PASSWORD)
        self.web = Web(self.live)
        administrator = self.login(ADMIN_NAME, ADMIN_PASSWORD)
        status, data = self.web.request('POST', '/v1/accounts', {'username': 'alex'}, administrator)
        self.assertEqual(201, status, data)
        alex_id = data['id']
        status, data = self.web.request('POST', '/v1/accounts/%s/password' % alex_id,
                                        {'new_password': ALEX_PASSWORD}, administrator)
        self.assertEqual(200, status, data)
        return administrator, alex_id

    def login(self, name, password, web=None):
        status, data = (web or self.web).request('POST', '/v1/sessions', {'username': name, 'password': password})
        self.assertEqual(201, status, data)
        return data['session']['token']

    def issue(self, token, project_id, label):
        status, data = self.web.request('POST', '/v1/projects/%s/worker-credentials' % project_id,
                                        {'label': label, 'actor': label}, token)
        self.assertEqual(201, status, data)
        return data['credential']['id'], data['credential']['secret']

    def drill(self, credentials, destination):
        """Back up a signed-in office, log out and revoke afterwards, stop the service, restore.

        Returns a dict of everything the assertions need."""
        self.new_project('hsr')
        administrator, alex_id = self.office()
        live_session = self.login('alex', ALEX_PASSWORD)
        loggedout_session = self.login('alex', ALEX_PASSWORD)
        status, data = self.web.request('POST', '/v1/projects', {'name': 'Alpha'}, live_session)
        self.assertEqual(201, status, data)
        project_id = data['id']
        revoked_id, revoked_secret = self.issue(live_session, project_id, 'later-revoked')
        _, kept_secret = self.issue(live_session, project_id, 'stays-live')
        # an unknown key a later kit writes into the state document, to see it come back whole
        self.web.store.state['handover_notes'] = {project_id: {'versions': [], 'dropped': 0}}
        self.web.store.save()
        self.backup('hsr')
        backed_up = json.loads((self.root / 'backups' / admin.HTTP_STATE_JSON).read_text(encoding='utf-8'))
        # after the backup: a sign-out, a revoke, a new password
        self.assertEqual(204, self.web.request('DELETE', '/v1/sessions/current', None, loggedout_session)[0])
        self.assertEqual(204, self.web.request('POST', '/v1/projects/%s/worker-credentials/%s/revoke'
                                               % (project_id, revoked_id), None, live_session)[0])
        status, data = self.web.request('POST', '/v1/accounts/%s/password' % alex_id,
                                        {'new_password': ALEX_NEW_PASSWORD}, administrator)
        self.assertEqual(200, status, data)
        self.assertEqual(401, self.web.request('GET', '/v1/sessions/current', None, loggedout_session)[0])
        self.assertEqual(401, self.web.request('GET', '/v1/projects/%s/tasks' % project_id, None,
                                               revoked_secret)[0])
        self.stop_web()
        code, out, err = self.main('restore-new', 'hsr', destination, '--restore-http-state', '--credentials', credentials)
        self.assertEqual(0, code, err or out)
        restored_text = self.live.read_text(encoding='utf-8')      # before anyone signs in again
        restored_mode = self.live.stat().st_mode & 0o777
        self.web = Web(self.live)          # a NEW Store and Service, loaded from the restored file
        return dict(restored_text=restored_text, restored_mode=restored_mode, project_id=project_id, live_session=live_session, loggedout_session=loggedout_session,
                    revoked_secret=revoked_secret, kept_secret=kept_secret, out=out, backed_up=backed_up)

    def tasks(self, project_id, secret):
        return self.web.request('GET', '/v1/projects/%s/tasks' % project_id, None, secret)[0]

    # -- F1 ------------------------------------------------------------------------------------------
    def test_restore_really_revokes_sessions_and_credentials_with_revoke(self):
        d = self.drill('revoke', 'hsrrev')
        # every session is dead: the signed-out one AND the one that was live at the backup
        self.assertEqual(401, self.web.request('GET', '/v1/sessions/current', None, d['loggedout_session'])[0])
        self.assertEqual(401, self.web.request('GET', '/v1/sessions/current', None, d['live_session'])[0])
        # every credential is dead, the one revoked since and the one that was live
        self.assertEqual(401, self.tasks(d['project_id'], d['revoked_secret']))
        self.assertEqual(401, self.tasks(d['project_id'], d['kept_secret']))
        # the password is back as of the backup: the old one signs in, the new one does not
        self.login('alex', ALEX_PASSWORD)
        self.assertEqual(401, self.web.request('POST', '/v1/sessions',
                                               {'username': 'alex', 'password': ALEX_NEW_PASSWORD})[0])
        self.assertIn('Every session in it was revoked', d['out'])
        self.assertIn('Every worker and agent credential in it was revoked (2)', d['out'])
        self.assertIn('Passwords changed since', d['out'])
        # the document was edited, not replaced by something else: users and the unknown key come back whole
        restored = json.loads(d['restored_text'])
        self.assertEqual(d['backed_up']['users'], restored['users'])
        self.assertEqual(d['backed_up']['handover_notes'], restored['handover_notes'])
        self.assertEqual(sorted(d['backed_up']), sorted(restored))
        self.assertTrue(all(s['revoked'] for s in restored['sessions'].values()))
        self.assertTrue(all(c['revoked'] for c in restored['credentials'].values()))
        if os.name == 'posix':
            self.assertEqual('0o600', oct(d['restored_mode']))

    def test_restore_really_revokes_sessions_and_keeps_credentials_with_keep(self):
        d = self.drill('keep', 'hsrkeep')
        self.assertEqual(401, self.web.request('GET', '/v1/sessions/current', None, d['loggedout_session'])[0])
        self.assertEqual(401, self.web.request('GET', '/v1/sessions/current', None, d['live_session'])[0])
        # keep: both credentials are back as the backup had them, the one revoked since is live again
        self.assertEqual(200, self.tasks(d['project_id'], d['revoked_secret']))
        self.assertEqual(200, self.tasks(d['project_id'], d['kept_secret']))
        self.login('alex', ALEX_PASSWORD)
        self.assertIn('Every session in it was revoked', d['out'])
        self.assertIn('Worker and agent credentials revoked since', d['out'])
        self.assertIn('are live again', d['out'])
        # the backup time and its source are named, with an age
        mtime = (self.root / 'backups' / admin.HTTP_STATE_JSON).stat().st_mtime
        generated = json.loads((self.root / 'backups' / admin.BACKUP_STATUS_NAME).read_text(
            encoding='utf-8'))['generated_at']
        self.assertIn('from %s UTC' % min(generated, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(mtime))),
                      d['out'])
        self.assertIn(' old; ', d['out'])

    def test_the_credentials_choice_is_required_and_a_refusal_creates_nothing(self):
        self.new_project('hsq')
        self.office()
        self.backup('hsq')
        self.stop_web()
        before = sha(self.live)
        for argv in (('restore-new', 'hsq', 'hsq2', '--restore-http-state'),
                     ('restore-new', 'hsq', 'hsq2', '--credentials', 'keep'),
                     ('restore-new', 'hsq', 'hsq2', '--restore-http-state', '--credentials', 'both')):
            code, out, err = self.main(*argv)
            self.assertNotEqual(0, code, argv)
            self.assertFalse(self.exists('hsq2'), argv)
            self.assertEqual(before, sha(self.live), argv)
        code, out, err = self.main('restore-new', 'hsq', 'hsq2', '--restore-http-state')
        self.assertIn('needs --credentials keep or --credentials revoke', err)
        code, out, err = self.main('restore-new', 'hsq', 'hsq2', '--credentials', 'keep')
        self.assertIn('--credentials is only for --restore-http-state', err)
        self.assertFalse((self.root / 'office-service.lock').exists(), 'refused before the lock is touched')

    def test_a_missing_or_unusable_state_pair_refuses_before_the_destination_exists(self):
        self.new_project('hsu')
        self.backup('hsu')                       # no state exists: no pair in backups/
        self.assertFalse((self.root / 'backups' / admin.HTTP_STATE_JSON).exists())
        code, out, err = self.main('restore-new', 'hsu', 'hsu2', '--restore-http-state', '--credentials', 'keep')
        self.assertNotEqual(0, code)
        self.assertIn('no HTTP state pair', err)
        self.assertFalse(self.exists('hsu2'))
        (self.root / 'backups' / admin.HTTP_STATE_JSON).write_text('{"schema_version": 1, "sessions": [1]}',
                                                                  encoding='utf-8')
        code, out, err = self.main('restore-new', 'hsu', 'hsu3', '--restore-http-state', '--credentials', 'keep')
        self.assertNotEqual(0, code)
        self.assertIn('not a map of records', err)
        self.assertFalse(self.exists('hsu3'))
        (self.root / 'backups' / admin.HTTP_STATE_JSON).write_text('not json', encoding='utf-8')
        code, out, err = self.main('restore-new', 'hsu', 'hsu4', '--restore-http-state', '--credentials', 'keep')
        self.assertNotEqual(0, code)
        self.assertIn('not valid JSON', err)
        self.assertFalse(self.exists('hsu4'))

    # -- F2 -------------------------------------------------------------------------------------------
    def stale_copy_and_degraded_run(self, *argv):
        """A real backup of a real state, then the live file moved away; the run that follows."""
        self.new_project('hsm')
        from http_auth import Service, Store
        Service.bootstrap_superuser(Store(self.live), ADMIN_NAME, ADMIN_PASSWORD)
        self.backup('hsm')
        stale = self.root / 'backups' / admin.HTTP_STATE_JSON
        self.assertTrue(stale.is_file())
        os.utime(str(stale), (time.time() - 7200, time.time() - 7200))
        stale_text = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(stale.stat().st_mtime))
        moved = self.root / 'moved' / 'http-state.json'
        self.live.rename(self.root / 'http-state.gone')
        self.addCleanup(lambda: (self.root / 'http-state.gone').exists() and (self.root / 'http-state.gone').unlink())
        return stale_text, moved, self.main('backup', 'hsm', *argv)

    def read_status(self):
        return json.loads((self.root / 'backups' / admin.BACKUP_STATUS_NAME).read_text(encoding='utf-8'))

    def test_a_named_state_path_that_is_missing_is_degraded_and_exits_non_zero(self):
        stale_text, moved, (code, out, err) = self.stale_copy_and_degraded_run('--http-state', str(self.root / 'moved' / 'x.json'))
        self.assertNotEqual(0, code, out + err)
        status = self.read_status()
        self.assertEqual('incomplete', status['status'])
        self.assertEqual('degraded', status['http_state']['status'])
        reason = status['http_state']['reason']
        self.assertIn(str(self.root / 'moved' / 'x.json'), reason)
        self.assertIn('named with --http-state', reason)
        self.assertIn('is missing', reason)
        self.assertIn('stale', reason)
        self.assertIn(stale_text, reason)
        self.assertIn('http-state degraded', out + err)

    def test_a_deployment_state_path_that_is_missing_is_degraded_and_exits_non_zero(self):
        self.new_project('hsm')
        config = self.root / 'deployment.private.json'
        document = json.loads(config.read_text(encoding='utf-8'))
        document['http_state'] = str(self.root / 'moved' / 'http-state.json')
        config.write_text(json.dumps(document), encoding='utf-8')
        code, out, err = self.main('backup', 'hsm')
        self.assertNotEqual(0, code, out + err)
        status = self.read_status()
        self.assertEqual('incomplete', status['status'])
        self.assertEqual('degraded', status['http_state']['status'])
        self.assertIn('named by http_state in deployment.private.json', status['http_state']['reason'])
        self.assertIn('there is no earlier copy', status['http_state']['reason'])

    def test_with_no_flag_no_key_and_no_default_file_the_state_stays_absent_and_the_run_clean(self):
        self.new_project('hsm')
        code, out, err = self.main('backup', 'hsm')
        self.assertEqual(0, code, out + err)
        status = self.read_status()
        self.assertEqual({'status': 'absent'}, status['http_state'])
        self.assertEqual('complete', status['status'])

    # -- F3 -------------------------------------------------------------------------------------------
    def a_backed_up_office(self, name):
        self.new_project(name)
        self.office()
        self.backup(name)
        self.stop_web()

    def test_a_held_office_lock_refuses_the_restore_and_creates_nothing(self):
        import fcntl
        self.a_backed_up_office('hsl')
        before = sha(self.live)
        supervisor = os.open(str(self.root / 'office-service.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(supervisor, fcntl.LOCK_EX | fcntl.LOCK_NB)      # as office_service.py run holds it
            code, out, err = self.main('restore-new', 'hsl', 'hsl2', '--restore-http-state', '--credentials', 'keep')
        finally:
            os.close(supervisor)
        self.assertNotEqual(0, code)
        self.assertIn('Stop the service first', err)
        self.assertIn('office-service.lock', err)
        self.assertFalse(self.exists('hsl2'))
        self.assertEqual(before, sha(self.live))
        # control: the lock free, the same command restores
        code, out, err = self.main('restore-new', 'hsl', 'hsl2', '--restore-http-state', '--credentials', 'keep')
        self.assertEqual(0, code, err or out)
        self.assertTrue(self.exists('hsl2'))

    def test_a_plain_http_service_holds_no_office_lock_and_is_refused_by_its_process(self):
        """The audit's service was a plain ``http_service.py``: it holds no lock, so the lock cannot see it."""
        self.a_backed_up_office('hsp')
        port = free_port()
        log = open(str(self.root / 'plain-service.log'), 'ab')
        service = subprocess.Popen([sys.executable, str(KIT / 'http_service.py'), '--state', str(self.live),
                                    '--port', str(port), '--backend', 'inprocess'],
                                   cwd=str(self.root), stdout=log, stderr=log)
        self.addCleanup(log.close)
        try:
            for _ in range(100):
                try:
                    if http_request(port, 'GET', '/healthz')[0] == 200:
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                self.fail('the plain http_service.py did not start; see %s' % (self.root / 'plain-service.log'))
            before = sha(self.live)         # a starting service may rewrite the file: judged from here
            # 1. the fact the audit's reproduction hit: this process holds NO office lock
            import http_service
            descriptor = http_service.runtime_service_lock(self.root)
            self.assertIsNotNone(descriptor, 'a plain http_service.py does not hold office-service.lock')
            os.close(descriptor)
            # 2. so the restore is refused by looking for the process
            code, out, err = self.main('restore-new', 'hsp', 'hsp2', '--restore-http-state', '--credentials', 'keep')
            self.assertNotEqual(0, code)
            self.assertIn('an http_service.py process (pid %d)' % service.pid, err)
            self.assertIn('Stop the service first', err)
            self.assertFalse(self.exists('hsp2'))
            self.assertEqual(before, sha(self.live))
        finally:
            service.terminate()
            try:
                service.wait(timeout=20)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait(timeout=20)
        # control: the process gone, the same command restores
        code, out, err = self.main('restore-new', 'hsp', 'hsp2', '--restore-http-state', '--credentials', 'revoke')
        self.assertEqual(0, code, err or out)
        self.assertTrue(self.exists('hsp2'))

    def test_a_service_that_serves_another_state_does_not_block_the_restore(self):
        self.a_backed_up_office('hso')
        port = free_port()
        other = self.root / 'other-state.json'
        service = subprocess.Popen([sys.executable, str(KIT / 'http_service.py'), '--state', str(other),
                                    '--port', str(port), '--backend', 'inprocess'],
                                   cwd=str(self.root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    if http_request(port, 'GET', '/healthz')[0] == 200:
                        break
                except OSError:
                    time.sleep(0.2)
            code, out, err = self.main('restore-new', 'hso', 'hso2', '--restore-http-state', '--credentials', 'keep')
            self.assertEqual(0, code, err or out)
        finally:
            service.terminate()
            service.wait(timeout=20)
            with contextlib.suppress(FileNotFoundError):
                other.unlink()

    # -- r2 (review of r1, request 01a1993f) ----------------------------------------------------------
    def bot(self, administrator):
        status, data = self.web.request('POST', '/v1/agents', {'name': 'bot'}, administrator)
        self.assertEqual(201, status, data)
        agent = data.get('agent')
        agent_id = agent if isinstance(agent, str) else (agent or {}).get('id') or data.get('id')
        return agent_id, data['credential']['secret']

    def plain_service(self, command, cwd, label):
        port = free_port()
        log = open(str(self.root / ('%s.log' % label)), 'ab')
        self.addCleanup(log.close)
        service = subprocess.Popen([part.replace('PORT', str(port)) for part in command], cwd=cwd, stdout=log, stderr=log)
        self.addCleanup(self.stop_process, service)
        for _ in range(150):
            try:
                if http_request(port, 'GET', '/healthz')[0] == 200:
                    return service, port
            except OSError:
                pass
            time.sleep(0.2)
        self.fail('%s did not start; see %s' % (label, self.root / ('%s.log' % label)))

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=20)

    def test_r3_accounts_and_agents_disabled_since_the_backup_are_named_and_the_backups_values_stand(self):
        """Review R3 (test_p1): nothing is merged, and the restore names what comes back enabled."""
        self.new_project('hr3')
        administrator, alex_id = self.office()
        agent_id, _ = self.bot(administrator)
        self.backup('hr3')
        self.assertEqual(200, self.web.request('POST', '/v1/accounts/%s/disable' % alex_id, {}, administrator)[0])
        self.assertEqual(200, self.web.request('POST', '/v1/agents/%s/disable' % agent_id, {}, administrator)[0])
        self.stop_web()
        code, out, err = self.main('restore-new', 'hr3', 'hr3d', '--restore-http-state', '--credentials', 'keep')
        self.assertEqual(0, code, err or out)
        self.assertIn('Accounts disabled since the backup are ENABLED again by this restore (1): alex.', out)
        self.assertIn('Agents disabled since the backup are ENABLED again by this restore (1): bot.', out)
        self.web = Web(self.live)
        self.login('alex', ALEX_PASSWORD)                                   # the backup's value stands, as said

    def test_r4_the_live_pair_is_kept_aside_and_what_was_created_since_is_named(self):
        """Review R4 (test_p2): the live pair is copied aside 0600 before the replace, and the restore says so."""
        self.new_project('hr4')
        administrator, _ = self.office()
        self.backup('hr4')
        status, data = self.web.request('POST', '/v1/accounts', {'username': 'newbie'}, administrator)
        self.assertEqual(201, status, data)
        newbie_id = data['id']
        self.bot(administrator)
        self.stop_web()
        before = self.live.read_bytes()
        code, out, err = self.main('restore-new', 'hr4', 'hr4d', '--restore-http-state', '--credentials', 'keep')
        self.assertEqual(0, code, err or out)
        self.assertIn('Accounts created since the backup are not in the restored state and are gone from it (1): newbie.', out)
        self.assertIn('Agents created since the backup are not in the restored state and are gone from it (1): bot.', out)
        kept = sorted(self.root.glob('http-state.json.before-restore-*'))
        self.assertEqual(2, len(kept), kept)                                # the JSON and its records file
        json_copy = [path for path in kept if not path.name.endswith('.records.sqlite3')][0]
        self.assertEqual(before, json_copy.read_bytes())
        self.assertIn(newbie_id, json_copy.read_text(encoding='utf-8'))
        for path in kept:
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertIn(str(path), out)
        self.assertNotIn(newbie_id, self.live.read_text(encoding='utf-8'))

    def test_r1_a_service_started_during_the_restore_is_found_before_the_write(self):
        """Review R1 (test_p3): the process check runs again immediately before the state is written."""
        self.new_project('hr1')
        self.office()
        self.backup('hr1')
        self.stop_web()
        started = {}
        original = admin.native_restore

        def a_service_starts_meanwhile(root, source, destination):
            started['service'] = self.plain_service(
                [sys.executable, str(KIT / 'http_service.py'), '--state', str(self.live), '--port', 'PORT',
                 '--backend', 'inprocess'], str(self.root), 'r1-service')
            started['sha'] = sha(self.live)                                 # a starting service may write it
            return original(root, source, destination)
        with mock.patch.object(admin, 'native_restore', side_effect=a_service_starts_meanwhile):
            code, out, err = self.main('restore-new', 'hr1', 'hr1d', '--restore-http-state', '--credentials', 'revoke')
        service, _ = started['service']
        self.assertNotEqual(0, code)
        self.assertIn('an http_service.py process (pid %d)' % service.pid, err)
        self.assertIn('The live HTTP state was not changed.', err)
        self.assertIn('To restore the HTTP state, run restore-new hr1 NEWNAME --restore-http-state --credentials '
                      'revoke into a NEW name; the clone hr1d can then be retired', err)   # review R7, r3 N1
        self.assertNotIn('run restore-new again', err)                    # the clone exists: never the same name
        self.assertNotIn('..', err)
        self.assertEqual(started['sha'], sha(self.live))
        self.assertEqual([], sorted(p.name for p in self.root.glob('.http-state-restore-*')))

    def test_r2_a_service_run_as_a_module_or_behind_interpreter_options_is_seen(self):
        """Review R2 (test_p4)."""
        self.a_backed_up_office('hr2')
        shapes = {
            'module': ([sys.executable, '-m', 'http_service', '--state', str(self.live), '--port', 'PORT',
                        '--backend', 'inprocess'], str(KIT)),
            'options': ([sys.executable, '-X', 'utf8', '-X', 'faulthandler', '-W', 'ignore', str(KIT / 'http_service.py'),
                         '--state', str(self.live), '--port', 'PORT', '--backend', 'inprocess'], str(self.root)),
        }
        for label, (command, cwd) in shapes.items():
            with self.subTest(shape=label):
                service, _ = self.plain_service(command, cwd, 'r2-' + label)
                self.assertIn(service.pid, admin.http_service_processes(self.root, self.live))
                if label == 'module':
                    before = sha(self.live)
                    code, out, err = self.main('restore-new', 'hr2', 'hr2d', '--restore-http-state', '--credentials', 'keep')
                    self.assertNotEqual(0, code)
                    self.assertIn('an http_service.py process (pid %d)' % service.pid, err)
                    self.assertFalse(self.exists('hr2d'))
                    self.assertEqual(before, sha(self.live))
                self.stop_process(service)
    # -- r3 (review of r2, request 01a19984) ----------------------------------------------------------
    def test_n1_the_way_out_the_failure_names_is_the_one_that_works(self):
        """Review N1 (Q7): the state stage fails after the project restored; the path the message names is run."""
        self.new_project('hn1')
        admin.run_bd(self.root, 'hn1', ['create', 'an issue the clone will hold'])
        self.office()
        self.backup('hn1')
        self.stop_web()
        failing = ValueError('the state could not be written: No space left on device')   # a kit refusal: exit 1
        with mock.patch.object(admin, '_write_restored_http_state', side_effect=failing):
            code, out, err = self.main('restore-new', 'hn1', 'hn1d', '--restore-http-state', '--credentials', 'keep')
        self.assertNotEqual(0, code)
        self.assertIn('The project hn1d was restored, but the HTTP state was NOT restored', err)
        self.assertIn('run restore-new hn1 NEWNAME --restore-http-state --credentials keep into a NEW name', err)
        self.assertIn('retire-project hn1d --actor OPERATOR --reason REASON, with --force when it holds issues', err)
        self.assertIn('its name stays reserved', err)
        self.assertIn('is kept as', err)                                   # the copies of the live pair are named
        self.assertNotIn('run this restore-new again', err)
        # the way out, as the message says it
        code, out, err = self.main('restore-new', 'hn1', 'hn1e', '--restore-http-state', '--credentials', 'keep')
        self.assertEqual(0, code, err or out)
        code, out, err = self.main('retire-project', 'hn1d', '--actor', 'ops', '--reason', 'state restore failed')
        self.assertNotEqual(0, code)                                       # it holds an issue: --force is needed
        self.assertIn('--force', out + err)
        code, out, err = self.main('retire-project', 'hn1d', '--actor', 'ops', '--reason', 'state restore failed', '--force')
        self.assertEqual(0, code, err or out)
        code, out, err = self.main('restore-new', 'hn1', 'hn1d', '--restore-http-state', '--credentials', 'keep')
        self.assertNotEqual(0, code)                                       # the name stays reserved, as said
        self.assertIn('retired', out + err)

    def test_n4_a_stop_between_the_halves_is_said_in_the_notice(self):
        """Review N4 (Q6b): the JSON restored, the records file as it was, both copies beside the state."""
        self.a_backed_up_office('hn4')
        records_live = Path(str(self.live) + '.records.sqlite3')
        records_before = sha(records_live)
        real = shutil.copyfile

        def stop_at_the_records(source, destination, *a, **k):
            if str(source) == str(self.root / 'backups' / admin.HTTP_STATE_RECORDS):
                raise admin.TerminatedBySignal(signal.SIGTERM)          # what the guard turns a SIGTERM into
            return real(source, destination, *a, **k)
        with mock.patch('shutil.copyfile', side_effect=stop_at_the_records):
            code, out, err = self.main('restore-new', 'hn4', 'hn4d', '--restore-http-state', '--credentials', 'keep')
        self.assertNotEqual(0, code)
        self.assertIn('the HTTP state was only HALF restored: its JSON %s is the backup\'s, its records file was left '
                      'as it was' % self.live, err)
        self.assertEqual(records_before, sha(records_live))
        kept = sorted(self.root.glob('http-state.json.before-restore-*'))
        self.assertEqual(2, len(kept), kept)
        for path in kept:
            self.assertIn(str(path), err)

    def test_n3_a_set_recorded_degraded_is_restored_with_its_reason_first_and_not_called_a_pair(self):
        """Review N3 (Q13), through the command."""
        self.a_backed_up_office('hn3')                                     # a complete pair in backups/ first
        named = self.root / 'named-state.json'
        shutil.copy(str(self.live), str(named))                            # no records file beside it
        config = self.root / 'deployment.private.json'
        document = json.loads(config.read_text(encoding='utf-8'))
        document['http_state'] = str(named)
        config.write_text(json.dumps(document), encoding='utf-8')
        self.addCleanup(lambda: named.unlink() if named.exists() else None)
        code, out, err = self.main('backup', 'hn3')
        self.assertNotEqual(0, code)
        self.assertEqual('degraded', self.read_status()['http_state']['status'])
        code, out, err = self.main('restore-new', 'hn3', 'hn3d', '--restore-http-state', '--credentials', 'keep')
        self.assertEqual(0, code, err or out)                              # not refused: it may be the only set
        state_text = out[out.index('WARNING: the backup run'):]
        self.assertTrue(state_text.startswith('WARNING: the backup run that made this set recorded the HTTP state '
                                              'DEGRADED: the HTTP state %s has no records file beside it' % named))
        self.assertIn('is from an earlier run', state_text.splitlines()[0])
        self.assertNotIn('HTTP state pair', out)
        self.assertIn('Restored the HTTP state files from a backup set recorded DEGRADED', out)

# --------------------------------------------------------------------- F5 and the time, no tracker needed
class StateFilesTests(unittest.TestCase):
    """The pair on disk: F5, and the backup time of verified.md item 1."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'backups').mkdir()

    def state_pair(self, records=True):
        from http_auth import Service, Store
        live = self.root / 'http-state.json'
        Service.bootstrap_superuser(Store(live), ADMIN_NAME, ADMIN_PASSWORD)
        store = Store(live)
        store.records.put('result', 'k', {'result': 1}, ttl=3600)
        admin.backup_http_state(self.root)
        if not records:
            (self.root / 'backups' / admin.HTTP_STATE_RECORDS).unlink()
        return live

    def test_the_json_half_alone_is_not_called_a_pair(self):
        live = self.state_pair(records=False)
        records = Path(str(live) + '.records.sqlite3')
        self.assertTrue(records.is_file())
        before = sha(records)
        text = admin.restore_http_state(self.root, credentials='keep')
        self.assertIn('Restored the HTTP state JSON half only (http-state.json)', text)
        self.assertNotIn('HTTP state pair', text)
        self.assertIn('The records file was not in this backup set; the live records file %s was left as it was.'
                      % records, text)
        self.assertEqual(before, sha(records))

    def test_the_json_half_alone_with_no_live_records_file_says_there_is_none(self):
        live = self.state_pair(records=False)
        records = Path(str(live) + '.records.sqlite3')
        records.unlink()
        text = admin.restore_http_state(self.root, credentials='revoke')
        self.assertIn('there is no live records file (%s does not exist)' % records, text)
        self.assertFalse(records.exists())

    def test_the_whole_pair_says_pair(self):
        self.state_pair()
        text = admin.restore_http_state(self.root, credentials='keep')
        self.assertIn('Restored the HTTP state pair (http-state.json, http-state.json.records.sqlite3)', text)
        self.assertNotIn('records file was not in this backup set', text)

    def status_file(self, generated_epoch):
        stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(generated_epoch))
        admin.write_backup_status(self.root, admin.backup_status_record(
            [{'name': 'alpha', 'status': 'complete', 'completed_at': stamp}], 'named', stamp))
        return stamp

    def stamped(self, epoch):
        os.utime(str(self.root / 'backups' / admin.HTTP_STATE_JSON), (epoch, epoch))
        return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch))

    def test_the_older_of_the_file_time_and_the_run_time_is_the_backup_time(self):
        self.state_pair()
        now = time.time()
        # a copy that lost its timestamps: the file looks NEW, the run that made it was two hours ago
        generated = self.status_file(now - 7200)
        self.stamped(now)
        when, how, disagreement = admin.http_state_backup_time(self.root)
        self.assertEqual(generated, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(when)))
        self.assertIn('generated_at in backups/backup-status.json', how)
        self.assertIsNotNone(disagreement)
        self.assertIn('differ by 2 h 00 min', disagreement)
        text = admin.restore_http_state(self.root, credentials='keep', now=now)
        self.assertIn('from %s UTC (2 h 00 min old; generated_at in backups/backup-status.json)' % generated, text)
        self.assertIn('The two times differ by', text)

    def test_when_the_file_is_the_older_it_is_the_one_named(self):
        self.state_pair()
        now = time.time()
        self.status_file(now - 60)
        file_stamp = self.stamped(now - 3 * 3600)
        when, how, disagreement = admin.http_state_backup_time(self.root)
        self.assertEqual(file_stamp, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(when)))
        self.assertEqual('the file time of backups/http-state.json', how)
        self.assertIsNotNone(disagreement)

    def test_times_within_a_minute_do_not_disagree_and_no_status_file_uses_the_file_time(self):
        self.state_pair()
        now = time.time()
        self.status_file(now - 100)
        self.stamped(now - 100 + 30)
        self.assertIsNone(admin.http_state_backup_time(self.root)[2])
        (self.root / 'backups' / admin.BACKUP_STATUS_NAME).unlink()
        when, how, disagreement = admin.http_state_backup_time(self.root)
        self.assertEqual('the file time of backups/http-state.json', how)
        self.assertIsNone(disagreement)

    def test_a_later_run_with_a_degraded_http_state_is_recorded_incomplete_not_a_crash(self):
        # The merge with the previous status file used to ignore the HTTP state: the record came out
        # 'complete' beside a degraded http_state, validate_backup_status refused it, and the run died
        # with that ValueError, leaving the previous status file standing.
        (self.root / 'projects' / 'alpha' / '.beads').mkdir(parents=True)
        (self.root / 'projects' / 'alpha' / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        stamp = self.status_file(time.time() - 100)
        results = [{'name': 'alpha', 'status': 'complete', 'completed_at': stamp}]
        degraded = {'status': 'degraded', 'reason': 'the HTTP state file is missing'}
        merged = admin.merged_backup_status(
            self.root, admin.backup_status_record(results, 'named', stamp, http_state=degraded))
        self.assertEqual('incomplete', merged['status'])
        admin.write_backup_status(self.root, merged)
        clean = admin.merged_backup_status(
            self.root, admin.backup_status_record(results, 'named', stamp, http_state={'status': 'absent'}))
        self.assertEqual('complete', clean['status'])

    def test_a_nested_or_damaged_state_document_is_refused_and_nothing_is_written(self):
        live = self.state_pair()
        before = sha(live)
        backed = self.root / 'backups' / admin.HTTP_STATE_JSON
        for text, wanted in (('{"schema_version": 1, "x": ' + '[' * 200 + ']' * 200 + '}', 'not valid JSON'),
                             ('{"schema_version": 99}', 'not a web service state document'),
                             ('[1]', 'not a web service state document')):
            backed.write_text(text, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, wanted):
                admin.restore_http_state(self.root, credentials='keep')
            self.assertEqual(before, sha(live))

    def test_unused_reset_values_are_revoked_with_the_sessions(self):
        live = self.state_pair()
        document = json.loads((self.root / 'backups' / admin.HTTP_STATE_JSON).read_text(encoding='utf-8'))
        document['reset_tokens'] = {'a': {'user_id': 'u', 'used': False}, 'b': {'user_id': 'u', 'used': True}}
        document['sessions'] = {'s': {'revoked': False}}
        (self.root / 'backups' / admin.HTTP_STATE_JSON).write_text(json.dumps(document), encoding='utf-8')
        text = admin.restore_http_state(self.root, credentials='keep')
        restored = json.loads(live.read_text(encoding='utf-8'))
        self.assertTrue(restored['reset_tokens']['a']['used'])
        self.assertTrue(restored['sessions']['s']['revoked'])
        self.assertIn('Unused password-reset values in it were revoked (1)', text)


    def test_r5_a_named_state_with_no_records_file_beside_it_is_degraded(self):
        """Review R5 (test_p10): the pair is what is promised; the stale records copy is named."""
        self.state_pair()                                                   # a full pair in backups/ first
        lone = self.root / 'lone.json'
        shutil.copy(str(self.root / 'http-state.json'), str(lone))
        entry = admin.backup_http_state(self.root, named=str(lone))
        self.assertEqual('degraded', entry['status'])
        self.assertIn('has no records file beside it', entry['reason'])
        self.assertIn('is from an earlier run', entry['reason'])
        self.assertEqual('complete', admin.backup_http_state(self.root)['status'])     # the default: as before

    SCRIPT_R6 = '''
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import admin
real = os.fsync
state = {'sent': False}
def fsync(fd):
    if not state['sent']:
        state['sent'] = True
        os.kill(os.getpid(), signal.SIGTERM)
    return real(fd)
admin.os.fsync = fsync
print(admin.restore_http_state(Path(sys.argv[2]), credentials='keep'))
'''

    def test_r6_a_stop_during_the_state_write_leaves_no_temporary_and_the_live_state_as_it_was(self):
        """Review R6 (test_p6)."""
        live = self.state_pair()
        before = sha(live)
        script = self.root / 'stop_during_state_write.py'
        script.write_text(self.SCRIPT_R6, encoding='utf-8')
        done = subprocess.run([sys.executable, str(script), str(KIT), str(self.root)], capture_output=True, text=True,
                              timeout=120, cwd=str(self.root))
        self.assertNotEqual(-signal.SIGTERM, done.returncode, 'the stop took the default action')
        self.assertNotEqual(0, done.returncode)
        self.assertEqual([], sorted(p.name for p in self.root.glob('.http-state-*')))
        self.assertEqual(before, sha(live))

    def test_n2_with_the_live_json_gone_the_records_file_is_still_kept_and_said_replaced(self):
        """Review N2 (Q5)."""
        live = self.state_pair()
        from http_auth import Store
        Store(live).records.put('result', 'after-the-backup', {'result': 2}, ttl=3600)
        records_live = Path(str(live) + '.records.sqlite3')
        records_before = records_live.read_bytes()
        live.unlink()
        out = admin.restore_http_state(self.root, credentials='keep')
        kept = sorted(self.root.glob('http-state.json.before-restore-*'))
        self.assertEqual(1, len(kept), kept)
        self.assertTrue(kept[0].name.endswith('.records.sqlite3'))
        self.assertEqual(0o600, kept[0].stat().st_mode & 0o777)
        import sqlite3
        holder = sqlite3.connect(str(kept[0]))
        try:
            dump_kept = '\n'.join(holder.iterdump())
        finally:
            holder.close()
        self.assertIn('after-the-backup', dump_kept)
        self.assertNotEqual(records_before, records_live.read_bytes())     # the live one was replaced
        self.assertIn('The live records file %s WAS there and is replaced' % records_live, out)
        self.assertIn(str(kept[0]), out)

# ------------------------------------------------------------------------------------------------ F6
@unittest.skipUnless(os.name == 'posix', 'SIGTERM semantics are POSIX')
class SigtermTemporaryTests(unittest.TestCase):
    """SIGTERM while backup copies the last-complete sidecar leaves no ``*.restore`` temporary."""

    SCRIPT = '''
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import admin
real = os.fsync
state = {'sent': False}
def fsync(fd):
    if not state['sent']:
        state['sent'] = True
        os.kill(os.getpid(), signal.SIGTERM)      # the stop arrives while the first copy is being written
    return real(fd)
admin.os.fsync = fsync
sys.argv = ['admin.py', '--root', sys.argv[2], 'backup', 'alpha']
admin.run_main()
'''

    def test_a_stop_during_the_sidecar_copy_leaves_no_temporary_and_the_pair_intact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'rt'
            (root / 'projects' / 'alpha' / '.beads').mkdir(parents=True)
            (root / 'backups' / 'alpha').mkdir(parents=True)
            (root / 'projects' / 'alpha' / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
            sidecar = root / 'backups' / 'alpha.coordination.json'
            sidecar.write_text(json.dumps({'schema_version': 1, 'status': 'complete', 'files': {}}), encoding='utf-8')
            before = sidecar.read_bytes()
            script = Path(temp) / 'stop_during_copy.py'
            script.write_text(self.SCRIPT, encoding='utf-8')
            done = subprocess.run([sys.executable, str(script), str(KIT), str(root)], capture_output=True, text=True,
                                  timeout=120, cwd=temp)
            self.assertEqual(143, done.returncode, done.stderr)
            self.assertEqual([], sorted(p.name for p in (root / 'backups').glob('*.restore')))
            self.assertEqual(before, sidecar.read_bytes())


# ------------------------------------------------------------------------------------------------ F4
class RestoreLeftoverRealBdTests(RealRuntimeCase):
    """A wrong-project sidecar is refused before creation; a half restore fails the gate."""

    def project_with_governance(self, name):
        import requirement_governance as governance
        path = self.new_project(name)
        governance.initialize(path, name, 'owner', 'creation-' + name)
        return path

    def injected_half_restore(self, source, destination):
        """restore-new with a fault injected in the coordination step, after the re-point."""
        with mock.patch.object(admin, 'restore_coordination', side_effect=ValueError('injected fault')):
            return self.main('restore-new', source, destination)

    def statuses(self):
        record = json.loads((self.root / 'backups' / admin.BACKUP_STATUS_NAME).read_text(encoding='utf-8'))
        return {entry['name']: entry for entry in record['projects']}

    def test_a_sidecar_of_another_project_is_refused_before_anything_is_created(self):
        self.project_with_governance('hsg')
        self.backup('hsg')
        shutil.copytree(str(self.root / 'backups' / 'hsg'), str(self.root / 'backups' / 'hswrong'))
        shutil.copy(str(self.root / 'backups' / 'hsg.coordination.json'),
                    str(self.root / 'backups' / 'hswrong.coordination.json'))
        code, out, err = self.main('restore-new', 'hswrong', 'hswrongd')
        self.assertNotEqual(0, code)
        self.assertIn('Requirements governance belongs to a different project', err)
        self.assertFalse(self.exists('hswrongd'), 'nothing was created')
        self.assertNotIn('restore-new did not complete', err, 'no leftover notice: there is no leftover')
        # control: the right name restores
        code, out, err = self.main('restore-new', 'hsg', 'hsgd')
        self.assertEqual(0, code, err or out)
        self.assertFalse((self.root / 'projects' / 'hsgd' / admin.RESTORE_INCOMPLETE_MARKER).exists())

    def test_a_half_restore_is_recorded_incomplete_and_fails_the_gate_until_retired(self):
        self.new_project('hsh')
        self.backup('hsh')
        code, out, err = self.injected_half_restore('hsh', 'hshd')
        self.assertNotEqual(0, code)
        self.assertIn('restore-new did not complete', err)
        marker = self.root / 'projects' / 'hshd' / admin.RESTORE_INCOMPLETE_MARKER
        self.assertTrue(marker.is_file(), 'the leftover carries the marker')
        # backup --all: the leftover is recorded failed, the run exits non-zero, the gate fails
        code, out, err = self.main('backup', '--all')
        self.assertNotEqual(0, code)
        entry = self.statuses()['hshd']
        self.assertEqual('failed', entry['status'])
        self.assertIn('restore-new that did not complete', entry['reason'])
        self.assertNotEqual(0, self.main('backup-status', '--require-complete')[0])
        # the marker is invisible to sweep (it is no temporary) and the copy leaves the leftover out
        import coordination
        self.assertFalse(coordination.is_temporary(admin.RESTORE_INCOMPLETE_MARKER))
        swept = admin.sweep_project_temporaries(self.root, 'hshd', min_age_seconds=0)
        self.assertEqual(0, swept['removed_total'])
        self.assertTrue(marker.is_file())
        away = Path(tempfile.mkdtemp(prefix='backup-copy-away-'))
        self.addCleanup(shutil.rmtree, str(away), ignore_errors=True)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                admin.backup_copy(self.root, str(away))
        self.assertIn('hshd', str(stopped.exception))
        self.assertFalse((away / 'hshd').exists())
        self.assertTrue(marker.is_file())
        # a retry into another name succeeds and carries no marker; the same name is refused
        code, out, err = self.main('restore-new', 'hsh', 'hshe')
        self.assertEqual(0, code, err or out)
        self.assertFalse((self.root / 'projects' / 'hshe' / admin.RESTORE_INCOMPLETE_MARKER).exists())
        self.assertNotEqual(0, self.main('restore-new', 'hsh', 'hshd')[0])
        self.assertTrue(marker.is_file(), 'a refused retry into the name leaves the leftover as it was')
        # retire-project keeps working on it and takes the marker with the rest
        code, out, err = self.main('retire-project', 'hshd', '--actor', 'ops', '--reason', 'half restore', '--force')
        self.assertEqual(0, code, err or out)
        self.assertFalse(self.exists('hshd'))
        retired = sorted((self.root / admin.RETIRED_DIR).glob('hshd-*'))
        self.assertEqual(1, len(retired))
        self.assertTrue((retired[0] / admin.RESTORE_INCOMPLETE_MARKER).is_file())
        # with the leftover retired the gate is green again
        self.assertEqual(0, self.main('backup', '--all')[0])
        self.assertEqual(0, self.main('backup-status', '--require-complete')[0])

    def test_a_finished_restore_leaves_no_marker_and_backs_up(self):
        self.new_project('hsk')
        self.backup('hsk')
        code, out, err = self.main('restore-new', 'hsk', 'hskd')
        self.assertEqual(0, code, err or out)
        self.assertFalse((self.root / 'projects' / 'hskd' / admin.RESTORE_INCOMPLETE_MARKER).exists())
        self.assertEqual(0, self.main('backup', 'hskd')[0])


if __name__ == '__main__':
    unittest.main()
