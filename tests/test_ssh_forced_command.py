"""Tests for the confined SSH forced-command boundary (kittrial-5bb.89).

The wrapper in ``ssh_forced_command.py`` is the boundary that makes the kit's authority
rules real for a contributor key: the key can select a configured endpoint and nothing
else. These tests pin the selection rule (another program, a flag, ``admin.py``, an extra
token, an empty or malformed command are all refused), the exact launched argv (fixed
``--root``, no authority flag), the client's ``forced_command`` support, the
``admin.py authorized-keys`` helper, and the documentation that states what needs the
confined setup.
"""
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import admin
import client
import ssh_forced_command as forced

ENDPOINT = '/srv/kit/endpoint.py'
OTHER_ENDPOINT = '/srv/kit/endpoint-legacy.py'
ROOT = '/srv/state'
PYTHON = '/usr/bin/python3'
KEY_BODY = base64.b64encode(b'orchestra-synthetic-test-key').decode()
KEY_LINE = 'ssh-ed25519 %s alex@laptop' % KEY_BODY
SSH = {'host': 'sample', 'endpoint': ENDPOINT, 'root': ROOT}
SSH_COMMAND = 'python3 %s --root %s' % (ENDPOINT, ROOT)
ANSWER = subprocess.CompletedProcess([], 0, json.dumps({'returncode': 0, 'stdout': 'ok', 'stderr': ''}), '')


class EndpointSelectionTests(unittest.TestCase):
    """`select_endpoint` ignores SSH_ORIGINAL_COMMAND except to select an endpoint."""

    def setUp(self):
        self.endpoints = [ENDPOINT, OTHER_ENDPOINT]

    def test_the_exact_endpoint_is_selected(self):
        self.assertEqual(forced.select_endpoint([ENDPOINT], self.endpoints), (ENDPOINT, None))

    def test_a_second_configured_endpoint_is_selected(self):
        self.assertEqual(forced.select_endpoint([OTHER_ENDPOINT], self.endpoints),
                         (OTHER_ENDPOINT, None))

    def test_another_program_is_refused(self):
        for program in ('/usr/bin/python3', '/srv/kit/admin.py', '/srv/kit/worker.py',
                        'bd', 'sh', '/bin/bash', 'rm', './endpoint.py', 'endpoint.py'):
            with self.subTest(program=program):
                chosen, reason = forced.select_endpoint([program], self.endpoints)
                self.assertIsNone(chosen)
                self.assertIn('may not run', reason)

    def test_an_admin_py_command_is_refused(self):
        tokens = ['/srv/kit/admin.py', '--root', ROOT, 'operators', 'add', 'alice']
        chosen, reason = forced.select_endpoint(tokens, self.endpoints)
        self.assertIsNone(chosen)
        self.assertIn('accepts no arguments', reason)

    def test_flags_are_refused_even_beside_the_endpoint(self):
        for tokens in ([ENDPOINT, '--root', '/srv/other'],
                       [ENDPOINT, '--authority-store', '/srv/authority.json'],
                       [ENDPOINT, '--authority-lock', '/srv/authority.lock'],
                       [ENDPOINT, '--require-authority'],
                       ['--root', ROOT],
                       ['--authority-store', '/srv/authority.json']):
            with self.subTest(tokens=tokens):
                chosen, reason = forced.select_endpoint(tokens, self.endpoints)
                self.assertIsNone(chosen)
                self.assertIn('accepts no arguments', reason)

    def test_extra_operands_are_refused(self):
        chosen, reason = forced.select_endpoint([ENDPOINT, 'operators', 'add', 'alice'],
                                                self.endpoints)
        self.assertIsNone(chosen)
        self.assertIn('accepts no arguments', reason)

    def test_an_empty_command_is_refused(self):
        chosen, reason = forced.select_endpoint([], self.endpoints)
        self.assertIsNone(chosen)
        self.assertIn('no endpoint selected', reason)

    def test_unconfigured_endpoints_are_refused(self):
        chosen, reason = forced.select_endpoint(['/srv/kit/endpoint.py.bak'], self.endpoints)
        self.assertIsNone(chosen)
        self.assertIn('may not run', reason)

    def test_no_configured_endpoint_refuses_everything(self):
        chosen, reason = forced.select_endpoint([ENDPOINT], [])
        self.assertIsNone(chosen)
        self.assertTrue(reason)

    def test_a_refusal_reason_is_bounded(self):
        chosen, reason = forced.select_endpoint(['x' * 5000], self.endpoints)
        self.assertIsNone(chosen)
        self.assertLess(len(reason), 300)


