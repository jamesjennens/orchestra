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
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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
        (self.root/'deployment.private.json').mkdir()
        with self.assertRaises(ValueError) as refused:
            forced.bound_keys_only(self.root)
        self.assertIn('not a regular file', str(refused.exception))
        # The host reader says UNKNOWN (not OFF) for the same shape, so setup-status cannot say
        # "the setting is off" while the wrapper refuses every key (item 2c).
        with self.assertRaises(admin.ConfigurationUnreadable) as refused:
            admin.bound_keys_only(self.root)
        self.assertIn('not a regular file', str(refused.exception))

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

    On Python 3.6 to 3.8 stderr to a pipe is block-buffered, so a warning written just before
    ``os.execvpe`` replaces the process never arrives unless it is flushed. These tests run the
    wrapper the way sshd does - a fresh process, stderr a pipe - so they hold that flush here and
    on the office interpreter.
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

    def line(self, projects=(), principal=None, body=KEY_BODY, comment='alex@laptop', root=None, kit=None):
        root = self.root if root is None else root
        kit = self.kit if kit is None else kit
        bound = ''.join(' --project ' + name for name in projects)
        if principal is not None:
            bound += ' --principal ' + principal
        marks = []
        if projects:
            marks.append('orchestra-projects=' + ','.join(projects))
        if principal is not None:
            marks.append('orchestra-principal=' + principal)
        return ('command="/usr/bin/python3 -E -s %s/ssh_forced_command.py --root %s --endpoint %s/endpoint.py%s",'
                'restrict,no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 %s %s'
                % (kit.as_posix(), root.as_posix(), kit.as_posix(), bound, body,
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
        self.assertIn('another kit or of another root is not refused by this setting at all', note)

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
        self.assertIn('lines 2, 3 are not reached', note)

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
                       # The four revision-2 additions: the bounds, the preview, the audit cap, and
                       # the two exceptions to "an installation that configures nothing" (item 4).
                       'reads at most the first 1 MiB',
                       'would_be_refused',
                       'keeping the last 20 flips',
                       'A line whose `--root` names a regular file is refused',
                       'refuses every forced-command key even where the setting was never on'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_the_design_note_records_that_slice_4_is_built(self):
        text = self.normalized('COORDINATORS_PER_PROJECT_DESIGN.md')
        self.assertIn('slice 4, the installation setting that accepts bound keys only (kittrial-5bb.196', text)
        self.assertIn('"Accept bound keys only" in', text)


if __name__ == '__main__':
    unittest.main()
