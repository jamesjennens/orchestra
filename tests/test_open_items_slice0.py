"""Open items, owner questions and decisions, slice 0 (kittrial-5bb.126): names only.

docs/OPEN_ITEMS_DECISIONS_DESIGN.md section 13, item 0, as amended by the review
conditions. Slice 0 changes behaviour in exactly two ways: a raw comment with one of the
four prefixes is refused, and an `open-item:` label is reserved. Everything else it adds
is a name: the hidden-surface tables, the void registration (named, not yet voidable),
the two journals in the backup allowlist (and `.open-item-requests` in
RESERVATION_JOURNALS), the frozen `.owner-answers` entry validator, and the read-only
deploy-time `open-item-label-check`. These tests pin each table row and both directions
of the journal validation, and that no kit module writes a record or a journal.
"""
import contextlib
import copy
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
import open_items
import recovery
import reserved_comments as rc
import work
from render import render
from requirements import content_hash

PREFIX_WRITERS = {
    'Kind: open-item-v1\n': 'items add|revise|block|unblock|resolve|reopen or questions ask',
    'Kind: item-resolution-v1\n': 'items resolve|reopen or questions answer',
    'Kind: owner-answer-v1\n': 'questions answer on the coordination host or in the web interface',
    'Kind: coordinator-decision-v1\n': 'decisions record on the coordination host or in the web interface',
}
KINDS = ('open-item', 'item-resolution', 'owner-answer', 'coordinator-decision')
RECORD_MARKERS = ('Kind: open-item-', 'Kind: item-resolution-', 'Kind: owner-answer-',
                  'Kind: coordinator-decision-')
STATE_LABELS = ['open-item:open', 'open-item:blocked', 'open-item:resolved', 'open-item:superseded',
                'open-item:anything']
ORDINARY_LABELS = ['open-item', 'open-items', 'open-item-old', 'openitem', 'Open-Item:open', 'item:open',
                   'decision', 'decision:x', 'ops']
JOURNALS = ('.open-item-requests', '.owner-answers')
STAMP = '2026-10-06T09:00:00Z'
SERVER_METADATA = {'dolt_server_host': '127.0.0.1', 'dolt_server_port': 3307, 'dolt_server_user': 'root',
                   'dolt_database': 'project'}


def block(actor='coordinator', route='host', person='person:james'):
    return {'actor': actor, 'route': route, 'identity': 'verified', 'person': person}


def answer_payload(**extra):
    payload = {'schema_version': 1, 'answer': 'a-0123456789ab', 'item': 'kit-7', 'question_revision': 1,
               'question_sha256': 'e' * 64, 'owner': 'person:james', 'option': 'keep',
               'options_offered': [{'id': 'keep', 'text': 'Keep it'}, {'id': 'drop', 'text': 'Drop it'}],
               'words': 'Keep it, for now.', 'authority': 'relayed', 'relayed_by': block(),
               'by': block(), 'at': STAMP}
    payload.update(extra)
    payload['sha256'] = content_hash(payload)
    return payload


def decision_payload(**extra):
    payload = {'schema_version': 1, 'decision': 'd-0123456789ab', 'revision': 1, 'issue': 'kit-9',
               'title': 'Use one journal', 'decides': ['kit-1', 'kit-2'], 'authority': 'owner',
               'supersedes': None, 'decided_by': block(person='person:james'), 'at': STAMP}
    payload.update(extra)
    payload['sha256'] = content_hash(payload)
    return payload


def answer_entry(payload=None, **extra):
    payload = payload or answer_payload()
    entry = {'schema_version': 1, 'kind': 'owner-answer', 'item': payload['item'],
             'revision': payload['question_revision'], 'owner': payload['owner'], 'option': payload['option'],
             'words': payload['words'], 'comment_id': '41', 'payload': payload, 'sha256': payload['sha256']}
    entry.update(extra)
    return entry


