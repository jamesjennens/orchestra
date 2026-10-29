"""Fresh record accepts, exact retries, and receipt-lost native-only recovery.

The reused fake-native fixtures explicitly freeze their clock; native route tests
exercise the real clock separately with dates derived at fixture construction.
"""
import contextlib
import copy
import json
import unittest
from pathlib import Path


class AcceptPendingTests(unittest.TestCase):
    @contextlib.contextmanager
    def seeded(self, kind, direct=False):
        import reference_records
        import capability_records
        from test_reference_records import ReferenceCase, OPERATOR, acceptance
        from test_capability_records import CapabilityCase
        from requirements import content_hash
        case = (ReferenceCase if kind == 'reference' else CapabilityCase)(methodName='runTest')
        case.setUp()
        module = reference_records if kind == 'reference' else capability_records
        key = 'calendar.trading' if kind == 'reference' else 'review.structured-contribution'
        try:
            if direct:
                from test_reference_records import entry as reference_entry
                from test_capability_records import entry as capability_entry
                entry = reference_entry if kind == 'reference' else capability_entry
                case.native.actor = OPERATOR
                initial = dict(entry(operation='draft', operation_id='direct-accepted'),
                               acceptance_state='accepted', acceptance=acceptance())
                accepted = module.apply_native(initial, OPERATOR, case.native, case.project,
                                               operator=True, operators=[OPERATOR])
            else:
                case.propose()
            payload = dict(schema_version=1, operation='accept', operation_id='original-accept',
                           key=key, revision=1, record_sha256=case.sha(1),
                           acceptance_state='accepted', acceptance=acceptance())
            def apply(body):
                case.native.actor = OPERATOR
                return module.apply_native(body, OPERATOR, case.native, case.project,
                                           operator=True, operators=[OPERATOR])
            if not direct:
                accepted = apply(payload)
            receipt = case.project / module.JOURNAL / (content_hash({'operation_id': payload['operation_id']}) + '.json')
            yield case, module, payload, accepted, receipt, apply
        finally:
            case.doCleanups()

    def must_apply(self, apply, body):
        try:
            return apply(body)
        except ValueError as error:
            self.fail('A valid replay or pending-draft accept must succeed: ' + str(error))

    def assert_no_native_write(self, case, before):
        self.assertEqual(case.native.rows, before)
        self.assertEqual(case.native.writes(), [])

    def test_fresh_accept_of_accepted_revision_refuses_before_any_write(self):
        for kind in ('reference', 'capability'):
            with self.subTest(kind=kind), self.seeded(kind) as (case, module, payload, accepted, receipt, apply):
                before = copy.deepcopy(case.native.rows)
                files = {p.name: p.read_bytes() for p in receipt.parent.glob('*.json')}
                case.native.calls = []
                fresh = dict(payload, operation_id='fresh-accepted', revision=2, record_sha256=case.sha(2))
                with self.assertRaisesRegex(ValueError, r'Revision 2 .*already accepted; nothing waits to be accepted'):
                    apply(fresh)
                self.assert_no_native_write(case, before)
                self.assertEqual({p.name: p.read_bytes() for p in receipt.parent.glob('*.json')}, files)

    def test_fresh_identity_for_consumed_draft_refuses_with_existing_receipt(self):
        for kind in ('reference', 'capability'):
            with self.subTest(kind=kind), self.seeded(kind) as (case, module, payload, accepted, receipt, apply):
                before = copy.deepcopy(case.native.rows)
                case.native.calls = []
                with self.assertRaisesRegex(ValueError, r'Revision 2 .*already accepted; nothing waits to be accepted'):
                    apply(dict(payload, operation_id='fresh-consumed'))
                self.assert_no_native_write(case, before)

    def test_receipt_lost_preserves_native_only_noop_for_original_and_fresh_identity(self):
        for kind in ('reference', 'capability'):
            for operation in ('original-accept', 'fresh-consumed'):
                with self.subTest(kind=kind, operation=operation), self.seeded(kind) as (case, module, payload, accepted, receipt, apply):
                    receipt.unlink()
                    before = copy.deepcopy(case.native.rows)
                    case.native.calls = []
                    result = self.must_apply(apply, dict(payload, operation_id=operation))
                    self.assertEqual(result['revision'], 2)
                    self.assertEqual(result['record_comment_id'], accepted['record_comment_id'])
                    self.assert_no_native_write(case, before)

    def test_receipt_lost_does_not_allow_an_extra_accepted_revision(self):
        for kind in ('reference', 'capability'):
            with self.subTest(kind=kind), self.seeded(kind) as (case, module, payload, accepted, receipt, apply):
                receipt.unlink()
                before = copy.deepcopy(case.native.rows)
                case.native.calls = []
                with self.assertRaisesRegex(ValueError, 'already accepted'):
                    apply(dict(payload, operation_id='fresh-accepted', revision=2, record_sha256=case.sha(2)))
                self.assert_no_native_write(case, before)

    def revised(self, case, module, payload):
        from test_reference_records import entry as reference_entry
        from test_capability_records import entry as capability_entry
        entry = reference_entry if module.__name__ == 'reference_records' else capability_entry
        extra = {'statement': 'A genuine pending draft.'} if module.__name__ == 'reference_records' else {'summary': 'A genuine pending draft.'}
        body = entry(operation='revise', operation_id='new-draft', revision=3,
                     expected_sha256=case.sha(2), **extra)
        case.native.actor = 'alice'
        return module.apply_native(body, 'alice', case.native, case.project)

    def test_genuine_pending_draft_on_accepted_key_and_old_exact_retry_still_work(self):
        for kind in ('reference', 'capability'):
            with self.subTest(kind=kind), self.seeded(kind) as (case, module, payload, accepted, receipt, apply):
                self.assertEqual(self.revised(case, module, payload)['revision'], 3)
                before = copy.deepcopy(case.native.rows)
                case.native.calls = []
                self.assertEqual(self.must_apply(apply, payload)['revision'], 2)
                self.assert_no_native_write(case, before)
                fresh = dict(payload, operation_id='accept-pending', revision=3, record_sha256=case.sha(3))
                self.assertEqual(self.must_apply(apply, fresh)['revision'], 4)
                before = copy.deepcopy(case.native.rows)
                case.native.calls = []
                self.assertEqual(self.must_apply(apply, payload)['revision'], 2)
                self.assert_no_native_write(case, before)

    def test_removed_operators_inert_evidence_can_be_replaced_without_new_entry_revision(self):
        from test_reference_records import OPERATOR
        for kind in ('reference', 'capability'):
            with self.subTest(kind=kind), self.seeded(kind) as (case, module, payload, accepted, receipt, apply):
                case.native.actor = 'replacement'
                case.native.calls = []
                def replacement(body):
                    return module.apply_native(body, 'replacement', case.native, case.project,
                                               operator=True, operators=['replacement'])
                result = self.must_apply(replacement, dict(payload, operation_id='replacement-accept'))
                self.assertEqual(result['revision'], 2)
                writes = case.native.writes()
                self.assertEqual(len(writes), 1)
                self.assertTrue(writes[0][3].startswith('Kind: ' + kind + '-acceptance-v1'))
                row = case.native.rows[0]
                self.assertEqual(len(module.existing_revisions(row)), 2)
                self.assertIn(2, module.KIND.live_acceptances(row, [OPERATOR, 'replacement']))

    def test_inert_direct_and_newest_accepted_revisions_are_repairable(self):
        for kind in ('reference', 'capability'):
            for direct in (True, False):
                with self.subTest(kind=kind, direct=direct), self.seeded(kind, direct) as values:
                    case, module, payload, accepted, receipt, apply = values
                    revision = accepted['revision']
                    row = case.native.rows[0]
                    self.assertNotIn(revision, module.KIND.live_acceptances(row, ['replacement']))
                    previous = copy.deepcopy(row['comments'])
                    case.native.actor = 'replacement'
                    body = dict(payload, operation_id='repair-named-accepted', revision=revision,
                                record_sha256=case.sha(revision))
                    result = self.must_apply(lambda request: module.apply_native(
                        request, 'replacement', case.native, case.project, operator=True,
                        operators=['replacement']), body)
                    self.assertEqual(result['revision'], revision + 1)
                    self.assertEqual(row['comments'][:len(previous)], previous)
                    self.assertIn(revision + 1, module.KIND.live_acceptances(row, ['replacement']))
                    self.assertEqual(len(module.existing_revisions(row)), revision + 1)

    def test_changed_payload_and_damaged_selected_receipt_are_not_replays(self):
        for kind in ('reference', 'capability'):
            with self.subTest(kind=kind), self.seeded(kind) as (case, module, payload, accepted, receipt, apply):
                before = copy.deepcopy(case.native.rows)
                case.native.calls = []
                changed = dict(payload, acceptance=dict(payload['acceptance'], evidence='Different request'))
                with self.assertRaisesRegex(ValueError, 'different content'):
                    apply(changed)
                self.assert_no_native_write(case, before)
                receipt.write_text('{damaged', encoding='utf-8')
                with self.assertRaises(ValueError):
                    apply(payload)
                self.assert_no_native_write(case, before)

    def test_damaged_selected_receipt_status_cannot_authorize_replay(self):
        for kind in ('reference', 'capability'):
            with self.subTest(kind=kind), self.seeded(kind) as (case, module, payload, accepted, receipt, apply):
                saved = json.loads(receipt.read_text())
                saved['status'] = 'unusable-status'
                receipt.write_text(json.dumps(saved))
                before = copy.deepcopy(case.native.rows)
                case.native.calls = []
                with self.assertRaisesRegex(ValueError, 'receipt status'):
                    apply(payload)
                self.assert_no_native_write(case, before)

    def test_pending_original_receipt_reconciles_but_fresh_identity_is_refused(self):
        for kind in ('reference', 'capability'):
            with self.subTest(kind=kind), self.seeded(kind) as (case, module, payload, accepted, receipt, apply):
                saved = json.loads(receipt.read_text())
                saved.update(status='pending')
                saved.pop('acceptance', None)
                saved.pop('acceptance_record', None)
                receipt.write_text(json.dumps(saved))
                before = copy.deepcopy(case.native.rows)
                case.native.calls = []
                with self.assertRaisesRegex(ValueError, 'already accepted'):
                    apply(dict(payload, operation_id='fresh-consumed'))
                self.assert_no_native_write(case, before)
                self.assertEqual(self.must_apply(apply, payload)['revision'], 2)
                self.assert_no_native_write(case, before)

    def test_unrelated_receipt_id_and_hash_do_not_block_receiptless_noop(self):
        for kind in ('reference', 'capability'):
            for field in ('id', 'hash'):
                with self.subTest(kind=kind, field=field), self.seeded(kind) as (case, module, payload, accepted, receipt, apply):
                    saved = json.loads(receipt.read_text())
                    receipt.unlink()
                    if field == 'id':
                        saved['id'] = 'another-anchor'
                    else:
                        saved['acceptance']['record_sha256'] = 'f' * 64
                    (receipt.parent / ('a' * 64 + '.json')).write_text(json.dumps(saved))
                    before = copy.deepcopy(case.native.rows)
                    case.native.calls = []
                    self.assertEqual(self.must_apply(apply, dict(payload, operation_id='fresh-consumed'))['revision'], 2)
                    self.assert_no_native_write(case, before)


if __name__ == '__main__':
    unittest.main()
