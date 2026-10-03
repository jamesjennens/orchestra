"""`admin.py anchor-release --duplicate` (kittrial-5bb.91).

Since kittrial-5bb.83 every write on a key with more than one anchor is refused. A
duplicate that holds a WELL-FORMED record cannot be voided (a void never withdraws a
record the entry reads), so this mode releases a NAMED anchor of a duplicated key. It
refuses the anchor with acceptance evidence unless `--set-aside-evidence`, never leaves
the key without an anchor or without its only accepted record, and writes an audit
comment naming everything it set aside.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import admin
import capability_records as cr
import reference_records as rr
from test_catalog_followups import anchor
from test_reference_records import OPERATOR, RefNative, acceptance, entry as reference_payload

OPS = [OPERATOR]
KEY = 'sample.fact'
WRITES = ('update', 'close', 'comments', 'create')


def evidence_by(module, row, operator, author=None):
    """Replace the row's acceptance evidence with one recorded by `operator`."""
    record = module.parse_entry(row['comments'][0]['text'])
    bound = dict(acceptance(), record_sha256=record['sha256'])
    _, body = module.KIND.acceptance_evidence(bound, row['id'], 1, record, operator, at='2026-10-01T12:00:00Z')
    row['comments'] = row['comments'][:1] + [{'id': row['id'] + '-accept', 'text': body,
                                              'author': author or operator}]
    return row


class ReleaseCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)

    def native(self, *rows):
        native = RefNative()
        native.actor = OPERATOR
        native.rows = [dict(row, title='Seeded', description='', issue_type='task') for row in rows]
        return native

    def release(self, module, native, task, actor=OPERATOR, operators=OPS, **options):
        native.actor = actor
        return module.KIND.release(self.project, task, actor, 'forged by a stray client', native,
                                   operators=operators, duplicate=True, **options)

    def writes(self, native):
        return [call for call in native.calls if call[0] in WRITES and call[:2] != ['comments', 'list']]

    def rows(self, native):
        return native.rows


