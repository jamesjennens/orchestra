import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
from client import request
import client

SSH = {'host': 'sample', 'endpoint': '/srv/kit/endpoint.py', 'root': '/srv/state'}
LOCAL = {'transport': 'local', 'endpoint': '/srv/kit/endpoint.py', 'root': '/srv/state',
         'python': '/usr/bin/python3'}
SSH_COMMAND = 'python3 /srv/kit/endpoint.py --root /srv/state'
ANSWER = subprocess.CompletedProcess([], 0, json.dumps({'returncode': 0, 'stdout': 'ok', 'stderr': ''}), '')
WRAPPER_ARGS = ['--config', 'client.json', '--actor', 'alice/deep', '--',
                'comments', 'add', 'kittrial-8nr.3', 'two words', '--json']
SH = shutil.which('sh')


class ClientTransportTests(unittest.TestCase):
    """SSH stays the default; local transport is explicit and never a fallback."""

    def capture(self, config, args=('list',), actor='alice', **kwargs):
        with patch('client.subprocess.run', return_value=ANSWER) as run:
            request(config, 'sample', actor, list(args), **kwargs)
            return run.call_args

    def payload(self, call):
        return json.loads(call.kwargs['input'])

    def test_request_api_is_unchanged(self):
        parameters = inspect.signature(request).parameters
        self.assertEqual(list(parameters), ['config', 'project', 'actor', 'args', 'action', 'path'])
        self.assertEqual(parameters['action'].default, 'bd')
        self.assertIsNone(parameters['path'].default)

    def test_version_command_works_without_client_config(self):
        with patch.object(sys, 'argv', ['client.py', '--version']):
            with patch('builtins.print') as output:
                self.assertEqual(client.main(), 0)
        self.assertIn('Orchestra client: version 0.1.0', output.call_args.args[0])

    def test_copied_standalone_client_has_version_command(self):
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / 'client.py'
            copied.write_text((Path(client.__file__).read_text(encoding='utf-8')),
                              encoding='utf-8')
            result = subprocess.run([sys.executable, str(copied), '--version'],
                                    capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Orchestra client: version 0.1.0, source unknown', result.stdout)

    def test_standalone_client_ignores_adjacent_unrelated_version_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'client.py').write_text(Path(client.__file__).read_text(encoding='utf-8'), encoding='utf-8')
            (root / 'version.py').write_text(
                'def report(*args, **kwargs): return {"component": "wrong", "version": "9.9.9", "source_commit": "bad"}\n'
                'def line(metadata): return "wrong"\n', encoding='utf-8')
            result = subprocess.run([sys.executable, str(root / 'client.py'), '--version'],
                                    capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Orchestra client: version 0.1.0, source unknown', result.stdout)

    def test_resume_cli_stdout_is_json_with_structured_unknown_provenance(self):
        response = {'returncode': 0, 'stdout': json.dumps({'resume': {'request_id': 'r'}}), 'stderr': ''}
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text('{}', encoding='utf-8')
            with patch.object(sys, 'argv', ['client.py', '--config', str(config),
                                            '--project', 'example', '--actor', 'worker-1',
                                            '--', 'session', 'resume']), \
                    patch.object(client, 'request', return_value=response), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(client.main(), 0)
        rendered = json.loads(output.getvalue())
        self.assertEqual(rendered['resume']['request_id'], 'r')
        self.assertEqual(rendered['provenance']['parity'], 'unknown')
        self.assertEqual(rendered['provenance']['kit']['source_commit'], 'unknown')

    def test_resume_cli_reports_differing_revisions_without_corrupting_json(self):
        response = {'returncode': 0, 'stdout': json.dumps({
            'resume': {'request_id': 'r'},
            'provenance': {'kit': {'component': 'kit', 'version': '0.1.0',
                                   'source_commit': 'server-revision', 'path': '/srv/kit'}},
        }), 'stderr': ''}
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text('{}', encoding='utf-8')
            with patch.object(sys, 'argv', ['client.py', '--config', str(config),
                                            '--project', 'example', '--actor', 'worker-1',
                                            '--', 'session', 'resume']), \
                    patch.object(client, 'request', return_value=response), \
                    patch.object(client, 'report', return_value={
                        'component': 'client', 'version': '0.1.0',
                        'source_commit': 'client-revision', 'path': directory}), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(client.main(), 0)
        rendered = json.loads(output.getvalue())
        self.assertEqual(rendered['provenance']['parity'], 'mismatch')

    def test_failed_resume_preserves_diagnostics_and_does_not_add_stdout_metadata(self):
        response = {'returncode': 7, 'stdout': '', 'stderr': 'resume failed\n'}
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text('{}', encoding='utf-8')
            with patch.object(sys, 'argv', ['client.py', '--config', str(config),
                                            '--project', 'example', '--actor', 'worker-1',
                                            '--', 'session', 'resume']), \
                    patch.object(client, 'request', return_value=response), \
                    patch('sys.stdout', new_callable=io.StringIO) as output, \
                    patch('sys.stderr', new_callable=io.StringIO) as error:
                self.assertEqual(client.main(), 7)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(error.getvalue(), 'resume failed\n')

    def test_onboard_output_includes_client_metadata_and_detects_mismatch(self):
        response = {'returncode': 0, 'stdout': 'Orchestra kit: version 0.0.9, source unknown\n',
                    'stderr': ''}
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text('{}', encoding='utf-8')
            with patch.object(sys, 'argv', ['client.py', '--config', str(config),
                                            '--project', 'example', '--actor', 'worker-1',
                                            '--', 'onboard']), patch.object(client, 'request',
                                            return_value=response):
                output = io.StringIO()
                with patch.object(sys, 'stdout', output):
                    self.assertEqual(client.main(), 0)
        rendered = output.getvalue()
        self.assertIn('Orchestra client: version 0.1.0', rendered)
        self.assertIn('Orchestra version mismatch: client=0.1.0, kit=0.0.9', rendered)

    def test_console_child_is_hidden_without_losing_captured_output(self):
        with patch('client.subprocess.CREATE_NO_WINDOW', 0x08000000, create=True):
            call = self.capture(SSH)
        self.assertEqual(call.kwargs['creationflags'], 0x08000000)
        self.assertTrue(call.kwargs['capture_output'])
        self.assertFalse(call.kwargs['shell'])

    def test_ssh_command_is_unchanged_when_transport_is_absent(self):
        call = self.capture(SSH)
        self.assertEqual(call.args[0], ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                        'sample', SSH_COMMAND])
        self.assertIs(call.kwargs['shell'], False)

    def test_local_runs_the_endpoint_as_argv_without_a_shell(self):
        call = self.capture(LOCAL)
        argv = call.args[0]
        self.assertEqual(argv, ['/usr/bin/python3', '/srv/kit/endpoint.py', '--root', '/srv/state'])
        self.assertIs(call.kwargs['shell'], False)
        self.assertNotIn('ssh', argv)
        self.assertNotIn('ssh', call.kwargs['input'])

    def test_local_config_wins_over_any_present_host(self):
        argv = self.capture(dict(LOCAL, host='sample')).args[0]
        self.assertEqual(argv[0], '/usr/bin/python3')
        self.assertNotIn('ssh', argv)

    def test_local_python_is_configurable_and_defaults_to_python3(self):
        self.assertEqual(self.capture(dict(LOCAL, python='/opt/venvs/beads/bin/python3.11')).args[0][0],
                         '/opt/venvs/beads/bin/python3.11')
        self.assertEqual(self.capture({k: v for k, v in LOCAL.items() if k != 'python'}).args[0][0],
                         'python3')

    def test_envelope_and_attachment_semantics_are_identical(self):
        text = 'intent, with $(shell) and "quotes"'
        with tempfile.TemporaryDirectory() as d:
            plan = Path(d) / 'plan.md'
            plan.write_text(text, encoding='utf-8')
            args = ['create', 'title', '--body-file', str(plan)]
            ssh_call = self.capture(SSH, args, action='bd', path='CURRENT.md')
            local_call = self.capture(LOCAL, args, action='bd', path='CURRENT.md')
        self.assertEqual(ssh_call.args[0][0], 'ssh')
        self.assertEqual(local_call.args[0][0], '/usr/bin/python3')
        self.assertEqual(ssh_call.kwargs['input'], local_call.kwargs['input'])
        payload = self.payload(local_call)
        self.assertEqual(sorted(payload), ['action', 'actor', 'args', 'attachments', 'path', 'project'])
        self.assertEqual(payload['actor'], 'alice')
        self.assertEqual(payload['args'], ['create', 'title', '@attachment:0'])
        self.assertEqual(payload['attachments'], {'0': {'flag': '--body-file', 'text': text}})
        self.assertEqual(payload['path'], 'CURRENT.md')
        self.assertNotIn(text, ' '.join(local_call.args[0]))

    def test_actor_is_validated_in_the_client_for_both_transports(self):
        for transport in (SSH, LOCAL):
            for actor in ('', ' ', 'alice smith', 'a' * 97, '-alice', '@alice', None, 7):
                with self.subTest(actor=actor):
                    with patch('client.subprocess.run', return_value=ANSWER) as run:
                        with self.assertRaises(ValueError):
                            request(transport, 'sample', actor, ['list'])
                        self.assertEqual(run.call_count, 0)

    def test_actor_the_endpoint_accepts_is_forwarded_verbatim(self):
        for actor in ('a', 'hermes-deepseek-transport', 'a' * 96, 'user/one', 'x.y@z'):
            with self.subTest(actor=actor):
                self.assertEqual(self.payload(self.capture(LOCAL, actor=actor))['actor'], actor)

    def test_unknown_transport_is_refused_without_falling_back(self):
        for transport in ('auto', 'SSH', 'LOCAL', ' local', '', None, 1, True):
            with self.subTest(transport=transport):
                with patch('client.subprocess.run', return_value=ANSWER) as run:
                    with self.assertRaises(ValueError):
                        request(dict(SSH, transport=transport), 'sample', 'alice', ['list'])
                    self.assertEqual(run.call_count, 0)

    def test_incomplete_or_unsafe_local_config_never_falls_back_to_ssh(self):
        cases = [
            {'transport': 'local', 'endpoint': '/srv/kit/endpoint.py'},
            {'transport': 'local', 'root': '/srv/state'},
            {'transport': 'local', 'host': 'sample', 'root': '/srv/state'},
            {'transport': 'local', 'endpoint': 'relative/endpoint.py', 'root': '/srv/state'},
            {'transport': 'local', 'endpoint': 'C:\\kit\\endpoint.py', 'root': '/srv/state'},
            {'transport': 'local', 'endpoint': '/srv/kit/endpoint.py', 'root': '/srv/state', 'python': ''},
            {'transport': 'local', 'endpoint': '/srv/kit/endpoint.py', 'root': '/srv/state', 'python': '-E'},
            {'transport': 'local', 'endpoint': '/srv/kit/endpoint.py', 'root': '/srv/state',
             'python': 'python3 -X utf8'},
            {'transport': 'local', 'endpoint': '/srv/kit/endpoint.py', 'root': '/srv/state',
             'python': ['python3']},
        ]
        for config in cases:
            with self.subTest(config=config):
                with patch('client.subprocess.run', return_value=ANSWER) as run:
                    with self.assertRaises(ValueError):
                        request(config, 'sample', 'alice', ['list'])
                    self.assertEqual(run.call_count, 0)

    def test_stdin_attachment_is_refused_on_both_transports(self):
        for transport in (SSH, LOCAL):
            with self.subTest(transport=transport.get('transport', 'ssh')):
                with patch('client.subprocess.run', return_value=ANSWER) as run:
                    with self.assertRaises(ValueError):
                        request(transport, 'sample', 'alice', ['create', 't', '--body-file', '-'])
                    self.assertEqual(run.call_count, 0)

    def test_failures_are_labelled_and_never_retried(self):
        with patch('client.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 3, '', 'boom')) as run:
            with self.assertRaisesRegex(RuntimeError, r'^Local endpoint failed \(3\); outcome may be uncertain'):
                request(LOCAL, 'sample', 'alice', ['list'])
            self.assertEqual(run.call_count, 1)
        with patch('client.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 255, '', 'offline')) as run:
            with self.assertRaisesRegex(RuntimeError, r'^SSH failed \(255\); outcome may be uncertain'):
                request(SSH, 'sample', 'alice', ['list'])
            self.assertEqual(run.call_count, 1)
        for transport, label in ((LOCAL, 'Local endpoint'), (SSH, 'SSH')):
            with self.subTest(label=label):
                with patch('client.subprocess.run',
                           side_effect=subprocess.TimeoutExpired('x', 150)) as run:
                    with self.assertRaisesRegex(RuntimeError,
                                                r'^%s timed out; outcome may be uncertain' % label):
                        request(transport, 'sample', 'alice', ['list'])
                    self.assertEqual(run.call_count, 1)

    def test_unreadable_endpoint_response_is_not_retried(self):
        with patch('client.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, 'not json', '')) as run:
            with self.assertRaisesRegex(RuntimeError, 'Invalid endpoint response'):
                request(LOCAL, 'sample', 'alice', ['list'])
            self.assertEqual(run.call_count, 1)


class WrapperSourceTests(unittest.TestCase):
    """Static checks that hold on every platform, including where a shell is absent."""

    def setUp(self):
        self.texts = {name: (KIT / name).read_text(encoding='utf-8')
                      for name in ('beads.cmd', 'beads.sh')}

    def test_wrappers_delegate_to_the_client_without_powershell(self):
        for name, text in self.texts.items():
            with self.subTest(name=name):
                self.assertIn('client.py', text)
                for line in text.splitlines():
                    self.assertFalse(line.strip().lower().startswith(('powershell', 'pwsh')), line)
                self.assertNotIn('executionpolicy', text.lower())

    def test_cmd_wrapper_selects_beads_python_then_python(self):
        text = self.texts['beads.cmd']
        self.assertIn('set "PY=%BEADS_PYTHON%"', text)
        self.assertIn('if not defined PY set "PY=python"', text)
        self.assertIn('set "KIT=%~dp0"', text)

    def test_sh_wrapper_selects_beads_python_then_python3(self):
        text = self.texts['beads.sh']
        self.assertIn('${BEADS_PYTHON:-python3}', text)
        self.assertIn('$(dirname -- "$0")', text)


def cmd_stub(directory, name='stub.cmd'):
    """A stand-in interpreter: records the argument string it received, exits 3."""
    path = Path(directory) / name
    path.write_text('@echo off\n>"%BEADS_STUB_LOG%" echo %*\nexit /b 3\n', encoding='utf-8')
    return path


def sh_stub(directory, name='stub'):
    """A stand-in interpreter for POSIX shells: one argument per line, exits 3."""
    path = Path(directory) / name
    path.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$BEADS_STUB_LOG"\nexit 3\n', encoding='utf-8')
    path.chmod(0o755)
    return path


@unittest.skipUnless(os.name == 'nt', 'Windows CMD wrapper')
class CmdWrapperTests(unittest.TestCase):
    def invoke(self, cwd, env, script=None):
        command = 'cmd /c ""%s" %s"' % (script or KIT / 'beads.cmd',
                                        subprocess.list2cmdline(WRAPPER_ARGS))
        return subprocess.run(command, cwd=str(cwd), env=env, capture_output=True, text=True)

    def test_forwards_arguments_and_exit_code_from_any_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'args.txt'
            env = dict(os.environ, BEADS_PYTHON=str(cmd_stub(d)), BEADS_STUB_LOG=str(log))
            completed = self.invoke(Path(d), env)
            self.assertTrue(log.is_file(), completed.stderr)
            recorded = log.read_text(encoding='utf-8').strip()
        self.assertEqual(completed.returncode, 3)
        self.assertIn('client.py', recorded)
        self.assertIn(str(KIT).lower(), recorded.lower())
        for token in ('alice/deep', 'kittrial-8nr.3', 'two words', '--json'):
            self.assertIn(token, recorded)

    def test_quotes_beads_python_containing_spaces(self):
        with tempfile.TemporaryDirectory() as d:
            spaced = Path(d) / 'py dir'
            spaced.mkdir()
            log = Path(d) / 'args.txt'
            env = dict(os.environ, BEADS_PYTHON=str(cmd_stub(spaced)), BEADS_STUB_LOG=str(log))
            completed = self.invoke(Path(d), env)
            self.assertTrue(log.is_file(), completed.stderr)
        self.assertEqual(completed.returncode, 3)

    def test_defaults_to_python_on_path_when_beads_python_is_unset(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'args.txt'
            cmd_stub(d, 'python.cmd')
            env = {k: v for k, v in os.environ.items() if k != 'BEADS_PYTHON'}
            env['BEADS_STUB_LOG'] = str(log)
            env['PATH'] = d + os.pathsep + env.get('PATH', '')
            completed = self.invoke(Path(d), env)
            self.assertTrue(log.is_file(), completed.stderr)
        self.assertEqual(completed.returncode, 3)


@unittest.skipUnless(os.name == 'nt', 'Windows real-interpreter wrapper')
class CmdInterpreterTests(unittest.TestCase):
    """Use an EXE so control returns to the wrapper's exit-status line."""

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        interpreter_dir = cls.root / 'python dir!literal!'
        venv.EnvBuilder(with_pip=False).create(interpreter_dir)
        cls.python = interpreter_dir / 'Scripts' / 'python.exe'
        cls.kit = cls.root / 'kit with spaces'
        cls.kit.mkdir()
        shutil.copyfile(KIT / 'beads.cmd', cls.kit / 'beads.cmd')
        (cls.kit / 'client.py').write_text(
            'import json, sys\nprint(json.dumps(sys.argv))\nsys.exit(3)\n',
            encoding='utf-8')

    def invoke(self, arguments, *, delayed=False, shadow=None, default_python=False):
        env = {k: v for k, v in os.environ.items()
               if k.upper() not in ('BEADS_PYTHON', 'ERRORLEVEL', 'LITERAL')}
        if default_python:
            env['PATH'] = str(self.python.parent) + os.pathsep + env.get('PATH', '')
        else:
            env['BEADS_PYTHON'] = str(self.python)
        if shadow is not None:
            env['ERRORLEVEL'] = shadow
        command = 'cmd /d /v:%s /s /c ""%s" %s"' % (
            'on' if delayed else 'off', self.kit / 'beads.cmd',
            subprocess.list2cmdline(arguments))
        completed = subprocess.run(command, cwd=str(self.root), env=env,
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertEqual(json.loads(completed.stdout),
                         [str(self.kit / 'client.py'), *arguments])

    def test_exact_arguments_and_real_interpreter_exit_code(self):
        self.invoke([*WRAPPER_ARGS, '', ' leading and trailing ',
                     'quoted "two words"', 'ampersand & pipe | literal',
                     'bang!literal!', 'C:\\a directory\\'])

    def test_inherited_errorlevel_cannot_mask_failure(self):
        self.invoke(WRAPPER_ARGS, shadow='0')

    def test_inherited_delayed_expansion_preserves_interpreter_path(self):
        self.invoke(WRAPPER_ARGS, delayed=True)

    def test_default_python_executable_on_path(self):
        self.invoke(WRAPPER_ARGS, default_python=True)


@unittest.skipUnless(SH, 'POSIX shell')
class ShWrapperTests(unittest.TestCase):
    def script(self):
        return (KIT / 'beads.sh').as_posix()

    def invoke(self, cwd, env):
        return subprocess.run([SH, self.script(), *WRAPPER_ARGS], cwd=str(cwd), env=env,
                              capture_output=True, text=True)

    def test_forwards_arguments_and_exit_code_from_any_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'args.txt'
            env = dict(os.environ, BEADS_PYTHON=sh_stub(d).as_posix(),
                       BEADS_STUB_LOG=log.as_posix())
            completed = self.invoke(Path(d), env)
            self.assertTrue(log.is_file(), completed.stderr)
            recorded = log.read_text(encoding='utf-8').splitlines()
        self.assertEqual(completed.returncode, 3)
        self.assertEqual(recorded[1:], WRAPPER_ARGS)
        self.assertEqual(os.path.basename(recorded[0]), 'client.py')
        if os.name == 'posix':
            self.assertEqual(Path(recorded[0]).resolve(), (KIT / 'client.py').resolve())
        else:
            # MSYS sh reports the kit in its own path form; check it stayed absolute.
            self.assertTrue(recorded[0].startswith('/') or ':' in recorded[0], recorded[0])

    def test_quotes_beads_python_containing_spaces(self):
        with tempfile.TemporaryDirectory() as d:
            spaced = Path(d) / 'py dir'
            spaced.mkdir()
            log = Path(d) / 'args.txt'
            env = dict(os.environ, BEADS_PYTHON=sh_stub(spaced).as_posix(),
                       BEADS_STUB_LOG=log.as_posix())
            completed = self.invoke(Path(d), env)
            self.assertTrue(log.is_file(), completed.stderr)
        self.assertEqual(completed.returncode, 3)

    @unittest.skipUnless(os.name == 'posix', 'POSIX PATH lookup')
    def test_defaults_to_python3_on_path_when_beads_python_is_unset(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'args.txt'
            sh_stub(d, 'python3')
            env = {k: v for k, v in os.environ.items() if k != 'BEADS_PYTHON'}
            env['BEADS_STUB_LOG'] = log.as_posix()
            env['PATH'] = d + os.pathsep + env.get('PATH', '')
            completed = self.invoke(Path(d), env)
            self.assertTrue(log.is_file(), completed.stderr)
        self.assertEqual(completed.returncode, 3)


if __name__ == '__main__':
    unittest.main()
