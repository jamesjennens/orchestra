"""The bounded ``work lanes`` read: rows, the three flags, idle lanes and route isolation.

kittrial-5bb.263; docs/LANE_BOARD_DESIGN.md sections 3 and 7. The read is one export plus
the session registry, so these tests drive it with an in-memory export and a registry
dict, and one test drives the whole ``work.execute`` path counting native calls.
"""
import json
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import briefing as b
import lane_state
import lanes
import sessions
import work
from requirements import canonical_bytes

try:
    import endpoint
except ImportError:            # endpoint.py imports fcntl; POSIX only
    endpoint = None

PROJECT = 'trial'
NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


def lane(seed):
    """A session actor of the shape the registry validates."""
    return 'session-' + str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


A, B, C = lane('lane-a'), lane('lane-b'), lane('lane-c')


def registry(*entries):
    records = {}
    for name, actor in entries:
        rid = str(uuid.uuid5(uuid.NAMESPACE_URL, name + '|' + actor))
        records[rid] = {'request_id': rid, 'actor': actor, 'name': name,
                        'created_at': '2026-09-01T00:00:00+00:00'}
    return {'schema_version': 1, 'records': records}


def comment(cid, text, author, stamp):
    return dict(id=cid, text=text, author=author, created_at=stamp)


def task(task_id, assignee, comments=None, status='in_progress', updated_at='2026-09-15T09:00:00Z'):
    return dict(id=task_id, title='Task ' + task_id, issue_type='task', status=status, assignee=assignee,
                description='Intent', acceptance_criteria='Acceptance', labels=[], priority=2,
                created_by=assignee, created_at='2026-09-01T00:00:00Z', updated_at=updated_at,
                dependencies=[], comments=list(comments or []))


def add_checkpoint(row, cid, stamp, previous=None, **changes):
    """Append a VALID checkpoint comment to a row and return its payload."""
    snap = b.snapshot([row], PROJECT, row['id'])
    payload = dict(schema_version=1, task=row['id'], previous=previous,
                   activity_cursor=b.activity_cursor(snap), source_commit='', branch='',
                   intent='intent', acceptance='acceptance', summary='summary',
                   next_action='next', open_items=[], resolved=[])
    payload.update(changes)
    row['comments'].append(comment(cid, b.PREFIX + canonical_bytes(payload).decode(), row['assignee'], stamp))
    return payload


def add_lane_state(row, cid, checkpoint_id, stamp, **changes):
    payload = dict(schema_version=1, task=row['id'], checkpoint=checkpoint_id, state='working')
    payload.update(changes)
    row['comments'].append(comment(cid, lane_state.PREFIX + canonical_bytes(payload).decode(),
                                   row['assignee'], stamp))
    return payload


def board(rows, actor=A, registry_data=None, *args, **options):
    options.setdefault('now', NOW)
    options.setdefault('journal', None)
    if registry_data is None:
        registry_data = registry(('lane-a', A), ('lane-b', B), ('lane-c', C))
    return lanes.read(rows, actor, registry_data, list(args), **options)


def entry_of(result, task_id):
    for row in result['items']:
        for entry in row['held']:
            if entry['task'] == task_id:
                return row, entry
    raise AssertionError('task %s is not on the board: %r' % (task_id, result['items']))


