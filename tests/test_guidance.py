"""Standing guidance channel (kittrial-5bb.99 slice 1): record, version, read, ack."""
import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin
import briefing
import guidance
from work import queue

TASK = 'trial-task'
PROJECT = 'trial'
SESSION_ACTOR = 'session-11111111-1111-1111-1111-111111111111'
SESSION_ID = '11111111-1111-1111-1111-111111111111'


def rows():
    return [dict(id=TASK, title='A task', issue_type='task', status='in_progress',
                 assignee='worker-1', description='Intent', acceptance_criteria='Acceptance',
                 labels=[], comments=[])]


def session_registry(project):
    (project / '.sessions.json').write_text(json.dumps({'schema_version': 1, 'records': {
        SESSION_ID: {'request_id': SESSION_ID, 'actor': SESSION_ACTOR,
                     'name': 'worker', 'created_at': '2026-10-03T00:00:00+00:00'}}}), encoding='utf-8')


def inject_stale_ack(project, actor, version):
    """Write a stale acknowledgement by hand, as a record from an older revision."""
    meta = json.loads((project / '.guidance.json').read_text(encoding='utf-8'))
    meta['acknowledged'][actor] = {'version': version,
                                   'acknowledged_at': '2026-01-01T00:00:00+00:00'}
    (project / '.guidance.json').write_text(json.dumps(meta), encoding='utf-8')


class GuidanceRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.project = self.root / 'project'; self.project.mkdir()

    def test_absent_guidance_reads_as_absent_not_an_error(self):
        self.assertEqual(guidance.read_text(self.project), None)
        state = guidance.state(self.project, 'worker-1')
        self.assertFalse(state['present']); self.assertIsNone(state['version'])
        self.assertFalse(state['attention'])
        result = guidance.read(self.project, [])
        self.assertFalse(result['present']); self.assertIsNone(result['text'])

    def test_write_read_version_and_audit(self):
        first = guidance.write_guidance(self.project, 'Read the design note first.', 'operator-1')
        self.assertTrue(first['changed']); self.assertFalse(first['repaired'])
        self.assertEqual(first['version'], guidance.version_of('Read the design note first.'))
        self.assertIsNone(first['previous_version'])
        state = guidance.state(self.project, 'worker-1')
        self.assertTrue(state['present']); self.assertEqual(state['set_by'], 'operator-1')
        self.assertEqual(state['version'], first['version']); self.assertTrue(state['attention'])
        self.assertFalse(state['acknowledged'])
        self.assertNotIn('warning', state)
        self.assertEqual(guidance.read(self.project, ['get'])['text'], 'Read the design note first.')

    def test_second_write_keeps_previous_version_text_and_history(self):
        guidance.write_guidance(self.project, 'First guidance', 'operator-1')
        second = guidance.write_guidance(self.project, 'Second guidance', 'operator-2')
        meta = guidance.read_meta(self.project)
        self.assertEqual(second['previous_version'], guidance.version_of('First guidance'))
        self.assertEqual(meta['previous_text'], 'First guidance')
        self.assertEqual(len(meta['history']), 1)
        self.assertEqual(meta['history'][0]['version'], guidance.version_of('First guidance'))
        # What changed since the version this caller still had.
        result = guidance.read(self.project, ['get', '--since', second['previous_version']], 'worker-1')
        self.assertTrue(result['changed']); self.assertTrue(result['since_known'])
        self.assertEqual(result['previous_text'], 'First guidance')
        # Since the current version, nothing changed.
        self.assertFalse(guidance.read(self.project, ['get', '--since', second['version']])['changed'])
        # An unknown but well-formed version is flagged, not mistaken for a known one.
        unknown = guidance.read(self.project, ['get', '--since', 'a' * 64])
        self.assertTrue(unknown['changed']); self.assertFalse(unknown['since_known'])
        self.assertIn('warning', unknown)

    def test_unchanged_write_is_idempotent_and_keeps_audit(self):
        first = guidance.write_guidance(self.project, 'Same text', 'operator-1')
        again = guidance.write_guidance(self.project, 'Same text', 'operator-2')
        self.assertFalse(again['changed']); self.assertFalse(again['repaired'])
        self.assertEqual(guidance.read_meta(self.project)['set_by'], 'operator-1')
        self.assertEqual(again['version'], first['version'])

    def test_size_plain_text_and_actor_are_bounded(self):
        for bad in ['x' * 8001, '  ', '', 5]:
            with self.subTest(bad=str(bad)[:20]), self.assertRaises(ValueError):
                guidance.write_guidance(self.project, bad, 'operator-1')
        with self.assertRaises(ValueError):
            guidance.write_guidance(self.project, 'ok', 'not a valid actor!')
        with self.assertRaises(ValueError):
            guidance.write_guidance(self.project, 'binary\x00text', 'operator-1')
        with self.assertRaises(ValueError):
            guidance.validate_text('escape\x1b[31m')

    def test_bidi_zero_width_tag_and_c1_characters_are_refused(self):
        # kittrial-5bb.99 review `small` 4/7: these rendered as harmless text while
        # reordering or hiding what a reader sees. The U+E0000 tag block is a known
        # invisible-instruction vector and is refused too.
        for bad in ['safe\u202eevil', 'a\u200bb', 'a\u0085b', 'a\u2066b', 'a\ufeffb', 'a\u2028b',
                    'a\U000E0041b', '\U000E0001', 'a\U000E007Fb']:
            with self.subTest(bad=repr(bad)), self.assertRaisesRegex(ValueError, 'plain text'):
                guidance.validate_text(bad)

    def test_zwnj_and_zwj_are_allowed_between_letters_only(self):
        # kittrial-5bb.99 review `small` 4: ZWNJ/ZWJ are legitimate in Persian text
        # (and emoji), so a blanket refusal blocked real prose. They are allowed
        # between letters and refused anywhere else.
        for good in ['می\u200cروم', 'a\u200cb', 'a\u200db', 'می\u200dروم']:
            with self.subTest(good=repr(good)):
                self.assertEqual(guidance.validate_text(good), good)
        for bad in ['\u200ca', 'a\u200c', 'a \u200cb', 'a\u200c1', '1\u200cb', 'a\u200c.']:
            with self.subTest(bad=repr(bad)), self.assertRaisesRegex(ValueError, 'joiner'):
                guidance.validate_text(bad)

    def test_acknowledge_records_actor_and_clears_attention(self):
        guidance.write_guidance(self.project, 'Current guidance', 'operator-1')
        version = guidance.version_of('Current guidance')
        self.assertTrue(guidance.state(self.project, 'worker-1')['attention'])
        first = guidance.acknowledge(self.project, 'worker-1', version)
        self.assertFalse(first['reconciled']); self.assertEqual(first['version'], version)
        self.assertTrue(first['acknowledged'])
        self.assertFalse(guidance.state(self.project, 'worker-1')['attention'])
        self.assertTrue(guidance.state(self.project, 'worker-1')['acknowledged'])
        again = guidance.acknowledge(self.project, 'worker-1', version)
        self.assertTrue(again['reconciled'])
        self.assertEqual(again['acknowledged_at'], first['acknowledged_at'])

    def test_acknowledgement_names_the_version_it_read(self):
        guidance.write_guidance(self.project, 'First', 'operator-1')
        v1 = guidance.version_of('First')
        guidance.write_guidance(self.project, 'Second', 'operator-1')
        v2 = guidance.version_of('Second')
        with self.assertRaisesRegex(ValueError, 'Name the guidance version'):
            guidance.acknowledge(self.project, 'worker-1')
        # A stale version is refused WITHOUT naming the current one, so a caller
        # cannot ack a version it never read by copying it out of the refusal
        # (kittrial-5bb.99 review `small` 2): it must call `guidance get` first.
        with self.assertRaisesRegex(ValueError, 'not the current version') as stale:
            guidance.acknowledge(self.project, 'worker-1', v1)
        self.assertNotIn(v2, str(stale.exception))
        with self.assertRaisesRegex(ValueError, 'sha256'):
            guidance.acknowledge(self.project, 'worker-1', 'not-a-version')
        self.assertTrue(guidance.acknowledge(self.project, 'worker-1', v2)['acknowledged'])
        self.assertEqual(guidance.state(self.project, 'worker-1')['acknowledged_version'], v2)

    def test_any_actor_may_ack_its_own_read_without_a_registration(self):
        # kittrial-5bb.99 review `registration-gate-locks-out-unregistered-lanes`:
        # live lanes include legacy actors with no session record. Their attention
        # flag must be clearable, so the ack has no registration requirement.
        guidance.write_guidance(self.project, 'Text', 'operator-1')
        version = guidance.version_of('Text')
        self.assertTrue(guidance.acknowledge(self.project, 'legacy-lane', version)['acknowledged'])
        self.assertFalse(guidance.state(self.project, 'legacy-lane')['attention'])

    def test_new_guidance_returns_attention_to_an_acknowledged_actor(self):
        guidance.write_guidance(self.project, 'First', 'operator-1')
        guidance.acknowledge(self.project, 'worker-1', guidance.version_of('First'))
        self.assertFalse(guidance.state(self.project, 'worker-1')['attention'])
        guidance.write_guidance(self.project, 'Second', 'operator-1')
        state = guidance.state(self.project, 'worker-1')
        self.assertTrue(state['attention'])
        self.assertFalse(guidance.state(self.project, 'worker-2')['acknowledged'])

    def test_stale_acknowledgement_and_missing_audit_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'No guidance'):
            guidance.acknowledge(self.project, 'worker-1')
        guidance.write_guidance(self.project, 'Text', 'operator-1')
        self.assertTrue(guidance.acknowledge(self.project, 'worker-1',
                                             guidance.version_of('Text'))['acknowledged'])
        # A text without audit metadata is still readable, but an acknowledgement is
        # refused rather than fabricating the audit record.
        (self.project / '.guidance.json').unlink()
        self.assertEqual(guidance.read_text(self.project), 'Text')
        self.assertIsNone(guidance.read_meta(self.project))
        with self.assertRaisesRegex(ValueError, 'audit metadata'):
            guidance.acknowledge(self.project, 'worker-1', guidance.version_of('Text'))

    def test_reader_tolerates_unknown_metadata_keys(self):
        guidance.write_guidance(self.project, 'Text', 'operator-1')
        meta = json.loads((self.project / '.guidance.json').read_text(encoding='utf-8'))
        meta['future_field'] = {'anything': True}
        (self.project / '.guidance.json').write_text(json.dumps(meta), encoding='utf-8')
        self.assertEqual(guidance.read_text(self.project), 'Text')
        self.assertEqual(guidance.state(self.project, 'worker-1')['set_by'], 'operator-1')

    def test_status_is_operator_only_and_lists_acks(self):
        guidance.write_guidance(self.project, 'Text', 'operator-1')
        guidance.acknowledge(self.project, 'worker-1', guidance.version_of('Text'))
        with self.assertRaisesRegex(ValueError, 'configured operator'):
            guidance.status(self.project, 'worker-1', ['operator-1'])
        with self.assertRaisesRegex(ValueError, 'No operator allowlist'):
            guidance.status(self.project, 'operator-1', [])
        report = guidance.status(self.project, 'operator-1', ['operator-1'])
        self.assertEqual(report['version'], guidance.version_of('Text'))
        self.assertEqual([row['actor'] for row in report['acknowledged']], ['worker-1'])
        self.assertTrue(report['acknowledged'][0]['current'])
        self.assertEqual(report['behind'], []); self.assertEqual(report['stale'], [])
        self.assertIsNone(report['compact_hint'])

    def test_status_prints_the_previous_text_and_classifies_stale_acks(self):
        guidance.write_guidance(self.project, 'v1', 'operator-1')
        guidance.write_guidance(self.project, 'v2', 'operator-1')
        guidance.acknowledge(self.project, 'behind-lane', guidance.version_of('v2'))
        guidance.write_guidance(self.project, 'v3', 'operator-1')
        # A set already drops acks older than the current and previous version; a
        # record from an older revision can still carry one, which status marks stale.
        inject_stale_ack(self.project, 'stale-lane', guidance.version_of('v1'))
        report = guidance.status(self.project, 'operator-1', ['operator-1'], host=True)
        self.assertEqual(report['text'], 'v3')
        self.assertEqual(report['previous_text'], 'v2')
        self.assertEqual(report['behind'], ['behind-lane'])
        self.assertEqual(report['stale'], ['stale-lane'])
        self.assertIn('compact-guidance-acks', report['compact_hint'])
        # The endpoint view (no host) shows the classification but no guidance content.
        endpoint_report = guidance.status(self.project, 'operator-1', ['operator-1'])
        self.assertEqual(endpoint_report['behind'], ['behind-lane'])
        self.assertEqual(endpoint_report['stale'], ['stale-lane'])
        self.assertNotIn('text', endpoint_report)
        self.assertNotIn('previous_text', endpoint_report)
        self.assertNotIn('history', endpoint_report)

    def test_brief_block_never_raises_and_marks_unreadable_guidance(self):
        self.assertIsNone(guidance.brief_block(None))
        block = guidance.brief_block(self.project, 'worker-1')
        self.assertFalse(block['present'])
        (self.project / 'GUIDANCE.md').write_text('x' * 9000, encoding='utf-8')
        broken = guidance.brief_block(self.project, 'worker-1')
        self.assertIsNone(broken['present']); self.assertIn('warning', broken)
        # A worker keyed on attention must not miss unreadable guidance.
        self.assertTrue(broken['attention']); self.assertTrue(broken['unreadable'])

    def test_unreadable_guidance_get_is_not_an_error(self):
        (self.project / 'GUIDANCE.md').write_text('x' * 9000, encoding='utf-8')
        result = guidance.read(self.project, ['get'], 'worker-1')
        self.assertIsNone(result['present']); self.assertIsNone(result['text'])
        self.assertTrue(result['unreadable']); self.assertTrue(result['attention'])
        self.assertIn('warning', result)
        self.assertTrue(guidance.state(self.project, 'worker-1')['attention'])

    def test_deeply_nested_metadata_is_a_read_with_a_warning_not_a_crash(self):
        guidance.write_guidance(self.project, 'Text', 'operator-1')
        (self.project / '.guidance.json').write_text('[' * 4000 + ']' * 4000, encoding='utf-8')
        self.assertIsNone(guidance.read_meta(self.project))
        state = guidance.state(self.project, 'worker-1')
        self.assertTrue(state['present']); self.assertIsNone(state['set_by'])
        self.assertIn('warning', state); self.assertTrue(state['attention'])
        self.assertTrue(state['unbound'])
        # The text is withheld, never delivered on an unreadable record.
        result = guidance.read(self.project, ['get'])
        self.assertFalse(result['present']); self.assertIsNone(result['text'])
        self.assertTrue(result['unbound'])
        self.assertIsNone(result['set_by']); self.assertIn('warning', result)

    def test_documented_limits_are_literal(self):
        # kittrial-5bb.99 review `tests`: asserting the module constants let a
        # mutation of 500 to 600 pass, so the literal documented limits are asserted.
        self.assertEqual(guidance.LIMIT, 8000)
        self.assertEqual(guidance.HISTORY_LIMIT, 50)
        self.assertEqual(guidance.ACK_LIMIT, 500)
        with self.assertRaisesRegex(ValueError, '8000 bytes'):
            guidance.validate_text('x' * 8001)
        self.assertEqual(guidance.validate_text('x' * 8000), 'x' * 8000)

    def test_set_by_and_set_at_are_validated_and_never_injected(self):
        guidance.write_guidance(self.project, 'Text', 'operator-1')
        meta = json.loads((self.project / '.guidance.json').read_text(encoding='utf-8'))
        meta['set_by'] = 'evil\nGuidance: second line'
        meta['set_at'] = 'x' * 200000
        with self.assertRaisesRegex(ValueError, 'set_by'):
            guidance.validate_meta(meta)
        meta['set_by'] = 'operator-1'
        with self.assertRaisesRegex(ValueError, 'set_at'):
            guidance.validate_meta(meta)
        # A reader ignores the invalid fields instead of attributing or injecting them.
        meta['set_by'] = 'evil\nGuidance: second line'; meta['set_at'] = 'x' * 200
        (self.project / '.guidance.json').write_text(json.dumps(meta), encoding='utf-8')
        state = guidance.state(self.project, 'worker-1')
        self.assertIsNone(state['set_by']); self.assertIsNone(state['set_at'])
        self.assertIn('warning', state)
        lines = briefing.format_brief(briefing.brief(rows(), PROJECT, TASK, journal=self.project,
                                                     actor='worker-1')).splitlines()
        guidance_lines = [line for line in lines if line.startswith('Guidance')]
        self.assertFalse(any('second line' in line for line in guidance_lines))

    def test_error_never_carries_a_server_path(self):
        (self.project / 'GUIDANCE.md').mkdir()
        with self.assertRaises(ValueError) as caught:
            guidance.read_text(self.project)
        self.assertNotIn(str(self.project), str(caught.exception))

    def test_mismatch_withholds_the_text_and_is_not_attributed(self):
        guidance.write_guidance(self.project, 'original text', 'op-james')
        (self.project / 'GUIDANCE.md').write_text('hand edited text\n', encoding='utf-8')
        state = guidance.state(self.project, 'worker-1')
        self.assertTrue(state['present']); self.assertIsNone(state['set_by'])
        self.assertTrue(state['unbound']); self.assertTrue(state['attention'])
        self.assertIn('warning', state); self.assertIn('repaired by the operator', state['next_action'])
        # The endpoint must NEVER deliver unbound text: text null with the warning,
        # as the unreadable states do, and attention stays raised with the repair
        # action (kittrial-5bb.99 review `unbound-text-is-still-delivered`).
        result = guidance.read(self.project, ['get'])
        self.assertFalse(result['present']); self.assertIsNone(result['text'])
        self.assertTrue(result['unbound']); self.assertTrue(result['attention'])
        self.assertIsNone(result['set_by']); self.assertIn('warning', result)
        self.assertIn('repaired by the operator', result['next_action'])
        self.assertNotIn('hand edited text', json.dumps(result))
        # The host guidance-status read may still show the text to the operator.
        report = guidance.status(self.project, 'op-james', ['op-james'], host=True)
        self.assertIsNone(report['set_by']); self.assertIn('warning', report)
        self.assertTrue(report['text_unbound']); self.assertEqual(report['text'], 'hand edited text\n')
        # The endpoint status view does not show guidance text at all.
        endpoint_report = guidance.status(self.project, 'op-james', ['op-james'])
        self.assertNotIn('text', endpoint_report); self.assertNotIn('previous_text', endpoint_report)
        self.assertNotIn('history', endpoint_report)
        block = guidance.brief_block(self.project, 'worker-1')
        self.assertIsNone(block['set_by']); self.assertIn('warning', block)
        self.assertTrue(block['unbound'])
        # A crash between the two writes is the same: the new text is withheld and is
        # never credited to the previous setter.
        guidance.write_text(self.project / 'GUIDANCE.md', 'op-two text')
        crashed = guidance.read(self.project, ['get'])
        self.assertIsNone(crashed['text']); self.assertTrue(crashed['unbound'])
        self.assertIsNone(crashed['set_by']); self.assertIn('warning', crashed)

    def test_same_text_repair_keeps_the_replaced_generation_known(self):
        # kittrial-5bb.99 review `audit-gaps` 1: a crashed set leaves the file at the
        # new text while the record still names the previous generation. Repairing
        # with the same text must not write previous_version == version, must not
        # write the current text as previous_text, and must keep the replaced
        # generation in history so `get --since` knows it.
        guidance.write_guidance(self.project, 'first generation', 'op-james')
        v1 = guidance.version_of('first generation')
        # op-two's set reached the file, then crashed before the metadata write.
        guidance.write_text(self.project / 'GUIDANCE.md', 'second generation')
        v2 = guidance.version_of('second generation')
        repaired = guidance.write_guidance(self.project, 'second generation', 'op-two')
        self.assertTrue(repaired['repaired']); self.assertFalse(repaired['changed'])
        meta = guidance.read_meta(self.project)
        self.assertEqual(meta['version'], v2)
        self.assertNotEqual(meta['previous_version'], v2)
        self.assertIsNone(meta['previous_text'])
        self.assertIn(v1, [entry['version'] for entry in meta['history']])
        result = guidance.read(self.project, ['get', '--since', v1])
        self.assertTrue(result['since_known'])
        self.assertTrue(result['changed'])
        self.assertEqual(result['text'], 'second generation')

    def test_same_text_set_repairs_a_missing_record(self):
        # Crash on the first set: text written, no metadata.
        guidance.write_text(self.project / 'GUIDANCE.md', 'crashed first set')
        self.assertIsNone(guidance.read_meta(self.project))
        repaired = guidance.write_guidance(self.project, 'crashed first set', 'op-james')
        self.assertTrue(repaired['repaired']); self.assertFalse(repaired['changed'])
        meta = guidance.read_meta(self.project)
        self.assertEqual(meta['set_by'], 'op-james')
        self.assertEqual(meta['version'], guidance.version_of('crashed first set'))
        self.assertEqual(meta['history'], [])
        # A repair with no earlier generation must not become its own previous
        # version or name the current text as the previous text.
        self.assertIsNone(meta['previous_version']); self.assertIsNone(meta['previous_text'])
        self.assertTrue(guidance.acknowledge(self.project, 'worker-1',
                                             meta['version'])['acknowledged'])

    def test_different_text_set_over_stale_metadata_does_not_misattribute_history(self):
        guidance.write_guidance(self.project, 'first real text', 'op-james')
        # op-two's text reached the file but its metadata write crashed.
        guidance.write_text(self.project / 'GUIDANCE.md', 'crashed op-two text')
        result = guidance.write_guidance(self.project, 'real second text', 'op-james')
        self.assertTrue(result['changed']); self.assertTrue(result['repaired'])
        meta = guidance.read_meta(self.project)
        self.assertEqual(meta['set_by'], 'op-james')
        crashed = [entry for entry in meta['history']
                   if entry['version'] == guidance.version_of('crashed op-two text')]
        self.assertEqual(len(crashed), 1)
        self.assertIsNone(crashed[0]['set_by']); self.assertIsNone(crashed[0]['set_at'])

    def test_ack_table_is_pruned_at_each_set_and_evicts_oldest_when_full(self):
        guidance.write_guidance(self.project, 'v1', 'operator-1')
        guidance.acknowledge(self.project, 'lane-1', guidance.version_of('v1'))
        guidance.write_guidance(self.project, 'v2', 'operator-1')
        guidance.acknowledge(self.project, 'lane-2', guidance.version_of('v2'))
        guidance.write_guidance(self.project, 'v3', 'operator-1')
        table = guidance.read_meta(self.project)['acknowledged']
        self.assertEqual(set(table), {'lane-2'})
        self.assertEqual(table['lane-2']['version'], guidance.version_of('v2'))

        guidance.write_guidance(self.project, 'fill', 'operator-1')
        version = guidance.version_of('fill')
        for i in range(guidance.ACK_LIMIT):
            guidance.acknowledge(self.project, 'fake-%d' % i, version)
        self.assertEqual(len(guidance.read_meta(self.project)['acknowledged']), guidance.ACK_LIMIT)
        # A new lane is admitted (the oldest entry is evicted), never refused.
        result = guidance.acknowledge(self.project, 'new-lane', version)
        self.assertTrue(result['acknowledged'])
        table = guidance.read_meta(self.project)['acknowledged']
        self.assertEqual(len(table), guidance.ACK_LIMIT)
        self.assertIn('new-lane', table)

    def test_history_is_bounded(self):
        for i in range(guidance.HISTORY_LIMIT + 10):
            guidance.write_guidance(self.project, 'text %d' % i, 'operator-1')
        self.assertEqual(len(guidance.read_meta(self.project)['history']), guidance.HISTORY_LIMIT)

    def test_compact_drops_stale_acks_and_is_audited(self):
        guidance.write_guidance(self.project, 'v1', 'operator-1')
        guidance.write_guidance(self.project, 'v2', 'operator-1')
        guidance.acknowledge(self.project, 'current-lane', guidance.version_of('v2'))
        guidance.write_guidance(self.project, 'v3', 'operator-1')
        inject_stale_ack(self.project, 'stale-lane', guidance.version_of('v1'))
        result = guidance.compact(self.project, 'operator-1')
        self.assertEqual(result['removed'], ['stale-lane'])
        self.assertEqual(result['kept'], ['current-lane'])
        meta = guidance.read_meta(self.project)
        self.assertEqual(meta['acks_compacted_by'], 'operator-1')
        self.assertTrue(meta['acks_compacted_at'])

    def test_clear_removes_both_files_and_keeps_a_local_audit_record(self):
        guidance.write_guidance(self.project, 'Text', 'operator-1')
        version = guidance.version_of('Text')
        result = guidance.clear(self.project, 'operator-1')
        self.assertEqual(result['removed'], sorted([guidance.GUIDANCE_NAME, guidance.META_NAME]))
        self.assertIsNone(guidance.read_text(self.project))
        self.assertIsNone(guidance.read_meta(self.project))
        # kittrial-5bb.99 review `audit-gaps` 2: clearing is no longer recorded only
        # on stdout. The local record keeps who, when and the cleared version and is
        # itself strictly valid.
        self.assertEqual(result['cleared_version'], version)
        record = guidance.read_clear_record(self.project)
        self.assertTrue((self.project / guidance.CLEAR_NAME).is_file())
        self.assertEqual(record['clears'][0]['cleared_by'], 'operator-1')
        self.assertEqual(record['clears'][0]['cleared_version'], version)
        self.assertTrue(record['clears'][0]['cleared_at'])
        guidance.validate_clear_record(record)
        # A second clear keeps the earlier one in the bounded history.
        guidance.write_guidance(self.project, 'Second', 'operator-2')
        guidance.clear(self.project, 'operator-2')
        record = guidance.read_clear_record(self.project)
        self.assertEqual([entry['cleared_by'] for entry in record['clears']], ['operator-2', 'operator-1'])

    def test_compaction_audit_survives_later_sets(self):
        # kittrial-5bb.99 review `audit-gaps` 3: a set dropped acks_compacted_by and
        # acks_compacted_at, losing who compacted the table and when.
        guidance.write_guidance(self.project, 'v1', 'operator-1')
        guidance.write_guidance(self.project, 'v2', 'operator-1')
        guidance.write_guidance(self.project, 'v3', 'operator-1')
        inject_stale_ack(self.project, 'stale-lane', guidance.version_of('v1'))
        guidance.compact(self.project, 'operator-1')
        guidance.write_guidance(self.project, 'v4', 'operator-1')
        meta = guidance.read_meta(self.project)
        self.assertEqual(meta['acks_compacted_by'], 'operator-1')
        self.assertTrue(meta['acks_compacted_at'])


    def test_symlinked_guidance_is_refused(self):
        secret = self.root / 'secret'; secret.write_text('secret', encoding='utf-8')
        try:
            (self.project / 'GUIDANCE.md').symlink_to(secret)
        except OSError:
            self.skipTest('No symlink privilege')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            guidance.read_text(self.project)
        # clear does not follow a symlink either, so that state needs a manual delete
        # (kittrial-5bb.99 review `audit-gaps` 2).
        with self.assertRaisesRegex(ValueError, 'symlink'):
            guidance.clear(self.project, 'operator-1')
        self.assertTrue((self.project / 'GUIDANCE.md').is_symlink())

    def test_brief_and_work_and_resume_carry_the_version_block(self):
        guidance.write_guidance(self.project, 'Standing instruction', 'operator-1')
        result = briefing.brief(rows(), PROJECT, TASK, journal=self.project, actor='worker-1')
        self.assertTrue(result['guidance']['present'])
        self.assertEqual(result['guidance']['version'], guidance.version_of('Standing instruction'))
        self.assertTrue(result['guidance']['attention'])
        self.assertIn('Guidance: version', briefing.format_brief(result))
        page = queue(rows(), 'worker-1', [], journal=self.project)
        self.assertEqual(page['guidance']['version'], guidance.version_of('Standing instruction'))
        from sessions import execute as sessions_execute
        actor = SESSION_ACTOR
        session_registry(self.project)
        answer = sessions_execute(self.project, PROJECT, ['resume', '--request-id', '22222222-2222-2222-2222-222222222222'],
                                  lambda: '', actor=actor)
        self.assertEqual(answer['guidance']['version'], guidance.version_of('Standing instruction'))


class GuidanceBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.project = self.root / 'projects' / 'example'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        guidance.write_guidance(self.project, 'Be careful with X', 'operator-1')
        guidance.acknowledge(self.project, 'worker-1', guidance.version_of('Be careful with X'))

    def files(self):
        return {'GUIDANCE.md': {'text': guidance.read_text(self.project)},
                '.guidance.json': guidance.read_meta(self.project)}

    def test_valid_pair_passes_validation_and_round_trips_through_restore(self):
        files = self.files()
        admin.validate_coordination_files(files)
        dest = self.root / 'projects' / 'dest'; dest.mkdir(parents=True)
        with patch.object(admin, 'coordination_backup', return_value=files):
            admin.restore_coordination(self.root, 'source', 'dest')
        self.assertEqual(guidance.read_text(dest), 'Be careful with X')
        self.assertEqual(guidance.state(dest, 'worker-1')['version'], guidance.version_of('Be careful with X'))
        self.assertTrue(guidance.state(dest, 'worker-1')['acknowledged'])

    def test_restore_new_with_a_backup_that_has_no_guidance_pair_reads_as_no_guidance(self):
        # kittrial-5bb.99 review `guidance-fault-skips-the-project-backup`: a degraded
        # backup carries no guidance pair. restore-new restores the tracker and the
        # project reads `present: false`, never a mismatched pair.
        files = {'.sessions.json': {'schema_version': 1, 'records': {}}}
        dest = self.root / 'projects' / 'noguidance'; dest.mkdir(parents=True)
        with patch.object(admin, 'coordination_backup', return_value=files):
            admin.restore_coordination(self.root, 'source', 'noguidance')
        self.assertIsNone(guidance.read_text(dest))
        self.assertIsNone(guidance.read_meta(dest))
        self.assertFalse(guidance.state(dest, 'worker-1')['present'])

    def test_malformed_guidance_records_are_refused(self):
        for bad in [{'GUIDANCE.md': {'text': ''}}, {'GUIDANCE.md': {'text': 'x', 'other': 1}},
                    {'.guidance.json': {'schema_version': 2}},
                    {'.guidance.json': dict(self.files()['.guidance.json'], unknown=1)},
                    {'.guidance.json': {}}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                admin.validate_coordination_files(bad)

    def test_mismatched_guidance_pair_is_refused(self):
        meta = self.files()['.guidance.json']
        mismatched = {'GUIDANCE.md': {'text': 'hand edited text'},
                      '.guidance.json': meta}
        with self.assertRaisesRegex(ValueError, 'does not match'):
            admin.validate_coordination_files(mismatched)
        with self.assertRaisesRegex(ValueError, 'together'):
            admin.validate_coordination_files({'GUIDANCE.md': {'text': 'text'}})
        with self.assertRaisesRegex(ValueError, 'together'):
            admin.validate_coordination_files({'.guidance.json': meta})

    def backup(self, names=(), all_projects=False):
        fake = types.SimpleNamespace(flock=Mock(), LOCK_EX=2)
        with patch.dict(sys.modules, {'fcntl': fake}), patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_projects(self.root, list(names), all_projects=all_projects)

    def make_second_project(self, name='second'):
        project = self.root / 'projects' / name
        (project / '.beads').mkdir(parents=True)
        (project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        guidance.write_guidance(project, 'Second guidance', 'operator-1')
        return project

    def test_mismatched_pair_degrades_but_does_not_skip_the_tracker_backup(self):
        # kittrial-5bb.99 review `guidance-fault-skips-the-project-backup`: the whole
        # project (including its tracker backup) was skipped for a two-file guidance
        # fault. Now everything else is backed up, the bad pair is left out, and the
        # project is marked degraded.
        (self.root / 'backups' / 'example').mkdir(parents=True)
        (self.project / 'GUIDANCE.md').write_text('hand edited text\n', encoding='utf-8')
        err = io.StringIO()
        fake = types.SimpleNamespace(flock=Mock(), LOCK_EX=2)
        with patch.dict(sys.modules, {'fcntl': fake}), patch.object(admin, 'run_bd', return_value='synced'), \
                contextlib.redirect_stderr(err):
            self.assertEqual(admin.backup_project(self.root, 'example'), 'synced')
        self.assertIn('guidance is degraded', err.getvalue())
        self.assertIn('set-guidance', err.getvalue())
        fragment, fault = admin.guidance_backup_pair(self.project)
        self.assertEqual(fragment, {})
        self.assertIn('guidance is degraded', fault)
        sidecar = json.loads((self.root / 'backups' / 'example.coordination.json').read_text(encoding='utf-8'))
        self.assertEqual(sidecar['status'], 'complete')
        self.assertNotIn('GUIDANCE.md', sidecar['files'])
        self.assertNotIn('.guidance.json', sidecar['files'])

    def test_backup_all_marks_the_degraded_project_and_keeps_the_status_complete(self):
        self.make_second_project()
        (self.root / 'backups' / 'example').mkdir(parents=True)
        (self.root / 'backups' / 'second').mkdir(parents=True)
        (self.project / 'GUIDANCE.md').write_text('hand edited text\n', encoding='utf-8')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.backup(all_projects=True)
        # The run exits 0 (no SystemExit above) because every tracker backup is usable.
        self.assertIn('degraded', err.getvalue())
        record = json.loads((self.root / 'backups' / admin.BACKUP_STATUS_NAME).read_text(encoding='utf-8'))
        self.assertEqual(record['status'], 'complete')
        entries = {entry['name']: entry for entry in record['projects']}
        self.assertEqual(entries['example']['status'], 'complete')
        self.assertIn('degraded', entries['example'])
        self.assertEqual(entries['second']['status'], 'complete')
        self.assertNotIn('degraded', entries['second'])
        # The healthy project's guidance pair still round-trips.
        second = json.loads((self.root / 'backups' / 'second.coordination.json').read_text(encoding='utf-8'))
        self.assertIn('GUIDANCE.md', second['files'])
        self.assertIn('.guidance.json', second['files'])

    def test_backup_status_degraded_entry_is_validated(self):
        stamp = admin.utc_stamp()
        entry = {'name': 'example', 'status': 'complete', 'completed_at': stamp,
                 'pair': {'native': 'backups/example', 'coordination': 'backups/example.coordination.json'},
                 'degraded': 'guidance is degraded: repair it'}
        record = {'schema_version': 1, 'scope': 'all', 'generated_at': stamp, 'status': 'complete',
                  'projects': [entry]}
        admin.validate_backup_status(record)
        with self.assertRaises(ValueError):
            admin.validate_backup_status(dict(record, projects=[dict(entry, degraded='')]))
        with self.assertRaises(ValueError):
            admin.validate_backup_status(dict(record, projects=[dict(entry, status='failed', reason='x',
                                                                     degraded='x')]))


class GuidanceAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.project = self.root / 'projects' / 'example'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        self.document = self.root / 'guidance.md'
        self.document.write_text('Operator guidance\n', encoding='utf-8')

    def run_admin(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            admin.main()
        return out.getvalue(), err.getvalue()

    @unittest.skipIf(sys.platform == 'win32', 'admin.py host commands take the POSIX lock')
    def test_only_a_configured_operator_may_set_guidance(self):
        with self.assertRaisesRegex(ValueError, 'No operator allowlist'):
            self.run_admin(['set-guidance', 'example', '--actor', 'operator-1', '--file', str(self.document)])
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'operators': ['operator-1']}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'configured operator'):
            self.run_admin(['set-guidance', 'example', '--actor', 'worker-1', '--file', str(self.document)])
        stdout, _ = self.run_admin(['set-guidance', 'example', '--actor', 'operator-1', '--file', str(self.document)])
        self.assertIn('installed', stdout)
        self.assertEqual(guidance.read_text(self.project), 'Operator guidance\n')
        self.assertEqual(guidance.read_meta(self.project)['set_by'], 'operator-1')

    @unittest.skipIf(sys.platform == 'win32', 'admin.py host commands take the POSIX lock')
    def test_same_text_set_reports_repaired(self):
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'operators': ['operator-1']}), encoding='utf-8')
        guidance.write_text(self.project / 'GUIDANCE.md', 'Operator guidance\n')
        stdout, _ = self.run_admin(['set-guidance', 'example', '--actor', 'operator-1', '--file', str(self.document)])
        self.assertIn('repaired', stdout)
        self.assertEqual(guidance.read_meta(self.project)['set_by'], 'operator-1')

    @unittest.skipIf(sys.platform == 'win32', 'admin.py host commands take the POSIX lock')
    def test_guidance_status_cli_is_operator_only(self):
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'operators': ['operator-1']}), encoding='utf-8')
        guidance.write_guidance(self.project, 'Guidance', 'operator-1')
        guidance.acknowledge(self.project, 'worker-1', guidance.version_of('Guidance'))
        with self.assertRaisesRegex(ValueError, 'configured operator'):
            self.run_admin(['guidance-status', 'example', '--actor', 'worker-1'])
        stdout, _ = self.run_admin(['guidance-status', 'example', '--actor', 'operator-1'])
        report = json.loads(stdout)
        self.assertEqual(report['up_to_date'], ['worker-1'])
        # The host read is authoritative and shows the text (the endpoint view does not).
        self.assertEqual(report['text'], 'Guidance')

    @unittest.skipIf(sys.platform == 'win32', 'admin.py host commands take the POSIX lock')
    def test_clear_and_compact_host_commands_need_the_operator_allowlist(self):
        guidance.write_guidance(self.project, 'v1', 'operator-1')
        guidance.write_guidance(self.project, 'v2', 'operator-1')
        guidance.write_guidance(self.project, 'v3', 'operator-1')
        inject_stale_ack(self.project, 'stale-lane', guidance.version_of('v1'))
        with self.assertRaisesRegex(ValueError, 'No operator allowlist'):
            self.run_admin(['compact-guidance-acks', 'example', '--actor', 'operator-1'])
        with self.assertRaisesRegex(ValueError, 'No operator allowlist'):
            self.run_admin(['clear-guidance', 'example', '--actor', 'operator-1'])
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'operators': ['operator-1']}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'configured operator'):
            self.run_admin(['compact-guidance-acks', 'example', '--actor', 'worker-1'])
        stdout, _ = self.run_admin(['compact-guidance-acks', 'example', '--actor', 'operator-1'])
        self.assertIn('removed 1', stdout)
        self.assertEqual(guidance.read_meta(self.project)['acks_compacted_by'], 'operator-1')
        stdout, _ = self.run_admin(['clear-guidance', 'example', '--actor', 'operator-1'])
        self.assertIn('cleared', stdout)
        self.assertIsNone(guidance.read_text(self.project))


class GuidanceEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.project = self.root / 'projects' / 'example'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'operators': ['operator-1']}), encoding='utf-8')
        session_registry(self.project)
        guidance.write_guidance(self.project, 'Endpoint guidance', 'operator-1')
        self.version = guidance.version_of('Endpoint guidance')
        self.text_bytes = (self.project / 'GUIDANCE.md').read_bytes()
        self.meta_bytes = (self.project / '.guidance.json').read_bytes()

    def call(self, action, args, actor=SESSION_ACTOR):
        import endpoint
        return endpoint.execute(self.root, {'project': 'example', 'actor': actor, 'action': action, 'args': args})

    def assert_files_unchanged(self):
        self.assertEqual((self.project / 'GUIDANCE.md').read_bytes(), self.text_bytes)
        self.assertEqual((self.project / '.guidance.json').read_bytes(), self.meta_bytes)

    @unittest.skipIf(sys.platform == 'win32', 'endpoint.py uses the POSIX coordination lock')
    def test_get_ack_version_and_status_route_through_the_endpoint(self):
        import endpoint  # noqa: F401  (import here: endpoint imports fcntl)
        answer = json.loads(self.call('guidance', [])['stdout'])
        self.assertEqual(answer['text'], 'Endpoint guidance')
        self.assertTrue(answer['attention'])
        version = answer['version']
        acked = json.loads(self.call('guidance', ['ack', '--version', version])['stdout'])
        self.assertTrue(acked['acknowledged'])
        again = json.loads(self.call('guidance', ['ack', '--version', version])['stdout'])
        self.assertTrue(again['reconciled'])
        self.assertFalse(json.loads(self.call('guidance', ['version'])['stdout'])['attention'])
        self.assertEqual(json.loads(self.call('guidance', ['get', '--since', version])['stdout'])['changed'], False)
        with self.assertRaisesRegex(ValueError, 'configured operator'):
            self.call('guidance', ['status'], actor='worker-1')
        report = json.loads(self.call('guidance', ['status'], actor='operator-1')['stdout'])
        self.assertEqual(report['up_to_date'], [SESSION_ACTOR])
        # kittrial-5bb.99 review `small` 1: endpoint status shows versions and actor
        # names, never guidance text; the host read is the one that shows the text.
        for absent in ('text', 'previous_text', 'history'):
            self.assertNotIn(absent, report)

    @unittest.skipIf(sys.platform == 'win32', 'endpoint.py uses the POSIX coordination lock')
    def test_ack_requires_the_flag_and_serves_every_accepted_actor(self):
        import endpoint  # noqa: F401
        with self.assertRaisesRegex(ValueError, 'Name the guidance version'):
            self.call('guidance', ['ack'])
        # kittrial-5bb.99 review `small` 3: a bare positional version is refused
        # though it used to be accepted.
        with self.assertRaisesRegex(ValueError, 'Use guidance ack --version VERSION'):
            self.call('guidance', ['ack', self.version])
        # A stale version is refused without naming the current one.
        with self.assertRaisesRegex(ValueError, 'not the current version') as stale:
            self.call('guidance', ['ack', '--version', 'a' * 64])
        self.assertNotIn(self.version, str(stale.exception))
        # kittrial-5bb.99 review `registration-gate-locks-out-unregistered-lanes`:
        # any actor the endpoint accepts may ack for itself; no session record needed.
        acked = json.loads(self.call('guidance', ['ack', '--version', self.version],
                                     actor='made-up-lane')['stdout'])
        self.assertTrue(acked['acknowledged'])
        self.assertFalse(json.loads(self.call('guidance', ['version'],
                                              actor='made-up-lane')['stdout'])['attention'])

    @unittest.skipIf(sys.platform == 'win32', 'endpoint.py uses the POSIX coordination lock')
    def test_every_write_shaped_endpoint_subcommand_is_refused_and_files_unchanged(self):
        import endpoint  # noqa: F401
        for args in [['set'], ['set-guidance', '--text', 'x'], ['write'], ['edit'], ['update'], ['put'],
                     ['clear'], ['clear-guidance'], ['compact'], ['compact-guidance-acks'],
                     ['remove'], ['delete'], ['reset'], ['install'], ['unset'], ['append'],
                     ['frobnicate'], ['get-text'], ['']]:
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, 'Unknown guidance action'):
                self.call('guidance', args)
            self.assert_files_unchanged()

    @unittest.skipIf(sys.platform == 'win32', 'endpoint.py uses the POSIX coordination lock')
    def test_accepted_guidance_subcommands_are_exactly_the_read_and_ack_set(self):
        # kittrial-5bb.99 review `tests`: a write route under a name the refusal test
        # does not list ('put') passed all tests, so this test (1) reads every string
        # literal the endpoint compares against `subcommand` out of the AST and fails
        # on any that is not in the known read+ack set, and (2) probes the live
        # endpoint with write-shaped candidates so a route installed through a name
        # not written as a literal is caught by behaviour too.
        import ast
        import endpoint
        known = {'get', 'version', 'ack', 'status'}
        tree = ast.parse(Path(endpoint.__file__).read_text(encoding='utf-8'))
        accepted = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == 'subcommand':
                for comparator in node.comparators:
                    values = comparator.elts if isinstance(comparator, (ast.Tuple, ast.List, ast.Set)) else [comparator]
                    for value in values:
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            accepted.add(value.value)
        self.assertLessEqual(accepted, known,
                             'the endpoint compares `subcommand` with names outside the read+ack set: %s'
                             % sorted(accepted - known))
        for candidate in sorted({'set', 'put', 'write', 'edit', 'update', 'clear', 'remove', 'delete', 'reset',
                                 'append', 'install', 'unset', 'drop', 'revoke', 'compact', 'purge', 'replace',
                                 'create', 'add', 'get-text', 'frobnicate', '', 'GET', 'Ack'} - known):
            with self.subTest(candidate=candidate):
                try:
                    self.call('guidance', [candidate])
                except ValueError as error:
                    self.assertIn('Unknown guidance action', str(error))
                else:
                    self.fail('guidance subcommand %r is accepted by the endpoint but is not in the read+ack set'
                              % candidate)
                self.assert_files_unchanged()

    @unittest.skipIf(sys.platform == 'win32', 'endpoint.py uses the POSIX coordination lock')
    def test_write_shaped_guidance_actions_do_not_exist(self):
        # A contributor guidance write route added as a new endpoint action (the
        # review's example) must fail this test rather than pass unnoticed.
        import endpoint  # noqa: F401
        for action in ['guidance-set', 'guidance-write', 'guidance-clear', 'guidance-update']:
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'Unknown action'):
                self.call(action, ['--text', 'x'])
            self.assert_files_unchanged()

    @unittest.skipIf(sys.platform == 'win32', 'endpoint.py uses the POSIX coordination lock')
    def test_version_and_status_refuse_extra_arguments(self):
        import endpoint  # noqa: F401
        for args in [['version', 'junk'], ['version', '--since', 'x' * 64], ['status', 'extra']]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.call('guidance', args)
        with self.assertRaisesRegex(ValueError, 'Unknown guidance action'):
            self.call('guidance', ['set', '--text', 'x'])

    @unittest.skipIf(sys.platform == 'win32', 'endpoint.py uses the POSIX coordination lock')
    def test_bad_guidance_arguments_are_refused(self):
        import endpoint  # noqa: F401
        for args in [['ack', '--version'], ['ack', '--version', 'a' * 64, 'extra'], ['ack', self.version],
                     ['--since'], ['--since', 'nope'],
                     ['get', '--since', 'a' * 64, '--since', 'b' * 64]]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.call('guidance', args)

    @unittest.skipIf(sys.platform == 'win32', 'endpoint.py uses the POSIX coordination lock')
    def test_ack_holds_the_coordination_lock(self):
        import endpoint
        with patch.object(endpoint.fcntl, 'flock') as flock:
            self.call('guidance', ['ack', '--version', self.version])
        self.assertTrue(flock.called)
        self.assertEqual(flock.call_args.args[1], endpoint.fcntl.LOCK_EX)

    @unittest.skipIf(sys.platform == 'win32', 'endpoint.py uses the POSIX coordination lock')
    def test_unreadable_and_nested_guidance_stay_readable_results(self):
        import endpoint  # noqa: F401
        (self.project / '.guidance.json').write_text('[' * 4000 + ']' * 4000, encoding='utf-8')
        answer = json.loads(self.call('guidance', [])['stdout'])
        self.assertFalse(answer['present']); self.assertIn('warning', answer)
        # An unreadable record makes the text unbound, so the endpoint withholds it
        # (kittrial-5bb.99 review `unbound-text-is-still-delivered`).
        self.assertIsNone(answer['text']); self.assertTrue(answer['unbound'])
        self.assertTrue(answer['attention'])
        self.assertIn('repaired by the operator', answer['next_action'])
        (self.project / 'GUIDANCE.md').write_text('x' * 9000, encoding='utf-8')
        answer = json.loads(self.call('guidance', [])['stdout'])
        self.assertIsNone(answer['present']); self.assertTrue(answer['unreadable'])
        self.assertTrue(answer['attention']); self.assertIn('warning', answer)
        self.assertIsNone(answer['text'])

    @unittest.skipIf(sys.platform == 'win32', 'endpoint.py uses the POSIX coordination lock')
    def test_client_routes_guidance_as_an_endpoint_action(self):
        import client
        config = self.root / 'config.json'; config.write_text('{}', encoding='utf-8')
        with patch.object(sys, 'argv', ['client.py', '--config', str(config), '--project', 'example',
                                        '--actor', 'worker-1', '--', 'guidance', 'get']), \
                patch.object(client, 'request', return_value={'stdout': '', 'stderr': '', 'returncode': 0}) as request:
            self.assertEqual(client.main(), 0)
            self.assertEqual(request.call_args.args[3:5], (['get'], 'guidance'))


if __name__ == '__main__':
    unittest.main()
