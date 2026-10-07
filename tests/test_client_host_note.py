"""The host in a printed client configuration must be one the CLIENT can reach (kittrial-5bb.191).

The first worker on another machine was given the server's own host name, which did not
resolve from its network. add-project prints a placeholder for the host; it now says what
the placeholder must lead to, and with ``--office-config`` names the address the office
configuration publishes.
"""
import json
import sys
import tempfile
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

    def test_with_the_office_configuration_the_published_address_is_named(self):
        text = admin.worker_client_setup(Path('/srv/rt'), 'alpha', ('https://orchestra.example.org:8443', 'orchestra.example.org'))
        self.assertIn(NOTE + ' The office configuration publishes\nthis service as https://orchestra.example.org:8443: '
                      'orchestra.example.org is the address clients reach.\nBootstrap command', text)
        self.assertIn('"host": "WORKER_SSH_HOST"', text)                  # the alias and the account are still the worker's


class OfficeConfigTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name)/'office.json'

    def written(self, value):
        self.file.write_text(value if isinstance(value, str) else json.dumps(value), encoding='utf-8')
        return self.file

    def test_the_public_address_is_read_from_public_url(self):
        for url, host in (('https://orchestra.example.org:8443', 'orchestra.example.org'),
                          ('https://orchestra.example.org/', 'orchestra.example.org'),
                          ('http://10.1.2.3:8080', '10.1.2.3'), ('https://[2001:db8::1]:8443', '2001:db8::1')):
            with self.subTest(url=url):
                self.assertEqual(admin.office_public_address(self.written({'http_host': '0.0.0.0', 'public_url': url})),
                                 (url, host))
        self.assertEqual(admin.office_public_address(self.written({'http_host': '127.0.0.1'})), (None, None))

    def test_a_file_that_is_not_an_office_configuration_is_refused_with_a_sentence(self):
        for value, said in (('not json', 'cannot be read as the office service configuration'),
                            ([], 'not a JSON object'), ('"text"', 'not a JSON object'),
                            ({'public_url': 7}, 'public_url is not an address with a host'),
                            ({'public_url': 'orchestra.example.org'}, 'public_url is not an address with a host'),
                            ({'public_url': ''}, 'public_url is not an address with a host')):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as refused:
                    admin.office_public_address(self.written(value))
                self.assertIn(said, str(refused.exception))
                self.assertTrue(str(refused.exception).startswith('--office-config %s: ' % self.file))
        with self.assertRaises(ValueError) as refused:
            admin.office_public_address(Path(self.tmp.name)/'missing.json')
        self.assertIn('cannot be read as the office service configuration', str(refused.exception))

    def test_add_project_refuses_such_a_file_before_anything_is_created(self):
        made = []
        with mock.patch.object(admin, 'initialize_project', lambda root, name: made.append(name)), \
                mock.patch.object(admin, 'scheduled_backup_coverage', lambda root, name: (True, 'SCHEDULE')), \
                mock.patch('builtins.print') as printed:
            with self.assertRaises(ValueError):
                admin.add_project(Path('/srv/rt'), 'alpha', self.written('not json'))
            self.assertEqual((made, printed.call_args_list), ([], []))
            admin.add_project(Path('/srv/rt'), 'alpha', self.written({'public_url': 'https://orchestra.example.org:8443'}))
            self.assertEqual(made, ['alpha'])
            self.assertIn('orchestra.example.org is the address clients reach', printed.call_args_list[-1].args[0])
            admin.add_project(Path('/srv/rt'), 'beta')                    # without the flag: the sentence only
            self.assertIn(NOTE, printed.call_args_list[-1].args[0])
            self.assertNotIn('publishes', printed.call_args_list[-1].args[0])

    def test_the_command_line_hands_the_file_to_add_project(self):
        for more, expected in ((['--office-config', 'office.json'], 'office.json'), ([], None)):
            with self.subTest(more=more), mock.patch.object(admin, 'add_project') as called, \
                    mock.patch.object(admin, 'root_path', Path), \
                    mock.patch.object(sys, 'argv', ['admin.py', '--root', self.tmp.name, 'add-project', 'alpha'] + more):
                admin.main()
                self.assertEqual(called.call_args.args[1:], ('alpha', expected))


if __name__ == '__main__':
    unittest.main()
