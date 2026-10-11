"""An installation setting that accepts bound keys only (kittrial-5bb.196).

Slice 4 of docs/COORDINATORS_PER_PROJECT_DESIGN.md, and migration steps 6 and 7. With
``deployment.private.json``'s ``bound_keys_only`` true, the forced command refuses an
authorized_keys line that names no project or no principal, ``setup-status`` reports the
setting, and the printing and listing commands name the lines that must be reprinted.
``admin.py bound-keys-only`` turns it on, off and reports it, on the deployment operator
allowlist and audited.

The test that matters is ``test_an_installation_that_configures_nothing_is_unchanged``: with
the setting absent or false, the line the wrapper launches is the same line as before, bound or
unbound, and every other reader of the setting reads off.
"""
import base64
import builtins
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import admin
import ssh_forced_command as forced

try:
    import endpoint
except ImportError:                                   # fcntl: the endpoint is POSIX only
    endpoint = None

POSIX = unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
POSIX_SHELL = unittest.skipUnless(os.name == 'posix', 'the printed line needs an absolute Linux path')
KEY_BODY = base64.b64encode(b'orchestra-bound-keys-only-key').decode()
MINE = 'lane:orc-coord'
PERSON = 'person:orc-coord'
OPERATOR = 'ops'
CLIENT = '/srv/kit/endpoint.py'
ROOT = '/srv/state'
PYTHON = '/usr/bin/python3'

#: The scratch home this WHOLE module reads through ``Path.home()``; no test of this module may
#: open the runner's real ``~/.ssh/authorized_keys`` (kittrial-5bb.196 review, item 1: 38 opens
#: of the runner's own key file). `bound_keys_only_switch`/`authorized_keys_listing` fall back
#: to `Path.home()/.ssh/authorized_keys` when no file is passed, so the fallback is patched for
#: every test here, and one guard test asserts no `authorized_keys` outside it is opened.
SCRATCH_HOME = None
_SCRATCH_HOME_PATCH = None


def setUpModule():
    global SCRATCH_HOME, _SCRATCH_HOME_PATCH
    SCRATCH_HOME = Path(tempfile.mkdtemp(prefix='bound-keys-only-home-'))
    (SCRATCH_HOME/'.ssh').mkdir()
    (SCRATCH_HOME/'.ssh'/'authorized_keys').write_text('', encoding='utf-8')
    _SCRATCH_HOME_PATCH = mock.patch.object(Path, 'home', return_value=SCRATCH_HOME)
    _SCRATCH_HOME_PATCH.start()


def tearDownModule():
    _SCRATCH_HOME_PATCH.stop()
    shutil.rmtree(SCRATCH_HOME, ignore_errors=True)


def runtime(base, *projects):
    """A runtime root with initialized-looking projects."""
    root = Path(base)/'rt'
    for name in projects:
        (root/'projects'/name/'.beads').mkdir(parents=True)
        (root/'projects'/name/'.beads'/'metadata.json').write_text('{}', encoding='utf-8')
    return root


def write_settings(root, value=None, operators=(OPERATOR,)):
    """The installation's own settings file; ``value`` None writes no bound_keys_only key."""
    document = {'password': 'x'}
    if operators:
        document['operators'] = list(operators)
    if value is not None:
        document['bound_keys_only'] = value
    (root/'deployment.private.json').write_text(json.dumps(document), encoding='utf-8')


def key_line_text(root, kit=None, bound='', extra='', comment='scratch'):
    """One authorized_keys line whose forced command runs this kit's wrapper on ``root``."""
    kit = KIT if kit is None else kit
    return ('command="/usr/bin/python3 -E -s %s/ssh_forced_command.py --root %s --endpoint %s/endpoint.py%s%s",'
            'restrict,no-pty ssh-ed25519 %s %s'
            % (kit.as_posix(), root.as_posix(), kit.as_posix(), extra, bound, KEY_BODY, comment))


class ScratchHomeGuardTests(unittest.TestCase):
    """Item 1: no test of this module may read the runner's real key file.

    The module patches ``Path.home`` for every test (``setUpModule``). This test drives the
    default-path calls and records every file the module opens, so a future test or code change
    that reaches the real ``~/.ssh/authorized_keys`` fails here rather than in somebody's CI.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(os.path.realpath(self.tmp.name), 'alpha')
        write_settings(self.root, None)

    def test_the_modules_default_path_is_the_scratch_home_and_nothing_else(self):
        real_key = str(Path(os.path.expanduser('~')).resolve()/'.ssh'/'authorized_keys')
        opened = []
        real_open = builtins.open

        def recording(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        with mock.patch.object(builtins, 'open', recording):
            admin.bound_keys_only_switch(self.root, 'status', OPERATOR)
            admin.bound_keys_only_switch(self.root, 'on', OPERATOR)
        keys = [name for name in opened if name.endswith('authorized_keys')]
        self.assertTrue(keys, 'the default authorized_keys path was not exercised: %r' % opened)
        for name in keys:
            self.assertTrue(name.startswith(str(SCRATCH_HOME)), name)
        # The runner's own default path is exactly what must never be opened.
        self.assertNotIn(real_key, keys)


class ReaderTests(unittest.TestCase):
    """One setting, two readers: the wrapper's own and admin.py's, pinned together."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(os.path.realpath(self.tmp.name), 'alpha')

    def test_the_setting_is_read_from_the_runtimes_own_settings_file(self):
        self.assertFalse(forced.bound_keys_only(self.root))                 # no file at all
        self.assertFalse(admin.bound_keys_only(self.root))
        write_settings(self.root, None)
        self.assertFalse(forced.bound_keys_only(self.root))                 # the key absent
        write_settings(self.root, False)
        self.assertFalse(forced.bound_keys_only(self.root))
        write_settings(self.root, True)
        self.assertTrue(forced.bound_keys_only(self.root))
        self.assertTrue(admin.bound_keys_only(self.root))

    def test_the_two_readers_agree_on_every_file(self):
        for value in (None, False, True):
            with self.subTest(value=value):
                write_settings(self.root, value)
                self.assertEqual(forced.bound_keys_only(self.root), admin.bound_keys_only(self.root))

    def test_a_value_that_is_not_true_or_false_reads_as_off_with_a_warning(self):
        for value in ('true', 'yes', 1, 0, [], {}):
            with self.subTest(value=value):
                write_settings(self.root, value)
                said = io.StringIO()
                with mock.patch.object(sys, 'stderr', said):
                    self.assertFalse(forced.bound_keys_only(self.root))
                self.assertIn('not true or false', said.getvalue())
                self.assertIn('NOT refusing', said.getvalue())
                # The connecting contributor is told neither the settings path nor the bad value
                # (kittrial-5bb.196 review, items 1 and 13).
                self.assertNotIn('deployment.private.json', said.getvalue())
                self.assertNotIn(repr(value), said.getvalue())
                warnings = []
                self.assertFalse(admin.bound_keys_only(self.root, warnings=warnings))
                self.assertEqual(len(warnings), 1)
                self.assertIn('reading it as off', warnings[0])
                # The operator's own reader keeps the key name and the value it found.
                self.assertIn('bound_keys_only', warnings[0])
                self.assertIn(repr(value), warnings[0])

    def test_a_settings_file_that_cannot_be_read_refuses_without_naming_the_path(self):
        for text, said in (('{not json', 'not valid JSON'), ('[1, 2]', 'not a JSON object'),
                           ('\udcff\udcfe', 'not readable text')):
            with self.subTest(text=text):
                (self.root/'deployment.private.json').write_bytes(text.encode('utf-8', 'surrogateescape'))
                with self.assertRaises(ValueError) as refused:
                    forced.bound_keys_only(self.root)
                self.assertNotIn('deployment.private.json', str(refused.exception))
                self.assertIn(said, str(refused.exception))

    def test_a_settings_name_that_is_not_a_regular_file_is_refused(self):
        """N09e: the not-a-regular-file refusal must not print the settings path again."""
        (self.root/'deployment.private.json').mkdir()
        with self.assertRaises(ValueError) as refused:
            forced.bound_keys_only(self.root)
        self.assertIn('not a regular file', str(refused.exception))
        self.assertNotIn('deployment.private.json', str(refused.exception))
        # The host reader says UNKNOWN (not OFF) for the same shape, so setup-status cannot say
        # "the setting is off" while the wrapper refuses every key (item 2c).
        with self.assertRaises(admin.ConfigurationUnreadable) as refused:
            admin.bound_keys_only(self.root)
        self.assertIn('not a regular file', str(refused.exception))

    def test_an_open_error_refuses_without_naming_the_path(self):
        """N09c: the open-error message must keep the strerror, never the OSError with its path."""
        write_settings(self.root, True)
        real_open = builtins.open

        def denied(file, *args, **kwargs):
            if str(file).replace('\\', '/').endswith('deployment.private.json'):
                raise PermissionError(13, 'Permission denied', str(file))
            return real_open(file, *args, **kwargs)

        with mock.patch.object(builtins, 'open', denied):
            with self.assertRaises(ValueError) as refused:
                forced.bound_keys_only(self.root)
        self.assertIn('cannot read the installation setting', str(refused.exception))
        self.assertIn('Permission denied', str(refused.exception))
        self.assertNotIn('deployment.private.json', str(refused.exception))

    def test_a_stat_error_that_is_not_missing_refuses_both_readers(self):
        """M05e: a stat error other than "missing" must not read OFF (the fail-open the design forbids)."""
        real_stat = os.stat

        def denied(path, *args, **kwargs):
            if str(path).endswith('deployment.private.json'):
                raise PermissionError(13, 'Permission denied', str(path))
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(forced.os, 'stat', side_effect=denied):
            with self.assertRaises(ValueError) as refused:
                forced.bound_keys_only(self.root)
            self.assertNotIn('deployment.private.json', str(refused.exception))
            # The host reader asks the same question and must not read OFF either.
            with self.assertRaises(admin.ConfigurationUnreadable):
                admin.bound_keys_only(self.root)

    @unittest.skipUnless(os.name == 'posix', 'symlinks and ENOTDIR are POSIX behaviour')
    def test_a_symlink_loop_or_a_regular_file_root_refuses_both_readers(self):
        settings = self.root/'deployment.private.json'
        os.symlink('deployment.private.json', settings)                 # a symlink to itself
        with self.assertRaises(ValueError) as refused:
            forced.bound_keys_only(self.root)
        self.assertNotIn('deployment.private.json', str(refused.exception))
        with self.assertRaises(admin.ConfigurationUnreadable):
            admin.bound_keys_only(self.root)
        settings.unlink()
        # --root naming a regular file: <file>/deployment.private.json stats as ENOTDIR.
        regular = self.root.parent/'not-a-directory'
        regular.write_text('x', encoding='utf-8')
        with self.assertRaises(ValueError):
            forced.bound_keys_only(regular)
        with self.assertRaises(admin.ConfigurationUnreadable):
            admin.bound_keys_only(regular)

    def test_a_deeply_nested_file_refuses_both_readers(self):
        """M05f: a deeply nested file must not read OFF; it says unknown with a reason."""
        (self.root/'deployment.private.json').write_text('['*200000, encoding='utf-8')
        with self.assertRaises(ValueError) as refused:
            forced.bound_keys_only(self.root)
        self.assertIn('nests too deeply', str(refused.exception))
        with self.assertRaises(admin.ConfigurationUnreadable) as refused:
            admin.bound_keys_only(self.root)
        self.assertIn('nests too deeply', str(refused.exception))

    def test_a_settings_file_larger_than_the_bound_is_refused_not_read(self):
        """Item 4a: the read is bounded; a larger file is refused, not read whole."""
        self.assertEqual(forced.SETTINGS_MAX_BYTES, admin.BOUND_KEYS_ONLY_MAX_BYTES)
        (self.root/'deployment.private.json').write_text(' '*(forced.SETTINGS_MAX_BYTES + 1),
                                                          encoding='utf-8')
        with self.assertRaises(ValueError) as refused:
            forced.bound_keys_only(self.root)
        self.assertIn('larger than', str(refused.exception))
        with self.assertRaises(admin.ConfigurationUnreadable) as refused:
            admin.bound_keys_only(self.root)
        self.assertIn('larger than', str(refused.exception))

    def test_admin_raises_its_own_class_for_a_file_that_is_not_json(self):
        (self.root/'deployment.private.json').write_text('{not json', encoding='utf-8')
        with self.assertRaises(admin.ConfigurationUnreadable):
            admin.bound_keys_only(self.root)


