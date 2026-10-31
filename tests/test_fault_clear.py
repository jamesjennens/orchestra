"""A lane's ``needs-investigation`` flag (kittrial-5bb.398): raise, show, wake, clear.

Pure-module tests over an in-memory export, a disposable project directory for the host
journal and a fake native runner. The HTTP lane board and the HTTP coordinator wait have their
own cases in ``test_web_lanes`` and ``test_coordinator_http_wait``.
"""
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import fault_clear
import lane_state
import lanes
import reserved_comments
import waiting
import work
import test_lanes as tl
from requirements import canonical_bytes

try:
    import endpoint
except ImportError:            # endpoint.py imports fcntl; POSIX only
    endpoint = None

NOW = tl.NOW
A, B = tl.A, tl.B
COORD = 'coordinator-1'
RAISED = '2026-09-15T10:00:00Z'


def raise_fault(row, cid='f1', stamp=RAISED, reason='the merge slot is stuck', checkpoint=None, **changes):
    """A checkpoint plus a needs-investigation record on a row; returns the record's comment id."""
    checkpoint = checkpoint or 'cp-' + cid
    tl.add_checkpoint(row, checkpoint, stamp)
    tl.add_lane_state(row, cid, checkpoint, stamp, state='needs-investigation', reason=reason, **changes)
    return cid


class FakeNative:
    """The runner ``fault_clear.apply`` writes through: it reads the rows and adds comments."""

    def __init__(self, rows):
        self.rows = rows
        self.added = []

    def __call__(self, argv):
        assert argv[:2] == ['comments', 'add'], argv
        row = next(item for item in self.rows if item['id'] == argv[2])
        cid = 'clear-%d' % (len(self.added) + 1)
        row['comments'].append(tl.comment(cid, argv[3], COORD, '2026-09-15T11:00:00Z'))
        self.added.append(cid)
        return json.dumps({'id': cid})

    def apply(self, project, payload, actor=COORD):
        with mock.patch.object(fault_clear, '_rows', return_value=self.rows):
            return fault_clear.apply(payload, actor, self, project)


def clear_request(task='t1', target='f1', reason='fixed the slot'):
    return {'schema_version': 1, 'task': task, 'target': target, 'reason': reason}


class BlockingReadTimeout(Exception):
    pass


def bounded(call):
    previous = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(BlockingReadTimeout()))
    signal.setitimer(signal.ITIMER_REAL, 1)
    try:
        return call()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def make_fifo(case, path):
    if not hasattr(os, 'mkfifo'):
        case.skipTest('mkfifo is unavailable')
    os.mkfifo(path)


class RecordTests(unittest.TestCase):
    def problems(self, **changes):
        payload = {'schema_version': 1, 'task': 't1', 'state': 'needs-investigation', 'reason': 'x'}
        payload.update(changes)
        return lane_state.problems(payload, 't1')

    def test_a_raise_needs_a_one_line_reason_of_at_most_300_characters(self):
        self.assertEqual([], self.problems())
        self.assertEqual([], self.problems(reason='r' * 300, about_task='other.1'))
        for bad in ('r' * 301, '', '   ', 'two\nlines', 'tab\there', 'bell\x07', 'bidi \u202e text',
                    'line sep\u2028', 7):
            self.assertTrue(self.problems(reason=bad), repr(bad))
        payload = {'schema_version': 1, 'task': 't1', 'state': 'needs-investigation'}
        self.assertTrue(lane_state.problems(payload, 't1'))

    def test_the_fault_fields_belong_to_the_fault_state_only(self):
        for state, extra in (('working', {}), ('waiting-on-coordinator', {'waiting_on': 'coordinator'})):
            self.assertTrue(lane_state.problems(
                dict({'schema_version': 1, 'task': 't1', 'state': state, 'reason': 'x'}, **extra), 't1'))
            self.assertTrue(lane_state.problems(
                dict({'schema_version': 1, 'task': 't1', 'state': state, 'about_task': 't2'}, **extra), 't1'))
        self.assertTrue(self.problems(waiting_on='coordinator'))
        self.assertTrue(self.problems(about_task='not a task id'))

    def test_the_display_form_is_escaped_and_clipped(self):
        shown = lane_state.display_reason('a\nb\u202ec\x00\r' + 'z' * 400)
        self.assertEqual(300, len(shown))
        self.assertTrue(shown.isprintable() and shown.isascii())
        self.assertTrue(shown.startswith('a\\nb\\u202ec\\x00\\r'))
        self.assertEqual(300, len(lane_state.display_reason('\n' * 301)))

    def test_an_older_kit_drops_the_record_and_keeps_every_other_reading(self):
        # The base kit's lane_state.py, run as it was before this change.
        source = subprocess.run(['git', '-C', str(KIT), 'show', '306529e:lane_state.py'],
                                capture_output=True, text=True)
        if source.returncode != 0:
            self.skipTest('the base commit is not in this checkout')
        with tempfile.TemporaryDirectory() as directory:
            old_path = Path(directory) / 'old_lane_state.py'
            old_path.write_text(source.stdout, encoding='utf-8')
            spec = importlib.util.spec_from_file_location('old_lane_state', old_path)
            old = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(old)
        row = tl.task('t1', A)
        raise_fault(row)
        self.assertIsNone(old.newest(row['comments'], 't1'))
        working = tl.task('t2', A)
        tl.add_checkpoint(working, 'cp2', RAISED)
        tl.add_lane_state(working, 'ls2', 'cp2', RAISED)
        self.assertEqual('working', old.newest(working['comments'], 't2')[1]['state'])
        # The clear is an ordinary comment to a kit that has never heard of its prefix.
        self.assertFalse(old.is_record(fault_clear.body(fault_clear.payload_check(
            dict(clear_request(), lane=A, actor=COORD)))))

    def test_the_raw_comment_path_can_never_write_a_clear_or_a_raise(self):
        clear = fault_clear.body(fault_clear.payload_check(dict(clear_request(), lane=A, actor=COORD)))
        self.assertIsNotNone(reserved_comments.reserved_match(clear))
        with self.assertRaisesRegex(ValueError, 'reserved lane fault clear write'):
            reserved_comments.check_raw_request(['comments', 'add', 't1', clear], {}, actor=A)
        raised = lane_state.PREFIX + canonical_bytes({'state': 'needs-investigation'}).decode()
        with self.assertRaisesRegex(ValueError, 'reserved lane state write'):
            reserved_comments.check_raw_request(['comments', 'add', 't1', raised], {}, actor=A)


