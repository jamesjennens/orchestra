"""Install-rehearsal guards for kittrial-5bb.162.

Three defects found while rehearsing ``docs/OFFICE_SERVICE.md`` on RHEL 8-level
Linux: ``prepare`` hid a bd that could not start, ``add-project`` without git
left a half-made project, and ``--bootstrap-user`` against a running service
reported an account the service then overwrote. These tests exercise each guard
with shims and a held lock.

The supervisor and the runtime lock need ``fcntl`` and real shebang scripts, so
those classes are skipped on a non-POSIX host; the git check, the no-clean-up
behaviour and the ``--root`` refusal run everywhere.
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

POSIX = os.name == 'posix'


def shim(body):
    return '#!%s\n%s' % (sys.executable, body)


def write_shim(path, body):
    path.write_text(shim(body), encoding='utf-8')
    path.chmod(0o755)


class PrepareFailureTests(unittest.TestCase):
    """A failed step in ``prepare`` must fail prepare, with the reason."""

    def _install(self, bodies):
        def install(root, asset_dir=None):
            binaries = root/'bin'
            binaries.mkdir(parents=True, exist_ok=True)
            for name, body in bodies.items():
                write_shim(binaries/name, body)
        return install

    def _run_prepare(self, root):
        import office_service
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            code = office_service.main(['prepare', '--root', str(root), '--db-port', '13399',
                                        '--port', '13398'])
        return code, stderr.getvalue()

    @unittest.skipUnless(sys.platform.startswith('linux'), 'executable shims are POSIX only')
    def test_prepare_fails_with_the_loader_reason_when_bd_cannot_start(self):
        import office_service  # noqa: F401  (imported to fail on a non-Linux host the same way)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'runtime'
            install = self._install({
                'bd': 'import sys\n'
                      'sys.stderr.write("bd: /lib64/libc.so.6: version `GLIBC_2.34\' not found'
                      ' (required by bd)\\n")\n'
                      'sys.exit(127)\n',
                'dolt': 'import sys\nsys.exit(0)\n'})
            with mock.patch('office_service.admin.install_binaries', side_effect=install):
                code, stderr = self._run_prepare(root)
            self.assertEqual(code, 1)
            self.assertIn('office-service:', stderr)
            self.assertIn('cannot start', stderr)
            self.assertIn('GLIBC_2.34', stderr)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'executable shims are POSIX only')
    def test_prepare_of_an_existing_runtime_still_checks_bd(self):
        """The reported hole: an upgrade runtime was reported prepared without running bd."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'runtime'
            root.mkdir()
            (root/'deployment.private.json').write_text(json.dumps({
                'schema': 1, 'port': 13399, 'unit': 'office-foreground',
                'password': 'disposable-only'}), encoding='utf-8')
            (root/'server.json').write_text('{}', encoding='utf-8')
            install = self._install({
                'bd': 'import sys\nsys.stderr.write("bd cannot start here\\n")\nsys.exit(127)\n'})
            with mock.patch('office_service.admin.install_binaries', side_effect=install):
                code, stderr = self._run_prepare(root)
            self.assertEqual(code, 1)
            self.assertIn('cannot start', stderr)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'executable shims are POSIX only')
    def test_prepare_reports_a_pin_mismatch_as_a_failure_not_a_systemexit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'runtime'
            root.mkdir()
            (root/'deployment.private.json').write_text(json.dumps({
                'schema': 1, 'port': 13399, 'unit': 'office-foreground',
                'password': 'disposable-only'}), encoding='utf-8')
            (root/'server.json').write_text('{}', encoding='utf-8')
            with mock.patch('office_service.admin.install_binaries',
                            side_effect=SystemExit('Binary/pin mismatch; use a new deployment root for upgrade')):
                code, stderr = self._run_prepare(root)
            self.assertEqual(code, 1)
            self.assertIn('office-service:', stderr)
            self.assertIn('Binary/pin mismatch', stderr)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'executable shims are POSIX only')
    def test_prepare_succeeds_with_a_working_bd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'runtime'
            install = self._install({'bd': 'import sys\nsys.exit(0)\n',
                                     'dolt': 'import sys\nsys.exit(0)\n'})
            with mock.patch('office_service.admin.install_binaries', side_effect=install):
                code, stderr = self._run_prepare(root)
            self.assertEqual(code, 0, stderr)
            self.assertTrue((root/'deployment.private.json').is_file())


