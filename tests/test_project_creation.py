"""The host half of creating a project from the web interface (kittrial-5bb.118 part 2)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import admin
import project_creation as pc

ALICE, BOB = 'usr_' + 'a' * 16, 'usr_' + 'b' * 16


class Stop(BaseException):
    """A kill: not an Exception, as a signal or an interpreter exit is not."""


class Host(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'runtime'
        (self.root / 'projects').mkdir(parents=True)
        self.calls = []

    def initialize(self, fail_at=None, error=RuntimeError('the server went away')):
        """A stand-in for admin.initialize_project: the same stages, with a stop on request."""
        def work(root, name, stage):
            path = admin.project_dir(root, name)
            path.mkdir(exist_ok=True)
            for label in pc.STAGES:
                stage(label)
                if label == fail_at:
                    raise error
                self.calls.append((name, label))
                if label == 'init':
                    (path / '.beads').mkdir()
                    (path / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
            print('Created project %s' % name)          # add-project prints; nothing may leak
        return work

    def create(self, name='alpha', account=ALICE, **options):
        options.setdefault('initialize', self.initialize())
        return pc.create(self.root, name, account, 'op-' + name, **options)

    def left_behind(self, name='alpha'):
        return ((self.root / 'projects' / name).exists(), pc.record_path(self.root, name).exists())


class CreateTests(Host):
    def test_a_creation_runs_every_stage_and_records_the_result(self):
        result = self.create()
        self.assertEqual((result['status'], result['project'], result['adopted']), ('created', 'alpha', False))
        self.assertEqual([label for _, label in self.calls], list(pc.STAGES))
        record = pc.read_record(self.root, 'alpha')
        self.assertEqual((record['state'], record['by'], record['operation_id'], record['stage']),
                         ('created', ALICE, 'op-alpha', None))
        self.assertTrue(record['started_at'] <= record['completed_at'])
        self.assertEqual(pc.made(self.root, 'alpha'), 'initialized')
        self.assertEqual(pc.attention(self.root), [])

    def test_the_intent_is_recorded_before_the_first_write(self):
        seen = []

        def work(root, name, stage):
            seen.append((pc.read_record(root, name)['state'], pc.made(root, name)))
            self.initialize()(root, name, stage)
        self.create(initialize=work)
        self.assertEqual(seen, [('started', 'nothing')])

    def test_what_the_work_prints_does_not_reach_the_callers_output(self):
        import contextlib
        import io
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            self.create()
        self.assertEqual(captured.getvalue(), '')

    @unittest.skipIf(sys.platform == 'win32', 'POSIX file modes')
    def test_the_record_is_private(self):
        self.create()
        self.assertEqual(pc.record_path(self.root, 'alpha').stat().st_mode & 0o777, 0o600)
        self.assertEqual(pc.records_dir(self.root).stat().st_mode & 0o777, 0o700)


class RefusalTests(Host):
    """Every refusal leaves no directory and no record."""

    def refused(self, sentence, name='alpha', **options):
        with self.assertRaises(pc.NothingMade) as caught:
            self.create(name, **options)
        self.assertIn(sentence, str(caught.exception))
        return str(caught.exception)

    def test_a_name_an_operator_already_made(self):
        made = self.root / 'projects' / 'alpha'
        (made / '.beads').mkdir(parents=True)
        (made / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        self.refused('Project name alpha is not available')
        self.assertFalse(pc.record_path(self.root, 'alpha').exists())
        self.assertEqual(self.calls, [])

    def test_a_retired_name_reads_the_same_as_a_taken_one(self):
        (self.root / 'retired' / 'alpha-20260101T000000Z').mkdir(parents=True)
        retired = self.refused('Project name alpha is not available')
        self.assertEqual(self.left_behind(), (False, False))
        (self.root / 'projects' / 'beta' / 'x').mkdir(parents=True)
        taken = self.refused('Project name beta is not available', name='beta')
        self.assertEqual(retired.replace('alpha', 'N'), taken.replace('beta', 'N'))

    def test_a_name_another_account_holds(self):
        self.create()
        self.refused('Project name alpha is not available', account=BOB)
        self.assertEqual(pc.read_record(self.root, 'alpha')['by'], ALICE)

    def test_a_bad_name_is_refused_before_anything_is_read(self):
        for name in ('A', 'x', 'has space', '../up', 'a' * 25, '9lives'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.create(name)
        self.assertEqual(list((self.root / 'projects').iterdir()), [])
        self.assertFalse(pc.records_dir(self.root).exists() and any(pc.records_dir(self.root).glob('*.json')))

    def test_a_failure_before_anything_is_made_leaves_nothing_and_can_be_sent_again(self):
        def work(root, name, stage):
            admin.project_dir(root, name).mkdir(exist_ok=True)
            stage('init')
            raise RuntimeError('the database server refused the connection')
        with self.assertRaises(pc.NothingMade) as caught:
            self.create(initialize=work)
        self.assertIn('nothing was made', str(caught.exception))
        self.assertEqual(self.left_behind(), (False, False))
        self.assertEqual(self.create()['status'], 'created')

    def test_the_limit_counts_registered_projects_and_names_held_on_the_host(self):
        self.assertEqual(self.create('one', limit=2)['status'], 'created')
        # `one` is held on the host and not registered yet: it counts.
        self.assertEqual(self.create('two', limit=2)['status'], 'created')
        said = self.refused('The limit of 2 project(s) for this account is reached', name='three', limit=2)
        self.assertIn('2 in use', said)
        self.assertEqual(self.left_behind('three'), (False, False))
        # Registered ones count once, whether or not a host record exists for them.
        self.refused('limit of 2', name='three', limit=2, registered=['one', 'two'])
        self.refused('limit of 3', name='three', limit=3, registered=['one', 'two', 'legacy'])
        self.assertEqual(self.create('three', limit=3, registered=['one', 'two'])['status'], 'created')
        # Another account's names do not count, and a superuser (no limit) is never refused.
        self.assertEqual(self.create('four', account=BOB, limit=1)['status'], 'created')
        self.assertEqual(self.create('five', limit=None)['status'], 'created')
        self.assertEqual(pc.holds(self.root, ALICE, registered=['one']), ['five', 'three', 'two'])

    def test_an_archived_project_is_known_and_is_not_counted_again_from_its_host_record(self):
        self.create('one', limit=1)
        # Registered and then archived: the web service no longer counts it, and knows it.
        with self.assertRaises(pc.NothingMade):
            self.create('two', limit=1)                             # unknown to the web service: still held
        self.assertEqual(self.create('two', limit=1, registered=[], known=['one'])['status'], 'created')


class StopTests(Host):
    def test_a_stop_after_something_was_made_is_incomplete_and_holds_the_name(self):
        result = self.create(initialize=self.initialize(fail_at='merge-slot'))
        self.assertEqual((result['status'], result['project'], result['stage']), ('incomplete', 'alpha', 'merge-slot'))
        for words in ('Project alpha was started on the server and did not finish', 'An operator must finish it',
                      'admin.py finish-project alpha', 'admin.py retire-project alpha', 'Nothing is registered'):
            self.assertIn(words, result['message'])
        record = pc.read_record(self.root, 'alpha')
        self.assertEqual((record['state'], record['stage']), ('incomplete', 'merge-slot'))
        self.assertIn('the server went away', record['error'])
        # The same request again does no work and gives the same answer.
        calls = list(self.calls)
        again = self.create()
        self.assertEqual((again['status'], again['stage']), ('incomplete', 'merge-slot'))
        self.assertEqual(self.calls, calls)
        # The name is held: it counts toward the limit, and nobody else gets it.
        self.assertEqual(pc.holds(self.root, ALICE), ['alpha'])
        with self.assertRaises(pc.NothingMade):
            self.create('beta', limit=1)
        with self.assertRaises(pc.NothingMade):
            self.create(account=BOB)
        self.assertEqual([item['project'] for item in pc.attention(self.root)], ['alpha'])
        self.assertEqual(pc.attention(self.root)[0]['by'], ALICE)

    def test_a_long_error_is_bounded_in_the_record(self):
        self.create(initialize=self.initialize(fail_at='configure', error=RuntimeError('x' * 5000)))
        self.assertLessEqual(len(pc.read_record(self.root, 'alpha')['error']), pc.ERROR_LIMIT + 3)

    def test_a_killed_process_leaves_the_intent_which_reads_incomplete(self):
        with self.assertRaises(Stop):
            self.create(initialize=self.initialize(fail_at='first-backup', error=Stop()))
        record = pc.read_record(self.root, 'alpha')
        self.assertEqual((record['state'], pc.effective_state(self.root, record)), ('incomplete', 'incomplete'))
        # Killed outright, with no chance to write anything after the intent:
        pc.write_record(self.root, 'alpha', dict(record, state='started'))
        self.assertEqual(pc.effective_state(self.root, pc.read_record(self.root, 'alpha')), 'incomplete')
        self.assertEqual(self.create()['status'], 'incomplete')
        self.assertEqual(pc.holds(self.root, ALICE), ['alpha'])

    def test_a_kill_before_anything_was_made_is_resumed_by_the_same_request(self):
        pc.write_record(self.root, 'alpha', {'project': 'alpha', 'by': ALICE, 'operation_id': 'op-alpha',
                                             'state': 'started', 'stage': None, 'started_at': 'x'})
        (self.root / 'projects' / 'alpha').mkdir()            # an empty directory, as add-project resumes
        self.assertEqual(pc.attention(self.root), [])
        self.assertEqual(self.create(limit=1)['status'], 'created')


class OperatorTests(Host):
    def finish_steps(self, root, name):
        self.calls.append((name, 'finished'))

    def test_finish_completes_an_initialized_project_and_the_creator_then_adopts_it(self):
        self.create(initialize=self.initialize(fail_at='merge-slot'))
        record = pc.finish(self.root, 'alpha', finish_steps=self.finish_steps)
        self.assertEqual((record['state'], record['finished_by'], 'error' in record), ('created', 'operator', False))
        self.assertIn(('alpha', 'finished'), self.calls)
        self.assertEqual(pc.attention(self.root), [])
        calls = list(self.calls)
        adopted = self.create()                       # the creator's next request: no work, only the answer
        self.assertEqual((adopted['status'], adopted['adopted']), ('created', True))
        self.assertEqual(self.calls, calls)
        with self.assertRaises(pc.NothingMade):       # still nobody else's
            self.create(account=BOB)
        # Finishing twice changes nothing.
        self.assertEqual(pc.finish(self.root, 'alpha', finish_steps=self.finish_steps)['state'], 'created')
        self.assertEqual(self.calls, calls)

    def test_finish_refuses_what_was_never_initialized(self):
        with self.assertRaises(pc.NothingMade):                       # nothing made: no record at all
            self.create(initialize=self.initialize(fail_at='init'))
        with self.assertRaises(ValueError) as caught:
            pc.finish(self.root, 'alpha', finish_steps=self.finish_steps)
        self.assertIn('no project creation record', str(caught.exception))

        def half(root, name, stage):
            path = admin.project_dir(root, name); path.mkdir(exist_ok=True)
            stage('init'); (path / 'half-written').write_text('x', encoding='utf-8')
            raise RuntimeError('stopped inside init')
        self.assertEqual(self.create('beta', initialize=half)['status'], 'incomplete')
        with self.assertRaises(ValueError) as caught:
            pc.finish(self.root, 'beta', finish_steps=self.finish_steps)
        self.assertIn('was not initialized', str(caught.exception))
        self.assertIn('retire-project beta', str(caught.exception))
        self.assertNotIn(('beta', 'finished'), self.calls)

    def test_a_removed_creation_holds_nothing_and_its_record_says_who_removed_it(self):
        self.create(initialize=self.initialize(fail_at='configure'))
        self.assertEqual(pc.holds(self.root, ALICE), ['alpha'])
        record = pc.mark_removed(self.root, 'alpha', 'ops')
        self.assertEqual((record['state'], record['removed_by']), ('removed', 'ops'))
        self.assertEqual(pc.holds(self.root, ALICE), [])
        self.assertEqual(pc.attention(self.root), [])
        # A finished creation is not touched by a later retire of the project.
        self.create('beta')
        self.assertIsNone(pc.mark_removed(self.root, 'beta', 'ops'))
        self.assertEqual(pc.read_record(self.root, 'beta')['state'], 'created')
        self.assertIsNone(pc.mark_removed(self.root, 'never', 'ops'))

    def test_a_damaged_record_is_reported_and_never_read_as_absent(self):
        self.create()
        pc.record_path(self.root, 'alpha').write_text('{"project": "other"}', encoding='utf-8')
        with self.assertRaises(ValueError):
            pc.read_record(self.root, 'alpha')
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual([(item['project'], item['state']) for item in pc.attention(self.root)], [('alpha', 'damaged')])

    def test_records_lists_every_creation_with_its_effective_state(self):
        self.create('one')
        self.create('two', initialize=self.initialize(fail_at='first-backup'))
        self.assertEqual([(r['project'], r['effective']) for r in pc.records(self.root)],
                         [('one', 'created'), ('two', 'incomplete')])


class AddProjectStillTests(unittest.TestCase):
    """add-project is the same work in the same order, and prints what it printed."""

    def test_the_command_runs_initialize_then_prints(self):
        calls = []
        saved = (admin.initialize_project, admin.scheduled_backup_coverage, admin.worker_client_setup)
        admin.initialize_project = lambda root, name, stage=None: calls.append(('initialize', name, stage))
        admin.scheduled_backup_coverage = lambda root, name: (True, 'SCHEDULE TEXT')
        admin.worker_client_setup = lambda root, name: 'CLIENT TEXT'
        try:
            import contextlib
            import io
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                admin.add_project(Path('/srv/rt'), 'alpha')
        finally:
            admin.initialize_project, admin.scheduled_backup_coverage, admin.worker_client_setup = saved
        self.assertEqual(calls, [('initialize', 'alpha', None)])
        self.assertEqual(out.getvalue(), 'Created project alpha\nSCHEDULE TEXT\nCLIENT TEXT\n')

    def test_initialize_runs_the_five_stages_in_order(self):
        seen = []
        saved = (admin.run_bd, admin.provision_merge_slot, admin.backup_project, admin.config,
                 admin.refuse_retired_name)
        admin.run_bd = lambda root, name, args: seen.append(args[0] if args[0] != 'config' else 'config ' + args[2])
        admin.provision_merge_slot = lambda root, name: seen.append('slot')
        admin.backup_project = lambda root, name: seen.append('first backup')
        admin.config = lambda root: {'port': 3306}
        admin.refuse_retired_name = lambda root, name: None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'projects').mkdir()
            try:
                admin.initialize_project(root, 'alpha', seen.append)
            finally:
                (admin.run_bd, admin.provision_merge_slot, admin.backup_project, admin.config,
                 admin.refuse_retired_name) = saved
        self.assertEqual(seen, ['init', 'init', 'configure', 'config no-git-ops', 'config dolt.auto-push',
                                'config dolt.auto-commit', 'config backup.git-push', 'backup-target', 'backup',
                                'merge-slot', 'slot', 'first-backup', 'first backup'])


if __name__ == '__main__':
    unittest.main()