def decision_entry(payload=None, **extra):
    payload = payload or decision_payload()
    entry = {'schema_version': 1, 'kind': 'coordinator-decision', 'issue': payload['issue'],
             'revision': payload['revision'], 'decides': list(payload['decides']), 'title': payload['title'],
             'comment_id': '42', 'payload': payload, 'sha256': payload['sha256']}
    entry.update(extra)
    return entry


def entry_name(entry):
    return '.owner-answers/%s.json' % entry['payload']['sha256']


def receipt(**extra):
    record = {'sha256': 'c' * 64, 'status': 'complete', 'actor': 'coordinator', 'id': 'kit-7', 'revision': 1}
    record.update(extra)
    return record


def rows_as_a_later_slice_writes_them():
    """An item anchor, a question anchor, a decision issue and the project's own labels."""
    return [
        {'id': 'kit-1', 'title': 'Ordinary task', 'status': 'open', 'issue_type': 'task', 'labels': ['ops'],
         'comments': [{'id': 1, 'text': 'A normal journal entry.', 'created_at': STAMP}]},
        {'id': 'kit-2', 'title': 'Item anchor', 'status': 'closed', 'issue_type': 'task',
         'labels': ['open-item', 'open-item:open'],
         'comments': [{'id': 2, 'text': 'Kind: open-item-v1\n{}', 'created_at': STAMP}]},
        {'id': 'kit-3', 'title': 'Question anchor', 'status': 'closed', 'issue_type': 'task',
         'labels': ['open-item', 'open-item:resolved'],
         'comments': [{'id': 3, 'text': 'Kind: open-item-v1\n{}', 'created_at': STAMP},
                      {'id': 4, 'text': 'Kind: owner-answer-v1\n{}', 'created_at': STAMP},
                      {'id': 5, 'text': 'Kind: item-resolution-v1\n{}', 'created_at': STAMP},
                      {'id': 6, 'text': 'Kind: open-item-v4\n{"future": true}', 'created_at': STAMP}]},
        # The native decision issue is a real work item: only its record comment hides.
        {'id': 'kit-4', 'title': 'Decision: one journal', 'status': 'open', 'issue_type': 'decision',
         'labels': [], 'comments': [
             {'id': 7, 'text': 'Kind: coordinator-decision-v1\n{}', 'created_at': STAMP},
             {'id': 8, 'text': 'Discussion on the decision.', 'created_at': STAMP}]},
        # A project's own `open-item` label without a record is an ordinary task.
        {'id': 'kit-5', 'title': 'Their open item', 'status': 'open', 'issue_type': 'task',
         'labels': ['open-item'], 'comments': []},
        # A decision record does not make an `open-item`-labelled row an anchor.
        {'id': 'kit-6', 'title': 'Mislabelled', 'status': 'open', 'issue_type': 'task',
         'labels': ['open-item'], 'comments': [
             {'id': 9, 'text': 'Kind: coordinator-decision-v1\n{}', 'created_at': STAMP}]},
    ]


ANCHOR_IDS = {'kit-2', 'kit-3'}
VISIBLE_IDS = ['kit-1', 'kit-4', 'kit-5', 'kit-6']


