"""The host in a printed client configuration must be one the CLIENT can reach (kittrial-5bb.191).

The first worker on another machine was given the server's own host name, which did not
resolve from its network. add-project prints a placeholder for the host; it now says what
the placeholder must lead to.
"""
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import admin

NOTE = ("WORKER_SSH_HOST must lead to a name or address the worker's machine can reach; this\n"
        "server's own host name may not resolve from the worker's network.")


class HostNoteTests(unittest.TestCase):

    def test_the_printed_text_says_what_the_host_must_be(self):
        text = admin.worker_client_setup(Path('/srv/rt'), 'alpha')
        self.assertIn('"host": "WORKER_SSH_HOST"', text)
        self.assertIn(NOTE + '\nBootstrap command', text)                 # after the configuration it is about
        self.assertLess(text.index('"host": "WORKER_SSH_HOST"'), text.index(NOTE))
        self.assertNotIn('publishes', text)

    def test_the_kit_does_not_guess_the_host_from_where_the_web_is_published(self):
        """Review of kittrial-5bb.191: public_url is where the web interface is published, not where SSH lands (a
        tunnel publishes localhost); add-project takes no office configuration and names no address."""
        with self.assertRaises(TypeError):
            admin.worker_client_setup(Path('/srv/rt'), 'alpha', ('https://orchestra.example.org', 'orchestra.example.org'))
        self.assertFalse(hasattr(admin, 'office_public_address'))
        with mock.patch.object(sys, 'argv', ['admin.py', '--root', '/srv/rt', 'add-project', 'alpha', '--office-config', 'f']), \
                mock.patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit):
            admin.main()

    def test_the_command_line_still_runs_add_project(self):
        with mock.patch.object(admin, 'add_project') as called, mock.patch.object(admin, 'root_path', Path), \
                mock.patch.object(sys, 'argv', ['admin.py', '--root', '/srv/rt', 'add-project', 'alpha']):
            admin.main()
        self.assertEqual(called.call_args.args[1:], ('alpha',))


if __name__ == '__main__':
    unittest.main()
