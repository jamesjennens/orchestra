"""Real-bd integration tests for Slice 1 server-computed waits (`waiting` action, kittrial-5bb.598).

Uses the same scratch server-mode Dolt/bd fixture as ``tests/test_lanes_real_bd.py``
(opt in via ``ORCHESTRA_BD_BIN`` or ``bd`` on PATH; skipped on Windows where endpoint
imports ``fcntl``). Derives all timestamps relative to ``now`` (never hard-coded dates
compared against the real clock).
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import briefing as b
import test_bd_label_aliases as rb
import waits
import work


@unittest.skipIf(rb.endpoint is None, 'real bd waits test needs POSIX endpoint imports')
@unittest.skipIf(rb.BD is None, 'real bd waits test needs ORCHESTRA_BD_BIN or bd on PATH')
class RealBdWaitingTests(unittest.TestCase):
    bd = rb.RealBdLabelAliasTests.bd
    export_rows = rb.RealBdLabelAliasTests.export_rows

    @classmethod
    def setUpClass(cls):
        base = rb.RealBdLabelAliasTests
        base.setUpClass()
        for name in ('_tmp', '_signals', 'root', 'bd_path', 'port', 'project', 'server'):
            setattr(cls, name, getattr(base, name))

    @classmethod
    def tearDownClass(cls):
        rb.RealBdLabelAliasTests.tearDownClass()

    def writer(self, actor):
        def run(args):
            argv = list(args)
            if argv[:2] == ['comments', 'add']:
                argv += ['--author', actor]
            result = self.bd(*argv, actor=actor)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout
        return run

    def test_open_task_wait_ack_verdict_and_metrics_over_real_bd(self):
        # Create an OPEN (not in_progress) task assigned to lane-1 (the .583 shape).
        created = self.bd('create', 'Open wait task', '--assignee', 'lane-1', '--json')
        self.assertEqual(created.returncode, 0, created.stderr)
        task_id = json.loads(created.stdout)['id']

        now = datetime.now(timezone.utc)
        past_expect = (now - timedelta(hours=1)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')

        data = self.export_rows()
        read = b.brief(data, 'pp', task_id)
        payload = {
            'schema_version': 1,
            'task': task_id,
            'previous': None,
            'activity_cursor': read['activity_cursor'],
            'source_commit': '',
            'branch': '',
            'intent': 'plan review',
            'acceptance': 'verified',
            'summary': 'waiting on coordinator',
            'next_action': 'await verdict',
            'open_items': [{'id': 'p1', 'kind': 'decision', 'text': 'verify plan', 'source': 'plan.md'}],
            'resolved': [],
            'lane_state': {
                'schema_version': 1,
                'task': task_id,
                'state': 'waiting-on-coordinator',
                'waiting_on': 'coordinator',
                'expect_by': past_expect,
            },
        }
        b.save_checkpoint(data, 'pp', task_id, payload, 'lane-1', self.writer('lane-1'))

        # Coordinator runs `waiting --on-me` through work.execute (shared cached export path).
        res = work.execute(
            self.project, 'coord-1', 'waiting', ['--on-me', '--json'],
            {}, self.writer('coord-1'), operators=['coord-1'],
        )
        self.assertEqual([w['task'] for w in res['items']], [task_id])
        self.assertTrue(res['items'][0]['overdue'])
        self.assertEqual(res['items'][0]['status'], 'open')
        # Plain read never writes .wait-escalations/.
        self.assertFalse((self.project / waits.ESCALATION_LEDGER_DIR).exists())

        # Coordinator posts a plain acknowledgement -> wait stays open!
        ack = self.bd('comments', 'add', task_id, 'Coordinator: received r1; for .511: VERIFIED earlier',
                      '--author', 'coord-1', actor='coord-1')
        self.assertEqual(ack.returncode, 0, ack.stderr)
        res_ack = work.execute(
            self.project, 'coord-1', 'waiting', ['--on-me'],
            {}, self.writer('coord-1'), operators=['coord-1'],
        )
        self.assertEqual([w['task'] for w in res_ack['items']], [task_id])

        # Coordinator posts a case-insensitive header verdict -> wait closes!
        verdict = self.bd('comments', 'add', task_id, 'Kittrial coordinator, 2026-11-23 00:52Z: VERIFIED',
                          '--author', 'coord-1', actor='coord-1')
        self.assertEqual(verdict.returncode, 0, verdict.stderr)
        res_done = work.execute(
            self.project, 'coord-1', 'waiting', ['--on-me'],
            {}, self.writer('coord-1'), operators=['coord-1'],
        )
        self.assertEqual(res_done['items'], [])

        # Lanes board clears stale waiting flags after the verdict too.
        board = work.execute(
            self.project, 'coord-1', 'work', ['lanes', '--idle-minutes', '1'],
            {}, self.writer('coord-1'), operators=['coord-1'],
        )
        held = next(h for row in board['items'] for h in row['held'] if h['task'] == task_id)
        self.assertNotIn('waiting_too_long', held['flags'])
        self.assertNotIn('expect_by_passed', held['flags'])

        # Metrics record the answered wait under both by_target and by_actor.
        metrics = work.execute(
            self.project, 'coord-1', 'waiting', ['--metrics', '--days=7'],
            {}, self.writer('coord-1'), operators=['coord-1'],
        )
        self.assertTrue(any(x['waiting_on'] == 'coordinator' and x['answered']['count'] >= 1
                            for x in metrics['by_target']))
        self.assertTrue(any(x['actor'] == 'coord-1' and x['answered']['count'] >= 1
                            for x in metrics['by_actor']))


if __name__ == '__main__':
    unittest.main()
