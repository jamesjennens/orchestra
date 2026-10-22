"""Task files slice 5 on a real tracker (kittrial-5bb.380): admin backup, restore-new and purge.

The harness of test_merge_slot_real_bd: a real bd against the fixture's scratch Dolt server. POSIX
only (admin takes fcntl locks), and skipped without a bd binary.
"""
import contextlib
import io
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import task_files
import test_bd_label_aliases as rb
from test_merge_slot_real_bd import without_inherited_tests
from test_task_files_ops import Store

admin = rb.admin


@unittest.skipIf(rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@without_inherited_tests
class TaskFilesOpsRealBdTests(rb.RealBdLabelAliasTests):

    def new_project(self, name):
        path = self.root / 'projects' / name
        path.mkdir(parents=True, exist_ok=True)
        init = subprocess.run(
            [str(self.bd_path), 'init', '--server', '--external', '--server-host', '127.0.0.1',
             '--server-port', str(self.port), '--server-user', 'root', '--prefix', name,
             '--database', name, '--skip-agents', '--skip-hooks', '--non-interactive'],
            cwd=str(path), env=admin.environment(self.root), capture_output=True, text=True)
        self.assertEqual(init.returncode, 0, init.stderr or init.stdout)
        (self.root / 'backups').mkdir(exist_ok=True)
        admin.run_bd(self.root, name, ['backup', 'init', str(self.root / 'backups' / name)])
        return path

    def bd_in(self, name, *args):
        done = subprocess.run([str(self.bd_path), '--directory', str(self.root / 'projects' / name), '--sandbox',
                               '--actor', 'op', *args], env=admin.environment(self.root), capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr or done.stdout)
        return done.stdout

    def task(self, name, title, close=False):
        made = json.loads(self.bd_in(name, 'create', title, '--json'))
        made = made[0] if isinstance(made, list) else made
        if close:
            self.bd_in(name, 'close', made['id'])
        return made['id']

    def store(self, path):
        store = Store.__new__(Store)
        store.project = path
        return store

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root)] + list(argv)), \
                mock.patch.object(admin, 'root_path', return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                admin.run_main()
            except SystemExit as stopped:
                code = stopped.code
        return code, out.getvalue(), err.getvalue()

    def test_backup_carries_the_files_and_restore_new_checks_and_places_them(self):
        path = self.new_project('tfsrc')
        open_task = self.task('tfsrc', 'open task')
        files = self.store(path)
        one = files.attach(open_task, b'input spreadsheet bytes')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            admin.backup_projects(self.root, ['tfsrc'])
        self.assertIn('task files of tfsrc: 1 files, 23 bytes (backups/tfsrc.task-files)', out.getvalue())
        status = json.loads((self.root / 'backups' / admin.BACKUP_STATUS_NAME).read_text(encoding='utf-8'))
        entry = [p for p in status['projects'] if p['name'] == 'tfsrc'][0]
        self.assertEqual(('complete', 1, 23), (entry['task_files']['status'], entry['task_files']['files'],
                                               entry['task_files']['bytes']))
        self.assertTrue((self.root / 'backups' / 'tfsrc.task-files' / task_files.MANIFEST).is_file())
        away = self.root.parent / (self.root.name + '-offsite')
        self.addCleanup(shutil.rmtree, away, ignore_errors=True)
        with contextlib.redirect_stdout(io.StringIO()) as copied:
            try:
                admin.backup_copy(self.root, str(away))
            except SystemExit as stopped:
                # The class shares one runtime: projects of other tests that have no complete backup are skipped
                # and named, and the run then exits non-zero. tfsrc's pair is complete and is copied all the same.
                self.assertNotIn('tfsrc', str(stopped).split('skipped')[-1].split(';')[0])
        self.assertIn('Copied tfsrc task files', copied.getvalue())
        self.assertTrue((away / 'tfsrc.task-files' / task_files.MANIFEST).is_file(), 'backup-copy carries the folder')
        code, out, err = self.main('restore-new', 'tfsrc', 'tfdst')
        self.assertEqual(0, code, err or out)
        self.assertIn('Task files restored: 1 files, 23 bytes', out)
        clone = self.root / 'projects' / 'tfdst'
        self.assertEqual(b'input spreadsheet bytes', task_files.blob_path(clone, one).read_bytes())
        self.assertEqual([one], [e['sha256'] for e in task_files.live(clone, open_task)])

    def test_a_backup_without_the_folder_still_restores_the_project(self):
        path = self.new_project('tfold')
        open_task = self.task('tfold', 'task with a file')
        self.store(path).attach(open_task, b'bytes the older backup lacks')
        with contextlib.redirect_stdout(io.StringIO()):
            admin.backup_projects(self.root, ['tfold'])
        shutil.rmtree(self.root / 'backups' / 'tfold.task-files')          # as an older kit's backup, or a lost copy
        code, out, err = self.main('restore-new', 'tfold', 'tfnew')
        self.assertEqual(0, code, err or out)
        self.assertIn('This backup has no task files (backups/tfold.task-files)', out)
        self.assertTrue((self.root / 'projects' / 'tfnew' / '.beads' / 'metadata.json').is_file())

    def test_a_flipped_byte_in_the_backup_restores_the_project_but_not_the_files(self):
        path = self.new_project('tfbad')
        one = self.store(path).attach(self.task('tfbad', 't'), b'precious')
        with contextlib.redirect_stdout(io.StringIO()):
            admin.backup_projects(self.root, ['tfbad'])
        (self.root / 'backups' / 'tfbad.task-files' / one[:2] / one).write_bytes(b'Precious')
        code, out, err = self.main('restore-new', 'tfbad', 'tfbadr')
        self.assertEqual(admin.RESTORE_AUTHORITY_NOT_REGRANTED, code, err or out)
        self.assertIn('Task files NOT restored', err)
        self.assertFalse((self.root / 'projects' / 'tfbadr' / task_files.FOLDER).exists())
        self.assertTrue((self.root / 'projects' / 'tfbadr' / '.beads' / 'metadata.json').is_file())

    def test_purge_reads_the_real_closed_rows_and_keeps_what_an_open_task_needs(self):
        path = self.new_project('tfpur')
        closed = self.task('tfpur', 'done long ago', close=True)
        open_task = self.task('tfpur', 'still open')
        files = self.store(path)
        alone = files.attach(closed, b'only the closed task has this')
        shared = files.attach(closed, b'both tasks have this', name='shared.bin')
        files.attach(open_task, b'both tasks have this', name='shared.bin')
        deployment = self.root / 'deployment.private.json'
        configured = json.loads(deployment.read_text(encoding='utf-8'))
        configured['operators'] = ['op-purger']
        deployment.write_text(json.dumps(configured), encoding='utf-8')
        code, out, err = self.main('task-files', 'purge', 'tfpur', '--closed-before', '2999-01-01', '--actor', 'op-purger',
                                   '--dry-run')
        self.assertEqual(0, code, err or out)
        self.assertIn('would remove file %s' % alone, out)
        self.assertIn('kept %s of task %s' % (shared, closed), out)
        self.assertTrue(files.blob(alone).exists())
        code, out, err = self.main('task-files', 'purge', 'tfpur', '--closed-before', '2999-01-01', '--actor', 'op-purger')
        self.assertEqual(0, code, err or out)
        self.assertFalse(files.blob(alone).exists())
        self.assertTrue(files.blob(shared).exists())
        line = json.loads((path / task_files.FOLDER / task_files.AUDIT).read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(('op-purger', [alone]), (line['actor'], [r['sha256'] for r in line['removed']]))
        code, out, err = self.main('task-files', 'verify', 'tfpur')
        self.assertEqual(0, code, err or out)
