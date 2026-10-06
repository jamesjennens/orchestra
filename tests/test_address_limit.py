"""kittrial-5bb.170 item 2: one client address has a share of the connections, not all of them.

Found by the re-check of kittrial-5bb.163 on the real service: one client that kept about 230
silent connections reopening held every one of the 200 places for as long as it liked, and
other clients were answered 2 or 3 times of 67 over 70 seconds.

* An address has at most ``address_limit`` connections open; one more is closed at once.
* A trusted proxy is not limited as an address (every client behind it arrives from it). The
  requests it forwards are limited per forwarded address while they are served, and a request
  over the limit is answered 503 ``busy`` before its route begins.
* A forwarded header from any other peer changes nothing.

The clients here are told apart by the loopback address they connect from (127.0.0.2 and so
on); where a second loopback address cannot be bound those tests are skipped.
"""
import contextlib
import http.client
import io
import json
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import http_service
from test_http_connections import ADMIN, PASSWORD, Case
from test_office_listener import LINUX, OPENSSL
if LINUX:
    import office_service

A, B, C, D = '127.0.0.2', '127.0.0.3', '127.0.0.4', '127.0.0.5'
PROXY = A
FAR, OTHER = '203.0.113.9', '198.51.100.7'


def second_address():
    try:
        with socket.socket() as listening, socket.socket() as probe:
            listening.bind(('127.0.0.1', 0))
            listening.listen(1)
            probe.settimeout(3)
            probe.bind((A, 0))
            probe.connect(listening.getsockname())
        return True
    except OSError:
        return False


SECOND = second_address()
needs_addresses = unittest.skipUnless(SECOND, 'a second loopback address (127.0.0.2) cannot be bound here')


class AddressCase(Case):

    def setUp(self):
        # What the server says goes here and not into the test run's output; a traceback in it fails the test.
        self.said = io.StringIO()
        quiet = contextlib.redirect_stderr(self.said)
        quiet.__enter__()
        self.addCleanup(lambda: self.assertFalse('Traceback' in self.said.getvalue(), self.said.getvalue()))
        self.addCleanup(quiet.__exit__, None, None, None)

    def held_open(self):
        """Stands in for the thread that serves a connection: holds its place until the test ends."""
        release = threading.Event()
        self.addCleanup(release.set)

        def serve(request, client_address):
            release.wait(20)
            with self.httpd._guard:
                self.httpd._left(threading.current_thread())
        return mock.patch.object(self.httpd, 'process_request_thread', serve)

    def silent_from(self, address, first=b''):
        connection = socket.create_connection((self.host, self.port), timeout=10, source_address=(address, 0))
        if first:
            connection.sendall(first)
        self.held.append(connection)
        return connection

    def client_from(self, address, timeout=10):
        if self.TLS:
            return http.client.HTTPSConnection('127.0.0.1', self.port, timeout=timeout, context=self.context,
                                               source_address=(address, 0))
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout, source_address=(address, 0))

    def get(self, address, path='/healthz', headers=None, method='GET', body=None):
        connection = self.client_from(address)
        try:
            sent = dict(headers or {})
            if body is not None:
                sent['Content-Type'] = 'application/json'
            connection.request(method, path, body=None if body is None else json.dumps(body), headers=sent)
            response = connection.getresponse()
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None), response
        finally:
            connection.close()

    def open_from(self, address, count, within=3.0):
        until = time.monotonic() + within
        while time.monotonic() < until and self.httpd.open_connections(address) != count:
            time.sleep(0.02)
        self.assertEqual(self.httpd.open_connections(address), count)

    def turned_away_at_once(self, address):
        extra = self.silent_from(address)
        self.assertLess(self.closed_by_the_server(extra, 1.0), 1.0)


class GroupTests(unittest.TestCase):

    def test_what_counts_as_one_address(self):
        group = http_service.address_group
        self.assertEqual(group('192.0.2.7'), '192.0.2.7')
        self.assertNotEqual(group('192.0.2.7'), group('192.0.2.8'))
        # One line holds a whole /64: every address in it is the same client.
        self.assertEqual(group('2001:db8:1:2::5'), '2001:db8:1:2::/64')
        self.assertEqual(group('2001:db8:1:2:ffff:ffff:ffff:ffff'), group('2001:db8:1:2::5'))
        self.assertNotEqual(group('2001:db8:1:3::5'), group('2001:db8:1:2::5'))
        self.assertEqual(group('fe80::1%eth0'), 'fe80::/64')
        # An IPv4 client seen through a dual-stack socket is that IPv4 client.
        self.assertEqual(group('::ffff:192.0.2.7'), '192.0.2.7')
        self.assertEqual(group('::1'), '::/64')
        self.assertEqual(group('not an address'), 'not an address')

    def test_the_limit_is_a_quarter_of_the_places(self):
        self.assertEqual((http_service.ADDRESS_LIMIT, http_service.GuardedServer.address_limit), (50, 50))
        self.assertLess(http_service.ADDRESS_LIMIT, http_service.CONNECTION_LIMIT)
        self.assertEqual(http_service.GuardedServer.trusted_proxies, ())


