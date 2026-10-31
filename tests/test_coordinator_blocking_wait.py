"""The blocking coordinator wait over HTTP (kittrial-5bb.457): timeout, comment wakes, caps, authority."""
import http.client
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import waiting
import test_coordinator_grant as grants

WAIT = '/v1/agents/me/wait?form=coordinator'


class BlockingWaitTests(grants.Grants):
    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.grant().status)
        patcher = mock.patch.multiple(http_service.ApiHandler, WAIT_INTERVAL=0.2)
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait(self, token=None, query=''):
        return self.request('GET', WAIT + query, token=token or self.agent)

    def baseline(self, token=None, comments=True):
        answer = self.wait(token, '&comments=others' if comments else '')
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    def comment(self, token, task, text='a comment'):
        made = self.request('POST', self.base(task) + '/comments', {'text': text}, token=token)
        self.assertEqual(201, made.status, made.data)
        return made.data

    def new_task(self, title='unassigned'):
        return self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': title},
                            token=self.admin).data['id']

    def in_thread(self, call):
        box = {}

        def run():
            box['started'] = time.monotonic()
            box['answer'] = call()
            box['seconds'] = time.monotonic() - box['started']
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        return thread, box

    def test_a_comment_by_another_actor_wakes_unassigned_task_included_and_own_never_does(self):
        task = self.new_task()
        before = self.baseline()
        self.assertEqual([], before['comments'])
        self.comment(self.agent, task, 'my own')
        self.assertEqual(before['fingerprint'], self.baseline()['fingerprint'])
        # Another actor of the SAME person: the account, not the agent.
        self.comment(self.tokens['cora'], task, 'the person')
        after = self.baseline()
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])
        self.assertEqual([task], [row['task'] for row in after['comments']])
        self.assertNotIn('the person', json.dumps(after))
        self.comment(self.tokens['carl'], task, 'a worker')
        self.assertEqual(2, len(self.baseline()['comments']))

    def test_newest_twenty_pointers_and_machine_records_do_not_wake(self):
        task = self.new_task()
        for number in range(23):
            self.comment(self.tokens['carl'], task, 'c%d' % number)
        pointers = self.baseline()['comments']
        self.assertEqual(waiting.COMMENT_POINTER_LIMIT, len(pointers))
        import lane_state
        quiet = self.baseline()
        self.assertTrue(lane_state.is_record(lane_state.PREFIX + '{}'))
        self.assertEqual([], waiting.comment_pointers(
            [{'id': 't', 'comments': [{'id': '1', 'author': 'x', 'text': lane_state.PREFIX + '{}'}]}], 'me'))
        self.assertEqual(quiet['fingerprint'], self.baseline()['fingerprint'])

    def test_without_the_parameter_the_fingerprint_is_the_cli_fingerprint(self):
        task = self.new_task()
        plain = self.baseline(comments=False)
        self.assertNotIn('comments', plain)
        expected = self.backend.coordinator_wait(self.project, self.service.authenticate(self.agent), 15)
        self.assertEqual(expected['fingerprint'], plain['fingerprint'])
        self.comment(self.tokens['carl'], task)
        self.assertEqual(plain['fingerprint'], self.baseline(comments=False)['fingerprint'])
        self.assertNotEqual(plain['fingerprint'], self.baseline()['fingerprint'])

    def test_it_blocks_until_a_comment_arrives_and_a_writer_is_not_held_up(self):
        task = self.new_task()
        since = self.baseline()['fingerprint']
        query = '&comments=others&timeout=20&since=' + since
        thread, box = self.in_thread(lambda: self.wait(query=query))
        time.sleep(0.6)
        self.assertTrue(thread.is_alive())
        # No lock is held while it sleeps: the service lock and a write are both free at once.
        self.assertTrue(self.service.store.lock.acquire(timeout=1))
        self.service.store.lock.release()
        began = time.monotonic()
        self.comment(self.tokens['carl'], task, 'wake up')
        self.assertLess(time.monotonic() - began, 2)
        thread.join(10)
        answer = box['answer']
        self.assertEqual(200, answer.status, answer.data)
        self.assertIs(True, answer.data['changed'])
        self.assertEqual({'blocked': True, 'timed_out': False}, {key: answer.data['wait'][key]
                                                                  for key in ('blocked', 'timed_out')})
        self.assertLess(box['seconds'], 8)

    def test_the_timeout_answers_unchanged_and_a_baseline_never_blocks(self):
        since = self.baseline()['fingerprint']
        started = time.monotonic()
        answer = self.wait(query='&comments=others&timeout=1&since=' + since)
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        self.assertIs(False, answer.data['changed'])
        self.assertIs(True, answer.data['wait']['timed_out'])
        self.assertEqual(since, answer.data['fingerprint'])
        started = time.monotonic()
        self.wait(query='&comments=others&timeout=30')
        self.assertLess(time.monotonic() - started, 5)

    def test_notes_wake_the_blocking_wait(self):
        since = self.baseline()['fingerprint']
        with mock.patch.object(http_service.ApiHandler, '_notes_waking',
                               return_value=[]) as notes:
            thread, box = self.in_thread(lambda: self.wait(query='&comments=others&timeout=20&since=' + since))
            time.sleep(0.5)
            note = {'id': 'note_1', 'to': {'project': self.project}, 'from': {'project': 'other'},
                    'created_at': '2026-01-01T00:00:00Z', 'reply_to': None, 'thread': 'note_1'}
            notes.return_value = [note]
            thread.join(10)
        self.assertEqual(200, box['answer'].status, box['answer'].data)
        self.assertIs(True, box['answer'].data['changed'])
        self.assertEqual(['note_1'], [row['note'] for row in box['answer'].data['coordinator_notes']])

    def test_bad_values_are_refused(self):
        since = '&since=' + waiting.fingerprint([])
        for query in ('&timeout=51', '&timeout=-1', '&timeout=x', '&timeout=', '&timeout=1.5', '&timeout=+5',
                      '&timeout=5&timeout=5', '&comments=all', '&comments=', '&comments=others&comments=others'):
            with self.subTest(query=query):
                refused = self.wait(query=query + since)
                self.assertEqual(400, refused.status, refused.data)

    def test_a_revoked_grant_ends_the_wait_with_a_refusal(self):
        since = self.baseline()['fingerprint']
        thread, box = self.in_thread(lambda: self.wait(query='&comments=others&timeout=30&since=' + since))
        time.sleep(0.5)
        self.assertEqual(200, self.grant(method='DELETE').status)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(403, box['answer'].status)
        self.assertLess(box['seconds'], 8)

    def test_a_revoked_credential_ends_the_wait(self):
        since = self.baseline()['fingerprint']
        thread, box = self.in_thread(lambda: self.wait(query='&comments=others&timeout=30&since=' + since))
        time.sleep(0.5)
        self.assertEqual(200, self.request('POST', '/v1/agents/%s/disable' % self.agent_id, {},
                                           token=self.tokens['cora']).status)
        thread.join(10)
        self.assertIn(box['answer'].status, (401, 403, 404))

    def test_caps_answer_at_once_with_todays_read(self):
        since = self.baseline()['fingerprint']
        second, second_id = self.agent_with_id('cora', 'Egret')
        self.assertEqual(200, self.grant(second_id).status)
        second_since = self.baseline(second)['fingerprint']
        with mock.patch.multiple(http_service.ApiHandler, WAIT_GLOBAL_CAP=1):
            thread, box = self.in_thread(lambda: self.wait(query='&comments=others&timeout=3&since=' + since))
            time.sleep(0.5)
            # Same credential again, and another credential over the global cap.
            for token, mark in ((self.agent, since), (second, second_since)):
                started = time.monotonic()
                answer = self.wait(token, '&comments=others&timeout=30&since=' + mark)
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(200, answer.status, answer.data)
                self.assertEqual({'blocked': False, 'reason': 'busy'}, answer.data['wait'])
                self.assertIs(False, answer.data['changed'])
            thread.join(10)
        self.assertIs(True, box['answer'].data['wait']['timed_out'])
        # The slot is back once the wait has ended.
        self.assertIs(True, self.wait(query='&comments=others&timeout=1&since=' + since).data['wait']['blocked'])

    def test_a_client_that_hangs_up_ends_the_loop_and_frees_its_slot(self):
        since = self.baseline()['fingerprint']
        calls = []
        real = self.backend.coordinator_wait

        def counting(*args, **kwargs):
            calls.append(time.monotonic())
            return real(*args, **kwargs)
        with mock.patch.object(self.backend, 'coordinator_wait', side_effect=counting):
            connection = socket.create_connection(('127.0.0.1', self.port))
            connection.sendall(('GET %s&timeout=30&since=%s HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\n\r\n'
                                % (WAIT + '&comments=others', since, self.agent)).encode())
            time.sleep(0.7)
            connection.close()
            time.sleep(0.8)
            settled = len(calls)
            time.sleep(1.0)
            self.assertEqual(settled, len(calls))
        # Its slot was released: the same credential can wait again.
        self.assertIs(True, self.wait(query='&comments=others&timeout=1&since=' + since).data['wait']['blocked'])

    def test_comments_of_a_project_the_caller_does_not_coordinate_never_appear(self):
        beta = self.create_project(self.admin, 'Beta')
        self.assertEqual(200, self.member('cora', 'coordinator', project=beta, token=self.admin).status)
        secret, agent_id = self.agent_with_id('cora', 'Two', projects=[self.project, beta])
        self.assertEqual(200, self.grant(agent_id).status)
        before = self.baseline(secret)
        task = self.request('POST', '/v1/projects/%s/tasks' % beta, {'title': 'secret beta'},
                            token=self.admin).data['id']
        made = self.request('POST', '/v1/projects/%s/tasks/%s/comments' % (beta, task), {'text': 'hidden'},
                            token=self.admin)
        self.assertEqual(201, made.status, made.data)
        after = self.baseline(secret)
        self.assertEqual(before['fingerprint'], after['fingerprint'])
        self.assertNotIn(beta, json.dumps(after))
        self.assertNotIn(task, json.dumps(after))