class WrapperGateTests(unittest.TestCase):
    """With the setting on, the forced command refuses a line bound to nothing.

    A line's ``--root`` is a server path, so these tests hand the gate the answer the reader
    would give for it (the reader itself is ReaderTests; the two together, on a real runtime, are
    WrapperSettingEndToEndTests on POSIX).
    """

    def launched(self, *line, command=CLIENT, setting=False):
        """What ``main`` would exec (None when it refused), its status, stderr, and the read."""
        said = io.StringIO()
        with mock.patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': command}), \
                mock.patch.object(forced, 'bound_keys_only', return_value=setting) as read, \
                mock.patch.object(forced.os, 'execvpe', side_effect=OSError('not run in a test')) as executed, \
                mock.patch.object(sys, 'stderr', said):
            status = forced.main(['--root', ROOT, '--endpoint', CLIENT, '--python', PYTHON, *line])
        return ((executed.call_args.args[1] if executed.called else None), status, said.getvalue(), read)

    def test_the_setting_is_read_from_the_root_the_line_names(self):
        _, _, _, read = self.launched('--project', 'alpha', '--principal', MINE)
        read.assert_called_once_with(ROOT)

    def test_with_the_setting_off_every_line_is_launched_exactly_as_before(self):
        for line, expected in (
                ((), [PYTHON, CLIENT, '--root', ROOT]),
                (('--project', 'alpha'), [PYTHON, CLIENT, '--root', ROOT, '--key-project', 'alpha']),
                (('--principal', MINE), [PYTHON, CLIENT, '--root', ROOT, '--key-principal', MINE]),
                (('--principal', MINE, '--project', 'beta', '--project', 'alpha'),
                 [PYTHON, CLIENT, '--root', ROOT, '--key-project', 'beta', '--key-project', 'alpha',
                  '--key-principal', MINE])):
            with self.subTest(line=line):
                argv, status, said, _ = self.launched(*line)
                self.assertEqual(argv, expected, said)

    def test_an_unbound_line_is_refused_with_the_missing_binding_named(self):
        for line, missing in (((), 'no --project and no --principal'),
                              (('--project', 'alpha'), 'no --principal'),
                              (('--principal', MINE), 'no --project')):
            with self.subTest(line=line):
                argv, status, said, _ = self.launched(*line, setting=True)
                self.assertIsNone(argv)
                self.assertEqual(status, 2)
                self.assertIn('accepts bound keys only', said)
                self.assertIn(missing, said)
                self.assertIn('admin.py authorized-keys', said)

    def test_a_line_that_names_both_is_launched_exactly_as_before(self):
        argv, status, said, _ = self.launched('--project', 'alpha', '--principal', MINE, setting=True)
        self.assertEqual(argv, [PYTHON, CLIENT, '--root', ROOT,
                                '--key-project', 'alpha', '--key-principal', MINE], said)
        argv, status, said, _ = self.launched('--project', 'beta', '--principal', PERSON, '--project', 'alpha',
                                              setting=True)
        self.assertEqual(argv, [PYTHON, CLIENT, '--root', ROOT,
                                '--key-project', 'beta', '--key-project', 'alpha',
                                '--key-principal', PERSON], said)

    def test_the_setting_is_answered_before_the_command_is_looked_at(self):
        argv, status, said, _ = self.launched(
            command='/srv/kit/admin.py --root /srv/state operators add alice', setting=True)
        self.assertIsNone(argv)
        self.assertEqual(status, 2)
        self.assertIn('accepts bound keys only', said)
        self.assertNotIn('may not run', said)
        # `ssh -T HOST` (no command) answers the setting too, not "no endpoint selected".
        argv, status, said, _ = self.launched(command='', setting=True)
        self.assertIsNone(argv)
        self.assertIn('accepts bound keys only', said)
        self.assertNotIn('no endpoint selected', said)


