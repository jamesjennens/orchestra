"""kittrial-5bb.588 r4: the coordinator's REQUEST CHANGES on r3 (Neon's delta review), one class per item.

- cap-on-cli-writer: ``briefing.save_checkpoint`` with ``now=None`` (what ``client.py checkpoint`` calls) refuses
  an own-job ``expect_by`` more than 24 hours ahead, with no ``not_before`` in the record.
- should (1): the write cap is measured from the start of the own-job episode, the instant the board clamps from,
  on both writers; leaving the state starts a new episode.
- should (2): an own-job record whose episode start cannot be read is not quiet on the board.
- note (1): sibling sub-labels of one actor keep one episode.
- note (2): the docs, the help and the board page name the cap and show the effective time.
"""
import json
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))

import briefing as b
import claim_limit
import lane_state
import nudges
import test_http_agents
from test_briefing import TASK
from test_lane_state import save
from test_lanes import A, add_checkpoint, add_lane_state, board, entry_of, task
from test_own_job_588 import BASE, OWN, iso

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)


def own(expect_by=None, state=OWN):
    record = dict(schema_version=1, task=TASK, state=state)
    if state == OWN:
        record['job'] = 'the full test run'
    if expect_by is not None:
        record['expect_by'] = expect_by
    return record


def at(moment):
    """The native writer's clock, read through ``briefing.utc_now`` (``create`` so r3, without it, still runs)."""
    return mock.patch.object(b, 'utc_now', create=True, return_value=moment)


def stamp_new(data, count, moment):
    """Date the ``count`` comments the stub writer just appended (it stamps a fixed time)."""
    for comment in data[0]['comments'][-count:]:
        comment['created_at'] = iso(moment)


class CliWriterCap(unittest.TestCase):
    """cap-on-cli-writer: the native writer read the clock only for a record with not_before."""

    def rows(self):
        from test_briefing import rows
        return rows()

    def test_save_checkpoint_with_now_none_refuses_a_far_future_own_job_expect_by(self):
        for value in ('9999-12-31T23:59:59Z', iso(NOW + timedelta(days=1000)), iso(NOW + timedelta(hours=24, seconds=1))):
            with self.subTest(value=value):
                data = self.rows()
                with at(NOW), self.assertRaisesRegex(ValueError, '24 hours'):
                    save(data, now=None, lane_state=own(value))
                self.assertEqual([], [c for c in data[0]['comments'] if lane_state.is_record(c.get('text'))])

    def test_on_the_real_clock_too(self):
        real = datetime.now(timezone.utc)
        for value in ('9999-12-31T23:59:59Z', iso(real + timedelta(days=1000))):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, '24 hours'):
                    save(self.rows(), now=None, lane_state=own(value))

    def test_twenty_four_hours_exactly_is_accepted(self):
        data = self.rows()
        with at(NOW):
            result, _store = save(data, now=None, lane_state=own(iso(NOW + timedelta(hours=24))))
        self.assertIn('lane_state', result)


class WriteCapFromTheEpisodeStart(unittest.TestCase):
    """should (1): a same-episode repost was measured from the write, and clamped by the board from the start."""

    def first_wait(self):
        from test_briefing import rows
        data = rows()
        with at(NOW - timedelta(hours=18)):
            save(data, now=None, lane_state=own(iso(NOW - timedelta(hours=14))))
        stamp_new(data, 2, NOW - timedelta(hours=18))
        return data

    def test_a_repost_in_the_same_episode_is_measured_from_its_start(self):
        data = self.first_wait()
        with at(NOW), self.assertRaisesRegex(ValueError, 'after this wait began') as caught:
            save(data, now=None, lane_state=own(iso(NOW + timedelta(hours=20))))
        self.assertIn('working', str(caught.exception))
        with at(NOW):
            result, _store = save(data, now=None, lane_state=own(iso(NOW + timedelta(hours=6))))
        self.assertIn('lane_state', result)

    def test_leaving_the_state_starts_a_new_episode(self):
        data = self.first_wait()
        with at(NOW - timedelta(hours=1)):
            save(data, now=None, lane_state=own(state='working'))
        stamp_new(data, 2, NOW - timedelta(hours=1))
        with at(NOW):
            result, _store = save(data, now=None, lane_state=own(iso(NOW + timedelta(hours=20))))
        self.assertIn('lane_state', result)

    def test_the_helper_dates_a_continued_episode_from_its_first_record(self):
        history = [(A, OWN, iso(NOW - timedelta(hours=18))), (A + '/sub1', OWN, iso(NOW - timedelta(hours=9)))]
        self.assertEqual(NOW - timedelta(hours=18), lane_state.own_job_write_began(history, A + '/sub2', NOW))
        self.assertEqual(NOW, lane_state.own_job_write_began(history, 'someone-else', NOW))
        self.assertEqual(NOW, lane_state.own_job_write_began(history + [(A, 'working', iso(NOW))], A, NOW))
        self.assertEqual(NOW, lane_state.own_job_write_began([(A, OWN, 'not-a-time')], A, NOW))


