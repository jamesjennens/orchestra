"""Real bd evidence for the lane-state record and the lanes read (kittrial-5bb.263 review).

Opt in with ``ORCHESTRA_BD_BIN`` (and a sibling ``dolt``), exactly as
``tests/test_checkpoint_real_bd.py`` and the ``tests/test_bd_label_aliases.py`` fixture do.
Nothing here uses a live runtime: the fixture starts a scratch Dolt server under its own
temporary HOME and initializes a fresh tracker in it; every write goes through the real
``bd`` binary, so the two writes of one checkpoint, the identity of the retry and the
reading of the record are the binary's own behaviour, not a stub's.

The released-kit half is opt in with ``ORCHESTRA_LANES_RELEASED_KIT``, a checkout of the
release the change is a revision of (16e68c7 at the time of writing). It shows the one
documented rollback reading: a kit that does not know the record reads the checkpoint as
stale -- and still reads the task, with no malformed-checkpoint warning.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import briefing as b
import lane_state
import lanes
import test_bd_label_aliases as rb
import work

RELEASED = os.environ.get('ORCHESTRA_LANES_RELEASED_KIT')
RELEASED_READ = '''
import json, sys
sys.path.insert(0, sys.argv[1])
import briefing as b
rows = [json.loads(line) for line in open(sys.argv[2], encoding='utf-8') if line.strip()]
read = b.brief(rows, 'pp', sys.argv[3])
checkpoint = read['checkpoint'] or {}
print(json.dumps({'checkpoint': bool(read['checkpoint']),
                  'newer_activity': checkpoint.get('newer_activity'),
                  'warnings': read['warnings']}))
'''


@unittest.skipIf(rb.endpoint is None, 'real bd lane test needs POSIX endpoint imports')
@unittest.skipIf(rb.BD is None, 'real bd lane test needs ORCHESTRA_BD_BIN or bd on PATH')
class RealBdLaneStateTests(unittest.TestCase):
    bd = rb.RealBdLabelAliasTests.bd
    export_rows = rb.RealBdLabelAliasTests.export_rows

    @classmethod
    def setUpClass(cls):
        # Reuse the label-alias fixture's scratch server-mode tracker. Its setUpClass runs on
        # its OWN class, so the attributes it creates are copied here; subclassing it would
        # also re-run the label-alias tests a second time under this class.
        base = rb.RealBdLabelAliasTests
        base.setUpClass()
        for name in ('_tmp', '_signals', 'root', 'bd_path', 'port', 'project', 'server'):
            setattr(cls, name, getattr(base, name))

    @classmethod
    def tearDownClass(cls):
        rb.RealBdLabelAliasTests.tearDownClass()

    def create(self, title='Lane task'):
        result = self.bd('create', title, '--assignee', 'op', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['id']

    def writer(self, actor='op'):
        """The endpoint's native runner shape: the audit actor AND the comment author."""
        def run(args):
            argv = list(args)
            if argv[:2] == ['comments', 'add']:
                argv += ['--author', actor]
            result = self.bd(*argv, actor=actor)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout
        return run

    def payload(self, task, previous=None, **lane):
        data = self.export_rows()
        read = b.brief(data, 'pp', task)
        state = dict(schema_version=1, task=task, state='waiting-on-coordinator', waiting_on='coordinator',
                     expect_by='2020-01-01T00:00:00+00:00')
        state.update(lane)
        return dict(schema_version=1, task=task,
                    previous=previous if previous is not None else (read['checkpoint'] or {}).get('comment_id'),
                    activity_cursor=read['activity_cursor'], source_commit='', branch='', intent='lane state',
                    acceptance='the record is written with the checkpoint', summary='declared wait',
                    next_action='continue', open_items=[], resolved=[], lane_state=state)

    def lane_records(self, task):
        row = b.task_row(self.export_rows(), task)
        return [c for c in row.get('comments') or [] if lane_state.is_record(c.get('text'))]

    def held(self, board, task):
        for item in board['items']:
            for entry in item['held']:
                if entry['task'] == task:
                    return item, entry
        raise AssertionError('task %s is not on the board' % task)

    def test_the_write_the_retry_the_crash_the_brief_and_the_lanes_read(self):
        task = self.create()
        run = self.writer()
        payload = self.payload(task)
        first = b.save_checkpoint(self.export_rows(), 'pp', task, payload, 'op', run)
        self.assertFalse(first['reconciled'])
        self.assertFalse(first['lane_state']['reconciled'])
        records = self.lane_records(task)
        self.assertEqual(len(records), 1)
        record = lane_state.parse(records[0]['text'])
        self.assertEqual(record['checkpoint'], first['comment_id'])
        self.assertEqual(record['state'], 'waiting-on-coordinator')
        # The record must not make its own checkpoint read stale, in the brief ...
        read = b.brief(self.export_rows(), 'pp', task)
        self.assertFalse(read['checkpoint']['newer_activity'], read['checkpoint'])
        # ... or in the queue's own/other counts (the in-progress flag).
        fields = b.checkpoint_queue_fields(self.export_rows(), b.task_row(self.export_rows(), task))
        self.assertEqual((fields['newer_activity_by_others'], fields['newer_activity_own'],
                          fields['newer_activity_coverage']), (0, 0, 'current'))
        # ... and the queue's row-level wake flag agrees.
        row = [item for item in work.queue(self.export_rows(), 'op', [])['items']
               if item['task'] == task][0]
        self.assertFalse(row['newer_activity'])
        # An identical retry reconciles both records and publishes nothing.
        retry = b.save_checkpoint(self.export_rows(), 'pp', task, payload, 'op', run)
        self.assertTrue(retry['reconciled'])
        self.assertTrue(retry['lane_state']['reconciled'])
        self.assertEqual(retry['comment_id'], first['comment_id'])
        self.assertEqual(retry['lane_state']['comment_id'], first['lane_state']['comment_id'])
        self.assertEqual(len(self.lane_records(task)), 1)
        # The lanes read over the real export: one lane, the declared state, its age from
        # the checkpoint, and the passed expect_by flagged.
        board = lanes.read(self.export_rows(), 'op', {}, ['--json'], operators=['op'],
                           journal=None, scope='all')
        lane_row, entry = self.held(board, task)
        self.assertTrue(entry['declared'])
        self.assertEqual((entry['state'], entry['waiting_on']), ('waiting-on-coordinator', 'coordinator'))
        self.assertEqual(entry['checkpoint'], first['comment_id'])
        checkpoint_comment = next(c for c in b.task_row(self.export_rows(), task)['comments']
                                  if str(c['id']) == first['comment_id'])
        self.assertEqual(entry['state_since'], checkpoint_comment['created_at'])
        self.assertIn('expect_by_passed', entry['flags'])
        own = lanes.read(self.export_rows(), 'someone-else', {}, ['--json'], operators=['op'],
                         journal=None, scope='own')
        self.assertEqual(own['total'], 0)

    def test_a_crash_between_the_two_writes_is_completed_by_the_retry(self):
        task = self.create('Crash task')
        run = self.writer()
        payload = self.payload(task)
        with mock.patch.object(lane_state, 'ensure', side_effect=RuntimeError('crash between the two writes')):
            with self.assertRaises(RuntimeError):
                b.save_checkpoint(self.export_rows(), 'pp', task, payload, 'op', run)
        # The checkpoint WAS published; the record was not.
        current, invalid = b.checkpoints(b.task_row(self.export_rows(), task))
        self.assertEqual(invalid, [])
        self.assertIsNotNone(current)
        self.assertEqual(self.lane_records(task), [])
        # The retry reconciles the checkpoint and completes the missing record.
        retry = b.save_checkpoint(self.export_rows(), 'pp', task, payload, 'op', run)
        self.assertTrue(retry['reconciled'])
        self.assertFalse(retry['lane_state']['reconciled'])
        records = self.lane_records(task)
        self.assertEqual(len(records), 1)
        self.assertEqual(lane_state.parse(records[0]['text'])['checkpoint'], retry['comment_id'])
        self.assertFalse(b.brief(self.export_rows(), 'pp', task)['checkpoint']['newer_activity'])

    def test_the_real_queue_marker_moves_with_each_other_party_comment_and_resets(self):
        """kittrial-5bb.351: the marker is computed from a real bd export, provenance on."""
        task = self.create('Marker task')
        run = self.writer('op')
        first = b.save_checkpoint(self.export_rows(), 'pp', task, self.payload(task), 'op', run,
                                  provenance_writes=True)
        self.assertFalse(first['reconciled'])
        other = self.writer('coordinator/session')
        other(['comments', 'add', task, 'First direction', '--json'])
        row = next(item for item in work.queue(self.export_rows(), 'op', [])['items']
                   if item['task'] == task)
        marker = row['newer_activity_marker']
        self.assertTrue(marker.startswith(task + '-c'), marker)
        self.assertEqual((row['newer_activity_by_others'], row['newer_activity_coverage']),
                         (1, 'snapshot'))
        other(['comments', 'add', task, 'Second direction', '--json'])
        row = next(item for item in work.queue(self.export_rows(), 'op', [])['items']
                   if item['task'] == task)
        self.assertNotEqual(row['newer_activity_marker'], marker)
        self.assertTrue(row['newer_activity_marker'].startswith(task + '-c'))
        self.assertEqual(row['newer_activity_by_others'], 2)
        # A checkpoint that incorporates both clears the marker to null.
        b.save_checkpoint(self.export_rows(), 'pp', task, self.payload(task), 'op',
                          self.writer('op'), provenance_writes=True)
        row = next(item for item in work.queue(self.export_rows(), 'op', [])['items']
                   if item['task'] == task)
        self.assertIsNone(row['newer_activity_marker'])
        self.assertEqual(row['newer_activity_coverage'], 'current')

    @unittest.skipIf(not RELEASED, 'released checkout not configured (ORCHESTRA_LANES_RELEASED_KIT)')
    def test_the_released_kit_reads_the_task_and_reports_the_documented_stale_reading(self):
        task = self.create('Released kit task')
        run = self.writer()
        first = b.save_checkpoint(self.export_rows(), 'pp', task, self.payload(task), 'op', run)
        self.assertFalse(first['reconciled'])
        export = Path(self.root) / 'lanes-export.jsonl'
        export.write_text('\n'.join(json.dumps(row) for row in self.export_rows()), encoding='utf-8')
        done = subprocess.run([sys.executable, '-c', RELEASED_READ, RELEASED, str(export), task],
                              capture_output=True, text=True, cwd='/')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        read = json.loads(done.stdout.strip().splitlines()[-1])
        # The task is readable: a valid checkpoint and no malformed-checkpoint warning.
        self.assertTrue(read['checkpoint'])
        self.assertFalse(any('Malformed checkpoint' in warning for warning in read['warnings']))
        # The record is an ordinary comment to it, so it cannot know it belongs to the
        # checkpoint and reports the checkpoint stale (docs/LANE_BOARD_DESIGN.md section 5).
        self.assertTrue(read['newer_activity'])


if __name__ == '__main__':
    unittest.main()
