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
import recovery
import reference_records as rr
from keyed_entries import content_hash
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


def orphan(module, task):
    return {'id': task, 'labels': [module.TYPE_LABEL, module.key_label(KEY)], 'status': 'closed', 'comments': []}


def malformed(module, task):
    return dict(orphan(module, task), comments=[{'id': task + '-bad', 'text': module.ENTRY_PREFIX + '{broken',
                                                 'author': 'alice'}])


def voided_only(module, task):
    """An anchor whose only record is malformed and named by an applied operator void."""
    native = RefNative()
    native.actor = OPERATOR
    native.rows = [dict(malformed(module, task), title='Seeded', description='', issue_type='task')]
    bad = native.rows[0]['comments'][0]
    module.KIND.apply_void({'schema_version': 1, 'operation': 'void-record', 'operation_id': 'void-' + task,
                            'task': task, 'target': bad['id'], 'target_kind': module.KIND.family + 'entry',
                            'target_sha256': recovery.digest(bad['text']), 'original': bad['text'],
                            'reason': 'Malformed record; repaired by the operator', 'disposition': 'void',
                            'operator': OPERATOR}, OPERATOR, native, OPS)
    row = native.row(task)
    assert not module.KIND.has_live_record(row, OPS), 'the fixture void must apply'
    return row


def accept_died(module, task):
    """A draft whose operator accept wrote its evidence and died before the accepted revision."""
    row = anchor(module, task, state='draft')
    draft = module.parse_entry(row['comments'][0]['text'])
    accepted = {name: value for name, value in draft.items() if name != 'sha256'}
    accepted.update(revision=2, acceptance_state='accepted')
    accepted['sha256'] = content_hash(accepted)
    _, body = module.KIND.acceptance_evidence(dict(acceptance(), record_sha256=accepted['sha256']), task, 2,
                                              accepted, OPERATOR, at='2026-10-01T12:00:00Z')
    row['comments'].append({'id': task + '-accept', 'text': body, 'author': OPERATOR})
    return row