class DuplicateReleaseTests(ReleaseCase):
    def test_a_named_duplicate_with_a_well_formed_record_is_released(self):
        for module in (cr, rr):
            with self.subTest(kind=module.TYPE_LABEL):
                native = self.native(anchor(module, 'z-real'), anchor(module, 'a-forged', state='draft'))
                with self.assertRaisesRegex(ValueError, 'duplicate anchors'):
                    module.KIND.require_unique_key(native.rows, KEY)
                # The plain mode still refuses an anchor that holds a record, and says where to go.
                with self.assertRaisesRegex(ValueError, 'still holds a record.*released with --duplicate'):
                    module.KIND.release(self.project, 'a-forged', OPERATOR, 'r', native, operators=OPS)
                # A void cannot repair it either: the record is well formed and the entry reads it.
                self.assertIn('never withdraws one', module.KIND.void_refusal(
                    native.row('a-forged'), {'target_kind': module.KIND.family + 'entry', 'target': 'a-forged-r1'}))
                self.assertEqual(self.writes(native), [])

                result = self.release(module, native, 'a-forged')
                self.assertTrue(self.writes(native))
                forged = native.row('a-forged')
                record = module.parse_entry(forged['comments'][0]['text'])
                self.assertEqual((result['released'], result['duplicate'], result['key'], result['remaining'],
                                  result['selected_before'], result['selected_after'], result['records'],
                                  result['evidence_set_aside']),
                                 ('a-forged', True, KEY, ['z-real'], 'z-real', 'z-real',
                                  [{'revision': 1, 'sha256': record['sha256']}], []))
                # Nothing is deleted: the type label and the records stay, the key label is gone.
                self.assertEqual(forged['labels'], [module.TYPE_LABEL])
                self.assertEqual(forged['status'], 'closed')
                note = forged['comments'][-1]
                self.assertEqual(note['author'], OPERATOR)
                for text in ('Released by operator %s: this %s anchor was a duplicate of key %s' % (
                        OPERATOR, module.KIND.noun, KEY), 'Remaining anchor(s): z-real',
                        'revision 1 sha256 ' + record['sha256'], 'Acceptance evidence set aside: none',
                        'Reason: forged by a stray client'):
                    self.assertIn(text, note['text'])
                # The key is whole again: writes are accepted, and the released row is not read for any key.
                self.assertEqual([row['id'] for row in module.KIND.require_unique_key(native.rows, KEY)], ['z-real'])
                view = module.get(native.rows, KEY, OPS)
                self.assertEqual((view['native_id'], view['state'], view['warnings']), ('z-real', 'accepted', []))
                listed = module.list_entries(native.rows, {}, OPS)
                self.assertEqual((listed['total'], [item['native_id'] for item in listed['items']]), (1, ['z-real']))
                self.assertNotIn('a-forged', listed['coverage'])
                self.assertNotIn('malformed', listed['coverage'])
                # A second release of the same row is refused: it is no longer an anchor.
                with self.assertRaisesRegex(ValueError, 'already released'):
                    self.release(module, native, 'a-forged')

    def test_every_refusal_comes_before_the_first_write(self):
        for module in (cr, rr):
            real, forged = anchor(module, 'z-real'), anchor(module, 'a-forged', state='draft')
            inert = evidence_by(module, anchor(module, 'b-inert'), 'former-operator')
            newer = anchor(module, 'c-newer', state='draft')
            newer['comments'].append({'id': 'v2', 'text': 'Kind: %sentry-v2\n{}' % module.KIND.family,
                                      'author': 'alice'})
            two_keys = anchor(module, 'd-two', state='draft')
            two_keys['labels'].append(module.key_label('other.fact'))
            alone = anchor(module, 'e-alone', key='lonely.fact', state='draft')
            released = dict(anchor(module, 'f-released', state='draft'), labels=[module.TYPE_LABEL])
            cases = (
                ('a-forged', dict(actor='mallory'), 'not a server-side configured operator'),
                ('a-forged', dict(operators=[]), 'No operator allowlist is configured'),
                ('e-alone', {}, 'is not duplicated: e-alone is its only anchor'),
                ('d-two', {}, 'carries more than one %s label' % module.KIND.key_prefix),
                ('f-released', {}, 'already released'),
                ('nope', {}, 'Unknown %s anchor nope' % module.KIND.noun),
                ('c-newer', {}, 'holds a record this kit cannot read'),
                ('z-real', {}, r'carries acceptance evidence \(revision 1 by %s, live\); it is the anchor readers '
                               'select' % OPERATOR),
                ('b-inert', {}, r'carries acceptance evidence \(revision 1 by former-operator, inert\)\. It is '
                                'released only with --set-aside-evidence'),
                ('z-real', dict(set_aside_evidence=True), 'holds the only live acceptance evidence of sample.fact'),
            )
            for task, options, message in cases:
                with self.subTest(kind=module.TYPE_LABEL, task=task, options=sorted(options)):
                    native = self.native(real, forged, inert, newer, two_keys, alone, released)
                    native.calls = []
                    with self.assertRaisesRegex(ValueError, message):
                        self.release(module, native, task, **options)
                    self.assertEqual(self.writes(native), [])
                    if 'actor' in options or 'operators' in options:
                        self.assertEqual(native.calls, [])               # before any native read
            native = self.native(real, forged)
            with self.assertRaisesRegex(ValueError, '--set-aside-evidence applies only with --duplicate'):
                module.KIND.release(self.project, 'a-forged', OPERATOR, 'r', native, operators=OPS,
                                    set_aside_evidence=True)
            with self.assertRaisesRegex(ValueError, 'A release reason is required'):
                module.KIND.release(self.project, 'a-forged', OPERATOR, ' ', native, operators=OPS, duplicate=True)
            self.assertEqual(self.writes(native), [])

    def test_two_anchors_with_live_evidence_need_the_explicit_flag(self):
        for module in (cr, rr):
            with self.subTest(kind=module.TYPE_LABEL):
                native = self.native(anchor(module, 'one'), anchor(module, 'two'))
                self.assertEqual(module.get(native.rows, KEY, OPS)['state'], 'conflicted')
                # The coordinator's correction: voiding the evidence is NOT a way out. A void of a
                # well-formed acceptance record that is the only holder of its place is refused.
                self.assertIn('never withdraws one', module.KIND.void_refusal(
                    native.row('two'), {'target_kind': module.KIND.family + 'acceptance', 'target': 'two-accept'}))
                with self.assertRaisesRegex(ValueError, 'released only with --set-aside-evidence'):
                    self.release(module, native, 'two')
                self.assertEqual(self.writes(native), [])
                result = self.release(module, native, 'two', set_aside_evidence=True)
                record = module.parse_entry(native.row('two')['comments'][0]['text'])
                self.assertEqual((result['selected_before'], result['selected_after'], result['remaining']),
                                 (None, 'one', ['one']))
                self.assertEqual(result['evidence_set_aside'], [
                    {'revision': 1, 'record_sha256': record['sha256'], 'operator': OPERATOR,
                     'decision_id': 'decision-42', 'at': '2026-10-01T12:00:00Z', 'live': True}])
                note = native.row('two')['comments'][-1]['text']
                self.assertIn('Readers selected no anchor (conflicted) before and select one now', note)
                self.assertIn('revision 1 record %s by %s, decision decision-42, live' % (record['sha256'], OPERATOR),
                              note)
                view = module.get(native.rows, KEY, OPS)
                self.assertEqual((view['native_id'], view['state']), ('one', 'accepted'))

    def test_inert_evidence_set_aside_stays_set_aside_when_its_operator_returns(self):
        # Q1 of the plan: a released row is no longer an anchor and is never read for the key.
        for module in (cr, rr):
            with self.subTest(kind=module.TYPE_LABEL):
                native = self.native(anchor(module, 'z-real'),
                                     evidence_by(module, anchor(module, 'a-former'), 'former-operator'))
                both = OPS + ['former-operator']
                self.assertEqual(module.get(native.rows, KEY, both)['state'], 'conflicted')   # were it still listed
                self.assertEqual(module.get(native.rows, KEY, OPS)['native_id'], 'z-real')
                result = self.release(module, native, 'a-former', set_aside_evidence=True)
                self.assertEqual([(item['operator'], item['live']) for item in result['evidence_set_aside']],
                                 [('former-operator', False)])
                self.assertIn('by former-operator, decision decision-42, inert',
                              native.row('a-former')['comments'][-1]['text'])
                for operators in (OPS, both):                             # the operator is re-added
                    view = module.get(native.rows, KEY, operators)
                    self.assertEqual((view['native_id'], view['state'], view['warnings']),
                                     ('z-real', 'accepted', []))
                    self.assertEqual(module.list_entries(native.rows, {}, operators)['total'], 1)
                    module.KIND.require_unique_key(native.rows, KEY)

    def test_a_malformed_or_record_less_duplicate_beside_others_and_three_anchors(self):
        for module in (cr, rr):
            with self.subTest(kind=module.TYPE_LABEL):
                broken = {'id': 'b-broken', 'labels': [module.TYPE_LABEL, module.key_label(KEY)], 'status': 'open',
                          'comments': [{'id': 'bad', 'text': module.ENTRY_PREFIX + '{broken', 'author': 'alice'}]}
                native = self.native(anchor(module, 'z-real'), anchor(module, 'a-forged', state='draft'), broken)
                first = self.release(module, native, 'b-broken')
                self.assertEqual((first['remaining'], first['records'], first['closed']),
                                 (['a-forged', 'z-real'], [], True))
                with self.assertRaisesRegex(ValueError, 'duplicate anchors'):
                    module.KIND.require_unique_key(native.rows, KEY)          # still two
                second = self.release(module, native, 'a-forged')
                self.assertEqual(second['remaining'], ['z-real'])
                module.KIND.require_unique_key(native.rows, KEY)
                # Two drafts and nothing accepted: either may go, never the last.
                native = self.native(anchor(module, 'one', state='draft'), anchor(module, 'two', state='draft'))
                self.release(module, native, 'one')
                with self.assertRaisesRegex(ValueError, 'is not duplicated: two is its only anchor'):
                    self.release(module, native, 'two')

    def test_a_draft_beside_an_orphan_selects_nothing_until_the_orphan_is_released(self):
        # Among duplicates only live acceptance evidence selects an anchor, so the anchor readers
        # select always carries live evidence and is always behind --set-aside-evidence.
        for module in (cr, rr):
            with self.subTest(kind=module.TYPE_LABEL):
                orphan = {'id': 'b-orphan', 'labels': [module.TYPE_LABEL, module.key_label(KEY)],
                          'status': 'closed', 'comments': []}
                native = self.native(anchor(module, 'a-draft', state='draft'), orphan)
                self.assertEqual(module.get(native.rows, KEY, OPS)['state'], 'conflicted')
                result = self.release(module, native, 'b-orphan')
                self.assertEqual((result['selected_before'], result['selected_after'], result['records'],
                                  result['remaining']), (None, 'a-draft', [], ['a-draft']))
                view = module.get(native.rows, KEY, OPS)
                self.assertEqual((view['native_id'], view['state'], view['warnings']), ('a-draft', 'draft-only', []))

    def test_a_shared_lookup_slug_is_not_a_duplicate(self):
        # `a.b.c` and `a.b-c` share one lookup label; they are two keys, not one duplicated key.
        native = self.native(anchor(rr, 'one', key='sample.fact.x', state='draft'),
                             anchor(rr, 'two', key='sample.fact-x', state='draft'))
        self.assertEqual(rr.key_label('sample.fact.x'), rr.key_label('sample.fact-x'))
        with self.assertRaisesRegex(ValueError, 'is not duplicated'):
            self.release(rr, native, 'one')
        self.assertEqual(self.writes(native), [])

    def test_an_interrupted_release_is_finished_by_a_re_run(self):
        native = self.native(anchor(rr, 'z-real'), anchor(rr, 'a-forged', state='draft'))
        real_call = RefNative.__call__
        failed = []

        def flaky(this, args):
            if args[:1] == ['update'] and '--remove-label' in args and not failed:
                failed.append(args)
                raise RuntimeError('connection lost')
            return real_call(this, args)
        with patch.object(RefNative, '__call__', flaky), self.assertRaises(RuntimeError):
            self.release(rr, native, 'a-forged')
        self.assertIn(rr.key_label(KEY), native.row('a-forged')['labels'])    # still an anchor of the key
        done = self.release(rr, native, 'a-forged')
        self.assertEqual(done['remaining'], ['z-real'])
        notes = [c for c in native.row('a-forged')['comments'] if c['text'].startswith('Released by operator')]
        self.assertEqual(len(notes), 1)                                       # the audit comment is not repeated
        self.assertEqual(native.row('a-forged')['labels'], [rr.TYPE_LABEL])