class WrapperExecTests(unittest.TestCase):
    """The wrapper refuses everything else and launches one fixed command line."""

    def invoke(self, original, argv=None):
        argv = argv or ['--root', ROOT, '--endpoint', ENDPOINT, '--python', PYTHON]
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': original}), \
                patch('ssh_forced_command.os.execvp') as execvp, \
                patch('sys.stdout', out), patch('sys.stderr', err):
            code = forced.main(argv)
        return code, out.getvalue(), err.getvalue(), execvp

    def test_a_normal_client_selection_execs_the_endpoint_with_the_fixed_root(self):
        code, out, err, execvp = self.invoke(ENDPOINT)
        self.assertEqual(execvp.call_args.args,
                         (PYTHON, [PYTHON, ENDPOINT, '--root', ROOT]))
        self.assertEqual(out, '')
        # The mocked exec returns, so the wrapper's fail-closed safety net reports it.
        self.assertEqual(code, 2)
        self.assertIn('did not replace this process', err)

    def test_the_launched_argv_carries_no_authority_flag(self):
        _, _, _, execvp = self.invoke(ENDPOINT)
        launched = execvp.call_args.args[1]
        for flag in ('--authority-store', '--authority-lock', '--require-authority'):
            self.assertNotIn(flag, launched)
        self.assertEqual(launched[2:], ['--root', ROOT])

    def test_a_caller_cannot_pass_flags(self):
        for original in (ENDPOINT + ' --root /srv/other',
                         ENDPOINT + ' --authority-store /srv/authority.json',
                         ENDPOINT + ' --require-authority',
                         '--root ' + ROOT):
            with self.subTest(original=original):
                code, out, err, execvp = self.invoke(original)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                execvp.assert_not_called()

    def test_a_caller_cannot_run_another_program_or_admin_py(self):
        for original in ('/srv/kit/admin.py --root %s operators add alice' % ROOT,
                         '/srv/kit/admin.py --root %s backup --all' % ROOT,
                         'bd --directory /srv/state/projects/p list',
                         '/bin/sh -c id',
                         'python3 %s --root %s' % (ENDPOINT, ROOT)):
            with self.subTest(original=original):
                code, out, err, execvp = self.invoke(original)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertTrue(err)
                execvp.assert_not_called()

    def test_shell_metacharacters_never_reach_a_shell(self):
        for original in (ENDPOINT + '; rm -rf /', ENDPOINT + ' && id', '$(id)',
                         ENDPOINT + ' `id`', ENDPOINT + ' | tee /tmp/x'):
            with self.subTest(original=original):
                code, out, _, execvp = self.invoke(original)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                execvp.assert_not_called()

    def test_an_empty_or_unbalanced_command_is_refused(self):
        for original in ('', '   ', '"unbalanced'):
            with self.subTest(original=original):
                code, out, err, execvp = self.invoke(original)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertTrue(err)
                execvp.assert_not_called()

    def test_the_endpoint_defaults_to_the_one_beside_the_wrapper(self):
        if os.name != 'posix':
            self.skipTest('the wrapper and its default endpoint use POSIX absolute paths')
        code, _, _, execvp = self.invoke(forced.default_endpoint(),
                                         argv=['--root', ROOT, '--python', PYTHON])
        self.assertEqual(execvp.call_args.args[1][1], forced.default_endpoint())

    def test_authority_flags_in_the_wrappers_own_argv_are_refused(self):
        for extra in (['--authority-store', '/srv/authority.json'], ['--require-authority'],
                      ['--authority-lock', '/srv/authority.lock']):
            with self.subTest(extra=extra):
                with patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': ENDPOINT}), \
                        patch('ssh_forced_command.os.execvp') as execvp, \
                        patch('sys.stderr', io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        forced.main(['--root', ROOT, '--endpoint', ENDPOINT, *extra])
                self.assertEqual(caught.exception.code, 2)
                execvp.assert_not_called()

    def test_relative_paths_in_the_wrappers_own_argv_are_refused(self):
        code, _, err, execvp = self.invoke(ENDPOINT, argv=['--root', 'relative',
                                                           '--endpoint', ENDPOINT])
        self.assertEqual(code, 2)
        self.assertIn('absolute', err)
        execvp.assert_not_called()


