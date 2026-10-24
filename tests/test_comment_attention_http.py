"""Loopback HTTP: comment attention survives the task's review evidence state."""
import unittest
from test_http_agents import AgentHarness, COMMIT, BASE, BUNDLE


class CommentHttpTests(AgentHarness):
    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.owner = self.login('alex', 'alex-password-1')[0]
        self.project = self.create_project(self.owner, 'Alpha')
        self.agent_id, self.secret, _ = self.agent_secret(self.owner, projects=[self.project])
        self.saved = {}

    def task(self, state):
        made = self.create_task(self.owner, self.project, state)
        self.assertEqual(made.status, 201, made.data)
        identity = made.data['id']
        base = '/v1/projects/%s/tasks/%s' % (self.project, identity)
        self.assertEqual(self.request('POST', base + '/claim', token=self.secret).status, 200)
        if state != 'none':
            result = self.request('POST', base + '/reviews', dict(operation='contribute', commit=COMMIT,
                base_commit=BASE, bundle_sha256=BUNDLE, summary='Delivered'), token=self.secret)
            self.assertEqual(result.status, 201, result.data)
            if state in ('changes-requested', 'awaiting-integration'):
                result = self.request('POST', base + '/reviews', dict(
                    operation='request-changes' if state == 'changes-requested' else 'approve',
                    summary='Review', **({'items': [{'id': 'fix', 'text': 'Fix'}]} if state == 'changes-requested' else {})),
                    token=self.owner)
                self.assertEqual(result.status, 201, result.data)
                if state == 'changes-requested':
                    self.feedback = result.data['review']['id']
        self.checkpoint(base)
        return identity, base

    def checkpoint(self, base):
        result = self.request('POST', base + '/checkpoints', dict(previous=self.saved.get(base),
            summary='Read all comments', open_items=[]), token=self.secret)
        self.assertEqual(result.status, 201, result.data)
        self.saved[base] = result.data['checkpoint']['id']

    def wait(self):
        result = self.request('GET', '/v1/agents/me/wait', token=self.secret)
        self.assertEqual(result.status, 200, result.data)
        return result.data

    def note(self, base, token):
        result = self.request('POST', base + '/comments', {'text': 'Continue with the remaining work'}, token=token)
        self.assertEqual(result.status, 201, result.data)

    def test_each_review_state_next_and_wait_agree_and_own_comment_is_quiet(self):
        for state in ('none', 'awaiting-review', 'changes-requested', 'awaiting-integration'):
            with self.subTest(state=state):
                identity, base = self.task(state)
                initial = self.wait()
                self.note(base, self.secret)
                self.assertEqual(initial['fingerprint'], self.wait()['fingerprint'])
                self.note(base, self.owner)
                wake = self.wait()
                self.assertNotEqual(initial['fingerprint'], wake['fingerprint'])
                self.assertIn(identity, [row['task'] for row in wake['items']])
                answer = self.request('GET', '/v1/agents/me/next', token=self.secret)
                self.assertEqual(answer.status, 200, answer.data)
                action = next(row for row in answer.data['next_actions'] if row['task'] == identity)
                self.assertEqual(action['whose_move'], 'you')
                self.assertEqual(action['review_state'], 'approved' if state == 'awaiting-integration' else state)
                self.assertEqual(wake['fingerprint'], self.wait()['fingerprint'])
                self.note(base, self.owner)
                self.assertNotEqual(wake['fingerprint'], self.wait()['fingerprint'])
                self.checkpoint(base)
                if state in ('awaiting-review', 'awaiting-integration'):
                    self.assertNotIn(identity, [row['task'] for row in self.wait()['items']])
                current = self.request('GET', base, token=self.owner).data
                closed = self.request('PATCH', base, {'status': 'closed', 'version': current['version']}, token=self.owner)
                self.assertEqual(closed.status, 200, closed.data)

    def test_response_clears_delivered_comment_attention(self):
        identity, base = self.task('changes-requested')
        self.note(base, self.owner)
        result = self.request('POST', base + '/reviews', dict(operation='respond', resolutions=[dict(
            request=self.feedback, item='fix', reason='Fixed', evidence='Commit')]), token=self.secret)
        self.assertEqual(result.status, 201, result.data)
        self.assertNotIn(identity, [row['task'] for row in self.wait()['items']])
        self.note(base, self.owner)
        self.assertIn(identity, [row['task'] for row in self.wait()['items']])

    def test_closed_task_never_gets_new_comment_work(self):
        identity, base = self.task('awaiting-review')
        self.backend.state['tasks'][identity]['status'] = 'closed'
        self.note(base, self.owner)
        self.assertNotIn(identity, [row['task'] for row in self.wait()['items']])
