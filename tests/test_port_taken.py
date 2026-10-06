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
        self.assertEqual(said.getvalue(), 'orchestra-http: port %d on 127.0.0.1 is taken: something else is listening on it, so the '
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
        self.assertIn('is taken: something else is listening on it', said.getvalue())
        self.assertEqual(len(said.getvalue().splitlines()), 1)

    def test_another_failure_to_start_is_reported_as_it_was(self):
        with mock.patch.object(http_service, 'create_server', side_effect=OSError(errno.EADDRNOTAVAIL, 'Cannot assign requested address')), \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError) as raised:
                http_service.main(['--state', str(Path(self.tmp) / 'state.json'), '--backend', 'inprocess', '--port', '0'])
        self.assertEqual(raised.exception.errno, errno.EADDRNOTAVAIL)

    @unittest.skipUnless(LINUX, 'the office service is for Linux')
    def test_the_supervisor_says_which_port_and_any_other_exit_is_what_it_was(self):
        self.assertEqual(office_service.WEB_PORT_TAKEN, http_service.EXIT_PORT_TAKEN)
        self.assertEqual(office_service.child_exit_reason(4, {}, 8443),
                         'The web service did not start: port 8443 on 127.0.0.1 is taken (something else is listening on it). '
                         'Stop that, or run the service with another --port.')
        self.assertIn('port 9000 on 10.1.2.3 is taken', office_service.child_exit_reason(4, {'http_host': '10.1.2.3'}, 9000))
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
