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
        self.assertFalse(self.backend._assignee_comment_pending(self.backend.state['tasks'][identity]))
        self.assertNotIn(identity, [row['task'] for row in self.wait()['items']])

    def test_plain_member_is_quiet_and_current_promotion_and_removal_change_attention(self):
        admin = self.admin_token()
        member = self.create_account(admin, 'member', 'member-password-1')
        token = self.login('member', 'member-password-1')[0]
        route = '/v1/projects/%s/members/%s' % (self.project, member)
        self.assertEqual(self.request('PUT', route, {'role': 'contributor'}, token=self.owner).status, 200)
        identity, base = self.task('awaiting-review')
        self.note(base, token)
        self.assertNotIn(identity, [r['task'] for r in self.wait()['items']])
        self.assertEqual(self.request('PUT', route, {'role': 'coordinator'}, token=self.owner).status, 200)
        self.assertIn(identity, [r['task'] for r in self.wait()['items']])
        self.assertEqual(self.request('PUT', route, {'role': 'contributor'}, token=self.owner).status, 200)
        self.assertNotIn(identity, [r['task'] for r in self.wait()['items']])
        self.assertEqual(self.request('PUT', route, {'role': 'coordinator'}, token=self.owner).status, 200)
        self.checkpoint(base)
        self.assertNotIn(identity, [r['task'] for r in self.wait()['items']])

    def test_reserved_review_event_and_reserved_comment_body_never_add_attention(self):
        identity, base = self.task('awaiting-integration')
        self.assertFalse(self.backend._assignee_comment_pending(self.backend.state['tasks'][identity]))
        self.backend.state['events'].append(dict(task=identity, project=self.project, action='approve',
                                                actor='someone-else'))
        self.assertFalse(self.backend._assignee_comment_pending(self.backend.state['tasks'][identity]))
        owner_id = next(uid for uid, role in self.service.state['memberships'][self.project].items() if role == 'owner')
        self.backend.state['events'].append(dict(task=identity, project=self.project, action='comment',
                    actor=owner_id, detail='Kind: contribution-review-v1\n{}'))
        self.assertFalse(self.backend._assignee_comment_pending(self.backend.state['tasks'][identity]))

    def test_another_agents_old_comment_needs_its_effective_coordinator_grant(self):
        admin = self.admin_token()
        member = self.create_account(admin, 'coordinator', 'coordinator-password-1')
        token = self.login('coordinator', 'coordinator-password-1')[0]
        route = '/v1/projects/%s/members/%s' % (self.project, member)
        self.assertEqual(self.request('PUT', route, {'role': 'coordinator'}, token=self.owner).status, 200)
        helper, secret, _ = self.agent_secret(token, projects=[self.project])
        identity, base = self.task('awaiting-review')
        self.note(base, secret)
        self.assertNotIn(identity, [r['task'] for r in self.wait()['items']])
        grant = '/v1/projects/%s/agents/%s/coordinator' % (self.project, helper)
        self.assertEqual(self.request('PUT', grant, {}, token=self.owner).status, 200)
        self.assertIn(identity, [r['task'] for r in self.wait()['items']])
        self.assertEqual(self.request('DELETE', grant, token=self.owner).status, 200)
        self.assertNotIn(identity, [r['task'] for r in self.wait()['items']])