class BoardTests(unittest.TestCase):
    def test_a_raised_fault_is_on_the_lane_row_with_reason_age_and_task(self):
        row = tl.task('t1', A)
        raise_fault(row, about_task='t9')
        lane_row = next(item for item in tl.board([row])['items'] if item['actor'] == A)
        fault = lane_row['needs_investigation']
        self.assertEqual({'record': 'f1', 'task': 't1', 'about_task': 't9',
                          'reason': 'the merge slot is stuck', 'since': RAISED, 'age_seconds': 7200,
                          'earlier': [], 'earlier_omitted': 0}, fault)
        self.assertEqual([A], [item['actor'] for item in tl.board([row])['items']])
        self.assertEqual(1, tl.board([row])['flags']['needs_investigation'])

    def waiting_row(self):
        row = tl.task('t1', A)
        tl.add_checkpoint(row, 'cp1', '2026-09-15T08:00:00Z')
        tl.add_lane_state(row, 'ls1', 'cp1', '2026-09-15T08:00:00Z', state='waiting-on-coordinator',
                          waiting_on='coordinator', expect_by='2026-09-15T09:00:00+00:00')
        return row

    def test_it_changes_neither_the_move_nor_the_other_flags(self):
        plain = self.waiting_row()
        flagged = json.loads(json.dumps(plain))
        raise_fault(flagged, cid='f1', stamp='2026-09-15T10:00:00Z')
        before = tl.entry_of(tl.board([plain]), 't1')[1]
        after = tl.entry_of(tl.board([flagged]), 't1')[1]
        self.assertEqual(['waiting_too_long', 'expect_by_passed'], [f for f in before['flags'] if f != 'your_move_idle'][:2])
        for name in ('review_state', 'work_state', 'whose_move', 'next_actor', 'next', 'flags', 'state',
                     'waiting_on', 'expect_by', 'declared', 'inferred', 'checkpoint', 'state_since'):
            self.assertEqual(before[name], after[name], name)
        # The held task still reads as waiting on the coordinator: the checkpoint-to-coordinator
        # trigger of the wait is the declared state's, not the fault's.
        view = waiting.coordinator_view([], lanes.read(
            [flagged], A, tl.registry(('lane-a', A)), [], now=NOW, journal=None))
        self.assertEqual(['t1'], [item['task'] for item in view['triggers']['checkpoint-to-coordinator']])

    def test_a_clear_gives_back_the_previous_declared_state_and_the_trigger(self):
        row = self.waiting_row()
        raise_fault(row, cid='f1', stamp='2026-09-15T10:00:00Z')
        with tempfile.TemporaryDirectory() as directory:
            FakeNative([row]).apply(Path(directory), clear_request())
            result = lanes.read([row], A, tl.registry(('lane-a', A)), [], now=NOW, journal=Path(directory))
            entry = tl.entry_of(result, 't1')[1]
            self.assertEqual(('waiting-on-coordinator', 'coordinator'), (entry['state'], entry['waiting_on']))
            self.assertIsNone(tl.entry_of(result, 't1')[0]['needs_investigation'])
            view = waiting.coordinator_view([], result)
            self.assertEqual(['t1'], [i['task'] for i in view['triggers']['checkpoint-to-coordinator']])
            self.assertEqual([], view['triggers']['needs-investigation'])

    def test_a_later_state_record_never_clears_it_but_a_new_raise_replaces_it(self):
        row = tl.task('t1', A)
        raise_fault(row)
        tl.add_checkpoint(row, 'cp2', '2026-09-15T10:30:00Z', previous='cp-f1')
        tl.add_lane_state(row, 'ls2', 'cp2', '2026-09-15T10:30:00Z', state='working')
        fault = tl.board([row])['items'][0]['needs_investigation']
        self.assertEqual('f1', fault['record'])
        raise_fault(row, cid='f2', stamp='2026-09-15T11:00:00Z', reason='a different fault')
        fault = next(i for i in tl.board([row])['items'] if i['actor'] == A)['needs_investigation']
        self.assertEqual(('f2', 'a different fault', 3600), (fault['record'], fault['reason'], fault['age_seconds']))

    def test_it_stays_shown_when_the_task_is_closed_and_the_lane_holds_nothing(self):
        row = tl.task('t1', A, status='closed')
        raise_fault(row)
        lane_row = next(item for item in tl.board([row])['items'] if item['actor'] == A)
        self.assertEqual([], lane_row['held'])
        self.assertEqual('f1', lane_row['needs_investigation']['record'])
        self.assertEqual(A, tl.board([row])['items'][0]['actor'])

    def test_it_is_shown_escaped_and_never_for_a_record_the_checkpoint_does_not_back(self):
        row = tl.task('t1', A)
        payload = {'schema_version': 1, 'task': 't1', 'checkpoint': 'missing', 'state': 'needs-investigation',
                   'reason': 'orphan'}
        row['comments'].append(tl.comment('x', lane_state.PREFIX + canonical_bytes(payload).decode(), A, RAISED))
        self.assertIsNone(tl.board([row])['items'][0]['needs_investigation'])
        forged = tl.task('t2', A)
        tl.add_checkpoint(forged, 'cp', RAISED)
        text = 'a\nb\u202ec'
        payload = {'schema_version': 1, 'task': 't2', 'checkpoint': 'cp', 'state': 'needs-investigation',
                   'reason': text}
        forged['comments'].append(tl.comment('y', lane_state.PREFIX + canonical_bytes(payload).decode(), A, RAISED))
        # A record the writer would have refused is not usable at all, never shown raw.
        self.assertIsNone(next(i for i in tl.board([forged])['items'] if i['actor'] == A)['needs_investigation'])

    def test_the_contributor_route_sees_only_its_own_fault(self):
        a_row, b_row = tl.task('t1', A), tl.task('t2', B)
        raise_fault(a_row)
        raise_fault(b_row, cid='f2', reason='b fault')
        result = tl.board([a_row, b_row], A, None, scope='own')
        self.assertEqual([A], [item['actor'] for item in result['items']])
        self.assertNotIn('b fault', json.dumps(result))

    def test_the_cli_read_shows_it_from_the_one_export_and_the_host_journal(self):
        row = tl.task('t1', A)
        raise_fault(row)
        lines = '\n'.join(json.dumps(item) for item in [row])
        with mock.patch.object(tl.sessions, 'read_registry', return_value=tl.registry(('lane-a', A))):
            result = work.execute(Path('.'), A, 'work', ['lanes'], {}, lambda argv: lines, operators=[A])
        self.assertEqual('f1', result['items'][0]['needs_investigation']['record'])


class ClearTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name)
        self.row = tl.task('t1', A)
        raise_fault(self.row)
        self.native = FakeNative([self.row])

    def board_fault(self):
        result = lanes.read(self.native.rows, A, tl.registry(('lane-a', A)), [], now=NOW, journal=self.project)
        return next(item for item in result['items'] if item['actor'] == A)['needs_investigation']

    def test_regular_file_host_journal_missing_damaged_and_valid_classifications(self):
        self.assertEqual(({}, []), fault_clear.host_journal(self.project))
        answer = self.native.apply(self.project, clear_request())
        path = next((self.project / fault_clear.JOURNAL_DIR).glob('*.json'))
        self.assertEqual([], fault_clear.host_journal(self.project)[1])
        self.assertIn(answer['comment_id'], fault_clear.host_journal(self.project)[0])
        path.write_text('{bad', encoding='utf-8')
        self.assertEqual(({}, [fault_clear.JOURNAL_DIR + '/' + path.name]),
                         fault_clear.host_journal(self.project))

    def test_fifo_host_journal_never_blocks_and_is_invalid(self):
        answer = self.native.apply(self.project, clear_request())
        path = next((self.project / fault_clear.JOURNAL_DIR).glob('*.json'))
        path.unlink()
        make_fifo(self, path)
        self.assertEqual(({}, [fault_clear.JOURNAL_DIR + '/' + path.name]),
                         bounded(lambda: fault_clear.host_journal(self.project)))

    def test_a_coordinators_clear_removes_the_flag_and_is_audited_who_and_when(self):
        answer = self.native.apply(self.project, clear_request())
        self.assertEqual(('t1', 'f1', A, False), (answer['task'], answer['target'], answer['lane'], answer['reconciled']))
        self.assertIsNone(self.board_fault())
        entry = fault_clear.host_journal(self.project)[0][answer['comment_id']]
        self.assertEqual(COORD, entry['actor'])
        self.assertEqual(COORD, entry['payload']['actor'])
        self.assertEqual('fixed the slot', entry['payload']['reason'])
        comment = next(c for c in self.row['comments'] if c['id'] == answer['comment_id'])
        self.assertEqual('2026-09-15T11:00:00Z', comment['created_at'])
        self.assertEqual(['t1'], [self.row['id']])
        again = self.native.apply(self.project, clear_request())
        self.assertTrue(again['reconciled'])
        self.assertEqual(1, len(self.native.added))

    def test_a_lane_may_raise_it_again_after_a_clear(self):
        self.native.apply(self.project, clear_request())
        raise_fault(self.row, cid='f2', stamp='2026-09-15T11:30:00Z', reason='back again')
        self.assertEqual('back again', self.board_fault()['reason'])

    def test_a_clear_without_its_host_journal_entry_is_inert(self):
        answer = self.native.apply(self.project, clear_request())
        for path in (self.project / fault_clear.JOURNAL_DIR).glob('*.json'):
            path.unlink()
        self.assertEqual('f1', self.board_fault()['record'])
        self.assertEqual({}, fault_clear.host_journal(self.project)[0])
        self.assertTrue(answer['comment_id'])
        no_journal = lanes.read(self.native.rows, A, tl.registry(('lane-a', A)), [], now=NOW, journal=None)
        self.assertEqual('f1', next(i for i in no_journal['items'] if i['actor'] == A)['needs_investigation']['record'])

    def test_a_forged_clear_comment_or_a_mismatched_entry_is_inert(self):
        payload = fault_clear.payload_check(dict(clear_request(), lane=A, actor=A))
        self.row['comments'].append(tl.comment('forged', fault_clear.body(payload), A, '2026-09-15T11:00:00Z'))
        self.assertEqual('f1', self.board_fault()['record'])
        other = fault_clear.payload_check(dict(clear_request(reason='other'), lane=A, actor=COORD))
        fault_clear.publish(self.project, fault_clear.journal_entry(other, 'forged', COORD, 0))
        self.assertEqual('f1', self.board_fault()['record'])

    def test_a_valid_entry_behind_a_symlinked_journal_directory_clears_nothing(self):
        self.native.apply(self.project, clear_request())
        self.assertIsNone(self.board_fault())
        journal = self.project / fault_clear.JOURNAL_DIR
        moved = self.project / 'elsewhere'
        journal.rename(moved)
        journal.symlink_to(moved)
        self.assertEqual(1, len(list(moved.glob('*.json'))))
        self.assertEqual('f1', self.board_fault()['record'])
        self.assertIsNone(fault_clear._directory(self.project))

    def test_a_symlinked_or_damaged_journal_clears_nothing(self):
        self.native.apply(self.project, clear_request())
        journal = self.project / fault_clear.JOURNAL_DIR
        entry = next(journal.glob('*.json'))
        entry.write_text('{"broken"', encoding='utf-8')
        self.assertEqual('f1', self.board_fault()['record'])
        self.assertEqual(1, len(fault_clear.host_journal(self.project)[1]))
        entry.unlink()
        moved = self.project / 'elsewhere'
        moved.mkdir()
        journal.rmdir()
        journal.symlink_to(moved)
        self.assertEqual('f1', self.board_fault()['record'])
        with self.assertRaises(ValueError):
            fault_clear.open_journal(self.project)

    def test_a_clear_names_the_current_raise_and_a_task_the_lane_wrote_it_on(self):
        raise_fault(self.row, cid='f2', stamp='2026-09-15T10:30:00Z', reason='newer')
        with self.assertRaisesRegex(ValueError, 'newer needs-investigation record.*f2.*Nothing was written'):
            self.native.apply(self.project, clear_request(target='f1'))
        for bad in (clear_request(target='nope'), clear_request(task='elsewhere')):
            with self.assertRaisesRegex(ValueError, 'Nothing was written'):
                self.native.apply(self.project, bad)
        self.assertEqual([], self.native.added)
        self.native.apply(self.project, clear_request(target='f2'))
        self.assertIsNone(self.board_fault())

    def test_the_request_carries_only_what_the_caller_may_say(self):
        for bad in (dict(clear_request(), lane=B), dict(clear_request(), actor=A),
                    clear_request(reason=''), clear_request(reason='r' * 301),
                    clear_request(reason='two\nlines'), dict(clear_request(), schema_version=2)):
            with self.assertRaisesRegex(ValueError, 'Nothing was written'):
                self.native.apply(self.project, bad)
        self.assertEqual([], self.native.added)

    def test_only_a_configured_operator_may_run_the_command(self):
        if endpoint is None:
            self.skipTest('endpoint.py needs fcntl')
        self.assertIn('fault-clear', endpoint.COORDINATOR_COMMANDS)
        # The lane that raised the fault, and any other member, is not on the operator list.
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(endpoint, 'configured_operators', return_value=[COORD]):
                endpoint.coordinator_refusal({'actor': COORD, 'args': ['fault-clear']}, Path(root), 'person:coord')
                for member in (A, B, 'someone-else'):
                    with self.assertRaises(ValueError):
                        endpoint.coordinator_refusal({'actor': member, 'args': ['fault-clear']},
                                                     Path(root), 'person:member')
                with self.assertRaises(ValueError):
                    endpoint.coordinator_refusal({'actor': COORD, 'args': ['fault-clear']}, Path(root), None)


