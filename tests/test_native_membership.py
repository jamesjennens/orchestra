"""Ordinary route tests from pinned bd 1.2.2 synthetic answer shapes.

The closed event fields below were captured by sol-1's probe-second.log on
2026-10-06 (probe-lrr.1). IDs/times stay synthetic; title/type/labels and metadata
are varied explicitly to test authority boundaries. These are replay tests, not
claims that a native process ran during the ordinary suite. The optional real-bd
suite independently covers creation, type transitions and the full runtime.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import lifecycle
import record_json
import reference_records as references
import requirement_records as requirements
from test_lifecycle import NativeStore, payload


CAPTURED_EVENT = {
    'id': 'probe-lrr.1', 'title': 'State change: tested → passed',
    'description': 'Set tested to passed\n\nReason: probe', 'status': 'closed',
    'priority': 4, 'issue_type': 'event', 'created_at': '2026-10-06T11:21:38Z',
    'created_by': 'ops', 'updated_at': '2026-10-06T11:21:38Z',
    'closed_at': '2026-10-06T11:21:39Z',
    'dependencies': [{'issue_id': 'probe-lrr.1', 'depends_on_id': 'probe-lrr',
                      'type': 'parent-child', 'created_at': '2026-10-06T11:21:38Z',
                      'created_by': 'ops', 'metadata': '{}'}],
    'dependency_count': 0, 'dependent_count': 0, 'comment_count': 0,
    'parent': 'probe-lrr',
}


def deep_answer(row):
    """The exact native metadata value used by the real probe, nested 751 times."""
    return json.dumps(row)[:-1] + ',"metadata":{"deep":' + '[' * 751 + '0' + ']' * 751 + '}}'


class Answers:
    """Replay read answers only; an unexpected native mutation fails immediately."""
    def __init__(self, exported, selected=None, shown=None, comments=None):
        self.exported = exported
        self.selected = selected or {}
        self.shown = shown
        self.comments = comments or {}
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args == ['export', '--all']:
            return self.exported
        if args[0] == 'list':
            # Membership must include closed rows and remove the default limit.
            if args[-4:] != ['--all', '--limit', '0', '--json']:
                return '[]'
            return self.selected.get(tuple(args[1:-4]), '[]')
        if args[0] == 'show' and self.shown is not None:
            return self.shown
        if args[0] == 'comments':
            return self.comments.get(args[1], '[]')
        raise AssertionError('Unexpected native command (including writes): %r' % args)


class MembershipReplayTests(unittest.TestCase):
    def event_answers(self, member=True):
        raw = deep_answer(CAPTURED_EVENT)
        return Answers(raw + '\n', {('--type', 'event'): '[' + raw + ']' if member else '[]'})

    def test_unreadable_export_is_marked_by_complete_native_type_membership(self):
        run = self.event_answers()
        row = lifecycle.read_event_rows(run)[0]
        self.assertTrue(row['malformed'])
        self.assertTrue(record_json.selected(row, types=['event']))
        self.assertEqual(row['issue_type'], 'event')
        self.assertEqual(run.calls, [['export', '--all'],
            ['list', '--type', 'event', '--all', '--limit', '0', '--json']])

    def test_recovered_event_fields_and_child_id_cannot_replace_native_membership(self):
        run = self.event_answers(member=False)
        row = lifecycle.read_event_rows(run)[0]
        self.assertTrue(row['malformed'])
        self.assertEqual(row['issue_type'], 'unknown')
        self.assertFalse(record_json.selected(row, types=['event']))
        self.assertEqual(lifecycle.unreadable_events([row]), [])

    def test_label_and_type_reads_independently_select_only_their_own_ids(self):
        ref = dict(CAPTURED_EVENT, id='probe-ref', labels=['event'], issue_type='event')
        evt = dict(CAPTURED_EVENT, id='probe-event', labels=['reference'], issue_type='task')
        untouched = dict(CAPTURED_EVENT, id='probe-ordinary.1', labels=['reference'])
        raw = [deep_answer(r) for r in (ref, evt, untouched)]
        run = Answers('\n'.join(raw), {('--label', 'reference'): '[' + raw[0] + ']',
                                      ('--type', 'event'): '[' + raw[1] + ']'})
        rows = record_json.classify(record_json.loads_rows(run.exported), run,
                                   labels=['reference'], types=['event'])
        self.assertTrue(record_json.selected(rows[0], labels=['reference']))
        self.assertFalse(record_json.selected(rows[0], types=['event']))
        self.assertTrue(record_json.selected(rows[1], types=['event']))
        self.assertFalse(record_json.selected(rows[1], labels=['reference']))
        self.assertFalse(record_json.selected(rows[2], labels=['reference'], types=['event']))
        self.assertEqual(rows[2]['labels'], [])
        self.assertEqual(run.calls, [
            ['list', '--label', 'reference', '--all', '--limit', '0', '--json'],
            ['list', '--type', 'event', '--all', '--limit', '0', '--json']])

    def test_keyed_get_uses_native_label_selection_and_refuses_the_unreadable_anchor(self):
        raw = deep_answer(dict(CAPTURED_EVENT, id='probe-ref', title='Unrelated retitle'))
        run = Answers('', {('--label', 'reference', '--label-any', 'reference-key:sample-ref'):
                           '[' + raw + ']'}, shown='[' + raw + ']')
        rows = references.KIND.read_key_rows(run, 'sample.ref')
        self.assertEqual(len(rows), 1)
        self.assertTrue(record_json.selected(rows[0], labels=['reference-key:sample-ref']))
        with self.assertRaisesRegex(ValueError, 'anchor probe-ref.*cannot be read'):
            references.KIND.find_entry(rows, 'sample.ref', ['ops'])

    def test_unreadable_requirement_without_a_bound_ledger_refuses_before_any_write(self):
        raw = deep_answer(dict(CAPTURED_EVENT, id='probe-req', issue_type='task',
                               title='Not a requirement title', labels=[]))
        run = Answers(raw + '\n', {('--label', 'requirement'): '[' + raw + ']'})
        draft = dict(schema_version=1, operation_id='replay-requirement', operation='draft',
            kind='requirement', title='R01: Intent', key='R01', description='Intent.',
            acceptance_state='draft', parent='job-1')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'anchor probe-req could not be parsed'):
                requirements.apply_native(draft, 'alice', run, Path(tmp))
        self.assertIn(['comments', 'probe-req', '--json'], run.calls)
        self.assertTrue(all(c[0] in ('export', 'list', 'comments') for c in run.calls))

    def test_unreadable_event_refuses_new_fact_and_exact_retry_before_native_writes(self):
        for operation in ('new-fact', 'operation-1'):
            with self.subTest(operation=operation):
                run = self.event_answers()
                with self.assertRaisesRegex(ValueError, 'probe-lrr.1.*operator must reconcile or repair'):
                    lifecycle.apply_native(payload(operation=operation), 'alice/session', run)
                self.assertEqual(len(run.calls), 2)

    def test_unreadable_newer_event_makes_an_older_pass_unknown(self):
        store = NativeStore().seed('tested')
        self.assertEqual(store.view()['facts']['tested']['value'], 'passed')
        newest = dict(CAPTURED_EVENT, id='trial-task.3', parent='trial-task',
            dependencies=[dict(issue_id='trial-task.3', depends_on_id='trial-task', type='parent-child')])
        raw = deep_answer(newest)
        run = Answers(''.join(json.dumps(r) + '\n' for r in store.rows) + raw + '\n',
                      {('--type', 'event'): '[' + raw + ']'})
        state = lifecycle.project_facts(lifecycle.read_event_rows(run))[0]
        self.assertEqual(state['facts']['tested']['value'], 'unknown')
        self.assertEqual(state['facts']['tested']['evidence'], [])

    def test_unselected_unreadable_child_does_not_erase_older_pass(self):
        store = NativeStore().seed('tested')
        raw = deep_answer(dict(CAPTURED_EVENT, id='trial-task.3'))
        run = Answers(''.join(json.dumps(r) + '\n' for r in store.rows) + raw + '\n')
        state = next(r for r in lifecycle.project_facts(lifecycle.read_event_rows(run))
                     if r['id'] == 'trial-task')
        self.assertEqual(state['facts']['tested']['value'], 'passed')

    def test_missing_id_or_failed_type_read_refuses_lifecycle_write(self):
        for answer in ('[{}]', '[null]', 'not JSON'):
            with self.subTest(answer=answer):
                run = self.event_answers()
                run.selected[('--type', 'event')] = answer
                with self.assertRaisesRegex(ValueError, 'event membership cannot be read.*operator must reconcile'):
                    lifecycle.apply_native(payload(), 'alice/session', run)
                self.assertEqual(len(run.calls), 2)

    def test_healthy_event_read_does_not_need_an_extra_selection(self):
        run = Answers(json.dumps(CAPTURED_EVENT) + '\n')
        self.assertEqual(lifecycle.read_event_rows(run), [CAPTURED_EVENT])
        self.assertEqual(run.calls, [['export', '--all']])


class DiscoveryIsolationTests(unittest.TestCase):
    def test_importing_record_json_tests_does_not_install_a_platform_lock_stub(self):
        code = '''
import sys
try:
    import fcntl
except ImportError:
    fcntl = None
import test_record_json
assert sys.modules.get('fcntl') is fcntl, 'test discovery leaked a lock stand-in'
import test_restore_authority_lock as restore
assert restore.real_fcntl is fcntl, 'restore detected a fake platform module'
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=KIT / 'tests',
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