class GuardTests(unittest.TestCase):
    """Behaviour change one: a raw comment with one of the four prefixes is refused."""

    def test_every_prefix_is_reserved_and_names_its_writer(self):
        self.assertEqual(open_items.PREFIXES, tuple(PREFIX_WRITERS))
        for prefix, writer in PREFIX_WRITERS.items():
            with self.subTest(prefix=prefix):
                self.assertIn(prefix, rc.PREFIXES)
                body = prefix + '{}'
                self.assertEqual(rc.reserved_match(body)[2], writer)
                for args, attachments in (
                        (['comments', 'add', 'kit-1', body], {}),
                        (['comments', '--json', 'add', 'kit-1', body], {}),
                        (['comments', 'add', 'kit-1', '@attachment:0'], {'0': {'flag': '--file', 'text': body}})):
                    with self.assertRaisesRegex(ValueError, 'Refusing raw'):
                        rc.check_raw_request(args, attachments, actor='alice', task='kit-1')
                for lookalike in ('﻿' + body, body.replace('\n', '\r\n')):
                    with self.assertRaises(ValueError):
                        rc.check_comment_body(lookalike, 'positional', actor='alice', task='kit-1')

    def test_every_version_of_each_kind_is_reserved_and_prose_is_not(self):
        rc.check_comment_body('We discussed Kind: owner-answer-v1 today.', 'positional', actor='alice', task='kit-1')
        rc.check_comment_body('Kind: open-items-v1\n{}', 'positional', actor='alice', task='kit-1')
        for kind in KINDS:
            for version in ('v1', 'v2', 'v123'):
                with self.subTest(kind=kind, version=version):
                    with self.assertRaisesRegex(ValueError, 'Refusing raw'):
                        rc.check_comment_body('Kind: %s-%s\n{}' % (kind, version), 'positional', actor='alice',
                                              task='kit-1')

    def test_record_comment_kind_reads_them_as_records_without_a_reader(self):
        for kind in KINDS:
            self.assertTrue(rc.is_record_comment('Kind: %s-v1\n{}' % kind))
            self.assertEqual(rc.record_comment_kind('Kind: %s-v1\n{}' % kind), (kind, 1, 'supported'))
            self.assertEqual(rc.record_comment_kind('Kind: %s-v2\n{}' % kind), (kind, 2, 'unsupported'))
        self.assertNotIn(('open-item', 2), rc.SUPPORTED_LATER_VERSIONS)


class LabelTests(unittest.TestCase):
    """Behaviour change two: `open-item:` labels are reserved; `open-item` itself is not."""

    def test_state_labels_are_reserved_and_their_neighbours_are_not(self):
        self.assertIn('open-item:', rc.RESERVED_LABEL_PREFIXES)
        self.assertEqual(open_items.STATE_LABEL_PREFIX, 'open-item:')
        for label in STATE_LABELS:
            self.assertEqual(rc.reserved_label(label), label)
        for label in ORDINARY_LABELS:
            self.assertIsNone(rc.reserved_label(label), label)
        # No exact label was added: `open-item` stays an ordinary label everywhere.
        self.assertEqual(rc.RESERVED_EXACT_LABELS, frozenset({'requirement', 'brd-section', 'gt:slot'}))

    def test_every_label_writing_spelling_is_refused(self):
        for label in STATE_LABELS:
            for args in (['update', 'kit-1', '--add-label', label],
                         ['update', 'kit-1', '--set-labels', 'ops,' + label],
                         ['update', 'kit-1', '--remove-label', label],
                         ['create', '--title', 't', '--labels', label],
                         ['create', '--title', 't', '--label', label],
                         ['create', '--title', 't', '-l', label]):
                with self.subTest(args=args):
                    self.assertEqual(rc.reserved_label_in_args(args), label)
        for args in (['update', 'kit-5', '--add-label', 'open-item'],
                     ['update', 'kit-5', '--remove-label', 'open-item'],
                     ['create', '--title', 't', '--labels', 'open-item']):
            self.assertIsNone(rc.reserved_label_in_args(args), args)

    def test_read_before_write_guard_sees_held_state_labels(self):
        for label in STATE_LABELS:
            self.assertEqual(rc.first_reserved_label(['ops', label]), label)
        self.assertIsNone(rc.first_reserved_label(ORDINARY_LABELS))


class HiddenSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.rows = rows_as_a_later_slice_writes_them()

    def test_the_tables(self):
        self.assertEqual(rc.RECORD_ANCHOR_FAMILIES['open-item'],
                         (rc.OPEN_ITEM_PREFIX, rc.ITEM_RESOLUTION_PREFIX, rc.OWNER_ANSWER_PREFIX))
        self.assertEqual(open_items.FAMILY_LABEL, 'open-item')
        self.assertNotIn('coordinator-decision', rc.RECORD_ANCHOR_FAMILIES)
        self.assertNotIn('decision', rc.RECORD_ANCHOR_FAMILIES)
        for marker in RECORD_MARKERS:
            self.assertIn(marker, rc.RECORD_COMMENT_FAMILIES)

    def test_an_anchor_needs_the_label_and_an_item_record(self):
        rows = {row['id']: row for row in self.rows}
        self.assertEqual({rid for rid, row in rows.items() if rc.is_record_anchor(row)}, ANCHOR_IDS)
        self.assertFalse(rc.is_record_anchor({'labels': ['open-item']}))

    def test_hide_records_hides_anchors_and_the_decision_comment_only(self):
        shown = rc.hide_records(self.rows)
        self.assertEqual([row['id'] for row in shown], VISIBLE_IDS)
        decision = next(row for row in shown if row['id'] == 'kit-4')
        self.assertEqual([c['id'] for c in decision['comments']], [8])
        self.assertEqual(next(row for row in shown if row['id'] == 'kit-6')['comments'], [])

    def test_work_render_and_history_never_show_them(self):
        listed = work.queue(self.rows, 'session-x', [])
        self.assertEqual(sorted(item['task'] for item in listed['items']), VISIBLE_IDS)
        with tempfile.TemporaryDirectory() as temp:
            views = Path(temp) / 'views'
            render(self.rows, views)
            for path in views.rglob('*'):
                if path.is_file():
                    text = path.read_text(encoding='utf-8')
                    for marker in RECORD_MARKERS:
                        self.assertNotIn(marker, text, path.name)
        import briefing
        data = briefing.snapshot(self.rows, 'kit', 'kit-4')
        self.assertEqual([entry['body'] for entry in data['entries']], ['Discussion on the decision.'])


class VoidRegistrationTests(unittest.TestCase):
    """Named for the void machinery, and still refused as a target until a reader honours voids."""

    def test_the_kinds_are_named_with_the_reserved_prefixes(self):
        self.assertEqual(recovery.OPEN_ITEM_KIND_PREFIXES, {
            'open-item': rc.OPEN_ITEM_PREFIX, 'item-resolution': rc.ITEM_RESOLUTION_PREFIX,
            'owner-answer': rc.OWNER_ANSWER_PREFIX, 'coordinator-decision': rc.COORDINATOR_DECISION_PREFIX})

    def test_a_void_naming_one_is_refused_as_today(self):
        for kind in KINDS:
            self.assertNotIn(kind, recovery.KIND_PREFIXES)
            self.assertNotIn(kind, recovery.KEYED_KIND_PREFIXES)
            original = 'Kind: %s-v1\n{}' % kind
            payload = dict(schema_version=1, operation='void-record', operation_id='v1', task='kit-2',
                           target='2', target_kind=kind, target_sha256=recovery.digest(original),
                           original=original, reason='Malformed', disposition='void', operator='operator')
            with self.assertRaisesRegex(ValueError, 'Unsupported operator void target kind'):
                recovery.issued(payload, 'kit-2')