class AddProjectGitTests(unittest.TestCase):
    """git is checked before anything is made, and a failure removes nothing.

    Revision 2 of kittrial-5bb.162 dropped the clean-up an earlier revision added
    to ``admin.initialize_project``: it deleted ``projects/NAME`` whenever
    ``.beads/metadata.json`` was not there yet, which deleted a concurrent
    creation's directory and hid a half-made database. The tests below pin the
    restored behaviour: a failed call leaves whatever is in the directory.
    """

    def test_initialize_project_refuses_without_git_before_creating_anything(self):
        import admin
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'projects').mkdir()
            with mock.patch.dict(os.environ, {'PATH': ''}):
                with self.assertRaisesRegex(ValueError, 'has no git on PATH'):
                    admin.initialize_project(root, 'alpha')
            self.assertFalse((root/'projects'/'alpha').exists())

    def test_a_failed_initialization_leaves_the_directory_main_leaves(self):
        """No clean-up: the failure leaves the empty directory initialize_project made."""
        import admin
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'projects').mkdir()
            with mock.patch.object(admin, 'config', return_value={'port': 13317}), \
                    mock.patch.object(admin, 'run_bd',
                                      side_effect=subprocess.CalledProcessError(1, 'bd init')), \
                    mock.patch.object(admin, 'provision_merge_slot'), \
                    mock.patch.object(admin, 'backup_project'):
                with self.assertRaises(subprocess.CalledProcessError):
                    admin.initialize_project(root, 'alpha')
            self.assertTrue((root/'projects'/'alpha').is_dir())

    def test_a_failure_never_removes_work_another_call_already_wrote(self):
        """The race behind item cleanup-deletes-concurrent-creation, at the mechanism level.

        A concurrent ``add-project NAME`` that is further along has already written
        into ``projects/NAME`` when this call fails. Nothing this call does may delete
        that; with the rmtree gone, whatever is there stays.
        """
        import admin
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'projects').mkdir()

            def run_bd(runtime, name, args):
                path = admin.project_dir(runtime, name)
                (path/'.beads').mkdir(parents=True, exist_ok=True)
                (path/'.beads'/'other-call-was-here').write_text('x', encoding='utf-8')
                raise subprocess.CalledProcessError(1, 'bd init')

            with mock.patch.object(admin, 'config', return_value={'port': 13317}), \
                    mock.patch.object(admin, 'run_bd', side_effect=run_bd), \
                    mock.patch.object(admin, 'provision_merge_slot'), \
                    mock.patch.object(admin, 'backup_project'):
                with self.assertRaises(subprocess.CalledProcessError):
                    admin.initialize_project(root, 'alpha')
            self.assertTrue((root/'projects'/'alpha'/'.beads'/'other-call-was-here').is_file())

    def test_a_failure_leaves_a_pre_existing_empty_directory(self):
        """A directory this call did not create must not be removed on failure."""
        import admin
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'projects'/'alpha').mkdir(parents=True)
            with mock.patch.object(admin, 'config', return_value={'port': 13317}), \
                    mock.patch.object(admin, 'run_bd',
                                      side_effect=subprocess.CalledProcessError(1, 'bd init')), \
                    mock.patch.object(admin, 'provision_merge_slot'), \
                    mock.patch.object(admin, 'backup_project'):
                with self.assertRaises(subprocess.CalledProcessError):
                    admin.initialize_project(root, 'alpha')
            self.assertTrue((root/'projects'/'alpha').is_dir())

    def test_a_failure_after_initialization_keeps_the_project(self):
        """A project bd finished stays readable: nothing is removed on a later failure."""
        import admin
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'projects').mkdir()

            def run_bd(runtime, name, args):
                if args[0] == 'init':
                    path = admin.project_dir(runtime, name)
                    (path/'.beads').mkdir(parents=True, exist_ok=True)
                    (path/'.beads'/'metadata.json').write_text('{}', encoding='utf-8')
                    return ''
                raise subprocess.CalledProcessError(1, 'bd configure')

            with mock.patch.object(admin, 'config', return_value={'port': 13317}), \
                    mock.patch.object(admin, 'run_bd', side_effect=run_bd), \
                    mock.patch.object(admin, 'provision_merge_slot'), \
                    mock.patch.object(admin, 'backup_project'):
                with self.assertRaises(subprocess.CalledProcessError):
                    admin.initialize_project(root, 'alpha')
            self.assertTrue((root/'projects'/'alpha'/'.beads'/'metadata.json').is_file())

    @unittest.skipUnless(POSIX, 'the creation lock needs fcntl')
    def test_the_web_creation_path_leaves_nothing_without_git(self):
        import project_creation
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'projects').mkdir()
            with mock.patch.dict(os.environ, {'PATH': ''}):
                with self.assertRaises(project_creation.NothingMade):
                    project_creation.create(root, 'alpha', 'acct', 'op-1')
            self.assertFalse((root/'projects'/'alpha').exists())
            self.assertFalse((project_creation.records_dir(root)/'alpha.json').exists())


