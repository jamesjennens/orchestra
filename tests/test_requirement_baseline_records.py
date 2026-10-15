"""Capture completeness and frozen exact evidence, using the existing native seam."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requirement_baseline_records as baselines
import requirement_governance as governance
import requirement_http as http
import requirement_owner_records as owner
from requirements import content_hash
from test_requirement_owner_records import OwnerNative, ACCOUNT


class CompleteNative(OwnerNative):
    def __call__(self, args):
        if args[0] != 'list':
            return super().__call__(args)
        self.calls.append(list(args))
        rows = self.rows
        for flag, field in (('--label', 'labels'), ('--type', 'issue_type')):
            if flag in args:
                value = args[args.index(flag) + 1]
                rows = ([row for row in rows if value in row[field]] if field == 'labels'
                        else [row for row in rows if row[field] == value])
        return json.dumps(rows)


class FrozenRecordsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'alpha'; self.path.mkdir()
        governance.initialize(self.path, 'alpha', ACCOUNT, 'provision-alpha')
        self.context = http.OwnerContext('alpha', ACCOUNT, governance.current(self.path, 'alpha'))
        self.native = CompleteNative()

    def create(self, operation='create-one', kind='requirement'):
        return http.apply(self.path, self.context, 'create', dict(kind=kind, parent='job-1',
            title='One requirement', description='Exact original wording'), operation, self.native)

    def accept(self, record, operation='accept-one'):
        return http.apply(self.path, self.context, 'accept', dict(expected_revision=record['revision'],
            expected_sha256=record['sha256']), operation, self.native, record['id'])

    def capture(self):
        return baselines.capture(self.path, 'alpha', self.native)

    def snapshot(self):
        return baselines.build('alpha', 'Review-1', ACCOUNT, 'cut-one', self.capture(), '2099-01-01T00:00:00Z')

    def test_portable_names_are_case_insensitive_and_device_names_never_paths(self):
        self.assertEqual(baselines.baseline_name('ReViEw-1_A'), 'review-1_a')
        for bad in ('', '../escape', '/tmp', 'a.b', 'a/b', 'a\\b', 'CON', 'lPt9', 'a-', 'a_', 'a' * 65, 'é', 'a\x00b'):
            with self.subTest(name=repr(bad)), self.assertRaises(ValueError):
                baselines.baseline_name(bad)
        self.assertEqual(baselines.MAX_BASELINES, 64)
        self.assertEqual(baselines.MAX_SNAPSHOT_BYTES, 4194304)

    def test_one_export_and_complete_native_membership_freeze_pending_and_context(self):
        first = self.create(); accepted = self.accept(first)
        http.apply(self.path, self.context, 'revise', dict(expected_revision=accepted['revision'],
            expected_sha256=accepted['sha256'], title='Pending next wording'), 'edit-one', self.native, first['id'])
        self.create('narrative', 'brd-section')
        self.native.seed('question-1', labels=['question'], title='Which option?')
        closed = self.native.seed('decision-closed', issue_type='decision'); closed['status'] = 'closed'
        frozen_rows = copy.deepcopy(self.native.rows); self.native.calls.clear()
        value = self.snapshot()
        self.assertEqual(sum(call == ['export', '--all'] for call in self.native.calls), 1)
        self.assertEqual(self.native.writes(), [])
        self.assertEqual(frozen_rows, self.native.rows)
        requirement = next(item for item in value['view']['items'] if item['id'] == first['id'])
        self.assertEqual(requirement['accepted']['revision'], accepted['revision'])
        self.assertEqual(requirement['pending_draft']['revision'], accepted['revision'] + 1)
        self.assertEqual(requirement['acceptance']['account_id'], ACCOUNT)
        self.assertEqual(requirement['legacy_accepted_refs'], [])
        self.assertEqual(value['view']['questions'][0]['id'], 'question-1')
        self.assertNotIn('decision-closed', [entry['id'] for entry in value['view']['decisions']])
        self.assertNotIn('terminal_recoveries', value['view'])
        self.assertTrue(all('pending_transition' not in item for item in value['view']['items']))
        self.assertEqual(baselines.decode_snapshot(baselines.json_bytes(value)), value)

    def test_export_truncation_duplicate_and_lost_id_refuse_before_any_cut(self):
        self.create(); ordinary = self.native
        raw = ordinary(['export', '--all'])
        for damaged in ('', raw + raw.split('\n')[0] + '\n', raw + '{"title":"lost ID"}\n'):
            def run(args):
                return damaged if args == ['export', '--all'] else ordinary(args)
            with self.subTest(export=damaged[:30]), self.assertRaises(ValueError):
                baselines.capture(self.path, 'alpha', run)
        self.assertFalse((self.path / '.requirements-baselines').exists())

    def test_unrelated_malformed_row_is_isolated_but_selected_requirement_refuses(self):
        selected = self.create(); self.native.seed('noise-1')
        ordinary = self.native
        def run(args):
            if args == ['export', '--all']:
                return '\n'.join(json.dumps(dict(id=row['id'], malformed=True)) if row['id'] == damaged
                                 else json.dumps(row) for row in ordinary.rows) + '\n'
            return ordinary(args)
        damaged = 'noise-1'
        value = baselines.capture(self.path, 'alpha', run)
        self.assertEqual(value['view']['items'][0]['id'], selected['id'])
        damaged = selected['id']
        with self.assertRaisesRegex(ValueError, 'cannot be verified'):
            baselines.capture(self.path, 'alpha', run)

    def test_selected_malformed_open_decision_cannot_disappear(self):
        self.create(); self.native.seed('decision-1', issue_type='decision')
        ordinary = self.native
        def run(args):
            if args == ['export', '--all']:
                return '\n'.join(json.dumps(dict(id=row['id'], malformed=True)) if row['id'] == 'decision-1'
                                 else json.dumps(row) for row in ordinary.rows) + '\n'
            return ordinary(args)
        with self.assertRaisesRegex(ValueError, 'cannot be verified'):
            baselines.capture(self.path, 'alpha', run)

    def test_forged_owner_acceptance_author_refuses_capture(self):
        created = self.create(); self.accept(created)
        for comment in self.native.row(created['id'])['comments']:
            if comment['text'].startswith(owner.ACCEPTANCE_PREFIX):
                comment['author'] = 'attacker'
        with self.assertRaisesRegex(ValueError, 'cannot be verified'):
            self.capture()

    def test_later_governance_switch_does_not_reinterpret_a_frozen_snapshot(self):
        self.accept(self.create())
        value = self.snapshot(); raw = baselines.json_bytes(value)
        current = governance.current(self.path, 'alpha')
        governance.set_mode(self.path, 'alpha', 'governed', ACCOUNT, 'switch-mode', current['revision'], current['sha256'])
        self.assertEqual(baselines.decode_snapshot(raw), value)
        with self.assertRaisesRegex(ValueError, 'simple project'):
            baselines.build('alpha', 'new-cut', ACCOUNT, 'new-cut', self.capture(), '2026-01-01T00:00:00Z')

    def test_frozen_receipt_binds_file_bytes_and_all_historical_exact_refs(self):
        self.accept(self.create())
        value = self.snapshot(); receipt = baselines.receipt(value)
        self.assertEqual(receipt['files']['snapshot.json']['sha256'], baselines.byte_hash(baselines.json_bytes(value)))
        self.assertEqual(len(receipt['references']), 2)
        self.assertEqual(baselines.validate_receipt(receipt, value), receipt)
        changed = copy.deepcopy(receipt); changed['references'].pop(); changed['sha256'] = content_hash(changed)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            baselines.validate_receipt(changed, value)
        changed = copy.deepcopy(receipt); changed['files']['BRD.md']['sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            baselines.validate_receipt(changed, value)

    def test_self_resealed_wrong_bindings_and_history_gaps_still_refuse(self):
        self.accept(self.create()); value = self.snapshot()
        mutations = []
        changed = copy.deepcopy(value); changed['view']['items'][0]['history'].pop(0); mutations.append(changed)
        changed = copy.deepcopy(value); changed['view']['items'][0]['accepted']['description'] = 'forged'; mutations.append(changed)
        changed = copy.deepcopy(value); changed['view']['items'][0]['acceptance']['record_sha256'] = '0' * 64; mutations.append(changed)
        changed = copy.deepcopy(value); changed['account_id'] = 'worker-1'; mutations.append(changed)
        changed = copy.deepcopy(value); changed['name'] = 'REVIEW-1'; mutations.append(changed)
        changed = copy.deepcopy(value); changed['extra'] = True; mutations.append(changed)
        for changed in mutations:
            changed['source_sha256'] = content_hash({key: changed[key] for key in ('view', 'governance_files', 'native')})
            changed['sha256'] = content_hash(changed)
            with self.subTest(fields=list(changed)), self.assertRaises(ValueError):
                baselines.validate_snapshot(changed)

    def test_pending_terminal_receipt_refuses_a_cut_without_repairing_anything(self):
        created = self.create(); accepted = self.accept(created)
        self.native.fail_comment_prefix = owner.STATE_PREFIX
        with self.assertRaisesRegex(ValueError, 'native comment failed'):
            http.apply(self.path, self.context, 'withdraw', dict(expected_revision=accepted['revision'],
                expected_sha256=accepted['sha256'], expected_state_sha256=None, reason='Withdraw this'),
                'withdraw-pending', self.native, created['id'])
        rows = copy.deepcopy(self.native.rows)
        receipts = {path.name: path.read_bytes() for path in (self.path / http.JOURNAL).iterdir()}
        with self.assertRaisesRegex(ValueError, 'cannot be verified'):
            self.capture()
        self.assertEqual(rows, self.native.rows)
        self.assertEqual(receipts, {path.name: path.read_bytes() for path in (self.path / http.JOURNAL).iterdir()})

    def test_native_answers_and_markdown_have_independent_byte_bounds(self):
        self.create()
        with patch.object(baselines, 'MAX_EXPORT_BYTES', 1), self.assertRaisesRegex(ValueError, 'native answer'):
            self.capture()
        value = self.snapshot()
        with patch.object(baselines, 'MAX_MARKDOWN_BYTES', 1), self.assertRaisesRegex(ValueError, 'Markdown exceeds'):
            baselines.receipt(value)

    def test_canonical_encoding_duplicate_keys_and_size_limit_are_strict(self):
        self.create(); value = self.snapshot(); raw = baselines.json_bytes(value)
        with patch.object(baselines, 'MAX_SNAPSHOT_BYTES', len(raw)):
            self.assertEqual(baselines.decode_snapshot(raw), value)
        with patch.object(baselines, 'MAX_SNAPSHOT_BYTES', len(raw) - 1), self.assertRaises(ValueError):
            baselines.validate_snapshot(value)
        for damaged in (raw.replace(b'\n', b'\r\n'), raw[:-1], b'{"schema_version":1,"schema_version":1}'):
            with self.subTest(raw=damaged[:30]), self.assertRaisesRegex(ValueError, 'cannot be verified'):
                baselines.decode_snapshot(damaged)


if __name__ == '__main__':
    unittest.main()
