"""Tests for the confined SSH forced-command boundary (kittrial-5bb.89).

The wrapper in ``ssh_forced_command.py`` is the boundary that makes the kit's authority
rules real for a contributor key: the key can select a configured endpoint and nothing
else. These tests pin the selection rule (another program, a flag, ``admin.py``, an extra
token, an empty or malformed command are all refused), the exact launched argv (fixed
``--root``, no authority flag), the minimal environment the endpoint is exec'd with, the
client's ``forced_command`` support, the ``admin.py authorized-keys`` helper (including the
interpreter character class and the literal single-key rule), and the documentation that
states what needs the confined setup and which sshd settings it assumes.
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
SECOND_KEY_BODY = base64.b64encode(b'orchestra-second-synthetic-key').decode()
SECOND_KEY_LINE = 'ssh-ed25519 %s mallory@laptop' % SECOND_KEY_BODY
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


class PythonInterpreterValidationTests(unittest.TestCase):
    def test_a_bare_name_is_resolved_and_probed_as_python_310_or_newer(self):
        completed = subprocess.CompletedProcess([], 0, 'orchestra-python:3.11\n', '')
        with patch('ssh_forced_command.shutil.which', return_value='/opt/python/bin/python3') as which, \
                patch('ssh_forced_command.subprocess.run', return_value=completed) as run:
            self.assertEqual(forced._python('python3'), '/opt/python/bin/python3')
        environment = forced.child_environment()
        which.assert_called_once_with('python3', path=environment['PATH'])
        args, kwargs = run.call_args
        self.assertEqual(args[0][:4], ['/opt/python/bin/python3', '-E', '-s', '-c'])
        self.assertIs(kwargs['stdin'], subprocess.DEVNULL)
        self.assertIs(kwargs['stdout'], subprocess.PIPE)
        self.assertIs(kwargs['stderr'], subprocess.PIPE)
        self.assertEqual(kwargs['timeout'], 5)
        self.assertEqual(kwargs['env'], environment)

    def test_a_missing_or_non_executable_interpreter_is_refused(self):
        with patch('ssh_forced_command.shutil.which', return_value=None) as which, \
                patch('ssh_forced_command.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, '--python'):
                forced._python('missing-python')
        which.assert_called_once()
        run.assert_not_called()

    def test_an_executable_that_is_not_python_is_refused(self):
        completed = subprocess.CompletedProcess([], 0, 'not-python\n', '')
        with patch('ssh_forced_command.shutil.which', return_value='/usr/bin/not-python'), \
                patch('ssh_forced_command.subprocess.run', return_value=completed):
            with self.assertRaisesRegex(ValueError, 'usable Python 3.10\\+'):
                forced._python('/usr/bin/not-python')

    def test_an_unsupported_python_version_is_refused(self):
        completed = subprocess.CompletedProcess([], 1, 'orchestra-python:3.9\n', '')
        with patch('ssh_forced_command.shutil.which', return_value='/usr/bin/python3.9'), \
                patch('ssh_forced_command.subprocess.run', return_value=completed):
            with self.assertRaisesRegex(ValueError, 'Python 3.10\\+'):
                forced._python('/usr/bin/python3.9')

    def test_a_probe_that_cannot_start_or_times_out_is_refused(self):
        failures = (OSError('not executable'),
                    subprocess.TimeoutExpired('/usr/bin/python3', 5))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch('ssh_forced_command.shutil.which', return_value='/usr/bin/python3'), \
                        patch('ssh_forced_command.subprocess.run', side_effect=failure):
                    with self.assertRaisesRegex(ValueError, '--python'):
                        forced._python('/usr/bin/python3')

    def test_an_unsafe_interpreter_is_refused_before_lookup(self):
        with patch('ssh_forced_command.shutil.which') as which:
            with self.assertRaisesRegex(ValueError, '--python'):
                forced._python('python3;touch')
        which.assert_not_called()


class WrapperExecTests(unittest.TestCase):
    """The wrapper refuses everything else and launches one fixed command line."""

    def invoke(self, original, argv=None):
        argv = argv or ['--root', ROOT, '--endpoint', ENDPOINT, '--python', PYTHON]
        out, err = io.StringIO(), io.StringIO()
        probe = subprocess.CompletedProcess([], 0, 'orchestra-python:3.11\n', '')
        # Both exec entry points are patched: `execvpe` is the one that must be used, and
        # `execvp` (which inherits the caller's environment) must never run - patching it too
        # means a regression there fails a test instead of replacing the test process.
        with patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': original}), \
                patch('ssh_forced_command.shutil.which', return_value=PYTHON), \
                patch('ssh_forced_command.subprocess.run', return_value=probe), \
                patch('ssh_forced_command.os.execvpe') as execvpe, \
                patch('ssh_forced_command.os.execvp') as execvp, \
                patch('sys.stdout', out), patch('sys.stderr', err):
            code = forced.main(argv)
        self.assertEqual(execvp.call_count, 0,
                         'os.execvp inherits the caller environment; use os.execvpe')
        return code, out.getvalue(), err.getvalue(), execvpe

    def test_a_normal_client_selection_execs_the_endpoint_with_the_fixed_root(self):
        code, out, err, execvpe = self.invoke(ENDPOINT)
        self.assertEqual(execvpe.call_args.args,
                         (PYTHON, [PYTHON, ENDPOINT, '--root', ROOT], execvpe.call_args.args[2]))
        self.assertEqual(execvpe.call_args.args[1], [PYTHON, ENDPOINT, '--root', ROOT])
        self.assertEqual(out, '')
        # The mocked exec returns, so the wrapper's fail-closed safety net reports it.
        self.assertEqual(code, 2)
        self.assertIn('did not replace this process', err)

    def test_the_launched_argv_carries_no_authority_flag(self):
        _, _, _, execvpe = self.invoke(ENDPOINT)
        launched = execvpe.call_args.args[1]
        for flag in ('--authority-store', '--authority-lock', '--require-authority'):
            self.assertNotIn(flag, launched)
        self.assertEqual(launched[2:], ['--root', ROOT])

    def test_a_caller_cannot_pass_flags(self):
        for original in (ENDPOINT + ' --root /srv/other',
                         ENDPOINT + ' --authority-store /srv/authority.json',
                         ENDPOINT + ' --require-authority',
                         '--root ' + ROOT):
            with self.subTest(original=original):
                code, out, err, execvpe = self.invoke(original)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                execvpe.assert_not_called()

    def test_a_caller_cannot_run_another_program_or_admin_py(self):
        for original in ('/srv/kit/admin.py --root %s operators add alice' % ROOT,
                         '/srv/kit/admin.py --root %s backup --all' % ROOT,
                         'bd --directory /srv/state/projects/p list',
                         '/bin/sh -c id',
                         'python3 %s --root %s' % (ENDPOINT, ROOT)):
            with self.subTest(original=original):
                code, out, err, execvpe = self.invoke(original)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertTrue(err)
                execvpe.assert_not_called()

    def test_a_refused_command_does_not_probe_the_interpreter(self):
        with patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': 'admin.py'}), \
                patch('ssh_forced_command.shutil.which') as which, \
                patch('ssh_forced_command.subprocess.run') as run, \
                patch('ssh_forced_command.os.execvpe') as execvpe, \
                patch('sys.stderr', io.StringIO()):
            self.assertEqual(forced.main(
                ['--root', ROOT, '--endpoint', ENDPOINT, '--python', PYTHON]), 2)
        which.assert_not_called()
        run.assert_not_called()
        execvpe.assert_not_called()

    def test_shell_metacharacters_never_reach_a_shell(self):
        for original in (ENDPOINT + '; rm -rf /', ENDPOINT + ' && id', '$(id)',
                         ENDPOINT + ' `id`', ENDPOINT + ' | tee /tmp/x'):
            with self.subTest(original=original):
                code, out, _, execvpe = self.invoke(original)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                execvpe.assert_not_called()

    def test_an_empty_or_unbalanced_command_is_refused(self):
        for original in ('', '   ', '"unbalanced'):
            with self.subTest(original=original):
                code, out, err, execvpe = self.invoke(original)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertTrue(err)
                execvpe.assert_not_called()

    def test_the_endpoint_defaults_to_the_one_beside_the_wrapper(self):
        if os.name != 'posix':
            self.skipTest('the wrapper and its default endpoint use POSIX absolute paths')
        code, _, _, execvpe = self.invoke(forced.default_endpoint(),
                                          argv=['--root', ROOT, '--python', PYTHON])
        self.assertEqual(execvpe.call_args.args[1][1], forced.default_endpoint())

    def test_authority_flags_in_the_wrappers_own_argv_are_refused(self):
        for extra in (['--authority-store', '/srv/authority.json'], ['--require-authority'],
                      ['--authority-lock', '/srv/authority.lock']):
            with self.subTest(extra=extra):
                with patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': ENDPOINT}), \
                        patch('ssh_forced_command.os.execvpe') as execvpe, \
                        patch('ssh_forced_command.os.execvp') as execvp, \
                        patch('sys.stderr', io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        forced.main(['--root', ROOT, '--endpoint', ENDPOINT, *extra])
                self.assertEqual(caught.exception.code, 2)
                execvpe.assert_not_called()
                execvp.assert_not_called()

    def test_relative_paths_in_the_wrappers_own_argv_are_refused(self):
        code, _, err, execvpe = self.invoke(ENDPOINT, argv=['--root', 'relative',
                                                           '--endpoint', ENDPOINT])
        self.assertEqual(code, 2)
        self.assertIn('absolute', err)
        execvpe.assert_not_called()

    def test_an_abbreviated_flag_is_not_accepted(self):
        # allow_abbrev=False: `--roo` must not silently become `--root`, and a later option
        # must not change what this installable line means.
        for argv in (['--roo', ROOT, '--endpoint', ENDPOINT],
                     ['--root', ROOT, '--end', ENDPOINT],
                     ['--root', ROOT, '--endpoint', ENDPOINT, '--pyt', PYTHON]):
            with self.subTest(argv=argv):
                with patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': ENDPOINT}), \
                        patch('ssh_forced_command.os.execvpe') as execvpe, \
                        patch('ssh_forced_command.os.execvp') as execvp, \
                        patch('sys.stderr', io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        forced.main(argv)
                self.assertEqual(caught.exception.code, 2)
                execvpe.assert_not_called()
                execvp.assert_not_called()

    def test_a_metacharacter_interpreter_in_the_wrappers_argv_is_refused(self):
        for value in ('$(touch${IFS}/tmp/canary)python3', 'python3;id', '`id`python3',
                      'python3 --flag', ''):
            with self.subTest(value=value):
                code, out, err, execvpe = self.invoke(
                    ENDPOINT, argv=['--root', ROOT, '--endpoint', ENDPOINT, '--python', value])
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertIn('--python', err)
                execvpe.assert_not_called()

    def test_the_endpoint_is_exec_d_with_a_minimal_explicit_environment(self):
        code, _, _, execvpe = self.invoke(ENDPOINT)
        launched_env = execvpe.call_args.args[2]
        self.assertNotIn('SSH_ORIGINAL_COMMAND', launched_env)
        for name in ('PYTHONPATH', 'BASH_ENV', 'ENV', 'LD_PRELOAD'):
            self.assertNotIn(name, launched_env)
        self.assertIn('PATH', launched_env)

    def test_a_forwarded_session_variable_never_reaches_the_endpoint(self):
        planted = {'SSH_ORIGINAL_COMMAND': ENDPOINT,
                   'PYTHONPATH': '/tmp/attacker-modules',
                   'BASH_ENV': '/tmp/attacker-bashenv',
                   'ENV': '/tmp/attacker-env',
                   'LD_PRELOAD': '/tmp/attacker.so',
                   'ATTACKER_ONLY': 'planted',
                   'PATH': '/usr/bin:/bin', 'HOME': '/home/beads',
                   'LANG': 'en_GB.UTF-8', 'LC_ALL': 'en_GB.UTF-8'}
        with patch.dict(os.environ, planted), \
                patch('ssh_forced_command.shutil.which', return_value=PYTHON), \
                patch('ssh_forced_command.subprocess.run',
                      return_value=subprocess.CompletedProcess(
                          [], 0, 'orchestra-python:3.11\n', '')), \
                patch('ssh_forced_command.os.execvpe') as execvpe, \
                patch('ssh_forced_command.os.execvp') as execvp, \
                patch('sys.stderr', io.StringIO()):
            forced.main(['--root', ROOT, '--endpoint', ENDPOINT, '--python', PYTHON])
        self.assertEqual(execvp.call_count, 0)
        launched_env = execvpe.call_args.args[2]
        self.assertEqual(launched_env['PATH'], '/usr/bin:/bin')
        self.assertEqual(launched_env['HOME'], '/home/beads')
        self.assertEqual(launched_env['LANG'], 'en_GB.UTF-8')
        self.assertEqual(launched_env['LC_ALL'], 'en_GB.UTF-8')
        for name in ('PYTHONPATH', 'BASH_ENV', 'ENV', 'LD_PRELOAD', 'ATTACKER_ONLY',
                     'SSH_ORIGINAL_COMMAND'):
            self.assertNotIn(name, launched_env)


class ChildEnvironmentTests(unittest.TestCase):
    """`child_environment` forwards a named few variables and drops everything else."""

    def test_only_path_home_locale_and_kit_variables_are_forwarded(self):
        source = {'PATH': '/usr/bin', 'HOME': '/home/beads', 'LANG': 'C', 'LC_TIME': 'C',
                  'LC_ALL': 'C.UTF-8', 'PYTHONPATH': '/tmp/x', 'BASH_ENV': '/tmp/y',
                  'ENV': '/tmp/e', 'LD_PRELOAD': '/tmp/z', 'SSH_ORIGINAL_COMMAND': 'evil',
                  'TERM': 'xterm'}
        self.assertEqual(forced.child_environment(source),
                         {'PATH': '/usr/bin', 'HOME': '/home/beads', 'LANG': 'C',
                          'LC_ALL': 'C.UTF-8', 'LC_TIME': 'C'})

    def test_a_missing_path_falls_back_to_the_platform_default(self):
        self.assertEqual(forced.child_environment({})['PATH'], os.defpath)

    def test_a_missing_home_is_not_invented(self):
        self.assertNotIn('HOME', forced.child_environment({}))

    def test_the_kit_can_name_a_variable_it_sets_itself(self):
        with patch.dict(forced.KIT_ENVIRONMENT, {'KIT_SETTING': '1'}):
            self.assertEqual(forced.child_environment({})['KIT_SETTING'], '1')


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

    def test_a_second_key_line_is_refused(self):
        # Regression (docs-and-small, item 1): the helper and OPERATIONS said several keys
        # are refused, but the second valid line was silently ignored.
        for text in ('%s\n%s\n' % (KEY_LINE, SECOND_KEY_LINE),
                     '%s\n# a comment\n%s\n' % (KEY_LINE, SECOND_KEY_LINE),
                     '# lead\n%s\n%s\n' % (KEY_LINE, SECOND_KEY_LINE)):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, 'second'):
                    admin.public_key_line(text, 'two-keys.pub')

    def test_comments_do_not_count_as_a_second_key(self):
        text = '%s\n# a second comment\n\n' % KEY_LINE
        self.assertEqual(admin.public_key_line(text, 'key.pub')[1], KEY_BODY)


class AuthorizedKeyLineTests(unittest.TestCase):
    """The helper prints the exact confined contributor line and the bare operator line."""

    def lines(self, **kwargs):
        return admin.authorized_key_lines('/srv/state', '/srv/kit', 'ssh-ed25519', KEY_BODY,
                                          'alex@laptop', **kwargs)

    def test_the_contributor_line_is_exact(self):
        expected = ('command="%s -E -s /srv/kit/ssh_forced_command.py '
                    '--root /srv/state --endpoint /srv/kit/endpoint.py",'
                    'restrict,no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding '
                    'ssh-ed25519 %s alex@laptop' % (PYTHON, KEY_BODY))
        self.assertEqual(self.lines(python=PYTHON)['contributor'], expected)

    def test_the_default_interpreter_is_absolute(self):
        default = admin.default_authorized_key_python()
        self.assertTrue(default.startswith('/'), default)
        self.assertIn('command="%s -E -s ' % default, self.lines()['contributor'])
        self.assertEqual(self.lines()['python'], default)

    def test_the_default_interpreter_falls_back_to_usr_bin_python3(self):
        with patch.object(admin.sys, 'executable', 'C:\\Python311\\python.exe'):
            self.assertEqual(admin.default_authorized_key_python(), '/usr/bin/python3')

    def test_the_interpreter_runs_with_E_and_s_but_not_I(self):
        contributor = self.lines(python=PYTHON)['contributor']
        self.assertIn('command="%s -E -s ' % PYTHON, contributor)
        # -I also removes the script's directory from sys.path, which the kit's modules need.
        self.assertNotIn(' -I ', contributor)
        self.assertEqual(list(admin.AUTHORIZED_KEY_PYTHON_FLAGS), ['-E', '-s'])

    def test_an_absolute_interpreter_appears_verbatim(self):
        lines = self.lines(python=PYTHON)
        self.assertTrue(lines['contributor'].startswith('command="%s -E -s ' % PYTHON))

    def test_a_bare_interpreter_name_is_accepted(self):
        for value in ('python3', 'python3.11'):
            with self.subTest(value=value):
                self.assertIn('command="%s -E -s ' % value, self.lines(python=value)['contributor'])

    def test_a_python_value_a_shell_would_expand_is_refused(self):
        # Regression (helper-python-metacharacters): sshd runs command= through the account
        # shell, so `$(touch${IFS}/tmp/canary)python3` created the canary on every connection.
        for value in ('$(touch${IFS}/tmp/canary)python3', '`id`python3', 'python3;id',
                      'python3|id', 'python3&id', 'python3(id)', 'python3>x', 'python3</dev/null',
                      'python3\n/tmp/x', 'python3 -E', 'python3\t', '', '-E', '-S',
                      'py"thon3', "py'thon3", '../python3', './python3', 'python3/',
                      '/srv/kit/python 3', '/srv/kit/py$thon3', '/srv/kit/py`thon3`'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, '--python'):
                    self.lines(python=value)

    def test_the_sshd_options_start_with_restrict_and_keep_the_four(self):
        options = list(admin.CONTRIBUTOR_KEY_OPTIONS)
        self.assertEqual(options[0], 'restrict')
        for option in ('no-pty', 'no-port-forwarding', 'no-agent-forwarding', 'no-X11-forwarding'):
            self.assertIn(option, options)
        for option in options:
            self.assertIn(option, self.lines(python=PYTHON)['contributor'])
        self.assertTrue(self.lines(python=PYTHON)['contributor']
                        .startswith('command="%s -E -s ' % PYTHON))

    def test_the_options_precede_the_key_after_the_restrict_lead(self):
        contributor = self.lines(python=PYTHON)['contributor']
        self.assertIn('",restrict,no-pty,', contributor)

    def test_the_operator_line_is_the_unrestricted_bare_key(self):
        self.assertEqual(self.lines()['operator'], KEY_LINE)
        self.assertNotIn('command=', self.lines()['operator'])
        self.assertNotIn('restrict', self.lines()['operator'])

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
        self.assertIn('Unrestricted', payload['operator_note'])
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

    def test_the_printed_interpreter_is_absolute_with_E_and_s(self):
        payload, _ = self.invoke('--role', 'contributor')
        interpreter = payload['python']
        self.assertTrue(interpreter.startswith('/'), interpreter)
        self.assertIn('command="%s -E -s ' % interpreter, payload['contributor'])
        self.assertEqual(payload['contributor_options'][0], 'restrict')

    def test_a_metacharacter_interpreter_is_refused_by_the_command(self):
        # The reproduction for helper-python-metacharacters at the command level: this used
        # to print a line whose command= created /tmp/canary on every connection.
        for value in ('$(touch${IFS}/tmp/ssh89-canary)python3', '`touch${IFS}/tmp/x`python3',
                      'python3;touch${IFS}/tmp/x'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, '--python'):
                    self.invoke('--role', 'contributor', '--python', value)

    def test_a_key_file_with_two_keys_is_refused_by_the_command(self):
        self.key_file.write_text(KEY_LINE + '\n' + SECOND_KEY_LINE + '\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'second'):
            self.invoke('--role', 'contributor')

    def test_the_notes_state_the_coupled_pair_and_the_actor_boundary(self):
        payload, _ = self.invoke('--role', 'contributor')
        notes = ' '.join(payload['notes'])
        for phrase in ('coupled pair', 'Permission denied', 'second SSH session',
                       'not to an actor'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, notes)


@unittest.skipUnless(os.name == 'posix', 'the wrapper replaces itself with os.execvpe')
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
            'import json, os, sys\n'
            'print(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read(), '
            '"env": dict(os.environ)}))\n',
            encoding='utf-8')

    def run_wrapper(self, original, data='{"project": "example"}', **extra):
        env = dict(os.environ, SSH_ORIGINAL_COMMAND=original, **extra)
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

    def test_a_forwarded_session_environment_does_not_reach_the_endpoint(self):
        # The environment-and-sshd-prerequisites reproduction: PYTHONPATH/BASH_ENV act on an
        # interpreter before any kit code runs, so the wrapper can only close the endpoint's
        # own process - and does.
        completed = self.run_wrapper(str(self.endpoint), data='{}',
                                     PYTHONPATH='/tmp/attacker-modules',
                                     BASH_ENV='/tmp/attacker-bashenv',
                                     LD_PRELOAD='/tmp/attacker.so',
                                     ATTACKER_ONLY='planted')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        received = json.loads(completed.stdout)['env']
        for name in ('PYTHONPATH', 'BASH_ENV', 'LD_PRELOAD', 'ATTACKER_ONLY',
                     'SSH_ORIGINAL_COMMAND'):
            with self.subTest(name=name):
                self.assertNotIn(name, received)
        self.assertIn('PATH', received)

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
        self.assertTrue(completed.stderr.startswith('ssh_forced_command: '))
        self.assertIn('refused', completed.stderr)

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

    def test_operations_states_the_sshd_prerequisites_and_how_to_check_them(self):
        text = self.text('OPERATIONS.md')
        for phrase in ('sshd settings the boundary needs', 'PermitUserEnvironment no',
                       'AcceptEnv LANG LC_*', 'sshd -T', 'permituserenvironment',
                       'acceptenv', 'SetEnv', 'PYTHONPATH', 'BASH_ENV'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_operations_states_the_hardening_and_the_coupled_pair(self):
        text = self.text('OPERATIONS.md')
        for phrase in ('-E -s', 'restrict', '~/.ssh/rc', 'explicit environment',
                       'coupled pair', 'Permission denied', 'second session'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_operations_states_confinement_does_not_bind_a_key_to_an_actor(self):
        text = self.text('OPERATIONS.md')
        self.assertIn('binds the key to the endpoint, not to an actor', text)
        self.assertIn('operator-gated and reserved operations', text)

    def test_operations_states_the_single_key_rule(self):
        normalized = ' '.join(self.text('OPERATIONS.md').split())
        self.assertIn('a second key line', normalized)

    def test_the_client_example_mentions_forced_command(self):
        self.assertIn('forced_command', (KIT / 'client.example.json').read_text(encoding='utf-8'))

    def test_the_onboarding_texts_mention_forced_command(self):
        for name in ('docs/ONBOARDING.md', 'templates/PROJECT_ONBOARDING.md'):
            with self.subTest(name=name):
                self.assertIn('forced_command', (KIT / name).read_text(encoding='utf-8'))

    def test_http_deployment_points_at_the_confined_setup(self):
        text = self.text('HTTP_DEPLOYMENT.md')
        self.assertIn('ssh_forced_command.py', text)
        self.assertIn('cannot choose the remote command', text)

    def test_readme_points_at_the_confined_setup(self):
        self.assertIn('OPERATIONS.md', (KIT / 'README.md').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