class RowTests(unittest.TestCase):
    def test_one_row_per_lane_keyed_by_the_registered_name(self):
        row = task('t1', A)
        result = board([row])
        row_a = next(item for item in result['items'] if item['actor'] == A)
        self.assertEqual(row_a['lane'], 'lane-a')
        self.assertFalse(row_a['idle'])
        self.assertEqual([entry['task'] for entry in row_a['held']], ['t1'])

    def test_a_task_with_no_lane_reads_as_its_assignee_actor(self):
        stranger = 'session-' + str(uuid.uuid5(uuid.NAMESPACE_URL, 'not-registered'))
        result = board([task('t1', stranger)])
        self.assertEqual([item['actor'] for item in result['items']], [stranger])
        self.assertEqual(result['items'][0]['lane'], stranger)

    def test_a_holding_row_carries_the_task_facts_and_the_declared_state(self):
        row = task('t1', A, [comment('c0', 'hello', A, '2026-09-15T09:00:00Z')])
        add_checkpoint(row, 'cp1', '2026-09-15T09:30:00Z', next_action='wait for the coordinator')
        add_lane_state(row, 'ls1', 'cp1', '2026-09-15T09:30:00Z', state='waiting-on-coordinator',
                       waiting_on='coordinator', expect_by='2026-09-20T00:00:00+00:00')
        _lane_row, entry = entry_of(board([row]), 't1')
        self.assertEqual(entry['review_state'], 'none')
        self.assertEqual(entry['work_state'], 'none')
        self.assertEqual(entry['state'], 'waiting-on-coordinator')
        self.assertEqual(entry['waiting_on'], 'coordinator')
        self.assertEqual(entry['expect_by'], '2026-09-20T00:00:00+00:00')
        self.assertEqual(entry['checkpoint'], 'cp1')
        self.assertTrue(entry['declared'])
        self.assertFalse(entry['inferred'])
        self.assertEqual(entry['state_age_seconds'], 9000)
        self.assertEqual(entry['held_age_seconds'], 10800)

    def test_the_board_ages_a_declared_state_from_its_checkpoint_not_its_record(self):
        # The record is written a little AFTER its checkpoint (the same act, two comments);
        # the board ages the wait from the checkpoint, never from the record.
        row = task('t1', A)
        add_checkpoint(row, 'cp1', '2026-09-15T09:30:00Z')
        add_lane_state(row, 'ls1', 'cp1', '2026-09-15T11:00:00Z', state='waiting-on-coordinator',
                       waiting_on='coordinator')
        _lane_row, entry = entry_of(board([row]), 't1')
        self.assertEqual(entry['checkpoint'], 'cp1')
        self.assertEqual(entry['state_since'], '2026-09-15T09:30:00Z')
        self.assertEqual(entry['state_age_seconds'], 9000)
        self.assertIn('waiting_too_long', entry['flags'])

    def test_whose_move_and_next_actor_come_from_the_one_decision(self):
        # A row injected with a review projection, compared against the same function the
        # work queue uses: the board never re-derives the move.
        row = task('t1', A)
        review = {'review_state': 'changes-requested',
                  'contribution': {'comment_id': 'c2'},
                  'pending_requests': [{'request': 'r1', 'contribution': 'c1'}]}
        entry = lanes.held_task(row, 't1', A, NOW, 900, review=review)
        expected = work.move_decision('changes-requested', 't1', True, pending_items=1, redelivered=False,
                                      assignee=A, closed=False, claimable=False)
        self.assertEqual(entry['whose_move'], expected[0])
        self.assertEqual(entry['next_actor'], expected[2])
        self.assertEqual(entry['whose_move'], 'you')
        self.assertEqual(entry['next_actor'], 'assignee')

    def test_a_lane_lane_age_is_the_newest_write_anywhere(self):
        first = task('t1', A, [comment('c0', 'old', A, '2026-09-15T08:00:00Z')])
        second = task('t2', B)
        result = board([first, second])
        row_a = next(item for item in result['items'] if item['actor'] == A)
        self.assertEqual(row_a['last_write'], '2026-09-15T08:00:00Z')
        self.assertEqual(row_a['last_write_age_seconds'], 14400)


