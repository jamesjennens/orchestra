"""A wait for a lock that runs out is "busy", not a rejection and not an internal error.

Review 01a109cc of kittrial-5bb.118 part 2: while a creation held the authority lock,
another account's task write answered 500 after 60 seconds. The creation no longer holds
that lock while it works; and wherever a lock wait still runs out, the answer says the
server is busy and that the request may be sent again.
"""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import test_http_agents

try:
    import endpoint
except ImportError:  # endpoint needs fcntl (POSIX)
    endpoint = None


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointTests(unittest.TestCase):
    def answer(self, error):
        printed = io.StringIO()
        with mock.patch.object(endpoint, 'execute', side_effect=error), \
                mock.patch.object(sys, 'argv', ['endpoint.py', '--root', '/srv/rt']), \
                mock.patch.object(sys, 'stdin', io.StringIO('{"project": "pp", "action": "bd", "args": []}')), \
                mock.patch.object(sys, 'stdout', printed):
            endpoint.main()
        return json.loads(printed.getvalue())

    def test_a_lock_wait_that_runs_out_answers_busy(self):
        answer = self.answer(TimeoutError('Timed out waiting for lock /srv/state.json.lock'))
        self.assertEqual(answer['returncode'], 75)
        self.assertEqual(answer['stdout'], '')
        self.assertTrue(answer['stderr'].startswith('Busy: Timed out waiting for lock'), answer['stderr'])
        self.assertIn('Nothing was done; try again shortly.', answer['stderr'])

    def test_a_rejection_keeps_its_own_code(self):
        self.assertEqual(self.answer(ValueError('no'))['returncode'], 2)

    def test_a_configuration_file_that_cannot_be_read_is_marked_as_a_fault_of_the_server(self):
        """kittrial-5bb.156: the line still names the file; the mark lets the web service keep it to its log."""
        import admin
        line = 'Deployment configuration /srv/rt/deployment.private.json is not valid JSON: Expecting value'
        answer = self.answer(admin.ConfigurationUnreadable(line))
        self.assertEqual(answer, {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: %s\n' % line,
                                  'fault': 'configuration'})
        self.assertNotIn('fault', self.answer(ValueError(line)))

    def test_a_configuration_file_cut_short_refuses_a_write_before_anything_is_reserved(self):
        """Seen on real bd (kittrial-5bb.156): read first inside the guarded write, it left the operation
        "outcome unknown" with nothing written, and the same idempotency key answered that until it expired."""
        import tempfile
        import admin
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'projects' / 'pp' / '.beads').mkdir(parents=True)
            (root / 'projects' / 'pp' / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
            (root / 'deployment.private.json').write_text('{"operators": ["ops"', encoding='utf-8')
            before = sorted(str(path) for path in root.rglob('*'))
            for action, args in (('bd', ['create', '--title', 'x', '--json']), ('work', ['--json'])):
                with self.subTest(action=action), self.assertRaises(admin.ConfigurationUnreadable):
                    endpoint.execute(root, {'project': 'pp', 'actor': 'someone', 'action': action, 'args': args,
                                            'operation_id': 'op-0001', 'request_id': 'r1'})
            self.assertEqual(sorted(str(path) for path in root.rglob('*')), before)     # no journal, no lock, nothing


class ConfigurationTests(unittest.TestCase):
    def test_every_reader_of_a_file_cut_short_raises_the_same_error_with_the_words_it_always_had(self):
        import tempfile
        import admin
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / 'deployment.private.json'
            marker.write_text('{"operators": ["ops"', encoding='utf-8')
            for reader in (admin.config, admin.operators, admin.verifiers, admin.review_workflow_writes):
                with self.subTest(reader=reader.__name__), self.assertRaises(admin.ConfigurationUnreadable) as caught:
                    reader(root)
                self.assertIsInstance(caught.exception, ValueError)
                self.assertTrue(str(caught.exception).startswith('Deployment configuration %s is not valid JSON: ' % marker),
                                str(caught.exception))
            marker.write_bytes(b'\xff\xfe\x00{')
            with self.assertRaises(admin.ConfigurationUnreadable) as caught:
                admin.operators(root)
            self.assertIn('Deployment configuration %s is not ' % marker, str(caught.exception))
            # A host command says it in one line, beginning as it always did.
            line = 'Deployment configuration %s is not valid JSON: Expecting value' % marker
            with mock.patch.object(admin, 'main', side_effect=admin.ConfigurationUnreadable(line)), \
                    self.assertRaises(SystemExit) as stopped:
                admin.run_main()
            self.assertEqual(str(stopped.exception), 'ValueError: ' + line)
            # A whole file reads as before.
            marker.write_text('{"operators": ["ops"]}', encoding='utf-8')
            self.assertEqual(admin.config(root), {'operators': ['ops']})


class BackendTests(unittest.TestCase):
    def test_the_busy_code_of_the_endpoint_is_503_busy_with_a_delay(self):
        import contextlib
        line = 'Busy: Timed out waiting for lock /srv/runtime/state.json.lock. Nothing was done; try again shortly.'
        logged = io.StringIO()
        with self.assertRaises(http_service.HttpError) as caught, contextlib.redirect_stderr(logged):
            http_service.EndpointBackend._checked({'returncode': 75, 'stdout': '', 'stderr': line + '\n'}, 'review')
        error = caught.exception
        self.assertEqual((error.status, error.code), (503, 'busy'))
        # The service's own sentence on every route (kittrial-5bb.149): no lock, no path, and
        # no claim that nothing was done.
        self.assertEqual(error.message, 'The server is busy and this request was not completed. Send it again in a moment.')
        self.assertEqual(error.retry_after, 30)
        # Which lock, and for which action, still reaches the operator in the service's log.
        self.assertEqual(logged.getvalue().strip(), 'busy: the endpoint answered return code 75 for review: %r' % line)
        with self.assertRaises(http_service.HttpError) as caught:
            http_service.EndpointBackend._checked({'returncode': 124, 'stdout': '', 'stderr': ''})
        self.assertEqual(caught.exception.code, 'uncertain')


class ServiceTests(test_http_agents.AgentHarness):
    def test_a_state_lock_wait_that_runs_out_answers_503_busy_not_500(self):
        admin = self.admin_token()
        with mock.patch.object(self.service, 'list_projects', side_effect=TimeoutError('Timed out waiting for lock')):
            answer = self.request('GET', '/v1/projects', token=admin)
        self.assertEqual(answer.status, 503, answer.data)
        self.assertEqual(answer.data['error']['code'], 'busy')
        self.assertEqual(answer.data['error']['message'],
                         'The server is busy and this request was not completed. Send it again in a moment.')
        self.assertEqual(answer.headers.get('retry-after'), '30')

    def test_a_state_lock_wait_that_runs_out_during_a_write_does_not_say_it_was_not_completed(self):
        """kittrial-5bb.149, seen on real bd: a creation was made and registered, and the lock for the
        receipt could not be had. For a write the service cannot know, and says so."""
        admin = self.admin_token()
        import contextlib
        logged = io.StringIO()
        waited = TimeoutError('Timed out waiting for lock /srv/state.json.lock')
        with mock.patch.object(self.service, 'create_user', side_effect=waited), contextlib.redirect_stderr(logged):
            answer = self.request('POST', '/v1/accounts', {'username': 'zoe'}, token=admin, key='account-zoe-0001')
        # Which lock reaches the operator in the service's log, and nobody else.
        self.assertIn("busy: a wait for a lock ran out in the service for POST: 'Timed out waiting for lock "
                      "/srv/state.json.lock'", logged.getvalue())
        self.assertEqual((answer.status, answer.data['error']['code']), (503, 'uncertain'), answer.data)
        self.assertEqual(answer.data['error']['message'],
                         'The server was busy and cannot say whether this request was carried out. Look before you '
                         'repeat it, or send it again with the same idempotency key.')
        self.assertNotIn('lock', json.dumps(answer.data))
        # Afterwards the service answers as usual.
        self.assertEqual(self.request('GET', '/v1/projects', token=admin).status, 200)


if __name__ == '__main__':
    unittest.main()