class AcceptTests(AddressCase):

    @needs_addresses
    def test_one_address_cannot_take_every_place_and_another_is_served_meanwhile(self):
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged):
            self.serve(client_seconds=30, connection_limit=8, address_limit=3)
            for _ in range(3):
                self.silent_from(A)
            self.open_from(A, 3)
            for _ in range(4):
                self.turned_away_at_once(A)
            self.assertEqual((self.httpd.open_connections(), self.httpd.open_connections(A)), (3, 3))
            self.assertEqual((self.httpd.turned_away, self.httpd.turned_away_for_address, self.httpd.cut_off), (4, 4, 0))
            # Another address is served, and has its own share.
            self.assertEqual(self.get(B)[0], 200)
            self.served(within=5)
            for _ in range(3):
                self.silent_from(B)
            self.open_from(B, 3)
            self.turned_away_at_once(B)
            self.open_from(A, 3)
            # The total still holds: 3 + 3 + 2 are the 8 places, and a fourth address finds none.
            for _ in range(2):
                self.silent_from(C)
            self.open_from(C, 2)
            self.assertEqual(self.httpd.open_connections(), 8)
            self.turned_away_at_once(D)
            self.turned_away_at_once(C)
            self.assertEqual((self.httpd.turned_away, self.httpd.turned_away_for_address), (7, 5))
        said = logged.getvalue()
        self.assertEqual(said.count("connections: '127.0.0.2' has 3 connections open, the limit for one address; "
                                    "further ones from it are closed at once"), 1, said)
        self.assertEqual(said.count('connections: the limit of 8 open connections was reached'), 1, said)
        self.assertEqual(len(said.strip().splitlines()), 2, said)             # not a line for each

    @needs_addresses
    def test_a_place_is_freed_when_the_client_closes_and_when_it_is_cut_off(self):
        self.serve(client_seconds=1.5, connection_limit=20, address_limit=2)
        first, second = self.silent_from(A), self.silent_from(A)
        self.open_from(A, 2)
        self.turned_away_at_once(A)
        first.close()
        self.open_from(A, 1)
        third = self.silent_from(A)
        self.open_from(A, 2)
        self.turned_away_at_once(A)
        # Cut off for being silent: the places come back without the client doing anything.
        for connection in (second, third):
            self.closed_by_the_server(connection, 1.5 + 3)
        self.open_from(A, 0, within=self.httpd.client_seconds + self.httpd.TIMEOUT_MARGIN + 5)
        self.assertEqual(self.httpd._by_address, {})
        self.assertEqual(self.get(A)[0], 200)
        self.settled()
        self.assertEqual(self.httpd._by_address, {})

    @needs_addresses
    def test_an_address_at_its_limit_is_itself_refused_and_served_again_when_it_lets_go(self):
        """Said plainly: the honest clients of that address are turned away with it."""
        self.serve(client_seconds=30, address_limit=2)
        held = [self.silent_from(A), self.silent_from(A)]
        self.open_from(A, 2)
        with self.assertRaises((ConnectionError, http.client.HTTPException, OSError)):
            self.get(A)
        for connection in held:
            connection.close()
        self.open_from(A, 0)
        self.assertEqual(self.get(A)[0], 200)

    def test_no_limit_per_address_when_it_is_set_to_nought(self):
        self.serve(client_seconds=30, connection_limit=8, address_limit=0)
        for _ in range(8):
            self.silent()
        until = time.monotonic() + 3
        while time.monotonic() < until and self.httpd.open_connections() < 8:
            time.sleep(0.02)
        self.assertEqual((self.httpd.open_connections(), self.httpd.turned_away, self.httpd._by_address), (8, 0, {}))

    def test_the_default_applies_to_a_server_made_without_a_word_about_it(self):
        self.serve(client_seconds=30)
        self.assertEqual((self.httpd.address_limit, self.httpd.connection_limit), (50, 200))
        for _ in range(50):
            self.silent()
        self.open_from('127.0.0.1', 50, within=10)
        extra = self.silent()
        self.assertLess(self.closed_by_the_server(extra, 1.0), 1.0)
        self.assertEqual((self.httpd.turned_away, self.httpd.turned_away_for_address), (1, 1))

    def test_ipv6_addresses_of_one_64_are_one_client(self):
        self.serve(client_seconds=30, address_limit=2)
        closed = []
        with self.held_open(), \
                mock.patch.object(self.httpd, 'shutdown_request', closed.append), \
                contextlib.redirect_stderr(io.StringIO()) as logged:
            for host in ('2001:db8:1:2::1', '2001:db8:1:2::2', '2001:db8:1:2:aaaa::3', '2001:db8:1:3::1', '::ffff:192.0.2.7',
                         '192.0.2.7', '192.0.2.7'):
                self.httpd.process_request('request from ' + host, (host, 4000, 0, 0))
            self.assertEqual(closed, ['request from 2001:db8:1:2:aaaa::3', 'request from 192.0.2.7'])
            self.assertEqual(self.httpd._by_address, {'2001:db8:1:2::/64': 2, '2001:db8:1:3::/64': 1, '192.0.2.7': 2})
            self.assertEqual(self.httpd.open_connections('2001:db8:1:2::9'), 2)
        self.assertIn("connections: '2001:db8:1:2::/64' has 2 connections open", logged.getvalue())

    def test_the_line_is_said_at_most_once_in_a_while_and_then_says_how_many_it_left_out(self):
        self.serve(client_seconds=30, address_limit=1)
        logged = io.StringIO()
        with self.held_open(), \
                mock.patch.object(self.httpd, 'shutdown_request', lambda request: None), \
                contextlib.redirect_stderr(logged):
            self.httpd.process_request('held', ('192.0.2.7', 1))
            for _ in range(40):
                self.httpd.process_request('more', ('192.0.2.7', 1))
            self.assertEqual(len(logged.getvalue().splitlines()), 1, logged.getvalue())
            self.httpd.ADDRESS_LINE_EVERY = 0.0
            self.httpd.process_request('more', ('192.0.2.7', 1))
        lines = logged.getvalue().splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertNotIn('more such', lines[0])
        self.assertTrue(lines[1].endswith('(39 more such, from any address, since the last such line)'), lines[1])
        self.assertEqual((self.httpd.turned_away, self.httpd.turned_away_for_address), (41, 41))

    def test_an_address_made_to_be_read_as_a_line_is_not_repeated_as_one(self):
        self.serve(client_seconds=30, address_limit=1)
        logged = io.StringIO()
        odd = 'x\nconnections: all is well ' + 'y' * 200
        with self.held_open(), \
                mock.patch.object(self.httpd, 'shutdown_request', lambda request: None), \
                contextlib.redirect_stderr(logged):
            self.httpd.process_request('held', (odd, 1))
            self.httpd.process_request('more', (odd, 1))
        lines = logged.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertLess(len(lines[0]), 220)

    def test_a_thread_that_cannot_be_started_or_dies_gives_the_address_its_place_back(self):
        self.serve(client_seconds=30, address_limit=2)
        with mock.patch.object(self.httpd, 'shutdown_request', lambda request: None), \
                contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(threading.Thread, 'start', side_effect=RuntimeError("can't start new thread")):
                for _ in range(5):
                    self.httpd.process_request('request', ('192.0.2.7', 1))
            self.assertEqual((self.httpd._by_address, self.httpd.open_connections()), ({}, 0))
            with mock.patch.object(self.httpd, 'process_request_thread', lambda request, address: None):
                for _ in range(5):
                    self.httpd.process_request('request', ('192.0.2.7', 1))
                    until = time.monotonic() + 5
                    while time.monotonic() < until and self.httpd.open_connections():
                        time.sleep(0.02)
            self.assertEqual((self.httpd._by_address, self.httpd.open_connections()), ({}, 0))
            self.assertEqual(self.httpd.turned_away_for_address, 0)


