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


class WhichCallsWriteTests(unittest.TestCase):
    """Review of the second delivery: `dep add` was listed as a read and `create --help` as a write."""

    def test_dep_writes_and_reads(self):
        writes = http_authority.is_mutating_invocation
        for argv in (['dep', 'add', 'pp-1', 'pp-2'], ['dep', 'remove', 'pp-1', 'pp-2'], ['dep', 'relate', 'pp-1', 'pp-2'],
                     ['dep', 'unrelate', 'pp-1', 'pp-2'], ['dep', 'pp-1', '--blocks', 'pp-2'],
                     ['dep', 'add', 'pp-1', 'pp-2', '--type', 'blocks']):
            with self.subTest(argv=argv):
                self.assertTrue(writes(argv))
        for argv in (['dep', 'list', 'pp-1'], ['dep', 'tree', 'pp-1'], ['dep', 'cycles'], ['dep', 'list', 'pp-1', '--json']):
            with self.subTest(argv=argv):
                self.assertFalse(writes(argv))

    def test_help_writes_nothing_and_a_value_that_looks_like_the_flag_is_not_help(self):
        writes = http_authority.is_mutating_invocation
        for argv in (['create', '--help'], ['create', '-h'], ['update', 'pp-1', '--help'], ['close', 'pp-1', '-h'],
                     ['comments', 'add', 'pp-1', 'text', '--help'], ['dep', 'add', 'pp-1', 'pp-2', '--help'],
                     ['create', '--title', 'x', '--help']):
            with self.subTest(argv=argv):
                self.assertFalse(writes(argv))
        for argv in (['create', '--title', '-h'], ['create', '--title', '--help'], ['create', '--title', 'x', '--', '--help'],
                     ['comments', 'add', 'pp-1', '--', '-h']):
            with self.subTest(argv=argv):
                self.assertTrue(writes(argv))

    def test_the_rest_is_what_it_was(self):
        writes = http_authority.is_mutating_invocation
        for argv, expected in ((['list', '--json'], False), (['show', 'pp-1'], False), (['export', '--all'], False),
                               (['create', '--title', 'x'], True), (['create', '--title', 'x', '--dry-run'], False),
                               (['create', '--title', 'x', '--dry-run=false'], True), (['update', 'pp-1', '--claim'], True),
                               (['comments', 'add', 'pp-1', 'x'], True), (['comments', 'pp-1'], False),
                               (['merge-slot', 'check'], False), (['merge-slot', 'acquire'], True), (['close', 'pp-1'], True),
                               ([], True), ('create', True)):
            with self.subTest(argv=argv):
                self.assertEqual(writes(argv), expected)


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
        # The stored answer, whole, and marked as one.
        self.assertEqual(again, dict(first, replayed=True))
        self.assertNotIn('replayed', first)
        self.assertEqual(other['server_time'], '2026-10-06T09:00:00+00:00')

    def test_a_stored_answer_without_a_time_is_replayed_without_one_and_marked(self):
        """An answer the previous kit stored: this kit must not put a time on it, and says it is a stored answer."""
        def effect():
            return {'returncode': 0, 'stdout': '{"id": "p-1"}', 'stderr': ''}
        with mock.patch.object(http_authority, 'stamp_write', lambda envelope: envelope):      # as the previous kit wrote it
            first = self.guarded(effect, operation_id='op-00000004')
        again = self.guarded(effect, operation_id='op-00000004')
        self.assertNotIn('server_time', first)
        self.assertEqual(again, dict(first, replayed=True))

    def test_an_effect_that_wrote_outside_bd_is_stamped_and_its_refusals_are_what_they_were(self):
        runner = http_authority.NativeRunner(lambda argv: {'returncode': 0, 'stdout': '[]', 'stderr': ''})
        self.assertFalse(runner.wrote)

        def journal_write():
            runner(['show', 'p-1', '--json'])
            runner.wrote = True
            return {'returncode': 0, 'stdout': '{}', 'stderr': ''}
        answer = self.guarded(journal_write, runner=runner)
        self.assertRegex(answer['server_time'], SHAPE)
        self.assertFalse(runner.attempted_write)                      # what decides a refusal is untouched
        refusing = http_authority.NativeRunner(lambda argv: {'returncode': 0, 'stdout': '[]', 'stderr': ''})

        def refuses():
            raise ValueError('not allowed')
        with self.assertRaises(ValueError):
            self.guarded(refuses, runner=refusing, operation_id='op-00000005')


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointTests(unittest.TestCase):
    """The writes of the endpoint that are not guarded writes: the session registry, an acknowledgement, feedback."""

    def run_action(self, action, args, reconciled=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'projects' / 'pp' / '.beads').mkdir(parents=True)
            (root / 'projects' / 'pp' / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
            import feedback
            import guidance
            import sessions
            with mock.patch.object(sessions, 'execute', return_value={'ok': True, 'reconciled': reconciled}), \
                    mock.patch.object(guidance, 'acknowledge', return_value={'acknowledged': 'v1', 'reconciled': reconciled}), \
                    mock.patch.object(guidance, 'read', return_value={'text': 'g'}), \
                    mock.patch.object(guidance, 'state', return_value={'version': 'v1'}), \
                    mock.patch.object(feedback, 'execute', return_value={'ok': True, 'reconciled': reconciled}), \
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
                ('session', ['resume', '--request-id', uuid], True),          # it records the resume
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

    def test_a_request_that_was_already_recorded_carries_none(self):
        """The same registration, acknowledgement or entry again writes nothing: the kit says `reconciled`."""
        uuid = '0b0f6f0c-1a55-4f0e-9d2b-5d7a3c1e9f10'
        for action, args in (('session', ['register', '--name', 'n', '--request-id', uuid]),
                             ('session', ['resume', '--request-id', uuid]),
                             ('session', ['run', 'start', '--run-id', 'r1']),
                             ('guidance', ['ack', '--version', 'v1']),
                             ('feedback', ['add', '--file', '@a']), ('feedback', ['correct', '--file', '@a'])):
            with self.subTest(action=action, args=args[:2]):
                answer = self.run_action(action, args, reconciled=True)
                self.assertEqual(answer['returncode'], 0)
                self.assertNotIn('server_time', answer)

    def handoff(self, result, action='handoff', args=('pp-1', '@attachment:a')):
        import work
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'projects' / 'pp' / '.beads').mkdir(parents=True)
            (root / 'projects' / 'pp' / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
            (root / 'deployment.private.json').write_text('{"password": "x", "operators": ["ops"]}', encoding='utf-8')
            request = {'project': 'pp', 'actor': 'worker', 'action': action, 'args': list(args),
                       'attachments': {'a': {'flag': '--file', 'text': '{"task": "pp-1"}'}}}
            with mock.patch.object(work, 'execute', return_value=result):
                return endpoint.execute(root, request)

    def test_a_handoff_that_was_recorded_is_stamped_though_bd_was_not_written(self):
        """Review of the second delivery: a request and a decline write the kit's handoff journal only."""
        recorded = {'task': 'pp-1', 'operation_id': 'move-1', 'reconciled': False}
        self.assertRegex(self.handoff(recorded)['server_time'], SHAPE)
        # The same request again is already recorded; the read of a task's handoffs is a read.
        self.assertNotIn('server_time', self.handoff(dict(recorded, reconciled=True)))
        self.assertNotIn('server_time', self.handoff({'task': 'pp-1', 'handoffs': []}, args=('pp-1',)))
        self.assertNotIn('server_time', self.handoff(recorded, args=('pp-1',)))        # whatever a read answers
        # A review and the work read go the same way; only what bd wrote makes them writes.
        self.assertNotIn('server_time', self.handoff({'task': 'pp-1', 'reconciled': False}, action='review', args=('pp-1',)))
        self.assertNotIn('server_time', self.handoff(recorded, action='review'))
        self.assertNotIn('server_time', self.handoff({'next_actions': []}, action='work', args=('--json',)))

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

    def test_a_time_an_earlier_request_left_behind_is_not_given_to_the_next_write(self):
        """On one connection the same thread serves request after request. A write that took the endpoint's
        time and then failed must not hand that time to the next write."""
        import http.client
        real = self.service.create_user
        left = []

        def fails_after_an_endpoint_write(*args, **kwargs):
            if not left:
                left.append(True)
                http_service.WRITTEN.at = '2026-01-01T00:00:00+00:00'     # in the serving thread
                raise http_service.conflict('refused after the endpoint had answered')
            return real(*args, **kwargs)
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        self.addCleanup(connection.close)
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.admin}

        def post(username):
            connection.request('POST', '/v1/accounts', body=json.dumps({'username': username}), headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        with mock.patch.object(self.service, 'create_user', side_effect=fails_after_an_endpoint_write),                 mock.patch.object(self.store, 'now', return_value=1791273012.0):
            self.assertEqual(post('amy')[0], 409)
            status, made = post('amy')                            # the same connection, so the same thread
        self.assertEqual(left, [True])
        self.assertEqual((status, made['server_time']), (201, '2026-10-06T07:50:12+00:00'))

class HeaderTests(test_http_agents.AgentHarness):
    """`X-Server-Time`: the same value, on every carried-out write, whatever the shape of the body."""

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.project = self.create_project(self.admin, 'Alpha')
        made = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'a task'}, token=self.admin)
        self.task = made.data['id']
        self.made = made

    def stamp(self, answer):
        return answer.headers.get('x-server-time')

    def test_an_object_answer_has_the_header_with_the_value_of_its_field(self):
        self.assertEqual(self.made.status, 201)
        self.assertRegex(self.stamp(self.made), '^%s$' % SHAPE.pattern)
        self.assertEqual(self.stamp(self.made), self.made.data['server_time'])
        self.assertEqual(http_service.SERVER_TIME_HEADER, 'X-Server-Time')

    def test_reads_refusals_and_a_log_in_have_none(self):
        for answer in (self.request('GET', '/v1/projects/%s/tasks' % self.project, token=self.admin),
                       self.request('GET', '/v1/sessions/current', token=self.admin),
                       self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': ''}, token=self.admin),
                       self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'x'}),            # 401
                       self.request('POST', '/v1/sessions', {'username': test_http_agents.ADMIN,
                                                             'password': test_http_agents.ADMIN_PASSWORD}),
                       self.request('GET', '/healthz')):
            with self.subTest(status=answer.status):
                self.assertIsNone(self.stamp(answer))

    def list_answers(self):
        """The endpoint backend answers a task change with the list bd prints: stand in for it."""
        real = self.backend.invoke

        def invoke(route, *args, **kwargs):
            result = real(route, *args, **kwargs)
            return [result] if route == 'tasks.update' else result
        return mock.patch.object(self.backend, 'invoke', invoke)

    def test_a_list_answer_has_the_header_and_its_retry_has_the_time_of_the_write(self):
        path = '/v1/projects/%s/tasks/%s' % (self.project, self.task)
        with self.list_answers():
            with mock.patch.object(self.store, 'now', return_value=1791273012.0):
                first = self.request('PATCH', path, {'title': 'renamed', 'version': 1}, token=self.admin, key='change-key-0001')
            with mock.patch.object(self.store, 'now', return_value=1791273072.0):
                again = self.request('PATCH', path, {'title': 'renamed', 'version': 1}, token=self.admin, key='change-key-0001')
                other = self.request('PATCH', path, {'title': 'renamed twice', 'version': 2}, token=self.admin, key='change-key-0002')
        self.assertEqual((first.status, type(first.data)), (200, list))
        self.assertEqual(self.stamp(first), '2026-10-06T07:50:12+00:00')
        self.assertEqual((again.status, again.data, self.stamp(again)), (200, first.data, '2026-10-06T07:50:12+00:00'))
        self.assertEqual(self.stamp(other), '2026-10-06T07:51:12+00:00')

    def test_the_header_of_a_list_answer_is_the_endpoints_time_when_an_endpoint_wrote(self):
        """Not the service's clock: the endpoint's envelope said when the write was carried out."""
        real = self.backend.invoke

        def invoke(route, *args, **kwargs):
            result = real(route, *args, **kwargs)
            if route != 'tasks.update':
                return result
            http_service.WRITTEN.at = '2026-10-06T07:49:59+00:00'       # what EndpointBackend._checked takes from the envelope
            return [result]
        path = '/v1/projects/%s/tasks/%s' % (self.project, self.task)
        with mock.patch.object(self.backend, 'invoke', invoke), mock.patch.object(self.store, 'now', return_value=1791273012.0):
            first = self.request('PATCH', path, {'title': 'renamed', 'version': 1}, token=self.admin, key='change-key-0011')
            again = self.request('PATCH', path, {'title': 'renamed', 'version': 1}, token=self.admin, key='change-key-0011')
        self.assertEqual((type(first.data), self.stamp(first), self.stamp(again)),
                         (list, '2026-10-06T07:49:59+00:00', '2026-10-06T07:49:59+00:00'))

    def test_a_stored_answer_of_the_endpoint_that_has_no_time_gets_none_from_the_service(self):
        """The upgrade window: a write made through the previous kit, sent again through this one with the
        service's own stored answer gone. The endpoint replays what it stored, without a time; the service's
        clock would be the time of the retry."""
        reply = {'returncode': 0, 'stdout': '{"id": "pp-1"}\n', 'stderr': '', 'replayed': True}
        self.assertEqual(http_service.EndpointBackend._checked(reply), {'id': 'pp-1'})
        self.assertIsNone(http_service.written_at(self.service))
        self.assertRegex(http_service.written_at(self.service), SHAPE)          # used once: the next write has the clock
        # A replayed answer that has its time keeps it.
        http_service.EndpointBackend._checked(dict(reply, server_time='2026-10-06T07:50:12+00:00'))
        self.assertEqual(http_service.written_at(self.service), '2026-10-06T07:50:12+00:00')
        real = self.backend.invoke

        def replays(route, *args, **kwargs):
            result = real(route, *args, **kwargs)
            if route == 'tasks.create':
                http_service.WRITTEN.at = False                               # what _checked leaves for such an answer
            return result
        path = '/v1/projects/%s/tasks' % self.project
        with mock.patch.object(self.backend, 'invoke', replays):
            first = self.request('POST', path, {'title': 'made by the previous kit'}, token=self.admin, key='old-key-0101')
        again = self.request('POST', path, {'title': 'made by the previous kit'}, token=self.admin, key='old-key-0101')
        for answer in (first, again):
            self.assertEqual(answer.status, 201)
            self.assertNotIn('server_time', answer.data)
            self.assertIsNone(self.stamp(answer))

    def test_an_answer_that_carries_its_own_time_keeps_it_in_the_body_and_the_header(self):
        real = self.backend.invoke

        def with_its_time(route, *args, **kwargs):
            result = real(route, *args, **kwargs)
            return dict(result, server_time='2026-10-06T07:49:59+00:00') if route == 'tasks.create' else result
        with mock.patch.object(self.backend, 'invoke', with_its_time), mock.patch.object(self.store, 'now', return_value=1791273012.0):
            made = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'x'}, token=self.admin)
        self.assertEqual((made.data['server_time'], self.stamp(made)), ('2026-10-06T07:49:59+00:00', '2026-10-06T07:49:59+00:00'))

    def test_the_stored_answer_has_the_time_where_it_is_not_the_answer_that_was_sent(self):
        """An agent is made: the answer carries its secret once, what is stored for a retry does not. Both have the time."""
        body = {'name': 'Kestrel', 'working_directory': '/home/priya/work/kestrel'}
        with mock.patch.object(self.store, 'now', return_value=1791273012.0):
            first = self.request('POST', '/v1/agents', body, token=self.admin, key='agent-key-0001')
        with mock.patch.object(self.store, 'now', return_value=1791273072.0):
            again = self.request('POST', '/v1/agents', body, token=self.admin, key='agent-key-0001')
        self.assertEqual((first.status, first.data['server_time'], self.stamp(first)),
                         (201, '2026-10-06T07:50:12+00:00', '2026-10-06T07:50:12+00:00'))
        self.assertIn(again.status, (200, 201))
        self.assertNotEqual(again.data, first.data)                    # the secret is not delivered twice
        self.assertEqual((again.data.get('server_time'), self.stamp(again)),
                         ('2026-10-06T07:50:12+00:00', '2026-10-06T07:50:12+00:00'))

    def test_the_retry_of_an_object_answer_has_the_header_of_the_write(self):
        path = '/v1/projects/%s/tasks' % self.project
        with mock.patch.object(self.store, 'now', return_value=1791273012.0):
            first = self.request('POST', path, {'title': 'keyed'}, token=self.admin, key='task-key-0009')
        with mock.patch.object(self.store, 'now', return_value=1791273072.0):
            again = self.request('POST', path, {'title': 'keyed'}, token=self.admin, key='task-key-0009')
        self.assertEqual(self.stamp(first), '2026-10-06T07:50:12+00:00')
        self.assertEqual((again.data, self.stamp(again)), (first.data, '2026-10-06T07:50:12+00:00'))

    def test_an_answer_stored_before_the_time_was_kept_beside_it(self):
        """Its retry has the header from an object body's own field, and none where the body has no field."""
        real = self.service.idempotency_commit

        def as_before(digest, status, response, written_at=None):
            return real(digest, status, response)
        tasks = '/v1/projects/%s/tasks' % self.project
        change = '%s/%s' % (tasks, self.task)
        with mock.patch.object(self.service, 'idempotency_commit', as_before), self.list_answers():
            first = self.request('POST', tasks, {'title': 'old kit'}, token=self.admin, key='old-key-0001')
            listed = self.request('PATCH', change, {'title': 'old kit list', 'version': 1}, token=self.admin, key='old-key-0002')
        with self.list_answers():
            again = self.request('POST', tasks, {'title': 'old kit'}, token=self.admin, key='old-key-0001')
            listed_again = self.request('PATCH', change, {'title': 'old kit list', 'version': 1}, token=self.admin, key='old-key-0002')
        self.assertEqual(self.stamp(again), first.data['server_time'])
        self.assertEqual((type(listed.data), listed_again.data), (list, listed.data))
        self.assertIsNone(self.stamp(listed_again))

    def test_a_header_does_not_stay_for_the_next_request_on_the_connection(self):
        import http.client
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        self.addCleanup(connection.close)
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.admin}
        seen = []
        for method, path, body in (('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'one'}),
                                   ('GET', '/v1/projects/%s/tasks' % self.project, None),
                                   ('POST', '/v1/projects/%s/tasks' % self.project, {'title': ''}),
                                   ('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'two'})):
            connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
            response = connection.getresponse()
            response.read()
            seen.append((response.status, response.getheader('X-Server-Time') is not None))
        self.assertEqual([present for _, present in seen], [True, False, False, True], seen)

    def test_the_stored_time_is_read_only_for_a_committed_answer_of_that_request(self):
        principal = self.service.authenticate(self.admin)
        self.assertIsNone(self.service.idempotency_written_at(principal, self.project, 'tasks.create x', None))
        self.assertIsNone(self.service.idempotency_written_at(principal, self.project, 'tasks.create x', 'no-such-key-0001'))

if __name__ == '__main__':
    unittest.main()
