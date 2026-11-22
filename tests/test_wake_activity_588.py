"""What a waking worker is told is new (kittrial-5bb.588 part 4).

The rule (coordinator, 2026-11-14): every comment by someone other than the lane, written after
the lane's OWN newest checkpoint, is new activity: in ``newer_activity``,
``newer_activity_by_others`` and the wait marker. And every reader prints one sentence the
service builds, ``wake_summary``, instead of composing its own "no new activity".

Pinned here:
- T1: a later checkpoint by someone else (a coordinator or a reviewer) does not hide a comment
  written after the lane's own checkpoint; that checkpoint is new activity itself.
- T2: two comments in one second are ordered by the tracker's own sequence, so the second one
  moves the wait marker (numeric comment ids compare as numbers, not as text).
- T4: every work row and every wait item carries ``wake_summary``, one sentence from the same
  fields, and it is never fingerprinted.
- ``wrote_since``: a comment by the actor or role waited on, after a checkpoint, counts; the
  lane's own comment never does; a comment on another task never does.
- The record families the snapshot hides are exactly today's list (T3 is not a live gap on
  b1f2356c: no kit writes the coordinator families onto a task yet).
"""
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import briefing as b
import waiting
import work
from test_briefing import PROJECT, STAMP, TASK, comment, rows, save_cp
from test_briefing import checkpoint as checkpoint_payload

LANE = 'alice/session'
COORD = 'coordinator/session'


