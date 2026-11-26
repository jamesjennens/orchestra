"""Durable authorization and coarse HTTP activity observations."""
import http.client
import json
import os
import re
import selectors
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import http_auth
import http_authority
from test_http_review_fixes import EndpointCase
from test_http_session_limits import LimitedHarness


class ActivityTests(LimitedHarness):
    def setUp(self):
        super().setUp()
        self.token = self.login_with_session().data['session']['token']

    def worker(self, project):
        answer = self.request('POST', '/v1/projects/%s/worker-credentials' % project,
                              {'label': 'worker', 'actor': 'worker'}, token=self.token)
        self.assertEqual(201, answer.status)
        return answer.data['credential']

    def test_warm_session_worker_and_agent_reads_write_zero_then_one(self):
        project = self.create_project(self.token, 'Activity')
        worker = self.worker(project)
        agent, agent_token, _ = self.agent_secret(self.token, projects=[project])
        routes = [
            ('/v1/sessions/current', self.token),
            ('/v1/projects/%s/tasks' % project, worker['secret']),
            ('/v1/agents/me', agent_token),
        ]
        for route, token in routes:
            self.assertEqual(200, self.request('GET', route, token=token).status)
        with mock.patch.object(self.store, '_write', wraps=self.store._write) as writes, \
                mock.patch.object(self.store, '_load', wraps=self.store._load) as reloads, \
                mock.patch.object(http_auth, 'file_lock', wraps=http_auth.file_lock) as fences:
            for elapsed in (1, 5, 20, 59):
                self.tick[0] = self.start + elapsed
                for route, token in routes:
                    self.assertEqual(200, self.request('GET', route, token=token).status)
            self.assertEqual(0, writes.call_count)
            self.assertEqual(0, reloads.call_count)
            if self.store._stat_fast_path:
                fences.assert_not_called()
            seen = self.request('GET', '/v1/agents/%s' % agent, token=self.token)
            self.assertEqual(http_auth.now_iso(self.tick[0]), seen.data['last_seen_at'])
            self.assertEqual(0, writes.call_count)
            self.tick[0] = self.start + 60
            for expected, (route, token) in enumerate(routes, 1):
                self.assertEqual(200, self.request('GET', route, token=token).status)
                self.assertEqual(expected, writes.call_count, route)
            self.assertEqual(0, reloads.call_count)
            if self.store._stat_fast_path:
                self.assertEqual(3, fences.call_count)
                self.assertFalse(any(call.kwargs.get('shared') for call in fences.call_args_list))

    def test_cookie_mutation_still_needs_csrf(self):
        with self.connection(self.port) as client:
            client.request('POST', '/v1/accounts', json.dumps({'username': 'csrf-check'}),
                           {'Cookie': 'orchestra_session=' + self.token,
                            'Content-Type': 'application/json'})
            response = client.getresponse()
            self.assertEqual(403, response.status)
            self.assertIn('CSRF token missing or invalid', response.read().decode())
        self.assertNotIn('csrf-check', self.store.state['usernames'])

    @staticmethod
    def connection(port):
        from contextlib import closing
        return closing(http.client.HTTPConnection('127.0.0.1', port, timeout=10))

    def test_session_idle_bound_is_conservative_within_one_stamp_window(self):
        digest = http_auth.token_hash(self.token)
        deadline = self.store.state['sessions'][digest]['idle_expires']
        self.tick[0] = self.start + 59
        read = self.session(self.token)
        self.assertEqual(200, read.status)
        self.assertEqual(http_auth.now_iso(deadline), read.data['idle_expires_at'])
        self.tick[0] = deadline
        self.assertEqual(401, self.session(self.token).status)

    def test_session_and_credential_revocation_wins_in_process_and_across_processes(self):
        project = self.create_project(self.token, 'Activity')
        worker = self.worker(project)
        session = self.login_with_session().data['session']['token']
        self.assertEqual(200, self.session(session).status)
        route = '/v1/projects/%s/tasks' % project
        self.assertEqual(200, self.request('GET', route, token=worker['secret']).status)
        stale = http_auth.Store(self.store.path, clock=lambda: self.tick[0])
        child = """
import json
import sys
from http_auth import Store, Service
data = json.load(sys.stdin)
store = Store(data['path'], clock=lambda: data['now'])
service = Service(store)
owner = service.authenticate(data['owner'])
service.revoke_session(data['session_hash'])
service.revoke_credential(owner, data['project'], data['credential'])
"""
        payload = dict(path=str(self.store.path), now=self.tick[0], owner=self.token,
                       session_hash=http_auth.token_hash(session), project=project,
                       credential=worker['id'])
        done = subprocess.run([sys.executable, '-c', child], input=json.dumps(payload),
                              text=True, cwd=KIT, capture_output=True, timeout=30)
        self.assertEqual(0, done.returncode, done.stderr)
        committed = self.store.path.read_bytes()
        with self.assertRaises(TimeoutError):
            stale.save()
        self.assertEqual(committed, self.store.path.read_bytes())
        with mock.patch.object(self.store, '_load', wraps=self.store._load) as refreshes:
            self.assertEqual(401, self.session(session).status)
            self.assertEqual(401, self.request('GET', route, token=worker['secret']).status)
        self.assertEqual(1, refreshes.call_count)

    def test_external_restore_revokes_next_warm_request(self):
        import admin
        project = self.create_project(self.token, 'Restored state')
        worker = self.worker(project)
        route = '/v1/projects/%s/tasks' % project
        self.assertEqual(200, self.session(self.token).status)
        self.assertEqual(200, self.request('GET', route, token=worker['secret']).status)
        backups = self.tmp_path / 'backups'
        backups.mkdir()
        (backups / admin.HTTP_STATE_JSON).write_bytes(self.store.path.read_bytes())
        # The HTTP fixture is in this process. Suppress system process discovery
        # while running the real restore-new publisher against our own state.
        with mock.patch.object(admin, 'http_service_processes', return_value=[]):
            message = admin.restore_http_state(self.tmp_path, named=self.store.path,
                                               credentials='revoke', now=self.tick[0])
        self.assertIn('Every session in it was revoked', message)
        self.assertEqual(401, self.session(self.token).status)
        self.assertEqual(401, self.request('GET', route, token=worker['secret']).status)

    def test_changed_generation_does_not_drop_unsaved_narrowing(self):
        session = self.login_with_session().data['session']['token']
        digest = http_auth.token_hash(session)
        alternate = http_auth.Store(self.store.path, clock=lambda: self.tick[0])
        with mock.patch.object(http_auth, 'file_lock', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.service.revoke_session(digest)
        self.assertTrue(self.store.state['sessions'][digest]['revoked'])
        unsaved = self.store.unsaved_since
        self.assertIsNotNone(unsaved)
        alternate.state['activity_fixture_nonce'] = 1
        alternate.save()
        self.assertFalse(alternate.state['sessions'][digest]['revoked'])
        committed = self.store.path.read_bytes()
        self.assertEqual(503, self.session(session).status)
        self.assertTrue(self.store.state['sessions'][digest]['revoked'])
        self.assertEqual(unsaved, self.store.unsaved_since)
        self.assertEqual(committed, self.store.path.read_bytes())

    def test_generation_fence_timeout_refuses_then_reloads(self):
        alternate = http_auth.Store(self.store.path, clock=lambda: self.tick[0])
        alternate.save()
        with mock.patch.object(http_auth, 'file_lock', side_effect=TimeoutError) as fence:
            self.assertEqual(503, self.session(self.token).status)
        self.assertTrue(fence.call_args.kwargs['shared'])
        self.assertIsNone(self.store.unsaved_since)
        self.assertEqual(200, self.session(self.token).status)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'stable Linux generation proof under a writer lock')
    def test_expired_session_with_unsaved_stamp_is_refused_on_either_clock(self):
        entered, release = threading.Event(), threading.Event()

        def hold_lock():
            with http_authority.file_lock(str(self.store.path) + '.lock'):
                entered.set()
                release.wait(15)

        for clock in ('monotone', 'raw'):
            with self.subTest(clock=clock):
                entered.clear()
                release.clear()
                initial = self.tick[0] + 500
                self.tick[0] = initial
                if clock == 'raw':
                    self.store.records.monotonic_now(now=initial + 1000)
                token = self.login_with_session().data['session']['token']
                digest = http_auth.token_hash(token)
                saved = dict(self.store.state['sessions'][digest])
                holder = threading.Thread(target=hold_lock, daemon=True)
                holder.start()
                try:
                    self.assertTrue(entered.wait(5))
                    self.tick[0] += 61
                    touch = self.session(token)
                    self.assertEqual(200, touch.status)
                    expected = min(saved['idle_expires'], saved['last_used_raw'] + self.service.session_idle)
                    self.assertEqual(http_auth.now_iso(expected), touch.data['idle_expires_at'])
                    self.assertIsNone(self.store.unsaved_since)
                    self.assertEqual(saved, self.store.state['sessions'][digest])
                finally:
                    release.set()
                    holder.join(5)
                if clock == 'monotone':
                    self.store.records.monotonic_now(now=saved['idle_expires'])
                else:
                    self.tick[0] = initial + self.service.session_idle
                self.assertEqual(401, self.session(token).status)
                self.assertNotIn(('session', digest), self.service._pending_activity)
                durable = json.loads(self.store.path.read_text())['sessions'][digest]
                self.assertEqual(saved['last_used'], durable['last_used'])

    @unittest.skipUnless(os.name == 'posix', 'SIGTERM and SIGKILL are POSIX signals')
    def test_crash_loses_observation_and_sigterm_flushes_actual_use(self):
        child = """
import sys
from pathlib import Path
import http_service
from http_auth import Store
path, clock = sys.argv[1:]
http_service.Store = lambda name: Store(name, clock=lambda: float(Path(clock).read_text()))
raise SystemExit(http_service.main(['--state', path, '--port', '0', '--backend', 'inprocess',
                                    '--no-web', '--session-idle-minutes', '5']))
"""
        timer = self.tmp_path / 'clock'
        for stop in ('kill', 'term'):
            with self.subTest(stop=stop):
                token = self.login_with_session().data['session']['token']
                digest = http_auth.token_hash(token)
                used = self.tick[0] + 20
                timer.write_text(str(used))
                process = subprocess.Popen([sys.executable, '-u', '-c', child,
                                            str(self.store.path), str(timer)], cwd=KIT,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    with selectors.DefaultSelector() as ready:
                        ready.register(process.stdout, selectors.EVENT_READ)
                        self.assertTrue(ready.select(10))
                        line = process.stdout.readline()
                    started = re.search(rb'listening on 127\.0\.0\.1:(\d+)', line)
                    self.assertIsNotNone(started, line)
                    with self.connection(int(started.group(1))) as client:
                        client.request('GET', '/v1/sessions/current',
                                       headers={'Authorization': 'Bearer ' + token})
                        response = client.getresponse()
                        self.assertEqual(200, response.status, response.read())
                    pre_stop = json.loads(self.store.path.read_text())['sessions'][digest]
                    self.assertEqual(self.tick[0], pre_stop['last_used'])
                    timer.write_text(str(used + 20))
                    if stop == 'kill':
                        process.kill()
                    else:
                        process.terminate()
                    _, stderr = process.communicate(timeout=12)
                    self.assertEqual(-9 if stop == 'kill' else 0, process.returncode, stderr)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=10)
                after = json.loads(self.store.path.read_text())['sessions'][digest]
                expected = self.tick[0] if stop == 'kill' else used
                self.assertEqual(expected, after['last_used'])
                self.assertEqual(expected, after['last_used_raw'])
                self.assertEqual(expected + self.service.session_idle, after['idle_expires'])
                self.tick[0] += 120


class CanonicalActivityTests(EndpointCase):
    def test_task_create_keeps_one_json_save_when_stamp_due(self):
        tick = [self.store.now()]
        self.store.clock = lambda: tick[0]
        token, project = self.setup_project()
        tick[0] += 60
        with mock.patch.object(self.store, '_write', wraps=self.store._write) as writes:
            made = self.create_task(token, project, 'one durable task')
        self.assertEqual(201, made.status)
        self.assertEqual(1, writes.call_count)
        self.assertEqual(1, sum(row['id'] == made.data['id'] for row in self.canonical_rows()))


if __name__ == '__main__':
    unittest.main()