class ProxyTests(AddressCase):
    """The peer named as a trusted proxy: not an address of its own; its clients are told apart by its header."""

    def forwarded(self, address, **more):
        return self.get(PROXY, headers={'X-Forwarded-For': address}, **more)

    @needs_addresses
    def test_a_trusted_proxy_is_not_limited_as_an_address_and_the_total_still_applies_to_it(self):
        self.serve(client_seconds=30, connection_limit=6, address_limit=2, trusted_proxies=(PROXY,))
        self.assertEqual(self.httpd.trusted_proxies, (PROXY,))
        for _ in range(6):
            self.silent_from(PROXY)
        until = time.monotonic() + 3
        while time.monotonic() < until and self.httpd.open_connections() < 6:
            time.sleep(0.02)
        self.assertEqual((self.httpd.open_connections(), self.httpd.open_connections(PROXY), self.httpd.turned_away), (6, 0, 0))
        with contextlib.redirect_stderr(io.StringIO()):
            self.turned_away_at_once(PROXY)
        self.assertEqual((self.httpd.turned_away, self.httpd.turned_away_for_address), (1, 0))

    @needs_addresses
    def test_an_address_that_is_not_the_proxy_is_limited_beside_it(self):
        self.serve(client_seconds=30, address_limit=2, trusted_proxies=(PROXY,))
        self.silent_from(B), self.silent_from(B)
        self.open_from(B, 2)
        with contextlib.redirect_stderr(io.StringIO()):
            self.turned_away_at_once(B)
        self.assertEqual(self.forwarded(FAR)[0], 200)

    @needs_addresses
    def test_a_forwarded_address_has_its_share_of_the_requests_being_served(self):
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged):
            self.serve(client_seconds=30, address_limit=2, trusted_proxies=(PROXY,))
            self.assertEqual(self.forwarded(FAR)[0], 200)
            self.assertEqual(self.httpd._requests_by_address, {})               # served and given back
            # Two of its requests are being served (held here, as a slow action would hold them).
            self.assertTrue(self.httpd.request_begins(FAR))
            self.assertTrue(self.httpd.request_begins(FAR))
            status, body, response = self.forwarded(FAR)
            self.assertEqual((status, body['error']['code'], body['error']['message']),
                             (503, 'busy', http_service.ADDRESS_BUSY))
            self.assertEqual(response.getheader('Retry-After'), '1')
            self.assertEqual(response.getheader('Connection'), 'close')
            self.assertEqual(response.getheader('X-Request-Id'), body['request_id'])
            self.assertTrue(body['request_id'])
            # The proxy's own last element is the one that counts, whatever the client put before it.
            self.assertEqual(self.forwarded(OTHER + ', ' + FAR)[0], 503)
            self.assertEqual(self.forwarded(FAR + ', ' + OTHER)[0], 200)
            # Another forwarded address is served meanwhile; so is the proxy without a usable header.
            self.assertEqual(self.forwarded(OTHER)[0], 200)
            self.assertEqual(self.get(PROXY)[0], 200)
            self.assertEqual(self.forwarded('not an address')[0], 200)
            self.assertEqual(self.httpd._requests_by_address, {FAR: 2})
            self.assertEqual((self.httpd.turned_away, self.httpd.turned_away_for_address), (0, 2))
            self.httpd.request_ends(FAR)
            self.assertEqual(self.forwarded(FAR)[0], 200)
            self.httpd.request_ends(FAR)
            self.assertEqual(self.httpd._requests_by_address, {})
        said = logged.getvalue()
        self.assertEqual(said.count("connections: '203.0.113.9' has 2 requests being served, the limit for one address; "
                                    "further ones from it are answered 503"), 1, said)
        self.assertEqual(len(said.strip().splitlines()), 1, said)

    @needs_addresses
    def test_the_place_is_held_while_the_request_is_served_and_given_back_however_it_ends(self):
        self.serve(client_seconds=30, address_limit=2, trusted_proxies=(PROXY,))
        seen = []
        real = http_service.ApiHandler._dispatch

        def watched(handler, method):
            seen.append(dict(handler.server._requests_by_address))
            if handler.path == '/boom':
                raise ConnectionAbortedError('the client went away')
            return real(handler, method)
        with mock.patch.object(http_service.ApiHandler, '_dispatch', watched):
            self.assertEqual(self.forwarded(FAR)[0], 200)
            self.assertEqual(self.forwarded('2001:db8:1:2::5')[0], 200)
            with self.assertRaises((ConnectionError, http.client.HTTPException, OSError)):
                self.forwarded(FAR, path='/boom')
            self.assertEqual(self.get(B)[0], 200)                                # not through the proxy: not counted
        self.assertEqual(seen, [{FAR: 1}, {'2001:db8:1:2::/64': 1}, {FAR: 1}, {}])
        self.settled()
        self.assertEqual(self.httpd._requests_by_address, {})

    @needs_addresses
    def test_a_request_over_the_limit_is_not_carried_out(self):
        self.serve(client_seconds=30, address_limit=1, trusted_proxies=(PROXY,))
        self.assertTrue(self.httpd.request_begins(FAR))
        before = len(self.service.store.state.get('sessions', {}))
        with mock.patch.object(http_service.ApiHandler, '_dispatch', side_effect=AssertionError('the route began')), \
                contextlib.redirect_stderr(io.StringIO()):
            status, body, response = self.forwarded(FAR, method='POST', path='/v1/sessions',
                                                    body={'username': ADMIN, 'password': PASSWORD})
        self.assertEqual((status, body['error']['code']), (503, 'busy'))
        self.assertEqual(len(self.service.store.state.get('sessions', {})), before)
        self.httpd.request_ends(FAR)
        status, body, _ = self.forwarded(FAR, method='POST', path='/v1/sessions',
                                         body={'username': ADMIN, 'password': PASSWORD})
        self.assertEqual(status, 201, body)

    @needs_addresses
    def test_every_method_is_under_the_limit(self):
        self.serve(client_seconds=30, address_limit=1, trusted_proxies=(PROXY,))
        self.assertTrue(self.httpd.request_begins(FAR))
        for method in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'):
            status, _, response = self.forwarded(FAR, method=method, path='/v1/projects')
            self.assertEqual((method, status, response.getheader('Retry-After')), (method, 503, '1'))
            self.assertNotEqual(self.forwarded(OTHER, method=method, path='/v1/projects')[0], 503, method)
        self.assertEqual(self.httpd.turned_away_for_address, 6)

    @needs_addresses
    def test_a_forwarded_header_from_a_peer_that_is_not_the_proxy_changes_nothing(self):
        self.serve(client_seconds=30, address_limit=1, trusted_proxies=(PROXY,))
        self.assertTrue(self.httpd.request_begins(FAR))
        # B is not the proxy: its header is not believed, so it is not refused for FAR's requests...
        self.assertEqual(self.get(B, headers={'X-Forwarded-For': FAR})[0], 200)
        self.assertEqual(self.httpd._requests_by_address, {FAR: 1})
        # ...and it cannot use the header to pass as someone else: it is limited as B.
        self.silent_from(B)
        self.open_from(B, 1)
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises((ConnectionError, http.client.HTTPException, OSError)):
            self.get(B, headers={'X-Forwarded-For': OTHER})

    @needs_addresses
    def test_no_limit_per_forwarded_address_when_it_is_set_to_nought(self):
        self.serve(client_seconds=30, address_limit=0, trusted_proxies=(PROXY,))
        for _ in range(5):
            self.assertTrue(self.httpd.request_begins(FAR))
        self.assertEqual(self.forwarded(FAR)[0], 200)

    def test_without_a_trusted_proxy_no_request_is_counted(self):
        self.serve(client_seconds=30, address_limit=1)
        with mock.patch.object(self.httpd, 'request_begins', side_effect=AssertionError('counted')):
            status, _, _ = self.ask('GET', '/healthz')
            self.assertEqual(status, 200)
            connection = self.client()
            connection.request('GET', '/healthz', headers={'X-Forwarded-For': FAR})
            self.assertEqual(connection.getresponse().status, 200)
            connection.close()

    def test_a_handler_on_a_server_that_does_not_count_is_served_as_before(self):
        import http.server
        handler = http_service.build_handler(mock.Mock(), mock.Mock(), trusted_proxies=('127.0.0.1',), web_root=None)
        plain = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=plain.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (plain.shutdown(), plain.server_close(), thread.join(timeout=5)))
        connection = http.client.HTTPConnection('127.0.0.1', plain.server_address[1], timeout=10)
        connection.request('GET', '/healthz', headers={'X-Forwarded-For': FAR, 'Connection': 'close'})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 200)
        connection.close()