class ClientForcedCommandTests(unittest.TestCase):
    """`"forced_command": true` sends the endpoint path alone; the default is unchanged."""

    def capture(self, config, args=('list',), actor='alice'):
        with patch('client.subprocess.run', return_value=ANSWER) as run:
            client.request(config, 'sample', actor, list(args))
            return run.call_args

    def test_the_default_command_is_byte_identical(self):
        call = self.capture(SSH)
        self.assertEqual(call.args[0], ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                        'sample', SSH_COMMAND])

    def test_false_is_the_default_command(self):
        call = self.capture(dict(SSH, forced_command=False))
        self.assertEqual(call.args[0][-1], SSH_COMMAND)

    def test_forced_command_sends_only_the_endpoint(self):
        call = self.capture(dict(SSH, forced_command=True))
        self.assertEqual(call.args[0][-1], ENDPOINT)
        self.assertNotIn('--root', call.args[0][-1])
        self.assertNotIn('python3', call.args[0][-1])
        self.assertEqual(call.args[0][:6], ['ssh', '-o', 'BatchMode=yes',
                                            '-o', 'ConnectTimeout=10', 'sample'])

    def test_the_envelope_is_unchanged_in_forced_mode(self):
        forced_call = self.capture(dict(SSH, forced_command=True))
        default_call = self.capture(SSH)
        self.assertEqual(forced_call.kwargs['input'], default_call.kwargs['input'])

    def test_forced_command_must_be_a_boolean(self):
        for value in ('true', 'yes', 1, 0, [], None, 'false'):
            with self.subTest(value=value):
                with patch('client.subprocess.run', return_value=ANSWER) as run:
                    with self.assertRaisesRegex(ValueError, 'forced_command'):
                        client.request(dict(SSH, forced_command=value), 'sample', 'alice',
                                       ['list'])
                    self.assertEqual(run.call_count, 0)

    def test_a_forced_command_config_still_needs_a_root(self):
        with patch('client.subprocess.run', return_value=ANSWER) as run:
            with self.assertRaises(ValueError):
                client.request({'host': 'sample', 'endpoint': ENDPOINT, 'forced_command': True},
                               'sample', 'alice', ['list'])
            self.assertEqual(run.call_count, 0)

    def test_the_local_transport_is_unaffected(self):
        local = {'transport': 'local', 'endpoint': ENDPOINT, 'root': ROOT,
                 'python': '/usr/bin/python3', 'forced_command': True}
        call = self.capture(local)
        self.assertEqual(call.args[0],
                         ['/usr/bin/python3', ENDPOINT, '--root', ROOT])


