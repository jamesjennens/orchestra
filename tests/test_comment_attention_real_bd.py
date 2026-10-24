"""Actual bd, endpoint, worker CLI wait and loopback HTTP share comment attention."""
import json
import subprocess
import sys
import unittest
import waiting
from pathlib import Path
import test_coordinator_grant_real as native
import test_http_review_fixes as fixes

KIT = Path(__file__).resolve().parents[1]


class NativeCommentWaitTests(native.RealStackTests):
    def checkpoint(self, base):
        brief = self.request('GET', base + '/brief', token=self.agent).data
        cp = dict(schema_version=1, previous=(brief.get('checkpoint') or {}).get('id'),
                  activity_cursor=brief['activity_cursor'], source_commit='', branch='',
                  intent='Read feedback', acceptance='Wait parity', summary='All comments read',
                  next_action='Continue', open_items=[], resolved=[])
        answer = self.request('POST', base + '/checkpoints', cp, token=self.agent)
        self.assertEqual(answer.status, 201, answer.data)

    def wait(self):
        answer = self.request('GET', '/v1/agents/me/wait', token=self.agent)
        self.assertEqual(answer.status, 200, answer.data)
        queue = self.backend._run('work', 'pp', self.backend.actor_namespace + '/read',
                                  ['--owner', self.agent_actor, '--json'])
        cli = waiting.worker_view(queue['items'])
        self.assertEqual(answer.data['fingerprint'], cli['fingerprint'])
        self.assertEqual([r['task'] for r in answer.data['items']], [r['task'] for r in cli['items']])
        return answer.data

    def note(self, base, token, text='Continue the remaining work'):
        answer = self.request('POST', base + '/comments', {'text': text}, token=token)
        self.assertEqual(answer.status, 201, answer.data)

    def test_native_review_states_comments_clear_and_worker_wait(self):
        final = None
        for index, state in enumerate(('none', 'awaiting-review', 'changes-requested', 'awaiting-integration')):
            with self.subTest(state=state):
                task = self.new('Comment wait ' + state)
                base = self.tasks + '/' + task
                claimed = self.request('POST', base + '/claim', {}, token=self.agent)
                self.assertEqual(claimed.status, 200, claimed.data)
                contribution = None
                feedback = None
                if state != 'none':
                    answer = self.request('POST', base + '/reviews', dict(fixes.CONTRIBUTION,
                        operation='contribute', schema_version=1, operation_id='comment-deliver-%d' % index,
                        previous=None), token=self.agent)
                    self.assertEqual(answer.status, 201, answer.data)
                    contribution = self.review(task)['contribution']['id']
                    if state == 'awaiting-integration':
                        answer = self.approve(self.tokens['alex'], task, contribution)
                        self.assertEqual(answer.status, 201, answer.data)
                    elif state == 'changes-requested':
                        answer = self.request('POST', base + '/reviews', dict(operation='request-changes',
                            schema_version=1, operation_id='comment-feedback', previous=contribution,
                            contribution=contribution, items=[dict(id='fix', text='Fix')]), token=self.tokens['alex'])
                        self.assertEqual(answer.status, 201, answer.data)
                        feedback = answer.data.get('comment_id') or (answer.data.get('review') or {}).get('id')
                        self.assertIsNotNone(feedback, answer.data)
                self.checkpoint(base)
                if state == 'awaiting-review':
                    self.note(base, self.tokens['casey'], 'A plain member comment')
                    self.assertNotIn(task, [r['task'] for r in self.wait()['items']])
                    role_route = '/v1/projects/pp/members/' + self.ids['casey']
                    changed = self.request('PUT', role_route, {'role': 'coordinator'}, token=self.tokens['alex'])
                    self.assertEqual(changed.status, 200, changed.data)
                    self.assertIn(task, [r['task'] for r in self.wait()['items']])
                    changed = self.request('PUT', role_route, {'role': 'contributor'}, token=self.tokens['alex'])
                    self.assertEqual(changed.status, 200, changed.data)
                    self.assertNotIn(task, [r['task'] for r in self.wait()['items']])
                before = self.wait()
                self.note(base, self.agent, 'Own progress')
                self.assertEqual(before['fingerprint'], self.wait()['fingerprint'])
                self.note(base, self.tokens['alex'])
                first = self.wait()
                self.assertNotEqual(before['fingerprint'], first['fingerprint'])
                self.assertIn(task, [r['task'] for r in first['items']])
                answer = self.request('GET', '/v1/agents/me/next', token=self.agent)
                self.assertEqual(answer.status, 200, answer.data)
                action = next(r for r in answer.data['next_actions'] if r['task'] == task)
                self.assertEqual(action['whose_move'], 'you')
                self.assertEqual(first['fingerprint'], self.wait()['fingerprint'])
                self.note(base, self.tokens['cora'], 'Second coordinator comment')
                self.assertNotEqual(first['fingerprint'], self.wait()['fingerprint'])
                if feedback:
                    answer = self.request('POST', base + '/reviews', dict(operation='respond',
                        schema_version=1, operation_id='comment-response', previous=feedback,
                        contribution=contribution, resolutions=[dict(request=feedback, item='fix',
                            reason='Fixed', evidence='Commit')]), token=self.agent)
                    self.assertEqual(answer.status, 201, answer.data)
                    self.assertNotIn(task, [r['task'] for r in self.wait()['items']])
                    self.note(base, self.tokens['alex'], 'After the response')
                    self.assertIn(task, [r['task'] for r in self.wait()['items']])
                self.checkpoint(base)
                if state != 'none':
                    self.assertNotIn(task, [r['task'] for r in self.wait()['items']])
                if state != 'awaiting-integration':
                    current_reply = self.request('GET', base, token=self.tokens['alex'])
                    self.assertEqual(current_reply.status, 200, current_reply.data)
                    current = current_reply.data
                    closed = self.request('PATCH', base, dict(status='closed', **({'version': current['version']} if 'version' in current else {})), token=self.tokens['alex'])
                    self.assertEqual(closed.status, 200, closed.data)
                final = task, base
        task, base = final
        # A host worker exercises the actual kit wait command. HTTP-reserved actor
        # names require a live descriptor, so this host identity is separate.
        config_path = self.root / 'deployment.private.json'
        document = json.loads(config_path.read_text())
        document['operators'] = list(document.get('operators') or []) + ['host-coordinator']
        config_path.write_text(json.dumps(document))
        actor = 'native-comment-lane'
        made = self.bd('create', 'Host comment wait', '--assignee', actor, '--json')
        self.assertEqual(made.returncode, 0, made.stderr)
        host_task = json.loads(made.stdout)['id']
        def host(action, args, attachment=None, caller=actor):
            request = dict(project='pp', actor=caller, action=action, args=args)
            if attachment is not None:
                request['attachments'] = {'payload': {'flag': '--file', 'text': json.dumps(attachment)}}
            answer = native.rb.endpoint.execute(self.root, request)
            self.assertEqual(answer['returncode'], 0, answer)
            return json.loads(answer['stdout'])
        delivery = dict(schema_version=1, operation='contribute', operation_id='host-comment-delivery',
            task=host_task, previous=None, repository='https://example.invalid/repo', commit='a' * 40,
            base_commit='b' * 40, delivery=dict(kind='bundle', path='host:/synthetic.bundle', sha256='c' * 64),
            summary='Synthetic delivery', supersedes=None)
        host('review', [host_task, '@attachment:payload'], delivery)
        brief = host('brief', [host_task, '--json'])
        cp = dict(schema_version=1, task=host_task, previous=None, activity_cursor=brief['activity_cursor'],
            source_commit='', branch='', intent='Read comments', acceptance='Wait parity', summary='Read all',
            next_action='Wait for coordinator', open_items=[], resolved=[])
        host('checkpoint', [host_task, '@attachment:payload'], cp)
        config = self.root / 'comment-wait-client.json'
        config.write_text(json.dumps(dict(transport='local', python=sys.executable,
            endpoint=str(KIT / 'endpoint.py'), root=str(self.root))), encoding='utf-8')
        command = [sys.executable, str(KIT / 'client.py'), '--config', str(config), '--project', 'pp',
                   '--actor', actor, '--', 'wait', '--timeout', '1', '--interval', '60']
        answer = subprocess.run(command, capture_output=True, text=True, timeout=150)
        self.assertEqual(answer.returncode, 1, answer.stdout + answer.stderr)
        self.assertNotIn(host_task, [r['task'] for r in json.loads(answer.stdout)['items']])
        host('bd', ['comments', 'add', host_task, 'Continue remaining work', '--json'], caller='host-coordinator')
        answer = subprocess.run(command, capture_output=True, text=True, timeout=150)
        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
        actual = json.loads(answer.stdout)
        self.assertIn(host_task, [r['task'] for r in actual['items']])
        answer = subprocess.run(command + ['--since', actual['fingerprint']], capture_output=True,
                                text=True, timeout=150)
        self.assertEqual(answer.returncode, 1, answer.stdout + answer.stderr)
        self.assertTrue(json.loads(answer.stdout)['timed_out'])
        self.checkpoint(base)
        current_reply = self.request('GET', base, token=self.tokens['alex'])
        self.assertEqual(current_reply.status, 200, current_reply.data)
        current = current_reply.data
        closed = self.request('PATCH', base, dict(status='closed', **({'version': current['version']} if 'version' in current else {})), token=self.tokens['alex'])
        self.assertEqual(closed.status, 200, closed.data)
        self.note(base, self.tokens['alex'], 'A closed task stays closed')
        self.assertNotIn(task, [r['task'] for r in self.wait()['items']])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([NativeCommentWaitTests('test_native_review_states_comments_clear_and_worker_wait')])