class CapabilityFollowUpTests(ReleaseCase):
    def test_aliases_and_verifications_on_the_released_anchor_are_counted(self):
        forged = anchor(cr, 'a-forged', state='draft')
        made = cr.alias_record(KEY, 'the fact', 'propose', 'alice', False)
        body = made[1] if isinstance(made, tuple) else made
        forged['comments'].append({'id': 'alias-1', 'text': body, 'author': 'alice'})
        native = self.native(anchor(cr, 'z-real'), forged)
        result = self.release(cr, native, 'a-forged')
        self.assertEqual(result['stop_counting'], {'capability-alias': 1})
        self.assertIn('Other records that stop counting for the key: 1 capability-alias',
                      native.row('a-forged')['comments'][-1]['text'])

    def test_retire_refuses_a_duplicated_successor(self):
        native = self.native(anchor(cr, 'old', key='sample.old'), anchor(cr, 'next-a', key='sample.next'),
                             anchor(cr, 'next-b', key='sample.next', state='draft'))
        record = cr.parse_entry(native.row('old')['comments'][0]['text'])
        payload = dict(schema_version=1, operation_id='retire-1', operation='retire', key='sample.old', revision=1,
                       record_sha256=record['sha256'], acceptance=acceptance(), acceptance_state='superseded',
                       successor='sample.next')
        native.calls = []
        with self.assertRaisesRegex(ValueError, 'key sample.next has duplicate anchors'):
            cr.apply_native(payload, OPERATOR, native, self.project, operator=True, operators=OPS)
        self.assertEqual(self.writes(native), [])
        # Once the duplicate is released the same retirement is accepted.
        self.release(cr, native, 'next-b')
        done = cr.apply_native(payload, OPERATOR, native, self.project, operator=True, operators=OPS)
        self.assertEqual(cr.get(native.rows, 'sample.old', OPS)['state'], 'superseded', done)

    def test_find_lists_a_conflicted_anchor_once(self):
        rows = [anchor(cr, 'one', state='draft'), anchor(cr, 'two', state='draft')]
        for phrase in (KEY, 'sample fact', 'fact'):
            with self.subTest(phrase=phrase):
                found = cr.find(rows, phrase, OPS)
                listed = [item['native_id'] for item in found['records']] + \
                    [item['native_id'] for item in found['candidates']]
                self.assertEqual(sorted(listed), sorted(set(listed)), found)
        found = cr.find(rows, KEY, OPS)
        self.assertEqual((sorted(item['native_id'] for item in found['records']), found['candidates']),
                         (['one', 'two'], []))


