"""The host half of creating a project from the web interface (kittrial-5bb.118 part 2)."""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
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
                      'admin.py finish-project alpha', 'admin.py remove-creation alpha', 'Nothing is registered'):
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
        self.assertEqual([item['state'] for item in pc.attention(self.root)], ['stalled'])
        self.assertEqual(self.create(limit=1)['status'], 'created')
        self.assertEqual(pc.attention(self.root), [])


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
        # Finishing twice changes nothing, and says so.
        again = pc.finish(self.root, 'alpha', finish_steps=self.finish_steps)
        self.assertEqual((again['state'], again['already']), ('created', True))
        self.assertNotIn('already', record)
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
        self.assertIn('remove-creation beta', str(caught.exception))
        self.assertIn('its name cannot be used again', str(caught.exception))
        self.assertNotIn('retire-project', str(caught.exception))
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


def in_thread(fn):
    """Run ``fn`` in another thread (a second holder of nothing) and return its result or its exception."""
    import threading
    box = {}

    def run():
        try:
            box['value'] = fn()
        except BaseException as error:           # noqa: BLE001
            box['error'] = error
    worker = threading.Thread(target=run)
    worker.start()
    worker.join(30)
    if 'error' in box:
        raise box['error']
    return box['value']


class Operators(Host):
    def setUp(self):
        super().setUp()
        (self.root / 'deployment.private.json').write_text(json.dumps({'operators': ['ops']}), encoding='utf-8')
        self.retired = []

    def retire(self, root, name, actor, reason, force=False, creation_locked=False):
        self.retired.append((name, actor, reason, force, creation_locked))
        os.rename(root / 'projects' / name, root / ('gone-' + name))


class StateTests(Operators):
    """What a record reads as: running, incomplete, stalled (review 01a109cc)."""

    def test_a_creation_in_flight_reads_running_and_nothing_else_touches_it(self):
        seen = {}

        def work(root, name, stage):
            self.initialize()(root, name, stage)
            seen['records'] = in_thread(lambda: [(r['project'], r['effective']) for r in pc.records(root)])
            seen['attention'] = in_thread(lambda: [(i['project'], i['state'], i['command']) for i in pc.attention(root)])
            seen['registrable'] = in_thread(lambda: pc.registrable(root, name))
            for label, call in (('create', lambda: pc.create(root, 'beta', BOB, 'op-beta', initialize=self.initialize())),
                                ('remove', lambda: pc.remove(root, name, 'ops', 'why', retire=self.retire))):
                try:
                    in_thread(call)
                    seen[label] = 'done'
                except (pc.Busy, ValueError) as refusal:
                    seen[label] = refusal
        self.assertEqual(self.create(initialize=work)['status'], 'created')
        self.assertEqual(seen['records'], [('alpha', 'running')])
        self.assertEqual(seen['attention'], [('alpha', 'running', 'nothing: it is being created now')])
        self.assertIn('has not finished (running)', seen['registrable'])
        self.assertIsInstance(seen['create'], pc.Busy)
        self.assertEqual(str(seen['create']), pc.BUSY)
        self.assertIn('a creation is running on this server (alpha)', str(seen['remove']))
        self.assertEqual(self.retired, [])
        # Afterwards nothing runs, and nothing needs attention.
        self.assertIsNone(pc.running_name(self.root))
        self.assertEqual(pc.attention(self.root), [])
        self.assertFalse((pc.records_dir(self.root) / pc.RUN_NAME).exists())
        self.assertFalse((self.root / 'projects' / 'beta').exists())

    def test_a_stale_name_beside_the_lock_means_nothing_runs(self):
        self.create(initialize=self.initialize(fail_at='configure'))
        (pc.records_dir(self.root) / pc.RUN_NAME).write_text('alpha\n', encoding='utf-8')
        self.assertIsNone(pc.running_name(self.root))
        self.assertEqual([(r['project'], r['effective']) for r in pc.records(self.root)], [('alpha', 'incomplete')])

    def test_a_kill_before_anything_was_made_reads_stalled_and_is_listed(self):
        pc.write_record(self.root, 'alpha', {'project': 'alpha', 'by': ALICE, 'operation_id': 'op-alpha',
                                             'state': 'started', 'stage': None, 'started_at': 'x'})
        self.assertEqual([(i['project'], i['state']) for i in pc.attention(self.root)], [('alpha', 'stalled')])
        self.assertIn('the same web request resumes it', pc.attention(self.root)[0]['command'])
        self.assertIn('remove-creation alpha', pc.attention(self.root)[0]['command'])
        self.assertEqual(pc.holds(self.root, ALICE), ['alpha'])               # it holds a place
        self.assertIn('has not finished (stalled)', pc.registrable(self.root, 'alpha'))
        with self.assertRaises(pc.NothingMade):                               # and the name, against others
            self.create(account=BOB)

    def test_registrable_only_when_there_is_no_record_or_it_finished(self):
        self.assertIsNone(pc.registrable(self.root, 'never'))
        self.create('done')
        self.assertIsNone(pc.registrable(self.root, 'done'))
        self.create('half', initialize=self.initialize(fail_at='merge-slot'))
        said = pc.registrable(self.root, 'half')
        self.assertIn('has not finished (incomplete)', said)
        self.assertIn('admin.py finish-project half', said)
        pc.mark_removed(self.root, 'half', 'ops')
        self.assertIsNone(pc.registrable(self.root, 'half'))
        pc.record_path(self.root, 'done').write_text('{"project": "other"}', encoding='utf-8')
        self.assertIn('is damaged', pc.registrable(self.root, 'done'))

    @unittest.skipIf(sys.platform == 'win32', 'endpoint imports fcntl (POSIX-only)')
    def test_the_endpoint_serves_nothing_from_a_creation_that_has_not_finished(self):
        import endpoint
        self.create('half', initialize=self.initialize(fail_at='merge-slot'))       # initialized, not finished
        for action, args in (('bd', ['list', '--json']), ('onboard', []), ('setup-status', [])):
            with self.subTest(action=action), self.assertRaises(ValueError) as caught:
                endpoint.execute(self.root, {'project': 'half', 'actor': 'alice', 'action': action, 'args': args})
            self.assertEqual(str(caught.exception),
                             'Unknown/uninitialized project: Project half is a creation that has not finished '
                             '(incomplete). It cannot be registered until an operator finishes it '
                             '(admin.py finish-project half).')