@POSIX_SHELL
class WrapperSettingEndToEndTests(unittest.TestCase):
    """The real reader and the gate together, on the runtime the line itself names."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(os.path.realpath(self.tmp.name), 'alpha')

    def launched(self, *line, command=CLIENT):
        said = io.StringIO()
        with mock.patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': command}), \
                mock.patch.object(forced.os, 'execvpe', side_effect=OSError('not run in a test')) as executed, \
                mock.patch.object(sys, 'stderr', said):
            status = forced.main(['--root', str(self.root), '--endpoint', CLIENT, '--python', PYTHON, *line])
        return (executed.call_args.args[1] if executed.called else None), status, said.getvalue()

    def test_an_installation_that_configures_nothing_is_unchanged(self):
        """No settings file, and a settings file that does not set it: every line as before."""
        for prepared in (None, lambda: write_settings(self.root, None), lambda: write_settings(self.root, False)):
            if prepared is not None:
                prepared()
            for line in ((), ('--project', 'alpha'), ('--principal', MINE),
                         ('--principal', MINE, '--project', 'alpha')):
                with self.subTest(prepared=prepared, line=line):
                    argv, status, said = self.launched(*line)
                    self.assertIsNotNone(argv, said)

    def test_the_setting_on_refuses_an_unbound_line_and_serves_a_bound_one(self):
        write_settings(self.root, True)
        for line in ((), ('--project', 'alpha'), ('--principal', MINE)):
            with self.subTest(line=line):
                argv, status, said = self.launched(*line)
                self.assertIsNone(argv)
                self.assertEqual(status, 2)
                self.assertIn('accepts bound keys only', said)
        argv, status, said = self.launched('--project', 'alpha', '--principal', MINE)
        self.assertEqual(argv, [PYTHON, CLIENT, '--root', str(self.root),
                                '--key-project', 'alpha', '--key-principal', MINE], said)

    def test_a_settings_file_that_cannot_be_read_refuses_every_line(self):
        (self.root/'deployment.private.json').write_text('{not json', encoding='utf-8')
        for line in ((), ('--project', 'alpha', '--principal', MINE)):
            with self.subTest(line=line):
                argv, status, said = self.launched(*line)
                self.assertIsNone(argv)
                self.assertEqual(status, 2)
                # The refusal reaches the connecting contributor without the settings path or value.
                self.assertIn('the installation setting', said)
                self.assertNotIn('deployment.private.json', said)

    def test_a_value_that_is_not_true_or_false_warns_and_the_line_still_runs(self):
        write_settings(self.root, 'on')
        argv, status, said = self.launched()
        self.assertIsNotNone(argv, said)
        self.assertIn('not true or false', said)


class WrapperProcessTests(unittest.TestCase):
    """The wrapper as a real process, with stderr on a PIPE (kittrial-5bb.196 review, item 1).

    These tests run the wrapper the way sshd does - a fresh process, stderr a pipe - and were the
    round-1 answer for the 3.6-office case. They do NOT by themselves hold the flush on this
    interpreter: on Python 3.9+ stderr to a pipe is line-buffered, and this module cannot be
    imported on 3.6 (``admin.py`` needs 3.10), so the process alone cannot show it. The flush
    itself is held by ``test_the_warning_and_a_refusal_flush_stderr``, which patches ``sys.stderr``
    and counts the calls, on the running interpreter (kittrial-5bb.196 review, item 3a).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(os.path.realpath(self.tmp.name), 'alpha')
        self.wrapper = KIT/'ssh_forced_command.py'
        # A stand-in for the endpoint: the wrapper execs the interpreter it is told to use on this
        # script, so nothing of the real endpoint runs and the process still replaces itself.
        self.standin = Path(self.tmp.name)/'endpoint-standin.py'
        self.standin.write_text('import sys\nsys.exit(0)\n', encoding='utf-8')

    def wrapper_path(self, path):
        """A server POSIX path for a local file.

        The wrapper takes the server's POSIX paths (it runs under sshd on Linux). On Windows an
        extended-length path with a leading ``//?/`` names the same file, is accepted by
        ``posixpath.isabs`` and can still be exec'd, so the process tests run here as well.
        """
        as_posix = Path(path).as_posix()
        return as_posix if os.name == 'posix' else '//?/' + as_posix

    def run_wrapper(self, *line, command=None):
        environment = dict(os.environ)
        environment['SSH_ORIGINAL_COMMAND'] = (self.wrapper_path(self.standin) if command is None else command)
        # The wrapper accepts a bare interpreter name or a POSIX absolute path, so on Windows the
        # basename plus the interpreter's own directory on PATH is what it can take.
        python = sys.executable if os.name == 'posix' else os.path.basename(sys.executable)
        if os.name != 'posix':
            environment['PATH'] = os.path.dirname(sys.executable) + os.pathsep + environment.get('PATH', '')
        return subprocess.run([sys.executable, '-E', '-s', str(self.wrapper),
                               '--root', self.wrapper_path(self.root),
                               '--endpoint', self.wrapper_path(self.standin),
                               '--python', python, *line],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=environment, timeout=120)

    @unittest.skipUnless(os.name == 'posix', 'exec over a pipe is exercised on the server here')
    def test_the_warning_reaches_a_pipe_before_the_process_is_replaced(self):
        write_settings(self.root, 'on')
        process = self.run_wrapper()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn(b'not true or false', process.stderr)
        self.assertIn(b'NOT refusing', process.stderr)
        self.assertNotIn(b'deployment.private.json', process.stderr)
        self.assertNotIn(b"'on'", process.stderr)

    def test_the_non_bool_warning_reaches_a_pipe(self):
        write_settings(self.root, 'on')
        process = self.run_wrapper(command='/not-the-endpoint')
        self.assertEqual(process.returncode, 2)
        self.assertIn(b'not true or false', process.stderr)
        self.assertIn(b'NOT refusing', process.stderr)
        self.assertNotIn(b'deployment.private.json', process.stderr)

    def test_a_refusal_reaches_a_pipe(self):
        write_settings(self.root, True)
        process = self.run_wrapper()
        self.assertEqual(process.returncode, 2)
        self.assertIn(b'accepts bound keys only', process.stderr)

    def test_a_damaged_settings_file_names_no_path_to_the_contributor(self):
        (self.root/'deployment.private.json').write_text('{not json', encoding='utf-8')
        process = self.run_wrapper()
        self.assertEqual(process.returncode, 2)
        self.assertIn(b'not valid JSON', process.stderr)
        self.assertNotIn(b'deployment.private.json', process.stderr)


class _CountingStderr(io.StringIO):
    """A stderr that records how many times it was flushed."""

    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        super().flush()


