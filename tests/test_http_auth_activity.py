"""Durable authorization and coarse HTTP activity observations."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import http_auth
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
                mock.patch.object(self.store, '_load', wraps=self.store._load) as reloads:
            for elapsed in (1, 5, 20, 59):
                self.tick[0] = self.start + elapsed
                for route, token in routes:
                    self.assertEqual(200, self.request('GET', route, token=token).status)
            self.assertEqual(0, writes.call_count)
            self.assertEqual(0, reloads.call_count)
            seen = self.request('GET', '/v1/agents/%s' % agent, token=self.token)
            self.assertEqual(http_auth.now_iso(self.tick[0]), seen.data['last_seen_at'])
            self.assertEqual(0, writes.call_count)
            self.tick[0] = self.start + 60
            for expected, (route, token) in enumerate(routes, 1):
                self.assertEqual(200, self.request('GET', route, token=token).status)
                self.assertEqual(expected, writes.call_count, route)
            self.assertEqual(0, reloads.call_count)

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
        self.assertEqual(401, self.session(session).status)
        self.assertEqual(401, self.request('GET', route, token=worker['secret']).status)


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
