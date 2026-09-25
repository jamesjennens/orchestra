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
    """Stateful native seam: distinguish a failed write from a lost response.

    `bd_validate_decisions` models bd 1.2.2's own rule: a decision needs its
    section names present case-insensitively as substrings, so every heading
    spelling bd accepts also passes the preflight.
    """

    def __init__(self):
        self.calls = []
        self.issues = []
        self.holder = None
        self.create_outcome = 'ok'
        self.acquire_outcome = 'ok'
        self.show_fails = False
        self.refuse_preflight = None
        self.bd_validate_decisions = True
        self.drop_request_label = False
        self.hide_label_reads = 0

    def section_refusal(self, args):
        if not self.bd_validate_decisions or args[args.index('--type') + 1] != 'decision':
            return
        description = args[args.index('--description') + 1].lower()
        missing = [name for name in ('Decision', 'Rationale', 'Alternatives Considered')
                   if name.lower() not in description]
        if missing:
            raise ValueError('missing required sections for decision: ' + ', '.join(missing))

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == 'list':
            if self.hide_label_reads > 0:
                self.hide_label_reads -= 1
                return '[]'
            label = args[args.index('--label') + 1]
            return json.dumps([issue for issue in self.issues if label in issue['labels']])
        if args[0] == 'create':
            if '--dry-run' in args:
                if self.refuse_preflight is not None:
                    raise ValueError(self.refuse_preflight)
                self.section_refusal(args)
                return json.dumps({'dry_run': True})
            if self.create_outcome == 'not-written':
                raise RuntimeError('uncertain native failure')
            labels = args[args.index('--labels') + 1].split(',')
            if self.drop_request_label:
                # bd committed the issue, then the request-label write failed and
                # the command exited nonzero without the request label.
                labels = [label for label in labels if not label.startswith('request:')]
            issue = {'id': 'sample-job.%d' % (len(self.issues) + 7), 'labels': labels}
            self.issues.append(issue)
            if self.drop_request_label:
                raise ValueError('adding labels: database is locked')
            if self.create_outcome == 'stale-label-read':
                # The commit exists, but it is not visible to the next label read.
                self.hide_label_reads = 1
                raise ValueError('create reported failure after committing')
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

    def count_create(self):
        return sum(1 for call in self.calls if call[0] == 'create' and '--dry-run' not in call)

    def count_dry_run(self):
        return sum(1 for call in self.calls if call[0] == 'create' and '--dry-run' in call)


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
        self.assertEqual(self.native.count_create(), 1)
        self.assertEqual(self.native.count_dry_run(), 1)
        create = next(c for c in self.native.calls if c[0] == 'create' and '--dry-run' not in c)
        self.assertEqual(create[create.index('--parent') + 1], CHILD['parent'])
        self.assertIn('--no-inherit-labels', create)
        preflight = next(c for c in self.native.calls if c[0] == 'create' and '--dry-run' in c)
        self.assertEqual([a for a in preflight if a != '--dry-run'], create)

    def test_lost_and_malformed_create_response_reconcile_without_duplicates(self):
        for outcome in ('lost-response', 'malformed-response'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                native = Native()
                native.create_outcome = outcome
                with self.assertRaises((ValueError, RuntimeError)):
                    coordination.apply_native(CHILD, 'alice', native, Path(tmp))
                result = coordination.apply_native(CHILD, 'alice', native, Path(tmp))
                self.assertEqual(result, {'id': 'sample-job.7', 'reconciled': True})
                self.assertEqual(native.count_create(), 1)

    def test_reserved_request_without_native_issue_fails_closed(self):
        self.native.create_outcome = 'not-written'
        with self.assertRaises(RuntimeError):
            self.apply(CHILD)
        self.native.create_outcome = 'ok'
        with self.assertRaisesRegex(ValueError, 'Reserved request'):
            self.apply(CHILD)
        self.assertEqual(self.native.count_create(), 1)
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
        self.assertEqual(self.native.count_create(), 1)

    def test_native_content_conflict_detected_even_without_local_receipt(self):
        self.apply(CHILD)
        for receipt in (self.project / '.coordination-requests').iterdir():
            receipt.unlink()
        with self.assertRaisesRegex(ValueError, 'Native request content mismatch'):
            self.apply(dict(CHILD, title='changed'))
        self.assertEqual(self.native.count_create(), 1)

    def test_duplicate_native_matches_require_operator_reconciliation(self):
        self.apply(CHILD)
        self.native.issues.append(dict(self.native.issues[0], id='sample-job.8'))
        with self.assertRaisesRegex(ValueError, 'Duplicate native'):
            self.apply(CHILD)
        self.assertEqual(self.native.count_create(), 1)

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
        self.assertEqual(self.native.count_create(), 1)

    def test_reservation_write_failure_does_not_create_child(self):
        with patch.object(coordination, 'atomic', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.apply(CHILD)
        self.assertEqual(self.native.count_create(), 0)

    def receipt(self):
        files = list((self.project / '.coordination-requests').iterdir())
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text(encoding='utf-8'))

    def test_preflight_refusal_reserves_nothing_and_corrected_retry_succeeds(self):
        broken = dict(DECISION, description='## Decision\nUse it.\n## Rationale\nBecause.')
        with self.assertRaisesRegex(ValueError, '## Alternatives Considered'):
            self.apply(broken)
        # Native validation refused before any reservation or issue existed; the
        # request ID stays free rather than being stranded as a failed receipt.
        self.assertEqual(list((self.project / '.coordination-requests').iterdir()), [])
        self.assertEqual(self.native.count_dry_run(), 1)
        self.assertEqual(self.native.count_create(), 0)
        self.assertEqual(self.native.issues, [])
        # bd's own missing-sections message gains the required-heading hint.
        with self.assertRaisesRegex(ValueError, '## Decision, ## Rationale, ## Alternatives Considered'):
            self.apply(broken)
        self.assertEqual(list((self.project / '.coordination-requests').iterdir()), [])
        # The free request ID accepts corrected content and creates one issue.
        self.assertEqual(self.apply(DECISION), {'id': 'sample-job.7', 'reconciled': False})
        self.assertEqual(self.native.count_create(), 1)
        self.assertEqual(len(self.native.issues), 1)
        self.assertEqual(self.receipt()['status'], 'complete')
        self.assertEqual(self.receipt()['actor'], 'alice')

    def test_unrelated_native_refusal_does_not_get_the_section_hint(self):
        self.native.refuse_preflight = 'parent issue sample-job not found'
        with self.assertRaises(ValueError) as caught:
            self.apply(CHILD)
        self.assertIn('parent issue sample-job not found', str(caught.exception))
        self.assertNotIn('section headers', str(caught.exception))
        self.assertEqual(list((self.project / '.coordination-requests').iterdir()), [])
        self.assertEqual(self.native.count_create(), 0)

    def test_bd_valid_decision_header_variants_are_deferred_to_native(self):
        variants = (
            ('prose Rationale:', '## Decision\nD\nDiscussing Rationale: in prose.\n## Alternatives Considered\nA'),
            ('## Rationale:', '## Decision\nD\n## Rationale:\nR\n## Alternatives Considered\nA'),
            ('## 2. Rationale', '## Decision\nD\n## 2. Rationale\nR\n## Alternatives Considered\nA'),
            ('setext', '## Decision\nD\nRationale\n-------\nR\n## Alternatives Considered\nA'),
            ('bold', '## Decision\nD\n**Rationale**\nR\n## Alternatives Considered\nA'),
            ('suffix', '## Decision\nD\n## Rationale and tradeoffs\nR\n## Alternatives Considered\nA'),
            ('trailing ##', '## Decision\nD\n## Rationale ##\nR\n## Alternatives Considered\nA'),
        )
        for index, (name, description) in enumerate(variants):
            with self.subTest(variant=name):
                payload = dict(DECISION, request_id='session/header-%d' % index, description=description)
                self.assertEqual(self.apply(payload), {'id': 'sample-job.%d' % (index + 7), 'reconciled': False})
        self.assertEqual(self.native.count_create(), len(variants))
        self.assertEqual(self.native.count_dry_run(), len(variants))

    def test_preflight_refusal_leaves_the_id_free_for_another_actor(self):
        broken = dict(DECISION, description='## Decision\nD\n## Rationale\nR')
        with self.assertRaises(ValueError):
            self.apply(broken, actor='alice')
        # Nothing was reserved, so a first create under that ID by another actor
        # is not blocked by a receipt that was never written.
        result = coordination.apply_native(copy.deepcopy(DECISION), 'bob', self.native, self.project)
        self.assertEqual(result, {'id': 'sample-job.7', 'reconciled': False})
        self.assertEqual(self.receipt()['status'], 'complete')
        self.assertEqual(self.receipt()['actor'], 'bob')

    def test_real_create_nonzero_after_preflight_stays_pending_and_cannot_duplicate(self):
        # bd commits the issue, then exits nonzero without writing the request
        # label, so the label read cannot see it (reviewer case P1c).
        self.native.drop_request_label = True
        with self.assertRaisesRegex(ValueError, 'uncertain'):
            self.apply(CHILD)
        self.assertEqual(self.receipt()['status'], 'pending')
        self.assertEqual(self.native.count_dry_run(), 1)
        self.assertEqual(self.native.count_create(), 1)
        self.assertEqual(len(self.native.issues), 1)
        with self.assertRaisesRegex(ValueError, 'Reserved request'):
            self.apply(CHILD)
        self.assertEqual(self.native.count_create(), 1)
        self.assertEqual(len(self.native.issues), 1)

    def test_stale_label_read_after_real_create_stays_pending(self):
        # The commit is visible only after the refused-create label read (P1b);
        # no label read decides the outcome any more, so it stays pending.
        self.native.create_outcome = 'stale-label-read'
        with self.assertRaisesRegex(ValueError, 'uncertain'):
            self.apply(CHILD)
        self.assertEqual(self.receipt()['status'], 'pending')
        with self.assertRaisesRegex(ValueError, 'Reserved request'):
            self.apply(CHILD)
        with self.assertRaisesRegex(ValueError, 'different content or actor'):
            self.apply(dict(CHILD, title='edited'))
        self.assertEqual(self.native.count_create(), 1)
        self.assertEqual(len(self.native.issues), 1)

    def test_failed_receipt_stays_bound_to_the_original_actor(self):
        self.native.create_outcome = 'not-written'
        with self.assertRaises(RuntimeError):
            self.apply(CHILD, actor='alice')
        coordination.reconcile_request(self.project, CHILD['request_id'], 'operator-1',
                                       'confirmed no issue', 'failed', self.native)
        with self.assertRaisesRegex(ValueError, 'different content or actor'):
            self.apply(CHILD, actor='bob')
        self.assertEqual(self.native.count_create(), 1)
        self.native.create_outcome = 'ok'
        self.assertEqual(self.apply(dict(CHILD, title='corrected'), actor='alice')['id'], 'sample-job.7')
        self.assertEqual(self.native.count_create(), 2)

    def test_released_receipt_stays_bound_to_the_original_actor(self):
        self.native.create_outcome = 'not-written'
        with self.assertRaises(RuntimeError):
            self.apply(CHILD, actor='alice')
        coordination.reconcile_request(self.project, CHILD['request_id'], 'operator-1',
                                       'confirmed no issue', 'released', self.native)
        with self.assertRaisesRegex(ValueError, 'different content or actor'):
            self.apply(CHILD, actor='bob')
        self.assertEqual(self.native.count_create(), 1)

    def test_any_actor_release_opens_the_id_to_another_actor(self):
        self.native.create_outcome = 'not-written'
        with self.assertRaises(RuntimeError):
            self.apply(CHILD, actor='alice')
        coordination.reconcile_request(self.project, CHILD['request_id'], 'operator-1',
                                       'handover to bob', 'released', self.native, any_actor=True)
        self.native.create_outcome = 'ok'
        self.assertEqual(self.apply(CHILD, actor='bob')['id'], 'sample-job.7')
        self.assertEqual(self.native.count_create(), 2)
        self.assertTrue(any(entry.get('reconciliation', {}).get('any_actor')
                            for entry in self.receipt()['history']))

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

    def test_differing_reconcile_retry_reports_the_conflict(self):
        self.pending()
        first = coordination.reconcile_request(self.project, CHILD['request_id'], 'operator-1',
                                               'confirmed no issue', 'released', self.native,
                                               at='2026-09-25T00:00:00Z')
        self.assertTrue(first['reconciled'])
        stored = self.receipt()
        for actor, reason, disposition, any_actor in (
                ('operator-2', 'confirmed no issue', 'released', False),
                ('operator-1', 'a different reason', 'released', False),
                ('operator-1', 'confirmed no issue', 'failed', False),
                ('operator-1', 'confirmed no issue', 'released', True)):
            with self.subTest(actor=actor, reason=reason, disposition=disposition, any_actor=any_actor):
                with self.assertRaisesRegex(ValueError, 'Reconciliation conflict'):
                    coordination.reconcile_request(self.project, CHILD['request_id'], actor, reason,
                                                   disposition, self.native, any_actor=any_actor)
                self.assertEqual(self.receipt(), stored)

    def test_reconcile_any_actor_only_with_a_release(self):
        self.pending()
        with self.assertRaisesRegex(ValueError, 'released disposition'):
            coordination.reconcile_request(self.project, CHILD['request_id'], 'operator-1', 'checked',
                                           'failed', self.native, any_actor=True)

    def test_reconcile_completes_the_receipt_from_a_labelled_native_issue(self):
        self.native.create_outcome = 'lost-response'
        with self.assertRaises(RuntimeError):
            coordination.apply_native(copy.deepcopy(CHILD), 'alice', self.native, self.project)
        result = coordination.reconcile_request(self.project, CHILD['request_id'], 'operator-1',
                                                'issue exists without a completed receipt',
                                                'complete', self.native, at='2026-09-25T00:00:00Z')
        self.assertEqual(result, {'request_id': CHILD['request_id'], 'status': 'complete', 'reconciled': True,
                                  'id': 'sample-job.7',
                                  'reconciliation': {'actor': 'operator-1',
                                                     'reason': 'issue exists without a completed receipt',
                                                     'disposition': 'complete', 'at': '2026-09-25T00:00:00Z',
                                                     'completed_from': 'native'}})
        stored = self.receipt()
        self.assertEqual(stored['status'], 'complete')
        self.assertEqual(stored['id'], 'sample-job.7')
        self.assertEqual(stored['actor'], 'alice')
        # The original actor's identical create reconciles to the completed issue.
        self.assertEqual(coordination.apply_native(copy.deepcopy(CHILD), 'alice', self.native, self.project),
                         {'id': 'sample-job.7', 'reconciled': True})
        self.assertEqual(self.native.count_create(), 1)

    def test_reconcile_complete_requires_a_labelled_native_issue(self):
        self.pending()
        with self.assertRaisesRegex(ValueError, 'No labelled native issue'):
            coordination.reconcile_request(self.project, CHILD['request_id'], 'operator-1', 'checked',
                                           'complete', self.native)

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
