"""The server's time in the answer of every write (kittrial-5bb.97, item 3).

A record that cites when something happened should cite the server's clock, not the
worker's. A write that was carried out answers with ``server_time`` (UTC with its offset,
whole seconds), once, at the top level: of the endpoint's envelope, which the client prints
as one line on standard error, and of the HTTP body. A replayed write returns the time it
was carried out. A refusal, a busy or uncertain answer, and a read carry none.
"""
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import client
import http_authority
import http_service
import test_http_agents

try:
    import endpoint
except ImportError:  # endpoint needs fcntl (POSIX)
    endpoint = None

SHAPE = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00')


class ClockTests(unittest.TestCase):
    def test_the_time_is_utc_with_its_offset_in_whole_seconds(self):
        self.assertEqual(http_authority.server_time(1791273012.987), '2026-10-06T07:50:12+00:00')
        self.assertRegex(http_authority.server_time(), '^%s$' % SHAPE.pattern)

    def test_only_a_successful_answer_is_stamped_and_a_stamp_is_kept(self):
        with mock.patch.object(http_authority, 'server_time', return_value='2026-10-06T07:50:12+00:00'):
            for code in (0, None):
                answer = {'returncode': code, 'stdout': 'x', 'stderr': ''} if code is not None else {'stdout': 'x'}
                self.assertIs(http_authority.stamp_write(answer), answer)
                self.assertEqual(answer['server_time'], '2026-10-06T07:50:12+00:00')
            for code in (2, 75, 124, 126, 1):
                answer = {'returncode': code, 'stdout': '', 'stderr': 'no'}
                self.assertNotIn('server_time', http_authority.stamp_write(answer))
            kept = {'returncode': 0, 'stdout': '', 'stderr': '', 'server_time': '2026-01-01T00:00:00+00:00'}
            self.assertEqual(http_authority.stamp_write(kept)['server_time'], '2026-01-01T00:00:00+00:00')
            self.assertEqual(http_authority.stamp_write('not an envelope'), 'not an envelope')


