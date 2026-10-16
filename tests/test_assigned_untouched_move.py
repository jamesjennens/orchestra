"""An assigned, never-touched open task is its holder's move (kittrial-5bb.288).

The reported failure: ``work --owner ACTOR --json`` answered ``your_move`` 0 and
"0 of your tasks need your action" while its own ``items`` held an OPEN task assigned
to ACTOR on which ACTOR had neither delivered a contribution nor written a checkpoint;
the row read ``whose_move`` 'unknown', ``next`` "Whose move this is cannot be told from
the review state shown here." and ``next_actor`` 'assignee'. A lane trusting the count
reported itself free with its next task in the list.

The rule pinned here, in every reader: an open task assigned to the actor the read
answers for, with no contribution and no checkpoint by that actor, IS that actor's
move -- ``whose_move`` 'you' and it counts in ``your_move`` (or is the holder's move on
the lane board and the agents-page projection). ``--owner ACTOR`` answers for ACTOR;
``--mine`` and an unfiltered scan answer for the caller, and ``next_actor`` names the
role either way. The integrated-but-open case follows kittrial-5bb.211: an integrated
revision does not finish a task kept open -- with an assignee it is that holder's move
(``work.move_decision('integrated', ..., True)`` -> ``you``/``assignee``), and for a
different caller it stays ``unknown``/``assignee``.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import http_service
import work
from test_briefing import PROJECT, TASK, append_checkpoint, blocker, checkpoint, rows
from test_lanes import A, B, board, comment, entry_of, task
from test_review_state import reviewed as integrated_open
from test_work_queue import add, contribute


def owner_read(data, owner, caller='coordinator/lane'):
    """The queue one actor reads for ANOTHER actor with ``--owner``."""
    return work.queue(data, caller, ['--owner', owner])


def row_of(page, task=TASK):
    return next(item for item in page['items'] if item['task'] == task)


class WorkAssignedUntouchedTests(unittest.TestCase):
    """Reader 1: `work` and `work --owner ACTOR`."""

    def test_assigned_untouched_open_task_is_the_owners_move_and_counts(self):
        page = owner_read(rows(), 'alice/session')
        item = row_of(page)
        # The row the bug report described: assigned, never touched. It is now the
        # assignee's move, named in words, and the top-level count agrees.
        self.assertEqual((item['owner'], item['review_state'], item['whose_move'], item['next_actor']),
                         ('alice/session', 'none', 'you', 'assignee'))
        self.assertIn('no contribution has been delivered yet', item['next'])
        self.assertNotIn(work.UNKNOWN_MOVE_TEXT, item['next'])
        self.assertEqual(page['your_move'], {'count': 1, 'text': '1 of your tasks need your action'})
        # The caller's own move sorts first, from the same reconciled rows.
        self.assertEqual(page['items'][0]['task'], TASK)

    def test_positive_control_an_unassigned_untouched_task_does_not_count(self):
        data = rows()
        data[0]['assignee'] = None
        page = work.queue(data, 'coordinator/lane', [])
        item = row_of(page)
        self.assertNotEqual(item['whose_move'], 'you')
        self.assertEqual(page['your_move'], {'count': 0, 'text': '0 of your tasks need your action'})

    def test_a_task_the_owner_contributed_to_keeps_its_reading(self):
        data = rows()
        add(data, contribute(), 'delivery', 'alice/session')
        page = owner_read(data, 'alice/session')
        item = row_of(page)
        # Delivered work is the reviewer's move, not the holder's, so it never counts.
        self.assertEqual((item['review_state'], item['whose_move'], item['next_actor']),
                         ('awaiting-review', 'reviewer', 'reviewer'))
        self.assertEqual(page['your_move']['count'], 0)

    def test_a_checkpoint_without_a_contribution_reads_consistently(self):
        data = rows()
        append_checkpoint(data, 'cp-own', checkpoint(data, open_items=[blocker()]))
        page = owner_read(data, 'alice/session')
        item = row_of(page)
        # A checkpoint is not a contribution: the task is still the holder's move, and
        # the holder's own recorded blocker is quoted in `next` beside that move.
        self.assertEqual((item['review_state'], item['whose_move'], item['next_actor']),
                         ('none', 'you', 'assignee'))
        self.assertEqual(item['checkpoint_wait']['status'], 'recorded')
        self.assertIn('You recorded a blocker or dependency in your own checkpoint', item['next'])
        self.assertEqual(page['your_move']['count'], 1)

    def test_integrated_but_open_follows_kittrial_5bb_211(self):
        # kittrial-5bb.211 amendment (3): a task whose first contribution is integrated
        # but which stays open reads the assignee's move; it is NOT finished by integration.
        store = integrated_open()
        page = owner_read(store.rows, 'alice/session', caller='coordinator/lane')
        item = row_of(page)
        self.assertEqual((item['review_state'], item['whose_move'], item['next_actor']),
                         ('integrated', 'you', 'assignee'))
        self.assertIn('stays open after its contribution was integrated', item['next'])
        self.assertEqual(page['your_move']['count'], 1)
        # A different caller still cannot tell the move, but the ROLE is the assignee's.
        other = work.queue(store.rows, 'someone/else', [])
        self.assertEqual((row_of(other)['whose_move'], row_of(other)['next_actor']),
                         ('unknown', 'assignee'))
        self.assertEqual(work.move_decision('integrated', TASK, True, assignee='alice/session')[0], 'you')
        self.assertEqual(work.move_decision('integrated', TASK, False, assignee='alice/session')[0], 'unknown')

    def test_mine_answers_for_the_caller_and_an_unfiltered_scan_stays_caller_relative(self):
        data = rows()
        mine = work.queue(data, 'alice/session', ['--mine'])
        self.assertEqual((row_of(mine)['whose_move'], mine['your_move']['count']), ('you', 1))
        # No --owner and no --mine: the caller-relative answer survives, and next_actor
        # still names the holder's role so a coordinator can scan without pretending.
        scan = work.queue(data, 'coordinator/lane', [])
        self.assertEqual((row_of(scan)['whose_move'], row_of(scan)['next_actor'], scan['your_move']['count']),
                         ('unknown', 'assignee', 0))

    def test_move_subject_answers_for_the_owner_it_is_given(self):
        self.assertEqual(work.move_subject('caller', None), 'caller')
        self.assertEqual(work.move_subject('caller', 'owner'), 'owner')


class LaneBoardAssignedUntouchedTests(unittest.TestCase):
    """Reader 2: the lane board (`work lanes`)."""

    def fixture(self):
        # The lane itself, its assigned untouched task, and a recent write by the lane
        # somewhere else, so "free" is a real answer the board can give once it holds
        # nothing.
        held = task('t1', A)
        recent = task('t2', B, [comment('c9', 'a note', A, '2026-09-15T11:30:00Z')])
        return held, recent

    def lane_row(self, result):
        return next(item for item in result['items'] if item['actor'] == A)

    def test_assigned_untouched_task_makes_the_lane_hold_it_and_not_idle(self):
        held, recent = self.fixture()
        lane_row, entry = entry_of(board([held, recent]), 't1')
        self.assertFalse(lane_row['idle'])
        self.assertEqual(entry['whose_move'], 'you')
        self.assertEqual(entry['next_actor'], 'assignee')
        self.assertIn('no contribution has been delivered yet', entry['next'])

    def test_closing_or_reassigning_restores_the_free_message(self):
        held, recent = self.fixture()
        held['status'] = 'closed'
        closed = self.lane_row(board([held, recent]))
        self.assertTrue(closed['idle'])
        self.assertEqual((closed['state'], closed['held']), ('idle', []))
        held['status'] = 'in_progress'
        held['assignee'] = B
        reassigned = self.lane_row(board([held, recent]))
        self.assertTrue(reassigned['idle'])
        self.assertEqual(reassigned['held'], [])
        # And the task went to its new holder, not nowhere.
        moved, entry = entry_of(board([held, recent]), 't1')
        self.assertEqual((moved['actor'], entry['whose_move']), (B, 'you'))


class PageProjectionAssignedUntouchedTests(unittest.TestCase):
    """Reader 4: the agents-page / bridge projection reads the work row's move."""

    def projection_row(self, task_id, assignee):
        item = http_service.queue_item(
            PROJECT, {'id': task_id, 'title': 'A task', 'status': 'in_progress', 'assignee': assignee},
            'none', None, 0,
            attention={'open_items': 0, 'blocking_items': 0, 'redelivered': False,
                       'checkpoint_at': None, 'newer_activity': None, 'pending_change_requests': []},
            claimable=None)
        return http_service.own_queue_tasks({'items': [item], 'complete': True}, assignee)['tasks'][0]

    def test_the_projection_agrees_with_the_work_owner_row(self):
        row = row_of(owner_read(rows(), 'alice/session'))
        projected = self.projection_row(TASK, 'alice/session')
        # Not a pixel test: the row the page reads carries the same move words, role and
        # holder the canonical `work --owner ACTOR` row carries.
        self.assertEqual((projected['whose_move'], projected['next_actor']),
                         (row['whose_move'], row['next_actor']))
        self.assertEqual(projected['whose_move'], 'you')
        self.assertEqual(projected['next'], row['next'])
        self.assertNotIn(work.UNKNOWN_MOVE_TEXT, projected['next'])

    def test_projection_rows_carry_the_move_from_the_one_decision(self):
        # The holder is the subject, exactly as in the lane board: an unassigned row is
        # not offered by this projection at all, and a delivered one is the reviewer's.
        projected = self.projection_row(TASK, 'alice/session')
        expected = work.move_decision('none', TASK, True, assignee='alice/session')
        self.assertEqual((projected['whose_move'], projected['next'], projected['next_actor']), expected)


if __name__ == '__main__':
    unittest.main()
