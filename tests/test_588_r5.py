"""kittrial-5bb.588 r5: r4 on .598 Slice 1 (13cf4b2e); how lanes.held_task resolves the two sources of a wait's times.

Slice 1 dates every wait from ``waits.py`` (an episode that continues across any run of waiting records, a 4 h
default and a 72 h cap on ``expect_by``). r4 dates a ``waiting-on-own-job`` record by ``lane_state``'s own-job rule
(the start of the unbroken own-job run, the same 4 h default, a 24 h cap that the writer also refuses past).

The resolution:
- own-job-is-not-an-episode: ``waits.py`` opens no episode for an own-job record. It waits on nobody, nobody can
  answer it, and it must not page anybody; like ``working`` it ends an open checkpoint wait.
- own-job-times: so the board dates an own-job record by the own-job rule alone: its own run start (not the
  start of a coordinator wait it followed) and the 24 h cap (not 72 h).
- own-job-keeps-the-row: a wait ``waits.py`` still reports for the task from another source (a delivery, an
  approval, a named review request) stays in the waiting list but does not replace the own-job row; the row's
  whose_move and next_actor come from the review state, so the coordinator's triggers still fire (r4 BoardTests).
- boundaries: each source keeps its own expect_by boundary (Slice 1 r6: flagged at the instant; r4: quiet then).
- unreadable: a comment time waits.py cannot read keeps the declared row instead of failing the board read.
"""
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))

import lanes
import waits
from test_lanes import A, add_checkpoint, add_lane_state, board, comment, entry_of, task
from test_own_job_588 import BASE, OWN, iso


def coordinator_then_own_job(task_id, own_after, **own_fields):
    """A coordinator wait at BASE, then (``own_after`` later) a waiting-on-own-job record."""
    row = task(task_id, A)
    add_checkpoint(row, 'cp-0', iso(BASE), next_action='ask the coordinator')
    add_lane_state(row, 'ls-0', 'cp-0', iso(BASE + timedelta(seconds=1)), state='waiting-on-coordinator',
                   waiting_on='coordinator')
    started = BASE + own_after
    add_checkpoint(row, 'cp-1', iso(started), previous='cp-0')
    fields = dict(state=OWN, job='the full test run')
    fields.update(own_fields)
    add_lane_state(row, 'ls-1', 'cp-1', iso(started + timedelta(seconds=1)), **fields)
    return row, started


class OwnJobIsNotAnEpisode(unittest.TestCase):
    """own-job-is-not-an-episode: waits.py reports no wait for an own-job record, and it ends the one before it."""

    def test_an_own_job_record_opens_no_wait(self):
        row, _started = coordinator_then_own_job('ep-1', timedelta(hours=2))
        self.assertEqual([], waits.task_wait_summary(row, 'ep-1', A, BASE + timedelta(hours=3)))

    def test_it_ends_the_coordinator_wait_before_it(self):
        row, started = coordinator_then_own_job('ep-2', timedelta(hours=2))
        computed = waits.compute_waits([row], now=BASE + timedelta(hours=3), project='trial')
        self.assertEqual([], [w for w in computed['waits'] if w.get('task') == 'ep-2'])
        ended = [e for e in computed.get('episodes') or [] if e.get('task') == 'ep-2']
        self.assertTrue(all(e.get('outcome') != 'open' for e in ended), ended)

    def test_a_coordinator_wait_alone_is_still_an_episode(self):
        row = task('ep-3', A)
        add_checkpoint(row, 'cp-0', iso(BASE))
        add_lane_state(row, 'ls-0', 'cp-0', iso(BASE + timedelta(seconds=1)), state='waiting-on-coordinator',
                       waiting_on='coordinator')
        found = waits.task_wait_summary(row, 'ep-3', A, BASE + timedelta(hours=1))
        self.assertEqual(['waiting-on-coordinator'], [w.get('state') for w in found])


