"""HTTP coordinate gates, independently of the canonical adapter's backstop."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import http_service as http
import test_http_review_fixes as fixtures


class RouteGateTests(fixtures.Harness):
    def setUp(self):
        super().setUp()
        self.addCleanup(self._stop_server)
        self.admin = self.admin_token()
        self.pid = self.create_project(self.admin, 'Route gates', 'routegates')
        self.url = '/v1/projects/' + self.pid
        self.tokens = {}
        for name, role in [('routeviewer', 'viewer'), ('routecontributor', 'contributor'),
                           ('routecoordinator', 'coordinator')]:
            uid = self.create_account(self.admin, name, name + '-password-1')
            reply = self.request('PUT', self.url + '/members/' + uid, {'role': role}, token=self.admin)
            self.assertEqual(reply.status, 200, reply.data)
            self.tokens[role] = self.login(name, name + '-password-1')[0]
        self.tokens['worker'] = self.issue_credential(self.admin, self.pid, label='route-worker',
            actor='route-worker', scopes=['tasks'])['secret']
        self.backend.guidance_command = Mock(return_value={'accepted': True})
        self.backend.set_onboarding = Mock(return_value={'accepted': True})
        self.backend.guidance_replay_guard = Mock()

    def check_routes(self, routes):
        for index, (method, suffix, body) in enumerate(routes):
            for role in ('viewer', 'contributor', 'worker', 'coordinator'):
                with self.subTest(method=method, route=suffix, role=role):
                    self.backend.guidance_command.reset_mock()
                    self.backend.set_onboarding.reset_mock()
                    asked = []
                    original = self.service.check_authority
                    def check(principal, project, capability, **options):
                        asked.append(capability)
                        return original(principal, project, capability, **options)
                    with patch.object(self.service, 'check_authority', side_effect=check):
                        reply = self.request(method, self.url + suffix, body, token=self.tokens[role],
                                             key='gate-%s-%s' % (index, role))
                    self.assertTrue(asked)
                    self.assertEqual(asked[0], http.CAP_COORDINATE,
                                     'The first HTTP gate must require coordinate before adapter access')
                    if role == 'coordinator':
                        self.assertEqual(reply.status, 200, reply.data)
                        self.assertEqual(self.backend.guidance_command.call_count +
                                         self.backend.set_onboarding.call_count, 1)
                    else:
                        self.assertEqual(reply.status, 403, reply.data)
                        self.backend.guidance_command.assert_not_called()
                        self.backend.set_onboarding.assert_not_called()

    def test_guidance_write_and_status_routes_gate_coordinate_before_adapter(self):
        self.check_routes([('PUT', '/guidance', {'text': 'Route-authorized instructions'}),
                           ('DELETE', '/guidance', {}), ('GET', '/guidance/status', None)])

    def test_onboarding_edit_routes_gate_coordinate_before_adapter(self):
        self.check_routes([('GET', '/onboarding', None),
                           ('PUT', '/onboarding', {'text': 'Route-authorized entry'}),
                           ('DELETE', '/onboarding', {})])
