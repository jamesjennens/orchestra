"""Operator hygiene (kittrial-5bb.85).

`admin.py retire-project` moves a partial or drill project aside without deleting
anything, so the backup gate passes again; a retired name is never reused; the
`restore-new` notice says what the destination really is; and every reconcile host
command checks the deployment operator allowlist before it reads a receipt.
"""
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import admin
from test_backup_multi import make_project, write_pair

OPERATOR = 'ops-james'
# The move renames a directory while its own coordination lock file is held open, which
# POSIX allows and Windows refuses; the command itself is POSIX-only (it takes fcntl locks).
REAL_SQL = admin.sql
SERVER = {'dolt_server_host': '127.0.0.1', 'dolt_server_port': 13317, 'dolt_server_user': 'root'}


def server_project(root, name):
    """A project as `bd init --server` leaves it: its metadata records the Dolt server."""
    path = make_project(root, name)
    (path / '.beads' / 'metadata.json').write_text(json.dumps(dict(SERVER, dolt_database=name)), encoding='utf-8')
    return path


moves = unittest.skipUnless(os.name == 'posix', 'retire-project renames a directory holding its open lock file '
                                                '(POSIX only)')


def tree(path):
    """Every file under `path` with its bytes: what "untouched" means."""
    return {str(item.relative_to(path)): item.read_bytes() for item in sorted(path.rglob('*'))
            if item.is_file() and item.suffix != '.lock'}       # lock files are empty and made on first use


class RetireCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'runtime'
        (self.root / 'backups').mkdir(parents=True)
        (self.root / 'projects').mkdir()
        (self.root / 'deployment.private.json').write_text(json.dumps(
            {'port': 13317, 'unit': 'beads-example.service', 'password': 'test-only-password', 'schema': 1,
             'operators': [OPERATOR]}), encoding='utf-8')
        # alpha is a healthy project; gamma is what a stopped restore-new leaves: an
        # initialized project bd cannot read, beside the pair add-project made.
        for name in ('alpha', 'gamma'):
            server_project(self.root, name)
            write_pair(self.root, name)
            (self.root / 'backups' / name / 'manifest').write_text('native ' + name, encoding='utf-8')
        self.flock = Mock()
        self.unreadable = {'gamma'}
        self.slot = {}
        self.issues = {}
        self.slot_fails = set()
        self.bd = []
        self.probes = []
        self.server = '1'                      # what the Dolt server answers the probe, or an exception
        for patcher in (patch.object(admin, 'sql', side_effect=self.sql),
                        patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=self.flock, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)}),
                        patch.dict(os.environ, {'ORCHESTRA_OPERATORS': ''}),
                        patch.object(admin, 'root_path', return_value=self.root),
                        patch.object(admin, 'run_bd', side_effect=self.run_bd)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def sql(self, root, query, password=None, timeout=None):
        self.probes.append((query, timeout))
        if isinstance(self.server, BaseException):
            raise self.server
        return self.server

    def run_bd(self, root, name, args):
        self.bd.append((name, list(args)))
        if name in self.unreadable:
            raise subprocess.CalledProcessError(1, ['bd'], stderr='PROJECT IDENTITY MISMATCH')
        if args[:2] == ['merge-slot', 'check']:
            if name in self.slot_fails:
                raise subprocess.CalledProcessError(1, ['bd'], stderr='merge slot unreadable')
            return json.dumps({'available': name not in self.slot, 'holder': self.slot.get(name), 'waiters': None,
                               'id': name + '-merge-slot'})
        return json.dumps([{'id': name + '-merge-slot'}] + [{'id': '%s-%d' % (name, index)}
                                                             for index in range(self.issues.get(name, 0))])

    def run_admin(self, *argv):
        stdout, stderr, code = io.StringIO(), io.StringIO(), 0
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                admin.main()
            except SystemExit as exit:
                code = exit.code if isinstance(exit.code, int) else 1
                if exit.code is not None and not isinstance(exit.code, int):
                    stderr.write(str(exit.code))
            except ValueError as refusal:
                code = 1
                stderr.write(str(refusal))
        return stdout.getvalue(), stderr.getvalue(), code

    def nightly(self, fail=('gamma',)):
        """`backup --all` as the schedule runs it; gamma's sync fails as a partial project's does."""
        def backup(root, name):
            if name in fail:
                raise RuntimeError("backup 'default' not found")
            write_pair(root, name)
            return 'native output for ' + name
        with patch.object(admin, 'backup_project', side_effect=backup):
            return self.run_admin('backup', '--all')

    def retire(self, name='gamma', actor=OPERATOR, *extra):
        return self.run_admin('retire-project', name, '--actor', actor, '--reason', 'left by a stopped restore',
                              *extra)

    def journal(self):
        path = self.root / 'retired' / 'journal.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    # -- the gate ------------------------------------------------------------------------
    @moves
    def test_the_nightly_gate_passes_again_after_retiring_a_partial_project(self):
        self.assertNotEqual(self.nightly()[2], 0)
        stdout, stderr, code = self.run_admin('backup-status', '--require-complete')
        self.assertNotEqual(code, 0)
        self.assertIn('gamma: the last run recorded failed', stderr)
        record_file = (self.root / 'backups' / admin.BACKUP_STATUS_NAME).read_bytes()
        backups = tree(self.root / 'backups')
        marker = (self.root / 'projects' / 'gamma' / '.beads' / 'metadata.json').read_bytes()

        stdout, stderr, code = self.retire()                       # no --force: it is not a working tracker
        self.assertEqual(code, 0, stderr)
        result = json.loads(stdout)
        entry = result['destination'].split('/', 1)[1]
        self.assertRegex(entry, r'^gamma-[0-9]{8}T[0-9]{6}Z$')
        self.assertEqual((result['forced'], result['overrode'], result['findings']['bd'],
                          result['findings']['issues'], result['findings']['merge_slot'],
                          result['findings']['last_backup_run']),
                         (False, [], 'rejects', None, 'not-applicable', 'failed'))
        self.assertIn('Nothing was deleted', stderr)
        self.assertIn('archive it there', stderr)                  # the web interface reminder
        # Moved, not deleted; no backup directory was touched, gamma's own included.
        self.assertFalse((self.root / 'projects' / 'gamma').exists())
        self.assertEqual((self.root / 'retired' / entry / '.beads' / 'metadata.json').read_bytes(), marker)
        self.assertEqual(tree(self.root / 'backups'), backups)
        self.assertEqual(admin.initialized_projects(self.root), ['alpha'])
        self.assertEqual([(line['event'], line['project'], line['actor'], line['reason'], line['destination'])
                          for line in self.journal()],
                         [(event, 'gamma', OPERATOR, 'left by a stopped restore', 'retired/' + entry)
                          for event in ('intent', 'retired')])

        # The gate passes at once, from the SAME run record, and names the retired project.
        stdout, stderr, code = self.run_admin('backup-status', '--require-complete')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)['retired'], [entry])
        self.assertEqual((self.root / 'backups' / admin.BACKUP_STATUS_NAME).read_bytes(), record_file)
        # ... and the next nightly run covers only what is left.
        stdout, stderr, code = self.nightly()
        self.assertEqual(code, 0, stderr)
        record = admin.read_backup_status(self.root)
        self.assertEqual(([item['name'] for item in record['projects']], record['status']), (['alpha'], 'complete'))

    def test_backup_status_is_unchanged_while_nothing_is_retired(self):
        self.nightly(fail=())
        stdout, _, code = self.run_admin('backup-status', '--require-complete')
        self.assertEqual((code, json.loads(stdout)), (0, admin.read_backup_status(self.root)))
        self.assertNotIn('retired', json.loads(stdout))

    @moves
    def test_a_retired_project_with_a_broken_pair_is_no_longer_a_gap(self):
        self.nightly()
        self.retire()
        (self.root / 'backups' / 'gamma.coordination.json').write_text(
            json.dumps({'schema_version': 1, 'status': 'pending'}), encoding='utf-8')
        self.assertEqual(admin.require_complete_problems(self.root, admin.read_backup_status(self.root)), [])

    # -- refusals ------------------------------------------------------------------------
    def test_only_a_listed_operator_retires_and_a_refusal_changes_nothing(self):
        before = tree(self.root)
        for actor, message in (('mallory', 'not a server-side configured operator'),):
            stdout, stderr, code = self.retire('gamma', actor)
            self.assertNotEqual(code, 0)
            self.assertIn(message, stderr)
        stdout, stderr, code = self.run_admin('retire-project', 'gamma', '--actor', OPERATOR, '--reason', '  ')
        self.assertIn('A reason is required', stderr)
        stdout, stderr, code = self.retire('nosuch')
        self.assertIn('Unknown project', stderr)
        self.assertEqual(tree(self.root), before)
        self.assertEqual(self.bd, [])                               # refused before any native call
        self.flock.assert_not_called()

    def test_the_force_rule_fails_closed(self):
        # Review 01a10219, P2 and P3 (a): what cannot be read is treated as the dangerous
        # answer, and a project that holds issues needs --force whatever its backups say.
        self.issues['alpha'] = 3
        self.nightly(fail=('alpha', 'gamma'))                       # alpha's own last backup failed
        before = tree(self.root)
        stdout, stderr, code = self.retire('alpha')
        self.assertNotEqual(code, 0)
        self.assertIn('bd reads it and it holds 3 issue(s), so it looks like a working tracker', stderr)
        # The Dolt server is down: nothing is known, so it is refused, and bd is not asked.
        self.bd.clear()
        self.server = subprocess.CalledProcessError(1, ['dolt'], stderr='refused')
        stdout, stderr, code = self.retire('gamma')
        self.server = '1'
        self.assertNotEqual(code, 0)
        self.assertIn('the Dolt server (or bd) could not be reached, so the project could not be checked: it may '
                      'be a healthy tracker', stderr)
        self.assertIn('its merge slot could not be read, so it is treated as held', stderr)
        self.assertEqual(self.bd, [])
        # The server answers and bd rejects the project: that is the partial project, no flag.
        findings = admin.retire_findings(self.root, 'gamma')
        self.assertEqual((findings['metadata'], findings['server'], findings['bd'], admin.retire_blockers(findings)),
                         ('server', 'up', 'rejects', []))
        # bd reads an empty project but not its merge slot: treated as held.
        server_project(self.root, 'delta')
        self.slot_fails.add('delta')
        stdout, stderr, code = self.retire('delta')
        self.assertNotEqual(code, 0)
        self.assertIn('its merge slot could not be read, so it is treated as held', stderr)
        self.assertNotIn('working tracker', stderr)
        # A receipt that cannot be read is treated as pending.
        self.slot_fails.clear()
        requests = self.root / 'projects' / 'delta' / '.requirement-requests'
        requests.mkdir()
        (requests / ('0' * 64 + '.json')).write_text('not json', encoding='utf-8')
        (requests / ('1' * 64 + '.json')).write_text(json.dumps({'status': 7}), encoding='utf-8')
        stdout, stderr, code = self.retire('delta')
        self.assertNotEqual(code, 0)
        self.assertIn('its reservations could not all be read, so they are treated as pending (2 in '
                      '.requirement-requests)', stderr)
        self.assertEqual(tree(self.root), before | {k: v for k, v in tree(self.root).items() if 'delta' in k})
        self.assertEqual(self.journal(), [])

    def test_an_unreadable_metadata_file_is_not_an_empty_project(self):
        # Review 01a1026a, P2: with metadata.json present but unusable the kit found no
        # server coordinates, skipped the server check and ran bd, which fell back to an
        # embedded database inside the project and reported zero issues.
        self.issues['alpha'] = 3
        metadata = self.root / 'projects' / 'alpha' / '.beads' / 'metadata.json'
        for label, text in (('not JSON', '{"dolt_server_host": '), ('not an object', '[]'),
                            ('no server coordinates', '{}'), ('not text', None)):
            with self.subTest(label):
                metadata.write_bytes(b'\xff\xfe') if text is None else metadata.write_text(text, encoding='utf-8')
                self.bd.clear()
                self.probes.clear()
                findings = admin.retire_findings(self.root, 'alpha')
                self.assertEqual((findings['metadata'], findings['bd'], findings['issues'], findings['merge_slot']),
                                 ('unreadable', 'unreachable', None, 'unreadable'))
                stdout, stderr, code = self.retire('alpha')
                self.assertNotEqual(code, 0)
                self.assertIn('its .beads/metadata.json is there but could not be read (or does not record the Dolt '
                              'server), so the project could not be checked: it may be a healthy tracker', stderr)
                self.assertEqual((self.bd, self.probes), ([], []))          # bd is never run without coordinates
                self.assertTrue(metadata.is_file())
        self.assertEqual(self.journal(), [])
        if os.name == 'posix' and os.geteuid() != 0:
            metadata.write_text(json.dumps(dict(SERVER, dolt_database='alpha')), encoding='utf-8')
            for target in (metadata, metadata.parent):                      # the file, then .beads itself, at mode 000
                mode = target.stat().st_mode
                target.chmod(0)
                try:
                    findings = admin.retire_findings(self.root, 'alpha')
                finally:
                    target.chmod(mode)
                self.assertEqual((findings['metadata'], findings['bd']), ('unreadable', 'unreachable'), target)
                self.assertIn('could not be read', '; '.join(admin.retire_blockers(findings)))
            self.assertEqual(self.bd, [])
        # A directory that was never initialized has nothing to read; bd is not run there either.
        (self.root / 'projects' / 'epsilon').mkdir()
        self.bd.clear()
        findings = admin.retire_findings(self.root, 'epsilon')
        self.assertEqual((findings['metadata'], findings['bd'], findings['merge_slot'],
                          admin.retire_blockers(findings), self.bd),
                         ('absent', 'uninitialized', 'not-applicable', [], []))

    def test_a_frozen_server_is_could_not_be_checked_not_a_hang(self):
        # Review 01a1026a, P3: the probe has a ceiling; no answer in time is "unreachable".
        self.server = subprocess.TimeoutExpired(['dolt'], admin.RETIRE_PROBE_TIMEOUT)
        stdout, stderr, code = self.retire('alpha')
        self.assertNotEqual(code, 0)
        self.assertIn('the Dolt server (or bd) could not be reached, so the project could not be checked', stderr)
        self.assertEqual(self.probes, [('SELECT 1;', admin.RETIRE_PROBE_TIMEOUT)])
        self.assertEqual(self.bd, [])
        self.assertLessEqual(admin.RETIRE_PROBE_TIMEOUT, 30)
        with patch.object(admin, 'config', return_value={'port': 13317}), \
                patch.object(admin, 'environment', return_value={}), patch.object(admin, 'checked') as checked:
            REAL_SQL(self.root, 'SELECT 1;', timeout=7)
        self.assertEqual(checked.call_args.kwargs['timeout'], 7)

    def test_a_retire_is_refused_while_a_restore_into_the_name_is_running(self):
        # Review 01a10219, P3: restore-new holds backups/NAME.restore.lock for its whole run.
        def flock(handle, flags):
            if flags & 4:                                           # LOCK_NB: the restore lock is taken
                raise BlockingIOError()
        self.flock.side_effect = flock
        before = tree(self.root)
        for extra in ((), ('--force',)):
            stdout, stderr, code = self.retire('gamma', OPERATOR, *extra)
            self.assertNotEqual(code, 0)
            self.assertIn('a restore-new into it is running', stderr)
        self.assertEqual(tree(self.root), before)
        self.assertEqual((self.bd, self.journal()), ([], []))
        self.assertEqual(admin.restore_lock_path(self.root, 'gamma'), self.root / 'backups' / 'gamma.restore.lock')

    def test_a_retire_is_refused_while_a_project_is_being_created(self):
        # kittrial-5bb.118 part 2, review 01a109cc: a creation in flight holds project-creations/.lock.
        creations = self.root / 'project-creations'
        creations.mkdir()
        (creations / '.lock').write_bytes(b'')
        (creations / '.running').write_text('delta\n', encoding='utf-8')

        def flock(handle, flags):
            if flags & 4:                                           # LOCK_NB: the creation lock is taken
                raise BlockingIOError()
        self.flock.side_effect = flock
        before = tree(self.root)
        for extra in ((), ('--force',)):
            stdout, stderr, code = self.retire('gamma', OPERATOR, *extra)
            self.assertNotEqual(code, 0)
            self.assertIn('Refusing to retire gamma: a project is being created on this server (delta). Wait for it '
                          'to finish, then retry. Nothing was changed.', stderr)
        self.assertEqual(tree(self.root), before)
        self.assertEqual((self.bd, self.journal()), ([], []))

    def test_where_no_creation_was_ever_started_a_retire_takes_no_creation_lock(self):
        stdout, stderr, code = self.retire('gamma', OPERATOR)
        self.assertEqual(code, 0, stderr)
        self.assertFalse((self.root / 'project-creations').exists())

    def test_a_failed_move_is_journaled_and_explained(self):
        # Review 01a10219, P3 (b): retired/ on another filesystem.
        self.nightly()
        with patch.object(admin.os, 'rename', side_effect=OSError(18, 'Invalid cross-device link')):
            stdout, stderr, code = self.retire()
        self.assertNotEqual(code, 0)
        self.assertIn('Could not move projects/gamma to retired/gamma-', stderr)
        self.assertIn('Invalid cross-device link', stderr)
        self.assertIn('Nothing was changed', stderr)
        self.assertEqual([(line['event'], line.get('error')) for line in self.journal()],
                         [('intent', None), ('failed', 'OSError: Invalid cross-device link')])
        self.assertTrue((self.root / 'projects' / 'gamma' / '.beads' / 'metadata.json').is_file())
        self.assertEqual(admin.retired_entries(self.root), [])

    def test_a_symlinked_retired_directory_refuses_instead_of_hiding_names(self):
        # Review 01a10219, P3 (c).
        elsewhere = Path(self.temp.name) / 'elsewhere'
        elsewhere.mkdir()
        try:
            os.symlink(elsewhere, self.root / 'retired', target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest('cannot create symlinks here: %s' % error.__class__.__name__)
        for argv in (('add-project', 'omega'), ('restore-new', 'alpha', 'omega')):
            stdout, stderr, code = self.run_admin(*argv)
            self.assertNotEqual(code, 0)
            self.assertIn('retired directory is a symlink, so retired project names cannot be checked', stderr)
        stdout, stderr, code = self.retire()
        self.assertIn('must not be a symlink', stderr)
        self.assertEqual((self.bd, list(elsewhere.iterdir())), ([], []))

    @moves
    def test_a_held_slot_and_pending_reservations_need_force(self):
        self.issues['alpha'] = 2
        self.nightly()                                              # alpha is recorded complete
        stdout, stderr, code = self.retire('alpha')
        self.assertNotEqual(code, 0)
        self.assertIn('looks like a working tracker', stderr)
        self.assertIn('Nothing was changed', stderr)
        # A project that is not working can still strand a held slot or a reservation.
        server_project(self.root, 'delta')
        self.slot['delta'] = 'alice/session'
        requests = self.root / 'projects' / 'delta' / '.coordination-requests'
        requests.mkdir()
        for index, status in enumerate(('pending', 'pending', 'complete')):
            (requests / ('%064d.json' % index)).write_text(json.dumps({'status': status}), encoding='utf-8')
        stdout, stderr, code = self.retire('delta')
        self.assertNotEqual(code, 0)
        self.assertIn('its merge slot is held by alice/session', stderr)
        self.assertIn('it has pending reservations (2 in .coordination-requests)', stderr)
        self.assertNotIn('working tracker', stderr)                 # delta has no backup pair
        self.assertEqual(self.journal(), [])
        self.assertTrue((self.root / 'projects' / 'delta').is_dir())

        for name in ('alpha', 'delta'):
            stdout, stderr, code = self.retire(name, OPERATOR, '--force')
            self.assertEqual(code, 0, stderr)
            result = json.loads(stdout)
            self.assertTrue(result['forced'])
            self.assertTrue(result['overrode'])
        done = [line for line in self.journal() if line['event'] == 'retired']
        self.assertEqual([(line['project'], line['forced'], len(line['overrode']), line['findings']['issues'])
                          for line in done], [('alpha', True, 1, 2), ('delta', True, 2, 0)])
        self.assertTrue((self.root / 'backups' / 'alpha' / 'manifest').is_file())

    # -- a retired name is never reused --------------------------------------------------
    @moves
    def test_a_retired_name_is_refused_by_add_project_and_restore_new(self):
        self.nightly()
        entry = json.loads(self.retire()[0])['destination']
        self.bd.clear()
        for argv in (('add-project', 'gamma'), ('restore-new', 'alpha', 'gamma')):
            stdout, stderr, code = self.run_admin(*argv)
            self.assertNotEqual(code, 0)
            self.assertIn('Project name gamma is retired (%s)' % entry, stderr)
            self.assertIn('Choose another name', stderr)
        self.assertEqual(self.bd, [])                               # refused before anything is created
        self.assertFalse((self.root / 'projects' / 'gamma').exists())
        # Only real entries reserve a name: a stray file or directory in retired/ does not.
        (self.root / 'retired' / 'notes.txt').write_text('x', encoding='utf-8')
        (self.root / 'retired' / 'beta-latest').mkdir()
        self.assertEqual([name for name, _ in admin.retired_entries(self.root)], ['gamma'])
        admin.refuse_retired_name(self.root, 'beta')

    @moves
    def test_backup_copy_lists_the_retired_projects(self):
        self.nightly()
        entry = json.loads(self.retire()[0])['destination'].split('/', 1)[1]
        stdout, stderr, code = self.run_admin('backup-copy', str(Path(self.temp.name) / 'off-machine'))
        self.assertEqual(code, 0, stderr)
        self.assertIn('Retired projects (not initialized, not copied): ' + entry, stdout)
        self.assertFalse((Path(self.temp.name) / 'off-machine' / 'gamma').exists())


class RestoreNoticeCase(unittest.TestCase):
    """The kittrial-5bb.82 review items on `restore-new`."""

    def test_the_notice_says_partial_only_when_the_destination_is_partial(self):
        error = subprocess.CalledProcessError(1, ['dolt'], stderr='refused')
        partial = admin.restore_failure_notice('beta', error, 'partial')
        self.assertIn('holds a partial restore', partial)
        self.assertIn('admin.py retire-project beta --actor OPERATOR --reason TEXT', partial)
        empty = admin.restore_failure_notice('beta', error, 'empty')
        self.assertIn('exists as an empty, working project: nothing was restored into it', empty)
        self.assertNotIn('partial', empty)
        self.assertIn('retire-project beta --actor OPERATOR --reason TEXT', empty)
        self.assertNotIn('--force', empty)                           # an empty project needs no flag now
        # Review 01a10219: the destination can disappear, and every step has a notice.
        gone = admin.restore_failure_notice('beta', FileNotFoundError(2, 'No such file'), 'missing',
                                            step='re-point and coordination')
        self.assertIn('the re-point and coordination step failed', gone)
        self.assertIn('The directory of project beta is no longer there', gone)
        self.assertNotIn('retire-project', gone)
        stopped = admin.restore_failure_notice('beta', KeyboardInterrupt(), 'empty', step='add-project')
        self.assertIn('the restore was interrupted during the add-project step', stopped)
        self.assertIn('holds a partial restore', admin.restore_failure_notice('beta', error))   # the default

    def test_the_destination_is_empty_only_when_bd_reads_it_and_it_holds_nothing(self):
        for answer, expected in (('[]', 'empty'), (json.dumps([{'id': 'beta-merge-slot'}]), 'empty'),
                                 (json.dumps([{'id': 'beta-merge-slot'}, {'id': 'beta-1'}]), 'partial'),
                                 ('not json', 'partial'), ('{}', 'partial'),
                                 (subprocess.CalledProcessError(1, ['bd'], stderr='PROJECT IDENTITY MISMATCH'),
                                  'partial')):
            with self.subTest(answer=str(answer)[:30]), tempfile.TemporaryDirectory() as temp, patch.object(
                    admin, 'run_bd', side_effect=[answer] if isinstance(answer, Exception) else None,
                    return_value=None if isinstance(answer, Exception) else answer):
                server_project(Path(temp), 'beta')
                self.assertEqual(admin.restore_destination_state(Path(temp), 'beta'), expected)
        with tempfile.TemporaryDirectory() as temp, patch.object(admin, 'run_bd') as native:
            self.assertEqual(admin.restore_destination_state(Path(temp), 'beta'), 'missing')
            # Review 01a1026a: a directory add-project never initialized is not "an empty,
            # working project", and bd is never run where there are no server coordinates
            # (it would create an embedded database inside the directory).
            (Path(temp) / 'projects' / 'beta').mkdir(parents=True)
            self.assertEqual(admin.restore_destination_state(Path(temp), 'beta'), 'uninitialized')
            for text in ('{}', 'not json', '[]'):
                make_project(Path(temp), 'beta').joinpath('.beads', 'metadata.json').write_text(text,
                                                                                                 encoding='utf-8')
                self.assertEqual(admin.restore_destination_state(Path(temp), 'beta'), 'partial')
            native.assert_not_called()
        stopped = admin.restore_failure_notice('beta', admin.TerminatedBySignal(signal.SIGTERM), 'uninitialized',
                                               step='add-project')
        self.assertIn('the restore was interrupted during the add-project step', stopped)
        self.assertIn('The directory projects/beta exists but the project was not initialized, so it is not a '
                      'working project', stopped)
        self.assertNotIn('empty, working project', stopped)
        self.assertIn('admin.py retire-project beta --actor OPERATOR --reason TEXT', stopped)

    def test_the_identity_step_runs_inside_the_termination_guard(self):
        seen = {}

        def adopt(root, name):
            seen['handler'] = signal.getsignal(signal.SIGTERM)
            return 'restored-identity'
        before = signal.getsignal(signal.SIGTERM)
        with patch.object(admin, 'project_server_metadata', return_value=('127.0.0.1', 13317, 'root', 'beta')), \
                patch.object(admin, 'native_restore_url', return_value='file:///backups/alpha'), \
                patch.object(admin, 'environment', return_value={}), \
                patch.object(admin, 'spawn_sync_client', return_value=''), \
                patch.object(admin, 'terminate_process_group'), \
                patch.object(admin, 'adopt_project_identity', side_effect=adopt):
            report = admin.native_restore(Path('/unused'), 'alpha', 'beta')
        self.assertIs(seen['handler'], admin.raise_termination)     # a SIGTERM here raises, it does not kill
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        self.assertIn('Adopted the restored project identity restored-identity', report)

    def test_ctrl_c_exits_with_the_sigint_status_and_no_traceback(self):
        with patch.object(admin, 'main', side_effect=KeyboardInterrupt):
            with self.assertRaises(SystemExit) as stopped:
                admin.run_main()
        self.assertEqual(stopped.exception.code, 128 + signal.SIGINT)

    def test_a_refusal_is_one_line_not_a_traceback(self):
        # Review 01a10219, P3 (e): the line a traceback would end with, and nothing else.
        import requirements
        for error, line in ((ValueError('Refusing to retire x: y'), 'ValueError: Refusing to retire x: y'),
                            (requirements.ValidationError('bad publication'), 'ValueError: bad publication')):
            with patch.object(admin, 'main', side_effect=error):
                with self.assertRaises(SystemExit) as refused:
                    admin.run_main()
            self.assertEqual(refused.exception.code, line)

    def test_an_error_that_is_not_a_kit_refusal_keeps_its_traceback(self):
        # Review 01a1026a, P2: JSONDecodeError is a ValueError; shortened to one line it named
        # no file. Only the kit's own refusals are shortened.
        for error in (json.JSONDecodeError('Expecting value', 'x', 0),
                      UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')):
            with self.subTest(error=type(error).__name__), patch.object(admin, 'main', side_effect=error):
                with self.assertRaises(type(error)):
                    admin.run_main()
        self.assertFalse(admin.kit_refusal(json.JSONDecodeError('Expecting value', 'x', 0)))
        self.assertTrue(admin.kit_refusal(ValueError('x')))

    def test_a_broken_json_file_is_a_refusal_that_names_the_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / 'deployment.private.json'
            marker.write_text('{"port": 13317,', encoding='utf-8')
            for read in (admin.config, admin.operators, admin.verifiers):
                with self.subTest(read=read.__name__), self.assertRaises(ValueError) as refused:
                    read(root)
                self.assertIs(type(refused.exception), ValueError)
                self.assertIn('Deployment configuration %s is not valid JSON: Expecting' % marker,
                              str(refused.exception))
            marker.write_text(json.dumps({'port': 13317, 'operators': [OPERATOR]}), encoding='utf-8')
            make_project(root, 'alpha')
            payload = root / 'void.json'
            payload.write_text('{"schema_version": 1, oops', encoding='utf-8')
            for command in ('void-record', 'requirement-apply', 'handoff'):
                argv = ['admin.py', '--root', str(root), command, 'alpha', '--actor', OPERATOR, '--file', str(payload)]
                with self.subTest(command=command), patch.object(sys, 'argv', argv), \
                        patch.object(admin, 'root_path', return_value=root), \
                        patch.dict(os.environ, {'ORCHESTRA_OPERATORS': ''}), \
                        patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=Mock(), LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)}), \
                        patch.object(admin, 'run_bd') as native, self.assertRaises(SystemExit) as refused:
                    admin.run_main()
                self.assertEqual(refused.exception.code.count('\n'), 0)
                self.assertIn('ValueError: Payload file %s is not valid JSON: ' % payload, refused.exception.code)
                native.assert_not_called()
            payload.write_bytes(b'\xff\xfe{')
            with self.assertRaisesRegex(ValueError, 'is not readable text'):
                admin.read_json_file(payload, 'Payload file', encoding='utf-8-sig')
            with self.assertRaises(FileNotFoundError):                      # names its path already
                admin.read_json_file(root / 'absent.json', 'Payload file')

    def test_a_stop_during_add_project_prints_the_notice(self):
        # Review 01a1026a, P3: SIGTERM is an exception for every step of restore-new, so the
        # notice is printed for a stop during add-project too.
        seen = {}

        def add_project(root, name):
            seen['handler'] = signal.getsignal(signal.SIGTERM)
            (root / 'projects' / name).mkdir(parents=True)
            raise admin.TerminatedBySignal(signal.SIGTERM)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'backups' / 'alpha').mkdir(parents=True)
            (root / 'deployment.private.json').write_text(json.dumps({'port': 13317}), encoding='utf-8')
            stderr = io.StringIO()
            before = signal.getsignal(signal.SIGTERM)
            argv = ['admin.py', '--root', str(root), 'restore-new', 'alpha', 'beta']
            with patch.object(sys, 'argv', argv), patch.object(admin, 'root_path', return_value=root), \
                    patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=Mock(), LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)}), \
                    patch.object(admin, 'coordination_backup'), patch.object(admin, 'add_project', add_project), \
                    patch.object(admin, 'run_bd') as native, contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as stopped:
                admin.run_main()
            self.assertEqual(stopped.exception.code, 128 + signal.SIGTERM)
            self.assertIs(seen['handler'], admin.raise_termination)
            self.assertIs(signal.getsignal(signal.SIGTERM), before)
            self.assertIn('restore-new did not complete: the restore was interrupted during the add-project step. '
                          'The directory projects/beta exists but the project was not initialized',
                          stderr.getvalue())
            native.assert_not_called()


