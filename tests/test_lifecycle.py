"""Lifecycle evidence and retry checks using the pinned native export shape."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lifecycle import DIMENSIONS, PREFIX, apply_native, native_event, project_facts, validate_payload
from requirements import canonical_bytes, content_hash


def scope(**changes):
    return {'source_commit': 'source-a', 'integration_commit': 'merge-a',
            'release_id': 'release-a', 'environment': 'staging', **changes}


def payload(dimension='implemented', value='passed', operation='operation-1', **changes):
    result = dict(schema_version=1, operation_id=operation, task='trial-task',
                  dimension=dimension, value=value, scope=scope(),
                  evidence=['commit:source-a'], provenance='performed', actor='alice/session')
    result.update(changes)
    if dimension == 'lifecycle-scope':
        result['value'] = content_hash(result['scope'])
    return result


class NativeStore:
    """Simulates export and native label/event writes, including lost responses."""
    def __init__(self):
        self.rows = [dict(_type='issue', id='trial-task', title='Synthetic task',
                         issue_type='task', status='open', labels=[])]
        self.calls = []
        self.fail_before = None
        self.fail_after = None

    def run(self, args):
        self.calls.append(args[:])
        if args == ['export', '--all']:
            return ''.join(json.dumps(r) + '\n' for r in self.rows)
        assert args[0] == 'set-state', args
        dim, value = args[2].split('=', 1)
        if self.fail_before == value:
            self.fail_before = None
            raise RuntimeError('interrupted before write')
        labels = self.rows[0]['labels']
        if dim + ':' + value in labels:
            return json.dumps({'changed': False})
        old_value = next((x.split(':', 1)[1] for x in labels if x.startswith(dim + ':')), None)
        self.rows[0]['labels'] = [x for x in labels if not x.startswith(dim + ':')] + [dim + ':' + value]
        number = len(self.rows)
        event_id = 'trial-task.' + str(number)
        reason = args[args.index('--reason') + 1]
        description = ('Set ' + dim + ' to ' + value if old_value is None else
                       'Changed ' + dim + ' from ' + old_value + ' to ' + value)
        self.rows.append(dict(_type='issue', id=event_id, issue_type='event',
                              title='State change: ' + dim + ' → ' + value,
                              description=description + '\n\nReason: ' + reason,
                              status='closed', created_by='alice/session',
                              created_at='2026-09-15T10:08:43Z',
                              dependencies=[dict(issue_id=event_id, depends_on_id='trial-task', type='parent-child')]))
        if self.fail_after == value:
            self.fail_after = None
            raise RuntimeError('response lost after committed write')
        return json.dumps(dict(changed=True, dimension=dim, event_id=event_id, new_value=value))

    def record(self, p):
        return apply_native(p, 'alice/session', self.run)

    def view(self):
        return project_facts(self.rows)[0]

    def seed(self, dimension='implemented'):
        self.record(payload('lifecycle-scope', operation='scope-1'))
        self.record(payload(dimension))
        return self


class LifecycleProjectionTests(unittest.TestCase):
    def test_closed_issue_does_not_imply_any_lifecycle_fact(self):
        store = NativeStore()
        store.rows[0]['status'] = 'closed'
        self.assertFalse(store.view()['has_lifecycle'])
        self.assertEqual({f['value'] for f in store.view()['facts'].values()}, {'unknown'})

    def test_native_shape_preserves_evidence_and_independent_dimensions(self):
        store = NativeStore().seed()
        fact = store.view()['facts']['implemented']
        self.assertEqual(fact, dict(value='passed', event_id='trial-task.2',
                                    evidence=['commit:source-a'], provenance='performed'))
        self.assertTrue(all(store.view()['facts'][d]['value'] == 'unknown' for d in DIMENSIONS if d != 'implemented'))

    def test_scope_rollover_does_not_inherit_any_prior_evidence(self):
        store = NativeStore().seed()
        for n, dim in enumerate(DIMENSIONS[1:], 2):
            store.record(payload(dim, operation='fact-' + str(n)))
        prior = copy.deepcopy(store.rows)
        store.record(payload('lifecycle-scope', operation='scope-2', scope=scope(source_commit='source-b')))
        self.assertEqual({f['value'] for f in store.view()['facts'].values()}, {'unknown'})
        self.assertTrue(all(not f['evidence'] for f in store.view()['facts'].values()))
        self.assertEqual(store.rows[1:len(prior)], prior[1:])

    def test_label_missing_mismatch_or_ambiguity_is_unknown(self):
        for labels in ([], ['implemented:failed'], ['implemented:passed', 'implemented:failed']):
            with self.subTest(labels=labels):
                store = NativeStore().seed()
                store.rows[0]['labels'] = [store.rows[0]['labels'][0]] + labels
                self.assertEqual(store.view()['facts']['implemented']['value'], 'unknown')

    def test_scope_label_mismatch_invalidates_all_facts(self):
        store = NativeStore().seed()
        store.rows[0]['labels'][0] = 'lifecycle-scope:unverified'
        self.assertIsNone(store.view()['scope'])
        self.assertEqual(store.view()['facts']['implemented']['value'], 'unknown')

    def test_missing_event_cannot_be_replaced_by_passed_label(self):
        store = NativeStore().seed()
        store.rows.pop()
        self.assertEqual(store.view()['facts']['implemented']['value'], 'unknown')

    def test_unknown_order_or_duplicate_event_id_is_ambiguous(self):
        for suffix in ('unrecognized', '2'):
            with self.subTest(suffix=suffix):
                store = NativeStore().seed()
                extra = copy.deepcopy(store.rows[-1])
                extra['id'] = 'trial-task.' + suffix
                store.rows.append(extra)
                self.assertEqual(store.view()['facts']['implemented']['value'], 'unknown')

    def test_unstructured_new_event_supersedes_older_structured_pass(self):
        store = NativeStore().seed()
        store.run(['set-state', 'trial-task', 'implemented=pending', '--reason', 'manual change', '--json'])
        store.run(['set-state', 'trial-task', 'implemented=passed', '--reason', 'manual assertion', '--json'])
        self.assertEqual(store.view()['facts']['implemented']['value'], 'unknown')
        self.assertEqual(store.view()['facts']['implemented']['event_id'], 'trial-task.4')

    def test_malformed_latest_state_event_cannot_preserve_old_pass(self):
        store = NativeStore().seed()
        event = copy.deepcopy(store.rows[-1])
        event['id'] = 'trial-task.3'
        event['description'] = 'Damaged or unrecognized event description'
        store.rows.append(event)
        self.assertEqual(store.view()['facts']['implemented']['value'], 'unknown')

    def test_correction_keeps_original_assertion_and_current_failure(self):
        store = NativeStore().seed('live-verified')
        original = copy.deepcopy(store.rows[-1])
        store.record(payload('live-verified', 'failed', 'correction-1', evidence=['issue:correction']))
        fact = store.view()['facts']['live-verified']
        self.assertEqual(fact['value'], 'failed')
        self.assertEqual(fact['evidence'], ['issue:correction'])
        self.assertIn(original, store.rows)

    def test_changed_native_description_recognizes_correction_evidence(self):
        store = NativeStore().seed()
        p = payload(value='failed', operation='correction-1', evidence=['report:regression'])
        store.record(p)
        row = store.rows[-1]
        self.assertEqual(row['description'], 'Changed implemented from passed to failed\n\nReason: ' + PREFIX + canonical_bytes(p).decode())
        self.assertEqual(native_event(row)['payload'], p)
        self.assertEqual(store.view()['facts']['implemented']['value'], 'failed')

    def test_numerical_native_order_does_not_use_export_order_or_timestamp(self):
        store = NativeStore().seed()
        for n in range(3, 11):
            store.record(payload(value='failed' if n % 2 else 'passed', operation='revision-' + str(n)))
        store.rows[1:] = reversed(store.rows[1:])
        self.assertEqual(store.view()['facts']['implemented']['event_id'], 'trial-task.10')

    def test_untrusted_structured_payload_is_not_evidence(self):
        for change in ('actor', 'parent', 'noncanonical', 'invalid-json'):
            with self.subTest(change=change):
                store = NativeStore().seed()
                event = store.rows[-1]
                if change == 'actor': event['created_by'] = 'mallory'
                elif change == 'parent': event['dependencies'][0]['depends_on_id'] = 'different-task'
                elif change == 'noncanonical': event['description'] += ' '
                else: event['description'] = 'Set implemented to passed\n\nReason: ' + PREFIX + '{bad'
                parsed = native_event(event)
                self.assertIsNone(parsed['payload'])
                self.assertEqual(store.view()['facts']['implemented']['value'], 'unknown')


class LifecycleWriteTests(unittest.TestCase):
    def test_idempotent_retry_after_lost_response_does_not_duplicate(self):
        store = NativeStore()
        p = payload('lifecycle-scope', operation='scope-1')
        store.fail_after = p['value']
        with self.assertRaises(RuntimeError): store.record(p)
        count = len(store.rows)
        self.assertEqual(store.record(p), dict(event_id='trial-task.1', reconciled=True))
        self.assertEqual(len(store.rows), count)

    def test_retry_old_success_does_not_overwrite_later_correction(self):
        store = NativeStore().seed()
        store.record(payload(value='failed', operation='correction-1'))
        self.assertTrue(store.record(payload())['reconciled'])
        self.assertEqual(store.view()['facts']['implemented']['value'], 'failed')

    def test_operation_id_reuse_with_different_content_is_rejected(self):
        store = NativeStore().seed()
        with self.assertRaisesRegex(ValueError, 'operation ID'):
            store.record(payload(evidence=['different:evidence']))

    def test_same_value_new_evidence_creates_new_native_event(self):
        store = NativeStore().seed()
        store.record(payload(operation='new-proof', evidence=['test:new-proof']))
        self.assertEqual(store.view()['facts']['implemented']['evidence'], ['test:new-proof'])
        self.assertEqual(len(store.rows), 5)
        self.assertIsNone(native_event(store.rows[-2])['payload'])

    def test_interrupted_intermediate_state_is_unknown_and_retry_recovers(self):
        store = NativeStore().seed()
        p = payload(operation='new-proof', evidence=['test:new-proof'])
        store.fail_before = 'passed'
        with self.assertRaises(RuntimeError): store.record(p)
        self.assertEqual(store.view()['facts']['implemented']['value'], 'unknown')
        store.record(p)
        self.assertEqual(store.view()['facts']['implemented']['evidence'], ['test:new-proof'])
        self.assertEqual(len(store.rows), 5)

    def test_fact_requires_exact_current_scope(self):
        store = NativeStore().seed()
        for p in (payload(operation='next', scope=scope(source_commit='source-b')),
                  payload(operation='other', task='missing-task')):
            with self.subTest(task=p['task']), self.assertRaises(ValueError): store.record(p)
        with self.assertRaisesRegex(ValueError, 'matching lifecycle scope'):
            NativeStore().record(payload())

    def test_request_actor_must_match_payload_actor(self):
        store = NativeStore()
        with self.assertRaisesRegex(ValueError, 'actor'):
            apply_native(payload(), 'someone-else', store.run)
        self.assertFalse(store.calls)

    def test_event_cannot_be_used_as_lifecycle_task(self):
        store = NativeStore().seed()
        for dim in ('implemented', 'lifecycle-scope'):
            with self.subTest(dimension=dim), self.assertRaises(ValueError):
                store.record(payload(dim, operation='event-target', task='trial-task.1'))

    def test_asserted_facts_require_evidence_and_relevant_identity(self):
        for dim in DIMENSIONS:
            with self.subTest(dimension=dim), self.assertRaises(ValueError):
                validate_payload(payload(dim, evidence=[]))
        for dim, key in [('implemented', 'source_commit'), ('tested', 'source_commit'),
                         ('reviewed', 'source_commit'), ('integrated', 'integration_commit'),
                         ('deployed', 'release_id'), ('live-verified', 'environment')]:
            with self.subTest(dimension=dim), self.assertRaises(ValueError):
                validate_payload(payload(dim, scope=scope(**{key: ''})))


if __name__ == '__main__':
    unittest.main()