class OwnJobTimes(unittest.TestCase):
    """own-job-times: the board dates an own-job record by the own-job rule, never by a waits.py episode."""

    def test_it_is_dated_from_its_own_run_not_the_coordinator_wait_before_it(self):
        row, started = coordinator_then_own_job('t-1', timedelta(hours=2))
        _lane, entry = entry_of(board([row], now=started + timedelta(hours=3)), 't-1')
        self.assertEqual(OWN, entry['state'])
        self.assertEqual('the full test run', entry['job'])
        self.assertEqual(iso(started), entry['state_since'])
        self.assertEqual(iso(started + timedelta(hours=4)), entry['expect_by_effective'])
        self.assertTrue(entry['declared'])
        self.assertFalse(entry['inferred'])
        self.assertNotIn('your_move_idle', entry['flags'])
        self.assertNotIn('expect_by_passed', entry['flags'])

    def test_the_default_passes_four_hours_after_its_own_run_began(self):
        row, started = coordinator_then_own_job('t-2', timedelta(hours=2))
        _lane, entry = entry_of(board([row], now=started + timedelta(hours=5)), 't-2')
        self.assertIn('expect_by_passed', entry['flags'])
        self.assertIn('your_move_idle', entry['flags'])

    def test_the_cap_is_twenty_four_hours_not_seventy_two(self):
        # A raw record past the writer's cap: the board clamps it to 24 h after the run began.
        row, started = coordinator_then_own_job('t-3', timedelta(hours=2),
                                                expect_by=iso(BASE + timedelta(hours=2 + 48)))
        _lane, early = entry_of(board([row], now=started + timedelta(hours=23)), 't-3')
        self.assertEqual(iso(started + timedelta(hours=24)), early['expect_by_effective'])
        self.assertNotIn('expect_by_passed', early['flags'])
        _lane, late = entry_of(board([row], now=started + timedelta(hours=30)), 't-3')
        self.assertEqual(iso(started + timedelta(hours=24)), late['expect_by_effective'])
        self.assertIn('expect_by_passed', late['flags'])
        self.assertIn('your_move_idle', late['flags'])


class OwnJobKeepsTheRow(unittest.TestCase):
    """own-job-keeps-the-row: a wait waits.py reports from another source does not replace the own-job row."""

    def delivery(self):
        return {'state': 'waiting-on-review', 'waiting_on': 'reviewer', 'inferred': False,
                'started_at': iso(BASE + timedelta(hours=1)), 'origin_checkpoint': None,
                'expect_by': iso(BASE + timedelta(hours=5)), 'expect_by_defaulted': True}

    def test_a_delivery_wait_leaves_the_own_job_row_and_its_times(self):
        row, started = coordinator_then_own_job('o-1', timedelta(hours=2))
        with mock.patch.object(waits, 'task_wait_summary', return_value=[self.delivery()]):
            entry = lanes.held_task(row, 'o-1', A, started + timedelta(hours=1), 15 * 60,
                                    review={'review_state': 'awaiting-review'})
        self.assertEqual(OWN, entry['state'])
        self.assertIsNone(entry['waiting_on'])
        self.assertEqual('the full test run', entry['job'])
        self.assertEqual(iso(started), entry['state_since'])
        self.assertEqual(iso(started + timedelta(hours=4)), entry['expect_by_effective'])
        self.assertNotIn('waiting_too_long', entry['flags'])

    def test_the_review_state_still_names_the_next_actor(self):
        # r4's BoardTests pin the coordinator's trigger; here the row's move comes from the review, not the record.
        row, started = coordinator_then_own_job('o-2', timedelta(hours=2))
        entry = lanes.held_task(row, 'o-2', A, started + timedelta(hours=1), 15 * 60,
                                review={'review_state': 'awaiting-integration'})
        self.assertEqual(OWN, entry['state'])
        self.assertEqual('coordinator', entry['next_actor'])


class EachSourceKeepsItsBoundary(unittest.TestCase):
    """boundaries: Slice 1 (r6) flags a waits.py wait AT its expect_by; r4 keeps an own-job wait quiet then."""

    def test_an_own_job_wait_is_quiet_at_the_instant_and_flagged_a_second_later(self):
        row, started = coordinator_then_own_job('b-1', timedelta(hours=2), expect_by=iso(BASE + timedelta(hours=3)))
        _lane, entry = entry_of(board([row], now=BASE + timedelta(hours=3)), 'b-1')
        self.assertNotIn('expect_by_passed', entry['flags'])
        _lane, entry = entry_of(board([row], now=BASE + timedelta(hours=3, seconds=1)), 'b-1')
        self.assertIn('expect_by_passed', entry['flags'])

    def test_a_waits_wait_is_flagged_at_the_instant(self):
        row = task('b-2', A)
        add_checkpoint(row, 'cp-0', iso(BASE))
        add_lane_state(row, 'ls-0', 'cp-0', iso(BASE + timedelta(seconds=1)), state='waiting-on-coordinator',
                       waiting_on='coordinator', expect_by=iso(BASE + timedelta(hours=2)))
        _lane, entry = entry_of(board([row], now=BASE + timedelta(hours=2)), 'b-2')
        self.assertIn('expect_by_passed', entry['flags'])


