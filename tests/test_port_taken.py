"""Starting the web service on a port that is taken says so (kittrial-5bb.180).

When the bind failed the base class closed the server, and `GuardedServer.server_close` read a
field that was only made after the bind: the operator saw a traceback ending in
"AttributeError: 'GuardedServer' object has no attribute '_stopping'" and the supervisor
"A supervised child exited", with the reason hidden.
"""
import contextlib
import errno
import io
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
from http_auth import Service, Store

LINUX = sys.platform.startswith('linux')
if LINUX:
    import office_service


def reapers():
    return [thread for thread in threading.enumerate() if thread.name == 'connection-reaper']


class PortTakenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # Something else is listening: a socket that does not share its port.
        self.other = socket.socket()
        self.other.bind(('127.0.0.1', 0))
        self.other.listen(1)
        self.addCleanup(self.other.close)
        self.port = self.other.getsockname()[1]

    def test_the_error_is_the_bind_error(self):
        service = Service(Store(Path(self.tmp) / 'state.json'))
        before = len(reapers())
        with self.assertRaises(OSError) as refused:
            http_service.create_server(service, http_service.InProcessBackend(service), port=self.port)
        self.assertNotIsInstance(refused.exception, AttributeError)
        # "In use"; on Windows the same refusal reads "access" (10013), with or without the holder's exclusive claim.
        self.assertTrue(refused.exception.errno == errno.EADDRINUSE or getattr(refused.exception, 'winerror', None) == 10013,
                        refused.exception)
        self.assertEqual(len(reapers()), before)                     # and no reaper is left for a server that is not there

    def test_closing_a_server_that_never_finished_starting_needs_nothing_made_later(self):
        """Whatever fails in the base class's start, and whichever field is read: the close itself does not fail."""
        for failing in ('server_bind', 'server_activate'):
            with self.subTest(step=failing), mock.patch.object(http_service.GuardedServer, failing, side_effect=OSError(errno.EACCES, 'no')):
                with self.assertRaises(OSError) as refused:
                    http_service.GuardedServer(('127.0.0.1', 0), http_service.ApiHandler)
                self.assertEqual(refused.exception.errno, errno.EACCES)
        source = (KIT / 'http_service.py').read_text(encoding='utf-8')
        start = source.index('class GuardedServer(')
        made, bound = source.index('self._stopping = threading.Event()', start), source.index('super().__init__(*args, **kwargs)', start)
        self.assertLess(made, bound)
        close = source[source.index('    def server_close(self):', start):source.index('def quiet_memory_errors')]
        self.assertEqual(sorted(set(word for word in close.split() if word.startswith('self._'))), ['self._stopping.set()'])

    def test_a_server_that_started_is_closed_as_before_and_its_reaper_ends(self):
        service = Service(Store(Path(self.tmp) / 'state.json'))
        httpd = http_service.create_server(service, http_service.InProcessBackend(service), port=0)
        reaper = httpd._reaper
        self.assertTrue(reaper.is_alive())
        httpd.server_close()
        reaper.join(30)
        self.assertFalse(reaper.is_alive())

    def test_the_command_says_it_in_one_line_and_exits_with_the_status_for_it(self):
        said, printed = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(said), contextlib.redirect_stdout(printed):
            status = http_service.main(['--state', str(Path(self.tmp) / 'state.json'), '--backend', 'inprocess', '--port', str(self.port)])
        self.assertEqual((status, http_service.EXIT_PORT_TAKEN), (4, 4))
        self.assertEqual(said.getvalue(), 'orchestra-http: port %d on 127.0.0.1 is taken: something else holds it, so the '
                         'web service did not start. Stop that, or start this service on another port.\n' % self.port)
        self.assertNotIn('listening on', printed.getvalue())

    @unittest.skipUnless(sys.platform == 'win32', 'the Windows refusal of a port held for one program alone')
    def test_on_windows_a_port_held_exclusively_is_taken_too(self):
        held = socket.socket()
        held.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        held.bind(('127.0.0.1', 0))
        held.listen(1)
        self.addCleanup(held.close)
        said = io.StringIO()
        with contextlib.redirect_stderr(said), contextlib.redirect_stdout(io.StringIO()):
            status = http_service.main(['--state', str(Path(self.tmp) / 'state.json'), '--backend', 'inprocess',
                                        '--port', str(held.getsockname()[1])])
        self.assertEqual(status, 4)
        self.assertIn('is taken: something else holds it', said.getvalue())
        self.assertEqual(len(said.getvalue().splitlines()), 1)

    def test_a_windows_exclusive_refusal_counts_as_taken_where_it_cannot_be_run(self):
        """A mocked winerror 10013 is "taken" on every platform (three mutants survived on Linux).

        On Windows an excluded port range and a port one program holds alone are both refused as
        "access" (10013) with no listener, so the arm must treat it as taken wherever it runs.
        """
        refused = OSError(errno.EACCES, 'access is denied')
        refused.winerror = 10013
        said, printed = io.StringIO(), io.StringIO()
        with mock.patch.object(http_service, 'create_server', side_effect=refused), \
                contextlib.redirect_stderr(said), contextlib.redirect_stdout(printed):
            status = http_service.main(['--state', str(Path(self.tmp) / 'state.json'), '--backend', 'inprocess',
                                        '--port', '4321'])
        self.assertEqual(status, http_service.EXIT_PORT_TAKEN)
        self.assertEqual(said.getvalue(), 'orchestra-http: port 4321 on 127.0.0.1 is taken: something else holds it, '
                         'so the web service did not start. Stop that, or start this service on another port.\n')

    def test_a_plain_eacces_is_not_a_taken_port(self):
        """A plain EACCES is a port that cannot be bound, never "taken" (a mocked winerror-less arm)."""
        refused = OSError(errno.EACCES, 'permission denied')
        said = io.StringIO()
        with mock.patch.object(http_service, 'create_server', side_effect=refused), \
                contextlib.redirect_stderr(said), contextlib.redirect_stdout(io.StringIO()):
            status = http_service.main(['--state', str(Path(self.tmp) / 'state.json'), '--backend', 'inprocess',
                                        '--port', '4321'])
        self.assertNotEqual(status, http_service.EXIT_PORT_TAKEN)
        self.assertEqual(status, http_service.EXIT_PORT_NOT_PERMITTED)
        self.assertIn('port 4321 cannot be bound', said.getvalue())
        self.assertNotIn('is taken', said.getvalue())

    def test_a_port_below_1024_is_one_line_not_a_traceback(self):
        refused = PermissionError(errno.EACCES, 'permission denied')
        said = io.StringIO()
        with mock.patch.object(http_service, 'create_server', side_effect=refused), \
                contextlib.redirect_stderr(said), contextlib.redirect_stdout(io.StringIO()):
            status = http_service.main(['--state', str(Path(self.tmp) / 'state.json'), '--backend', 'inprocess',
                                        '--port', '80'])
        self.assertEqual(status, http_service.EXIT_PORT_NOT_PERMITTED)
        self.assertIn('port 80 cannot be bound', said.getvalue())
        self.assertIn('below 1024 needs privilege', said.getvalue())
        self.assertEqual(len(said.getvalue().splitlines()), 1)

    def test_an_address_that_is_not_on_the_host_is_one_line_not_a_traceback(self):
        for refused in (OSError(errno.EADDRNOTAVAIL, 'Cannot assign requested address'),
                        socket.gaierror(-2, 'Name or service not known')):
            with self.subTest(refused=refused):
                said = io.StringIO()
                with mock.patch.object(http_service, 'create_server', side_effect=refused), \
                        contextlib.redirect_stderr(said), contextlib.redirect_stdout(io.StringIO()):
                    status = http_service.main(['--state', str(Path(self.tmp) / 'state.json'), '--backend',
                                                'inprocess', '--host', 'no.such.host.invalid', '--port', '4321'])
                self.assertEqual(status, http_service.EXIT_ADDRESS_UNAVAILABLE)
                self.assertIn('the address no.such.host.invalid cannot be used', said.getvalue())
                self.assertEqual(len(said.getvalue().splitlines()), 1)

    def test_an_unusable_certificate_is_one_line_not_a_traceback(self):
        refused = ssl.SSLError(1, '[SSL] PEM lib (_ssl.c:3851)')
        said = io.StringIO()
        with mock.patch.object(http_service, 'create_server', side_effect=refused), \
                contextlib.redirect_stderr(said), contextlib.redirect_stdout(io.StringIO()):
            status = http_service.main(['--state', str(Path(self.tmp) / 'state.json'), '--backend', 'inprocess',
                                        '--cert', '/c.pem', '--key', '/k.pem', '--port', '0'])
        self.assertEqual(status, http_service.EXIT_CERTIFICATE_UNUSABLE)
        self.assertIn('the certificate or key cannot be used', said.getvalue())
        self.assertEqual(len(said.getvalue().splitlines()), 1)

    def test_another_failure_to_start_is_reported_as_it_was(self):
        """A failure that is none of the named ones still propagates, exactly as before."""
        with mock.patch.object(http_service, 'create_server', side_effect=OSError(errno.EIO, 'I/O error')), \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError) as raised:
                http_service.main(['--state', str(Path(self.tmp) / 'state.json'), '--backend', 'inprocess', '--port', '0'])
        self.assertEqual(raised.exception.errno, errno.EIO)

    @unittest.skipUnless(LINUX, 'the office service is for Linux')
    def test_the_supervisor_says_which_port_and_any_other_exit_is_what_it_was(self):
        self.assertEqual(office_service.WEB_PORT_TAKEN, http_service.EXIT_PORT_TAKEN)
        self.assertEqual(office_service.WEB_ADDRESS_UNAVAILABLE, http_service.EXIT_ADDRESS_UNAVAILABLE)
        self.assertEqual(office_service.WEB_PORT_NOT_PERMITTED, http_service.EXIT_PORT_NOT_PERMITTED)
        self.assertEqual(office_service.WEB_CERTIFICATE_UNUSABLE, http_service.EXIT_CERTIFICATE_UNUSABLE)
        self.assertEqual(office_service.child_exit_reason(4, {}, 8443),
                         'The web service did not start: port 8443 on 127.0.0.1 is taken (something else holds it). '
                         'Stop that, or run the service with another --port.')
        self.assertIn('port 9000 on 10.1.2.3 is taken', office_service.child_exit_reason(4, {'http_host': '10.1.2.3'}, 9000))
        self.assertEqual(office_service.child_exit_reason(5, {'http_host': '10.1.2.3'}, 8443),
                         'The web service did not start: 10.1.2.3 is not an address of this host, or its name does '
                         'not resolve. Give an address of the host, or a name that resolves to one.')
        self.assertIn('port 8443 cannot be bound without privilege',
                      office_service.child_exit_reason(6, {}, 8443))
        self.assertIn('the certificate or key cannot be used',
                      office_service.child_exit_reason(7, {}, 8443))
        for status in (None, 0, 1, 2, -9):
            self.assertEqual(office_service.child_exit_reason(status, {}, 8443), 'A supervised child exited; inspect service logs')

    def test_the_supervisors_loop_uses_that_sentence(self):
        """The loop itself runs only on a real host (smoke180.py on koopa); here, that it asks for the reason."""
        source = (KIT / 'office_service.py').read_text(encoding='utf-8')
        loop = source[source.index('def run(root, logs, config, port, stop_seconds):'):]
        self.assertIn('raise RuntimeError(child_exit_reason(web.poll(), settings, port))', loop)
        self.assertNotIn("raise RuntimeError('A supervised child exited", loop)


if __name__ == '__main__':
    unittest.main()
