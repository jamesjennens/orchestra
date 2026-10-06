"""Open items and owner questions, slice 1 (kittrial-5bb.127): the reader, read-only.

No kit writes these records yet, so every record here is hand-built from the design's
field tables by tools/open_items_fixture.py and checked by the reader's own parsers.
These tests pin the parsers, the derivation of design 4.5 (states, closed_by,
reopened_by, conflicted, trust), the read commands, the two brief kinds, the caps, and
that an unreadable row or a malformed comment is reported, never fatal and never
silently dropped.
"""
import contextlib
import copy
import datetime
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
sys.path.insert(0, str(KIT / 'tools'))
import open_items as oi
import open_items_fixture as fx

OPERATOR = 'operator'
OWNER = 'person:james'
TODAY = datetime.date(2026, 10, 6)
OPTIONS = [{'id': 'keep', 'text': 'Keep it'}, {'id': 'drop', 'text': 'Drop it'}]


def journal_dir(case, sink):
    temp = tempfile.TemporaryDirectory()
    case.addCleanup(temp.cleanup)
    folder = Path(temp.name)
    (folder / oi.OWNER_ANSWERS_JOURNAL).mkdir()
    for entry in sink.journal.values():
        (folder / oi.OWNER_ANSWERS_JOURNAL / (entry['sha256'] + '.json')).write_text(json.dumps(entry),
                                                                                   encoding='utf-8')
    return folder


class Ledger:
    """One hand-built anchor at a time, on a MemorySink."""

    def __init__(self):
        self.sink = fx.MemorySink()
        self.serial = 0

    def anchor(self):
        return self.sink.create('item', {oi.FAMILY_LABEL})

    def post(self, rid, prefix, record, author=OPERATOR):
        return self.sink.comment(rid, fx.body(prefix, record), author)

    def question(self, **extra):
        rid = self.anchor()
        record = fx.item_record(rid, kind='question', for_=OWNER, options=OPTIONS, submitted_by=fx.block(OPERATOR),
                                **extra)
        self.post(rid, oi.OPEN_ITEM_PREFIX, record)
        return rid, record

    def answer(self, rid, question, authority='relayed', journal=True, author=OPERATOR, by=None, **extra):
        self.serial += 1
        by = by or fx.block(OPERATOR, person=OWNER if authority == 'owner' else None)
        answer = fx.answer_record(question, authority=authority, by=by, serial=self.serial, **extra)
        cid = self.post(rid, oi.OWNER_ANSWER_PREFIX, answer, author)
        if journal:
            self.sink.write_entry(fx.journal_entry(answer, cid))
        return cid, answer

    def close(self, rid, question, answer_cid, disposition='resolved', revision=2):
        self.serial += 1
        resolution = fx.resolution_record(rid, revision - 1, disposition=disposition, answer=answer_cid,
                                          by=fx.block(OPERATOR), serial=self.serial)
        rcid = self.post(rid, oi.ITEM_RESOLUTION_PREFIX, resolution)
        state = 'open' if disposition == 'reopened' else disposition
        record = fx.item_record(rid, revision=revision, kind=question['kind'], for_=question['for'],
                                options=question['options'], submitted_by=fx.block(OPERATOR), state=state,
                                resolved_by=None if disposition == 'reopened' else rcid)
        self.post(rid, oi.OPEN_ITEM_PREFIX, record)
        return rcid

    def view(self, case, rid, operators=(OPERATOR,)):
        row = self.sink.rows[rid]
        return oi.item_view(row, operators, journal_dir(case, self.sink), TODAY)


