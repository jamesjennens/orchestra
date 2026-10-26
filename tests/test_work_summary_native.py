"""HTTP client -> real host summary, native visibility and fresh membership."""
import contextlib
import io
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import briefing
import http_client
import lane_state
from requirements import canonical_bytes
import test_bd_label_aliases as fixture
import test_web_merge_slot as slot_fixture
from test_coordinator_rules_endpoint import RulesHelpers


@unittest.skipIf(fixture.endpoint is None, 'native summary endpoint requires POSIX')
@unittest.skipIf(fixture.BD is None, 'native summary requires pinned ORCHESTRA_BD_BIN')
class NativeSummaryTests(RulesHelpers, unittest.TestCase):
    web = slot_fixture.NativeWebSlotTests.web

    def post(self, task, body):
        answer = self.r.bd('comments', 'add', task, body, '--author', 'ops')
        self.assertEqual(answer.returncode, 0, answer.stderr)

    def test_http_client_and_cli_preserve_hidden_unknown_filters_and_fresh_membership(self):
        harness, token = self.web()
        admin = http_client.Client('http://127.0.0.1:%d' % harness.port).use_credential(token)
        uid = harness.create_account(token, 'summary-coordinator', 'synthetic-summary-password-1')
        admin.set_member('pp', uid, 'coordinator')
        caller = http_client.Client('http://127.0.0.1:%d' % harness.port)
        caller.login('summary-coordinator', 'synthetic-summary-password-1')
        task = self.task('Summary visible task')
        self.post(task, 'Ordinary visible post')
        row = next(row for row in self.rows() if row['id'] == task)
        snap = briefing.snapshot([row], 'pp', task)
        payload = dict(schema_version=1, task=task, previous=None,
            activity_cursor=briefing.activity_cursor(snap), source_commit='a' * 40,
            branch='synthetic/summary', intent='Synthetic native read', acceptance='No effects',
            summary='Checkpoint', next_action='Review the result', open_items=[], resolved=[])
        self.post(task, briefing.PREFIX + canonical_bytes(payload).decode())
        row = next(row for row in self.rows() if row['id'] == task)
        cp = briefing.checkpoint_state(row)['current'][1]
        declared = dict(schema_version=1, task=task, checkpoint=str(cp['id']),
                        state='waiting-on-review', waiting_on='reviewer')
        self.post(task, lane_state.PREFIX + canonical_bytes(declared).decode())
        hidden_id = 'pp-summary-hidden'
        try:
            unknown_before = caller.task_summaries('pp', [hidden_id])
        except http_client.HttpApiError as error:
            self.fail('An authorized HTTP summary must answer an unknown ID successfully: ' + str(error))
        hidden = self.r.bd('create', 'Hidden record', '--id', hidden_id, '--labels', 'reference', '--json')
        self.assertEqual(hidden.returncode, 0, hidden.stderr)
        hidden = json.loads(hidden.stdout)['id']
        self.assertEqual(hidden, hidden_id)
        self.post(hidden, 'Kind: reference-entry-v1\n{}')
        # Fixture writes use raw bd, outside the kit's committed-write contract.
        # Discard only this fixture's derived cache to prove filtering on fresh rows.
        shutil.rmtree(self.project / '.read-cache', ignore_errors=True)
        before = canonical_bytes(self.rows())
        with mock.patch.object(harness.backend, '_endpoint', wraps=harness.backend._endpoint) as route:
            try:
                result = caller.task_summaries('pp', [task, hidden, 'pp-unknown'])
            except http_client.HttpApiError as error:
                self.fail('An authorized HTTP coordinator summary must answer successfully: ' + str(error))
        self.assertEqual(route.call_count, 1)
        self.assertEqual(route.call_args.args[0], 'work')
        self.assertEqual(route.call_args.args[3], ['summary', task, hidden, 'pp-unknown'])
        visible, private, absent = result['items']
        self.assertEqual(dict(private, task='pp-unknown'), absent)
        self.assertFalse(private['found'])
        self.assertEqual(private, unknown_before['items'][0])
        self.assertEqual(visible['checkpoint']['comment_id'], str(cp['id']))
        self.assertEqual(visible['checkpoint']['state'], 'waiting-on-review')
        self.assertEqual(visible['checkpoint']['next_action'], 'Review the result')
        self.assertEqual(visible['checkpoint']['open_items_count'], 0)
        self.assertNotEqual(visible['last_post']['comment_id'], str(cp['id']))
        self.assertEqual(canonical_bytes(self.rows()), before)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = http_client.main(['--base-url', 'http://127.0.0.1:%d' % harness.port,
                                     '--token=' + caller.token, 'work', 'pp', 'summary', task])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(captured.getvalue())['items'][0], visible)
        with mock.patch.object(harness.backend, 'task_summaries', wraps=harness.backend.task_summaries) as read:
            for tasks in ([], [task, task], ['x'] * 101):
                with self.assertRaises(http_client.HttpApiError):
                    caller.task_summaries('pp', tasks)
            self.assertEqual(read.call_count, 0)
        admin.remove_member('pp', uid)
        with self.assertRaises(http_client.HttpApiError) as refused:
            caller.task_summaries('pp', [task, hidden])
        self.assertIn(refused.exception.status, (403, 404))


if __name__ == '__main__':
    unittest.main()