class GuardedWriteTests(unittest.TestCase):
    def guarded(self, effect, runner=None, operation_id=None, journal=None):
        request = {'project': 'p', 'actor': 'worker', 'action': 'bd', 'args': ['x']}
        if operation_id:
            request['operation_id'] = operation_id
        return http_authority.run_guarded(request, journal or Path(self.tmp) / 'journal.sqlite3', effect, runner=runner)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = directory.name

    def test_a_write_is_stamped_and_a_guarded_read_is_not(self):
        def answer():
            return {'returncode': 0, 'stdout': '{}', 'stderr': ''}
        self.assertRegex(self.guarded(answer)['server_time'], SHAPE)                 # no runner: a write action
        wrote = http_authority.NativeRunner(lambda argv: {'returncode': 0, 'stdout': '', 'stderr': ''})

        def writes():
            wrote(['create', '--title', 'x'])
            return answer()
        self.assertRegex(self.guarded(writes, runner=wrote)['server_time'], SHAPE)
        read = http_authority.NativeRunner(lambda argv: {'returncode': 0, 'stdout': '[]', 'stderr': ''})

        def reads():
            read(['list', '--json'])
            return answer()
        self.assertNotIn('server_time', self.guarded(reads, runner=read))
        dry = http_authority.NativeRunner(lambda argv: {'returncode': 0, 'stdout': '', 'stderr': ''})

        def preflight():
            dry(['create', '--title', 'x', '--dry-run'])
            return answer()
        self.assertNotIn('server_time', self.guarded(preflight, runner=dry))

    def test_a_refusal_and_an_uncertain_answer_carry_none(self):
        self.assertNotIn('server_time', self.guarded(lambda: {'returncode': 2, 'stdout': '', 'stderr': 'no\n'}))
        self.assertNotIn('server_time', self.guarded(lambda: {'returncode': 124, 'stdout': '', 'stderr': '?\n'}))

        def breaks():
            raise OSError('disk')
        broken = self.guarded(breaks, operation_id='op-00000001')
        self.assertEqual(broken['returncode'], 124)
        self.assertNotIn('server_time', broken)

    def test_the_same_request_sent_again_is_answered_with_the_time_of_the_write(self):
        calls = []

        def effect():
            calls.append(1)
            return {'returncode': 0, 'stdout': '{"id": "p-1"}', 'stderr': ''}
        with mock.patch.object(http_authority, 'server_time', return_value='2026-10-06T07:50:12+00:00'):
            first = self.guarded(effect, operation_id='op-00000002')
        with mock.patch.object(http_authority, 'server_time', return_value='2026-10-06T09:00:00+00:00'):
            again = self.guarded(effect, operation_id='op-00000002')
            other = self.guarded(effect, operation_id='op-00000003')
        self.assertEqual(len(calls), 2)                               # the retry ran nothing
        self.assertEqual(first['server_time'], '2026-10-06T07:50:12+00:00')
        self.assertEqual(again, first)
        self.assertEqual(other['server_time'], '2026-10-06T09:00:00+00:00')


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointTests(unittest.TestCase):
    """The writes of the endpoint that are not guarded writes: the session registry, an acknowledgement, feedback."""

    def run_action(self, action, args):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'projects' / 'pp' / '.beads').mkdir(parents=True)
            (root / 'projects' / 'pp' / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
            import feedback
            import guidance
            import sessions
            with mock.patch.object(sessions, 'execute', return_value={'ok': True}), \
                    mock.patch.object(guidance, 'acknowledge', return_value={'acknowledged': 'v1'}), \
                    mock.patch.object(guidance, 'read', return_value={'text': 'g'}), \
                    mock.patch.object(guidance, 'state', return_value={'version': 'v1'}), \
                    mock.patch.object(feedback, 'execute', return_value={'ok': True}), \
                    mock.patch.object(endpoint, 'report', return_value={}):
                return endpoint.execute(root, {'project': 'pp', 'actor': 'worker', 'action': action, 'args': args})

    def test_which_of_them_are_stamped(self):
        uuid = '0b0f6f0c-1a55-4f0e-9d2b-5d7a3c1e9f10'
        for action, args, written in (
                ('session', ['register', '--name', 'n', '--request-id', uuid], True),
                ('session', ['run', 'start', '--run-id', 'r1'], True),
                ('session', ['run', 'heartbeat', '--run-id', 'r1'], True),
                ('session', ['run', 'end', '--run-id', 'r1', '--status', 'succeeded'], True),
                ('session', ['run', 'status', '--run-id', 'r1'], False),
                ('session', ['show', 'worker'], False),
                ('session', ['resume', '--request-id', uuid], False),
                ('guidance', ['ack', '--version', 'v1'], True),
                ('guidance', ['get'], False),
                ('guidance', ['version'], False),
                ('feedback', ['add', '--file', '@a'], True),
                ('feedback', ['correct', '--file', '@a'], True),
                ('feedback', ['list'], False)):
            with self.subTest(action=action, args=args[:2]):
                answer = self.run_action(action, args)
                self.assertEqual(answer['returncode'], 0)
                if written:
                    self.assertRegex(answer['server_time'], SHAPE)
                else:
                    self.assertNotIn('server_time', answer)

    def test_the_envelope_printed_by_main_carries_it(self):
        printed = io.StringIO()
        stamped = {'returncode': 0, 'stdout': '{}\n', 'stderr': '', 'server_time': '2026-10-06T07:50:12+00:00'}
        with mock.patch.object(endpoint, 'execute', return_value=stamped), \
                mock.patch.object(sys, 'argv', ['endpoint.py', '--root', '/srv/rt']), \
                mock.patch.object(sys, 'stdin', io.StringIO('{"project": "pp", "action": "bd", "args": []}')), \
                mock.patch.object(sys, 'stdout', printed):
            endpoint.main()
        self.assertEqual(json.loads(printed.getvalue()), stamped)


class ClientTests(unittest.TestCase):
    def run_client(self, result, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'client.json'
            config.write_text('{}', encoding='utf-8')
            argv = ['client.py', '--config', str(config), '--project', 'pp', '--actor', 'worker', '--', *arguments]
            with mock.patch.object(client, 'request', return_value=dict(result)), mock.patch.object(sys, 'argv', argv), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = client.main()
        return code, out.getvalue(), err.getvalue()

    def test_a_write_prints_one_line_on_standard_error_and_standard_output_is_what_it_was(self):
        receipt = '{"id": "pp-1"}\n'
        plain = self.run_client({'returncode': 0, 'stdout': receipt, 'stderr': ''}, 'create', '--title', 'x', '--json')
        stamped = self.run_client({'returncode': 0, 'stdout': receipt, 'stderr': '', 'server_time': '2026-10-06T07:50:12+00:00'},
                                  'create', '--title', 'x', '--json')
        self.assertEqual(plain, (0, receipt, ''))                     # an older endpoint: nothing is printed
        self.assertEqual(stamped, (0, receipt, 'server_time: 2026-10-06T07:50:12+00:00\n'))
        # After the endpoint's own warnings, which stay as they were.
        warned = self.run_client({'returncode': 0, 'stdout': receipt, 'stderr': 'warning: x\n',
                                  'server_time': '2026-10-06T07:50:12+00:00'}, 'create', '--title', 'x')
        self.assertEqual(warned[2], 'warning: x\nserver_time: 2026-10-06T07:50:12+00:00\n')

    def test_nothing_is_printed_for_a_refusal_or_for_a_value_that_is_not_a_time(self):
        for result in ({'returncode': 2, 'stdout': '', 'stderr': 'no\n', 'server_time': '2026-10-06T07:50:12+00:00'},
                       {'returncode': 0, 'stdout': 'x\n', 'stderr': '', 'server_time': 'rm -rf /\x1b[2J'},
                       {'returncode': 0, 'stdout': 'x\n', 'stderr': '', 'server_time': 17},
                       {'returncode': 0, 'stdout': 'x\n', 'stderr': '', 'server_time': None}):
            with self.subTest(result=result):
                code, out, err = self.run_client(result, 'create', '--title', 'x')
                self.assertNotIn('server_time', err)
                self.assertEqual(out, result['stdout'])

    def test_a_capture_file_holds_the_output_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'out.json'
            config = Path(tmp) / 'client.json'
            config.write_text('{}', encoding='utf-8')
            err = io.StringIO()
            argv = ['client.py', '--config', str(config), '--project', 'pp', '--actor', 'worker', '--out', str(target),
                    '--', 'create', '--title', 'x', '--json']
            answer = {'returncode': 0, 'stdout': '{"id": "pp-1"}\n', 'stderr': '', 'server_time': '2026-10-06T07:50:12+00:00'}
            with mock.patch.object(client, 'request', return_value=answer), mock.patch.object(sys, 'argv', argv), \
                    contextlib.redirect_stderr(err):
                self.assertEqual(client.main(), 0)
            self.assertEqual(target.read_text(encoding='utf-8'), '{"id": "pp-1"}\n')
            self.assertEqual(err.getvalue(), 'server_time: 2026-10-06T07:50:12+00:00\n')


class HttpTests(test_http_agents.AgentHarness):
    """Over HTTP: at the top level of the body of a write that was carried out."""

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.project = self.create_project(self.admin, 'Alpha')

    def test_a_write_carries_it_and_a_read_and_a_refusal_do_not(self):
        made = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'a task'}, token=self.admin,
                            key='task-key-0001')
        self.assertEqual(201, made.status, made.data)
        self.assertRegex(made.data['server_time'], '^%s$' % SHAPE.pattern)
        self.assertEqual(list(made.data).count('server_time'), 1)
        for path in ('/v1/projects/%s/tasks' % self.project, '/v1/projects/%s' % self.project, '/v1/projects',
                     '/v1/sessions/current'):
            read = self.request('GET', path, token=self.admin)
            self.assertEqual(200, read.status)
            self.assertNotIn('server_time', json.dumps(read.data))
        refused = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': ''}, token=self.admin)
        self.assertGreaterEqual(refused.status, 400)
        self.assertNotIn('server_time', json.dumps(refused.data))

    def test_the_time_is_the_services_clock_where_no_endpoint_stands_behind_it(self):
        with mock.patch.object(self.store, 'now', return_value=1791273012.4):
            made = self.request('POST', '/v1/accounts', {'username': 'zoe'}, token=self.admin, key='account-key-0001')
        self.assertEqual((made.status, made.data['server_time']), (201, '2026-10-06T07:50:12+00:00'))

    def test_the_same_request_sent_again_is_answered_with_the_time_of_the_write(self):
        path = '/v1/projects/%s/tasks' % self.project
        with mock.patch.object(self.store, 'now', return_value=1791273012.0):
            first = self.request('POST', path, {'title': 'a task'}, token=self.admin, key='task-key-0002')
        with mock.patch.object(self.store, 'now', return_value=1791273072.0):
            again = self.request('POST', path, {'title': 'a task'}, token=self.admin, key='task-key-0002')
            other = self.request('POST', path, {'title': 'another'}, token=self.admin, key='task-key-0003')
        self.assertEqual((first.status, again.status), (201, 201))
        self.assertEqual(first.data['server_time'], '2026-10-06T07:50:12+00:00')
        self.assertEqual(again.data, first.data)
        self.assertEqual(other.data['server_time'], '2026-10-06T07:51:12+00:00')

    def test_the_endpoints_time_is_passed_through_and_used_once(self):
        """On the endpoint backend the time is the endpoint's: its envelope's, not the service's clock."""
        reply = {'returncode': 0, 'stdout': '{"id": "pp-1"}\n', 'stderr': '', 'server_time': '2026-10-06T07:50:12+00:00'}
        self.assertEqual(http_service.EndpointBackend._checked(reply), {'id': 'pp-1'})
        self.assertEqual(http_service.written_at(self.service), '2026-10-06T07:50:12+00:00')
        with mock.patch.object(self.store, 'now', return_value=1791277200.0):
            self.assertEqual(http_service.written_at(self.service), '2026-10-06T09:00:00+00:00')   # used once
        # A read's envelope has none, and a refusal's is not taken.
        http_service.EndpointBackend._checked({'returncode': 0, 'stdout': '[]\n', 'stderr': ''})
        with self.assertRaises(http_service.HttpError):
            http_service.EndpointBackend._checked({'returncode': 2, 'stdout': '', 'stderr': 'ValueError: no\n',
                                                   'server_time': '2026-10-06T07:50:12+00:00'})
        with mock.patch.object(self.store, 'now', return_value=1791277200.0):
            self.assertEqual(http_service.written_at(self.service), '2026-10-06T09:00:00+00:00')
        # A time an earlier request of this thread left behind is not given to the next write.
        http_service.WRITTEN.at = '2026-01-01T00:00:00+00:00'
        with mock.patch.object(self.store, 'now', return_value=1791273012.0):
            made = self.request('POST', '/v1/accounts', {'username': 'amy'}, token=self.admin)
        self.assertEqual(made.data['server_time'], '2026-10-06T07:50:12+00:00')


if __name__ == '__main__':
    unittest.main()
