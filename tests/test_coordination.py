import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coordination


CHILD = {'operation': 'create-child', 'request_id': 'session/request1',
         'parent': 'sample-job', 'title': 'Bounded contribution',
         'description': 'Keep this exact intent.', 'type': 'task'}
DECISION = {'operation': 'create-child', 'request_id': 'session/decision1',
            'parent': 'sample-job', 'title': 'Bounded decision',
            'description': '## Decision\nUse it.\n## Rationale\nBecause.\n## Alternatives Considered\nNone.',
            'type': 'decision'}
MERGE = {'operation': 'merge-acquire', 'task': 'sample-task', 'target': 'main@commit1'}


class Native:
    """Stateful native seam: distinguish a failed write from a lost response."""

    def __init__(self):
        self.calls = []
        self.issues = []
        self.holder = None
        self.create_outcome = 'ok'
        self.refuse_create = None
        self.acquire_outcome = 'ok'
        self.show_fails = False

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == 'list':
            label = args[args.index('--label') + 1]
            return json.dumps([issue for issue in self.issues if label in issue['labels']])
        if args[0] == 'create':
            if self.refuse_create is not None:
                raise ValueError(self.refuse_create)
            if self.create_outcome == 'not-written':
                raise RuntimeError('uncertain native failure')
            issue = {'id': 'sample-job.7',
                     'labels': args[args.index('--labels') + 1].split(',')}
            self.issues.append(issue)
            if self.create_outcome == 'lost-response':
                raise RuntimeError('write committed, response lost')
            if self.create_outcome == 'malformed-response':
                return 'not JSON'
            return json.dumps(issue)
        if args[0] == 'show':
            if self.show_fails:
                raise RuntimeError('task missing')
            return json.dumps([{'id': args[1]}])
        if args[:2] == ['merge-slot', 'check']:
            return json.dumps({'available': self.holder is None, 'holder': self.holder})
        if args[:2] == ['merge-slot', 'acquire']:
            if self.acquire_outcome == 'not-written':
                raise RuntimeError('acquire response uncertain')
            if self.holder is not None:
                return json.dumps({'acquired': False, 'holder': self.holder})
            self.holder = args[args.index('--holder') + 1]
            if self.acquire_outcome == 'lost-response':
                raise RuntimeError('acquire committed, response lost')
            return json.dumps({'acquired': True, 'holder': self.holder})
        if args[:2] == ['merge-slot', 'release']:
            self.holder = None
            return json.dumps({'released': True})
        raise AssertionError('unexpected native command: ' + repr(args))

    def count(self, *prefix):
        return sum(call[:len(prefix)] == list(prefix) for call in self.calls)


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.native = Native()

    def apply(self, payload, actor='alice'):
        return coordination.apply_native(copy.deepcopy(payload), actor, self.native, self.project)

    def test_child_repeat_uses_returned_native_id_without_second_create(self):
        self.assertEqual(self.apply(CHILD), {'id': 'sample-job.7', 'reconciled': False})
        self.assertEqual(self.apply(CHILD), {'id': 'sample-job.7', 'reconciled': True})
        self.assertEqual(self.native.count('create'), 1)
        create = next(c for c in self.native.calls if c[0] == 'create')
        self.assertEqual(create[create.index('--parent') + 1], CHILD['parent'])
        self.assertIn('--no-inherit-labels', create)

    def test_lost_and_malformed_create_response_reconcile_without_duplicates(self):
        for outcome in ('lost-response', 'malformed-response'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                native = Native()
                native.create_outcome = outcome
                with self.assertRaises((ValueError, RuntimeError)):
                    coordination.apply_native(CHILD, 'alice', native, Path(tmp))
                result = coordination.apply_native(CHILD, 'alice', native, Path(tmp))
                self.assertEqual(result, {'id': 'sample-job.7', 'reconciled': True})
                self.assertEqual(native.count('create'), 1)

    def test_reserved_request_without_native_issue_fails_closed(self):
        self.native.create_outcome = 'not-written'
        with self.assertRaises(RuntimeError):
            self.apply(CHILD)
        self.native.create_outcome = 'ok'
        with self.assertRaisesRegex(ValueError, 'Reserved request'):
            self.apply(CHILD)
        self.assertEqual(self.native.count('create'), 1)
        self.assertEqual(self.native.issues, [])

    def test_same_request_cannot_change_actor_or_content(self):
        self.apply(CHILD)
        with self.assertRaisesRegex(ValueError, 'different content or actor'):
            self.apply(CHILD, actor='bob')
        for field, value in (('title', 'different'), ('parent', 'another-job'),
                             ('description', 'new intent'), ('type', 'bug')):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, 'different content or actor'):
                    self.apply(dict(CHILD, **{field: value}))
        self.assertEqual(self.native.count('create'), 1)

    def test_native_content_conflict_detected_even_without_local_receipt(self):
        self.apply(CHILD)
        for receipt in (self.project / '.coordination-requests').iterdir():
            receipt.unlink()
        with self.assertRaisesRegex(ValueError, 'Native request content mismatch'):
            self.apply(dict(CHILD, title='changed'))
        self.assertEqual(self.native.count('create'), 1)

    def test_duplicate_native_matches_require_operator_reconciliation(self):
        self.apply(CHILD)
        self.native.issues.append(dict(self.native.issues[0], id='sample-job.8'))
        with self.assertRaisesRegex(ValueError, 'Duplicate native'):
            self.apply(CHILD)
        self.assertEqual(self.native.count('create'), 1)

    def test_receipt_write_failure_after_create_can_reconcile(self):
        atomic = coordination.atomic

        def fail_completion(path, data):
            if data.get('status') == 'complete':
                raise OSError('disk full after native commit')
            atomic(path, data)

        with patch.object(coordination, 'atomic', side_effect=fail_completion):
            with self.assertRaises(OSError):
                self.apply(CHILD)
        self.assertEqual(self.apply(CHILD)['id'], 'sample-job.7')
        self.assertEqual(self.native.count('create'), 1)

    def test_reservation_write_failure_does_not_create_child(self):
        with patch.object(coordination, 'atomic', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.apply(CHILD)
        self.assertEqual(self.native.count('create'), 0)

    def receipt(self):
        files = list((self.project / '.coordination-requests').iterdir())
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text(encoding='utf-8'))

    def test_missing_decision_sections_record_failed_receipt_and_corrected_retry_succeeds(self):
        broken = dict(DECISION, description='## Decision\nUse it.\n## Rationale\nBecause.')
        with self.assertRaisesRegex(ValueError, '## Alternatives Considered'):
            self.apply(broken)
        receipt = self.receipt()
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('## Alternatives Considered', receipt['error'])
        self.assertIn('## Decision, ## Rationale, ## Alternatives Considered', receipt['error'])
        self.assertEqual(self.native.count('create'), 0)
        self.assertEqual(self.native.issues, [])
        # The same request ID accepts corrected content and creates exactly one issue.
        self.assertEqual(self.apply(DECISION), {'id': 'sample-job.7', 'reconciled': False})
        self.assertEqual(self.native.count('create'), 1)
        self.assertEqual(len(self.native.issues), 1)
        self.assertEqual(self.receipt()['status'], 'complete')
        history = self.receipt()['history']
        self.assertEqual(history[0]['status'], 'failed')

    def test_refused_native_decision_records_failed_and_corrected_retry_succeeds(self):
        self.native.refuse_create = 'missing required sections for decision: ## Alternatives Considered'
        with self.assertRaisesRegex(ValueError, '## Decision, ## Rationale, ## Alternatives Considered'):
            self.apply(DECISION)
        receipt = self.receipt()
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(self.native.count('create'), 1)
        self.assertEqual(self.native.issues, [])
        self.native.refuse_create = None
        self.assertEqual(self.apply(DECISION)['id'], 'sample-job.7')
        self.assertEqual(self.native.count('create'), 2)
        self.assertEqual(len(self.native.issues), 1)

    def test_refused_create_that_landed_reconciles_as_complete(self):
        native = self.native

        def landed(args):
            if args[0] == 'create':
                native.calls.append(list(args))
                native.issues.append({'id': 'sample-job.7',
                                      'labels': args[args.index('--labels') + 1].split(',')})
                raise ValueError('create reported failure after committing')
            return native(args)

        result = coordination.apply_native(copy.deepcopy(CHILD), 'alice', landed, self.project)
        self.assertEqual(result, {'id': 'sample-job.7', 'reconciled': True})
        self.assertEqual(self.receipt()['status'], 'complete')
        self.assertEqual(self.receipt()['id'], 'sample-job.7')

    def test_refused_create_that_cannot_be_confirmed_stays_pending(self):
        self.native.refuse_create = 'native create refused'
        calls = {'list': 0}

        def run(args):
            if args[0] == 'list':
                calls['list'] += 1
                if calls['list'] == 2:
                    raise RuntimeError('native read unavailable')
            return self.native(args)

        with self.assertRaisesRegex(ValueError, 'uncertain'):
            coordination.apply_native(copy.deepcopy(CHILD), 'alice', run, self.project)
        self.assertEqual(self.receipt()['status'], 'pending')
        with self.assertRaisesRegex(ValueError, 'Reserved request'):
            coordination.apply_native(copy.deepcopy(CHILD), 'alice', run, self.project)

    def test_uncertain_create_keeps_pending_reservation(self):
        self.native.create_outcome = 'not-written'
        with self.assertRaises(RuntimeError):
            self.apply(CHILD)
        self.assertEqual(self.receipt()['status'], 'pending')
        with self.assertRaisesRegex(ValueError, 'Reserved request'):
            self.apply(CHILD)

    def test_merge_nonholder_cannot_release_or_acquire(self):
        self.apply(MERGE)
        with self.assertRaisesRegex(ValueError, 'Only the current holder'):
            self.apply({'operation': 'merge-release'}, actor='bob')
        self.assertEqual(self.apply(MERGE, actor='bob'), {'acquired': False, 'holder': 'alice'})
        self.assertEqual(self.native.count('merge-slot', 'release'), 0)
        self.assertEqual(self.native.count('merge-slot', 'acquire'), 1)

    def test_same_holder_reacquires_only_exact_context(self):
        self.apply(MERGE)
        self.assertTrue(self.apply(MERGE)['reconciled'])
        for field, value in (('task', 'sample-other'), ('target', 'main@commit2')):
            with self.assertRaisesRegex(ValueError, 'different or missing context'):
                self.apply(dict(MERGE, **{field: value}))
        (self.project / '.merge-context.json').unlink()
        with self.assertRaisesRegex(ValueError, 'different or missing context'):
            self.apply(MERGE)
        self.assertEqual(self.native.count('merge-slot', 'acquire'), 1)

    def test_uncertain_merge_acquire_reconciles_recorded_holder(self):
        self.native.acquire_outcome = 'lost-response'
        with self.assertRaises(RuntimeError):
            self.apply(MERGE)
        self.assertTrue(self.apply(MERGE)['reconciled'])
        self.assertEqual(self.native.count('merge-slot', 'acquire'), 1)

    def test_unwritten_merge_acquire_can_retry_after_native_check(self):
        self.native.acquire_outcome = 'not-written'
        with self.assertRaises(RuntimeError):
            self.apply(MERGE)
        self.native.acquire_outcome = 'ok'
        self.assertTrue(self.apply(MERGE)['acquired'])
        self.assertEqual(self.native.count('merge-slot', 'acquire'), 2)

    def test_missing_task_or_context_write_failure_prevents_native_acquire(self):
        self.native.show_fails = True
        with self.assertRaises(RuntimeError):
            self.apply(MERGE)
        self.native.show_fails = False
        with patch.object(coordination, 'atomic', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.apply(MERGE)
        self.assertEqual(self.native.count('merge-slot', 'acquire'), 0)

    def test_release_keeps_audit_context_but_check_reports_no_current_context(self):
        self.apply(MERGE)
        self.assertTrue(self.apply({'operation': 'merge-release'})['released'])
        self.assertTrue((self.project / '.merge-context.json').exists())
        self.assertIsNone(self.apply({'operation': 'merge-check'})['context'])
        self.assertEqual(self.apply({'operation': 'merge-release'}),
                         {'released': False, 'available': True})
        self.assertEqual(self.native.count('merge-slot', 'release'), 1)


class ReconcileTests(unittest.TestCase):
    """Operator release of a reservation with no native issue, audited and idempotent."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.native = Native()

    def pending(self):
        self.native.create_outcome = 'not-written'
        with self.assertRaises(RuntimeError):
            coordination.apply_native(copy.deepcopy(CHILD), 'alice', self.native, self.project)

    def receipt(self):
        return json.loads(next((self.project / '.coordination-requests').iterdir()).read_text(encoding='utf-8'))

    def test_operator_reconcile_is_audited_and_idempotent(self):
        self.pending()
        result = coordination.reconcile_request(self.project, CHILD['request_id'], 'operator-1',
                                                'native validation refused before the fix', 'released',
                                                self.native, at='2026-09-25T00:00:00Z')
        self.assertEqual(result, {'request_id': CHILD['request_id'], 'status': 'released', 'reconciled': True,
                                  'reconciliation': {'actor': 'operator-1',
                                                     'reason': 'native validation refused before the fix',
                                                     'disposition': 'released', 'at': '2026-09-25T00:00:00Z'}})
        stored = self.receipt()
        self.assertEqual(stored['status'], 'released')
        self.assertEqual(stored['reconciliation'], result['reconciliation'])
        again = coordination.reconcile_request(self.project, CHILD['request_id'], 'operator-1',
                                               'native validation refused before the fix', 'released',
                                               self.native, at='2026-09-26T00:00:00Z')
        self.assertTrue(again['already'])
        self.assertFalse(again['reconciled'])
        self.assertEqual(self.receipt(), stored)
        # A released request ID accepts the same content on a corrected retry.
        self.native.create_outcome = 'ok'
        self.assertEqual(coordination.apply_native(copy.deepcopy(CHILD), 'alice', self.native, self.project)['id'],
                         'sample-job.7')

    def test_reconcile_refuses_when_native_issue_exists(self):
        self.native.create_outcome = 'lost-response'
        with self.assertRaises(RuntimeError):
            coordination.apply_native(copy.deepcopy(CHILD), 'alice', self.native, self.project)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            coordination.reconcile_request(self.project, CHILD['request_id'], 'operator-1', 'checked',
                                           'released', self.native)

    def test_reconcile_requires_a_reservation(self):
        with self.assertRaisesRegex(ValueError, 'No coordination request reservation'):
            coordination.reconcile_request(self.project, 'missing/request', 'operator-1', 'checked',
                                           'released', self.native)

    def test_reconcile_refuses_a_completed_request(self):
        coordination.apply_native(copy.deepcopy(CHILD), 'alice', self.native, self.project)
        with self.assertRaisesRegex(ValueError, 'not pending'):
            coordination.reconcile_request(self.project, CHILD['request_id'], 'operator-1', 'checked',
                                           'released', self.native)


if __name__ == '__main__':
    unittest.main()