class EndpointReadTests(unittest.TestCase):
    """The canonical endpoint read (work.execute) the HTTP route and a host form share."""

    ME = 'agent_me'

    def row(self, ident, comments, status='open'):
        return {'id': ident, 'title': 'Task ' + ident, 'issue_type': 'task', 'status': status,
                'assignee': None, 'description': 'Intent', 'acceptance_criteria': 'Acceptance',
                'labels': [], 'priority': 2, 'created_by': 'x', 'created_at': '2026-09-01T00:00:00Z',
                'updated_at': '2026-09-15T09:00:00Z', 'dependencies': [], 'comments': comments}

    def comment(self, ident, author, minute, text='hello'):
        return {'id': ident, 'issue_id': 't', 'author': author, 'text': text,
                'created_at': '2026-09-15T10:%02d:00Z' % minute}

    def read(self, rows, *flags):
        import shutil
        import tempfile
        import work
        root = Path(tempfile.mkdtemp(prefix='blocking-wait-'))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        project = root / 'projects' / 'p'
        project.mkdir(parents=True)
        lines = '\n'.join(json.dumps(row) for row in rows)
        return work.execute(project, self.ME, 'wait', ['--coordinator', '--idle-minutes', '15'] + list(flags),
                            {}, lambda _argv: lines, operators=[self.ME])

    def test_the_flag_adds_pointers_without_text_and_folds_them_in_only_when_asked(self):
        rows = [self.row('t1', [self.comment('1', 'carl', 1, 'secret words'),
                                self.comment('2', self.ME, 2), self.comment('3', self.ME + '/lane', 3)]),
                self.row('t2', [self.comment('1', 'drew', 4)], status='closed')]
        plain = self.read(rows)
        asked = self.read(rows, '--comments-others')
        self.assertNotIn('comments', plain)
        self.assertEqual([('t1', '1', 'carl'), ('t2', '1', 'drew')],
                         [(r['task'], r['comment'], r['author']) for r in asked['comments']])
        self.assertNotIn('secret words', json.dumps(asked))
        self.assertNotEqual(plain['fingerprint'], asked['fingerprint'])
        quiet = self.read([self.row('t1', [self.comment('2', self.ME, 2)])], '--comments-others')
        self.assertEqual([], quiet['comments'])
        # The fingerprint of a read without the flag does not move when comments arrive.
        self.assertEqual(plain['fingerprint'], self.read(rows[:1] + [self.row('t2', [])])['fingerprint'])

    def test_the_flag_belongs_to_the_coordinator_form(self):
        with self.assertRaises(ValueError):
            waiting.parse_read_args(['--comments-others'])


if __name__ == '__main__':
    unittest.main()
