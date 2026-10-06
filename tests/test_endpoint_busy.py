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


class BackendTests(unittest.TestCase):
    def test_the_busy_code_of_the_endpoint_is_503_busy_with_a_delay(self):
        with self.assertRaises(http_service.HttpError) as caught:
            http_service.EndpointBackend._checked({'returncode': 75, 'stdout': '',
                                                   'stderr': 'Busy: Timed out waiting for lock x. Nothing was done; try again shortly.\n'})
        error = caught.exception
        self.assertEqual((error.status, error.code), (503, 'busy'))
        self.assertIn('Nothing was done', error.message)
        self.assertEqual(error.retry_after, 30)
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
        self.assertIn('nothing was done', answer.data['error']['message'])
        # Afterwards the service answers as usual.
        self.assertEqual(self.request('GET', '/v1/projects', token=admin).status, 200)


if __name__ == '__main__':
    unittest.main()
