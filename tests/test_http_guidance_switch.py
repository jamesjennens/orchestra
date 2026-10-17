"""kittrial-5bb.334: the HTTP guidance item waits for an HTTP route that can clear it.

A web agent cannot run ``guidance ack --version``: over HTTP the only way to clear the
item kittrial-5bb.325 made is a read route and an acknowledgement route, and those are
kittrial-5bb.212's to ship. Until this service registers BOTH, the HTTP agent routes must
hand out no guidance item and no wake for one.

The switch is a probe of this service's own route table (:func:`http_service.serves_web_guidance`),
so these tests drive the real table, then a table with the two routes appended, then each
route dropped in turn. They are cheap ones: no bd, no server, no endpoint.
"""
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service


ITEM = {'task': 'guidance', 'title': 'The project guidance changed',
        'text': 'Read it and acknowledge the version.', 'whose_move': 'you',
        'next_actor': 'agent-1', 'guidance_version': 'v' * 64,
        'command': 'guidance get, then guidance ack --version ' + 'v' * 64}


class BackendWithGuidance:
    """The one backend method the item gate reads."""

    def __init__(self, item=None):
        self.item = ITEM if item is None else item
        self.reads = 0

    def guidance_state(self, project_id, actor):
        self.reads += 1
        return {'present': True, 'attention': True, 'item': self.item}


class UnreadableGuidance:
    """A guidance read that fails: still no item, and never an error."""

    def guidance_state(self, project_id, actor):
        raise RuntimeError('the sidecar could not be read')


def handler(backend):
    """A request handler with just the one attribute the gate needs."""
    made = http_service.ApiHandler.__new__(http_service.ApiHandler)
    made.backend = backend
    return made


def guidance_routes(*, read=True, ack=True):
    """The two routes .212 would register, compiled the way the decorator compiles them."""
    import re
    made = []
    if read:
        made.append(('GET', re.compile('^' + r'/v1/projects/(?P<pid>[^/]+)/guidance' + '$'),
                     'project_guidance_read', False, True))
    if ack:
        made.append(('POST', re.compile('^' + r'/v1/projects/(?P<pid>[^/]+)/guidance/ack' + '$'),
                     'project_guidance_ack', False, True))
    return made


class RouteTableProbeTests(unittest.TestCase):
    def test_the_shipped_service_serves_no_guidance_route_yet(self):
        # Today's table: no read route, no ack route, so no item anywhere over HTTP.
        self.assertEqual((False, False), http_service.serves_web_guidance())

    def test_the_two_routes_are_recognised_when_they_exist(self):
        self.assertEqual((True, True), http_service.serves_web_guidance(guidance_routes()))
        # Either acknowledgement spelling .212 may choose is recognised.
        import re
        spelled = list(guidance_routes())
        spelled.append(('POST', re.compile('^' + r'/v1/projects/(?P<pid>[^/]+)/guidance/acknowledge' + '$'),
                        'project_guidance_acknowledge', False, True))
        self.assertEqual((True, True), http_service.serves_web_guidance(spelled))

    def test_one_route_alone_is_not_enough(self):
        # A read without an acknowledgement would still trap the agent (it could see the
        # version and never clear it), and an acknowledgement without a read is no use.
        self.assertEqual((True, False), http_service.serves_web_guidance(guidance_routes(ack=False)))
        self.assertEqual((False, True), http_service.serves_web_guidance(guidance_routes(read=False)))
        self.assertEqual((True, True), http_service.serves_web_guidance(guidance_routes()))

    def test_an_unrelated_guidance_route_does_not_count_as_the_ack(self):
        # The coordinator's status read (or any other guidance path) is not an
        # acknowledgement: it must not switch the item on.
        import re
        made = guidance_routes(read=True, ack=False)
        made.append(('GET', re.compile('^' + r'/v1/projects/(?P<pid>[^/]+)/guidance/status' + '$'),
                     'project_guidance_status', False, True))
        self.assertEqual((True, False), http_service.serves_web_guidance(made))


class ItemGateTests(unittest.TestCase):
    def append_routes(self, *routes):
        for entry in routes:
            http_service.ROUTES.append(entry)
        self.addCleanup(lambda: [http_service.ROUTES.remove(entry) for entry in routes
                                 if entry in http_service.ROUTES])

    def test_the_item_is_withheld_while_the_clear_routes_are_missing(self):
        backend = BackendWithGuidance()
        self.assertIsNone(handler(backend)._agent_guidance_item('pp', 'agent-1'))
        # Withheld without even a guidance read: the gate is on the route table, so a
        # web agent never pays for a read whose answer it could not act on.
        self.assertEqual(0, backend.reads)

    def test_the_item_returns_when_both_routes_are_registered(self):
        backend = BackendWithGuidance()
        self.append_routes(*guidance_routes())
        item = handler(backend)._agent_guidance_item('pp', 'agent-1')
        self.assertEqual(ITEM['guidance_version'], item['guidance_version'])
        self.assertEqual(1, backend.reads)

    def test_the_item_is_withheld_again_when_either_route_is_dropped(self):
        backend = BackendWithGuidance()
        read_route, ack_route = guidance_routes()
        self.append_routes(read_route)
        self.assertIsNone(handler(backend)._agent_guidance_item('pp', 'agent-1'))
        http_service.ROUTES.remove(read_route)
        self.append_routes(ack_route)
        self.assertIsNone(handler(backend)._agent_guidance_item('pp', 'agent-1'))
        self.append_routes(read_route)
        self.assertIsNotNone(handler(backend)._agent_guidance_item('pp', 'agent-1'))

    def test_no_guidance_set_is_still_no_item_and_an_unreadable_read_is_still_no_error(self):
        self.append_routes(*guidance_routes())
        empty = BackendWithGuidance(item=None)
        empty.item = None
        self.assertIsNone(handler(empty)._agent_guidance_item('pp', 'agent-1'))
        # A failing read answers None, never an exception (kittrial-5bb.325 behaviour
        # preserved on the route that can clear the item).
        self.assertIsNone(handler(UnreadableGuidance())._agent_guidance_item('pp', 'agent-1'))


if __name__ == '__main__':
    unittest.main()