def later(seconds):
    """STAMP plus some seconds, in the fixtures' own format."""
    base = datetime.strptime(STAMP, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%SZ')


def queue_row(data, actor=LANE):
    return work.queue(data, actor, ['--mine'])['items'][0]


def save_cp_by(data, cid, author, stamp, **changes):
    """A checkpoint written by ``author`` (not the lane), as save_checkpoint stores it."""
    writes = []

    def run(args):
        writes.append(args)
        return json.dumps(dict(id=cid))
    payload = checkpoint_payload(data, **changes)
    b.save_checkpoint(data, PROJECT, TASK, payload, author, run, provenance_writes=True)
    data[0]['comments'].append(comment(cid, writes[0][3], stamp, author=author))


class T1SomeoneElsesCheckpointIsNotTheBaseline(unittest.TestCase):
    """The lane's own newest checkpoint is the baseline, not the newest by anyone."""

    def setUp(self):
        self.data = rows()
        save_cp(self.data, 'cp1')
        self.data[0]['comments'].append(comment('c-coord', 'Please also cover the export', later(300), author=COORD))
        save_cp_by(self.data, 'cp2', COORD, later(600))

    def test_a_coordinator_comment_after_my_checkpoint_stays_new_after_a_coordinator_checkpoint(self):
        row = queue_row(self.data)
        self.assertTrue(row['newer_activity'])
        # The comment and the coordinator's own checkpoint are both by another actor.
        self.assertEqual(2, row['newer_activity_by_others'])
        self.assertEqual(0, row['newer_activity_own'])
        self.assertNotEqual('current', row['newer_activity_coverage'])

    def test_brief_reads_the_same_fields(self):
        activity = b.brief(self.data, PROJECT, TASK)['activity']
        row = queue_row(self.data)
        for field in ('newer_activity', 'newer_activity_by_others', 'newer_activity_own',
                      'newer_activity_marker', 'newer_activity_coverage'):
            self.assertEqual(row[field], activity[field], field)

    def test_my_next_checkpoint_resets_it(self):
        save_cp(self.data, 'cp3')
        self.data[0]['comments'][-1]['created_at'] = later(900)
        row = queue_row(self.data)
        self.assertFalse(row['newer_activity'])
        self.assertEqual(0, row['newer_activity_by_others'])

    def test_with_no_checkpoint_of_the_lanes_own_the_newest_by_anyone_stays_the_baseline(self):
        """Unchanged: a lane that never checkpointed is measured as before."""
        data = rows()
        save_cp_by(data, 'cpx', COORD, later(60))
        row = queue_row(data)
        self.assertFalse(row['newer_activity'])
        self.assertEqual(0, row['newer_activity_by_others'])


class T2OneSecondTwoComments(unittest.TestCase):
    """The newest entry of a second is the tracker's newest, not the larger id as text."""

    def setUp(self):
        self.data = rows()
        self.data[0]['comments'][0]['id'] = '1'
        save_cp(self.data, '2')

    def test_the_tenth_comment_moves_the_wait_marker_after_the_ninth_in_the_same_second(self):
        self.data[0]['comments'].append(comment('9', 'First note', later(300), author=COORD))
        after_nine = queue_row(self.data)
        self.data[0]['comments'].append(comment('10', 'Second note, same second', later(300), author=COORD))
        after_ten = queue_row(self.data)
        self.assertNotEqual(after_nine['wait_newer_activity_marker'], after_ten['wait_newer_activity_marker'])
        self.assertTrue(after_ten['wait_newer_activity_marker'].startswith(TASK + '-c10:'),
                        after_ten['wait_newer_activity_marker'])
        self.assertTrue(after_ten['newer_activity_marker'].startswith(TASK + '-c10:'),
                        after_ten['newer_activity_marker'])

    def test_so_a_worker_waiting_after_the_ninth_wakes_for_the_tenth(self):
        self.data[0]['comments'].append(comment('9', 'First note', later(300), author=COORD))
        since = waiting.worker_view([queue_row(self.data)])['fingerprint']
        self.data[0]['comments'].append(comment('10', 'Second note, same second', later(300), author=COORD))
        self.assertNotEqual(since, waiting.worker_view([queue_row(self.data)])['fingerprint'])

    def test_text_ids_keep_their_order(self):
        """Non-numeric ids (the endpoint's time-ordered ids) compare exactly as before."""
        self.assertLess(b.entry_order_key({'timestamp': later(1), 'entry_id': TASK + '-c01a1-aaa'}),
                        b.entry_order_key({'timestamp': later(1), 'entry_id': TASK + '-c01a1-bbb'}))
        self.assertLess(b.entry_order_key({'timestamp': later(1), 'entry_id': TASK + '-c9'}),
                        b.entry_order_key({'timestamp': later(1), 'entry_id': TASK + '-c10'}))
        self.assertLess(b.entry_order_key({'timestamp': later(1), 'entry_id': TASK + '-c10'}),
                        b.entry_order_key({'timestamp': later(2), 'entry_id': TASK + '-c9'}))


class T4OneSentenceForEveryReader(unittest.TestCase):
    """``wake_summary``: built in one place, carried by rows and wait items, never fingerprinted."""

    def test_nothing_new_says_so_with_the_checkpoint_time(self):
        data = rows()
        save_cp(data, 'cp1')
        summary = queue_row(data)['wake_summary']
        self.assertEqual('Nothing new by others since your checkpoint at %s.' % STAMP, summary)

    def test_a_coordinator_comment_is_named_with_its_author_and_time(self):
        data = rows()
        save_cp(data, 'cp1')
        data[0]['comments'].append(comment('c-coord', 'Please also cover the export', later(300), author=COORD))
        summary = queue_row(data)['wake_summary']
        self.assertTrue(summary.startswith('1 new entry by others since your checkpoint at %s' % STAMP), summary)
        self.assertIn('newest by %s at %s' % (COORD, later(300)), summary)

    def test_someone_elses_later_checkpoint_is_named(self):
        data = rows()
        save_cp(data, 'cp1')
        data[0]['comments'].append(comment('c-coord', 'Please also cover the export', later(300), author=COORD))
        save_cp_by(data, 'cp2', COORD, later(600))
        summary = queue_row(data)['wake_summary']
        self.assertTrue(summary.startswith('2 new entries by others since your checkpoint at %s' % STAMP), summary)
        self.assertIn('%s recorded a checkpoint at %s' % (COORD, later(600)), summary)

    def test_no_checkpoint_says_so(self):
        summary = queue_row(rows())['wake_summary']
        self.assertEqual('You have no checkpoint on this task yet.', summary)

    def test_the_summary_is_the_same_in_brief_and_in_the_wait_item(self):
        data = rows()
        save_cp(data, 'cp1')
        data[0]['comments'].append(comment('c-coord', 'Please also cover the export', later(300), author=COORD))
        row = queue_row(data)
        self.assertEqual(row['wake_summary'], b.brief(data, PROJECT, TASK)['activity']['wake_summary'])
        item = waiting.worker_view([row])['items'][0]
        self.assertEqual(row['wake_summary'], item['wake_summary'])

    def test_the_summary_is_never_fingerprinted(self):
        data = rows()
        save_cp(data, 'cp1')
        row = queue_row(data)
        other = dict(row, wake_summary='Some other sentence.')
        self.assertEqual(waiting.worker_view([row])['fingerprint'], waiting.worker_view([other])['fingerprint'])
        self.assertNotIn('wake_summary', waiting.WORKER_MOVE_FIELDS)
        self.assertNotIn('wake_summary', waiting.MOVE_FIELDS)


class WroteSince(unittest.TestCase):
    """``wrote_since(row, checkpoint_comment, who, in_role=None)`` for .585 and .598."""

    def setUp(self):
        self.data = rows()
        save_cp(self.data, 'cp1')
        self.cp = next(c for c in self.data[0]['comments'] if c['id'] == 'cp1')

    def role(self, author, name):
        return name == 'coordinator' and author == COORD

    def test_a_comment_by_the_actor_waited_on_after_the_checkpoint_counts(self):
        self.assertFalse(b.wrote_since(self.data[0], self.cp, COORD))
        self.data[0]['comments'].append(comment('c1', 'Answer', later(60), author=COORD))
        self.assertTrue(b.wrote_since(self.data[0], self.cp, COORD))

    def test_a_comment_by_a_holder_of_the_role_waited_on_counts(self):
        self.data[0]['comments'].append(comment('c1', 'Answer', later(60), author=COORD))
        self.assertTrue(b.wrote_since(self.data[0], self.cp, 'coordinator', in_role=self.role))
        self.assertFalse(b.wrote_since(self.data[0], self.cp, 'reviewer', in_role=self.role))

    def test_the_lanes_own_comment_never_counts(self):
        self.data[0]['comments'].append(comment('c1', 'My own note', later(60), author=LANE))
        self.data[0]['comments'].append(comment('c2', 'My sub-label note', later(61), author=LANE + '/sub0'))
        self.assertFalse(b.wrote_since(self.data[0], self.cp, LANE))
        self.assertFalse(b.wrote_since(self.data[0], self.cp, 'coordinator', in_role=lambda author, name: True))

    def test_a_comment_on_another_task_never_counts(self):
        other = dict(self.data[0], id='other-task', comments=[comment('o1', 'Elsewhere', later(60), author=COORD)])
        self.assertFalse(b.wrote_since(self.data[0], self.cp, COORD))
        with self.assertRaises(ValueError):
            b.wrote_since(other, self.cp, COORD)

    def test_a_comment_before_the_checkpoint_does_not_count(self):
        data = rows()
        data[0]['comments'].append(comment('c0', 'Earlier', later(-60), author=COORD))
        save_cp(data, 'cp1')
        cp = next(c for c in data[0]['comments'] if c['id'] == 'cp1')
        self.assertFalse(b.wrote_since(data[0], cp, COORD))

    def test_a_role_needs_a_resolver(self):
        with self.assertRaises(ValueError):
            b.wrote_since(self.data[0], self.cp, 'coordinator')


class HiddenFamiliesToday(unittest.TestCase):
    """T3, recorded: the families the snapshot hides, as on b1f2356c."""

    def test_the_hidden_record_families_are_todays(self):
        from reserved_comments import RECORD_COMMENT_FAMILIES
        self.assertEqual(('Kind: reference-', 'Kind: requirement-proposal-', 'Kind: proposal-disposition-',
                          'Kind: contribution-settings-', 'Kind: capability-', 'Kind: interface-',
                          'Kind: open-item-', 'Kind: item-resolution-', 'Kind: owner-answer-',
                          'Kind: coordinator-decision-', 'Kind: design-index-', 'Kind: design-verification-'),
                         RECORD_COMMENT_FAMILIES)



import claim_limit  # noqa: E402
import test_http_agents  # noqa: E402


class T1InProcessBackend(test_http_agents.AgentHarness):
    """The disposable backend measures from the assignee's own checkpoint too."""

    def test_an_owners_later_checkpoint_does_not_hide_the_owners_comment(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        _agent, secret, _ = self.agent_secret(alex, projects=[project], claim_limit=claim_limit.LIMIT_MAX)
        task = self.request('POST', '/v1/projects/%s/tasks' % project, {'title': 'the task'}, token=alex).data['id']
        base = '/v1/projects/%s/tasks/%s' % (project, task)
        self.assertEqual(200, self.request('POST', base + '/claim', token=secret).status)

        def checkpoint(token, previous, summary):
            page = self.request('GET', base + '/brief', token=token).data
            made = self.request('POST', base + '/checkpoints', {
                'schema_version': 1, 'previous': previous, 'activity_cursor': page.get('activity_cursor') or '',
                'source_commit': '', 'branch': '', 'intent': 'do the task', 'acceptance': 'the checks pass',
                'summary': summary, 'next_action': 'wait', 'open_items': [], 'resolved': []}, token=token)
            self.assertEqual(201, made.status, made.data)
            return made.data['checkpoint']['id']

        mine = checkpoint(secret, None, 'stopped')
        said = self.request('POST', base + '/comments', {'text': 'A direction.'}, token=alex)
        self.assertIn(said.status, (200, 201), said.data)
        checkpoint(alex, mine, 'the owner records where things are')

        def action():
            return next(x for x in self.request('GET', '/v1/agents/me/next', token=secret).data['next_actions']
                        if x['task'] == task)
        found = action()
        self.assertTrue(found['newer_activity'])
        self.assertGreaterEqual(found['newer_activity_by_others'], 1)
        self.assertNotIn('Nothing new', found['wake_summary'])


if __name__ == '__main__':
    unittest.main()