class FlushTests(unittest.TestCase):
    """Item 3a: the two flushes are held by a test, on whatever interpreter runs the suite.

    On Python 3.9+ stderr to a pipe is line-buffered, so the process tests cannot fail when a
    flush is removed; and this module cannot be imported on 3.6 (``admin.py`` needs 3.10), so the
    office interpreter cannot run it either. Patching ``sys.stderr`` with a counting object and
    calling the two functions directly holds the flush itself, here, on every interpreter.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(os.path.realpath(self.tmp.name), 'alpha')

    def test_the_warning_and_a_refusal_flush_stderr(self):
        write_settings(self.root, 'on')
        said = _CountingStderr()
        with mock.patch.object(sys, 'stderr', said):
            self.assertFalse(forced.bound_keys_only(self.root))
        self.assertIn('not true or false', said.getvalue())
        self.assertGreaterEqual(said.flushes, 1, 'the non-bool warning was not flushed')
        # The refusal path flushes too, so a refusal never depends on CPython's exit-time flush
        # and is complete before any later exec.
        said = _CountingStderr()
        with mock.patch.object(sys, 'stderr', said):
            self.assertEqual(forced.refuse('a reason'), 2)
        self.assertIn('a reason', said.getvalue())
        self.assertGreaterEqual(said.flushes, 1, 'the refusal was not flushed')


class SettingsCommandTests(unittest.TestCase):
    """``admin.py bound-keys-only``: operator-gated, audited, and off by default."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(os.path.realpath(self.tmp.name), 'alpha')
        write_settings(self.root, None)
        self.marker = self.root/'deployment.private.json'

    def switch(self, action, actor=OPERATOR):
        return admin.bound_keys_only_switch(self.root, action, actor)

    def document(self):
        return json.loads(self.marker.read_text(encoding='utf-8'))

    def test_status_reads_off_then_on_then_off_again(self):
        status = self.switch('status')
        self.assertEqual((status['bound_keys_only'], status['audit'], status['audit_readable']), (False, None, True))
        flipped = self.switch('on')
        self.assertEqual((flipped['bound_keys_only'], flipped['changed'], flipped['audit_records']), (True, True, 1))
        self.assertIs(self.document()['bound_keys_only'], True)
        status = self.switch('status')
        self.assertEqual(status['bound_keys_only'], True)
        self.assertEqual(status['audit']['action'], 'on')
        self.assertEqual(status['audit']['actor'], OPERATOR)
        self.assertIs(status['audit']['previous'], False)
        self.assertIs(status['audit']['enabled'], True)
        self.assertRegex(status['audit']['at'], r'^\d{4}-\d\d-\d\dT')
        flipped = self.switch('off')
        self.assertEqual((flipped['bound_keys_only'], flipped['changed']), (False, True))
        # OFF is the absent key: an installation that turned it off reads as one that never did.
        self.assertNotIn(admin.BOUND_KEYS_ONLY_KEY, self.document())
        self.assertEqual(self.switch('status')['audit']['action'], 'off')

    def test_the_switch_needs_an_operator_and_then_writes_nothing(self):
        before = self.marker.read_bytes()
        with self.assertRaises(ValueError) as refused:
            self.switch('on', 'mallory')
        self.assertIn('operator allowlist', str(refused.exception))
        self.assertEqual(self.marker.read_bytes(), before)
        # No operator list at all: nobody may flip it.
        write_settings(self.root, None, operators=())
        with self.assertRaises(ValueError):
            self.switch('on')
        with self.assertRaises(ValueError):
            admin.bound_keys_only_switch(self.root, 'bogus', OPERATOR)

    def test_status_is_refused_for_a_non_operator_too(self):
        """M14: ``status`` is operator-gated as well, not only the flips."""
        with self.assertRaises(ValueError) as refused:
            self.switch('status', 'mallory')
        self.assertIn('operator allowlist', str(refused.exception))
        with self.assertRaises(ValueError):
            self.switch('status', '')                   # not an identity at all

    def test_status_reports_whether_the_value_and_the_last_audit_entry_agree(self):
        """Item 2b: the review found the documented comparison did not exist."""
        self.switch('on')
        self.switch('off')
        document = self.document()
        document[admin.BOUND_KEYS_ONLY_KEY] = True      # a hand edit: value on, last entry off
        self.marker.write_text(json.dumps(document), encoding='utf-8')
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            status = self.switch('status')
        self.assertTrue(status['bound_keys_only'])
        self.assertEqual(status['audit']['action'], 'off')
        self.assertFalse(status['audit_agrees'])
        self.assertIn('stale', said.getvalue())
        self.switch('off')                              # a real flip records the agreement again
        self.assertTrue(self.switch('status')['audit_agrees'])

    def test_off_on_a_value_that_is_not_a_bool_writes_false(self):
        """Item 2d: ``off`` on a non-bool value is a real flip, not a no-op that leaves it."""
        write_settings(self.root, 'on')
        with mock.patch.object(sys, 'stderr', io.StringIO()):
            flipped = self.switch('off')
        self.assertTrue(flipped['changed'])
        self.assertNotIn(admin.BOUND_KEYS_ONLY_KEY, self.document())
        self.assertFalse(self.switch('status')['bound_keys_only'])
        # The bad value is gone, so the wrapper no longer warns on every connection.
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            self.assertFalse(forced.bound_keys_only(self.root))
        self.assertEqual(said.getvalue(), '')

    def test_the_audit_list_keeps_the_last_bounded_number_of_flips(self):
        """Item 4b: 1,001 flips grew the file to 101 KB and status printed all of it."""
        for _ in range(admin.BOUND_KEYS_ONLY_AUDIT_MAX + 5):
            self.switch('on')
            self.switch('off')
        audit = self.document()[admin.BOUND_KEYS_ONLY_AUDIT_KEY]
        self.assertEqual(len(audit), admin.BOUND_KEYS_ONLY_AUDIT_MAX)
        status = self.switch('status')
        self.assertEqual((status['audit_records'], status['audit_max']),
                         (admin.BOUND_KEYS_ONLY_AUDIT_MAX, admin.BOUND_KEYS_ONLY_AUDIT_MAX))
        self.assertIs(status['audit']['enabled'], False)
        self.assertEqual(status['audit']['action'], 'off')

    def test_a_flip_that_changes_nothing_writes_nothing_and_keeps_the_history(self):
        self.switch('on')
        after = self.marker.read_bytes()
        again = self.switch('on')
        self.assertEqual((again['changed'], again['audit_records']), (False, 1))
        self.assertEqual(self.marker.read_bytes(), after)
        self.switch('off')
        after = self.marker.read_bytes()
        again = self.switch('off')
        self.assertEqual(again['changed'], False)
        self.assertEqual(self.marker.read_bytes(), after)

    def test_a_damaged_audit_is_kept_aside_and_the_switch_still_flips(self):
        document = self.document()
        document[admin.BOUND_KEYS_ONLY_AUDIT_KEY] = 'nonsense'
        self.marker.write_text(json.dumps(document), encoding='utf-8')
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            flipped = self.switch('on')
        self.assertTrue(flipped['changed'])
        kept = self.document()
        self.assertEqual(len(kept[admin.BOUND_KEYS_ONLY_AUDIT_KEY]), 1)
        aside = [key for key in kept if key.startswith(admin.BOUND_KEYS_ONLY_AUDIT_KEY + '_damaged_')]
        self.assertEqual(len(aside), 1)
        self.assertEqual(kept[aside[0]], 'nonsense')
        self.assertIn('damaged', said.getvalue())

    def test_a_damaged_audit_does_not_read_as_agreeing(self):
        """N08b: a damaged audit is a disagreement, never "no history, everything agrees"."""
        document = self.document()
        document[admin.BOUND_KEYS_ONLY_AUDIT_KEY] = 'nonsense'
        self.marker.write_text(json.dumps(document), encoding='utf-8')
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            status = self.switch('status')
        self.assertFalse(status['audit_readable'])
        self.assertFalse(status['audit_agrees'])
        self.assertEqual(status['audit_note'], 'damaged')
        self.assertIn('damaged', said.getvalue())

    def test_a_hand_set_value_with_no_audit_entry_says_no_entry(self):
        """Item 4: nothing was recorded, so a hand-set on must not read as agreeing."""
        document = self.document()
        document[admin.BOUND_KEYS_ONLY_KEY] = True
        self.marker.write_text(json.dumps(document), encoding='utf-8')
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            status = self.switch('status')
        self.assertTrue(status['bound_keys_only'])
        self.assertIsNone(status['audit_agrees'])
        self.assertEqual(status['audit_note'], 'no entry')
        self.assertIn('no entry', said.getvalue())

    def test_a_hostile_audit_field_is_escaped_and_bounded(self):
        """Item 4: a hand-edited actor/at must not put ESC or 200 kB on the terminal."""
        self.switch('on')
        self.switch('off')
        document = self.document()
        document[admin.BOUND_KEYS_ONLY_KEY] = True           # stale against the last (off) entry
        audit = document[admin.BOUND_KEYS_ONLY_AUDIT_KEY]
        audit[-1]['actor'] = '\x1b[31m' + 'A' * 200000
        audit[-1]['at'] = '2020-01-01\n\x07'
        self.marker.write_text(json.dumps(document), encoding='utf-8')
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            status = self.switch('status')
        self.assertFalse(status['audit_agrees'])
        text = said.getvalue()
        self.assertNotIn('\x1b', text)
        self.assertNotIn('\x07', text)
        self.assertLess(len(text), 2000)
        self.assertIn('\\x1b', text)                         # escaped, not silently dropped

    def test_a_non_bool_value_warns_once_in_status(self):
        """Item 6: `status` reads the setting twice; the warning must be printed once."""
        write_settings(self.root, 'on')
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            self.switch('status')
        self.assertEqual(said.getvalue().count('not true or false'), 1)

    def test_status_on_a_directory_or_fifo_says_unknown_not_not_installed(self):
        """Item 4: a present but non-regular settings path is UNKNOWN, not "not installed"."""
        self.marker.unlink()
        self.marker.mkdir()
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            status = self.switch('status')
        self.assertIsNone(status['bound_keys_only'])
        self.assertEqual(status['audit_note'], 'unknown')
        self.assertIn('not a regular file', said.getvalue())
        self.assertNotIn('not installed', said.getvalue())
        with self.assertRaises(admin.ConfigurationUnreadable):
            self.switch('on')

    def test_a_non_operator_reads_no_authorized_keys_and_no_preview(self):
        """N17: the preview (which opens the keys file and any wrapper it names) is read only
        AFTER the operator check - patching what the switch actually calls, with a counting
        open underneath, so a mutant that reads for a non-operator fails (kittrial-5bb.250
        r2 item 5: the old shape patched a helper nothing calls)."""
        file = Path(self.tmp.name)/'authorized_keys'
        file.write_text('', encoding='utf-8')
        opened = []
        real_open = builtins.open
        def counting_open(target, *args, **kwargs):
            opened.append(str(target))
            return real_open(target, *args, **kwargs)
        with mock.patch.object(admin, 'authorized_keys_preview',
                               side_effect=AssertionError('read before the operator check')) as preview, \
                mock.patch.object(builtins, 'open', counting_open):
            for action in ('status', 'on', 'off'):
                with self.assertRaises(ValueError):
                    admin.bound_keys_only_switch(self.root, action, 'mallory', str(file))
        preview.assert_not_called()
        self.assertEqual([], opened)

    def test_status_reports_the_preview(self):
        """N11c: `status` must name the lines `on` would refuse, not None."""
        file = Path(self.tmp.name)/'authorized_keys'
        file.write_text(key_line_text(self.root) + '\n', encoding='utf-8')
        status = admin.bound_keys_only_switch(self.root, 'status', OPERATOR, str(file))
        self.assertEqual(status['would_be_refused'], [1])

    def test_status_names_the_lines_beyond_the_twenty_it_lists(self):
        """kittrial-5bb.250 F1: with 25 unbound lines status says what `on` says - twenty
        named, would_be_refused_more 5, the warning naming both - not a silent list of 20."""
        file = Path(self.tmp.name)/'authorized_keys'
        file.write_text(''.join(key_line_text(self.root) + '\n' for _ in range(25)), encoding='utf-8')
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            status = admin.bound_keys_only_switch(self.root, 'status', OPERATOR, str(file))
        self.assertEqual(list(range(1, 21)), status['would_be_refused'])
        self.assertEqual(5, status['would_be_refused_more'])
        self.assertFalse(status['truncated'])
        self.assertIn('would refuse 25 authorized_keys line(s)', said.getvalue())
        self.assertIn('and 5 more', said.getvalue())

    def test_status_says_when_only_part_of_the_file_was_inspected(self):
        """kittrial-5bb.250 F1: one unbound line beyond the 1 MiB read bound makes status
        report an empty named list WITH truncated true and the warning, not an empty preview."""
        file = Path(self.tmp.name)/'authorized_keys'
        filler = ('#' * 99 + '\n') * 10485
        file.write_text(filler + key_line_text(self.root) + '\n', encoding='utf-8')
        self.assertGreater(file.stat().st_size, admin.AUTHORIZED_KEYS_MAX_BYTES)
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            status = admin.bound_keys_only_switch(self.root, 'status', OPERATOR, str(file))
        self.assertEqual([], status['would_be_refused'])
        self.assertEqual(0, status['would_be_refused_more'])
        self.assertTrue(status['truncated'])
        self.assertIn('only its first part was inspected', said.getvalue())

    def test_on_refuses_when_only_part_of_the_file_was_inspected(self):
        """kittrial-5bb.250 F2: the truncated preview refuses `on` (a line beyond the bound
        would be refused after on, unnamed); --without-preview flips with the warning."""
        file = Path(self.tmp.name)/'authorized_keys'
        filler = ('#' * 99 + '\n') * 10485
        file.write_text(filler + key_line_text(self.root) + '\n', encoding='utf-8')
        with self.assertRaises(ValueError) as refused:
            admin.bound_keys_only_switch(self.root, 'on', OPERATOR, str(file))
        self.assertIn('only its first part was inspected', str(refused.exception))
        self.assertIn('--without-preview', str(refused.exception))
        self.assertFalse(admin.bound_keys_only(self.root))
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            result = admin.bound_keys_only_switch(self.root, 'on', OPERATOR, str(file),
                                                  without_preview=True)
        self.assertTrue(result['changed'])
        self.assertTrue(result['truncated'])
        self.assertIn('only its first part was inspected', said.getvalue())

    def test_status_and_on_refuse_a_file_that_is_not_regular(self):
        """kittrial-5bb.250 F2: a fifo as --file made status and on wait for ever; a
        directory and a dangling symlink are refused too, before anything is opened - the
        listing raises instead of blocking, status answers the could-not-be-read warning,
        and `on` refuses (nothing flipped)."""
        fifo = self.fifo()
        directory = Path(self.tmp.name)
        dangling = Path(self.tmp.name)/'dangling'
        dangling.symlink_to(Path(self.tmp.name)/'no-such-target')
        for label, path in (('a fifo', fifo), ('a directory', directory), ('a dangling symlink', dangling)):
            with self.subTest(shape=label):
                # The reader itself refuses the shape before opening it: no open, no wait.
                with self.assertRaises(ValueError) as refused:
                    admin.authorized_keys_listing(self.root, str(path))
                self.assertIn('not a regular file', str(refused.exception))
                said = io.StringIO()
                with mock.patch.object(sys, 'stderr', said):
                    status = admin.bound_keys_only_switch(self.root, 'status', OPERATOR, str(path))
                self.assertIsNone(status['would_be_refused'])
                self.assertIn('could not be read', said.getvalue())
                with self.assertRaises(ValueError) as on_refused:
                    admin.bound_keys_only_switch(self.root, 'on', OPERATOR, str(path))
                self.assertIn('Refusing to turn bound-keys-only on', str(on_refused.exception))
                self.assertIn('--without-preview', str(on_refused.exception))
        self.assertFalse(admin.bound_keys_only(self.root))
        self.assertNotIn(admin.BOUND_KEYS_ONLY_KEY, self.document())

    def test_a_foreign_wrapper_that_names_the_setting_is_a_hint_not_a_decision(self):
        """kittrial-5bb.250 r2 item 3, the coordinator's decision: a wrapper outside this kit
        carrying the word is a HINT. Line 92's shape (the word only in a comment) and line
        95's (the word beyond 64 KiB) are served before and after on: never stated refused.
        The line lands in group two - may be refused, wrapper_names_setting saying the hint."""
        naming = Path(self.tmp.name)/'naming-release'
        naming.mkdir()
        shutil.copy(KIT/'ssh_forced_command.py', naming/'ssh_forced_command.py')
        self.assertIn(b'bound_keys_only', (naming/'ssh_forced_command.py').read_bytes()[:65536])
        commented = Path(self.tmp.name)/'comment-only'
        commented.mkdir()
        (commented/'ssh_forced_command.py').write_text(
            '# a comment that says bound_keys_only and nothing else\n#!/bin/sh\necho hi\n', encoding='utf-8')
        delayed = Path(self.tmp.name)/'word-past-the-bound'
        delayed.mkdir()
        (delayed/'ssh_forced_command.py').write_text(
            '#' + 'x' * 70000 + ' bound_keys_only\n', encoding='utf-8')
        file = Path(self.tmp.name)/'authorized_keys'
        file.write_text('\n'.join(key_line_text(self.root, kit=kit) for kit in (naming, commented, delayed)) + '\n',
                        encoding='utf-8')
        listing = admin.authorized_keys_listing(self.root, str(file))
        self.assertEqual([], listing['would_be_refused'])
        self.assertEqual([1, 2, 3], listing['may_be_refused'])
        self.assertEqual({1: True, 2: True, 3: False}, listing['wrapper_names_setting'])
        status = admin.bound_keys_only_switch(self.root, 'status', OPERATOR, str(file))
        self.assertEqual([], status['would_be_refused'])
        self.assertEqual([1, 2, 3], status['may_be_refused'])

    def test_a_wrapper_outside_every_kit_of_this_line_may_be_refused_never_not_reached(self):
        """kittrial-5bb.250 F3: a line whose wrapper cannot be read or names nothing is not
        decided here: the note says the kit that serves it MAY refuse it, and never says the
        setting does not reach the line at all."""
        other = Path(self.tmp.name)/'foreign'
        other.mkdir()
        (other/'ssh_forced_command.py').write_text('#!/bin/sh\necho hello\n', encoding='utf-8')
        file = Path(self.tmp.name)/'authorized_keys'
        file.write_text(key_line_text(self.root, kit=other) + '\n', encoding='utf-8')
        listing = admin.authorized_keys_listing(self.root, str(file))
        self.assertEqual([], listing['would_be_refused'])
        self.assertEqual([1], listing['may_be_refused'])
        notes = ' '.join(listing['notes'])
        self.assertIn('may refuse it', notes)
        self.assertNotIn('not refused by this setting at all', notes)
        self.assertNotIn('are not reached', notes)

    def test_line_numbers_follow_the_line_feed_alone(self):
        """kittrial-5bb.250 F6 (main too): U+2028, U+0085, a form feed or a bare CR inside a
        line must not shift every later number against sshd's, which splits on \n only."""
        first = key_line_text(self.root, bound=' --project alpha --principal ' + MINE,
                              comment='first\u2028line\rwith\u0085odd\u000cchars')
        second = key_line_text(self.root)
        file = Path(self.tmp.name)/'authorized_keys'
        file.write_text(first + '\n' + second + '\n', encoding='utf-8')
        listing = admin.authorized_keys_listing(self.root, str(file))
        # sshd numbers by \n alone: both lines keep their numbers (str.splitlines() would
        # split the first at each odd character and shift the second to 5 or later).
        self.assertEqual([1, 2], [entry['line'] for entry in listing['lines']])
        self.assertEqual([2], listing['would_be_refused'])

    def test_a_wrapper_that_is_a_fifo_is_never_opened(self):
        """kittrial-5bb.250 r2 item 1: a key line can name ANY path ending in
        ssh_forced_command.py; a fifo (or a symlink to one) there must not hang status, on or
        the listing - the hint read opens regular files only, after lstat."""
        fifo_dir = Path(tempfile.mkdtemp(prefix='bound-keys-wrapper-fifo-'))
        self.addCleanup(shutil.rmtree, fifo_dir, ignore_errors=True)
        os.mkfifo(fifo_dir/'ssh_forced_command.py')
        symlinked = Path(self.tmp.name)/'via-symlink'
        symlinked.mkdir(exist_ok=True)
        (symlinked/'ssh_forced_command.py').symlink_to(fifo_dir/'ssh_forced_command.py')
        for label, kit in (('a fifo', fifo_dir), ('a symlink to one', symlinked)):
            file = Path(self.tmp.name)/'authorized_keys'
            file.write_text(key_line_text(self.root, kit=kit) + '\n', encoding='utf-8')
            with self.subTest(shape=label):
                with self.assertWarns(None) if False else self.subTest(action='status'):
                    pass
                # The listing answers, with the line in group two, and never blocks.
                listing = admin.authorized_keys_listing(self.root, str(file))
                self.assertEqual([1], listing['may_be_refused'])
                self.assertIsNone(listing['wrapper_names_setting'].get(1))
                said = io.StringIO()
                with mock.patch.object(sys, 'stderr', said):
                    status = admin.bound_keys_only_switch(self.root, 'status', OPERATOR, str(file))
                self.assertEqual([1], status['may_be_refused'])
                self.assertIn('were not decided', said.getvalue())

    def test_the_wrapper_hint_is_read_once_per_path_and_bounded(self):
        """kittrial-5bb.250 r2 item 2: one read per distinct wrapper path per run (3,000
        lines must not become 2,505 opens), and only the first 64 KiB of it."""
        other = Path(self.tmp.name)/'one-release'
        other.mkdir()
        shutil.copy(KIT/'ssh_forced_command.py', other/'ssh_forced_command.py')
        file = Path(self.tmp.name)/'authorized_keys'
        file.write_text(''.join(key_line_text(self.root, kit=other) + '\n' for _ in range(300)), encoding='utf-8')
        real_open = builtins.open
        opens = []
        def counting(target, *args, **kwargs):
            if str(target).endswith('one-release/ssh_forced_command.py'):
                opens.append(str(target))
            return real_open(target, *args, **kwargs)
        with mock.patch.object(builtins, 'open', counting):
            admin.authorized_keys_listing(self.root, str(file))
        self.assertEqual(1, len(opens))
        # The 64 KiB bound: a wrapper whose word sits beyond it says hint False, not True.
        delayed = Path(self.tmp.name)/'word-past-the-bound-2'
        delayed.mkdir()
        (delayed/'ssh_forced_command.py').write_text('#' + 'y' * 70000 + ' bound_keys_only\n', encoding='utf-8')
        file2 = Path(self.tmp.name)/'authorized_keys-2'
        file2.write_text(key_line_text(self.root, kit=delayed) + '\n', encoding='utf-8')
        listing = admin.authorized_keys_listing(self.root, str(file2))
        self.assertEqual({1: False}, listing['wrapper_names_setting'])

    def test_a_relative_wrapper_path_is_not_decided(self):
        """kittrial-5bb.250 r2 item 2: a relative wrapper path is group two wherever the
        command runs from - never resolved against the caller's directory."""
        file = Path(self.tmp.name)/'authorized_keys'
        file.write_text(key_line_text(self.root, kit=Path('relative-kit')) + '\n', encoding='utf-8')
        listing = admin.authorized_keys_listing(self.root, str(file))
        self.assertEqual([], listing['would_be_refused'])
        self.assertEqual([1], listing['may_be_refused'])
        self.assertIsNone(listing['wrapper_names_setting'].get(1))

    def test_status_and_on_say_the_undecided_lines(self):
        """kittrial-5bb.250 r2 item 4: the shapes of the review's lines 58 (relative), 71
        ($HOME), 72/84 (shell text), 94 (a shim) and 95 (the word beyond 64 KiB) are printed
        by status AND on, in the JSON and on stderr - not cut off unnamed."""
        shim = Path(self.tmp.name)/'shim'
        shim.mkdir()
        (shim/'ssh_forced_command.py').write_text('#!/bin/sh\nexec %s/ssh_forced_command.py "$@"\n'
                                                  % KIT.as_posix(), encoding='utf-8')
        lines = [key_line_text(self.root),                                                # 1: this kit, bound? no: unbound
                 key_line_text(self.root, kit=Path('rel/kit')),                           # 2: relative
                 key_line_text(self.root).replace('/usr/bin/python3', '$HOME/venv/bin/python3'),  # 3: $HOME
                 key_line_text(self.root).replace('endpoint.py"', 'endpoint.py ; true"'),   # 4: shell text
                 key_line_text(self.root, kit=shim),                                      # 5: shim
                 key_line_text(self.root, bound=' --project alpha --principal ' + MINE)]  # 6: bound
        file = Path(self.tmp.name)/'authorized_keys'
        file.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            status = admin.bound_keys_only_switch(self.root, 'status', OPERATOR, str(file))
        self.assertEqual([1], status['would_be_refused'])          # this kit's own wrapper only
        self.assertEqual([2, 3, 4, 5], status['may_be_refused'])
        self.assertIn('were not decided: 2, 3, 4, 5', said.getvalue())
        self.assertIn('the kit that serves them may refuse them', said.getvalue())
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            result = admin.bound_keys_only_switch(self.root, 'on', OPERATOR, str(file))
        self.assertTrue(result['changed'])
        self.assertEqual([2, 3, 4, 5], result['may_be_refused'])
        self.assertIn('were not decided: 2, 3, 4, 5', said.getvalue())
        admin.bound_keys_only_switch(self.root, 'off', OPERATOR)

    def test_the_command_line_carries_the_without_preview_flag(self):
        """kittrial-5bb.250 r2 item 5: main() drives --without-preview, and the small
        things of item 7: --file '' is refused, the flag is refused on status and off, and a
        no-op on with an unreadable keys file does not say 'Refusing to turn on'."""
        missing = Path(self.tmp.name)/'no-keys-here'
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'bound-keys-only', 'on',
                                             '--actor', OPERATOR, '--file', str(missing),
                                             '--without-preview']), \
                redirect_stdout(out), redirect_stderr(err):
            admin.main()
        self.assertTrue(json.loads(out.getvalue())['changed'])
        self.assertIn('could not be read', err.getvalue())
        admin.bound_keys_only_switch(self.root, 'off', OPERATOR)
        # --file '' is refused, not silently the default path.
        with self.assertRaises(ValueError) as refused:
            admin.bound_keys_only_switch(self.root, 'on', OPERATOR, '')
        self.assertIn('--file is empty', str(refused.exception))
        # The flag belongs to `on` only.
        for action in ('status', 'off'):
            with self.subTest(action=action):
                with self.assertRaises(ValueError) as refused:
                    admin.bound_keys_only_switch(self.root, action, OPERATOR, str(missing),
                                                 without_preview=True)
                self.assertIn('--without-preview belongs to bound-keys-only on', str(refused.exception))
        # A no-op on with the setting already on and an unreadable keys file: no refusal text.
        admin.bound_keys_only_switch(self.root, 'on', OPERATOR, without_preview=True)
        noop = admin.bound_keys_only_switch(self.root, 'on', OPERATOR, str(missing), without_preview=True)
        self.assertFalse(noop['changed'])
        admin.bound_keys_only_switch(self.root, 'off', OPERATOR)

    def test_status_bounds_the_audit_entries_it_prints(self):
        """kittrial-5bb.250 F7: the audit list is hand-editable; the entries status prints
        on stdout are escaped and bounded as the stderr warnings are, not whole."""
        admin.bound_keys_only_switch(self.root, 'on', OPERATOR, without_preview=True)
        document = self.document()
        document[admin.BOUND_KEYS_ONLY_AUDIT_KEY][0]['actor'] = '\x1b[1M' + 'x' * 200000
        self.marker.write_text(json.dumps(document), encoding='utf-8')
        status = admin.bound_keys_only_switch(self.root, 'status', OPERATOR)
        shown = json.dumps(status)
        self.assertLess(len(shown), 10000)
        self.assertLess(len(status['audit']['actor']), admin.AUDIT_FIELD_MAX + 10)
        self.assertTrue(status['audit']['actor'].endswith('...'))
        self.assertTrue(status['audit']['actor'].startswith('\\x1b'))  # the ESCAPE TEXT, not the raw control
        self.assertEqual(1, status['audit_records'])

    def test_a_dangling_symlink_at_the_settings_says_what_is_wrong(self):
        """kittrial-5bb.250 F10: a dangling symlink answered 'requires an actor on the
        deployment operator allowlist' to a real operator; it says the file is a dangling
        symlink now, and a flip refuses."""
        self.marker.unlink()
        self.marker.symlink_to(Path(self.tmp.name)/'no-such-deployment')
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            status = admin.bound_keys_only_switch(self.root, 'status', OPERATOR)
        self.assertIsNone(status['bound_keys_only'])
        self.assertEqual('unknown', status['audit_note'])
        self.assertIn('dangling symlink', status['unknown'])
        self.assertIn('dangling symlink', said.getvalue())
        self.assertNotIn('operator allowlist', said.getvalue())
        with self.assertRaises(admin.ConfigurationUnreadable):
            admin.bound_keys_only_switch(self.root, 'on', OPERATOR)

    def test_the_preview_is_read_before_the_flip_is_written(self):
        """Item 1: read and build the list first, then flip, then print."""
        events = []
        real_preview = admin.authorized_keys_preview
        real_write = admin.atomic_private_write

        def preview(*args, **kwargs):
            events.append('preview')
            return real_preview(*args, **kwargs)

        def write(*args, **kwargs):
            events.append('write')
            return real_write(*args, **kwargs)

        file = Path(self.tmp.name)/'authorized_keys'
        file.write_text(key_line_text(self.root) + '\n', encoding='utf-8')
        with mock.patch.object(admin, 'authorized_keys_preview', preview), \
                mock.patch.object(admin, 'atomic_private_write', write), \
                mock.patch.object(sys, 'stderr', io.StringIO()):
            admin.bound_keys_only_switch(self.root, 'on', OPERATOR, str(file))
        self.assertEqual(events[:2], ['preview', 'write'])

    def test_a_keys_file_that_cannot_be_read_refuses_on_without_flipping(self):
        """Item 1, and kittrial-5bb.250 F2 with the coordinator's decision: the REAL cases of an
        unreadable keys file - missing, a directory, a fifo, no permission - make `on` refuse
        (nothing changed) unless the operator says so deliberately with --without-preview. The
        old shape of this test mocked an OSError the real reader never raises: the listing
        converts OSError to ValueError and the preview swallows both to None."""
        missing = Path(self.tmp.name)/'no-such-authorized_keys'
        for label, path in (('missing', missing),
                            ('a directory', Path(self.tmp.name)),
                            ('a fifo', self.fifo())):
            with self.subTest(shape=label):
                with self.assertRaises(ValueError) as refused:
                    admin.bound_keys_only_switch(self.root, 'on', OPERATOR, str(path))
                self.assertIn('Refusing to turn bound-keys-only on', str(refused.exception))
                self.assertIn('--without-preview', str(refused.exception))
                self.assertFalse(admin.bound_keys_only(self.root))
                self.assertNotIn(admin.BOUND_KEYS_ONLY_KEY, self.document())
        # Saying so deliberately flips, with the unreadable preview said, not silent.
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            result = admin.bound_keys_only_switch(self.root, 'on', OPERATOR, str(missing),
                                                  without_preview=True)
        self.assertTrue(result['changed'])
        self.assertIsNone(result['would_be_refused'])
        self.assertIn('could not be read', said.getvalue())
        self.assertTrue(admin.bound_keys_only(self.root))

    FIFO_FOLDERS = []
    @classmethod
    def fifo(cls):
        folder = Path(tempfile.mkdtemp(prefix='bound-keys-fifo-'))
        cls.FIFO_FOLDERS.append(folder)          # removed at class teardown: nothing left in TMPDIR
        path = folder/'authorized_keys'
        os.mkfifo(path)
        return path
    @classmethod
    def tearDownClass(cls):
        for folder in cls.FIFO_FOLDERS:
            shutil.rmtree(folder, ignore_errors=True)

    def test_the_status_command_prints_what_the_reader_says(self):
        said = io.StringIO()
        with mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root),
                                             'bound-keys-only', 'on', '--actor', OPERATOR]), \
                mock.patch.object(admin, 'root_path', Path), redirect_stdout(said):
            admin.main()
        written = json.loads(said.getvalue())
        self.assertIs(written['bound_keys_only'], True)
        self.assertTrue(written['changed'])
        self.assertTrue(admin.bound_keys_only(self.root))

    def test_the_switch_holds_the_deployment_lock(self):
        # Every writer of deployment.private.json takes the same lock (kittrial-5bb.136).
        with mock.patch.object(admin, 'review_writes_lock', side_effect=AssertionError('no lock')):
            with self.assertRaises(AssertionError):
                admin.bound_keys_only_switch(self.root, 'on', OPERATOR)