class ReconcileAllowlistCase(unittest.TestCase):
    """Every reconcile host command checks the allowlist, strictly, before the receipt."""

    COMMANDS = (('requirement-reconcile', '--operation-id', ()),
                ('reference-reconcile', '--operation-id', ()),
                ('capability-reconcile', '--operation-id', ()),
                ('record-reconcile', '--operation-id', ('--kind', 'requirement')),
                ('record-reconcile', '--operation-id', ('--kind', 'reference')),
                ('record-reconcile', '--operation-id', ('--kind', 'capability')),
                ('reconcile-request', '--request-id', ()))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = make_project(self.root, 'trial')
        self.flock = Mock()
        self.native = Mock(return_value='[]')
        for patcher in (patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=self.flock, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)}),
                        patch.dict(os.environ, {'ORCHESTRA_OPERATORS': ''}),
                        patch.object(admin, 'root_path', return_value=self.root),
                        patch.object(admin, 'run_bd', self.native)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def configure(self, operators):
        config = {'password': 'x', 'unit': 'none'}
        if operators is not None:
            config['operators'] = operators
        (self.root / 'deployment.private.json').write_text(json.dumps(config), encoding='utf-8')

    def run_admin(self, command, flag, extra, actor):
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), command, 'trial', flag, 'op-1',
                                        '--actor', actor, '--reason', 'r', *extra]), \
                contextlib.redirect_stdout(io.StringIO()):
            admin.main()

    def test_an_unlisted_actor_is_refused_before_the_receipt_is_read(self):
        self.configure([OPERATOR])
        for command, flag, extra in self.COMMANDS:
            with self.subTest(command=' '.join((command,) + extra)):
                with self.assertRaisesRegex(ValueError, 'Actor mallory is not a server-side configured operator'):
                    self.run_admin(command, flag, extra, 'mallory')
        self.native.assert_not_called()
        self.flock.assert_not_called()
        self.assertEqual(sorted(path.name for path in self.project.iterdir()), ['.beads'])   # no journal made

    def test_an_empty_allowlist_authorizes_nobody_and_says_what_to_do(self):
        for operators in (None, []):
            self.configure(operators)
            for command, flag, extra in self.COMMANDS:
                with self.subTest(operators=operators, command=' '.join((command,) + extra)):
                    with self.assertRaisesRegex(ValueError, 'No operator allowlist is configured'):
                        self.run_admin(command, flag, extra, OPERATOR)
        self.native.assert_not_called()

    def test_a_listed_operator_reaches_the_receipt(self):
        self.configure([OPERATOR])
        for command, flag, extra in self.COMMANDS:
            with self.subTest(command=' '.join((command,) + extra)):
                with self.assertRaisesRegex(ValueError, 'receipt|request'):
                    self.run_admin(command, flag, extra, OPERATOR)

    def test_a_shell_allowlist_that_disagrees_is_refused(self):
        self.configure([OPERATOR])
        with patch.dict(os.environ, {'ORCHESTRA_OPERATORS': 'mallory'}), \
                self.assertRaisesRegex(ValueError, 'ORCHESTRA_OPERATORS is set in this shell'):
            self.run_admin('reference-reconcile', '--operation-id', (), 'mallory')


if __name__ == '__main__':
    unittest.main()
