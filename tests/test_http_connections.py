"""Connections that say nothing must not stop the web service (review of kittrial-5bb.163).

On an open port anybody can open a connection and send nothing: a port scanner, a
browser's spare connection, a laptop that dropped off half way through a TLS handshake.
With the listening socket wrapped in TLS one such connection stopped every other client,
because the handshake ran inside ``accept``. The handshake is now the connection's own, in
its own thread; a connection the service is waiting on has a deadline, after which it is
closed whatever it had sent; and the number of connections served at once is bounded.

Every test holds its silent connections open WHILE another client is served.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tests'))
import http_service
from http_auth import Service, Store
from test_office_listener import OPENSSL, self_signed

ADMIN, PASSWORD = 'root-admin', 'correct horse battery staple 1'
#: The deadline the tests give a client; long enough for a loaded machine to serve a request in.
CLIENT = 1.5


def has_ipv6():
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(('::1', 0))
        return True
    except OSError:
        return False


class Case(unittest.TestCase):
    TLS = False

    def serve(self, client_seconds=CLIENT, web_root=None, host='127.0.0.1', margin=None, **more):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.context = None
        if self.TLS:
            cert, key = self_signed(tmp)
            more.update(certfile=cert, keyfile=key)
            self.context = ssl.create_default_context(cafile=cert)
        store = Store(Path(tmp)/'state.json')
        Service.bootstrap_superuser(store, ADMIN, PASSWORD)
        service = self.service = Service(store)
        self.httpd = http_service.create_server(service, http_service.InProcessBackend(service), host=host, port=0,
                                                web_root=web_root, client_seconds=client_seconds, **more)
        if margin is not None:
            # Where a test counts the connections the reaper cut off: the socket's own timeout (the backstop,
            # client_seconds + this) must not end a wait first on a machine where the reaper is late.
            self.httpd.TIMEOUT_MARGIN = margin
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.httpd.shutdown(), self.httpd.server_close(), thread.join(timeout=5)))
        self.host, self.port = host, self.httpd.server_address[1]
        self.held = []
        self.addCleanup(lambda: [connection.close() for connection in self.held])

    def silent(self, first=b''):
        """A TCP connection that sends ``first`` and then nothing."""
        connection = socket.create_connection((self.host, self.port), timeout=10)
        if first:
            connection.sendall(first)
        self.held.append(connection)
        return connection

    def client(self, timeout=10):
        if self.TLS:
            return http.client.HTTPSConnection('127.0.0.1', self.port, timeout=timeout, context=self.context)
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def ask(self, method, path, body=None, connection=None, close=True):
        connection = connection or self.client()
        started = time.monotonic()
        connection.request(method, path, body=None if body is None else json.dumps(body),
                           headers={'Content-Type': 'application/json'} if body is not None else {})
        response = connection.getresponse()
        raw = response.read()
        if close:
            connection.close()
        return response.status, (json.loads(raw) if raw else None), time.monotonic() - started

    def served(self, within=10):
        """The page's health read and a log-in, each answered. ``within`` proves that nothing held them until a
        silent client's deadline only where that deadline is longer (the tests that show it serve with 30 s)."""
        status, _, seconds = self.ask('GET', '/healthz')
        self.assertEqual(status, 200)
        self.assertLess(seconds, within)
        status, body, seconds = self.ask('POST', '/v1/sessions', {'username': ADMIN, 'password': PASSWORD})
        self.assertEqual(status, 201, body)
        self.assertLess(seconds, within + 1.0)                    # the password hash takes its own time

    def closed_by_the_server(self, connection, within):
        """Seconds until the server closed ``connection`` (the read ends); fails when it stays open."""
        connection.settimeout(within)
        started = time.monotonic()
        try:
            while connection.recv(4096):
                pass
        except socket.timeout:
            self.fail('the connection was still open after %.1f s' % within)
        except OSError:
            pass                                                  # reset: closed all the same
        return time.monotonic() - started

    def cut(self, connections, within=30):
        """Wait until the reaper has cut that many connections off; the count only lags, it does not go back."""
        until = time.monotonic() + within
        while time.monotonic() < until and self.httpd.cut_off != connections:
            time.sleep(0.02)
        self.assertEqual(self.httpd.cut_off, connections)

    def counted(self, connections, within=30):
        """Wait until the server counts that many open connections; they are counted one by one, as accepted."""
        until = time.monotonic() + max(within, self.httpd.client_seconds + self.httpd.TIMEOUT_MARGIN + 5)
        while time.monotonic() < until and self.httpd.open_connections() != connections:
            time.sleep(0.02)
        self.assertEqual(self.httpd.open_connections(), connections)

    def settled(self, connections=0):
        # On Windows a read that was already waiting ends with the socket's timeout, one more
        # deadline after the shutdown that closed the connection for the client.
        until = time.monotonic() + self.httpd.client_seconds + self.httpd.TIMEOUT_MARGIN + 5
        while time.monotonic() < until and self.httpd.open_connections() != connections:
            time.sleep(0.05)
        self.assertEqual(self.httpd.open_connections(), connections)