@unittest.skipUnless(OPENSSL, 'openssl is not installed: no certificate to serve with')
class TlsAddressTests(AddressCase):
    TLS = True

    @needs_addresses
    def test_a_connection_over_the_limit_is_closed_before_any_handshake(self):
        self.serve(client_seconds=30, address_limit=2)
        self.silent_from(A), self.silent_from(A)
        self.open_from(A, 2)
        until = time.monotonic() + 5                              # both are in their handshake before the check begins
        while time.monotonic() < until and len(self.httpd._deadlines) < 2:
            time.sleep(0.02)
        self.assertEqual(len(self.httpd._deadlines), 2)
        with mock.patch.object(ssl.SSLContext, 'wrap_socket', side_effect=AssertionError('a handshake was begun')), \
                contextlib.redirect_stderr(io.StringIO()):
            self.turned_away_at_once(A)
        self.assertEqual(self.get(B)[0], 200)
        self.assertEqual((self.httpd.turned_away, self.httpd.turned_away_for_address), (1, 1))


class SettingTests(unittest.TestCase):

    @contextlib.contextmanager
    def folder(self):
        # Not TemporaryDirectory: on Windows the record store's file is still open when the block ends.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        yield tmp

    def test_the_command_line_takes_the_number_and_refuses_one_below_nought(self):
        with self.folder() as tmp, contextlib.redirect_stderr(io.StringIO()) as said:
            with self.assertRaises(SystemExit) as stopped:
                http_service.main(['--state', str(Path(tmp)/'state.json'), '--backend', 'inprocess',
                                   '--connections-per-address', '-1'])
            self.assertEqual(stopped.exception.code, 2)
            self.assertIn('--connections-per-address must be 0 (no limit per address) or more', said.getvalue())
            self.assertFalse((Path(tmp)/'state.json').exists())
        for words, expected in (([], 50), (['--connections-per-address', '7'], 7), (['--connections-per-address', '0'], 0)):
            with self.folder() as tmp:
                made = []

                def create(*args, **kwargs):
                    made.append(kwargs)
                    raise KeyboardInterrupt
                with mock.patch.object(http_service, 'create_server', create), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(KeyboardInterrupt):
                        http_service.main(['--state', str(Path(tmp)/'state.json'), '--backend', 'inprocess', '--port', '0']
                                          + words)
                self.assertEqual(made[0]['address_limit'], expected)

    def test_the_server_is_told_its_trusted_proxies_and_its_limit(self):
        with self.folder() as tmp:
            store = http_service.Store(Path(tmp)/'state.json')
            service = http_service.Service(store)
            for more, expected in (({}, ((), 50)), ({'trusted_proxies': ['10.0.0.1', 'localhost'], 'address_limit': 4},
                                                    (('10.0.0.1', 'localhost'), 4)),
                                   ({'address_limit': 0}, ((), 0))):
                httpd = http_service.create_server(service, http_service.InProcessBackend(service), port=0, **more)
                try:
                    self.assertEqual((httpd.trusted_proxies, httpd.address_limit), expected)
                    self.assertEqual(httpd._limited_as(('10.0.0.1', 5)), None if expected[0] or not expected[1] else '10.0.0.1')
                    self.assertEqual(httpd._limited_as(('127.0.0.1', 5)), None if expected[0] or not expected[1] else '127.0.0.1')
                    self.assertEqual(httpd._limited_as(('10.0.0.2', 5)), '10.0.0.2' if expected[1] else None)
                    self.assertIsNone(httpd._limited_as(None))
                finally:
                    httpd.server_close()

    @unittest.skipUnless(LINUX, 'the office service is for Linux')
    def test_the_office_configuration_takes_the_number_and_hands_it_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'office.json'

            def config(**settings):
                path.write_text(json.dumps(dict({'schema_version': 1}, **settings)), encoding='utf-8')
                return office_service.service_config(path)
            self.assertNotIn('connections_per_address', config())
            for good in (0, 1, 50, 400):
                self.assertEqual(config(connections_per_address=good)['connections_per_address'], good)
            for bad in (-1, 1.5, '50', True, None, [50]):
                with self.assertRaises(ValueError) as refused:
                    config(connections_per_address=bad)
                self.assertIn('connections_per_address must be a whole number', str(refused.exception))
        script, root = Path('/release/kit/office_service.py'), Path('/runtime')

        def command(**settings):
            return [str(part) for part in office_service.web_command(settings, root, 10000, '/release/python', script)]
        self.assertNotIn('--connections-per-address', command())
        for number in (0, 12):
            found = command(connections_per_address=number)
            self.assertEqual(found[found.index('--connections-per-address') + 1], str(number))


if __name__ == '__main__':
    unittest.main()
