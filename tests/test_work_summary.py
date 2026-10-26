"""Bulk checkpoint summaries and request-local work selection reuse."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import briefing
import lane_state
import work
import work_summary as summary
from requirements import canonical_bytes
from test_briefing import TASK, checkpoint, comment, rows, save_cp


class SummaryTests(unittest.TestCase):
    def fixture(self):
        data = rows()
        save_cp(data, 'cp')
        return data

    def test_closed_request_bounds_duplicates_identity_and_json_flag(self):
        for values in ([], ['x'] * 2, ['x%d' % i for i in range(101)], ['../x'], [None], 'x'):
            with self.subTest(values=values), self.assertRaises(ValueError):
                summary.task_ids(values)
        self.assertEqual(summary.parse_args(['x', '--json', 'y']), ['x', 'y'])
        for args in ([], ['x', '--limit', '2'], ['--owner', 'x']):
            with self.subTest(args=args), self.assertRaises(ValueError):
                summary.parse_args(args)

    def test_invalid_and_help_are_pre_native_and_batch_uses_one_export(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'projects' / 'pp'
            calls = []
            def run(args):
                calls.append(args)
                self.assertEqual(args, ['export', '--all'])
                return '\n'.join(json.dumps(row) for row in self.fixture())
            for args in (['summary'], ['summary', TASK, TASK], ['summary'] + ['x%d' % i for i in range(101)]):
                with self.assertRaises(ValueError):
                    work.execute(path, 'reader', 'work', args, {}, run)
            work.execute(path, 'reader', 'work', ['summary', '--help'], {}, run)
            self.assertEqual(calls, [])
            ids = [TASK] + ['unknown%d' % i for i in range(99)]
            got = work.execute(path, 'reader', 'work', ['summary'] + ids, {}, run)
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(got['items']), 100)
            self.assertTrue(got['items'][0]['found'])

    def test_hidden_anchor_and_merge_slot_match_unknown_without_hiding_visible_types(self):
        # Use an actual existing record family, including the matching label.
        data = self.fixture()
        data[0]['labels'] = ['reference']
        data[0]['comments'].append(comment('record', 'Kind: reference-entry-v1\n{}'))
        from reserved_comments import is_record_anchor
        self.assertTrue(is_record_anchor(data[0]))
        answer = summary.read(data, [TASK, 'unknown'])['items']
        self.assertEqual(dict(answer[0], task='unknown'), answer[1])
        self.assertEqual(summary.read(data, [TASK]), summary.read([], [TASK]))
        data[0].update(id='trial-merge-slot', labels=['gt:slot'])
        self.assertEqual(summary.read(data, ['trial-merge-slot'])['items'][0], summary.missing('trial-merge-slot'))
        for kind in ('event', 'gate'):
            visible = dict(rows()[0], issue_type=kind, comments=[])
            self.assertTrue(summary.read([visible], [TASK])['items'][0]['found'])

    def test_checkpoint_is_linked_not_comment_order_and_state_is_only_declared(self):
        data = self.fixture()
        save_cp(data, 'cp-two', next_action='Read the result')
        state = dict(schema_version=1, task=TASK, checkpoint='cp-two', state='waiting-on-review', waiting_on='reviewer')
        data[0]['comments'].append(comment('lane', lane_state.PREFIX + canonical_bytes(state).decode()))
        data[0]['comments'].reverse()
        got = summary.read(data, [TASK])['items'][0]
        self.assertEqual(got['checkpoint']['comment_id'], 'cp-two')
        self.assertEqual(got['checkpoint']['state'], 'waiting-on-review')
        self.assertEqual(got['status'], 'in_progress')
        data[0]['comments'] = [c for c in data[0]['comments'] if c['id'] != 'lane']
        self.assertEqual(summary.read(data, [TASK])['items'][0]['checkpoint']['state'], 'unknown')

    def test_declared_state_for_an_older_checkpoint_does_not_label_the_selected_one(self):
        data = self.fixture()
        state = dict(schema_version=1, task=TASK, checkpoint='cp', state='working')
        data[0]['comments'].append(comment('lane-old', lane_state.PREFIX + canonical_bytes(state).decode()))
        save_cp(data, 'cp-two')
        selected = summary.read(data, [TASK])['items'][0]['checkpoint']
        self.assertEqual(selected['comment_id'], 'cp-two')
        self.assertEqual(selected['state'], 'unknown')

    def test_record_families_never_win_last_post_and_same_time_uses_id(self):
        data = self.fixture()
        for cid in ('z', 'a'):
            data[0]['comments'].append(comment(cid, 'Ordinary post', stamp='2026-09-16T00:00:00Z'))
        from reserved_comments import PREFIXES
        for index, prefix in enumerate(PREFIXES):
            data[0]['comments'].append(comment('r' + str(index), '\ufeff' + prefix.replace('\n', '\r\n') + '{}',
                                                stamp='2026-09-17T00:00:00Z'))
        got = summary.read(data, [TASK])['items'][0]
        self.assertEqual(got['last_post']['comment_id'], 'z')
        self.assertEqual(got['last_post']['author'], 'alice/session')

    def test_missing_and_broken_checkpoint_coverage_is_not_zero_completion(self):
        data = rows()
        self.assertIsNone(summary.read(data, [TASK])['items'][0]['checkpoint'])
        save_cp(data, 'cp')
        broken = checkpoint(data)
        broken['previous'] = None
        data[0]['comments'].append(comment('fork', briefing.PREFIX + canonical_bytes(broken).decode()))
        got = summary.read(data, [TASK])['items'][0]
        self.assertIsNone(got['checkpoint'])
        self.assertEqual(got['coverage'], 'unknown')
        self.assertEqual(summary.read(data, [TASK, 'absent'])['items'][1], summary.missing('absent'))

    def test_malformed_visible_history_is_unknown_without_poisoning_other_items(self):
        for comments in ([None], [{'id': 'x', 'text': None}], {'id': 'not-a-list'},
                         [comment('x' * 162)], [comment('bad-time', stamp='not-a-time')],
                         [comment('overflow', stamp='0001-01-01T00:00:00+14:00')]):
            data = self.fixture()
            data[0]['comments'] = comments
            other = dict(self.fixture()[0], id='other')
            other['comments'] = []
            with self.subTest(comments=comments):
                got = summary.read(data + [other, {'id': []}], [TASK, 'other'])['items']
                self.assertEqual(got[0]['coverage'], 'unknown')
                self.assertIsNone(got[0]['checkpoint'])
                self.assertEqual(got[1]['coverage'], 'validated')
                self.assertTrue(got[1]['found'])

    def test_help_names_the_enforced_summary_bounds(self):
        help_read = work.help_payload('work')
        self.assertIn('1..%d' % summary.IDS_MAX, help_read['limits']['summary-tasks'])
        self.assertIn(str(summary.NEXT_ACTION_MAX), help_read['limits']['summary-next-action'])
        self.assertIn(str(summary.AUTHOR_MAX), help_read['limits']['summary-author'])

    def test_foreign_project_row_is_identical_to_unknown_at_the_snapshot_boundary(self):
        data = self.fixture()
        data[0]['project_id'] = 'another-project'
        answer = summary.read(data, [TASK, 'absent'], project='this-project')['items']
        self.assertEqual(dict(answer[0], task='absent'), answer[1])

    def test_full_hundred_item_output_keeps_text_and_collection_bounds(self):
        data = []
        for index in range(100):
            row = dict(rows()[0], id='task%d' % index)
            row['comments'] = [comment('post', author='\n' * 1000)]
            data.append(row)
        result = summary.read(data, [row['id'] for row in data])
        self.assertEqual(len(result['items']), 100)
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), 512 * 1024)
        self.assertTrue(all(len(item['last_post']['author']) == summary.AUTHOR_MAX for item in result['items']))

    def test_text_bounds_omissions_input_order_and_no_source_mutation(self):
        data = rows()
        save_cp(data, 'cp', next_action='\u2603\n' * 300)
        data[0]['comments'].append(comment('last', 'Post', author='x' * 120, stamp='2026-09-16T00:00:00Z'))
        before = copy.deepcopy(data)
        result = summary.read(data, ['absent', TASK])
        got = result['items'][1]
        self.assertEqual(got['checkpoint']['next_action'], ('\u2603\n' * 300)[:500])
        self.assertEqual(got['checkpoint']['next_action_omitted_chars'], 100)
        self.assertEqual(got['last_post']['author_omitted_chars'], 24)
        self.assertEqual(data, before)
        self.assertEqual([item['task'] for item in result['items']], ['absent', TASK])
        self.assertNotIn('provenance', json.dumps(result))


class SelectionReuseTests(unittest.TestCase):
    def changed(self):
        data = rows()
        save_cp(data, 'cp')
        data[0]['comments'].append(comment('new', 'New coordinator comment', author='reviewer',
                                            stamp='2026-09-16T00:00:00Z'))
        return data

    def test_one_selection_walk_serves_attention_and_display_for_same_checkpoint(self):
        data = self.changed()
        with mock.patch.object(briefing, 'newer_selection', wraps=briefing.newer_selection) as walk:
            got = work.queue(data, 'alice/session', ['--mine'], operators=['reviewer'])
        self.assertEqual(walk.call_count, 1)
        self.assertTrue(got['items'][0]['assignee_comment_pending'])
        self.assertTrue(got['items'][0]['newer_activity'])

    def test_earlier_owner_checkpoint_is_distinct_and_new_read_rechecks_authority(self):
        data = self.changed()
        current = checkpoint(data)
        data[0]['comments'].append(comment('other-cp', briefing.PREFIX + canonical_bytes(current).decode(),
                                            author='reviewer', stamp='2026-09-17T00:00:00Z'))
        data[0]['comments'].append(comment('later', 'Later coordinator post', author='reviewer',
                                            stamp='2026-09-18T00:00:00Z'))
        with mock.patch.object(briefing, 'newer_selection', wraps=briefing.newer_selection) as walk:
            allowed = work.queue(data, 'alice/session', ['--mine'], operators=['reviewer'])
        self.assertEqual(walk.call_count, 2)
        self.assertTrue(allowed['items'][0]['assignee_comment_pending'])
        denied = work.queue(data, 'alice/session', ['--mine'], operators=[])
        self.assertFalse(denied['items'][0]['assignee_comment_pending'])


class PrototypeHttpSummaryTests(unittest.TestCase):
    def test_portable_http_backend_reads_saved_checkpoint_and_ignores_unusable_lane_state(self):
        from test_http_client import ClientCase, ADMIN, ADMIN_PASSWORD
        harness = ClientCase(methodName='runTest')
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        caller = harness.client()
        caller.login(ADMIN, ADMIN_PASSWORD)
        project = caller.create_project('Synthetic summary')['id']
        task = caller.create_task(project, 'Visible task')['id']
        caller.add_comment(project, task, 'Ordinary post')
        saved = caller.add_checkpoint(project, task, previous=None, summary='Saved checkpoint',
            lane_state=dict(schema_version=1, task=task, state='working'))['checkpoint']
        got = caller.task_summaries(project, [task, 'absent'])['items']
        self.assertEqual(got[0]['checkpoint']['comment_id'], saved['id'])
        self.assertEqual(got[0]['checkpoint']['state'], 'working')
        self.assertEqual(got[0]['checkpoint']['open_items_count'], 0)
        self.assertFalse(got[1]['found'])
        harness.backend.state['checkpoints'][task][-1]['lane_state']['checkpoint'] = 'another'
        got = caller.task_summaries(project, [task])['items'][0]
        self.assertEqual(got['checkpoint']['comment_id'], saved['id'])
        self.assertEqual(got['checkpoint']['state'], 'unknown')


if __name__ == '__main__':
    unittest.main()