class FlagTests(unittest.TestCase):
    def waiting_row(self, stamp='2026-09-15T11:00:00Z', **changes):
        row = task('t1', A, [comment('c0', 'hello', A, '2026-09-15T08:00:00Z')])
        add_checkpoint(row, 'cp1', stamp, next_action='wait')
        add_lane_state(row, 'ls1', 'cp1', stamp, state='waiting-on-coordinator', waiting_on='coordinator',
                       **changes)
        return row

    def test_waiting_too_long_only_when_the_checkpoint_is_older_than_the_threshold(self):
        fresh = self.waiting_row('2026-09-15T11:55:00Z')
        _row, entry = entry_of(board([fresh]), 't1')
        self.assertNotIn('waiting_too_long', entry['flags'])
        old = self.waiting_row('2026-09-15T10:00:00Z')
        lane_row, entry = entry_of(board([old]), 't1')
        self.assertIn('waiting_too_long', entry['flags'])
        self.assertTrue(lane_row['waiting_too_long'])
        self.assertEqual(lane_row['flag_tasks']['waiting_too_long'], ['t1'])

    def test_the_threshold_is_an_argument_and_a_flag_is_never_silenced_by_age(self):
        row = self.waiting_row('2026-09-15T10:00:00Z')
        # Two hours old: above the 15-minute default, below a two-hour argument.
        _row, entry = entry_of(board([row], A, None, '--idle-minutes', '120'), 't1')
        self.assertNotIn('waiting_too_long', entry['flags'])
        _row, entry = entry_of(board([row], A, None, '--idle-minutes', '119'), 't1')
        self.assertIn('waiting_too_long', entry['flags'])
        with self.assertRaisesRegex(ValueError, '--idle-minutes'):
            board([row], A, None, '--idle-minutes', '0')

    def test_expect_by_passed_is_independent_of_the_threshold(self):
        row = self.waiting_row('2026-09-15T11:59:00Z', expect_by='2026-09-15T11:00:00+00:00')
        lane_row, entry = entry_of(board([row], A, None, '--idle-minutes', '1440'), 't1')
        self.assertIn('expect_by_passed', entry['flags'])
        self.assertNotIn('waiting_too_long', entry['flags'])
        self.assertTrue(lane_row['expect_by_passed'])
        future = self.waiting_row('2026-09-15T11:59:00Z', expect_by='2026-09-15T13:00:00+00:00')
        _row, entry = entry_of(board([future]), 't1')
        self.assertNotIn('expect_by_passed', entry['flags'])

    def test_your_move_idle_when_the_holder_owes_the_move_and_wrote_nothing(self):
        # A claimed task with nothing delivered: whose_move is the holder's own.
        row = task('t1', A, [comment('c0', 'claimed it', A, '2026-09-15T08:00:00Z')])
        lane_row, entry = entry_of(board([row]), 't1')
        self.assertEqual(entry['whose_move'], 'you')
        self.assertIn('your_move_idle', entry['flags'])
        self.assertTrue(lane_row['your_move_idle'])
        # A write inside the threshold clears it.
        fresh = task('t2', A, [comment('c0', 'working', A, '2026-09-15T11:55:00Z')])
        lane_row, entry = entry_of(board([fresh]), 't2')
        self.assertNotIn('your_move_idle', entry['flags'])

    def test_an_idle_lane_raises_no_flag(self):
        # A lane that holds nothing owes no move: its row is idle and carries no flag.
        holder = task('t1', A)
        idle_write = task('t2', A, [comment('c9', 'a note', C, '2026-09-15T11:30:00Z')])
        result = board([holder, idle_write], A)
        row_c = next(item for item in result['items'] if item['actor'] == C)
        self.assertTrue(row_c['idle'])
        self.assertFalse(row_c['waiting_too_long'])
        self.assertFalse(row_c['expect_by_passed'])
        self.assertFalse(row_c['your_move_idle'])

    def test_the_summary_counts_each_flag(self):
        waiting = self.waiting_row('2026-09-15T10:00:00Z', expect_by='2026-09-15T10:30:00+00:00')
        owing = task('t2', B, [comment('c0', 'claimed', B, '2026-09-15T08:00:00Z')])
        result = board([waiting, owing])
        # Both holders owe their OWN move (no contribution has been delivered on either),
        # so your_move_idle counts both; the waiting and expect_by flags name lane A only.
        self.assertEqual(result['flags'], {'waiting_too_long': 1, 'expect_by_passed': 1, 'your_move_idle': 2})
        row_a = next(item for item in result['items'] if item['actor'] == A)
        self.assertEqual(row_a['flag_tasks']['expect_by_passed'], ['t1'])
        row_b = next(item for item in result['items'] if item['actor'] == B)
        self.assertEqual(row_b['flag_tasks'], {'your_move_idle': ['t2']})