class SilentConnectionTests(Case):
    """Plain HTTP: the shape that was not stopped, and must not use up threads or descriptors either."""

    def test_the_listening_socket_is_never_a_tls_socket(self):
        self.serve()
        self.assertNotIsInstance(self.httpd.socket, ssl.SSLSocket)
        self.assertIsInstance(self.httpd, http_service.GuardedServer)
        self.assertEqual((http_service.CLIENT_SECONDS, http_service.CONNECTION_LIMIT), (30, 200))
        self.assertEqual((http_service.GuardedServer.client_seconds, http_service.GuardedServer.connection_limit), (30, 200))

    def test_silent_connections_are_held_open_while_another_client_is_served_and_then_closed(self):
        """In two steps, so that neither races a deadline (kittrial-5bb.177: it held the 25 on a 1.5 s deadline
        and had to serve two clients and read the count inside it; in CI it read 24 for 25)."""
        self.serve(client_seconds=30, margin=8)
        # 1. Held open, and another client is served meanwhile: nothing can be cut off for 30 s.
        held = [self.silent() for _ in range(20)] + [self.silent(b'GET /healthz HT') for _ in range(5)]
        self.counted(25)
        self.served()
        self.served()
        self.counted(25)                                          # they were all still open meanwhile
        self.assertEqual(self.httpd.cut_off, 0)
        for connection in held:
            connection.close()
        self.counted(0)
        # 2. Then closed: with a short deadline every silent connection is cut off, none before its time.
        self.httpd.client_seconds = CLIENT
        started = time.monotonic()
        silent = [self.silent() for _ in range(20)] + [self.silent(b'GET /healthz HT') for _ in range(5)]
        for connection in silent:
            self.closed_by_the_server(connection, 60)
        self.assertGreaterEqual(time.monotonic() - started, CLIENT * 0.9)
        self.cut(25)
        self.settled()
        self.httpd.client_seconds = 30
        self.served()

    def test_a_client_that_sends_a_byte_now_and_then_is_cut_off_all_the_same(self):
        """The bound is on the whole wait, not on each read: a timeout per read would let this one stay."""
        self.serve(client_seconds=30, margin=8)
        self.httpd.client_seconds = CLIENT                        # the short deadline is for the one that is cut off
        slow = self.silent()
        request = b'GET /healthz HTTP/1.1\r\nHost: x\r\nX-A: ' + b'a' * 400      # forty seconds of it, a byte at a time
        sent = 0
        try:
            for byte in request:
                slow.sendall(bytes([byte]))
                sent += 1
                time.sleep(0.1)
        except OSError:
            pass                                                  # cut off while still sending
        self.closed_by_the_server(slow, 30)
        self.cut(1)
        self.assertGreater(sent, 3)
        self.assertLess(sent, len(request))                       # it was not let finish
        self.settled()
        self.httpd.client_seconds = 30                            # an honest request is not raced against 1.5 s
        self.served()

    def test_a_body_that_does_not_arrive_is_cut_off_and_is_not_an_internal_error(self):
        self.serve(client_seconds=30, margin=8)
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged):
            self.httpd.client_seconds = CLIENT                    # the short deadline is for the one that is cut off
            half = self.silent(b'POST /v1/sessions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n'
                               b'Content-Length: 80\r\n\r\n{"username": "root')
            self.closed_by_the_server(half, 60)
            self.cut(1)
            self.settled()
        self.assertEqual(self.httpd.cut_off, 1)
        self.assertNotIn('Traceback', logged.getvalue())
        self.httpd.client_seconds = 30                            # an honest request is not raced against 1.5 s
        self.served()                                             # (that others are served MEANWHILE is the first test's)

    def test_a_body_shorter_than_it_said_is_not_carried_out_and_not_answered(self):
        """A request is its whole body. A log-in that ends early is not a log-in, and not an internal error."""
        self.serve(client_seconds=30)
        body = json.dumps({'username': ADMIN, 'password': PASSWORD}).encode('utf-8')
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged):
            short = self.silent(b'POST /v1/sessions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n'
                                b'Content-Length: %d\r\n\r\n' % (len(body) + 40) + body)
            short.shutdown(socket.SHUT_WR)                        # the client has finished, forty bytes early
            short.settimeout(30)
            received = b''
            while True:
                piece = short.recv(4096)
                if not piece:
                    break
                received += piece
            self.settled()
        self.assertEqual(received, b'')
        self.assertEqual(len(self.service.state['sessions']), 0)
        self.assertNotIn('Traceback', logged.getvalue())
        self.assertEqual(self.httpd.cut_off, 0)                   # it went away by itself, at once
        self.served()

    def test_an_idle_keep_alive_connection_is_served_again_inside_the_bound_and_closed_after_it(self):
        bound = 4.0                                               # each wait below is 2.5 s inside it
        self.serve(client_seconds=bound, margin=8)
        connection = self.client(timeout=30)
        for _ in range(3):
            self.assertEqual(self.ask('GET', '/healthz', connection=connection, close=False)[0], 200)
            time.sleep(1.5)                                       # each wait is its own: three of them exceed the bound
        self.assertEqual(self.httpd.cut_off, 0)
        self.closed_by_the_server(connection.sock, 60)
        self.cut(1)
        self.settled()

    def test_the_time_the_service_takes_is_not_the_clients(self):
        """A request that takes longer than the bound to answer (an endpoint call can take minutes) is answered."""
        self.serve(client_seconds=30)                            # the request is read under 30 s, not raced against 1.5
        real = http_service.ApiHandler._request_id
        watched = []

        def slow(handler):
            # No clock in this: read under 30 s, a watch left on would not fire in the 2.4 s below, and the
            # test passed with the unwatch in _dispatch taken out (review of kittrial-5bb.186).
            watched.append(handler.connection in handler.server._deadlines)
            handler.server.client_seconds = CLIENT                # the bound is short from here on: the service's own time
            time.sleep(CLIENT * 1.6)
            return real(handler)
        with mock.patch.object(http_service.ApiHandler, '_request_id', slow):
            status, _, seconds = self.ask('GET', '/healthz')
        self.assertEqual(status, 200)
        self.assertGreater(seconds, CLIENT * 1.5)
        self.assertEqual(self.httpd.cut_off, 0)
        self.assertEqual(watched, [False])                        # the service's time is under no client deadline
        self.settled()
        self.assertEqual(self.httpd._deadlines, {})               # and none is left when the connection is gone

    def test_a_client_that_does_not_take_its_response_is_cut_off(self):
        """The wait is in the WRITE: every request is already there, so the service never waits to read one."""
        web = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, web, ignore_errors=True)
        page = b'<!doctype html><title>x</title>' + b'<!-- -->\n' * 400_000
        self.assertLess(len(page), http_service.STATIC_MAX_BYTES)
        (Path(web)/'index.html').write_bytes(page)
        self.serve(web_root=web, client_seconds=30, margin=8)
        self.httpd.client_seconds = CLIENT                        # the short deadline is for the one that is cut off
        # Sixty requests for a page of 3.6 MB, and nothing is read: far more than both ends' buffers hold.
        deaf = self.silent()
        deaf.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        deaf.sendall(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n' * 60)
        self.counted(1)                                           # its thread is in a write
        self.cut(1, within=CLIENT + 6)                            # by the reaper: before the socket's own timeout could end it
        self.settled()
        self.httpd.client_seconds = 30                            # an honest request is not raced against 1.5 s
        self.served()
        # It was a response that was being written: the client finds the beginning of one.
        deaf.settimeout(10)
        self.assertTrue(deaf.recv(4096).startswith(b'HTTP/1.1 200 '))

    def test_a_thread_that_has_not_started_yet_is_not_taken_for_one_that_died(self):
        self.serve()
        waiting, request = threading.Thread(target=lambda: None), mock.Mock()
        with self.httpd._guard:
            self.httpd._serving[waiting] = (request, None)
            self.httpd._open += 1
        time.sleep(self.httpd.REAP_EVERY * 4)
        self.assertEqual((self.httpd.open_connections(), self.httpd.turned_away), (1, 0))
        request.close.assert_not_called()
        waiting.start()                                           # it ran and ended without serving...
        waiting.join()
        time.sleep(self.httpd.REAP_EVERY * 4)
        self.assertEqual((self.httpd.open_connections(), self.httpd.turned_away), (1, 0))
        with self.httpd._guard:
            self.httpd._begun.add(waiting)                        # ...and start() has returned for it: now it is given up
        self.settled()
        self.assertEqual(self.httpd.turned_away, 1)
        self.assertEqual((self.httpd._serving, self.httpd._begun), ({}, set()))

    def test_a_thread_that_is_only_starting_is_not_taken_for_one_that_died(self):
        """kittrial-5bb.175, seen once in CI: an honest POST found its socket closed in set-up (WinError 10038;
        on Linux it is EBADF). A thread has its ident a few steps before it is marked started, and is_alive()
        is False until then; the reaper took "has an ident and is not alive" for a thread that had ended, closed
        the connection and freed its place. Here those few steps are made to last four of the reaper's looks."""
        self.serve(client_seconds=30)
        held = []

        class SlowToBeMarkedStarted(threading.Thread):
            def _set_ident(self):
                super()._set_ident()
                held.append(self.is_alive())                      # False: this is what the reaper saw
                time.sleep(self.server.REAP_EVERY * 4)
        SlowToBeMarkedStarted.server = self.httpd
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged), mock.patch.object(http_service.threading, 'Thread', SlowToBeMarkedStarted):
            for _ in range(3):
                status, _, _ = self.ask('GET', '/healthz')
                self.assertEqual(status, 200)
            status, body, _ = self.ask('POST', '/v1/sessions', {'username': ADMIN, 'password': PASSWORD})
            self.assertEqual(status, 201, body)
        self.assertEqual(held, [False] * 4)
        self.settled()
        self.assertEqual((self.httpd.turned_away, self.httpd.cut_off), (0, 0))
        self.assertEqual(logged.getvalue(), '')
        self.assertEqual((self.httpd._serving, self.httpd._begun), ({}, set()))

    def test_no_thread_is_remembered_after_its_connection_is_served(self):
        self.serve(client_seconds=30)
        for _ in range(40):
            self.assertEqual(self.ask('GET', '/healthz')[0], 200)
        self.settled()
        self.assertEqual((self.httpd._serving, self.httpd._begun, self.httpd.turned_away), ({}, set(), 0))

    def test_a_thread_that_served_before_start_returned_is_not_remembered(self):
        """The other order: the connection is served and its thread gone before start() returns for it."""
        self.serve(client_seconds=30)
        real = threading.Thread.start
        waited = []

        def start_and_wait(thread):
            # Asked BEFORE the thread is started: a thread that has ended has dropped its target
            # (`Thread.run` deletes it), and this test is about threads that end before start()
            # returns. Asked afterwards, a quick one was not counted: four for five, on Linux in CI.
            serves = getattr(getattr(thread, '_target', None), '__name__', '') == 'process_request_thread'
            real(thread)
            if serves:
                thread.join(60)
                waited.append(thread.is_alive())
        with mock.patch.object(threading.Thread, 'start', start_and_wait):
            for _ in range(5):
                status, _, _ = self.ask('GET', '/healthz')
                self.assertEqual(status, 200)
            # The answer comes before the thread has ended, and the accepting thread waits for each end in turn:
            # the fifth may be some way behind the fifth answer (in CI, on Windows with 3.13, more than the ten
            # seconds this waited at first, and the test read four for five).
            until = time.monotonic() + 300
            while time.monotonic() < until and len(waited) < 5:
                time.sleep(0.02)
        self.assertEqual(waited, [False] * 5)
        self.counted(0)
        self.assertEqual((self.httpd._serving, self.httpd._begun), ({}, set()))
        self.assertEqual((self.httpd.open_connections(), self.httpd.turned_away), (0, 0))

    def test_set_up_on_a_socket_that_is_already_gone_is_quiet_and_frees_the_place(self):
        """Whatever closed it: no traceback out of the connection's thread, and the service goes on."""
        self.serve(client_seconds=30)
        real = self.httpd.finish_request
        closed = []

        def gone_first(request, client_address):
            closed.append(request)
            request.close()
            return real(request, client_address)
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged):
            with mock.patch.object(self.httpd, 'finish_request', gone_first):
                for _ in range(3):
                    connection = self.silent(b'GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n')
                    self.closed_by_the_server(connection, 5)
            self.assertEqual(len(closed), 3)
            self.settled()
            self.served()
            self.settled()
        self.assertEqual(logged.getvalue(), '')
        self.assertEqual((self.httpd.turned_away, self.httpd.cut_off), (0, 0))
        self.assertEqual((self.httpd._serving, self.httpd._begun), ({}, set()))

    def test_connections_beyond_the_limit_are_closed_at_once_and_the_limit_frees_itself(self):
        """No step of this test races a deadline (kittrial-5bb.175). It used to hold the five on a server that
        cuts clients off after 2 s and gave the one accepting thread 3 s to have counted them all: on a starved
        machine the first were cut off before the fifth was counted, and it failed in CI with "4 != 5"."""
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged):
            self.serve(client_seconds=30, connection_limit=5)     # nothing is cut off while the limit is looked at
            held = [self.silent() for _ in range(5)]
            self.counted(5)
            for _ in range(3):
                extra = self.silent()
                self.assertLess(self.closed_by_the_server(extra, 10.0), 10.0)    # at once, not after the 30 s deadline
            self.assertEqual((self.httpd.turned_away, self.httpd.cut_off, self.httpd.open_connections()), (3, 0, 5))
            # A place comes back when its client goes...
            held.pop().close()
            self.counted(4)
            held.append(self.silent())
            self.counted(5)
            extra = self.silent()
            self.assertLess(self.closed_by_the_server(extra, 10.0), 10.0)
            for connection in held:
                connection.close()
            self.counted(0)
            # ...and by itself when its client stays and says nothing: the service cuts it off.
            self.httpd.client_seconds = 1.0
            silent = [self.silent() for _ in range(5)]
            for connection in silent:
                self.closed_by_the_server(connection, 30)
            self.counted(0)
            self.assertEqual((self.httpd.turned_away, self.httpd.cut_off), (4, 5))
            self.httpd.client_seconds = 30
            self.served(within=10)
        self.assertEqual(logged.getvalue().count('connections: the limit of 5 open connections was reached'), 1)

    def test_the_bound_holds_and_no_place_is_lost_while_threads_are_slow_to_start(self):
        """A connection is counted when it is accepted, before its thread exists; the reaper never frees the
        place of a thread that is starting. So with every thread slow to start: never more than the bound open,
        every connection beyond it closed, and every place back when the clients have gone."""
        self.serve(client_seconds=30, connection_limit=4)
        at_start = []
        server = self.httpd

        class SlowToBeMarkedStarted(threading.Thread):
            def _set_ident(self):
                super()._set_ident()
                at_start.append(server.open_connections())
                time.sleep(server.REAP_EVERY * 2)
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged):
            with mock.patch.object(http_service.threading, 'Thread', SlowToBeMarkedStarted):
                clients = [self.silent() for _ in range(12)]     # all connected at once: they wait in the backlog
                peak, until = 0, time.monotonic() + 60
                while time.monotonic() < until and self.httpd.turned_away < 8:
                    peak = max(peak, self.httpd.open_connections())
                    time.sleep(0.005)
                self.assertEqual((self.httpd.turned_away, self.httpd.open_connections()), (8, 4))
                self.assertEqual((peak, max(at_start), len(at_start)), (4, 4, 4))
                closed = 0
                for connection in clients:
                    connection.settimeout(0.5)
                    try:
                        closed += connection.recv(1) == b''
                    except socket.timeout:
                        pass                                      # one of the four that are being served
                    except OSError:
                        closed += 1
                self.assertEqual(closed, 8)
                for connection in clients:
                    connection.close()
                self.counted(0)
            self.assertEqual((self.httpd._serving, self.httpd._begun, self.httpd.cut_off), ({}, set(), 0))
            # Every place is there again, and the bound is the bound.
            again = [self.silent() for _ in range(4)]
            self.counted(4)
            extra = self.silent()
            self.assertLess(self.closed_by_the_server(extra, 10.0), 10.0)
            self.assertEqual((self.httpd.turned_away, self.httpd.open_connections()), (9, 4))
        said = logged.getvalue()
        self.assertNotIn('no thread could be started', said)
        self.assertNotIn('Traceback', said)

    def set_up_fails_with(self, error, connections=3):
        """What the server says when giving a connection its timeout raises ``error``, for that many connections."""
        logged = io.StringIO()
        real = socket.socket.settimeout
        failed = []

        def settimeout(connection, seconds):
            if seconds == self.httpd.client_seconds + self.httpd.TIMEOUT_MARGIN and len(failed) < connections:
                failed.append(connection)
                raise error
            return real(connection, seconds)
        with contextlib.redirect_stderr(logged):
            with mock.patch.object(socket.socket, 'settimeout', settimeout):
                for _ in range(connections):
                    connection = self.silent(b'GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n')
                    self.closed_by_the_server(connection, 10)
                self.counted(0)
            self.assertEqual(len(failed), connections)
            self.served(within=10)
            self.counted(0)
        self.assertEqual((self.httpd._serving, self.httpd._begun, self.httpd.turned_away, self.httpd.cut_off), ({}, set(), 0, 0))
        return logged.getvalue()

    def test_set_up_on_a_socket_that_is_gone_is_quiet_whatever_it_raises(self):
        import errno
        self.serve(client_seconds=30)
        for error in (OSError(errno.EBADF, 'Bad file descriptor'), OSError(errno.ENOTSOCK, 'not a socket'),
                      OSError(errno.ENOTCONN, 'Transport endpoint is not connected'), ConnectionResetError(errno.ECONNRESET, 'reset'),
                      OSError(errno.ECONNRESET, 'Connection reset by peer'), OSError(errno.ECONNABORTED, 'aborted'),
                      BrokenPipeError(errno.EPIPE, 'Broken pipe'),
                      # a connection error is the client's whatever number it carries, or none
                      ConnectionResetError('reset by the peer'), ConnectionAbortedError()):
            with self.subTest(error=repr(error)):
                self.assertEqual(self.set_up_fails_with(error, connections=2), '')

    def test_a_fault_of_the_server_in_set_up_is_one_line_and_not_a_traceback_each(self):
        """kittrial-5bb.177: with every OSError taken for a socket that is gone, the server running out of
        descriptors in a connection's set-up was silent (and before kittrial-5bb.175 a traceback for each)."""
        import errno
        self.serve(client_seconds=30)
        said = self.set_up_fails_with(OSError(errno.EMFILE, 'Too many open files'), connections=5)
        lines = said.strip().splitlines()
        self.assertEqual(len(lines), 1, said)
        self.assertRegex(lines[0], r"^setup: '127\.0\.0\.1': '\[Errno %d\] Too many open files'; the connection was closed$" % errno.EMFILE)
        self.assertNotIn('Traceback', said)
        # The next line, when its time has come, says how many were not shown.
        self.httpd.TLS_LINE_EVERY = 0.0
        again = self.set_up_fails_with(OSError(errno.ENOMEM, 'Cannot allocate memory'), connections=1)
        self.assertRegex(again.strip(), r"^setup: '127\.0\.0\.1': '\[Errno %d\] Cannot allocate memory'; the connection was closed "
                                        r"\(and 4 more since the last such line\)$" % errno.ENOMEM)

    def test_an_error_in_set_up_that_is_not_the_sockets_is_reported_as_before(self):
        """Only an OSError is the socket's or the system's. Anything else is a fault in the code and keeps its traceback."""
        self.serve(client_seconds=30)
        said = self.set_up_fails_with(RuntimeError('broken in set-up'), connections=1)
        self.assertIn('Traceback', said)
        self.assertIn('RuntimeError: broken in set-up', said)
        self.assertNotIn('setup:', said)
        # ...and it is that error which is reported, not one made by taking it for an OSError.
        self.assertNotIn('AttributeError', said)
        self.assertEqual(said.count('Traceback'), 1)

    def test_a_process_that_cannot_start_a_thread_closes_the_connection_and_says_so_once(self):
        """Review of kittrial-5bb.163: under a memory cap "can't start new thread" was a traceback for every
        connection, 21,000 lines of them. It is the connection limit met early: closed at once, said once."""
        self.serve(client_seconds=30)
        logged = io.StringIO()
        real = threading.Thread.start
        refused = []

        def start(thread):
            if getattr(getattr(thread, '_target', None), '__name__', '') == 'process_request_thread':
                refused.append(thread)
                raise RuntimeError("can't start new thread")
            return real(thread)
        with contextlib.redirect_stderr(logged):
            with mock.patch.object(threading.Thread, 'start', start):
                for _ in range(6):
                    extra = self.silent()
                    self.assertLess(self.closed_by_the_server(extra, 10.0), 10.0)       # at once, not at the 30 s deadline
            self.assertEqual(len(refused), 6)
            self.assertEqual((self.httpd.turned_away, self.httpd.open_connections()), (6, 0))
            self.served()                                          # and it serves again when it can
        said = logged.getvalue()
        self.assertNotIn('Traceback', said)
        self.assertEqual(said.count('connections: no thread could be started for a connection'), 1)
        self.assertEqual(len(said.strip().splitlines()), 1, said)

    def test_a_thread_that_dies_before_it_serves_does_not_keep_its_connection_or_its_place(self):
        """Seen on koopa under a tight memory cap: the thread starts and ends with MemoryError in its first
        steps, so nothing served the connection, nothing closed it, and its place stayed taken."""
        self.serve(client_seconds=30, connection_limit=4)
        logged = io.StringIO()
        real = self.httpd.process_request_thread
        died = []

        def dies(request, client_address):
            died.append(request)                                  # the thread ends without having served
        with contextlib.redirect_stderr(logged):
            with mock.patch.object(self.httpd, 'process_request_thread', dies):
                for _ in range(8):                                # twice the limit: no place stays taken
                    extra = self.silent()
                    self.assertLess(self.closed_by_the_server(extra, 10.0), 10.0)
            self.assertEqual(len(died), 8)
            self.settled()
            self.assertEqual(self.httpd.turned_away, 8)
            self.served()
        said = logged.getvalue()
        self.assertEqual(said.count('connections: no thread could be started for a connection'), 1)
        self.assertIn('the thread ended before it served', said)
        self.assertNotIn('the limit of 4', said)
        self.assertEqual(len(said.strip().splitlines()), 1, said)

    def test_out_of_memory_in_a_new_thread_is_one_line_and_other_reports_are_kept(self):
        import types
        usual = sys.unraisablehook
        self.addCleanup(setattr, sys, 'unraisablehook', usual)
        seen = []
        sys.unraisablehook = seen.append
        hook = http_service.quiet_memory_errors()
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged):
            for _ in range(5):
                hook(types.SimpleNamespace(exc_value=MemoryError(), exc_type=MemoryError, exc_traceback=None,
                                           err_msg=None, object=None))
            other = types.SimpleNamespace(exc_value=ValueError('x'), exc_type=ValueError, exc_traceback=None,
                                          err_msg=None, object=None)
            hook(other)
        self.assertEqual(logged.getvalue().count('memory: the process ran out of memory while starting a thread'), 1)
        self.assertEqual(len(logged.getvalue().strip().splitlines()), 1)
        self.assertEqual(seen, [other])

    def test_a_closed_client_frees_its_place_without_waiting_for_the_deadline(self):
        self.serve(client_seconds=30)
        for connection in [self.silent() for _ in range(10)]:
            connection.close()
        self.settled()
        self.assertEqual(self.httpd.cut_off, 0)

    @unittest.skipUnless(has_ipv6(), 'no IPv6 loopback here')
    def test_an_ipv6_address_is_bound(self):
        self.serve(host='::1', client_seconds=30)
        self.assertEqual(self.httpd.socket.family, socket.AF_INET6)
        self.assertEqual(self.ask('GET', '/healthz')[0], 200)

    def test_a_server_that_was_closed_leaves_no_reaper(self):
        self.serve()
        reaper = self.httpd._reaper
        self.assertTrue(reaper.is_alive())
        self.httpd.shutdown()
        self.httpd.server_close()
        reaper.join(timeout=30)
        self.assertFalse(reaper.is_alive())