class OwnerEntryTests(unittest.TestCase):
    """The frozen `.owner-answers` entry: exact field sets, bound to the payload and its name."""

    def test_the_field_sets(self):
        self.assertEqual(open_items.ENTRY_FIELDS, {
            'owner-answer': frozenset({'schema_version', 'kind', 'item', 'revision', 'owner', 'option', 'words',
                                       'comment_id', 'payload', 'sha256'}),
            'coordinator-decision': frozenset({'schema_version', 'kind', 'issue', 'revision', 'decides', 'title',
                                               'comment_id', 'payload', 'sha256'})})
        self.assertEqual(len(open_items.ANSWER_FIELDS), 14)
        self.assertEqual(len(open_items.DECISION_FIELDS), 11)

    def test_valid_entries_pass_with_their_names(self):
        for entry in (answer_entry(), answer_entry(answer_payload(authority='owner', relayed_by=None)),
                      answer_entry(answer_payload(option=None)), decision_entry(),
                      decision_entry(decision_payload(supersedes='17', decided_by=block(route='web')))):
            self.assertIs(open_items.validate_owner_entry(entry, entry['sha256'] + '.json'), entry)

    def bad_entries(self):
        good = answer_entry()
        yield 'not an object', []
        yield 'unknown kind', dict(good, kind='owner-reply')
        yield 'extra field', dict(good, operator='x')
        missing = dict(good)
        del missing['comment_id']
        yield 'missing field', missing
        yield 'version', dict(good, schema_version=2)
        yield 'boolean version', dict(good, schema_version=True)
        yield 'comment id', dict(good, comment_id='')
        yield 'item mismatch', dict(good, item='kit-8')
        yield 'revision mismatch', dict(good, revision=2)
        yield 'owner mismatch', dict(good, owner='person:someone')
        yield 'option mismatch', dict(good, option='drop')
        yield 'words mismatch', dict(good, words='Drop it.')
        yield 'entry hash', dict(good, sha256='f' * 64)
        tampered = copy.deepcopy(good)
        tampered['payload']['at'] = '2026-10-06T09:00:01Z'
        yield 'payload hash', tampered
        # content_hash leaves out the top-level sha256, so only the payload's own field
        # being compared catches a payload whose sha256 is wrong while the entry's is right.
        wrong_own = copy.deepcopy(good)
        wrong_own['payload']['sha256'] = 'f' * 64
        yield "payload's own sha256", wrong_own
        for name, payload in (
                ('answer id', answer_payload(answer='a-XYZ')),
                ('question revision', answer_payload(question_revision=0)),
                ('boolean revision', answer_payload(question_revision=True)),
                ('question hash', answer_payload(question_sha256='E' * 64)),
                ('owner session', answer_payload(owner='session-abcd')),
                ('option not offered', answer_payload(option='other')),
                ('no options', answer_payload(options_offered=[])),
                ('nine options', answer_payload(options_offered=[{'id': 'o%d' % i, 'text': 't'} for i in range(9)])),
                ('repeated option', answer_payload(options_offered=[{'id': 'keep', 'text': 'a'},
                                                                   {'id': 'keep', 'text': 'b'}])),
                ('option shape', answer_payload(options_offered=[{'id': 'keep', 'text': 'a', 'x': 1}])),
                ('empty words', answer_payload(words=' ')),
                ('long words', answer_payload(words='w' * 4001)),
                ('authority', answer_payload(authority='coordinator')),
                ('owner with relayer', answer_payload(authority='owner')),
                ('relayed without relayer', answer_payload(relayed_by=None)),
                ('endpoint route', answer_payload(by=dict(block(), route='endpoint', identity='unverified'))),
                ('endpoint route claiming verified', answer_payload(by=dict(block(), route='endpoint'))),
                ('endpoint relayer claiming verified', answer_payload(relayed_by=dict(block(), route='endpoint'))),
                ('extra answer payload field', answer_payload(note='x')),
                ('unverified', answer_payload(by=dict(block(), identity='unverified'))),
                ('attribution keys', answer_payload(by=dict(block(), extra=1))),
                ('person', answer_payload(by=dict(block(), person=None))),
                ('stamp', answer_payload(at='2026-10-06 09:00:00'))):
            yield name, answer_entry(payload)
        good = decision_entry()
        yield 'decision issue mismatch', dict(good, issue='kit-8')
        yield 'decision decides mismatch', dict(good, decides=['kit-1'])
        yield 'decision title mismatch', dict(good, title='Other')
        yield 'decision with answer fields', dict(good, kind='owner-answer')
        for name, payload in (
                ('decision id', decision_payload(decision='d-1')),
                ('coordinator authority', decision_payload(authority='coordinator')),
                ('no decides', decision_payload(decides=[])),
                ('seventeen decides', decision_payload(decides=['kit-%d' % i for i in range(17)])),
                ('repeated decides', decision_payload(decides=['kit-1', 'kit-1'])),
                ('decides id', decision_payload(decides=['../x'])),
                ('long title', decision_payload(title='t' * 201)),
                ('supersedes', decision_payload(supersedes='')),
                ('decision revision', decision_payload(revision=0)),
                ('extra payload field', decision_payload(statement='x'))):
            yield name, decision_entry(payload)

    def test_malformed_entries_are_refused(self):
        for name, entry in self.bad_entries():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    open_items.validate_owner_entry(entry)

    def test_the_name_must_be_the_payload_hash(self):
        entry = answer_entry()
        for name in ('f' * 64 + '.json', entry['sha256'], entry['sha256'].upper() + '.json'):
            with self.assertRaisesRegex(ValueError, 'Owner answers journal path mismatch'):
                open_items.validate_owner_entry(entry, name)


class JournalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'projects' / 'source'
        self.destination = self.root / 'projects' / 'destination'
        self.source.mkdir(parents=True)
        self.destination.mkdir()
        (self.root / 'backups' / 'source').mkdir(parents=True)
        self.bundle = self.root / 'backups' / 'source.coordination.json'
        patcher = patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=Mock(), LOCK_EX=2)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, record):
        path = self.source / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(record), encoding='utf-8')

    def test_the_journal_tables(self):
        self.assertIn('.open-item-requests', admin.RECORD_JOURNALS)
        self.assertIn('.open-item-requests', admin.RESERVATION_JOURNALS)
        self.assertEqual(open_items.REQUESTS_JOURNAL, '.open-item-requests')
        self.assertEqual(admin.OWNER_ANSWERS_JOURNAL, open_items.OWNER_ANSWERS_JOURNAL)
        self.assertEqual(admin.OWNER_ANSWERS_JOURNAL, '.owner-answers')
        # Not a receipt journal, and no reservations.
        self.assertNotIn('.owner-answers', admin.RECORD_JOURNALS)
        self.assertNotIn('.owner-answers', admin.RESERVATION_JOURNALS)

    def test_both_journals_round_trip_through_backup_and_restore(self):
        files = {'.open-item-requests/' + 'a' * 64 + '.json': receipt(operation='questions-ask'),
                 '.open-item-requests/' + 'b' * 64 + '.json': receipt(status='pending')}
        for entry in (answer_entry(), decision_entry()):
            files[entry_name(entry)] = entry
        for name, record in files.items():
            self.write(name, record)
        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'source')
        self.assertEqual(json.loads(self.bundle.read_text(encoding='utf-8'))['files'], files)
        admin.restore_coordination(self.root, 'source', 'destination')
        for name, record in files.items():
            self.assertEqual(json.loads((self.destination / name).read_text(encoding='utf-8')), record)

    def test_a_malformed_entry_or_receipt_is_refused_before_anything_is_created(self):
        bad = [(entry_name(answer_entry()), dict(answer_entry(), item='kit-8')),
               ('.owner-answers/' + 'f' * 64 + '.json', answer_entry()),
               ('.owner-answers/' + 'f' * 64 + '.json', {'status': 'complete', 'sha256': 'f' * 64}),
               ('.open-item-requests/' + 'a' * 64 + '.json', receipt(status='done')),
               ('.open-item-requests/' + 'a' * 64 + '.json', receipt(revision=0))]
        for name, record in bad:
            with self.subTest(name=name, record=record):
                with self.assertRaises(ValueError):
                    admin.validate_coordination_files({name: record})
                # A valid entry beside it, so the refusal is not just of the first file.
                files = {entry_name(decision_entry()): decision_entry(), name: record}
                with patch.object(admin, 'coordination_backup', return_value=files):
                    with self.assertRaises(ValueError):
                        admin.restore_coordination(self.root, 'source', 'destination')
                self.assertEqual(list(self.destination.iterdir()), [])

    def test_a_malformed_entry_stops_the_backup_before_native_sync(self):
        self.write(entry_name(answer_entry()), dict(answer_entry(), words='other'))
        with patch.object(admin, 'run_bd') as native:
            with self.assertRaises(ValueError):
                admin.backup_project(self.root, 'source')
        native.assert_not_called()

    def test_paths_must_stay_in_the_journal(self):
        entry = answer_entry()
        for name in ('.owner-answers/../../escape.json', '.owner-answers/notahash.json',
                     '.owner-answers/' + 'b' * 63 + '.json', '.owner-answer/' + entry['sha256'] + '.json',
                     '.open-items-requests/' + 'b' * 64 + '.json'):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, 'Invalid coordination backup path'):
                    admin.validate_coordination_files({name: entry})

    def test_a_symlinked_journal_or_entry_is_refused(self):
        outside = self.root / 'outside'
        outside.mkdir()
        try:
            os.symlink(outside, self.source / '.owner-answers', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('cannot create symlinks here')
        with patch.object(admin, 'run_bd'):
            with self.assertRaisesRegex(ValueError, 'Owner answers journal must not be a symlink'):
                admin.backup_project(self.root, 'source')
        os.unlink(self.source / '.owner-answers')
        (self.source / '.owner-answers').mkdir()
        entry = answer_entry()
        (outside / 'e.json').write_text(json.dumps(entry), encoding='utf-8')
        os.symlink(outside / 'e.json', self.source / entry_name(entry))
        with patch.object(admin, 'run_bd'):
            with self.assertRaisesRegex(ValueError, 'Owner answers journal entry must not be a symlink'):
                admin.backup_project(self.root, 'source')

    def test_writes_off_backup_names_neither_journal(self):
        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'source')
        files = json.loads(self.bundle.read_text(encoding='utf-8'))['files']
        self.assertFalse([name for name in files if name.startswith(JOURNALS)])
        self.assertFalse(any((self.source / journal).exists() for journal in JOURNALS))

    def test_retire_counts_pending_open_item_receipts(self):
        self.write('.open-item-requests/' + 'a' * 64 + '.json', receipt(status='pending'))
        self.write('.open-item-requests/' + 'b' * 64 + '.json', receipt())
        findings = admin.retire_findings(self.root, 'source')
        self.assertEqual(findings['pending_reservations'], {'.open-item-requests': 1})