class IdleLaneTests(unittest.TestCase):
    def test_a_registered_lane_that_holds_nothing_and_wrote_recently_is_idle(self):
        holder = task('t1', A)
        idle_write = task('t2', A, [comment('c9', 'a note', C, '2026-09-15T11:30:00Z')])
        result = board([holder, idle_write], A)
        row_c = next(item for item in result['items'] if item['actor'] == C)
        self.assertTrue(row_c['idle'])
        self.assertEqual(row_c['state'], 'idle')
        self.assertEqual(row_c['held'], [])
        self.assertEqual(row_c['last_write'], '2026-09-15T11:30:00Z')
        self.assertEqual(row_c['last_write_age_seconds'], 1800)

    def test_a_lane_that_wrote_longer_ago_than_the_window_is_not_listed(self):
        holder = task('t1', A)
        old_write = task('t2', A, [comment('c9', 'long ago', C, '2026-09-13T11:30:00Z')])
        result = board([holder, old_write], A)
        self.assertEqual([item['actor'] for item in result['items']], [A])

    def test_an_idle_lane_that_holds_a_task_is_a_holding_row_instead(self):
        result = board([task('t1', C, [comment('c0', 'x', C, '2026-09-15T11:30:00Z')])], A)
        row_c = next(item for item in result['items'] if item['actor'] == C)
        self.assertFalse(row_c['idle'])
        self.assertEqual(len(row_c['held']), 1)


class InferenceTests(unittest.TestCase):
    def test_the_prose_fallback_marks_the_row_inferred(self):
        row = task('t1', A, [comment('c0', 'hello', A, '2026-09-15T08:00:00Z')])
        add_checkpoint(row, 'cp1', '2026-09-15T09:00:00Z', next_action='Ask the coordinator to verify the plan')
        lane_row, entry = entry_of(board([row]), 't1')
        self.assertEqual(entry['state'], 'waiting-on-coordinator')
        self.assertEqual(entry['waiting_on'], 'coordinator')
        self.assertTrue(entry['inferred'])
        self.assertFalse(entry['declared'])
        self.assertEqual(entry['checkpoint'], 'cp1')
        self.assertIn('waiting_too_long', entry['flags'])

    def test_a_blocking_open_item_is_the_helpers_other_reason(self):
        row = task('t1', A, [comment('c0', 'hello', A, '2026-09-15T08:00:00Z')])
        add_checkpoint(row, 'cp1', '2026-09-15T09:00:00Z', next_action='continue',
                       open_items=[dict(id='i1', kind='blocker', text='blocked', source='x')])
        _lane_row, entry = entry_of(board([row]), 't1')
        self.assertEqual(entry['state'], 'waiting-on-coordinator')
        self.assertTrue(entry['inferred'])

    def test_a_declared_record_supersedes_the_prose(self):
        row = task('t1', A, [comment('c0', 'hello', A, '2026-09-15T08:00:00Z')])
        add_checkpoint(row, 'cp1', '2026-09-15T09:00:00Z', next_action='Ask the coordinator')
        add_lane_state(row, 'ls1', 'cp1', '2026-09-15T09:00:00Z', state='working')
        _lane_row, entry = entry_of(board([row]), 't1')
        self.assertEqual(entry['state'], 'working')
        self.assertFalse(entry['inferred'])
        self.assertNotIn('waiting_too_long', entry['flags'])

    def test_a_held_task_with_no_checkpoint_is_shown(self):
        row = task('t1', A, [comment('c0', 'claimed', A, '2026-09-15T08:00:00Z')])
        _lane_row, entry = entry_of(board([row]), 't1')
        self.assertTrue(entry['no_checkpoint'])
        self.assertIsNone(entry['state'])
        self.assertTrue(entry['inferred'])
        self.assertEqual(entry['held_age_seconds'], 10800)