class UnreadableTimestamp(unittest.TestCase):
    """unreadable: waits.py raises on a comment time it cannot read; the board keeps the declared row."""

    def test_a_declared_coordinator_wait_with_a_bad_time_stays_on_the_board(self):
        row = task('u-1', A)
        add_checkpoint(row, 'cp-0', 'not-a-timestamp')
        add_lane_state(row, 'ls-0', 'cp-0', iso(BASE + timedelta(seconds=1)), state='waiting-on-coordinator',
                       waiting_on='coordinator')
        with self.assertRaises(ValueError):
            waits.task_wait_summary(row, 'u-1', A, BASE + timedelta(hours=1))
        _lane, entry = entry_of(board([row], now=BASE + timedelta(hours=1)), 'u-1')
        self.assertEqual('waiting-on-coordinator', entry['state'])
        self.assertEqual('coordinator', entry['waiting_on'])


class AnotherSourceWithNoRecord(unittest.TestCase):
    """r7 (unbound-payload): a waits.py wait from another source; CI on r6 raised UnboundLocalError on payload."""

    def delivery(self, inferred=False):
        return {'state': 'waiting-on-review', 'waiting_on': 'reviewer', 'inferred': inferred,
                'started_at': iso(BASE + timedelta(hours=1)), 'origin_checkpoint': None,
                'expect_by': None, 'expect_by_defaulted': True}

    def held(self, row, task_id, waits_found):
        with mock.patch.object(waits, 'task_wait_summary', return_value=waits_found):
            return lanes.held_task(row, task_id, A, BASE + timedelta(hours=2), 15 * 60,
                                   review={'review_state': 'awaiting-review'})

    def test_a_delivery_wait_on_a_task_without_a_lane_state_record(self):
        row = task('n-1', A)
        add_checkpoint(row, 'cp-0', iso(BASE))
        entry = self.held(row, 'n-1', [self.delivery()])
        self.assertEqual('waiting-on-review', entry['state'])
        self.assertTrue(entry['declared'])
        self.assertIsNone(entry['local_fact'])          # no record, so no fact: waits.py adds none
        # whose_move and next_actor come from the review state, not from the wait.
        plain = self.held(row, 'n-1', [])
        self.assertEqual((plain['whose_move'], plain['next_actor']), (entry['whose_move'], entry['next_actor']))
        self.assertNotEqual('you', entry['whose_move'])

    def test_the_board_reads_such_a_task(self):
        # The CI case end to end: tests/test_comment_attention.py:130 through lanes.read as well.
        row = task('n-4', A)
        add_checkpoint(row, 'cp-0', iso(BASE))
        with mock.patch.object(waits, 'task_wait_summary', return_value=[self.delivery()]):
            _lane, entry = entry_of(board([row], now=BASE + timedelta(hours=2)), 'n-4')
        self.assertEqual('waiting-on-review', entry['state'])
        self.assertIsNone(entry['local_fact'])

    def test_the_lanes_own_fact_is_still_shown_under_another_sources_wait(self):
        fact = {'kind': 'job-finished', 'what': 'the suite', 'at': iso(BASE)}
        for inferred in (False, True):
            with self.subTest(inferred=inferred):
                row = task('n-2', A)
                add_checkpoint(row, 'cp-0', iso(BASE))
                add_lane_state(row, 'ls-0', 'cp-0', iso(BASE + timedelta(seconds=1)), state='working', local_fact=fact)
                entry = self.held(row, 'n-2', [self.delivery(inferred=inferred)])
                self.assertEqual('the suite', (entry['local_fact'] or {}).get('what'))


