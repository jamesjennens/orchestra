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

        # Contributor scope='own' hides another lane's wait over real bd (MUST-2 / M11).
        other_view = work.execute(
            self.project, 'lane-2', 'waiting', ['--all'],
            {}, self.writer('lane-2'), operators=['coord-1'], lanes_scope='own',
        )
        self.assertEqual(other_view['scope'], 'own')
        self.assertNotIn(task_id, [w['task'] for w in other_view['items']])
        self.assertNotIn(task_id, [s['task'] for s in other_view['silent']])

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

    def test_waiting_snooze_over_real_bd_endpoint(self):
        """Slice 2 real-bd test: endpoint `waiting snooze`, journal, anti-self-suppression, 422/403/409, raw refusal."""
        import wait_snooze
        created = self.bd('create', 'Snooze real-bd task', '--assignee', 'lane-s2', '--json')
        self.assertEqual(created.returncode, 0, created.stderr)
        task_id = json.loads(created.stdout)['id']

        now = datetime.now(timezone.utc)
        past_expect = (now - timedelta(hours=1)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        future1 = (now + timedelta(hours=6)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        future2 = (now + timedelta(hours=12)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        future3 = (now + timedelta(hours=18)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')

        data = self.export_rows()
        read = b.brief(data, 'pp', task_id)
        payload = {
            'schema_version': 1, 'task': task_id, 'previous': None,
            'activity_cursor': read['activity_cursor'], 'source_commit': '', 'branch': '',
            'intent': 'snooze test', 'acceptance': 'done',
            'summary': 'waiting on coordinator', 'next_action': 'wait',
            'open_items': [], 'resolved': [],
            'lane_state': {
                'schema_version': 1, 'task': task_id, 'state': 'waiting-on-coordinator',
                'waiting_on': 'coordinator', 'expect_by': past_expect,
            },
        }
        b.save_checkpoint(data, 'pp', task_id, payload, 'lane-s2', self.writer('lane-s2'))

        # Register a real bound contributor session via endpoint, and configure coord-1 as operator.
        reg_resp = rb.endpoint.execute(self.root, {
            'project': self.project.name, 'actor': '', 'action': 'session',
            'args': ['register', '--name', 'lane-s2-contrib', '--request-id', '11111111-2222-4333-8444-555555555555'],
        }, key_principal='lane:lane-s2')
        self.assertEqual(reg_resp['returncode'], 0, reg_resp)
        reg_data = json.loads(reg_resp['stdout'])
        bound_actor = reg_data['session']['actor']
        dep_file = self.root / 'deployment.private.json'
        orig_dep = dep_file.read_text(encoding='utf-8')
        dep_doc = json.loads(orig_dep)
        dep_doc['operators'] = ['coord-1']
        dep_file.write_text(json.dumps(dep_doc, indent=2) + '\n', encoding='utf-8')
        try:
            # 1. Missing --until refused 422.
            r_bad = rb.endpoint.execute(self.root, {
                'project': self.project.name, 'actor': 'coord-1', 'action': 'waiting',
                'args': ['snooze', task_id, '--reason', 'missing until'],
            })
            self.assertEqual(r_bad['returncode'], 2, r_bad)
            self.assertEqual(r_bad.get('snooze_status'), 422)

            # 2. Bound non-operator contributor key refused 403.
            r_forb = rb.endpoint.execute(self.root, {
                'project': self.project.name, 'actor': bound_actor, 'action': 'waiting',
                'args': ['snooze', task_id, '--until', future1, '--reason', 'not coord'],
            }, key_principal='lane:lane-s2')
            self.assertEqual(r_forb['returncode'], 2, r_forb)
            self.assertEqual(r_forb.get('snooze_status'), 403)

            # 3. 1st snooze succeeds via operator, writes .wait-snoozes/ + comment, anti-self-suppression active.
            r1 = rb.endpoint.execute(self.root, {
                'project': self.project.name, 'actor': 'coord-1', 'action': 'waiting',
                'args': ['snooze', task_id, '--until', future1, '--reason', 'Waiting on 11-26 batch'],
            })
            self.assertEqual(r1['returncode'], 0, r1)
            out1 = json.loads(r1['stdout'])
            self.assertEqual(out1['snooze_count'], 1)
            self.assertTrue(out1['waited_on_party'])
            self.assertFalse(out1['suppresses_owner_notice'])
            self.assertTrue(out1['comment_id'])
            self.assertTrue((self.project / wait_snooze.JOURNAL_DIR).is_dir())

            # 4. Read still shows the wait transparently with snoozed=True.
            r_read = rb.endpoint.execute(self.root, {
                'project': self.project.name, 'actor': 'coord-1', 'action': 'waiting',
                'args': ['--on-me', '--json'],
            })
            self.assertEqual(r_read['returncode'], 0, r_read)
            listed = json.loads(r_read['stdout'])
            row = next(w for w in listed['items'] if w['task'] == task_id)
            self.assertTrue(row['snoozed'])
            self.assertEqual(row['snooze_count'], 1)
            self.assertEqual(row['snooze']['reason'], 'Waiting on 11-26 batch')

            # 5. Raw comments add with Kind: wait-snooze-v1 is refused.
            with self.assertRaises(ValueError):
                rb.endpoint.execute(self.root, {
                    'project': self.project.name, 'actor': 'coord-1', 'action': 'bd',
                    'args': ['comments', 'add', task_id, 'Kind: wait-snooze-v1\n{"forged":true}'],
                })

            # 6. 2nd snooze succeeds; 3rd snooze refused 409.
            r2 = rb.endpoint.execute(self.root, {
                'project': self.project.name, 'actor': 'coord-1', 'action': 'waiting',
                'args': ['snooze', task_id, f'--until={future2}', '--reason=Second renewal'],
            })
            self.assertEqual(r2['returncode'], 0, r2)
            self.assertEqual(json.loads(r2['stdout'])['snooze_count'], 2)

            r3 = rb.endpoint.execute(self.root, {
                'project': self.project.name, 'actor': 'coord-1', 'action': 'waiting',
                'args': ['snooze', task_id, f'--until={future3}', '--reason=Third renewal'],
            })
            self.assertEqual(r3['returncode'], 2, r3)
            self.assertEqual(r3.get('snooze_status'), 409)
        finally:
            dep_file.write_text(orig_dep, encoding='utf-8')


if __name__ == '__main__':
    unittest.main()