class JournalGateTests(unittest.TestCase):
    """The clear's journal gate with other entries present, and each binding the entry carries."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name)
        self.row_a, self.row_b = tl.task('t1', A), tl.task('t2', B)
        raise_fault(self.row_a, cid='fa', reason='lane a fault')
        raise_fault(self.row_b, cid='fb', reason='lane b fault')
        self.native = FakeNative([self.row_a, self.row_b])

    def faults(self):
        return fault_clear.open_faults(self.native.rows, fault_clear.host_journal(self.project)[0])

    def clear_payload(self, task, target, lane):
        return fault_clear.payload_check(
            {'schema_version': 1, 'task': task, 'target': target, 'lane': lane, 'reason': 'ok', 'actor': COORD})

    def test_an_unjournaled_clear_for_one_lane_clears_nothing_while_another_lane_has_an_entry(self):
        self.native.apply(self.project, clear_request(task='t1', target='fa'))
        self.assertEqual({B}, set(self.faults()))
        forged = self.clear_payload('t2', 'fb', B)
        self.row_b['comments'].append(tl.comment('forged-b', fault_clear.body(forged), COORD, '2026-09-15T11:00:00Z'))
        self.assertEqual({B}, set(self.faults()))
        self.assertEqual('fb', self.faults()[B]['record'])

    def test_the_entry_must_match_the_comment_payload_and_the_task_the_comment_is_on(self):
        payload = self.clear_payload('t1', 'fa', A)
        # The same comment, journaled, but sitting on another task: not honoured.
        self.row_b['comments'].append(tl.comment('moved', fault_clear.body(payload), COORD, '2026-09-15T11:00:00Z'))
        fault_clear.publish(self.project, fault_clear.journal_entry(payload, 'moved', COORD, 0))
        self.assertEqual({A, B}, set(self.faults()))
        # On its own task it is honoured, so the refusal above was the task binding.
        self.row_a['comments'].append(tl.comment('home', fault_clear.body(payload), COORD, '2026-09-15T11:00:00Z'))
        fault_clear.publish(self.project, fault_clear.journal_entry(payload, 'home', COORD, 0))
        self.assertEqual({B}, set(self.faults()))

    def test_the_entry_file_name_is_bound_to_the_payload_hash(self):
        payload = self.clear_payload('t1', 'fa', A)
        entry = fault_clear.journal_entry(payload, 'home', COORD, 0)
        self.assertIs(entry, fault_clear.validate_entry(entry, entry['sha256'] + '.json'))
        with self.assertRaisesRegex(ValueError, 'hash or path mismatch'):
            fault_clear.validate_entry(entry, '0' * 64 + '.json')
        self.row_a['comments'].append(tl.comment('home', fault_clear.body(payload), COORD, '2026-09-15T11:00:00Z'))
        directory = fault_clear.open_journal(self.project)
        (directory / ('0' * 64 + '.json')).write_text(json.dumps(entry), encoding='utf-8')
        self.assertEqual({}, fault_clear.host_journal(self.project)[0])
        self.assertEqual({A, B}, set(self.faults()))

    def test_the_lane_and_actor_of_the_payload_are_validated(self):
        payload = self.clear_payload('t1', 'fa', A)
        for field in ('lane', 'actor'):
            for bad in ('', None, 7, 'two words'):
                with self.assertRaises(ValueError, msg=(field, bad)):
                    fault_clear.payload_check(dict(payload, **{field: bad}))
            entry = fault_clear.journal_entry(payload, 'home', COORD, 0)
            with self.assertRaises(ValueError):
                fault_clear.validate_entry(dict(entry, payload=dict(payload, **{field: ''})))
        with self.assertRaises(ValueError):
            fault_clear.validate_entry(dict(fault_clear.journal_entry(payload, 'home', COORD, 0), actor=''))

    def test_a_symlinked_journal_directory_is_refused_before_anything_is_written(self):
        elsewhere = self.project / 'elsewhere'
        elsewhere.mkdir()
        (self.project / fault_clear.JOURNAL_DIR).symlink_to(elsewhere)
        with self.assertRaises(ValueError):
            self.native.apply(self.project, clear_request(task='t1', target='fa'))
        self.assertEqual([], self.native.added)
        self.assertEqual([], list(elsewhere.iterdir()))

    def test_older_open_raises_stay_visible_until_the_newest_is_cleared(self):
        raise_fault(self.row_a, cid='fa2', stamp='2026-09-15T10:30:00Z', reason='benign')
        shown = self.faults()[A]
        self.assertEqual(('fa2', 'benign'), (shown['record'], shown['reason']))
        self.assertEqual(['lane a fault'], [item['reason'] for item in shown['earlier']])
        self.native.apply(self.project, clear_request(task='t1', target='fa2'))
        self.assertNotIn(A, self.faults())
        raise_fault(self.row_a, cid='fa3', stamp='2026-09-15T11:30:00Z', reason='third')
        self.assertEqual([], self.faults()[A]['earlier'])

    def test_more_than_five_older_open_raises_report_how_many_are_not_shown(self):
        for number in range(2, 9):
            raise_fault(self.row_a, cid='fa%d' % number, stamp='2026-09-15T1%d:00:00Z' % number,
                        reason='raise %d' % number)
        shown = self.faults()[A]
        self.assertEqual('raise 8', shown['reason'])
        self.assertEqual(fault_clear.EARLIER_LIMIT, len(shown['earlier']))
        self.assertEqual(['raise %d' % n for n in range(4, 8)], [item['reason'] for item in shown['earlier']][1:])
        self.assertEqual(8 - 1 - fault_clear.EARLIER_LIMIT, shown['earlier_omitted'])


class WaitTests(unittest.TestCase):
    def lanes_payload(self, *rows):
        return lanes.read(list(rows), A, tl.registry(('lane-a', A), ('lane-b', B)), [], now=NOW, journal=None)

    def test_the_coordinator_wait_has_a_needs_investigation_trigger(self):
        self.assertEqual('needs-investigation', waiting.COORDINATOR_TRIGGERS[-1])
        row = tl.task('t1', A)
        raise_fault(row, about_task='t9')
        view = waiting.coordinator_view([], self.lanes_payload(row))
        rows = view['triggers']['needs-investigation']
        self.assertEqual([{'lane': 'lane-a', 'actor': A, 'task': 't9', 'state': 'needs-investigation',
                           'raised_on': 't1', 'reason': 'the merge slot is stuck', 'record': 'f1',
                           'earlier': [], 'earlier_omitted': 0, 'since': RAISED}], rows)
        self.assertIn({'trigger': 'needs-investigation', **rows[0]}, view['items'])
        # The raise's own task is shown beside the task it is about.
        self.assertEqual(('t9', 't1'), (rows[0]['task'], rows[0]['raised_on']))
        raise_fault(row, cid='f2', stamp='2026-09-15T11:00:00Z', reason='benign')
        again = waiting.coordinator_view([], self.lanes_payload(row))['triggers']['needs-investigation']
        self.assertEqual(('benign', ['the merge slot is stuck']), (again[0]['reason'], again[0]['earlier']))

    def test_the_trigger_fires_for_a_fault_and_leaves_the_other_triggers_alone(self):
        quiet = tl.task('t1', B)
        row = tl.task('t2', A)
        tl.add_checkpoint(row, 'cp-f1', RAISED)
        before = waiting.coordinator_view([], self.lanes_payload(quiet, row))
        self.assertEqual([], before['triggers']['needs-investigation'])
        tl.add_lane_state(row, 'f1', 'cp-f1', RAISED, state='needs-investigation', reason='the merge slot is stuck')
        after = waiting.coordinator_view([], self.lanes_payload(quiet, row))
        for name in waiting.COORDINATOR_TRIGGERS[:-1]:
            self.assertEqual(before['triggers'][name], after['triggers'][name], name)
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])

    def test_the_fingerprint_moves_with_a_new_reason_and_not_with_age(self):
        row = tl.task('t1', A)
        raise_fault(row)
        first = waiting.coordinator_view([], self.lanes_payload(row))['fingerprint']
        later = lanes.read([row], A, tl.registry(('lane-a', A)), [], journal=None,
                           now=datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(first, waiting.coordinator_view([], later)['fingerprint'])
        raise_fault(row, cid='f2', stamp='2026-09-15T11:00:00Z', reason='another')
        self.assertNotEqual(first, waiting.coordinator_view([], self.lanes_payload(row))['fingerprint'])

    def test_an_empty_fault_group_stays_out_of_the_fingerprint(self):
        view = waiting.coordinator_view([], self.lanes_payload())
        self.assertEqual([], view['triggers']['needs-investigation'])
        old = {name: [] for name in waiting.COORDINATOR_TRIGGERS if name not in waiting.OPTIONAL_TRIGGERS}
        self.assertEqual(waiting.digest(old), view['fingerprint'])
        import client
        self.assertEqual(view['fingerprint'],
                         client._InlineWait.coordinator_view([], self.lanes_payload())['fingerprint'])

    def test_the_fingerprint_is_the_raise_not_its_prose_and_many_raises_stay_in_parity(self):
        import client
        row = tl.task('t1', A)
        for number in range(8):
            raise_fault(row, cid='f%d' % number, stamp='2026-09-15T1%d:00:00Z' % number, reason='r%d' % number)
        payload = self.lanes_payload(row)
        view = waiting.coordinator_view([], payload)
        shown = view['triggers']['needs-investigation'][0]
        self.assertEqual((5, 2), (len(shown['earlier']), shown['earlier_omitted']))
        self.assertEqual(view, client._InlineWait.coordinator_view([], payload))
        edited = json.loads(json.dumps(payload))
        edited['items'][0]['needs_investigation']['reason'] = 'rewritten'
        edited['items'][0]['needs_investigation']['earlier'] = []
        self.assertEqual(view['fingerprint'], waiting.coordinator_view([], edited)['fingerprint'])

    def test_the_standalone_client_reads_the_same_way(self):
        import client
        row = tl.task('t1', A)
        raise_fault(row)
        payload = self.lanes_payload(row)
        self.assertEqual(waiting.coordinator_view([], payload),
                         client._InlineWait.coordinator_view([], payload))


class AdminCommandTests(unittest.TestCase):
    """``admin.py fault-clear``: the operator check, taken again under the lock, then the same apply."""

    def setUp(self):
        import io
        import contextlib
        import types
        self.io, self.contextlib = io, contextlib
        try:
            import admin
        except ImportError:
            self.skipTest('admin.py needs a POSIX interpreter')
        self.admin = admin
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.project = self.root / 'projects' / 'pp'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        self.row = tl.task('t1', A)
        raise_fault(self.row)
        self.native = FakeNative([self.row])
        fake = types.SimpleNamespace(flock=lambda *a, **k: None, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, LOCK_SH=1)
        patcher = mock.patch.dict(sys.modules, {'fcntl': fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_bd(self, root, project, argv):
        assert argv[:2] == ['--actor', argv[1]], argv
        return self.native(argv[2:])

    def command(self, payload, actor=COORD, operators=None):
        path = self.root / 'payload.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        argv = ['admin.py', '--root', str(self.root), 'fault-clear', '--project', 'pp',
                '--actor', actor, '--file', str(path)]
        out = self.io.StringIO()
        listed = frozenset([COORD]) if operators is None else operators
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(self.admin, 'root_path', return_value=self.root), \
                mock.patch.object(self.admin, 'project_dir', return_value=self.project), \
                mock.patch.object(self.admin, 'operators', side_effect=listed if callable(listed) else None,
                                  return_value=None if callable(listed) else listed), \
                mock.patch.object(self.admin, 'run_bd', side_effect=self.run_bd), \
                mock.patch.object(self.admin, 'deployment_config_lock', self.contextlib.nullcontext), \
                mock.patch.object(fault_clear, '_rows', return_value=self.native.rows), \
                self.contextlib.redirect_stdout(out):
            self.admin.main()
        return json.loads(out.getvalue())

    def refusal(self, *args, **kwargs):
        with self.assertRaises(ValueError) as refused:
            self.command(*args, **kwargs)
        return str(refused.exception)

    def journal_names(self):
        directory = self.project / fault_clear.JOURNAL_DIR
        return sorted(path.name for path in directory.glob('*.json')) if directory.is_dir() else []

    def test_an_operator_clears_the_fault_and_the_journal_names_who(self):
        answer = self.command(clear_request())
        self.assertEqual(('f1', A, False), (answer['target'], answer['lane'], answer['reconciled']))
        entry = fault_clear.host_journal(self.project)[0][answer['comment_id']]
        self.assertEqual(COORD, entry['actor'])
        self.assertEqual({}, fault_clear.open_faults(self.native.rows, fault_clear.host_journal(self.project)[0]))

    def test_a_non_operator_is_refused_before_the_lock_and_writes_nothing(self):
        for actor in (A, 'someone-else'):
            message = self.refusal(clear_request(), actor=actor)
            self.assertIn('is not a server-side configured operator', message)
        self.assertEqual([], self.native.added)
        self.assertEqual([], self.journal_names())
        self.assertFalse((self.project / '.coordination.lock').exists())

    def test_the_operator_list_is_read_again_inside_the_lock(self):
        calls = []

        def changing(root, **kwargs):
            calls.append(kwargs)
            return frozenset([COORD]) if len(calls) == 1 else frozenset([A])

        message = self.refusal(clear_request(), operators=changing)
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn('is not a server-side configured operator', message)
        self.assertEqual([], self.native.added)
        self.assertEqual([], self.journal_names())


class BackupTests(unittest.TestCase):
    """The journal travels with a backup and a restore (it lives outside the tracker)."""

    def setUp(self):
        import types
        try:
            import admin
        except ImportError:
            self.skipTest('admin.py needs a POSIX interpreter')
        self.admin = admin
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'projects' / 'source'
        self.destination = self.root / 'projects' / 'destination'
        self.source.mkdir(parents=True)
        self.destination.mkdir()
        (self.root / 'backups' / 'source').mkdir(parents=True)
        fake = types.SimpleNamespace(flock=lambda *a, **k: None, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, LOCK_SH=1)
        patcher = mock.patch.dict(sys.modules, {'fcntl': fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (('backup_lifecycle_void_family', {}), ('restore_lifecycle_void_family', False)):
            patched = mock.patch.object(admin, name, return_value=value)
            patched.start()
            self.addCleanup(patched.stop)

    def test_backup_and_restore_carry_the_journal_and_a_restored_clear_still_clears(self):
        row = tl.task('t1', A)
        raise_fault(row)
        native = FakeNative([row])
        native.apply(self.source, clear_request())
        name = next((self.source / fault_clear.JOURNAL_DIR).glob('*.json')).name
        with mock.patch.object(self.admin, 'run_bd', return_value='synced'):
            self.admin.backup_project(self.root, 'source')
        bundle = json.loads((self.root / 'backups' / 'source.coordination.json').read_text(encoding='utf-8'))
        self.assertIn(fault_clear.JOURNAL_DIR + '/' + name, bundle['files'])
        self.admin.restore_coordination(self.root, 'source', 'destination')
        self.assertTrue((self.destination / fault_clear.JOURNAL_DIR / name).is_file())
        entries = fault_clear.host_journal(self.destination)[0]
        self.assertEqual({}, fault_clear.open_faults([row], entries))
        (self.destination / fault_clear.JOURNAL_DIR / name).unlink()
        self.assertEqual({A}, set(fault_clear.open_faults([row], fault_clear.host_journal(self.destination)[0])))

    def test_a_damaged_entry_is_refused_by_backup_validation(self):
        payload = fault_clear.payload_check(dict(clear_request(), lane=A, actor=COORD))
        entry = fault_clear.journal_entry(payload, 'c1', COORD, 0)
        path = fault_clear.JOURNAL_DIR + '/' + entry['sha256'] + '.json'
        self.admin.validate_coordination_files({path: entry})
        with self.assertRaises(ValueError):
            self.admin.validate_coordination_files({path: dict(entry, sha256='0' * 64)})


if __name__ == '__main__':
    unittest.main()
