"""Known-command help works with a stand-alone client and no configuration."""
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import client
import work

COMMANDS = {
    'files': 'usage: files ',
    'checkpoint': 'usage: checkpoint TASK ',
    'brief': 'usage: brief TASK ',
    'history': 'usage: history TASK ',
    'wait': 'usage: wait ',
    'review': 'usage: review TASK ',
    'docs': 'usage: docs ',
}


class StandaloneCommandHelpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.standalone = self.root / 'standalone'
        self.home = self.root / 'home'
        self.cwd = self.root / 'cwd'
        self.tmp = self.root / 'tmp'
        for directory in (self.standalone, self.home, self.cwd, self.tmp):
            directory.mkdir()
        self.client = self.standalone / 'client.py'
        shutil.copy2(KIT / 'client.py', self.client)
        self.environment = os.environ.copy()
        self.environment.update(HOME=str(self.home), TMPDIR=str(self.tmp), PYTHONPATH='')

    def tearDown(self):
        self.temporary.cleanup()

    def run_client(self, *args):
        return subprocess.run([sys.executable, str(self.client), *args], cwd=str(self.cwd),
                              env=self.environment, capture_output=True, text=True, timeout=30)

    def assert_no_files_created(self):
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(list(self.cwd.iterdir()), [])
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertEqual([path.name for path in self.standalone.iterdir()], ['client.py'])

    def test_each_command_help_needs_no_config_project_actor_or_sibling_modules(self):
        for command, usage in COMMANDS.items():
            for help_flag in ('--help', '-h'):
                with self.subTest(command=command, help_flag=help_flag):
                    result = self.run_client(command, help_flag)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, '')
                    self.assertTrue(result.stdout.startswith(usage), result.stdout)
                    self.assertNotIn('the following arguments are required', result.stdout)
                    self.assert_no_files_created()

    def test_standalone_help_does_not_read_a_named_config(self):
        config = self.root / 'config-is-a-directory'
        config.mkdir()
        result = self.run_client('--config', str(config), 'brief', '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith(COMMANDS['brief']))
        self.assertEqual(result.stderr, '')

    def test_help_token_rule_and_positional_help(self):
        result = self.run_client('work', '--owner', '-h')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('usage: work ', result.stdout)
        self.assertIn('the following arguments are required: --config, --project', result.stderr)
        for args, usage in ((('review', 'task-id', '--help'), COMMANDS['review']),
                            (('handoff', '--json', '--help'), 'usage: handoff TASK ')):
            with self.subTest(args=args):
                result = self.run_client(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.startswith(usage), result.stdout)
                self.assert_no_files_created()

    def test_unknown_command_and_real_call_keep_their_refusals(self):
        for args in (('nonsense-xyz', '--help'), ('brief', 'task-id')):
            with self.subTest(args=args):
                result = self.run_client(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('the following arguments are required: --config, --project', result.stderr)
        result = self.run_client('--config', 'missing.json', '--project', 'example',
                                 'nonsense-xyz', '--help')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn("'nonsense-xyz' is outside the contributor interface", result.stderr)

    def test_top_level_help_and_version_are_unchanged(self):
        help_result = self.run_client('--help')
        self.assertEqual(help_result.returncode, 0)
        self.assertTrue(help_result.stdout.startswith('usage: client.py '))
        self.assertIn('The commands a contributor may run, one line each:', help_result.stdout)
        version_result = self.run_client('--version')
        self.assertEqual(version_result.returncode, 0)
        self.assertTrue(version_result.stdout.startswith('Orchestra client: version '))


class ConfiguredCommandHelpTests(unittest.TestCase):
    def test_configured_help_keeps_the_endpoint_json_envelope(self):
        for command in ('brief', 'work'):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / 'config.json'
                config.write_text('{}', encoding='utf-8')
                payload = work.help_payload(command)
                response = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
                with mock.patch.object(sys, 'argv', ['client.py', '--config', str(config),
                                                     '--project', 'example', '--actor', 'alice/session',
                                                     '--', command, '--help']), \
                        mock.patch.object(client, 'request',
                                          return_value={'stdout': response, 'stderr': '',
                                                        'returncode': 0}) as request, \
                        mock.patch('sys.stdout', new_callable=io.StringIO) as output, \
                        mock.patch('sys.stderr', new_callable=io.StringIO) as error:
                    code = client.main()
                self.assertEqual(code, 0)
                self.assertEqual(output.getvalue(), response)
                self.assertEqual(json.loads(output.getvalue()), payload)
                self.assertEqual(error.getvalue(), '')
                arguments = request.call_args.args
                self.assertEqual(arguments[3], ['--help'])
                self.assertEqual(arguments[4], command)


class HelpImplementationConsistencyTests(unittest.TestCase):
    def test_help_token_rule_matches_the_endpoint(self):
        self.assertEqual(client.HELP_TOKENS, work.HELP_TOKENS)
        self.assertEqual(client.VALUE_OPTIONS, work.VALUE_OPTIONS)
        self.assertEqual(inspect.getsource(client.help_requested),
                         inspect.getsource(work.help_requested))

    def test_static_usage_matches_the_endpoint_help(self):
        for command in ('work', 'review', 'handoff', 'brief', 'history', 'checkpoint'):
            with self.subTest(command=command):
                usage = client.contributor_command_help(command).splitlines()[0]
                self.assertEqual(usage, 'usage: ' + work.help_payload(command)['usage'])


if __name__ == '__main__':
    unittest.main()