class LabelCheckTests(unittest.TestCase):
    """Condition 7: a read-only deploy-time listing of the projects using an open-item label."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ('alpha', 'beta', 'gamma'):
            (self.root / 'projects' / name / '.beads').mkdir(parents=True)
            (self.root / 'projects' / name / '.beads' / 'metadata.json').write_text(json.dumps(SERVER_METADATA),
                                                                                  encoding='utf-8')
        self.rows = {
            'alpha': [{'id': 'alpha-1', 'labels': ['ops']}, {'id': 'alpha-2', 'labels': None}],
            'beta': [{'id': 'beta-1', 'labels': ['open-item', 'ops']},
                     {'id': 'beta-2', 'labels': ['open-item:open']},
                     {'id': 'beta-3', 'labels': ['open-items', 'open-item-x']}],
            'gamma': [{'id': 'gamma-1', 'labels': []}]}
        self.calls = []

    def run_bd(self, root, name, argv):
        self.calls.append((name, argv))
        if name not in self.rows:
            raise admin.subprocess.CalledProcessError(1, 'bd')
        return json.dumps(self.rows[name])

    def check(self, *projects):
        out = io.StringIO()
        argv = ['admin.py', '--root', str(self.root), 'open-item-label-check', *projects]
        # root_path insists on a Linux runtime path; the check itself is what is under test,
        # so it runs on the Windows jobs too.
        with patch.object(admin, 'run_bd', side_effect=self.run_bd), patch.object(sys, 'argv', argv), \
                patch.object(admin, 'root_path', Path), contextlib.redirect_stdout(out):
            try:
                admin.main()
                code = 0
            except SystemExit as error:
                code = error.code
        return code, json.loads(out.getvalue())

    def test_it_lists_the_rows_and_fails_when_a_project_uses_a_label(self):
        code, report = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(report['using'], ['beta'])
        self.assertEqual(report['unreadable'], [])
        self.assertEqual(report['projects']['beta']['rows'],
                         [{'id': 'beta-1', 'labels': ['open-item']}, {'id': 'beta-2', 'labels': ['open-item:open']}])
        self.assertEqual(report['projects']['alpha'], {'rows': []})
        # Read only: one list per project and nothing else.
        self.assertEqual(self.calls, [(name, ['list', '--all', '--limit', '0', '--json'])
                                      for name in ('alpha', 'beta', 'gamma')])

    def test_clean_projects_exit_zero(self):
        code, report = self.check('alpha', 'gamma')
        self.assertEqual((code, report['using'], report['unreadable']), (0, [], []))

    def test_an_unreadable_or_unknown_project_is_not_clean(self):
        del self.rows['gamma']
        code, report = self.check('alpha', 'gamma', 'nosuch', '../x')
        self.assertEqual(code, 1)
        self.assertEqual(report['unreadable'], ['../x', 'gamma', 'nosuch'])
        self.assertIn('error', report['projects']['gamma'])


    def test_damaged_metadata_is_unreadable_and_bd_never_runs(self):
        # kittrial-5bb.126 review: bd falls back to an embedded database when the metadata
        # does not record the server, CREATES .beads/embeddeddolt and lists nothing, so the
        # project read as clean although it carries the label.
        metadata = self.root / 'projects' / 'beta' / '.beads' / 'metadata.json'
        for text in ('not json', '{}', '[]', json.dumps(dict(SERVER_METADATA, dolt_server_port=None)),
                     json.dumps({key: value for key, value in SERVER_METADATA.items() if key != 'dolt_database'})):
            with self.subTest(metadata=text):
                metadata.write_text(text, encoding='utf-8')
                self.calls = []
                code, report = self.check()
                self.assertEqual(code, 1)
                self.assertEqual((report['using'], report['unreadable']), ([], ['beta']))
                self.assertIn('was not run', report['projects']['beta']['error'])
                self.assertNotIn('beta', [name for name, _ in self.calls])
                self.assertEqual([name for name, _ in self.calls], ['alpha', 'gamma'])
        self.assertFalse((self.root / 'projects' / 'beta' / '.beads' / 'embeddeddolt').exists())


class NoWriterTests(unittest.TestCase):
    """Slice 0 writes nothing: no record body, no journal file."""

    def test_only_the_guard_and_the_void_names_carry_the_prefixes(self):
        allowed = {'reserved_comments.py', 'recovery.py'}
        offenders = [(path.name, marker) for path in sorted(KIT.glob('*.py')) if path.name not in allowed
                     for marker in RECORD_MARKERS if marker in path.read_text(encoding='utf-8')]
        self.assertEqual(offenders, [])

    def test_no_kit_module_creates_either_journal(self):
        for path in sorted(KIT.glob('*.py')):
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if any(journal in line for journal in JOURNALS) or 'OWNER_ANSWERS_JOURNAL' in line \
                        or 'REQUESTS_JOURNAL' in line:
                    for verb in ('mkdir', 'write_text', 'write_bytes', 'atomic', 'open('):
                        self.assertNotIn(verb, line, '%s:%d' % (path.name, number))


if __name__ == '__main__':
    unittest.main()
