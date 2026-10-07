"""The operator's ``add-project`` keeps the web route's creation record (kittrial-5bb.176).

Item 1 (P2) of the task: a CLI ``add-project`` that fails after ``bd init`` (or whose
``bd init`` is killed part way) used to leave a project folder and database with no
creation record, so ``finish-project`` and ``remove-creation`` both refused it and a
second ``add-project`` only said "Project already exists". These tests pin the record the
operator's route now keeps, the two commands that act on it, and the re-run sentence.

Item 2: ``http_service.py --bootstrap-user`` prints one sentence instead of a traceback on
a host without ``fcntl`` and when a superuser already exists. Item 5's two uncaught
mutants: the git check must read the runtime's own ``bin``, and the state store must not be
opened before the runtime lock is checked.

The creation lock and the bootstrap lock need ``fcntl``; those classes are skipped where
it is absent. Everything else runs on both hosts.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
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


def patch_creation(module, run_bd):
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
        with patched(*patch_creation(admin, run_bd)):
            admin.add_project(self.root, name)

    def add_expecting(self, name='alpha', run_bd=bd_that_fails_after_init):
        with patched(*patch_creation(admin, run_bd)), contextlib.redirect_stdout(io.StringIO()):
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
        with patched(*patch_creation(admin, bd_that_works)):
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
        with patched(*patch_creation(admin, bd_that_works)):
            with self.assertRaisesRegex(ValueError, 'Project already exists'):
                admin.add_project(self.root, 'alpha')
        self.assertIsNone(pc.read_record(self.root, 'alpha'))
        self.assertEqual((self.root / 'projects' / 'alpha' / 'keep.txt').read_text(encoding='utf-8'), 'mine')


class RecordReuseTests(CreationRecordCase):
    """A record a web request stalled in ``started`` is finished, not overwritten."""

    def test_the_account_that_began_it_stays_on_the_finished_record(self):
        pc.write_record(self.root, 'alpha', {'project': 'alpha', 'by': ALICE, 'operation_id': 'op-alpha',
                                             'state': 'started', 'stage': None, 'started_at': 'then'})
        self.add()
        record = pc.read_record(self.root, 'alpha')
        self.assertEqual((record['state'], record['by'], record['operation_id']), ('created', ALICE, 'op-alpha'))
        self.assertTrue(record['completed_at'] >= record['started_at'])


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


class BootstrapSentenceTests(unittest.TestCase):
    """Item 2: ``--bootstrap-user`` answers in sentences, never a traceback."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'runtime'
        self.root.mkdir()
        self.state = Path(self.tmp.name) / 'http-state.json'

    def bootstrap(self, user='admin'):
        import http_service
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch('getpass.getpass', return_value='disposable-password'), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = http_service.main(['--state', str(self.state), '--root', str(self.root),
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
        self.assertNotIn('Traceback', stderr)


if __name__ == '__main__':
    unittest.main()