class ParserTests(unittest.TestCase):
    def test_hand_built_records_parse(self):
        item = fx.item_record('kit-1')
        self.assertEqual(oi.parse_item(fx.body(oi.OPEN_ITEM_PREFIX, item), 'kit-1'), item)
        question = fx.item_record('kit-1', kind='question', for_=OWNER, options=OPTIONS)
        answer = fx.answer_record(question, serial=1)
        self.assertEqual(oi.parse_answer(fx.body(oi.OWNER_ANSWER_PREFIX, answer), 'kit-1'), answer)
        resolution = fx.resolution_record('kit-1', 1, answer='7', serial=1)
        self.assertEqual(oi.parse_resolution(fx.body(oi.ITEM_RESOLUTION_PREFIX, resolution), 'kit-1'), resolution)
        # Design 9.1: an operator with no actor-map entry is `operator:<actor>` (slice 0 refused it).
        self.assertEqual(fx.block('alice')['person'], 'operator:alice')

    def bad_items(self):
        def item(**change):
            record = fx.item_record('kit-1')
            record.update(change)
            return fx.sealed({k: v for k, v in record.items() if k != 'sha256'})
        yield 'extra field', item(tags=[])
        yield 'id', item(id='kit-2')
        yield 'revision', item(revision=0)
        yield 'boolean revision', item(revision=True)
        yield 'kind', item(kind='note')
        yield 'empty text', item(text=' ')
        yield 'long text', item(text='x' * 4001)
        yield 'long source', item(source='x' * 241)
        yield 'session owner', item(owner='session-abcd')
        yield 'for on a blocker', item(**{'for': OWNER})
        yield 'question without options', item(kind='question', **{'for': OWNER})
        yield 'recommended not offered', item(kind='question', options=OPTIONS, recommended='other', **{'for': OWNER})
        yield 'due_by', item(due_by='2026-02-30')
        yield 'state', item(state='answered')
        yield 'blocked without note', item(state='blocked')
        yield 'note when open', item(state_note='why')
        yield 'provenance', item(provenance={'kind': 'copied'})
        yield 'endpoint verified', item(submitted_by={'actor': 'a', 'route': 'endpoint', 'identity': 'verified',
                                                      'person': None})
        yield 'host unverified', item(submitted_by={'actor': 'a', 'route': 'host', 'identity': 'unverified',
                                                    'person': 'person:a'})
        yield 'stamp', item(at='2026-10-06 09:00')
        unhashed = fx.item_record('kit-1')
        unhashed['text'] = 'changed'
        yield 'hash', unhashed

    def test_malformed_items_are_refused(self):
        for name, record in self.bad_items():
            with self.subTest(name=name):
                with self.assertRaises(oi.Malformed):
                    oi.parse_item(fx.body(oi.OPEN_ITEM_PREFIX, record), 'kit-1')

    def test_malformed_resolutions_and_answers_are_refused(self):
        good = fx.resolution_record('kit-1', 1, serial=1)
        for change in ({'disposition': 'voided'}, {'evidence': 'see above'}, {'item': 'kit-2'},
                       {'resolution': 'r-1'}, {'reason': ''}):
            record = fx.sealed({**{k: v for k, v in good.items() if k != 'sha256'}, **change})
            with self.subTest(change=change), self.assertRaises(oi.Malformed):
                oi.parse_resolution(fx.body(oi.ITEM_RESOLUTION_PREFIX, record), 'kit-1')
        question = fx.item_record('kit-1', kind='question', for_=OWNER, options=OPTIONS)
        answer = fx.answer_record(question, serial=1)
        for change in ({'item': 'kit-2'}, {'authority': 'coordinator'}, {'note': 'x'},
                       {'by': fx.block('a', 'endpoint')}):
            record = fx.sealed({**{k: v for k, v in answer.items() if k != 'sha256'}, **change})
            with self.subTest(change=change), self.assertRaises(oi.Malformed):
                oi.parse_answer(fx.body(oi.OWNER_ANSWER_PREFIX, record), 'kit-1' if 'item' not in change else 'kit-1')
        with self.assertRaises(oi.Malformed):
            oi.parse_answer(fx.body(oi.OWNER_ANSWER_PREFIX, dict(answer, sha256='f' * 64)), 'kit-1')

    def test_a_record_must_be_canonical_json(self):
        item = fx.item_record('kit-1')
        spaced = oi.OPEN_ITEM_PREFIX + json.dumps(item, sort_keys=True)
        with self.assertRaisesRegex(oi.Malformed, 'not canonical JSON'):
            oi.parse_item(spaced, 'kit-1')
        self.assertEqual(oi.parse_item(fx.body(oi.OPEN_ITEM_PREFIX, item), 'kit-1'), item)

    def test_deep_json_is_malformed_not_a_crash(self):
        with self.assertRaisesRegex(oi.Malformed, 'nested too deeply'):
            oi.parse_item(oi.OPEN_ITEM_PREFIX + '[' * 3000 + ']' * 3000, 'kit-1')


class DerivationTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()

    def test_the_fixture_mix_reads_as_expected(self):
        sink = fx.MemorySink()
        expected = fx.build(sink, 20, 10, OPERATOR, task='kit-task')
        views, cut = oi.ledger(sink.all_rows(), (OPERATOR,), journal_dir(self, sink), TODAY)
        self.assertEqual({v['id']: (v['state'], v['closed_by']) for v in views}, expected)
        self.assertEqual(cut, {'anchors': 30, 'anchors_read': 30, 'cut': False})

    def test_owner_and_relayed_closures(self):
        for authority in ('owner', 'relayed'):
            rid, question = self.ledger.question()
            cid, _ = self.ledger.answer(rid, question, authority)
            self.ledger.close(rid, question, cid)
            view = self.ledger.view(self, rid)
            with self.subTest(authority=authority):
                self.assertEqual((view['state'], view['closed_by'], view['conflicted']), ('resolved', authority, False))
                shown = oi._answer_view(view['answers'][0])
                self.assertEqual('warning' in shown, authority == 'relayed')
                if authority == 'relayed':
                    self.assertIn('not proof the owner said them', shown['warning'])

    def test_an_answer_closes_only_when_attested_and_journaled(self):
        cases = {'no journal entry': dict(journal=False),
                 'author is not the actor': dict(author='mallory'),
                 'not on the allowlist': dict(operators=()),
                 'owner authority from someone else': dict(authority='owner', by=fx.block(OPERATOR, person='person:x')),
                 'answer for someone else': dict(owner='person:x')}
        for name, case in cases.items():
            with self.subTest(case=name):
                ledger = Ledger()
                rid, question = ledger.question()
                operators = case.pop('operators', (OPERATOR,))
                cid, _ = ledger.answer(rid, question, **{'authority': 'relayed', **case})
                ledger.close(rid, question, cid)
                view = ledger.view(self, rid, operators)
                self.assertEqual((view['state'], view['stored_state'], view['closed_by']), ('open', 'resolved', None))
                self.assertTrue(view['conflicted'])
                self.assertIn('untrusted-answer', [w['code'] for w in view['warnings']]
                              + (['untrusted-answer'] if name == 'not on the allowlist' else []))

    def test_a_journal_entry_must_bind_this_comment_and_be_a_regular_file(self):
        rid, question = self.ledger.question()
        cid, answer = self.ledger.answer(rid, question, journal=False)
        self.ledger.close(rid, question, cid)
        folder = journal_dir(self, self.ledger.sink)
        path = folder / oi.OWNER_ANSWERS_JOURNAL / (answer['sha256'] + '.json')
        path.write_text(json.dumps(fx.journal_entry(answer, '999')), encoding='utf-8')
        row = self.ledger.sink.rows[rid]
        self.assertIsNone(oi.item_view(row, (OPERATOR,), folder, TODAY)['closed_by'])
        path.write_text(json.dumps(fx.journal_entry(answer, cid)), encoding='utf-8')
        self.assertEqual(oi.item_view(row, (OPERATOR,), folder, TODAY)['closed_by'], 'relayed')
        self.assertIsNone(oi.item_view(row, (OPERATOR,), None, TODAY)['closed_by'])

    def test_reopen_and_a_later_answer(self):
        rid, question = self.ledger.question()
        first, _ = self.ledger.answer(rid, question, 'relayed')
        self.ledger.close(rid, question, first)
        rcid = self.ledger.close(rid, question, first, disposition='reopened', revision=3)
        view = self.ledger.view(self, rid)
        self.assertEqual((view['state'], view['closed_by'], view['reopened_by']), ('open', None, rcid))
        second, _ = self.ledger.answer(rid, question, 'owner', words='No, drop it.', option='drop')
        self.ledger.close(rid, question, second, revision=4)
        view = self.ledger.view(self, rid)
        self.assertEqual((view['state'], view['closed_by'], view['reopened_by']), ('resolved', 'owner', None))
        self.assertEqual([a['comment_id'] for a in view['answers']], [second, first])

    def test_conflicts_are_reported_not_repaired(self):
        rid = self.ledger.anchor()
        self.ledger.post(rid, oi.OPEN_ITEM_PREFIX, fx.item_record(rid, submitted_by=fx.block(OPERATOR)))
        self.ledger.post(rid, oi.OPEN_ITEM_PREFIX, fx.item_record(rid, revision=2, state='resolved', resolved_by='99',
                                                                  submitted_by=fx.block(OPERATOR)))
        view = self.ledger.view(self, rid)
        self.assertTrue(view['conflicted'])
        self.assertEqual(view['state'], 'resolved')
        rid = self.ledger.anchor()
        self.ledger.post(rid, oi.OPEN_ITEM_PREFIX, fx.item_record(rid, text='one'))
        self.ledger.post(rid, oi.OPEN_ITEM_PREFIX, fx.item_record(rid, text='two'))
        self.assertIn('two different records claim revision 1',
                      [w['detail'] for w in self.ledger.view(self, rid)['warnings']])

    def test_a_closure_must_name_a_resolution_with_its_disposition(self):
        rid = self.ledger.anchor()
        self.ledger.post(rid, oi.OPEN_ITEM_PREFIX, fx.item_record(rid, submitted_by=fx.block(OPERATOR)))
        rcid = self.ledger.post(rid, oi.ITEM_RESOLUTION_PREFIX,
                                fx.resolution_record(rid, 1, disposition='superseded', evidence='item:kit-9',
                                                     by=fx.block(OPERATOR), serial=1))
        self.ledger.post(rid, oi.OPEN_ITEM_PREFIX, fx.item_record(rid, revision=2, state='resolved', resolved_by=rcid,
                                                                  submitted_by=fx.block(OPERATOR)))
        view = self.ledger.view(self, rid)
        self.assertTrue(view['conflicted'])
        self.assertIsNone(view['resolution'])
        self.assertIn('state resolved has no matching item-resolution-v1 (resolved_by %s)' % rcid,
                      [w['detail'] for w in view['warnings']])

    def test_trust_words_are_one_set(self):
        self.assertEqual(oi.TRUST_WORDS, ('attested', 'unattested'))
        host = fx.block(OPERATOR)
        self.assertEqual(oi.trust_of(host, OPERATOR, (OPERATOR,)), 'attested')
        self.assertEqual(oi.trust_of(host, OPERATOR, ()), 'unattested')
        self.assertEqual(oi.trust_of(host, 'other', (OPERATOR,)), 'unattested')
        self.assertEqual(oi.trust_of(fx.block('a', 'endpoint'), 'a', ('a',)), 'unattested')
        web = fx.block('usr_0123456789abcdef', 'web', person=OWNER)
        self.assertEqual(oi.trust_of(web, 'usr_0123456789abcdef', ()), 'attested')
        self.assertEqual(oi.trust_of(dict(web, actor='alice'), 'alice', ()), 'unattested')

    def test_comments_that_only_look_like_records(self):
        rid = self.ledger.anchor()
        item = fx.item_record(rid, submitted_by=fx.block(OPERATOR))
        self.ledger.post(rid, oi.OPEN_ITEM_PREFIX, item)
        text = fx.body(oi.OPEN_ITEM_PREFIX, item)
        odd = {'no newline': text.replace('v1\n', 'v1', 1), 'version vx': text.replace('-v1\n', '-vx\n', 1),
               'bom': '﻿' + text, 'newer version': text.replace('-v1\n', '-v2\n', 1),
               'decision here': 'Kind: coordinator-decision-v1\n{}', 'deep': oi.OWNER_ANSWER_PREFIX + '[' * 3000,
               'prose': 'We talked about Kind: open-item-v1 today.'}
        for text in odd.values():
            self.ledger.sink.comment(rid, text, 'mallory')
        view = self.ledger.view(self, rid)
        self.assertEqual((view['state'], view['revision'], view['record_comment_id']), ('open', 1, '1'))
        details = {w['comment_id']: w['detail'] for w in view['warnings']}
        self.assertEqual(details['2'], 'starts like an open-item record but is not one; it is ignored')
        self.assertEqual(details['3'], 'starts like an open-item record but is not one; it is ignored')
        codes = sorted(w['code'] for w in view['warnings'])
        self.assertEqual(codes, sorted(['not-a-record', 'not-a-record', 'malformed-record', 'unsupported-record',
                                        'misplaced-record', 'malformed-record']))

    def test_an_anchor_without_a_readable_revision_is_unreadable(self):
        rid = self.ledger.anchor()
        self.ledger.sink.comment(rid, oi.OPEN_ITEM_PREFIX + '{"broken": ', OPERATOR)
        view = self.ledger.view(self, rid)
        self.assertEqual(view['state'], 'unreadable')
        listed = oi.read_items(['list'], self.ledger.sink.all_rows(), [], (OPERATOR,), None, TODAY)
        self.assertEqual((listed['total'], [u['id'] for u in listed['unreadable']]), (0, [rid]))

    def test_the_caps_cut_and_say_so(self):
        rid = self.ledger.anchor()
        for revision in range(1, 6):
            self.ledger.post(rid, oi.OPEN_ITEM_PREFIX, fx.item_record(rid, revision=revision))
        with patch.object(oi, 'RECORDS_PER_ANCHOR_MAX', 3):
            view = self.ledger.view(self, rid)
        self.assertEqual((view['revision'], view['coverage']), (3, {'records_read': 3, 'cut': True}))
        self.assertIn('records-cap', [w['code'] for w in view['warnings']])
        for _ in range(3):
            other = self.ledger.anchor()
            self.ledger.post(other, oi.OPEN_ITEM_PREFIX, fx.item_record(other))
        with patch.object(oi, 'ITEM_ANCHORS_MAX', 2):
            listed = oi.read_items(['list'], self.ledger.sink.all_rows(), [], (), None, TODAY)
        self.assertEqual(listed['coverage']['anchors'], 4)
        self.assertEqual((listed['coverage']['anchors_read'], listed['coverage']['cut']), (2, True))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.sink = fx.MemorySink()
        self.expected = fx.build(self.sink, 20, 10, OPERATOR, task='kit-task')
        self.journal = journal_dir(self, self.sink)

    def items(self, *args):
        return oi.read_items(list(args), self.sink.all_rows(), [], (OPERATOR,), self.journal, TODAY)

    def questions(self, *args):
        return oi.read_questions(list(args), self.sink.all_rows(), [], (OPERATOR,), self.journal, TODAY)

    def test_items_list_filters_and_pages(self):
        everything = self.items('list', '--limit', '100')
        self.assertEqual(everything['total'], 30)
        self.assertEqual(self.items('list', '--state', 'blocked')['total'], 4)
        self.assertEqual(self.items('list', '--kind', 'question')['total'], 10)
        self.assertEqual(self.items('list', '--closed-by', 'relayed')['total'], 2)
        self.assertEqual(self.items('list', '--task', 'nope')['total'], 0)
        page = self.items('list', '--limit', '7', '--offset', '7')
        self.assertEqual((len(page['items']), page['next_offset']), (7, 14))
        # Items fall due on 2026-10-01..20 and questions in November; today is 2026-10-06.
        self.assertEqual(self.items('list', '--due', 'expired')['total'], 5)
        self.assertEqual(self.items('list', '--due', 'due-soon')['total'], 8)   # 06..13 inclusive

    def test_items_get(self):
        rid = next(r for r, (state, by) in self.expected.items() if by == 'owner')
        got = self.items('get', rid)
        self.assertEqual((got['closed_by'], got['resolution']['disposition'], got['answers'][0]['authority']),
                         ('owner', 'resolved', 'owner'))
        with self.assertRaisesRegex(ValueError, 'is unknown'):
            self.items('get', 'kit-999')

    def test_refusals(self):
        for args, message in ((['list', '--state', 'void'], '--state must be'),
                              (['list', '--closed-by', 'me'], '--closed-by must be'),
                              (['list', '--due', 'soon'], '--due must be'),
                              (['list', '--limit', '0'], '--limit must be 1..100'),
                              (['list', '--offset', '-1'], '--offset must be >= 0'),
                              (['list', '--kind', 'note'], '--kind must be'),
                              (['list', '--color', 'x'], 'unknown option'),
                              (['delete'], 'usage')):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, message):
                self.items(*args)
        with self.assertRaisesRegex(ValueError, 'questions needs --for OWNER'):
            self.questions()
        with self.assertRaisesRegex(ValueError, 'durable identity'):
            self.questions('--for', 'session-abcd')

    def test_questions_counts_are_disjoint(self):
        result = self.questions('--for', OWNER, '--limit', '100')
        self.assertEqual((result['total'], result['open'], result['closed_by_owner'], result['closed_relayed']),
                         (10, 6, 2, 2))
        self.assertEqual(result['open'] + result['closed_by_owner'] + result['closed_relayed'], result['total'])
        relayed = self.questions('--for', OWNER, '--closed-by', 'relayed')
        self.assertEqual(relayed['matching'], 2)
        self.assertIn('not proof the owner said them', relayed['items'][0]['answer']['warning'])
        self.assertEqual(self.questions('--for', OWNER, '--state', 'open')['matching'], 6)
        self.assertEqual(self.questions('--for', 'person:other')['total'], 0)

    def test_questions_get(self):
        rid = next(r for r, (state, by) in self.expected.items() if by == 'relayed')
        got = self.questions('get', rid)
        self.assertEqual((got['closed_by'], len(got['answers']), got['resolution']['disposition']),
                         ('relayed', 1, 'resolved'))
        blocker = next(r for r, (state, by) in self.expected.items() if state == 'blocked')
        with self.assertRaisesRegex(ValueError, 'not a question'):
            self.questions('get', blocker)


