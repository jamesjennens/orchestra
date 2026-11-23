"""kittrial-5bb.588 r3: the coordinator's REQUEST CHANGES on r2 (Neon's delta review), one class per item.

- expect-by-cap: an own-job ``expect_by`` more than 24 hours ahead is refused at write, and one that reached a
  record anyway is clamped on the board to 24 hours after its episode began; at the ``expect_by`` instant the
  wait is still quiet, and one second later both flags fire (Neon's surviving R_m1_exact_boundary_gt).
- note 1: a repost under a sub-label of the same actor does not restart the own-job clock.
- agent-md-v4 is pinned by tests/test_agent_md_version.py (version 4, on .598's version 3).
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))

import lane_state
import nudges
from test_lanes import A, add_checkpoint, add_lane_state, board, entry_of, task
from test_own_job_588 import BASE, OWN, iso, own_job_row, payload

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)


class ExpectByCapAtWrite(unittest.TestCase):
    def refused(self, expect_by, **changes):
        return [p for p in lane_state.problems(payload(expect_by=expect_by, **changes), 't1', now=NOW)
                if 'hours ahead' in p]

    def test_a_far_future_own_job_expect_by_is_refused(self):
        for value in (iso(NOW + timedelta(days=1000)), '9999-12-31T23:59:59Z', iso(NOW + timedelta(hours=24, seconds=1))):
            with self.subTest(value=value):
                self.assertEqual(1, len(self.refused(value)))
                self.assertIn('24 hours', self.refused(value)[0])

    def test_twenty_four_hours_exactly_and_a_past_value_are_accepted(self):
        for value in (iso(NOW + timedelta(hours=24)), iso(NOW + timedelta(minutes=30)), iso(NOW - timedelta(days=3))):
            with self.subTest(value=value):
                self.assertEqual([], lane_state.problems(payload(expect_by=value), 't1', now=NOW))

    def test_other_waits_keep_any_expect_by(self):
        found = lane_state.problems(dict(schema_version=1, task='t1', state='waiting-on-review', waiting_on='reviewer',
                                         expect_by='9999-12-31T23:59:59Z'), 't1', now=NOW)
        self.assertEqual([], found)

    def test_the_read_path_does_not_refuse_so_an_old_record_stays_usable(self):
        self.assertEqual([], lane_state.problems(payload(expect_by='9999-12-31T23:59:59Z'), 't1'))


class ExpectByCapOnTheBoard(unittest.TestCase):
    def test_a_far_future_record_is_clamped_to_a_day_after_its_episode_began(self):
        row = own_job_row(expect_by='9999-12-31T23:59:59Z')
        _lane, entry = entry_of(board([row], now=BASE + timedelta(hours=23)), 'own-1')
        self.assertEqual([], entry['flags'])
        self.assertEqual(iso(BASE + timedelta(hours=24)), entry['expect_by_effective'])
        self.assertEqual('9999-12-31T23:59:59Z', entry['expect_by'])
        lane, entry = entry_of(board([row], now=BASE + timedelta(hours=24, seconds=1)), 'own-1')
        self.assertEqual(['expect_by_passed', 'your_move_idle'], sorted(entry['flags']))
        self.assertIsNone(nudges.declared_wait(lane))

    def test_a_value_within_the_day_is_not_clamped(self):
        row = own_job_row(expect_by=iso(BASE + timedelta(hours=6)))
        _lane, entry = entry_of(board([row], now=BASE + timedelta(hours=1)), 'own-1')
        self.assertEqual(iso(BASE + timedelta(hours=6)), entry['expect_by_effective'])

    def test_the_boundary_instant_is_quiet_and_one_second_later_both_flags_fire(self):
        row = own_job_row(expect_by=iso(BASE + timedelta(minutes=30)))
        _lane, entry = entry_of(board([row], now=BASE + timedelta(minutes=30)), 'own-1')
        self.assertEqual([], entry['flags'])
        _lane, entry = entry_of(board([row], now=BASE + timedelta(minutes=30, seconds=1)), 'own-1')
        self.assertEqual(['expect_by_passed', 'your_move_idle'], sorted(entry['flags']))


class EpisodeSubLabel(unittest.TestCase):
    """Note 1: lane_state.own_job_episode_start compared authors by exact string."""

    def test_a_repost_under_a_sub_label_keeps_the_first_start(self):
        row = task('sub-1', A)
        add_checkpoint(row, 'cp-0', iso(BASE))
        add_lane_state(row, 'ls-0', 'cp-0', iso(BASE + timedelta(seconds=1)), state=OWN, job='the run')
        add_checkpoint(row, 'cp-1', iso(BASE + timedelta(hours=3)), previous='cp-0')
        add_lane_state(row, 'ls-1', 'cp-1', iso(BASE + timedelta(hours=3, seconds=1)), state=OWN, job='the run, again')
        for comment in row['comments'][-2:]:
            comment['author'] = A + '/sub1'
        _lane, entry = entry_of(board([row], now=BASE + timedelta(hours=5)), 'sub-1')
        self.assertEqual(iso(BASE), entry['state_since'])
        self.assertIn('expect_by_passed', entry['flags'])

    def test_another_actor_still_starts_a_new_episode(self):
        row = task('oth-1', A)
        add_checkpoint(row, 'cp-0', iso(BASE))
        add_lane_state(row, 'ls-0', 'cp-0', iso(BASE + timedelta(seconds=1)), state=OWN, job='the run')
        add_checkpoint(row, 'cp-1', iso(BASE + timedelta(hours=3)), previous='cp-0')
        add_lane_state(row, 'ls-1', 'cp-1', iso(BASE + timedelta(hours=3, seconds=1)), state=OWN, job='theirs')
        for comment in row['comments'][-2:]:
            comment['author'] = A + 'x'                # a shared prefix without the slash is another actor
        first = lane_state.own_job_episode_start(row['comments'], 'oth-1')
        self.assertEqual('cp-1', first['id'])


if __name__ == '__main__':
    unittest.main()