class ListingTests(unittest.TestCase):
    """``authorized-keys-list`` names the lines the setting refuses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(os.path.realpath(self.tmp.name))
        self.root = runtime(self.base, 'alpha', 'beta')
        # The kit that runs the listing is the kit a line must point at to be this kit's line:
        # `other_kit` is what the listing compares against itself.
        self.kit = Path(admin.__file__).resolve().parent

    def line(self, projects=(), principal=None, body=KEY_BODY, comment='alex@laptop', root=None, kit=None,
             roots=None, endpoints=None, extra=''):
        root = self.root if root is None else root
        kit = self.kit if kit is None else kit
        roots = [root] if roots is None else list(roots)
        endpoints = [kit/'endpoint.py'] if endpoints is None else list(endpoints)
        bound = ''.join(' --project ' + name for name in projects)
        if principal is not None:
            bound += ' --principal ' + principal
        marks = []
        if projects:
            marks.append('orchestra-projects=' + ','.join(projects))
        if principal is not None:
            marks.append('orchestra-principal=' + principal)
        root_part = ''.join(' --root ' + Path(item).as_posix() for item in roots)
        endpoint_part = ''.join(' --endpoint ' + Path(item).as_posix() for item in endpoints)
        return ('command="/usr/bin/python3 -E -s %s/ssh_forced_command.py%s%s%s%s",'
                'restrict,no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 %s %s'
                % (kit.as_posix(), root_part, endpoint_part, extra, bound, body,
                   ' '.join([comment] + marks)))

    def listing(self, text):
        file = self.base/'authorized_keys'
        file.write_text(text, encoding='utf-8')
        return admin.authorized_keys_listing(self.root, str(file))

    def lines(self):
        return [self.line(projects=['alpha'], principal=MINE),          # 1 bound
                self.line(),                                            # 2 confined
                self.line(projects=['alpha']),                           # 3 projects only
                self.line(principal=MINE),                               # 4 principal only
                'ssh-ed25519 %s james@desk' % KEY_BODY,                  # 5 unrestricted
                'command="/usr/bin/rsync --server" ssh-ed25519 %s backup' % KEY_BODY]  # 6 other-command

    def test_with_the_setting_off_nothing_is_flagged_and_the_report_says_off(self):
        listing = self.listing('\n'.join(self.lines()) + '\n')
        self.assertIs(listing['bound_keys_only'], False)
        self.assertEqual(listing['attention'], [])
        self.assertEqual(listing['summary'], {'bound': 3, 'confined': 1, 'unrestricted': 1, 'other-command': 1,
                                              'principal-bound': 2})
        self.assertNotIn('bound_keys_only:', ' '.join(listing['notes']))
        # The preview: nothing is refused today, but the listing says what `on` would cut off
        # (kittrial-5bb.196 review, item 4c).
        self.assertEqual(listing['would_be_refused'], [2, 3, 4])
        note = next(note for note in listing['notes'] if note.startswith('would_be_refused:'))
        self.assertIn('lines 2, 3, 4', note)

    def test_with_the_setting_on_the_lines_it_refuses_are_under_attention(self):
        write_settings(self.root, True)
        listing = self.listing('\n'.join(self.lines()) + '\n')
        self.assertIs(listing['bound_keys_only'], True)
        # The confined line and the two bound to only one of the two; a line that does not run
        # this wrapper (unrestricted, another command) is not refused by the setting.
        self.assertEqual(listing['attention'], [2, 3, 4])
        self.assertEqual(listing['would_be_refused'], [2, 3, 4])
        note = next(note for note in listing['notes'] if note.startswith('bound_keys_only:'))
        self.assertIn('lines 2, 3, 4', note)
        self.assertIn('another kit or of another root is not decided here', note)
        self.assertIn('may refuse it', note)

    def test_lines_of_another_kit_or_root_are_not_counted_as_refused(self):
        """Item 2a: those lines are SERVED by the release or the root they name."""
        write_settings(self.root, True)
        other_root = self.base/'other-root'
        other_root.mkdir()
        text = '\n'.join([self.line(),                                    # 1 this kit, unbound: refused
                          self.line(kit=Path('/srv/other-kit')),          # 2 other kit: not reached
                          self.line(root=other_root)]) + '\n'             # 3 other root: not reached
        listing = self.listing(text)
        self.assertEqual(listing['would_be_refused'], [1])
        # Line 2 is already under attention as other_kit; an other-root line of this kit is not
        # itself under attention here, and no other-kit or other-root line is named as refused.
        self.assertEqual(listing['attention'], [1, 2])
        note = next(note for note in listing['notes'] if note.startswith('bound_keys_only:'))
        self.assertIn('lines 2, 3 may be refused', note)

    def test_a_settings_file_that_cannot_be_read_does_not_fail_the_listing(self):
        (self.root/'deployment.private.json').write_text('{not json', encoding='utf-8')
        listing = self.listing('\n'.join(self.lines()) + '\n')
        self.assertIsNone(listing['bound_keys_only'])
        self.assertEqual(listing['attention'], [])
        # The preview still names the lines, since it does not depend on reading the setting.
        self.assertEqual(listing['would_be_refused'], [2, 3, 4])

    def test_turning_the_setting_on_prints_the_lines_it_would_refuse(self):
        """Item 4c: `on` must print the list, so nothing is cut off unseen."""
        write_settings(self.root, None)
        file = self.base/'authorized_keys'
        file.write_text('\n'.join(self.lines()) + '\n', encoding='utf-8')
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            flipped = admin.bound_keys_only_switch(self.root, 'on', OPERATOR, str(file))
        self.assertTrue(flipped['changed'])
        self.assertEqual(flipped['would_be_refused'], [2, 3, 4])
        self.assertIn('2, 3, 4', said.getvalue())

    def test_a_foreign_endpoint_does_not_move_the_line_out_of_this_kit(self):
        """Item 2, the harness's lines 36 and 42: the WRAPPER, not an endpoint, decides."""
        write_settings(self.root, True)
        other = Path('/srv/other-kit')
        text = '\n'.join([
            self.line(endpoints=[other/'endpoint.py']),                            # 1 other kit's endpoint
            self.line(endpoints=[other/'endpoint.py', self.kit/'endpoint.py']),    # 2 both endpoints
        ]) + '\n'
        listing = self.listing(text)
        self.assertEqual(listing['would_be_refused'], [1, 2])
        # `other_kit` still flags the foreign endpoint for the operator; it is not the SETTING's answer.
        self.assertTrue(listing['lines'][0]['other_kit'])
        self.assertEqual(listing['lines'][0]['endpoints'], [other.as_posix() + '/endpoint.py'])
        note = next(note for note in listing['notes'] if note.startswith('bound_keys_only:'))
        self.assertNotIn('are not reached', note)

    def test_the_last_root_decides_like_the_wrappers_argument_parser(self):
        """Item 2, the harness's lines 37 and 38: argparse takes the LAST --root."""
        write_settings(self.root, True)
        other_root = self.base/'other-root'
        other_root.mkdir()
        text = '\n'.join([
            self.line(roots=[other_root, self.root]),   # 1 other first, this last: reached
            self.line(roots=[self.root, other_root]),   # 2 this first, other last: not reached
        ]) + '\n'
        listing = self.listing(text)
        self.assertEqual(listing['would_be_refused'], [1])
        self.assertEqual(listing['lines'][0]['root'], self.root.as_posix())     # the LAST one
        self.assertFalse(listing['lines'][0]['other_root'])
        self.assertEqual(listing['lines'][1]['root'], other_root.as_posix())
        self.assertTrue(listing['lines'][1]['other_root'])
        note = next(note for note in listing['notes'] if note.startswith('bound_keys_only:'))
        self.assertIn('line 2', note)
        self.assertIn('may be refused', note)

    def test_lines_refused_for_another_reason_are_named_but_not_as_the_settings_doing(self):
        """Item 2, the harness's lines 18, 20, 40, 41, 44: say refused, not "the setting refuses"."""
        write_settings(self.root, True)
        text = '\n'.join([
            self.line(extra=' --proj alpha --princ lane:a'),             # 1 unknown abbreviations
            self.line(extra=' --project alpha --principal='),            # 2 empty principal
            self.line(extra=' --principal lane:a --principal lane:b'),   # 3 principal named twice
            self.line(extra=' --project alpha --principal --x'),         # 4 flag-looking principal
            self.line(extra=' --project alpha --project alpha'),         # 5 project named twice
        ]) + '\n'
        listing = self.listing(text)
        self.assertEqual(listing['would_be_refused'], [1, 2, 3, 4, 5])
        note = ' '.join(listing['notes'])
        self.assertIn('lines 1, 2, 3, 4, 5 are refused whether the setting is on or off', note)
        self.assertIn('the setting is not why they are refused', note)

    def test_the_preview_names_at_most_the_stated_number_of_lines(self):
        """Item 5: name at most N lines and say how many more."""
        write_settings(self.root, None)
        total = admin.WOULD_BE_REFUSED_NAMED_MAX + 5
        listing = self.listing('\n'.join(self.line() for _ in range(total)) + '\n')
        self.assertEqual(len(listing['would_be_refused']), admin.WOULD_BE_REFUSED_NAMED_MAX)
        self.assertEqual(listing['would_be_refused_total'], total)
        self.assertEqual(listing['would_be_refused_more'], 5)
        self.assertIn('and 5 more', ' '.join(listing['notes']))
        self.assertLess(len(json.dumps(listing['would_be_refused'])), 400)

    def test_on_says_how_many_more_lines_it_would_refuse(self):
        write_settings(self.root, None)
        file = self.base/'authorized_keys'
        total = admin.WOULD_BE_REFUSED_NAMED_MAX + 3
        file.write_text('\n'.join(self.line() for _ in range(total)) + '\n', encoding='utf-8')
        said = io.StringIO()
        with mock.patch.object(sys, 'stderr', said):
            flipped = admin.bound_keys_only_switch(self.root, 'on', OPERATOR, str(file))
        self.assertEqual(len(flipped['would_be_refused']), admin.WOULD_BE_REFUSED_NAMED_MAX)
        self.assertEqual(flipped['would_be_refused_more'], 3)
        self.assertIn('and 3 more', said.getvalue())

    def test_the_keys_file_read_is_bounded_and_says_so(self):
        """Item 5: read at most the stated size; say the tail was not inspected."""
        write_settings(self.root, None)
        one = self.line() + '\n'
        big = one * (admin.AUTHORIZED_KEYS_MAX_BYTES // len(one) + 2)
        listing = self.listing(big)
        self.assertTrue(listing['truncated'])
        self.assertLessEqual(len(listing['would_be_refused']), admin.WOULD_BE_REFUSED_NAMED_MAX)
        self.assertIn('Only the first %d bytes' % admin.AUTHORIZED_KEYS_MAX_BYTES, ' '.join(listing['notes']))


@POSIX_SHELL
class PrintingTests(unittest.TestCase):
    """``authorized-keys`` refuses to print a line the setting refuses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(os.path.realpath(self.tmp.name), 'alpha', 'beta')
        self.key = Path(self.tmp.name)/'alex.pub'
        self.key.write_text('ssh-ed25519 %s alex@laptop\n' % KEY_BODY, encoding='utf-8')

    def printed(self, *more):
        said = io.StringIO()
        with mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'authorized-keys',
                                             '--key-file', str(self.key), '--python', PYTHON, *more]), \
                mock.patch.object(admin, 'root_path', Path), redirect_stdout(said):
            admin.main()
        return json.loads(said.getvalue())

    def test_with_the_setting_off_the_helper_is_unchanged(self):
        plain = self.printed()
        self.assertIn('contributor', plain)
        self.assertIn('operator', plain)
        self.assertNotIn('--project', plain['contributor'])

    def test_an_unbound_contributor_line_is_refused_and_names_what_is_missing(self):
        write_settings(self.root, True)
        for more, missing in (((), 'no --project and no --principal'),
                              (('--project', 'alpha'), 'no --principal'),
                              (('--principal', MINE), 'no --project')):
            with self.subTest(more=more):
                with self.assertRaises(ValueError) as refused:
                    self.printed(*more)
                self.assertIn('accepts bound keys only', str(refused.exception))
                self.assertIn(missing, str(refused.exception))
                self.assertIn('--role operator', str(refused.exception))

    def test_a_bound_line_and_the_operator_line_are_still_printed(self):
        write_settings(self.root, True)
        bound = self.printed('--project', 'alpha', '--principal', MINE)
        self.assertIn(' --project alpha --principal %s",restrict,' % MINE, bound['contributor'])
        self.assertNotIn('operator', bound)
        operator = self.printed('--role', 'operator')
        self.assertNotIn('contributor', operator)
        self.assertEqual(operator['operator'], 'ssh-ed25519 %s alex@laptop' % KEY_BODY)

    def test_a_settings_file_that_cannot_be_read_refuses_to_print(self):
        (self.root/'deployment.private.json').write_text('{not json', encoding='utf-8')
        with self.assertRaises(admin.ConfigurationUnreadable):
            self.printed()


@POSIX
class SetupStatusTests(unittest.TestCase):
    """The endpoint's read-only ``setup-status`` action reports the setting."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = runtime(os.path.realpath(self.tmp.name), 'alpha')
        write_settings(self.root, None)

    def status(self):
        answer = endpoint.execute(self.root, {'project': 'alpha', 'actor': 'alice',
                                              'action': 'setup-status', 'args': []})
        self.assertEqual(answer['returncode'], 0, answer)
        return json.loads(answer['stdout'])

    def test_it_reports_the_setting_off_and_on(self):
        body = self.status()
        self.assertIs(body['bound_keys_only']['enabled'], False)
        self.assertIn('the setting is off', body['bound_keys_only']['detail'])
        admin.bound_keys_only_switch(self.root, 'on', OPERATOR)
        body = self.status()
        self.assertIs(body['bound_keys_only']['enabled'], True)
        self.assertIn('accepts bound keys only', body['bound_keys_only']['detail'])
        self.assertIn('authorized-keys-list', body['bound_keys_only']['detail'])
        # The setting is read from the runtime the action serves, before any project part.
        self.assertEqual(body['project'], 'alpha')

    def test_a_settings_file_that_cannot_be_read_says_unknown_and_fails_nothing_else(self):
        (self.root/'deployment.private.json').write_text('{not json', encoding='utf-8')
        body = self.status()
        self.assertIsNone(body['bound_keys_only']['enabled'])
        self.assertIn('could not be read', body['bound_keys_only']['detail'])
        self.assertEqual((body['project'], body['guidance']['state']), ('alpha', 'not-set'))
        self.assertIn('admin', body)                    # the rest of the report is still there

    def test_the_setting_is_only_read_and_changes_nothing(self):
        before = {str(path): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self.status()
        self.assertEqual({str(path): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, before)

    def test_a_deeply_nested_settings_file_does_not_break_setup_status(self):
        """Item 3b: the report answers null for the parts it cannot read, never RecursionError."""
        (self.root/'deployment.private.json').write_text('[' * 200000, encoding='utf-8')
        body = self.status()
        self.assertIsNone(body['bound_keys_only']['enabled'])
        self.assertIn('could not be read', body['bound_keys_only']['detail'])
        # project_creation.server_usage reads the same file; the RecursionError must not escape
        # (the sentence in docs/OPERATIONS.md says setup-status answers null with its reason).
        self.assertIsNone(body['project_databases'])
        self.assertIn('admin', body)


class DocumentationTests(unittest.TestCase):
    """What the task asks to be said plainly is in the documents."""

    def normalized(self, name):
        # The sentences are wrapped, so a phrase is looked for across the line breaks.
        return ' '.join((KIT/'docs'/name).read_text(encoding='utf-8').split())

    def test_operations_has_the_section_and_says_what_the_setting_cannot_do(self):
        text = self.normalized('OPERATIONS.md')
        for phrase in ('### Accept bound keys only',
                       'bound-keys-only on --actor OPERATOR',
                       'Clean `authorized_keys` by hand',
                       'which know nothing of this setting',
                       'admin.py authorized-keys-list',
                       'bound_keys_only',
                       'setup-status',
                       'silently stops refusing unbound',
                       'A damaged or unreadable `deployment.private.json` refuses a confined key',
                       # The revision-2 additions: the bounds, the preview, the audit cap, and the
                       # two exceptions to "an installation that configures nothing" (item 4).
                       'reads at most the first 1 MiB',
                       'would_be_refused',
                       'keeping the last 20 flips',
                       'A line whose `--root` names a regular file is refused',
                       'refuses every forced-command key even where the setting was never on',
                       # The revision-3 additions of items 2, 3 and 4.
                       '`audit_note` `no entry`',
                       'after reading and building it and before it',
                       'names at most 20 line numbers',
                       'would_be_refused_more',
                       'its LAST `--root`',
                       'does not move the line out of this kit',
                       'the setting is not why',
                       'VALID `deployment.private.json` larger than 1 MiB'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_the_design_note_records_that_slice_4_is_built(self):
        text = self.normalized('COORDINATORS_PER_PROJECT_DESIGN.md')
        self.assertIn('slice 4, the installation setting that accepts bound keys only (kittrial-5bb.196', text)
        self.assertIn('"Accept bound keys only" in', text)


if __name__ == '__main__':
    unittest.main()
