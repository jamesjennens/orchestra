"""The blocking coordinator wait over HTTP (kittrial-5bb.457): timeout, comment wakes, caps, authority."""
import http.client
import json
import shutil
import socket
import ssl
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

    def test_the_newest_comment_wakes_even_when_it_ties_and_falls_out_of_the_bound(self):
        import random
        def rows(tasks):
            return [{'id': t, 'comments': [{'id': c, 'author': 'other', 'text': 'x', 'created_at': '2026-10-31T09:00:00Z'}
                                           for c in ids]} for t, ids in tasks.items()]
        def print_of(tasks):
            return waiting.coordinator_view([], {}, waiting.comment_pointers(rows(tasks), 'me'))['fingerprint']
        before = {'t1': [str(n) for n in range(1, 100)]}
        after = {'t1': [str(n) for n in range(1, 101)]}
        # Numeric ids: "100" sorts below "9" as a string and used to fall out of the 20.
        self.assertNotEqual(print_of(before), print_of(after))
        # Random ids, tied time, across tasks: any addition changes the fingerprint, in any order.
        generator = random.Random(457)
        base = {'a': ['%08x' % generator.getrandbits(32) for _ in range(40)], 'b': ['%08x' % generator.getrandbits(32) for _ in range(40)]}
        seen = {print_of(base)}
        for task in ('a', 'b', 'z'):
            grown = {name: list(ids) for name, ids in base.items()}
            grown.setdefault(task, []).append('%08x' % generator.getrandbits(32))
            seen.add(print_of(grown))
        self.assertEqual(4, len(seen))
        self.assertEqual(print_of(base), print_of({'b': base['b'], 'a': list(reversed(base['a']))}))
        # The same count with a different comment outside the bounded list is still a change.
        swapped = {name: list(ids) for name, ids in base.items()}
        swapped['a'][0] = 'ffffffff'
        self.assertNotEqual(print_of(base), print_of(swapped))
        shown = waiting.comment_pointers(rows(after), 'me')
        self.assertEqual(waiting.COMMENT_POINTER_LIMIT, len(shown))
        self.assertEqual(100, shown.total)

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

    def test_the_timeout_bounds_the_whole_request_even_when_each_read_is_slow(self):
        since = self.baseline()['fingerprint']
        real = self.backend.coordinator_wait
        calls = []

        def slow(*args, **kwargs):
            calls.append(time.monotonic())
            time.sleep(0.3)
            return real(*args, **kwargs)
        with mock.patch.object(self.backend, 'coordinator_wait', side_effect=slow):
            started = time.monotonic()
            answer = self.wait(query='&comments=others&timeout=2&since=' + since)
            elapsed = time.monotonic() - started
        self.assertEqual(200, answer.status, answer.data)
        self.assertIs(True, answer.data['wait']['timed_out'])
        # The first read counts: from the route's start the request ends inside its two seconds, not after them.
        self.assertLessEqual(answer.data['wait']['seconds'], 2.4)   # the timeout plus the jitter of one read (it was about 2.6 and up)
        # The interval grows with the cost of a read (four times it), so a slow backend is read less often.
        self.assertLessEqual(len(calls), 3)

    def test_a_read_slower_than_every_earlier_one_can_overrun_and_the_wait_reports_it_truthfully(self):
        since = self.baseline()['fingerprint']
        real = self.backend.coordinator_wait
        calls = []

        def varied(*args, **kwargs):
            calls.append(time.monotonic())
            time.sleep(0.05 if len(calls) == 1 else 2.5)   # the second read is far slower than the first
            return real(*args, **kwargs)
        with mock.patch.object(self.backend, 'coordinator_wait', side_effect=varied):
            answer = self.wait(query='&comments=others&timeout=2&since=' + since)
        self.assertEqual(200, answer.status, answer.data)
        self.assertIs(True, answer.data['wait']['timed_out'])
        # The documented soft bound: the slow read overran the 2 seconds, nothing started another
        # pass after it, and wait.seconds tells the caller how long the request really held.
        self.assertEqual(2, len(calls))
        self.assertGreater(answer.data['wait']['seconds'], 2.0)
        self.assertLess(answer.data['wait']['seconds'], 2.0 + 2.5 + 0.5)

    def test_the_public_text_does_not_promise_a_hard_bound(self):
        root = Path(__file__).resolve().parent.parent
        docs = (root / 'docs' / 'HTTP_DEPLOYMENT.md').read_text(encoding='utf-8')
        page = (root / 'web' / 'js' / 'agentSetup.js').read_text(encoding='utf-8')
        self.assertIn('The bound is soft', docs)
        self.assertNotIn('holds for up to 50 seconds', page)
        self.assertIn('a loaded server can take longer', page)

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


@unittest.skipUnless(shutil.which('openssl'), 'openssl is not installed; the test certificate is made with it')
class TlsHangupTests(unittest.TestCase):
    """The sleep must notice a TLS client that has gone: its close_notify is encrypted and sits in
    front of the EOF, so peeking the raw socket never sees the end."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cert, key = str(Path(tmp.name) / 'c.crt'), str(Path(tmp.name) / 'c.key')
        subprocess.check_call([shutil.which('openssl'), 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
                               '-keyout', key, '-out', cert, '-subj', '/CN=localhost',
                               '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(cert, key)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        client = ssl.create_default_context(cafile=cert).wrap_socket(
            socket.create_connection(listener.getsockname()), server_hostname='localhost', do_handshake_on_connect=False)
        accepted = server.wrap_socket(listener.accept()[0], server_side=True, do_handshake_on_connect=False)
        shaker = threading.Thread(target=accepted.do_handshake)
        shaker.start()
        client.do_handshake()
        shaker.join(10)
        self.addCleanup(accepted.close)
        self.addCleanup(client.close)
        self.client = client
        self.handler = http_service.ApiHandler.__new__(http_service.ApiHandler)
        self.handler.connection = accepted
        self.handler.close_connection = False

    def test_a_silent_client_is_not_gone(self):
        started = time.monotonic()
        self.assertIs(True, self.handler._sleep_unless_gone(0.4))
        self.assertGreaterEqual(time.monotonic() - started, 0.35)

    def test_a_client_that_closes_cleanly_and_disconnects_ends_the_sleep_at_once(self):
        self.client.setblocking(False)
        try:
            self.client.unwrap()                        # sends close_notify
        except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
            pass
        self.client.close()
        started = time.monotonic()
        self.assertIs(False, self.handler._sleep_unless_gone(5.0))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_client_that_just_drops_the_connection_ends_the_sleep_at_once(self):
        self.client.close()
        started = time.monotonic()
        self.assertIs(False, self.handler._sleep_unless_gone(5.0))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_bytes_sent_during_the_wait_do_not_end_it_but_close_the_connection_after_the_answer(self):
        self.client.sendall(b'G')
        started = time.monotonic()
        self.assertIs(True, self.handler._sleep_unless_gone(0.4))
        self.assertGreaterEqual(time.monotonic() - started, 0.35)
        self.assertIs(True, self.handler.close_connection)


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