class WaitsUnreadable(unittest.TestCase):
    """r7 (Neon MUST-2): a task waits.py cannot read is shown, marked 'waits-unreadable', never silently clean."""

    def bad_comment(self, row):
        row['comments'].append(comment('bad-1', 'a plain note', 'someone-else', 'not-a-timestamp'))

    def test_a_delivery_wait_with_a_bad_comment_time_is_marked(self):
        row = task('w-1', A)
        add_checkpoint(row, 'cp-0', iso(BASE))
        add_lane_state(row, 'ls-0', 'cp-0', iso(BASE + timedelta(seconds=1)), state='working')
        self.bad_comment(row)
        entry = lanes.held_task(row, 'w-1', A, BASE + timedelta(hours=2), 15 * 60,
                                review={'review_state': 'awaiting-review'})
        self.assertEqual('working', entry['state'])
        self.assertIn('waits-unreadable', entry['flags'])
        self.assertEqual('w-1 comment bad-1: created_at is not ISO-8601', entry['waits_unreadable_reason'])

    def test_the_reason_is_one_printable_line_within_its_limit_and_never_the_value(self):
        row = task('w-4', A)
        add_checkpoint(row, 'cp-0', iso(BASE))
        add_lane_state(row, 'ls-0', 'cp-0', iso(BASE + timedelta(seconds=1)), state='working')
        row['comments'].append(comment('x\n' + 'y' * 300, 'a plain note', 'someone-else', 'secret\u202evalue'))
        reason = lanes.held_task(row, 'w-4', A, BASE + timedelta(hours=1), 15 * 60, review={})['waits_unreadable_reason']
        self.assertTrue(reason.startswith('w-4 comment x?yyy'), reason)
        self.assertLessEqual(len(reason), 240)
        self.assertTrue(reason.isprintable())
        self.assertNotIn('secret', reason)

    def test_a_readable_task_has_no_reason(self):
        row = task('w-5', A)
        add_checkpoint(row, 'cp-0', iso(BASE))
        entry = lanes.held_task(row, 'w-5', A, BASE + timedelta(hours=1), 15 * 60, review={})
        self.assertIsNone(entry['waits_unreadable_reason'])
        self.assertNotIn('waits-unreadable', entry['flags'])

    def test_an_overdue_declared_wait_keeps_its_own_flags(self):
        row = task('w-2', A)
        add_checkpoint(row, 'cp-0', iso(BASE))
        add_lane_state(row, 'ls-0', 'cp-0', iso(BASE + timedelta(seconds=1)), state='waiting-on-coordinator',
                       waiting_on='coordinator', expect_by=iso(BASE + timedelta(hours=1)))
        self.bad_comment(row)
        # The holder wrote five minutes ago, so your_move_idle alone would not show the wait.
        row['comments'].append(comment('note-1', 'still here', A, iso(BASE + timedelta(hours=8, minutes=55))))
        entry = lanes.held_task(row, 'w-2', A, BASE + timedelta(hours=9), 15 * 60, review={})
        self.assertEqual('waiting-on-coordinator', entry['state'])
        self.assertEqual(['expect_by_passed', 'waiting_too_long', 'waits-unreadable'],
                         sorted(f for f in entry['flags'] if f != 'your_move_idle'))

    def test_the_coordinator_view_treats_it_as_needing_attention(self):
        import waiting
        import client
        row = task('w-3', A)
        add_checkpoint(row, 'cp-0', iso(BASE))
        add_lane_state(row, 'ls-0', 'cp-0', iso(BASE + timedelta(seconds=1)), state='working')
        self.bad_comment(row)
        row['comments'].append(comment('note-1', 'still here', A, iso(BASE + timedelta(minutes=59))))
        result = board([row], now=BASE + timedelta(hours=1))
        _lane, entry = entry_of(result, 'w-3')
        self.assertEqual(['waits-unreadable'], entry['flags'])
        self.assertEqual(['w-3'], result['items'][0]['flag_tasks']['waits-unreadable'])
        for module in (waiting, client._InlineWait):
            with self.subTest(view=module):
                quiet = module.coordinator_view([], result)['triggers']['lane-quiet']
                self.assertEqual([['waits-unreadable']], [item['flags'] for item in quiet])


class WaitsFallbackOwnJob(unittest.TestCase):
    """r7 (Neon SHOULD-1): the holder fallback (no checkpoint chain) opens no episode for an own-job record."""

    def row(self, **state):
        row = task('f-1', A)
        row['comments'].append(comment('plain-1', 'working on it', A, iso(BASE)))
        add_lane_state(row, 'ls-0', 'plain-1', iso(BASE + timedelta(seconds=1)), **state)
        return row

    def test_an_own_job_record_without_a_checkpoint_chain_opens_no_wait(self):
        found = waits.task_wait_summary(self.row(state=OWN, job='the run'), 'f-1', A, BASE + timedelta(hours=1))
        self.assertEqual([], found)

    def test_a_coordinator_record_there_still_does(self):
        found = waits.task_wait_summary(self.row(state='waiting-on-coordinator', waiting_on='coordinator'), 'f-1', A,
                                        BASE + timedelta(hours=1))
        self.assertEqual(['waiting-on-coordinator'], [w.get('state') for w in found])


if __name__ == '__main__':
    unittest.main()
