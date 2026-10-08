"""The operator's ``add-project`` keeps the web route's creation record (kittrial-5bb.176).

Item 1 (P2) of the task: a CLI ``add-project`` that fails after ``bd init`` (or whose
``bd init`` is killed part way) used to leave a project folder and database with no
creation record, so ``finish-project`` and ``remove-creation`` both refused it and a
second ``add-project`` only said "Project already exists". These tests pin the record the
operator's route now keeps, the two commands that act on it, and the re-run sentence.

Revision 2 adds what the review of that delivery asked for: the operator's route holds the
web route's creation lock and writes the same running marker, so a creation in flight reads
as RUNNING and ``remove-creation`` refuses it (two real processes, ``TwoProcessTests``), a
failing run never overwrites a record it did not write, the stage and the error are on the
record as the work goes, a finished project is not called unfinished, an interrupt after
something was made is ``incomplete``, and only a ``started`` record is reused.

Item 2: ``http_service.py --bootstrap-user`` prints one sentence instead of a traceback on
a host without ``fcntl`` and when a superuser already exists, and the advice matches the
case (a first bootstrap with a short password says to run the command again). Item 3: a
creation on a host without git says so in the answer, not only in
``project-creations/last-failure.txt``. Item 4: the unmanaged-binary refusal says the
prepared-looking root must be discarded. Item 5's two uncaught mutants: the git check must
read the runtime's own ``bin``, and the state store must not be opened before the runtime
lock is checked.

The creation lock and the bootstrap lock need ``fcntl``; those classes are skipped where
it is absent. Everything else runs on both hosts.
"""
import contextlib
import io
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import admin
import project_creation as pc

POSIX = os.name == 'posix'
ALICE = 'usr_' + 'a' * 16


class Stop(BaseException):
    """A kill or an interrupt: not an Exception, as a signal or Ctrl-C is not."""


def patch_creation(run_bd):
    """The stand-ins every ``add_project`` test needs, so only the work is real."""
    return [
        mock.patch.object(admin, 'config', return_value={'port': 13317}),
        mock.patch.object(admin, 'run_bd', side_effect=run_bd),
        mock.patch.object(admin, 'provision_merge_slot'),
        mock.patch.object(admin, 'backup_project'),
        mock.patch.object(admin, 'require_bd_init_tools'),
    ]


@contextlib.contextmanager
def patched(*patches):
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield


def bd_that_fails_after_init(runtime, name, args):
    """``bd init`` leaves a database and metadata; the next step fails."""
    if args[0] == 'init':
        path = admin.project_dir(runtime, name)
        (path / '.beads').mkdir(parents=True, exist_ok=True)
        (path / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        return ''
    raise subprocess.CalledProcessError(1, args)


def bd_that_fails_at_init(runtime, name, args):
    raise subprocess.CalledProcessError(1, args)


def bd_that_works(runtime, name, args):
    if args[0] == 'init':
        path = admin.project_dir(runtime, name)
        path.mkdir(exist_ok=True)
        (path / '.beads').mkdir(exist_ok=True)
        (path / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
    return ''


class CreationRecordCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'runtime'
        (self.root / 'projects').mkdir(parents=True)

    def add(self, name='alpha', run_bd=bd_that_works):
        # ``add-project`` prints its advice; none of it may reach the test run's output.
        with patched(*patch_creation(run_bd)), contextlib.redirect_stdout(io.StringIO()):
            admin.add_project(self.root, name)

    def add_expecting(self, name='alpha', run_bd=bd_that_fails_after_init):
        with patched(*patch_creation(run_bd)), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(BaseException) as caught:
                admin.add_project(self.root, name)
        return caught.exception


class FailureAfterInitTests(CreationRecordCase):
    """A CLI creation that stopped after ``bd init`` leaves a record the commands act on."""

    def test_it_leaves_an_incomplete_record_for_the_operator(self):
        self.add_expecting()
        record = pc.read_record(self.root, 'alpha')
        self.assertEqual((record['state'], record['by'], record['stage']), ('incomplete', pc.HOST, 'configure'))
        self.assertEqual(pc.made(self.root, 'alpha'), 'initialized')
        self.assertEqual([(item['project'], item['state']) for item in pc.attention(self.root)],
                         [('alpha', 'incomplete')])
        # The web service will not register it until an operator finishes it.
        self.assertIn('has not finished', pc.registrable(self.root, 'alpha'))
        self.assertIn('finish-project alpha', pc.registrable(self.root, 'alpha'))
        # The backup run's own hint names it too.
        self.assertIn('admin.py finish-project alpha', admin.unfinished_creation_hint(self.root, ['alpha']))

    def test_a_rerun_names_the_two_commands_instead_of_dead_ending(self):
        self.add_expecting()
        second = self.add_expecting()
        self.assertIsInstance(second, ValueError)
        text = str(second)
        self.assertIn('Project already exists', text)
        self.assertIn('admin.py finish-project alpha', text)
        self.assertIn('admin.py remove-creation alpha --actor OPERATOR --reason REASON', text)

    @unittest.skipUnless(POSIX, 'the creation lock needs fcntl')
    def test_finish_project_completes_it(self):
        self.add_expecting()
        with patched(*patch_creation(bd_that_works)):
            result = pc.finish(self.root, 'alpha')
        self.assertEqual((result['state'], result['effective']), ('created', 'created'))
        self.assertEqual(pc.read_record(self.root, 'alpha')['finished_by'], 'operator')
        self.assertIsNone(pc.registrable(self.root, 'alpha'))
        self.assertEqual(pc.attention(self.root), [])

    @unittest.skipUnless(POSIX, 'the creation lock needs fcntl')
    def test_remove_creation_retires_it(self):
        (self.root / 'deployment.private.json').write_text(json.dumps({'operators': ['ops']}), encoding='utf-8')
        self.add_expecting()
        retired = []

        def retire(root, name, actor, reason, force=False, creation_locked=False):
            retired.append((name, actor, reason, force, creation_locked))
            os.rename(root / 'projects' / name, root / ('gone-' + name))

        result = pc.remove(self.root, 'alpha', 'ops', 'it stopped', retire=retire)
        self.assertEqual(result, {'project': 'alpha', 'removed': 'directory', 'name': 'retired', 'by': pc.HOST})
        self.assertEqual(retired, [('alpha', 'ops', 'it stopped', True, True)])
        self.assertEqual(pc.attention(self.root), [])


class NothingMadeTests(CreationRecordCase):
    """A creation that made nothing leaves no record, so a re-run simply works."""

    def test_a_failure_at_init_deletes_the_record_it_wrote(self):
        self.add_expecting(run_bd=bd_that_fails_at_init)
        self.assertIsNone(pc.read_record(self.root, 'alpha'))
        self.assertEqual(pc.attention(self.root), [])
        # The empty directory main leaves is tolerated by the next run, which succeeds.
        self.add()
        self.assertEqual(pc.read_record(self.root, 'alpha')['state'], 'created')

    def test_a_refusal_before_anything_is_made_leaves_no_record(self):
        (self.root / 'projects' / 'alpha' / 'keep.txt').parent.mkdir(parents=True)
        (self.root / 'projects' / 'alpha' / 'keep.txt').write_text('mine', encoding='utf-8')
        with patched(*patch_creation(bd_that_works)):
            with self.assertRaisesRegex(ValueError, 'Project already exists'):
                admin.add_project(self.root, 'alpha')
        self.assertIsNone(pc.read_record(self.root, 'alpha'))
        self.assertEqual((self.root / 'projects' / 'alpha' / 'keep.txt').read_text(encoding='utf-8'), 'mine')


class RecordReuseTests(CreationRecordCase):
    """A record a web request stalled in ``started`` is finished, not overwritten."""

    def test_the_account_that_began_it_stays_on_the_finished_record(self):
        pc.write_record(self.root, 'alpha', {'project': 'alpha', 'by': ALICE, 'operation_id': 'op-alpha',
                                             'state': 'started', 'stage': None,
                                             'started_at': '2000-01-01T00:00:00Z'})
        self.add()
        record = pc.read_record(self.root, 'alpha')
        self.assertEqual((record['state'], record['by'], record['operation_id']), ('created', ALICE, 'op-alpha'))
        self.assertTrue(record['completed_at'] >= record['started_at'])

    def test_a_record_that_is_there_is_not_reused_whatever_its_state(self):
        """Reviewer mutant H3: only a ``started`` record is another run's to finish.

        Any other record on disk is not this run's, whatever it says: the operator's run
        writes its own record rather than adopting a ``created``/``incomplete``/``removed``
        one (kittrial-5bb.176 item 4).
        """
        for state in ('created', 'incomplete', 'removed'):
            with self.subTest(state=state):
                name = 'st' + state[:4]
                pc.write_record(self.root, name, {'project': name, 'by': ALICE, 'operation_id': 'op-other',
                                                  'state': state, 'stage': 'configure',
                                                  'started_at': '2000-01-01T00:00:00Z'})
                self.add(name)
                record = pc.read_record(self.root, name)
                self.assertEqual((record['state'], record['by']), ('created', pc.HOST))
                self.assertNotIn('operation_id', record)
                self.assertNotEqual(record['started_at'], '2000-01-01T00:00:00Z')


class WorkRecordedAsItGoesTests(CreationRecordCase):
    """The stage and the error are on the record while the work runs (mutants H11, H12)."""

    def host_create(self, initialize, name='alpha'):
        with self.assertRaises(BaseException) as caught:
            pc.host_create(self.root, name, initialize, lambda root, name: None)
        return caught.exception

    def test_the_stage_is_written_as_the_work_goes(self):
        seen = []

        def initialize(root, name, stage):
            for label in ('init', 'configure'):
                stage(label)
                # What is on disk right now, seen from inside the work (reviewer mutant H11).
                seen.append(pc.read_record(root, name)['stage'])
            raise RuntimeError('the server went away')

        with contextlib.redirect_stdout(io.StringIO()):
            self.host_create(initialize)
        self.assertEqual(seen, ['init', 'configure'])

    def test_the_incomplete_record_keeps_the_error(self):
        def initialize(root, name, stage):
            path = admin.project_dir(root, name)
            (path / '.beads').mkdir(parents=True, exist_ok=True)
            (path / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
            stage('configure')
            raise RuntimeError('bd refused the settings')

        with contextlib.redirect_stdout(io.StringIO()):
            self.host_create(initialize)
        record = pc.read_record(self.root, 'alpha')
        self.assertEqual(record['state'], 'incomplete')
        self.assertEqual(record.get('error'), 'RuntimeError: bd refused the settings')   # mutant H12

    def test_an_interrupt_after_something_was_made_leaves_it_incomplete(self):
        def initialize(root, name, stage):
            path = admin.project_dir(root, name)
            (path / '.beads').mkdir(parents=True, exist_ok=True)
            (path / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
            stage('init')
            raise Stop('Ctrl-C in the first backup')

        with contextlib.redirect_stdout(io.StringIO()):
            error = self.host_create(initialize)
        self.assertIsInstance(error, Stop)
        record = pc.read_record(self.root, 'alpha')
        # An interrupt is not an Exception, and must still be recorded (reviewer mutant H10).
        self.assertEqual((record['state'], record.get('error')),
                         ('incomplete', 'Stop: Ctrl-C in the first backup'))


class RerunSentenceTests(CreationRecordCase):
    """What a re-run says about a record that is finished, stalled or running (A2, A3, item 4)."""

    def test_a_finished_project_is_not_called_unfinished_on_a_rerun(self):
        self.add()
        second = self.add_expecting()
        text = str(second)
        self.assertIn('Project already exists', text)
        # No hint at all for a finished project (reviewer mutant A2).
        self.assertNotIn('did not finish', text)
        self.assertNotIn('finish-project', text)
        self.assertNotIn('remove-creation', text)

    @unittest.skipUnless(POSIX, 'the creation lock needs fcntl')
    def test_a_stalled_host_creation_says_to_run_add_project_again(self):
        pc.write_record(self.root, 'alpha', {'project': 'alpha', 'by': pc.HOST, 'state': 'started',
                                             'stage': None, 'started_at': '2000-01-01T00:00:00Z'})
        (self.root / 'projects' / 'alpha').mkdir()
        item = pc.attention(self.root)[0]
        self.assertEqual((item['project'], item['state']), ('alpha', 'stalled'))
        self.assertIn('admin.py add-project alpha', item['command'])
        self.assertNotIn('the same web request', item['command'])
        with self.assertRaises(ValueError) as caught:
            pc.finish(self.root, 'alpha')
        self.assertIn('admin.py add-project alpha', str(caught.exception))
        self.assertNotIn('the same web request', str(caught.exception))


#: Two real processes of one runtime: the child is a real ``add-project`` held inside its
#: work (its ``bd`` blocks until the parent writes the release file), so the parent can see
#: what the host says and does about a creation in flight.
TWO_PROCESS_DRIVER = '''
import sys, time
from pathlib import Path
kit, root, name, ready, release, outcome = sys.argv[1:7]
sys.path.insert(0, kit)
import admin
root, ready, release = Path(root), Path(ready), Path(release)

def run_bd(runtime, project, args):
    if args[0] == 'init':
        path = admin.project_dir(runtime, project)
        path.mkdir(exist_ok=True)
        (path / '.beads').mkdir(exist_ok=True)
        (path / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
    ready.write_text('inside the work', encoding='utf-8')
    while not release.exists():
        time.sleep(0.02)
    if outcome == 'fail':
        raise RuntimeError('the first run failed late')
    return ''

admin.config = lambda root: {'port': 13317}
admin.run_bd = run_bd
admin.provision_merge_slot = lambda root, name: None
admin.backup_project = lambda root, name: None
admin.require_bd_init_tools = lambda root: None
try:
    admin.add_project(root, name)
except BaseException as error:
    sys.stderr.write('%s: %s' % (type(error).__name__, error))
    sys.exit(3)
sys.exit(0)
'''


@unittest.skipUnless(POSIX, 'two real processes need the POSIX creation lock')
class TwoProcessTests(unittest.TestCase):
    """One creation at a time on the host, across two real processes (items 1 and 2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'runtime'
        (self.root / 'projects').mkdir(parents=True)
        (self.root / 'deployment.private.json').write_text(json.dumps({'operators': ['ops']}), encoding='utf-8')
        self.driver = self.base / 'driver.py'
        self.driver.write_text(TWO_PROCESS_DRIVER, encoding='utf-8')

    def start(self, name, outcome='ok'):
        ready, release = self.base / (name + '.ready'), self.base / (name + '.release')
        process = subprocess.Popen([sys.executable, '-X', 'utf8', str(self.driver), str(ROOT), str(self.root),
                                    name, str(ready), str(release), outcome],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, process)
        deadline = time.monotonic() + 120
        while not ready.exists():
            if process.poll() is not None:
                raise AssertionError('the driver exited before its work: %s' % (process.communicate(),))
            if time.monotonic() > deadline:
                raise AssertionError('the driver never reached its work')
            time.sleep(0.02)
        return process, release

    def stop(self, process):
        if process.poll() is None:
            process.kill()
        with contextlib.suppress(Exception):
            process.communicate(timeout=120)              # already reaped by the test: fine

    def test_a_second_add_project_of_a_running_name_is_told_running(self):
        process, release = self.start('alpha')
        with patched(*patch_creation(bd_that_works)), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError) as caught:
                admin.add_project(self.root, 'alpha')
        text = str(caught.exception)
        self.assertIn('Project already exists', text)
        # The "is running" sentence, reached for an operator's creation (mutants A3 and N3).
        self.assertIn('A project creation for alpha is running on this server right now', text)
        self.assertNotIn('did not finish', text)
        # The real second run changed nothing.
        self.assertEqual(pc.read_record(self.root, 'alpha')['state'], 'started')
        self.assertTrue((self.root / 'projects' / 'alpha' / '.beads' / 'metadata.json').is_file())
        release.write_text('go', encoding='utf-8')
        _out, err = process.communicate(timeout=120)
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(pc.read_record(self.root, 'alpha')['state'], 'created')
        self.assertIsNone(pc.running_name(self.root))
        self.assertEqual(pc.attention(self.root), [])

    def test_remove_creation_refuses_a_running_host_creation_and_names_it_running(self):
        process, release = self.start('beta')
        with self.assertRaises(ValueError) as caught:
            pc.remove(self.root, 'beta', 'ops', 'review')
        self.assertIn('a creation is running on this server (beta)', str(caught.exception))
        self.assertTrue((self.root / 'projects' / 'beta').is_dir())
        self.assertEqual(pc.read_record(self.root, 'beta')['state'], 'started')
        self.assertFalse((self.root / 'retired').exists())
        release.write_text('go', encoding='utf-8')
        process.communicate(timeout=120)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(pc.read_record(self.root, 'beta')['state'], 'created')

    def test_a_failing_run_does_not_overwrite_a_record_it_did_not_write(self):
        """A record written under a run in flight is not the failing run's to change.

        The creation lock now stops a second ``add-project`` from running at all (the test
        above); this pins the second half of item 2 directly: while the real first run is
        inside its work, the record is replaced as a second, finished run would have written
        it, and the first run's late failure leaves that record alone.
        """
        process, release = self.start('gamma', outcome='fail')
        pc.write_record(self.root, 'gamma', {'project': 'gamma', 'by': pc.HOST, 'state': 'created', 'stage': None,
                                             'started_at': '2000-01-01T00:00:00Z',
                                             'completed_at': '2000-01-01T00:00:05Z'})
        release.write_text('go', encoding='utf-8')
        _out, err = process.communicate(timeout=120)
        self.assertEqual(process.returncode, 3)
        self.assertIn('the first run failed late', err)
        record = pc.read_record(self.root, 'gamma')
        self.assertEqual((record['state'], record.get('completed_at')), ('created', '2000-01-01T00:00:05Z'))
        self.assertNotIn('error', record)


class GitCheckReadsTheRuntimeBinTests(unittest.TestCase):
    """Item 5: the check reads the runtime's ``bin``, not only the caller's PATH."""

    def test_a_git_in_the_runtime_bin_is_found_with_an_empty_caller_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binaries = root / 'bin'
            binaries.mkdir()
            for name in ('git', 'git.exe'):
                shim = binaries / name
                shim.write_text('', encoding='utf-8')
                shim.chmod(0o755)
            with mock.patch.dict(os.environ, {'PATH': ''}):
                self.assertEqual(admin.missing_bd_init_tools(root), [])
                admin.require_bd_init_tools(root)          # does not raise
            # And with no runtime bin at all the caller's empty PATH is what refuses.
            empty = root / 'empty'
            empty.mkdir()
            with mock.patch.dict(os.environ, {'PATH': ''}):
                self.assertEqual(admin.missing_bd_init_tools(empty), ['git'])


class MissingToolsSentenceTests(unittest.TestCase):
    """Item 3: a host without git says so in the answer, not only in last-failure.txt."""

    def test_the_sentence_is_one_the_service_passes_on(self):
        for missing in (['git'], ['git', 'make']):
            sentence = pc.missing_tools_message(missing)
            self.assertIn(missing[0], sentence)
            self.assertEqual(pc.creation_sentence('ValueError: ' + sentence), sentence)
            self.assertEqual(pc.creation_sentence(sentence), sentence)

    def test_a_path_is_still_not_a_creation_sentence(self):
        self.assertIsNone(pc.creation_sentence(
            'ValueError: The server has no /etc/passwd, which bd init needs to create a project; nothing was made. '
            'Ask an operator of the server to install it and try again.'))

    @unittest.skipUnless(POSIX, 'the creation lock needs fcntl')
    def test_a_creation_without_git_answers_with_the_program_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'runtime'
            (root / 'projects').mkdir(parents=True)
            with mock.patch.dict(os.environ, {'PATH': ''}):
                with self.assertRaises(pc.NothingMade) as caught:
                    pc.create(root, 'alpha', ALICE, 'op-alpha')
            self.assertEqual(str(caught.exception), pc.missing_tools_message(['git']))
            self.assertFalse((root / 'projects' / 'alpha').exists())
            self.assertIsNone(pc.read_record(root, 'alpha'))


@unittest.skipUnless(sys.platform.startswith('linux') and platform.machine() in ('x86_64', 'amd64'),
                     'the binary installer supports Linux x86-64 only')
class UnmanagedBinarySentenceTests(unittest.TestCase):
    """Item 4: the refusal says the prepared-looking root must be discarded."""

    def test_an_unmanaged_binary_says_discard_the_root(self):
        import bootstrap
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'runtime'
            (root / 'bin').mkdir(parents=True)
            (root / 'bin' / 'bd').write_bytes(b'not the pinned bd')
            with self.assertRaises(SystemExit) as caught:
                bootstrap.install(root)
        self.assertIn('Refusing to replace unmanaged binary', str(caught.exception))
        self.assertIn('discard this deployment root', str(caught.exception))


class BootstrapSentenceTests(unittest.TestCase):
    """Item 2: ``--bootstrap-user`` answers in sentences, never a traceback."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'runtime'
        self.root.mkdir()
        self.state = Path(self.tmp.name) / 'http-state.json'

    def bootstrap(self, user='admin', password='disposable-password', state=None):
        import http_service
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch('getpass.getpass', return_value=password), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = http_service.main(['--state', str(state or self.state), '--root', str(self.root),
                                      '--bootstrap-user', user])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_a_host_without_fcntl_refuses_in_one_sentence(self):
        import http_service
        refusal = ValueError('Bootstrapping needs the runtime lock, which requires a POSIX host')
        with mock.patch.object(http_service, 'runtime_service_lock', side_effect=refusal):
            code, _stdout, stderr = self.bootstrap()
        self.assertEqual(code, 1)
        self.assertIn('Refusing to bootstrap admin', stderr)
        self.assertIn('requires a POSIX host', stderr)
        self.assertNotIn('Traceback', stderr)

    def test_the_state_store_is_not_opened_when_the_service_holds_the_runtime(self):
        import http_service
        with mock.patch.object(http_service, 'runtime_service_lock', return_value=None), \
                mock.patch.object(http_service, 'Store') as store:
            code, _stdout, stderr = self.bootstrap()
        self.assertEqual(code, 1)
        self.assertIn('Refusing to bootstrap admin', stderr)
        store.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertEqual([path.name for path in Path(self.tmp.name).iterdir()], ['runtime'])

    @unittest.skipUnless(POSIX, 'the runtime lock needs fcntl')
    def test_a_second_bootstrap_refuses_in_one_sentence(self):
        first_code, _stdout, first_stderr = self.bootstrap()
        self.assertEqual(first_code, 0, first_stderr)
        code, _stdout, stderr = self.bootstrap('second-root')
        self.assertEqual(code, 1)
        self.assertIn('Refusing to bootstrap second-root', stderr)
        self.assertIn('already exists', stderr)
        # Only this refusal can say to add the account inside the service: a superuser exists.
        self.assertIn('Add this person as an account inside the service instead.', stderr)
        self.assertNotIn('Traceback', stderr)

    @unittest.skipUnless(POSIX, 'the runtime lock needs fcntl')
    def test_a_short_password_at_the_first_bootstrap_says_to_run_it_again(self):
        """Reviewer mutant N2: a short or empty password is one sentence with the right advice."""
        for number, password in enumerate(('x', 'seven--', '')):
            with self.subTest(password=password):
                state = Path(self.tmp.name) / ('short-state-%d.json' % number)
                code, _stdout, stderr = self.bootstrap(password=password, state=state)
                self.assertEqual(code, 1, stderr)
                self.assertIn('Refusing to bootstrap admin', stderr)
                self.assertIn('Password must be 8-1024 characters', stderr)
                self.assertIn('Run the command again with a password of 8 to 1024 characters.', stderr)
                # No superuser exists yet, so nobody can add this person inside the service.
                self.assertNotIn('Add this person as an account inside the service', stderr)
                self.assertNotIn('Traceback', stderr)

    @unittest.skipUnless(POSIX, 'the runtime lock needs fcntl')
    def test_a_refused_user_name_at_the_first_bootstrap_says_to_run_it_again(self):
        code, _stdout, stderr = self.bootstrap(user='x')
        self.assertEqual(code, 1)
        self.assertIn('Refusing to bootstrap x', stderr)
        self.assertIn('Username must be 2-64 characters', stderr)
        self.assertIn('Run the command again with a name of 2 to 64 characters', stderr)
        self.assertNotIn('Add this person as an account inside the service', stderr)
        self.assertNotIn('Traceback', stderr)


if __name__ == '__main__':
    unittest.main()
