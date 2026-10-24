"""The installation operator's host route for a recorded hand-back (kittrial-5bb.358).

`admin.py followon --project P --actor A --file payload.json` records and withdraws
the SAME `assignee-followon-v1` records the confined coordinator's SSH command and the
web route write, through the SAME `followon.apply`, so an installation whose
coordinator works from the host without a bound key or a web account can still record
a hand-back.

The authority gate is the deployment operator allowlist, read strictly
(`admin.operators(root, strict=True)`): `--actor` must itself be listed, the recorded
actor is exactly that name, and a payload that repeats an `actor` must agree with it.
A refused caller writes nothing - no native comment, no host journal entry, and not
even the coordination-lock file.

The real-bd half uses the scratch server-mode tracker and the integrated-task chain of
`test_followon_real_bd.FollowonCase`, so the record's applicability is the real
projection. It asserts the move through `work`, the lane board, and that `wait` wakes
for the assignee; the journal alone would not prove the move.
"""
import contextlib
import io
import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import admin
import followon
import test_bd_label_aliases as fixture
import test_followon_real_bd as real

COORD = real.COORD
WORKER = real.WORKER
PREFIX = 'followon --project PROJECT --actor OPERATOR --file payload.json'
NO_MOVE_SENTENCE = ('A newer coordinator comment on an open assigned task requires the assignee to read and acknowledge it; '
                    'a recorded hand-back separately holds the implementation move until resolved.')