class ReadableRecordRuleTests(ReleaseCase):
    """Review of febaad7: a release never leaves the key with no readable record, and
    never takes its only accepted record away."""

    def test_a_readable_anchor_is_not_released_beside_anchors_with_nothing_readable(self):
        for module in (cr, rr):
            for other in (orphan, malformed, voided_only):
                for label, named, flags in (
                        ('draft', anchor(module, 'a-named', state='draft'), {}),
                        ('draft with the flag', anchor(module, 'a-named', state='draft'), {'set_aside_evidence': True}),
                        ('inert accepted', evidence_by(module, anchor(module, 'a-named'), 'former-operator'),
                         {'set_aside_evidence': True}),
                        ('live accepted', anchor(module, 'a-named'), {'set_aside_evidence': True})):
                    with self.subTest(kind=module.TYPE_LABEL, other=other.__name__, named=label):
                        native = self.native(named, other(module, 'b-other'))
                        self.assertEqual(module.KIND.readable_revisions(native.row('b-other'), OPS), [])
                        native.calls = []
                        with self.assertRaisesRegex(ValueError, r'No remaining anchor of .* sample.fact holds a '
                                                                r'readable record \(b-other\), so releasing a-named '
                                                                'would leave the key with nothing readable'):
                            self.release(module, native, 'a-named', **flags)
                        self.assertEqual(self.writes(native), [])
                        self.assertIn(module.key_label(KEY), native.row('a-named')['labels'])
                # The other direction is the repair: the anchor with nothing readable goes.
                with self.subTest(kind=module.TYPE_LABEL, other=other.__name__, named='the other one'):
                    native = self.native(anchor(module, 'a-named', state='draft'), other(module, 'b-other'))
                    done = self.release(module, native, 'b-other')
                    self.assertEqual((done['remaining'], done['records']), (['a-named'], []))
                    with self.assertRaisesRegex(ValueError, 'is not duplicated: a-named is its only anchor'):
                        self.release(module, native, 'a-named')            # so the key keeps an anchor
            # Two anchors with nothing readable: either may go (nothing readable is lost).
            native = self.native(orphan(module, 'a-one'), malformed(module, 'b-two'))
            self.assertEqual(self.release(module, native, 'b-two')['remaining'], ['a-one'])

    def test_readable_is_what_the_reader_presents_not_what_the_ledger_parses(self):
        # Review of b1b2b10: forged anchor B holds a well-formed draft and one more comment
        # that makes the READER refuse the whole entry. Its revision ledger still parses, so
        # releasing the genuine draft A left the key malformed or unsupported.
        for module in (cr, rr):
            family = module.KIND.family
            extras = (('a CRLF lookalike', module.ENTRY_PREFIX.replace('\n', '\r\n') + '{}', 'malformed'),
                      ('a malformed acceptance', module.KIND.acceptance_prefix + '{broken', 'malformed'),
                      ('a -v2 record', 'Kind: %sentry-v2\n{}' % family, 'unsupported'),
                      ('an unknown kind of the family', 'Kind: %sfuture-v1\n{}' % family, 'unsupported'))
            for label, text, state in extras:
                with self.subTest(kind=module.TYPE_LABEL, extra=label):
                    forged = anchor(module, 'b-forged', state='draft')
                    forged['comments'].append({'id': 'b-extra', 'text': text, 'author': 'mallory'})
                    native = self.native(anchor(module, 'a-genuine', state='draft'), forged)
                    row = native.row('b-forged')
                    self.assertEqual((module.KIND.entry_view(row, OPS)['state'],
                                      bool(module.KIND.readable_revisions(row, OPS)),
                                      module.KIND.presents_record(row, OPS)), (state, True, False))
                    native.calls = []
                    for flags in ({}, {'set_aside_evidence': True}):
                        with self.assertRaisesRegex(ValueError, r'No remaining anchor of .* holds a readable record '
                                                                r'\(b-forged\), so releasing a-genuine would leave '
                                                                'the key with nothing readable'):
                            self.release(module, native, 'a-genuine', **flags)
                    self.assertEqual(self.writes(native), [])
                    if state == 'malformed':
                        # The forged one is the one to release; the genuine draft then reads alone.
                        self.assertEqual(self.release(module, native, 'b-forged')['remaining'], ['a-genuine'])
                        view = module.get(native.rows, KEY, OPS)
                        self.assertEqual((view['native_id'], view['warnings']), ('a-genuine', []))
                    else:
                        with self.assertRaisesRegex(ValueError, 'holds a record this kit cannot read'):
                            self.release(module, native, 'b-forged')            # the host procedure, or a newer kit
            self.assertTrue(module.KIND.presents_record(anchor(module, 'x', state='draft'), OPS))
            self.assertTrue(module.KIND.presents_record(anchor(module, 'x'), OPS))
            for other in (orphan, malformed, voided_only):
                self.assertFalse(module.KIND.presents_record(other(module, 'x'), OPS), other.__name__)

    def test_an_accepted_anchor_that_reads_malformed_is_not_set_aside(self):
        # Review of b1b2b10: the genuine accepted anchor carries one unvoided malformed
        # comment, so it reads no accepted record and neither rule protected it.
        for module in (cr, rr):
            with self.subTest(kind=module.TYPE_LABEL):
                genuine = anchor(module, 'a-genuine')
                genuine['comments'].append({'id': 'a-bad', 'text': module.KIND.acceptance_prefix + '{broken',
                                            'author': 'mallory'})
                native = self.native(genuine, anchor(module, 'b-forged', state='draft'))
                self.assertEqual(module.KIND.entry_view(native.row('a-genuine'), OPS)['state'], 'malformed')
                native.calls = []
                for flags in ({}, {'set_aside_evidence': True}):
                    with self.assertRaisesRegex(ValueError, 'a-genuine reads malformed and carries live acceptance '
                                                            'evidence.*Void the malformed comment first'):
                        self.release(module, native, 'a-genuine', **flags)
                self.assertEqual(self.writes(native), [])
                # After the void the same release is refused by the accepted-record rule.
                bad = native.row('a-genuine')['comments'][-1]
                module.KIND.apply_void({
                    'schema_version': 1, 'operation': 'void-record', 'operation_id': 'void-a-bad', 'task': 'a-genuine',
                    'target': bad['id'], 'target_kind': module.KIND.family + 'acceptance',
                    'target_sha256': recovery.digest(bad['text']), 'original': bad['text'],
                    'reason': 'Malformed record; repaired by the operator', 'disposition': 'void',
                    'operator': OPERATOR}, OPERATOR, native, OPS)
                self.assertEqual(module.KIND.entry_view(native.row('a-genuine'), OPS)['state'], 'accepted')
                with self.assertRaisesRegex(ValueError, 'a-genuine holds the only accepted record of sample.fact'):
                    self.release(module, native, 'a-genuine', set_aside_evidence=True)
                done = self.release(module, native, 'b-forged')
                self.assertEqual((done['remaining'], done['selected_after']), (['a-genuine'], 'a-genuine'))
                # A malformed anchor WITHOUT live evidence is still releasable: it is the broken duplicate.
                broken = anchor(module, 'c-broken', state='draft')
                broken['comments'].append({'id': 'c-bad', 'text': module.KIND.acceptance_prefix + '{broken',
                                           'author': 'mallory'})
                native = self.native(anchor(module, 'a-genuine'), broken)
                self.assertEqual(self.release(module, native, 'c-broken')['remaining'], ['a-genuine'])

    def test_live_evidence_on_a_remaining_anchor_is_not_an_accepted_record(self):
        # Anchor B is a draft whose accept died after its evidence; anchor A holds the key's
        # only accepted record. Releasing A took the key from accepted to draft-only.
        for module in (cr, rr):
            with self.subTest(kind=module.TYPE_LABEL):
                native = self.native(anchor(module, 'a-accepted'), accept_died(module, 'b-died'))
                died = native.row('b-died')
                self.assertEqual([(item['revision'], item['live']) for item in module.KIND.acceptance_on(died, OPS)],
                                 [(2, True)])
                self.assertIsNone(module.KIND.entry_view(died, OPS)['record'])
                before = module.get(native.rows, KEY, OPS)
                self.assertEqual((before['native_id'], before['state']), ('a-accepted', 'accepted'))
                native.calls = []
                with self.assertRaisesRegex(ValueError, 'a-accepted holds the only accepted record of sample.fact: '
                                                        'no remaining anchor reads an accepted record'):
                    self.release(module, native, 'a-accepted', set_aside_evidence=True)
                self.assertEqual(self.writes(native), [])
                # The broken one is the one to release; its dangling evidence needs the flag.
                with self.assertRaisesRegex(ValueError, 'released only with --set-aside-evidence'):
                    self.release(module, native, 'b-died')
                done = self.release(module, native, 'b-died', set_aside_evidence=True)
                self.assertEqual((done['selected_after'], [item['revision'] for item in done['evidence_set_aside']]),
                                 ('a-accepted', [2]))
                after = module.get(native.rows, KEY, OPS)
                self.assertEqual((after['native_id'], after['state'], after['warnings']),
                                 ('a-accepted', 'accepted', []))


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
                ('z-real', dict(set_aside_evidence=True), 'holds the only accepted record of sample.fact'),
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

    def test_the_key_is_never_left_with_nothing_readable(self):
        # Among duplicates only live acceptance evidence selects an anchor, so the anchor readers
        # select always carries live evidence and is always behind --set-aside-evidence. A draft
        # beside a record-less anchor selects nothing; releasing the draft would leave the key
        # with nothing readable, so it is refused, with or without the flag.
        for module in (cr, rr):
            with self.subTest(kind=module.TYPE_LABEL):
                orphan = {'id': 'b-orphan', 'labels': [module.TYPE_LABEL, module.key_label(KEY)],
                          'status': 'closed', 'comments': []}
                native = self.native(anchor(module, 'a-draft', state='draft'), orphan)
                self.assertEqual(module.get(native.rows, KEY, OPS)['state'], 'conflicted')
                native.calls = []
                with self.assertRaisesRegex(ValueError, 'would leave the key with nothing readable'):
                    self.release(module, native, 'a-draft')
                self.assertEqual(self.writes(native), [])
                # The safe order: the record-less anchor goes first (either mode releases it).
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
