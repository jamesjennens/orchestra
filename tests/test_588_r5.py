"""kittrial-5bb.588 r5: r4 on .598 Slice 1 (13cf4b2e); how lanes.held_task resolves the two sources of a wait's times.

Slice 1 dates every wait from ``waits.py`` (an episode that continues across any run of waiting records, a 4 h
default and a 72 h cap on ``expect_by``). r4 dates a ``waiting-on-own-job`` record by ``lane_state``'s own-job rule
(the start of the unbroken own-job run, the same 4 h default, a 24 h cap that the writer also refuses past).

The resolution:
- own-job-is-not-an-episode: ``waits.py`` opens no episode for an own-job record. It waits on nobody, nobody can
  answer it, and it must not page anybody; like ``working`` it ends an open checkpoint wait.
- own-job-times: so the board dates an own-job record by the own-job rule alone: its own run start (not the
  start of a coordinator wait it followed) and the 24 h cap (not 72 h).
- other-party-wins: a wait ``waits.py`` still reports for the task (a delivery, an approval, a named review request)
  is another party's move and governs the row, as it does for a declared ``working`` record on Slice 1.
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
from test_lanes import A, add_checkpoint, add_lane_state, board, entry_of, task
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


class OtherPartyWins(unittest.TestCase):
    """other-party-wins: a delivery or review wait from waits.py governs the row over an own-job record."""

    def test_a_delivery_wait_governs_an_own_job_row(self):
        row, started = coordinator_then_own_job('o-1', timedelta(hours=2))
        delivery = {'state': 'waiting-on-review', 'waiting_on': 'reviewer', 'inferred': False,
                    'started_at': iso(BASE + timedelta(hours=1)), 'origin_checkpoint': None,
                    'expect_by': iso(BASE + timedelta(hours=5)), 'expect_by_defaulted': True}
        now = started + timedelta(hours=1)
        with mock.patch.object(waits, 'task_wait_summary', return_value=[delivery]):
            entry = lanes.held_task(row, 'o-1', A, now, 15 * 60, review={'review_state': 'awaiting-review'})
        self.assertEqual('waiting-on-review', entry['state'])
        self.assertEqual('reviewer', entry['waiting_on'])
        self.assertIsNone(entry['job'])
        self.assertIsNone(entry['expect_by'])
        self.assertIsNone(entry['expect_by_effective'])
        self.assertEqual(iso(BASE + timedelta(hours=1)), entry['state_since'])

    def test_without_one_the_own_job_record_governs(self):
        row, started = coordinator_then_own_job('o-2', timedelta(hours=2))
        with mock.patch.object(waits, 'task_wait_summary', return_value=[]):
            entry = lanes.held_task(row, 'o-2', A, started + timedelta(hours=1), 15 * 60, review={})
        self.assertEqual(OWN, entry['state'])
        self.assertEqual(iso(started + timedelta(hours=4)), entry['expect_by_effective'])


if __name__ == '__main__':
    unittest.main()