class FollowonAdminDocsTests(unittest.TestCase):
    """The command is documented in the three documents the task names."""

    def text(self, name):
        return (KIT / 'docs' / name).read_text(encoding='utf-8-sig')

    def test_the_three_documents_carry_the_host_command(self):
        for name in ('OPERATIONS.md', 'CLI_CONTRACT.md', 'REVIEWS.md'):
            with self.subTest(document=name):
                text = self.text(name)
                self.assertIn(PREFIX, text)
                self.assertIn('admin.py', text)
                # The gate the command actually applies, named plainly.
                self.assertIn('operator allowlist', text)
                self.assertIn('followon.apply', text)

    def test_the_coordinator_documents_distinguish_attention_from_a_handback(self):
        # Comment attention requires a read/acknowledgement; it creates no journalled
        # hand-back. Both coordinator documents explain the independent rules.
        for name in ('OPERATIONS.md', 'REVIEWS.md'):
            with self.subTest(document=name):
                self.assertIn(NO_MOVE_SENTENCE, self.text(name))


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class FollowonAdminCommandTests(real.FollowonCase):
    """Record/withdraw from the host, and the refused callers around the gate."""

    # -- the host command -------------------------------------------------------
    def cli(self, payload, actor=COORD):
        """Run `admin.py followon` in-process; returns the JSON it printed."""
        path = self.root / ('followon-payload-' + uuid.uuid4().hex + '.json')
        path.write_text(json.dumps(payload), encoding='utf-8')
        out, err = io.StringIO(), io.StringIO()
        argv = ['admin.py', '--root', str(self.root), 'followon', '--project', 'pp',
                '--actor', actor, '--file', str(path)]
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(admin, 'root_path', return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            admin.main()
        return json.loads(out.getvalue())

    def refusal(self, payload, actor=COORD):
        """The exact sentence `admin.py followon` refuses this payload with."""
        path = self.root / ('followon-payload-' + uuid.uuid4().hex + '.json')
        path.write_text(json.dumps(payload), encoding='utf-8')
        out = io.StringIO()
        argv = ['admin.py', '--root', str(self.root), 'followon', '--project', 'pp',
                '--actor', actor, '--file', str(path)]
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(admin, 'root_path', return_value=self.root), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(ValueError) as refused:
                admin.main()
        return str(refused.exception)

    # -- the shared reads -------------------------------------------------------
    def journal_names(self):
        directory = self.project / followon.JOURNAL_DIR
        return sorted(path.name for path in directory.glob('*.json')) if directory.is_dir() else []

    def journal_entry(self, comment_id):
        entries, invalid = followon.host_journal(self.project)
        self.assertEqual(invalid, [])
        entry = entries.get(comment_id)
        self.assertIsNotNone(entry, 'no host journal entry for native comment ' + str(comment_id))
        return entry

    def wait_items(self, actor=WORKER):
        reply = self.endpoint('wait', actor, [])
        self.assertEqual(reply['returncode'], 0, reply)
        return json.loads(reply['stdout'])['items']

    def lane_entry(self, task, actor=WORKER):
        reply = self.endpoint('work', actor, ['lanes', '--json'])
        self.assertEqual(reply['returncode'], 0, reply)
        for lane in json.loads(reply['stdout'])['items']:
            if lane.get('actor') != actor:
                continue
            for entry in lane.get('held') or []:
                if entry.get('task') == task:
                    return entry
        self.fail('task %s is not on lane %s' % (task, actor))

    # -- tests ------------------------------------------------------------------
    def test_an_operator_records_and_withdraws_and_the_move_changes(self):
        task = self.integrated_task()
        self.acknowledge_comments(task)
        text = 'Remaining scope: finish the release notes and the run book.'
        # Baseline: integrated-but-open is the coordinator's move, so wait sleeps.
        self.assertNotIn(task, [item['task'] for item in self.wait_items()])

        # The payload may repeat the actor, but only AGREEING with --actor: it is
        # checked, dropped from the request shape, and the recorded actor is --actor.
        recorded = self.cli({'schema_version': 1, 'operation': 'record', 'task': task,
                             'text': text, 'actor': COORD})
        self.assertEqual((recorded['operation'], recorded['task']), ('record', task))
        entry = self.journal_entry(recorded['comment_id'])
        self.assertEqual(entry['actor'], COORD)
        self.assertEqual(entry['payload']['text'], text)

        # work: the move is the assignee's, with the recorded text quoted.
        row = self.row(task)
        self.assertEqual(row['followon']['id'], recorded['comment_id'])
        self.assertEqual(row['followon']['actor'], COORD)
        self.assertTrue(row['followon']['applies'])
        self.assertEqual((row['whose_move'], row['next_actor']), ('you', 'assignee'))
        self.assertIn(text, row['next'])

        # lanes: the SAME block through the ONE move decision.
        lane = self.lane_entry(task)
        self.assertEqual((lane['whose_move'], lane['next_actor']), ('you', 'assignee'))
        self.assertIn(text, lane['next'])

        # wait wakes for the assignee without any new rule.
        self.assertIn(task, [item['task'] for item in self.wait_items()])

        # Withdraw through the host command: the move returns to the coordinator.
        withdrawn = self.cli({'schema_version': 1, 'operation': 'withdraw',
                              'target': recorded['comment_id'],
                              'reason': 'The coordinator kept the scope.'})
        self.assertEqual((withdrawn['operation'], withdrawn['target']),
                         ('withdraw', recorded['comment_id']))
        self.assertEqual(self.journal_entry(withdrawn['comment_id'])['actor'], COORD)
        self.acknowledge_comments(task)
        row = self.row(task)
        self.assertEqual((row['whose_move'], row['next_actor']), ('coordinator', 'coordinator'))
        self.assertEqual(row['followon']['state'], 'withdrawn')
        self.assertEqual(self.lane_entry(task)['whose_move'], 'coordinator')
        self.assertNotIn(task, [item['task'] for item in self.wait_items()])
        # Both records stay in native history.
        native = self.comment_ids(task)
        self.assertIn(recorded['comment_id'], native)
        self.assertIn(withdrawn['comment_id'], native)

    def test_a_non_operator_actor_is_refused_and_writes_nothing(self):
        task = self.integrated_task()
        self.acknowledge_comments(task)
        before_ids = self.comment_ids(task)
        before_journal = self.journal_names()
        lock = self.project / '.coordination.lock'
        lock.unlink(missing_ok=True)

        message = self.refusal({'schema_version': 1, 'operation': 'record', 'task': task,
                                'text': 'A worker may not hand this back.'}, actor=WORKER)
        self.assertIn('Actor ' + WORKER + ' is not a server-side configured operator', message)
        self.assertIn('only a configured operator may record or withdraw a hand-back', message)

        self.assertEqual(self.comment_ids(task), before_ids)
        self.assertEqual(self.journal_names(), before_journal)
        self.assertFalse(lock.exists(),
                         'a refused caller must not create the coordination lock either')
        self.assertEqual(self.row(task)['whose_move'], 'coordinator')
        self.assertNotIn(task, [item['task'] for item in self.wait_items()])

    def test_a_payload_naming_a_different_actor_is_refused_and_writes_nothing(self):
        task = self.integrated_task()
        before_ids = self.comment_ids(task)
        before_journal = self.journal_names()
        lock = self.project / '.coordination.lock'
        lock.unlink(missing_ok=True)

        message = self.refusal({'schema_version': 1, 'operation': 'record', 'task': task,
                                'text': 'Attributed to somebody else.', 'actor': WORKER})
        self.assertIn('The hand-back payload names actor ' + repr(WORKER), message)
        self.assertIn('but --actor is ' + repr(COORD), message)
        self.assertIn('Nothing was written.', message)

        self.assertEqual(self.comment_ids(task), before_ids)
        self.assertEqual(self.journal_names(), before_journal)
        self.assertFalse(lock.exists(), 'the mismatched payload must be refused before the lock')

    def test_the_allowlist_is_read_again_inside_the_lock(self):
        """kittrial-5bb.361 (review of .358, mutant F2): the operator allowlist read inside the
        lock decides. An operator removed between the pre-lock check and the lock is refused
        and nothing is written."""
        task = self.integrated_task()
        before_ids = self.comment_ids(task)
        before_journal = self.journal_names()
        real_operators = admin.operators
        calls = []

        def changing(root, **kwargs):
            calls.append(kwargs)
            found = real_operators(root, **kwargs)
            # The first strict read (before the lock) still lists COORD; every later one does not.
            return found if len(calls) == 1 else frozenset(found) - {COORD}

        with mock.patch.object(admin, 'operators', side_effect=changing):
            message = self.refusal({'schema_version': 1, 'operation': 'record', 'task': task,
                                    'text': 'Removed from the allowlist meanwhile.'})
        self.assertGreaterEqual(len(calls), 2, 'the allowlist must be read again inside the lock')
        # COORD is this fixture's only operator, so the in-lock reading is an empty allowlist
        # and the refusal is the one an empty allowlist gets.
        self.assertEqual('No operator allowlist is configured on the coordination host; add the acting operator '
                         'to deployment.private.json before you record or withdraw a hand-back', message)
        self.assertEqual(self.comment_ids(task), before_ids)
        self.assertEqual(self.journal_names(), before_journal)

    def test_a_symlinked_coordination_lock_is_refused(self):
        """kittrial-5bb.361 (review of .358, mutant F6): a .coordination.lock that is a symlink
        is refused before anything is opened through it; nothing is written and the link's
        target is not created."""
        task = self.integrated_task()
        before_ids = self.comment_ids(task)
        before_journal = self.journal_names()
        lock = self.project / '.coordination.lock'
        lock.unlink(missing_ok=True)
        target = self.root / ('elsewhere-' + uuid.uuid4().hex)
        lock.symlink_to(target)
        self.addCleanup(lambda: lock.unlink(missing_ok=True))
        message = self.refusal({'schema_version': 1, 'operation': 'record', 'task': task,
                                'text': 'Through a planted link.'})
        self.assertEqual('Coordination lock must not be a symlink', message)
        self.assertFalse(target.exists(), 'nothing may be created through the link')
        self.assertEqual(self.comment_ids(task), before_ids)
        self.assertEqual(self.journal_names(), before_journal)

    def test_the_admin_route_adds_no_bypass_of_followons_own_refusals(self):
        task = self.integrated_task()
        before_ids = self.comment_ids(task)
        before_journal = self.journal_names()

        # An unknown task: followon.apply's own missing-task refusal, not a new one.
        message = self.refusal({'schema_version': 1, 'operation': 'record',
                                'task': 'no-such-task-' + uuid.uuid4().hex[:6], 'text': 'x'})
        self.assertIn('is missing, duplicated or is an event', message)
        # A withdrawal with no reason: the library's request gate.
        message = self.refusal({'schema_version': 1, 'operation': 'withdraw',
                                'target': 'fo1', 'reason': '   '})
        self.assertIn('A withdrawal reason must be nonempty text', message)
        # A server-derived fact cannot be supplied through the host route either.
        message = self.refusal({'schema_version': 1, 'operation': 'record', 'task': task,
                                'text': 'x', 'commit': 'a' * 40})
        self.assertIn('carries a field it does not take: commit', message)

        self.assertEqual(self.comment_ids(task), before_ids)
        self.assertEqual(self.journal_names(), before_journal)


if __name__ == '__main__':
    unittest.main()