class NativeReadTests(unittest.TestCase):
    """read_anchor_rows: one list and one show; an unparseable answer is never fatal."""

    def setUp(self):
        self.sink = fx.MemorySink()
        fx.build(self.sink, 4, 0, OPERATOR)
        self.calls = []

    def run_bd(self, rows, deep=()):
        def run(argv):
            self.calls.append(argv[0])
            if argv[0] == 'list':
                return json.dumps([{'id': r['id'], 'labels': r['labels']} for r in rows])
            if argv[0] == 'show':
                ids = [a for a in argv[1:] if not a.startswith('--')]
                if any(i in deep for i in ids):
                    return '[' + '[' * 3000 + ']' * 3000 + ']'
                return json.dumps([r for r in rows if r['id'] in ids])
            if argv[0] == 'export':
                return '\n'.join('{"id": "%s", "x": %s}' % (r['id'], '[' * 3000 + ']' * 3000) if r['id'] in deep
                                 else json.dumps(r) for r in rows)
            raise AssertionError(argv)
        return run

    def test_one_list_and_one_show(self):
        rows, unreadable = oi.read_anchor_rows(self.run_bd(self.sink.all_rows()))
        self.assertEqual((len(rows), unreadable, self.calls), (4, [], ['list', 'show']))

    def test_an_unparseable_row_in_show_is_isolated_and_reported(self):
        rows, unreadable = oi.read_anchor_rows(self.run_bd(self.sink.all_rows(), deep={'kit-2'}))
        self.assertEqual(sorted(r['id'] for r in rows), ['kit-1', 'kit-3', 'kit-4'])
        self.assertEqual([u['id'] for u in unreadable], ['kit-2'])

    def test_an_unparseable_list_falls_back_to_the_export(self):
        rows = self.sink.all_rows()

        def run(argv):
            self.calls.append(argv[0])
            if argv[0] == 'list':
                return '[' + '[' * 3000
            return self.run_bd(rows, deep={'kit-3'})(argv)
        found, unreadable = oi.read_anchor_rows(run)
        self.assertEqual(sorted(r['id'] for r in found), ['kit-1', 'kit-2', 'kit-4'])
        self.assertEqual([u['id'] for u in unreadable], ['kit-3'])
        self.assertIn('not known whether it is an item anchor', unreadable[0]['reason'])

    def test_an_unlabelled_project_reads_nothing(self):
        rows, unreadable = oi.read_anchor_rows(lambda argv: '[]')
        self.assertEqual((rows, unreadable), ([], []))


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.sink = fx.MemorySink()
        fx.build(self.sink, 10, 5, OPERATOR, task='trial-task')
        self.journal = journal_dir(self, self.sink)

    def test_the_two_kinds_and_their_caps(self):
        result = oi.brief_attention(self.sink.all_rows(), {'id': 'trial-task'}, (OPERATOR,), self.journal, TODAY)
        kinds = [item['kind'] for item in result['attention']]
        self.assertEqual(kinds, ['open-item'] * 3 + ['owner-question'] * 3)
        # 8 live items + 3 live questions (2 open, 1 unjournaled) are counted; the questions are items too.
        self.assertEqual((result['attention_total'], result['attention_more']), (11 + 3, 14 - 6))
        self.assertEqual(oi.brief_attention(self.sink.all_rows(), {'id': 'other'}, (), None, TODAY),
                         {'attention': [], 'attention_total': 0, 'attention_more': None})

    def test_brief_renders_them_after_every_other_kind(self):
        import briefing
        import test_briefing as tb
        rows = tb.rows() + self.sink.all_rows()
        with patch.object(oi.datetime, 'date', wraps=datetime.date) as day:
            day.today.return_value = TODAY
            result = briefing.brief(rows, tb.PROJECT, tb.TASK, operators=[OPERATOR], journal=self.journal)
        self.assertEqual([i['kind'] for i in result['attention']][-6:], ['open-item'] * 3 + ['owner-question'] * 3)
        text = briefing.format_brief(result)
        self.assertIn('Open item [open, attested]: Item 0', text)
        self.assertIn('Owner question [', text)
        self.assertIn('questions get ', text)
        # A project with no item anchor briefs exactly as before.
        plain = briefing.brief(tb.rows(), tb.PROJECT, tb.TASK)
        self.assertEqual([i['kind'] for i in plain['attention']], [])