class ReferenceDuplicateWriteTests(ReleaseCase):
    def test_every_reference_write_is_refused_on_a_duplicate_and_works_after_the_release(self):
        # The reference kind has no retire operation: accept, draft, propose and revise are its writes.
        native = self.native(anchor(rr, 'z-real'), anchor(rr, 'a-forged', state='draft'))
        record = rr.parse_entry(native.row('z-real')['comments'][0]['text'])
        accept = dict(schema_version=1, operation_id='ref-accept', operation='accept', key=KEY, revision=1,
                      record_sha256=record['sha256'], acceptance=acceptance(), acceptance_state='accepted')
        revise = dict(reference_payload(key=KEY, operation='revise'), revision=2, expected_sha256=record['sha256'])
        writes = ((accept, OPERATOR, True),
                  (dict(reference_payload(key=KEY, operation='draft'), acceptance=acceptance(),
                        acceptance_state='accepted'), OPERATOR, True),
                  (reference_payload(key=KEY, operation='propose'), 'alice', False), (revise, 'alice', False))
        for payload, actor, operator in writes:
            native.calls = []
            native.actor = actor
            with self.subTest(operation=payload['operation']), self.assertRaisesRegex(ValueError, 'duplicate anchors'):
                rr.apply_native(payload, actor, native, self.project, operator=operator, operators=OPS)
            self.assertEqual(self.writes(native), [])
        self.release(rr, native, 'a-forged')
        native.actor = 'alice'
        made = rr.apply_native(revise, 'alice', native, self.project, operators=OPS)
        self.assertEqual((made['revision'], made['native_id']), (2, 'z-real'), made)