class RemoveTests(Operators):
    """`remove-creation` is tied to a creation that did not finish (review 01a109cc)."""

    def test_an_incomplete_creation_is_retired_and_its_name_stays_retired(self):
        self.create(initialize=self.initialize(fail_at='merge-slot'))
        result = pc.remove(self.root, 'alpha', 'ops', 'it stopped', retire=self.retire)
        self.assertEqual(result, {'project': 'alpha', 'removed': 'directory', 'name': 'retired', 'by': ALICE})
        self.assertEqual(self.retired, [('alpha', 'ops', 'it stopped', True, True)])
        record = pc.read_record(self.root, 'alpha')
        self.assertEqual((record['state'], record['removed_by']), ('removed', 'ops'))
        self.assertEqual((pc.holds(self.root, ALICE), pc.attention(self.root)), ([], []))

    def test_a_stalled_creation_is_cleared_and_its_name_is_free(self):
        pc.write_record(self.root, 'alpha', {'project': 'alpha', 'by': ALICE, 'operation_id': 'op-alpha',
                                             'state': 'started', 'stage': None, 'started_at': 'x'})
        (self.root / 'projects' / 'alpha').mkdir()
        result = pc.remove(self.root, 'alpha', 'ops', 'never started', retire=self.retire)
        self.assertEqual(result, {'project': 'alpha', 'removed': 'record', 'name': 'free', 'by': ALICE})
        self.assertEqual(self.retired, [])
        self.assertEqual(self.left_behind(), (False, False))
        self.assertEqual(self.create(account=BOB)['status'], 'created')        # anyone may use the name now

    def test_it_cannot_touch_a_finished_project_a_name_without_a_record_or_a_removed_one(self):
        self.create()
        (self.root / 'projects' / 'plain' / '.beads').mkdir(parents=True)
        (self.root / 'projects' / 'plain' / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        self.create('half', initialize=self.initialize(fail_at='configure'))
        pc.remove(self.root, 'half', 'ops', 'x', retire=self.retire)
        self.retired = []
        for name, part in (('alpha', 'finished: it is a project, not an unfinished creation'),
                           ('plain', 'There is no project creation record for plain'),
                           ('never', 'There is no project creation record for never'),
                           ('half', 'was already removed')):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as caught:
                    pc.remove(self.root, name, 'ops', 'why', retire=self.retire)
                self.assertIn(part, str(caught.exception))
                self.assertIn('Nothing was changed.', str(caught.exception))
        self.assertEqual(self.retired, [])
        self.assertEqual(pc.read_record(self.root, 'alpha')['state'], 'created')
        self.assertTrue((self.root / 'projects' / 'alpha').is_dir() and (self.root / 'projects' / 'plain').is_dir())

    def test_it_needs_a_listed_operator_and_a_reason(self):
        self.create(initialize=self.initialize(fail_at='configure'))
        with self.assertRaises(ValueError):
            pc.remove(self.root, 'alpha', 'mallory', 'why', retire=self.retire)
        with self.assertRaisesRegex(ValueError, 'A reason is required'):
            pc.remove(self.root, 'alpha', 'ops', '  ', retire=self.retire)
        self.assertEqual(self.retired, [])

    def test_the_messages_name_the_new_command_and_never_retire_force(self):
        result = self.create(initialize=self.initialize(fail_at='merge-slot'))
        self.assertIn('admin.py remove-creation alpha --actor OPERATOR --reason REASON', result['message'])
        self.assertNotIn('retire-project', result['message'])
        self.assertNotIn('retire-project', json.dumps(pc.attention(self.root)))
        self.assertNotIn('--force', json.dumps(pc.attention(self.root)) + pc.incomplete_message('x'))


class ServerLimitTests(Operators):
    """A limit on project databases for the whole server (review 01a109cc)."""

    def limit(self, value):
        pc.set_server_limit(self.root, value, 'ops')

    def test_the_default_and_the_setting(self):
        self.assertEqual((pc.SERVER_LIMIT_DEFAULT, pc.server_limit(self.root)), (20, 20))
        self.assertEqual(pc.set_server_limit(self.root, 3, 'ops'), {'used': 0, 'limit': 3})
        stored = json.loads((self.root / 'deployment.private.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['project_database_limit'], 3)
        audit = stored['project_database_limit_audit']
        self.assertEqual([(a['actor'], a['from'], a['to']) for a in audit], [('ops', 20, 3)])
        self.assertEqual(stored['operators'], ['ops'])                        # the rest of the file is kept
        with self.assertRaises(ValueError):
            pc.set_server_limit(self.root, 5, 'mallory')
        for bad in (0, -1, 501, True, '5', 2.0, None):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                pc.set_server_limit(self.root, bad, 'ops')
        self.assertEqual(pc.server_limit(self.root), 3)
        (self.root / 'deployment.private.json').write_text(json.dumps({'project_database_limit': 'many'}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'must be a whole number'):
            pc.server_limit(self.root)

    def test_everything_that_has_or_will_have_a_database_counts(self):
        self.create('made')
        self.create('half', initialize=self.initialize(fail_at='merge-slot'))
        (self.root / 'projects' / 'byoperator' / '.beads').mkdir(parents=True)       # add-project, no record
        (self.root / 'projects' / 'partial').mkdir()
        (self.root / 'projects' / 'partial' / 'x').write_text('x', encoding='utf-8')
        (self.root / 'retired' / 'gone-20260101T000000Z').mkdir(parents=True)        # retired: the database stays
        pc.write_record(self.root, 'stalled', {'project': 'stalled', 'by': BOB, 'operation_id': 'o', 'state': 'started',
                                               'stage': None, 'started_at': 'x'})        # holds a name, nothing made
        self.create('removed', initialize=self.initialize(fail_at='configure'))
        pc.mark_removed(self.root, 'removed', 'ops')                                  # its directory still counts
        self.assertEqual(sorted(pc.server_names(self.root)),
                         ['byoperator', 'gone', 'half', 'made', 'partial', 'removed', 'stalled'])
        self.assertEqual(pc.server_usage(self.root), {'used': 7, 'limit': 20})

    def test_at_the_limit_a_creation_is_refused_and_told_no_numbers(self):
        self.limit(2)
        self.create('one')
        self.create('two', account=BOB)
        with self.assertRaises(pc.NothingMade) as caught:
            self.create('three')
        self.assertEqual(str(caught.exception), pc.AT_SERVER_LIMIT)
        self.assertNotRegex(str(caught.exception), r'\d')
        self.assertEqual(self.left_behind('three'), (False, False))
        # The account's own limit is told first when it is the one reached.
        with self.assertRaisesRegex(pc.NothingMade, 'The limit of 1 project'):
            self.create('three', limit=1)
        self.limit(3)
        self.assertEqual(self.create('three')['status'], 'created')

    def test_a_reservation_counts_from_the_moment_it_is_written(self):
        self.limit(1)
        seen = {}

        def work(root, name, stage):
            seen['usage'] = pc.server_usage(root)['used']                    # before anything is made
            self.initialize()(root, name, stage)
        self.create(initialize=work)
        self.assertEqual(seen['usage'], 1)

    def test_a_creation_that_stalled_is_resumed_even_at_the_limit(self):
        self.limit(1)
        pc.write_record(self.root, 'alpha', {'project': 'alpha', 'by': ALICE, 'operation_id': 'op-alpha',
                                             'state': 'started', 'stage': None, 'started_at': 'x'})
        self.assertEqual(self.create()['status'], 'created')

    def test_a_stalled_creation_is_not_resumed_when_the_others_alone_fill_the_server(self):
        """Its own place is not counted against it; it is refused before anything is made, not after."""
        pc.write_record(self.root, 'alpha', {'project': 'alpha', 'by': ALICE, 'operation_id': 'op-alpha',
                                             'state': 'started', 'stage': None, 'started_at': 'x'})
        (self.root / 'projects' / 'byoperator' / '.beads').mkdir(parents=True)
        self.limit(1)                                                         # lowered while it was stalled
        with self.assertRaises(pc.NothingMade) as caught:
            self.create()
        self.assertEqual(str(caught.exception), pc.AT_SERVER_LIMIT)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / 'projects' / 'alpha').exists())
        self.limit(2)
        self.assertEqual(self.create()['status'], 'created')

    def test_the_database_servers_own_names_are_refused_before_anything_is_made(self):
        for name in sorted(pc.RESERVED_NAMES):
            with self.subTest(name=name):
                with self.assertRaises(pc.NothingMade) as caught:
                    self.create(name)
                self.assertIn('is used by the database server itself', str(caught.exception))
                self.assertEqual(self.left_behind(name), (False, False))
        with self.assertRaisesRegex(ValueError, 'is used by the database server itself'):
            admin.initialize_project(self.root, 'mysql')
        self.assertFalse((self.root / 'projects' / 'mysql').exists())


class FailureTests(Host):
    def test_a_failure_with_nothing_made_tells_the_caller_no_host_detail_and_keeps_it_for_the_operator(self):
        secret = RuntimeError('bd init --server --database alpha failed in /srv/orchestra/rt/projects/alpha')
        with self.assertRaises(pc.NothingMade) as caught:
            self.create(initialize=self.initialize(fail_at='init', error=secret))
        said = str(caught.exception)
        self.assertEqual(said, 'The project could not be created and nothing was made. Try again; if it fails again, '
                               'ask an operator of the server.')
        kept = json.loads((pc.records_dir(self.root) / 'last-failure.txt').read_text(encoding='utf-8'))
        self.assertEqual((kept['project'], kept['by']), ('alpha', ALICE))
        self.assertIn('/srv/orchestra/rt/projects/alpha', kept['error'])

    def test_an_incomplete_creation_keeps_the_detail_in_its_record_only(self):
        secret = RuntimeError('bd backup init /srv/orchestra/rt/backups/alpha failed')
        result = self.create(initialize=self.initialize(fail_at='backup-target', error=secret))
        self.assertNotIn('/srv/', json.dumps(result))
        self.assertIn('/srv/orchestra', pc.read_record(self.root, 'alpha')['error'])


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


class BackupHintTests(Operators):
    def test_an_unfinished_creation_that_failed_its_backup_is_named_with_the_two_commands(self):
        self.create('half', initialize=self.initialize(fail_at='backup-target'))
        self.create('fine')
        said = admin.unfinished_creation_hint(self.root, ['half', 'fine', 'other'])
        self.assertIn('half is a project creation from the web interface that did not finish', said)
        self.assertIn('admin.py finish-project half', said)
        self.assertIn('admin.py remove-creation half --actor OPERATOR --reason REASON', said)
        self.assertIn('Until then every backup --all is incomplete.', said)
        self.assertNotIn('fine is', said)
        self.assertEqual(admin.unfinished_creation_hint(self.root, ['fine', 'other']), '')
        self.assertEqual(admin.unfinished_creation_hint(self.root / 'nowhere', ['x']), '')


class DamagedRecordTests(Operators):
    """A creation record that cannot be read, and what holds a retired name (kittrial-5bb.143)."""

    def damage(self, name='alpha', text='{not json'):
        pc.records_dir(self.root).mkdir(exist_ok=True)
        path = pc.records_dir(self.root) / (name + '.json')
        path.write_text(text, encoding='utf-8')
        return path

    def test_it_is_listed_with_the_exact_command_and_holds_a_place_on_the_server(self):
        self.damage()
        listed = pc.attention(self.root)
        self.assertEqual([(item['project'], item['state'], item['by']) for item in listed], [('alpha', 'damaged', None)])
        self.assertIn('admin.py remove-creation alpha --actor OPERATOR --reason REASON', listed[0]['command'])
        self.assertIn('alpha.json.damaged-STAMP', listed[0]['command'])
        self.assertIn('admin.py retire-project alpha', listed[0]['command'])
        self.assertEqual(sorted(pc.server_names(self.root)), ['alpha'])
        self.assertEqual(pc.holds(self.root, ALICE), [])               # it names nobody, so it is nobody's

    def test_no_request_can_adopt_or_resume_it(self):
        for text in ('{not json', json.dumps({'project': 'alpha', 'by': ALICE, 'state': 'finished'}),
                     json.dumps({'project': 'beta', 'by': ALICE, 'state': 'created'}), '[]'):
            with self.subTest(record=text[:30]):
                path = self.damage(text=text)
                with self.assertRaises(ValueError) as caught:
                    self.create()
                self.assertNotIsInstance(caught.exception, pc.NothingMade)   # not a sentence for a person: see create_action
                self.assertEqual(self.calls, [])
                self.assertFalse((self.root / 'projects' / 'alpha').exists())
                self.assertEqual(path.read_text(encoding='utf-8'), text)
                self.assertIn('is damaged', pc.registrable(self.root, 'alpha'))

    def test_remove_creation_sets_it_aside_and_touches_nothing_else(self):
        path = self.damage()
        result = pc.remove(self.root, 'alpha', 'ops', 'unreadable record', retire=self.retire)
        self.assertEqual((result['removed'], result['name'], result['by']), ('damaged-record', 'free', None))
        self.assertRegex(result['kept_as'], r'^alpha\.json\.damaged-[0-9]{8}T[0-9]{6}Z$')
        self.assertFalse(path.exists())
        self.assertEqual((pc.records_dir(self.root) / result['kept_as']).read_text(encoding='utf-8'), '{not json')
        self.assertEqual((self.retired, pc.attention(self.root), sorted(pc.server_names(self.root))), ([], [], []))
        # The name is free again: the next request creates it.
        self.assertEqual(self.create()['status'], 'created')

    def test_set_aside_twice_in_one_second_keeps_both(self):
        first = pc.keep_damaged_record(self.root, self.damage().stem)
        second = pc.keep_damaged_record(self.root, self.damage(text='[]').stem)
        self.assertNotEqual(first.name, second.name)
        self.assertEqual((first.read_text(encoding='utf-8'), second.read_text(encoding='utf-8')), ('{not json', '[]'))

    def test_with_a_project_directory_it_leaves_a_project_with_no_creation_record(self):
        self.create()                                                  # a finished project ...
        self.damage()                                                  # ... whose record was then damaged
        before = sorted(str(path.relative_to(self.root)) for path in (self.root / 'projects').rglob('*'))
        result = pc.remove(self.root, 'alpha', 'ops', 'unreadable record', retire=self.retire)
        self.assertEqual((result['removed'], result['name']), ('damaged-record', 'a project with no creation record'))
        self.assertEqual(self.retired, [])                             # never retired on a guess
        self.assertEqual(sorted(str(path.relative_to(self.root)) for path in (self.root / 'projects').rglob('*')), before)
        self.assertIsNone(pc.registrable(self.root, 'alpha'))          # a superuser may register it
        with self.assertRaises(pc.NothingMade):                        # and no web request can create over it
            self.create(account=BOB)

    def test_it_still_needs_a_listed_operator_and_a_reason(self):
        path = self.damage()
        with self.assertRaises(ValueError):
            pc.remove(self.root, 'alpha', 'mallory', 'why', retire=self.retire)
        with self.assertRaises(ValueError):
            pc.remove(self.root, 'alpha', 'ops', ' ', retire=self.retire)
        self.assertTrue(path.exists())

    def test_a_retired_name_is_held_by_its_directory_not_by_the_journal(self):
        """`retired/journal.jsonl` is an audit trail that nothing reads; `retired/NAME-STAMP` holds the name."""
        retired = self.root / admin.RETIRED_DIR
        (retired / 'alpha-20260101T000000Z').mkdir(parents=True)
        journal = retired / admin.RETIRE_JOURNAL
        for number, (label, prepare) in enumerate((('a broken line', lambda: journal.write_text('{"action": "retire"\nnot json\n', encoding='utf-8')),
                               ('not text', lambda: journal.write_bytes(b'\xff\xfe\x00')),
                               ('absent', journal.unlink))):
            with self.subTest(journal=label):
                prepare()
                with self.assertRaises(pc.NothingMade) as caught:
                    self.create()
                self.assertEqual(str(caught.exception), pc.NOT_AVAILABLE % 'alpha')
                self.assertEqual([call for call in self.calls if call[0] == 'alpha'], [])
                self.assertEqual(self.create('beta%d' % number)['status'], 'created')   # other names are not held up
        self.assertIn('alpha', pc.server_names(self.root))

    def test_the_failure_note_keeps_the_newest_and_a_few_before_it(self):
        for number in range(8):
            pc.note_failure(self.root, 'p%d' % number, ALICE, RuntimeError('failure %d' % number), step='work')
        noted = json.loads((pc.records_dir(self.root) / pc.FAILURE_FILE).read_text(encoding='utf-8'))
        self.assertEqual((noted['project'], noted['step'], noted['error']), ('p7', 'work', 'RuntimeError: failure 7'))
        self.assertEqual([entry['project'] for entry in noted['earlier']], ['p6', 'p5', 'p4', 'p3'])
        self.assertNotIn('earlier', noted['earlier'][0])
        # A note that cannot be written, or an old one that cannot be read, never stops the answer.
        for unreadable in ('not json', '[' * 200000, '[]', '{"earlier": 7}'):
            with self.subTest(old_note=unreadable[:12]):
                (pc.records_dir(self.root) / pc.FAILURE_FILE).write_text(unreadable, encoding='utf-8')
                pc.note_failure(self.root, 'p9', ALICE, RuntimeError('x'))
                noted = json.loads((pc.records_dir(self.root) / pc.FAILURE_FILE).read_text(encoding='utf-8'))
                self.assertEqual((noted['project'], [entry for entry in noted['earlier'] if 'project' in entry]), ('p9', []))
        pc.note_failure(self.root / 'no' / 'such' / 'root', 'p9', ALICE, RuntimeError('x'))
        # Whatever goes wrong while the note is written, of whatever kind (kittrial-5bb.149).
        for failure in (RuntimeError('disk'), KeyError('x'), TypeError('y'), OSError(28, 'No space left on device')):
            with self.subTest(failure=type(failure).__name__), \
                    unittest.mock.patch.object(admin, 'atomic_private_write', side_effect=failure):
                pc.note_failure(self.root, 'p9', ALICE, RuntimeError('x'))


class UnopenableRecordTests(Operators):
    """A record that cannot be opened, a record that is a directory, a file that is no record (kittrial-5bb.149)."""

    def records_dir(self):
        pc.records_dir(self.root).mkdir(exist_ok=True)
        return pc.records_dir(self.root)

    def unopenable(self, name='alpha'):
        """A record whose every read fails, as a file owned by another user would."""
        path = self.records_dir() / (name + '.json')
        path.write_text(json.dumps({'project': name, 'by': ALICE, 'state': 'created'}), encoding='utf-8')
        real = Path.read_text

        def read_text(target, *args, **kwargs):
            if Path(target) == path:
                raise PermissionError(13, 'Permission denied', str(path))
            return real(target, *args, **kwargs)
        patcher = unittest.mock.patch.object(Path, 'read_text', read_text)
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def test_it_reads_as_damaged_everywhere_and_does_not_stop_other_names(self):
        self.unopenable()
        with self.assertRaises(ValueError) as caught:
            pc.read_record(self.root, 'alpha')
        self.assertIn('cannot be opened (PermissionError)', str(caught.exception))
        self.assertEqual([(r['project'], r['effective']) for r in pc.records(self.root)], [('alpha', 'damaged')])
        listed = pc.attention(self.root)
        self.assertEqual([(i['project'], i['state'], i['served']) for i in listed], [('alpha', 'damaged', False)])
        self.assertIn('admin.py remove-creation alpha --actor OPERATOR --reason REASON', listed[0]['command'])
        self.assertEqual(sorted(pc.server_names(self.root)), ['alpha'])
        self.assertIn('is damaged', pc.registrable(self.root, 'alpha'))
        # Its own name is held ...
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(self.calls, [])
        # ... and another name is created as usual.
        self.assertEqual(self.create('beta')['status'], 'created')

    def test_remove_creation_sets_it_aside_without_opening_it(self):
        path = self.unopenable()
        result = pc.remove(self.root, 'alpha', 'ops', 'cannot be opened', retire=self.retire)
        self.assertEqual((result['removed'], result['name']), ('damaged-record', 'free'))
        self.assertFalse(path.exists())
        self.assertTrue((pc.records_dir(self.root) / result['kept_as']).is_file())
        self.assertEqual((self.retired, pc.attention(self.root)), ([], []))

    @unittest.skipIf(sys.platform == 'win32' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'needs a file the process cannot open (POSIX, not root)')
    def test_a_file_of_mode_000_for_real(self):
        path = self.records_dir() / 'alpha.json'
        path.write_text('{}', encoding='utf-8')
        os.chmod(path, 0)
        self.addCleanup(lambda: path.exists() and os.chmod(path, 0o600))
        self.assertEqual([(r['project'], r['effective']) for r in pc.records(self.root)], [('alpha', 'damaged')])
        self.assertEqual(self.create('beta')['status'], 'created')
        result = pc.remove(self.root, 'alpha', 'ops', 'mode 000', retire=self.retire)
        self.assertEqual(result['removed'], 'damaged-record')
        self.assertEqual(pc.attention(self.root), [])

    def test_a_record_that_is_a_directory_or_a_symlink_is_damaged_like_any_other(self):
        (self.records_dir() / 'alpha.json').mkdir()
        shapes = ['alpha']
        if hasattr(os, 'symlink') and sys.platform != 'win32':
            os.symlink(self.root / 'elsewhere.json', self.records_dir() / 'beta.json')
            shapes.append('beta')
        self.assertEqual([(r['project'], r['effective']) for r in pc.records(self.root)],
                         [(name, 'damaged') for name in shapes])
        self.assertEqual(sorted(pc.server_names(self.root)), shapes)
        for name in shapes:
            with self.subTest(record=name):
                with self.assertRaises(ValueError):                    # the name is held: never "nothing was made, try again"
                    self.create(name)
                self.assertNotIn(name, [call[0] for call in self.calls])
                result = pc.remove(self.root, name, 'ops', 'not a file', retire=self.retire)
                self.assertEqual((result['removed'], result['name']), ('damaged-record', 'free'))
        self.assertEqual(pc.attention(self.root), [])
        self.assertEqual(self.create()['status'], 'created')

    def test_a_file_whose_name_no_project_can_have_is_not_a_creation_and_is_not_counted(self):
        for name in ('UPPER.json', 'a.json', 'has space.json', '.json', 'x' * 30 + '.json'):
            (self.records_dir() / name).write_text('{}', encoding='utf-8')
        found = pc.records(self.root)
        self.assertEqual(sorted((r['project'], r['effective']) for r in found),
                         sorted((Path(name).stem, 'not-a-record') for name in ('UPPER.json', 'a.json', 'has space.json', '.json',
                                                                         'x' * 30 + '.json')))
        self.assertEqual(pc.server_names(self.root), set())
        self.assertEqual(pc.server_usage(self.root)['used'], 0)
        self.assertEqual(pc.holds(self.root, ALICE), [])
        listed = {item['project']: item for item in pc.attention(self.root)}
        self.assertEqual(listed['UPPER']['state'], 'not-a-record')
        self.assertEqual(listed['UPPER']['command'],
                         'this file is not a creation record, because no project can have that name. It holds no name '
                         'and is not counted. Move project-creations/UPPER.json out of that directory')
        self.assertNotIn('remove-creation', listed['has space']['command'])
        self.assertIn('Move project-creations/.json out of that directory', listed['.json']['command'])
        with self.assertRaises(ValueError):                                # remove-creation takes project names only
            pc.remove(self.root, 'UPPER', 'ops', 'junk', retire=self.retire)
        self.assertTrue((self.records_dir() / 'UPPER.json').is_file())
        self.assertEqual(self.create()['status'], 'created')              # and it stops nothing

    def test_the_endpoint_rule_serves_a_damaged_record_and_refuses_an_unfinished_one(self):
        self.create('done')
        self.create('half', initialize=self.initialize(fail_at='merge-slot'))
        pc.write_record(self.root, 'stalled', {'project': 'stalled', 'by': ALICE, 'operation_id': 'o', 'state': 'started',
                                               'stage': None, 'started_at': 'x'})
        self.assertIsNone(pc.unfinished(self.root, 'done'))
        self.assertIsNone(pc.unfinished(self.root, 'never'))
        self.assertIn('has not finished (incomplete)', pc.unfinished(self.root, 'half'))
        self.assertIn('has not finished (stalled)', pc.unfinished(self.root, 'stalled'))
        # The records are damaged: serving no longer depends on them, registration still does.
        for name in ('done', 'half'):
            (pc.records_dir(self.root) / (name + '.json')).write_text('{not json', encoding='utf-8')
            self.assertIsNone(pc.unfinished(self.root, name))
            self.assertIn('is damaged', pc.registrable(self.root, name))
        listed = {item['project']: item for item in pc.attention(self.root)}
        self.assertEqual((listed['done']['state'], listed['done']['served']), ('damaged', True))
        self.assertTrue(listed['done']['command'].startswith(
            'projects/done is initialized and is SERVED WITH A DAMAGED CREATION RECORD: the kit cannot tell whether its '
            'creation finished. the record cannot be read'), listed['done']['command'])
        self.assertIn('admin.py remove-creation done --actor OPERATOR --reason REASON', listed['done']['command'])
        self.assertEqual(sorted(pc.server_names(self.root)), ['done', 'half', 'stalled'])

    @unittest.skipIf(sys.platform == 'win32', 'endpoint imports fcntl (POSIX-only)')
    def test_the_endpoint_serves_a_project_whose_record_is_damaged_but_not_one_that_was_never_initialized(self):
        import endpoint
        request = {'actor': 'alice', 'action': 'setup-status', 'args': []}
        # Finished and then damaged: served (setup-status is a read that needs no bd).
        self.create('done')
        (pc.records_dir(self.root) / 'done.json').write_text('{not json', encoding='utf-8')
        answer = endpoint.execute(self.root, dict(request, project='done'))
        self.assertEqual(answer['returncode'], 0, answer)
        self.assertEqual(json.loads(answer['stdout'])['creation_record'],
                         'The creation record of project done is damaged; an operator must look at it first')
        # Died inside bd init (a directory, no metadata), then its record was damaged: still refused,
        # by the same test an operator-made project has to pass.
        self.create('partial', initialize=lambda root, name, stage: (admin.project_dir(root, name) / 'x').mkdir(parents=True)
                    or (_ for _ in ()).throw(RuntimeError('died in bd init')))
        self.assertEqual(pc.made(self.root, 'partial'), 'partial')
        (pc.records_dir(self.root) / 'partial.json').write_text('{not json', encoding='utf-8')
        with self.assertRaises(ValueError) as caught:
            endpoint.execute(self.root, dict(request, project='partial'))
        self.assertEqual(str(caught.exception), 'Unknown/uninitialized project')
        # Unfinished with a readable record: refused with its sentence, as before.
        self.create('half', initialize=self.initialize(fail_at='merge-slot'))
        with self.assertRaises(ValueError) as caught:
            endpoint.execute(self.root, dict(request, project='half'))
        self.assertIn('is a creation that has not finished (incomplete)', str(caught.exception))


class RemoveCommandOutputTests(Operators):
    """What `admin.py remove-creation` prints: each sentence is true of what was done (kittrial-5bb.149)."""

    def run_admin(self, *argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                unittest.mock.patch.object(admin, 'root_path', return_value=self.root), \
                unittest.mock.patch.dict(os.environ, {'ORCHESTRA_OPERATORS': ''}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            admin.main()
        return json.loads(out.getvalue()), err.getvalue().strip()

    def damage(self, name):
        pc.records_dir(self.root).mkdir(exist_ok=True)
        (pc.records_dir(self.root) / (name + '.json')).write_text('{not json', encoding='utf-8')

    def test_a_damaged_record_with_nothing_made_is_set_aside_and_the_name_is_free(self):
        self.damage('alpha')
        result, said = self.run_admin('remove-creation', 'alpha', '--actor', 'ops', '--reason', 'unreadable')
        self.assertEqual(said, 'The creation record of alpha could not be read. It is kept as project-creations/%s and '
                               'nothing else was touched. Nothing is under projects/alpha, so the name is free again.'
                         % result['kept_as'])
        self.assertNotIn('Removed the creation record', said)             # it was not removed: it was set aside

    def test_a_damaged_record_over_a_project_directory_does_not_say_the_name_is_free(self):
        self.create()
        self.damage('alpha')
        result, said = self.run_admin('remove-creation', 'alpha', '--actor', 'ops', '--reason', 'unreadable')
        self.assertTrue(said.startswith('The creation record of alpha could not be read. It is kept as project-creations/%s '
                                        'and nothing else was touched. projects/alpha exists and is now a project with no '
                                        'creation record' % result['kept_as']), said)
        self.assertNotIn('free again', said)
        self.assertNotIn('Removed', said)

    def test_a_stalled_creation_is_removed_and_the_name_is_free(self):
        pc.write_record(self.root, 'alpha', {'project': 'alpha', 'by': ALICE, 'operation_id': 'o', 'state': 'started',
                                             'stage': None, 'started_at': 'x'})
        result, said = self.run_admin('remove-creation', 'alpha', '--actor', 'ops', '--reason', 'stalled')
        self.assertEqual(said, 'Removed the creation record of alpha. Nothing had been made for it, so the name is free again.')

    def test_the_listing_command_does_not_end_in_a_traceback_on_a_record_it_cannot_read(self):
        self.damage('alpha')
        (pc.records_dir(self.root) / 'beta.json').mkdir()
        (pc.records_dir(self.root) / 'UPPER.json').write_text('{}', encoding='utf-8')
        listed, said = self.run_admin('project-creations', '--attention')
        self.assertEqual(sorted((item['project'], item['state']) for item in listed),
                         [('UPPER', 'not-a-record'), ('alpha', 'damaged'), ('beta', 'damaged')])
        everything, said = self.run_admin('project-creations')
        self.assertEqual(len(everything), 3)


if __name__ == '__main__':
    unittest.main()