@unittest.skipUnless(POSIX, 'the runtime lock needs fcntl')
class BootstrapRuntimeLockTests(unittest.TestCase):
    """``--bootstrap-user`` must refuse while a service holds the runtime."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base/'runtime'
        self.root.mkdir()
        self.state = self.base/'http-state.json'

    def _bootstrap(self, extra=()):
        import http_service
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch('getpass.getpass', return_value='disposable-password'), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = http_service.main(['--state', str(self.state), '--root', str(self.root),
                                      '--bootstrap-user', 'admin', *extra])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_bootstrap_is_refused_while_a_service_holds_the_runtime(self):
        import fcntl
        lock = self.root/'office-service.lock'
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, _stdout, stderr = self._bootstrap()
        self.assertEqual(code, 1)
        self.assertIn('Refusing to bootstrap', stderr)
        self.assertFalse(self.state.exists())  # refused before the state store is opened

    def test_bootstrap_takes_effect_while_the_runtime_is_free(self):
        from http_auth import Store
        code, stdout, stderr = self._bootstrap()
        self.assertEqual(code, 0, stderr)
        self.assertIn('Bootstrapped admin', stdout)
        self.assertIn('admin', Store(str(self.state)).state['usernames'])

    def test_bootstrap_refuses_without_root(self):
        import http_service
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                http_service.main(['--state', str(self.state), '--bootstrap-user', 'admin'])


class BootstrapRootTests(unittest.TestCase):
    """``--root`` naming something that is not an existing directory: one sentence.

    This check runs before the lock is opened, so it holds on a host without
    ``fcntl`` too and a mistyped root never reaches ``os.open``.
    """

    def _bootstrap_with_root(self, root):
        import http_service
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = http_service.main(['--state', str(Path(root).parent/'http-state.json'),
                                      '--root', str(root), '--bootstrap-user', 'admin'])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_bootstrap_refuses_a_root_that_does_not_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _stdout, stderr = self._bootstrap_with_root(Path(tmp)/'missing')
        self.assertEqual(code, 1)
        self.assertIn('--root must name an existing runtime directory', stderr)
        self.assertNotIn('Traceback', stderr)

    def test_bootstrap_refuses_a_root_that_is_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'a-file'
            root.write_text('not a directory', encoding='utf-8')
            code, _stdout, stderr = self._bootstrap_with_root(root)
        self.assertEqual(code, 1)
        self.assertIn('--root must name an existing runtime directory', stderr)
        self.assertNotIn('Traceback', stderr)


if __name__ == '__main__':
    unittest.main()