class BoundAndRouteTests(unittest.TestCase):
    def many(self, count=3):
        """``count`` lanes, the first of them the caller A, and their registry."""
        holders = [A] + [lane('lane-%d' % index) for index in range(1, count)]
        rows = [task('t%d' % index, holders[index]) for index in range(count)]
        registry_data = registry(*[('lane-%d' % index, holders[index]) for index in range(count)])
        return rows, registry_data

    def test_the_page_is_bounded_with_next_offset(self):
        rows, registry_data = self.many(3)
        first = board(rows, A, registry_data, '--limit', '2')
        self.assertEqual((first['total'], len(first['items']), first['next_offset']), (3, 2, 2))
        second = board(rows, A, registry_data, '--limit', '2', '--offset', '2')
        self.assertEqual((second['total'], len(second['items']), second['next_offset']), (3, 1, None))
        with self.assertRaisesRegex(ValueError, '--limit'):
            board(rows, A, registry_data, '--limit', '0')

    def test_the_contributor_scope_shows_only_the_callers_own_row(self):
        rows, registry_data = self.many(3)
        every = board(rows, A, registry_data)
        self.assertEqual(every['total'], 3)
        own = board(rows, A, registry_data, scope='own')
        self.assertEqual([item['actor'] for item in own['items']], [A])
        self.assertEqual(own['total'], 1)
        self.assertEqual(own['scope'], 'own')

    def test_one_export_and_the_registry_are_the_only_reads(self):
        row = task('t1', A, [comment('c0', 'hello', A, '2026-09-15T09:00:00Z')])
        lines = '\n'.join(json.dumps(item) for item in [row])
        calls = []

        def run(argv):
            calls.append(list(argv))
            return lines

        with mock.patch.object(sessions, 'read_registry',
                               return_value=registry(('lane-a', A))):
            result = work.execute(Path('.'), A, 'work', ['lanes'], {}, run, operators=[A])
        self.assertEqual(calls, [['export', '--all']])
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['lane'], 'lane-a')
        self.assertEqual(result['items'][0]['held'][0]['task'], 't1')

    def test_the_work_help_names_the_lanes_read(self):
        payload = work.help_payload('work')
        self.assertIn('work lanes', payload['usage'])
        self.assertIn('--idle-minutes', payload['usage'])
        self.assertIn('lanes-idle-minutes', payload['limits'])


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointRouteTests(unittest.TestCase):
    def test_the_endpoint_scope_uses_the_kit_one_operator_definition(self):
        # A plain listed operator name is decided without reading the registry.
        self.assertEqual(endpoint.lanes_scope_for('contributor', frozenset(), 'lane:alex'), 'own')
        self.assertEqual(endpoint.lanes_scope_for('contributor', frozenset({'op'}), 'lane:alex'), 'own')
        self.assertEqual(endpoint.lanes_scope_for('op', frozenset({'op'}), 'lane:alex'), 'all')
        # An actor bound to a listed operator's principal is an operator too ...
        owners = {'bound': 'lane:op', 'op': 'lane:op'}
        self.assertEqual(endpoint.lanes_scope_for('bound', frozenset({'op'}), 'lane:alex', owners), 'all')
        # ... and an actor of another principal is not.
        self.assertEqual(endpoint.lanes_scope_for('other', frozenset({'op'}), 'lane:alex',
                                                  {'other': 'lane:someone'}), 'own')
        # The host route has no bound key at all and sees every lane.
        self.assertEqual(endpoint.lanes_scope_for('anyone', frozenset(), None), 'all')

    def test_a_damaged_registry_does_not_stop_the_lanes_read(self):
        def damaged():
            raise OSError('registry unreadable')
        # The plain listed name still decides 'all'; a non-listed actor narrows to 'own'
        # rather than failing the read.
        self.assertEqual(endpoint.lanes_scope_for('op', frozenset({'op'}), 'lane:alex', damaged), 'all')
        self.assertEqual(endpoint.lanes_scope_for('contributor', frozenset({'op'}), 'lane:alex', damaged), 'own')

    def test_a_member_of_another_project_sees_nothing(self):
        with self.assertRaisesRegex(ValueError, 'Unknown/uninitialized project'):
            endpoint.key_project_refusal({'project': 'beta', 'action': 'work', 'args': ['lanes']},
                                         frozenset({'alpha'}))
        self.assertIsNone(endpoint.key_project_refusal({'project': 'alpha', 'action': 'work'},
                                                       frozenset({'alpha'})))


if __name__ == '__main__':
    unittest.main()