class AdminCommandTests(ReleaseCase):
    def setUp(self):
        super().setUp()
        self.root = self.project / 'runtime'
        trial = self.root / 'projects' / 'trial'
        (trial / '.beads').mkdir(parents=True)
        (trial / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'unit': 'none', 'operators': [OPERATOR]}), encoding='utf-8')
        self.flock = Mock()
        for patcher in (patch.dict(os.environ, {'ORCHESTRA_OPERATORS': ''}),
                        patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=self.flock, LOCK_EX=2)})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = self.native(anchor(cr, 'one'), anchor(cr, 'two'))

    def run_native(self, root, name, args):
        if args[:1] == ['--actor']:
            self.store.actor = args[1]
            args = args[2:]
        return self.store(list(args))

    def admin(self, *argv):
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'anchor-release', 'trial', '--kind',
                                        'capability', '--reason', 'forged', *argv]), \
                patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'run_bd', side_effect=self.run_native), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        return json.loads(out.getvalue())

    def test_the_flags_reach_the_release_under_the_project_lock(self):
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.admin('--issue-id', 'two', '--actor', 'mallory', '--duplicate', '--set-aside-evidence')
        with self.assertRaisesRegex(ValueError, 'still holds a record'):
            self.admin('--issue-id', 'two', '--actor', OPERATOR)
        with self.assertRaisesRegex(ValueError, 'released only with --set-aside-evidence'):
            self.admin('--issue-id', 'two', '--actor', OPERATOR, '--duplicate')
        self.flock.reset_mock()
        result = self.admin('--issue-id', 'two', '--actor', OPERATOR, '--duplicate', '--set-aside-evidence')
        self.assertEqual((result['released'], result['selected_after'], self.flock.call_count), ('two', 'one', 1))
        self.assertEqual(self.store.row('two')['comments'][-1]['author'], OPERATOR)


if __name__ == '__main__':
    unittest.main()