@unittest.skipUnless(OPENSSL, 'openssl is not installed; the self-signed certificate is made with it')
class SilentTlsConnectionTests(Case):
    """HTTPS: the review's case. One silent connection stopped every other client."""
    TLS = True

    def test_silent_connections_do_not_stop_the_handshake_of_another_client(self):
        """In two steps, like the plain one, so that neither races a deadline (kittrial-5bb.177)."""
        with contextlib.redirect_stderr(io.StringIO()):
            self.serve(client_seconds=30, margin=8)
            self.assertNotIsInstance(self.httpd.socket, ssl.SSLSocket)
            # 1. Nothing at all, and three bytes of a handshake: all twenty are held, and another client is served.
            held = [self.silent() for _ in range(10)] + [self.silent(b'\x16\x03\x01') for _ in range(10)]
            self.counted(20)
            self.served()
            self.served()
            self.counted(20)                                      # all twenty still open while both were served
            self.assertEqual(self.httpd.cut_off, 0)
            for connection in held:
                connection.close()
            self.counted(0)
        # 2. With a short deadline all twenty are cut off, and that is one line for the operator.
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged):
            with self.httpd._guard:
                self.httpd._tls_said, self.httpd._tls_unsaid = None, 0
            self.httpd.TLS_LINE_EVERY = 600.0                     # the quiet time does not run out under the test
            self.httpd.client_seconds = CLIENT
            started = time.monotonic()
            silent = [self.silent() for _ in range(10)] + [self.silent(b'\x16\x03\x01') for _ in range(10)]
            for connection in silent:
                self.closed_by_the_server(connection, 60)
            self.assertGreaterEqual(time.monotonic() - started, CLIENT * 0.9)
            self.cut(20)
            self.settled()
            self.httpd.client_seconds = 30
            self.served()
            # One more, after the quiet time of the log line: it says how many were not shown.
            self.httpd.TLS_LINE_EVERY = 0.0
            late = self.silent(b'GET / HTTP/1.1\r\n\r\n')         # a whole plain request to the TLS port
            self.closed_by_the_server(late, 30)
            self.settled()
        said = logged.getvalue()
        self.assertNotIn('Traceback', said)
        # Twenty at once are one line for the operator, not twenty: a port scan does not fill the log.
        self.assertEqual(said.count('tls: '), 2, said)
        self.assertIn("tls: '127.0.0.1': 'TLS handshake not completed", said)
        self.assertIn('(and 19 more since the last such line)', said)

    def test_set_up_on_a_socket_that_is_already_gone_is_quiet_with_tls_too(self):
        self.serve(client_seconds=30)
        real = self.httpd.finish_request

        def gone_first(request, client_address):
            request.close()
            return real(request, client_address)
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged):
            with mock.patch.object(self.httpd, 'finish_request', gone_first):
                connection = self.silent()
                self.closed_by_the_server(connection, 5)
            self.settled()
            self.served()
            self.settled()
        self.assertEqual(logged.getvalue(), '')
        self.assertEqual((self.httpd._serving, self.httpd._begun), ({}, set()))

    def test_a_fault_of_the_server_in_the_tls_wrap_is_one_line_and_what_the_wrap_refuses_is_a_handshake_line(self):
        import errno
        self.serve(client_seconds=30)
        for error, begins in ((OSError(errno.EMFILE, 'Too many open files'), 'setup: '),
                              (ssl.SSLError(1, 'closed before the handshake'), 'tls: ')):
            self.httpd.TLS_LINE_EVERY = 5.0
            self.httpd._tls_said = self.httpd._setup_said = None
            logged = io.StringIO()
            with contextlib.redirect_stderr(logged):
                with mock.patch.object(ssl.SSLContext, 'wrap_socket', side_effect=error):
                    for _ in range(4):
                        connection = self.silent()
                        self.closed_by_the_server(connection, 10)
                    self.settled()
                self.served(within=10)
                self.settled()
            said = logged.getvalue()
            with self.subTest(error=repr(error)):
                self.assertEqual(len(said.strip().splitlines()), 1, said)
                self.assertTrue(said.startswith(begins), said)
                self.assertNotIn('Traceback', said)
                self.assertEqual((self.httpd._serving, self.httpd._begun), ({}, set()))

    def test_a_handshake_that_fails_is_one_line_and_not_a_traceback(self):
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged):
            self.serve(client_seconds=30)
            self.httpd.TLS_LINE_EVERY = 0.0
            # A client that was not given the certificate refuses it; plain HTTP to the TLS port is not TLS.
            with self.assertRaises(ssl.SSLError):
                http.client.HTTPSConnection('127.0.0.1', self.port, timeout=30,
                                            context=ssl.create_default_context()).request('GET', '/healthz')
            plain = self.silent(b'GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n')
            self.assertLess(self.closed_by_the_server(plain, 10), 10)         # refused at once, not at the 30 s deadline
            self.settled()
            self.assertEqual(self.httpd._deadlines, {})           # the handshake's own watch went with it
            self.served()
        said = logged.getvalue()
        self.assertNotIn('Traceback', said)
        self.assertEqual(said.count('TLS handshake not completed'), 2)
        self.assertNotIn('more since', said)
        self.assertEqual(self.httpd.cut_off, 0)

    def test_the_secure_channel_is_still_seen_as_one(self):
        self.serve(client_seconds=30)
        connection = self.client()
        connection.request('POST', '/v1/sessions', body=json.dumps({'username': ADMIN, 'password': PASSWORD}),
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 201)
        self.assertIn('; Secure', response.getheader('Set-Cookie'))
        self.assertEqual(connection.sock.version() in ('TLSv1.2', 'TLSv1.3'), True)
        connection.close()
        self.settled()

    def test_a_certificate_or_key_that_cannot_be_used_stops_the_start_before_anything_is_bound(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cert, key = self_signed(tmp)
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        _, other_key = self_signed(other)
        (Path(tmp)/'text.pem').write_text('not a certificate\n', encoding='utf-8')
        made = []
        real = http_service.GuardedServer.__init__

        def counting(server, *args, **kwargs):
            made.append(server)
            return real(server, *args, **kwargs)
        with mock.patch.object(http_service.GuardedServer, '__init__', counting):
            for certfile, keyfile in ((str(Path(tmp)/'missing.crt'), key), (cert, str(Path(tmp)/'missing.key')),
                                      (str(Path(tmp)/'text.pem'), key), (cert, str(Path(tmp)/'text.pem')),
                                      (cert, other_key)):
                with self.subTest(cert=Path(certfile).name, key=Path(keyfile).name), \
                        self.assertRaises((OSError, ssl.SSLError)):
                    http_service.create_server(None, None, host='127.0.0.1', port=0, web_root=None,
                                               certfile=certfile, keyfile=keyfile)
        self.assertEqual(made, [])


def a_deadline_is_as_long_as_it_was_asked_for(self):
    """Review of kittrial-5bb.177, finding 1: nothing bounded a deadline from above; one twice as long passed
    every test here. Races nothing: the deadline is read back the moment it is set."""
    self.serve(client_seconds=30)
    for seconds in (0.5, 1.5, 30, 600):
        marker = object()
        self.httpd.watch(marker, seconds)
        left = self.httpd._deadlines[marker] - time.monotonic()
        self.httpd.unwatch(marker)
        self.assertNotIn(marker, self.httpd._deadlines)
        self.assertGreater(left, seconds - 0.5)
        # A clock that has not ticked since the deadline was armed gives (t + s) - t, which floating point
        # may round a hair above s (seen on Windows: 30.000000000000057): a millisecond of room, no more.
        self.assertLessEqual(left, seconds + 0.001)


def every_wait_for_a_client_is_armed_with_the_servers_bound(self):
    """The same for every place that arms one: the read of a request, the read of its body, the write of the
    answer, and with TLS the handshake. Each is armed with the server's bound and nothing longer."""
    self.serve(client_seconds=30)
    armed, real = [], self.httpd.watch

    def watch(connection, seconds):
        real(connection, seconds)
        deadline = self.httpd._deadlines.get(connection)
        armed.append((seconds, None if deadline is None else deadline - time.monotonic()))
    with mock.patch.object(self.httpd, 'watch', watch):
        status, body, _ = self.ask('POST', '/v1/sessions', {'username': ADMIN, 'password': PASSWORD})
        self.assertEqual(status, 201, body)
        self.settled()
    self.assertGreaterEqual(len(armed), 4 if self.TLS else 3, armed)
    for seconds, left in armed:
        self.assertEqual(seconds, 30)
        if left is not None:
            self.assertGreater(left, 29.5)
            self.assertLessEqual(left, 30 + 0.001)                 # as above: rounding, not a longer deadline


def no_wait_for_a_client_is_left_armed_when_it_is_over(self):
    """Review of kittrial-5bb.186: with every honest request read under 30 s, a watch that was not taken off
    no longer fired in any test. No clock here: whether the connection is watched is read at the moments when
    it must not be. A log-in (a body is read, an answer written) and a read, each on a connection of its own."""
    self.serve(client_seconds=30)
    seen = []
    handler_class = http_service.ApiHandler
    real_id, real_body, real_dispatch = handler_class._request_id, handler_class._read_body, handler_class._dispatch

    def watched(handler, where):
        seen.append((where, handler.connection in handler.server._deadlines))

    def request_id(handler):
        watched(handler, 'the request is read')                   # the service's time begins
        return real_id(handler)

    def read_body(handler):
        body = real_body(handler)
        watched(handler, 'the body is read')
        return body

    def dispatch(handler, method):
        try:
            return real_dispatch(handler, method)
        finally:
            watched(handler, 'the answer is written')
    with mock.patch.object(handler_class, '_request_id', request_id), \
            mock.patch.object(handler_class, '_read_body', read_body), \
            mock.patch.object(handler_class, '_dispatch', dispatch):
        status, body, _ = self.ask('POST', '/v1/sessions', {'username': ADMIN, 'password': PASSWORD})
        self.assertEqual(status, 201, body)
        self.settled()
        self.assertEqual(self.httpd._deadlines, {})               # the wait for a next request went with the connection
        self.assertEqual(self.ask('GET', '/healthz')[0], 200)
        self.settled()
        self.assertEqual(self.httpd._deadlines, {})
    self.assertEqual(seen, [(where, False) for where in ('the request is read', 'the body is read', 'the answer is written',
                                                         'the request is read', 'the body is read', 'the answer is written')])
    self.assertEqual(self.httpd.cut_off, 0)


for case in (SilentConnectionTests, SilentTlsConnectionTests):
    case.test_no_wait_for_a_client_is_left_armed_when_it_is_over = no_wait_for_a_client_is_left_armed_when_it_is_over
    case.test_a_deadline_is_as_long_as_it_was_asked_for = a_deadline_is_as_long_as_it_was_asked_for
    case.test_every_wait_for_a_client_is_armed_with_the_servers_bound = every_wait_for_a_client_is_armed_with_the_servers_bound
del case                 # a name of the module that is a test class is loaded as one: the TLS class ran twice


if __name__ == '__main__':
    unittest.main()
