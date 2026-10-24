"""A lane's ``needs-investigation`` flag (kittrial-5bb.398): raise, show, wake, clear.

Pure-module tests over an in-memory export, a disposable project directory for the host
journal and a fake native runner. The HTTP lane board and the HTTP coordinator wait have their
own cases in ``test_web_lanes`` and ``test_coordinator_http_wait``.
"""
import importlib.util
import json
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
                          'reason': 'the merge slot is stuck', 'since': RAISED, 'age_seconds': 7200}, fault)
        self.assertEqual([A], [item['actor'] for item in tl.board([row])['items']])
        self.assertEqual(1, tl.board([row])['flags']['needs_investigation'])

    def test_it_changes_neither_the_move_nor_the_other_flags(self):
        plain = tl.task('t1', A)
        tl.add_checkpoint(plain, 'cp1', RAISED)
        flagged = json.loads(json.dumps(plain))
        raise_fault(flagged, cid='f1', stamp=RAISED, checkpoint='cp1')
        before = tl.entry_of(tl.board([plain]), 't1')[1]
        after = tl.entry_of(tl.board([flagged]), 't1')[1]
        for name in ('review_state', 'work_state', 'whose_move', 'next_actor', 'next', 'flags'):
            self.assertEqual(before[name], after[name], name)

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
                           'reason': 'the merge slot is stuck', 'record': 'f1'}], rows)
        self.assertIn({'trigger': 'needs-investigation', **rows[0]}, view['items'])

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

    def test_the_standalone_client_reads_the_same_way(self):
        import client
        row = tl.task('t1', A)
        raise_fault(row)
        payload = self.lanes_payload(row)
        self.assertEqual(waiting.coordinator_view([], payload),
                         client._InlineWait.coordinator_view([], payload))


if __name__ == '__main__':
    unittest.main()