class PublicKeyLineTests(unittest.TestCase):
    """One plain public key line is accepted; anything else is refused."""

    def test_a_plain_line_is_parsed(self):
        self.assertEqual(admin.public_key_line(KEY_LINE, 'key.pub'),
                         ('ssh-ed25519', KEY_BODY, 'alex@laptop'))

    def test_comments_and_blank_lines_are_skipped(self):
        text = '# alex key\n\n   \n%s\n' % KEY_LINE
        self.assertEqual(admin.public_key_line(text, 'key.pub')[2], 'alex@laptop')

    def test_an_options_prefixed_line_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'plain public key'):
            admin.public_key_line('command="/bin/true" %s' % KEY_LINE, 'key.pub')

    def test_a_private_key_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'private key'):
            admin.public_key_line('-----BEGIN OPENSSH PRIVATE KEY-----\n', 'id_ed25519')

    def test_garbage_and_an_empty_file_are_refused(self):
        for text in ('not a key\n', '', 'ssh-ed25519 not!base64! here\n',
                     'ssh-ed25519\n'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    admin.public_key_line(text, 'key.pub')


class AuthorizedKeyLineTests(unittest.TestCase):
    """The helper prints the exact confined contributor line and the bare operator line."""

    def lines(self, **kwargs):
        return admin.authorized_key_lines('/srv/state', '/srv/kit', 'ssh-ed25519', KEY_BODY,
                                          'alex@laptop', **kwargs)

    def test_the_contributor_line_is_exact(self):
        expected = ('command="python3 /srv/kit/ssh_forced_command.py '
                    '--root /srv/state --endpoint /srv/kit/endpoint.py",'
                    'no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding '
                    'ssh-ed25519 %s alex@laptop' % KEY_BODY)
        self.assertEqual(self.lines()['contributor'], expected)

    def test_an_absolute_interpreter_appears_verbatim(self):
        lines = self.lines(python=PYTHON)
        self.assertTrue(lines['contributor'].startswith('command="%s ' % PYTHON))

    def test_the_four_sshd_options_are_present(self):
        for option in admin.CONTRIBUTOR_KEY_OPTIONS:
            self.assertIn(option, self.lines()['contributor'])
        self.assertEqual(list(admin.CONTRIBUTOR_KEY_OPTIONS),
                         ['no-pty', 'no-port-forwarding', 'no-agent-forwarding', 'no-X11-forwarding'])

    def test_the_operator_line_is_the_unrestricted_bare_key(self):
        self.assertEqual(self.lines()['operator'], KEY_LINE)
        self.assertNotIn('command=', self.lines()['operator'])

    def test_the_contributor_line_never_carries_an_authority_flag(self):
        contributor = self.lines()['contributor']
        for flag in ('--authority-store', '--authority-lock', '--require-authority'):
            self.assertNotIn(flag, contributor)
        self.assertIn('--root /srv/state', contributor)

    def test_the_interpreter_and_comment_can_be_overridden(self):
        lines = self.lines(python='/opt/python3.11/bin/python3', comment='workstation')
        self.assertIn('command="/opt/python3.11/bin/python3 ', lines['contributor'])
        self.assertTrue(lines['contributor'].endswith('ssh-ed25519 %s workstation' % KEY_BODY))

    def test_paths_that_cannot_be_quoted_safely_are_refused(self):
        for root in ('/srv/my state', '/srv/sta"te', 'relative/state', '/srv/state\n', ''):
            with self.subTest(root=root):
                with self.assertRaises(ValueError):
                    admin.authorized_key_lines(root, '/srv/kit', 'ssh-ed25519', KEY_BODY)
        for kit in ('/srv/my kit', 'kit', '/srv/ki"t'):
            with self.subTest(kit=kit):
                with self.assertRaises(ValueError):
                    admin.authorized_key_lines('/srv/state', kit, 'ssh-ed25519', KEY_BODY)

    def test_a_multiline_comment_is_refused(self):
        with self.assertRaises(ValueError):
            self.lines(comment='two\nlines')


@unittest.skipUnless(os.name == 'posix', 'admin.py host commands use Linux absolute paths')
class AuthorizedKeysCommandTests(unittest.TestCase):
    """The admin.py subcommand prints parseable JSON and honours --role."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.key_file = self.root / 'alex.pub'
        self.key_file.write_text(KEY_LINE + '\n', encoding='utf-8')
        # A fake kit directory keeps the test independent of the checkout's own path.
        self.kit = self.root / 'kit'
        self.kit.mkdir()
        for name in ('ssh_forced_command.py', 'endpoint.py'):
            (self.kit / name).write_text('', encoding='utf-8')
        self.install_root = '/srv/orchestra-runtime'

    def invoke(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = ['admin.py', '--root', self.install_root, 'authorized-keys',
                '--key-file', str(self.key_file), *extra]
        with patch.object(sys, 'argv', argv), patch('sys.stdout', out), patch('sys.stderr', err), \
                patch.object(admin, '__file__', str(self.kit / 'admin.py')):
            admin.main()
        return json.loads(out.getvalue()), err.getvalue()

    def test_both_lines_are_printed_and_warn_about_the_operator_line(self):
        payload, err = self.invoke()
        self.assertEqual(payload['schema_version'], 1)
        self.assertIn('ssh_forced_command.py', payload['contributor'])
        self.assertTrue(payload['contributor'].startswith('command="'))
        self.assertEqual(payload['operator'], KEY_LINE)
        self.assertIn('unrestricted', payload['operator_note'])
        self.assertIn('warning', err)

    def test_role_contributor_omits_the_operator_line(self):
        payload, err = self.invoke('--role', 'contributor')
        self.assertIn('contributor', payload)
        self.assertNotIn('operator', payload)
        self.assertEqual(err, '')

    def test_role_operator_omits_the_contributor_line(self):
        payload, _ = self.invoke('--role', 'operator')
        self.assertNotIn('contributor', payload)
        self.assertEqual(payload['operator'], KEY_LINE)


@unittest.skipUnless(os.name == 'posix', 'the wrapper replaces itself with os.execvp')
class EndToEndTests(unittest.TestCase):
    """The real wrapper runs a stub endpoint with the fixed root and passes stdio through."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.endpoint = self.root / 'endpoint.py'
        self.endpoint.write_text(
            'import json, sys\n'
            'print(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}))\n',
            encoding='utf-8')

    def run_wrapper(self, original, data='{"project": "example"}'):
        env = dict(os.environ, SSH_ORIGINAL_COMMAND=original)
        return subprocess.run([sys.executable, str(KIT / 'ssh_forced_command.py'),
                               '--root', str(self.state), '--endpoint', str(self.endpoint)],
                              input=data, capture_output=True, text=True, encoding='utf-8',
                              env=env, timeout=60)

    def test_a_normal_client_selection_reaches_the_endpoint_byte_for_byte(self):
        completed = self.run_wrapper(str(self.endpoint), data='{"project": "example"}')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload['argv'], ['--root', str(self.state)])
        self.assertEqual(payload['stdin'], '{"project": "example"}')
        self.assertEqual(completed.stderr, '')

    def test_another_program_is_refused_without_running_it(self):
        canary = self.root / 'canary'
        completed = self.run_wrapper('touch %s' % canary)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, '')
        self.assertTrue(completed.stderr)
        self.assertFalse(canary.exists())

    def test_admin_py_is_refused_without_running_it(self):
        completed = self.run_wrapper('%s --root %s operators add alice' % (KIT / 'admin.py',
                                                                          self.state))
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, '')
        self.assertIn('may not run', completed.stderr)

    def test_flags_are_refused(self):
        completed = self.run_wrapper('%s --root /srv/other' % self.endpoint)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, '')
        self.assertIn('accepts no arguments', completed.stderr)


class ForcedCommandDocumentationTests(unittest.TestCase):
    """OPERATIONS and HTTP_DEPLOYMENT state what needs the confined setup."""

    def text(self, name):
        return (KIT / 'docs' / name).read_text(encoding='utf-8')

    def test_operations_documents_the_setup_the_options_and_the_client_key(self):
        text = self.text('OPERATIONS.md')
        for phrase in ('ssh_forced_command.py', 'authorized-keys', 'forced_command',
                       'no-pty', 'no-port-forwarding', 'no-agent-forwarding', 'no-X11-forwarding',
                       '--authority-store', 'migration'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_operations_names_the_guarantees_that_need_the_confined_setup(self):
        text = self.text('OPERATIONS.md')
        for phrase in ('operator allowlist', 'unverified', 'reserved',
                       'actor-shape reservation', '--root'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_http_deployment_points_at_the_confined_setup(self):
        text = self.text('HTTP_DEPLOYMENT.md')
        self.assertIn('ssh_forced_command.py', text)
        self.assertIn('cannot choose the remote command', text)

    def test_readme_points_at_the_confined_setup(self):
        self.assertIn('OPERATIONS.md', (KIT / 'README.md').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
