"""kittrial-5bb.430: an HTTP test harness leaves no server socket or server thread behind after its cleanups.

Windows CI failed with WinError 10055 (no buffer space) in suites unrelated to the change under test. The
census (tests/socket_census.py) showed why: `fixes.Harness` started a server for every test and never stopped
it, so one run of the whole suite in one process (tests/run_ci.py) kept hundreds of listening sockets and
their serve_forever threads alive. Each harness here is set up, asked one request, and cleaned up the way
unittest does; afterwards the process must hold no more listening sockets or server threads than before.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from socket_census import open_sockets, server_threads
import test_http_agents
import test_http_client
import test_http_review_fixes as fixes
import test_http_service
import test_http_web


def built(harness_class):
    """One instance of `harness_class` with a do-nothing test, ready for setUp."""
    case = type('Probe' + harness_class.__name__, (harness_class,), {'runTest': lambda self: None})
    return case(methodName='runTest')


class HarnessTeardownTests(unittest.TestCase):
    def assert_leaves_nothing(self, harness_class, ask):
        before = (open_sockets()[0], server_threads())
        harness = built(harness_class)
        harness.setUp()
        try:
            self.assertEqual(server_threads(), before[1] + 1, 'the harness did not start its server')
            ask(harness)
        finally:
            harness.doCleanups()
        self.assertEqual((open_sockets()[0], server_threads()), before,
                         '%s left its server socket or thread after its cleanups' % harness_class.__name__)

    def test_the_review_fixes_harness_stops_its_server(self):
        self.assert_leaves_nothing(fixes.Harness, lambda h: h.request('GET', '/v1/me'))

    def test_the_endpoint_case_stops_its_server(self):
        self.assert_leaves_nothing(fixes.EndpointCase, lambda h: h.request('GET', '/v1/me'))

    def test_a_restarted_review_fixes_harness_stops_the_current_server(self):
        def restart(h):
            h.restart(h.service, h.backend)
            h.request('GET', '/v1/me')
        self.assert_leaves_nothing(fixes.Harness, restart)

    def test_the_service_harness_stops_its_server(self):
        self.assert_leaves_nothing(test_http_service.ServerHarness, lambda h: h.request('GET', '/v1/me'))

    def test_the_team_harness_stops_its_server(self):
        self.assert_leaves_nothing(test_http_web.TeamHarness, lambda h: h.request('GET', '/v1/me', token=h.olive))

    def test_the_agent_harness_stops_its_server(self):
        self.assert_leaves_nothing(test_http_agents.AgentHarness, lambda h: h.request('GET', '/v1/me'))

    def test_the_client_case_stops_its_server(self):
        self.assert_leaves_nothing(test_http_client.ClientCase, lambda h: h.client())


if __name__ == '__main__':
    unittest.main()