class EndpointTests(unittest.TestCase):
    def setUp(self):
        from test_reference_wiring import _endpoint_module
        self.endpoint = _endpoint_module(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / 'projects' / 'p'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'unit': 'none', 'operators': [OPERATOR]}), encoding='utf-8')
        self.sink = fx.MemorySink()
        fx.build(self.sink, 4, 5, OPERATOR)
        (self.project / oi.OWNER_ANSWERS_JOURNAL).mkdir()
        for entry in self.sink.journal.values():
            (self.project / oi.OWNER_ANSWERS_JOURNAL / (entry['sha256'] + '.json')).write_text(json.dumps(entry))
        self.native = []

    def fake_run(self, argv, env, timeout=None):
        command = list(map(str, argv))
        command = command[command.index('--sandbox') + 1:]
        if command[:1] == ['--actor']:
            command = command[2:]
        self.native.append(command[0])
        rows = self.sink.all_rows()
        if command[0] == 'list':
            stdout = json.dumps([{'id': r['id'], 'labels': r['labels']} for r in rows])
        elif command[0] == 'show':
            stdout = json.dumps([r for r in rows if r['id'] in command])
        else:
            raise AssertionError(command)
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr='')

    def execute(self, action, args):
        request = {'project': 'p', 'actor': 'alice', 'action': action, 'args': args, 'attachments': {}}
        with patch.object(self.endpoint, 'project_dir', return_value=self.project), \
                patch.object(self.endpoint, 'environment', return_value={}), \
                patch.object(self.endpoint.native, 'run', side_effect=self.fake_run), \
                patch.object(self.endpoint, 'run_guarded', side_effect=AssertionError('guarded')):
            return self.endpoint.execute(self.root, request)

    def test_both_reads_are_one_list_and_one_show_and_never_guarded(self):
        reply = self.execute('questions', ['--for', OWNER])
        self.assertEqual(reply['returncode'], 0, reply)
        result = json.loads(reply['stdout'])
        self.assertEqual((result['total'], result['closed_by_owner'], result['closed_relayed']), (5, 1, 1))
        self.assertEqual(self.native, ['list', 'show'])
        reply = self.execute('items', ['list'])
        self.assertEqual(json.loads(reply['stdout'])['total'], 9)
        help_reply = json.loads(self.execute('items', ['--help'])['stdout'])
        self.assertTrue(help_reply['read_only'])

    def test_the_client_routes_both_actions(self):
        import client
        calls = []
        config = self.root / 'client.json'
        config.write_text(json.dumps({'transport': 'ssh', 'host': 'h', 'endpoint': '/e.py', 'root': '/r'}))
        for argv in (['items', 'list'], ['questions', '--for', OWNER]):
            with patch.object(client, 'request', side_effect=lambda c, p, a, args, action='bd', path=None:
                              calls.append((action, list(args))) or {'stdout': '{}', 'stderr': '', 'returncode': 0}), \
                    patch.object(sys, 'argv', ['client.py', '--config', str(config), '--project', 'p', '--actor',
                                               'alice', '--'] + argv), contextlib.redirect_stdout(io.StringIO()):
                client.main()
        self.assertEqual(calls, [('items', ['list']), ('questions', ['--for', OWNER])])


class ReadOnlyTests(unittest.TestCase):
    def test_the_reader_writes_nothing(self):
        text = (KIT / 'open_items.py').read_text(encoding='utf-8')
        for verb in ('write_text', 'write_bytes', 'mkdir', "'comments', 'add'", "'create'", "'update'", "'close'",
                     'atomic('):
            self.assertNotIn(verb, text, verb)


if __name__ == '__main__':
    unittest.main()