class InProcessWriterAgrees(test_http_agents.AgentHarness):
    """should (1): the in-process writer measures from the same episode start."""

    def test_a_same_episode_repost_is_refused_over_http_too(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        _agent, secret, _ = self.agent_secret(alex, projects=[project], claim_limit=claim_limit.LIMIT_MAX)
        task_id = self.request('POST', '/v1/projects/%s/tasks' % project, {'title': 'the task'}, token=alex).data['id']
        base = '/v1/projects/%s/tasks/%s' % (project, task_id)
        self.assertEqual(200, self.request('POST', base + '/claim', token=secret).status)
        start = self.service._now()
        began = datetime.fromtimestamp(start, timezone.utc).replace(microsecond=0)

        def checkpoint(previous, expect_by, state=OWN):
            page = self.request('GET', base + '/brief', token=secret).data
            lane = dict(schema_version=1, task=task_id, state=state)
            if state == OWN:
                lane.update(job='the full test run', expect_by=expect_by)
            return self.request('POST', base + '/checkpoints', {
                'schema_version': 1, 'previous': previous, 'activity_cursor': page.get('activity_cursor') or '',
                'source_commit': '', 'branch': '', 'intent': 'do the task', 'acceptance': 'the checks pass',
                'summary': 'waiting on my run', 'next_action': 'wait', 'open_items': [], 'resolved': [],
                'lane_state': lane}, token=secret)

        first = checkpoint(None, iso(began + timedelta(hours=4)))
        self.assertEqual(201, first.status, first.data)
        with mock.patch.object(self.service, '_now', return_value=start + 18 * 3600):
            refused = checkpoint(first.data['checkpoint']['id'], iso(began + timedelta(hours=38)))
            self.assertEqual(422, refused.status, refused.data)
            self.assertIn('after this wait began', json.dumps(refused.data))


class UnreadableEpisodeStartIsNotQuiet(unittest.TestCase):
    """should (2): the board's clamp was skipped when the episode start could not be read."""

    def test_a_far_future_wait_with_an_unreadable_start_is_flagged(self):
        row = task('bad-1', A)
        add_checkpoint(row, 'cp-0', 'not-a-timestamp')
        add_lane_state(row, 'ls-0', 'cp-0', iso(BASE + timedelta(seconds=1)), state=OWN, job='the run',
                       expect_by='9999-12-31T23:59:59Z')
        lane, entry = entry_of(board([row], now=BASE + timedelta(days=30)), 'bad-1')
        self.assertIn('your_move_idle', entry['flags'])
        self.assertIsNone(entry['expect_by_effective'])
        self.assertIsNone(nudges.declared_wait(lane))


class SiblingSubLabels(unittest.TestCase):
    """note (1): A/sub1 then A/sub2 restarted the episode."""

    def test_siblings_keep_one_episode(self):
        row = task('sib-1', A)
        add_checkpoint(row, 'cp-0', iso(BASE))
        add_lane_state(row, 'ls-0', 'cp-0', iso(BASE + timedelta(seconds=1)), state=OWN, job='the run')
        add_checkpoint(row, 'cp-1', iso(BASE + timedelta(hours=3)), previous='cp-0')
        add_lane_state(row, 'ls-1', 'cp-1', iso(BASE + timedelta(hours=3, seconds=1)), state=OWN, job='the run, again')
        row['comments'][0]['author'] = row['comments'][1]['author'] = A + '/sub1'
        row['comments'][2]['author'] = row['comments'][3]['author'] = A + '/sub2'
        self.assertEqual('cp-0', lane_state.own_job_episode_start(row['comments'], 'sib-1')['id'])

    def test_a_shared_prefix_without_a_slash_is_still_another_actor(self):
        self.assertFalse(lane_state.same_episode_actor(A, A + 'x'))
        self.assertFalse(lane_state.same_episode_actor('alice/one', 'bob/one'))
        self.assertTrue(lane_state.same_episode_actor('alice/one', 'alice/two'))


class DocsHelpAndPage(unittest.TestCase):
    """note (2): what the docs, the help and the board page say about the cap."""

    def test_the_design_no_longer_says_expect_by_is_refused_only_when_unparseable(self):
        text = ' '.join((KIT / 'docs' / 'LANE_BOARD_DESIGN.md').read_text(encoding='utf-8').split())
        self.assertNotIn('refused only when it cannot be parsed', text)
        self.assertIn('24 hours after the wait began', text)

    def test_the_checkpoint_help_names_the_cap(self):
        limits = json.dumps(b.help_limits('checkpoint')) if hasattr(b, 'help_limits') else ''
        notes = ' '.join(b.help_notes('checkpoint'))
        self.assertIn('24 hours', notes)
        self.assertIn('24 hours', limits)

    def test_the_board_page_shows_the_effective_time_wherever_it_shows_expect_by(self):
        script = (KIT / 'web' / 'js' / 'views' / 'project.js').read_text(encoding='utf-8')
        self.assertEqual([], [line for line in script.splitlines()
                              if re.search(r'entry\.expect_by\b', line) and 'expect_by_effective' not in line])

    def test_the_local_waits_row_reads_as_a_sentence(self):
        text = (KIT / 'docs' / 'LOCAL_WAITS_DESIGN.md').read_text(encoding='utf-8')
        self.assertNotIn('(r3, note 2), is built', text)


if __name__ == '__main__':
    unittest.main()
