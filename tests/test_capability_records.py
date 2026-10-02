"""Capability records, .60 slice 1a (kittrial-5bb.67; docs/CAPABILITY_INDEX_DESIGN.md).

Runs over the reference tests' fake bd (exact `list --label`, `show --include-comments`,
comment author = the acting actor). Rollback: the anchors this writer creates satisfy
the unchanged `is_record_anchor`, and its receipts pass the frozen receipt schema.
"""
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import admin
import capability_records as cr
import requirement_records
import reserved_comments
from requirements import content_hash
from test_reference_records import OPERATOR, TODAY, RefNative, acceptance

ALIAS = 'Kind: capability-alias-v1\n'


def entry(**extra):
    payload = {'schema_version': 1, 'operation_id': 'cap-1', 'operation': 'propose',
               'key': 'review.structured-contribution', 'name': 'Structured contribution review',
               'aliases': ['review workflow'], 'summary': 'Contribute, request changes, respond and approve.',
               'requirements': [], 'anchors': ['docs/REVIEWS.md#structured-reviews'],
               'code': ['review_workflow.py::execute'], 'tests': ['tests/test_review_workflow.py'],
               'owner': 'person:james', 'tags': ['review'], 'revision': 1, 'expected_sha256': None}
    payload.update(extra)
    return {name: value for name, value in payload.items() if value is not ...}


class CapabilityNative(RefNative):
    """The reference fake plus a failure aimed at one anchor's acceptance evidence."""

    def __init__(self):
        super().__init__()
        self.fail_evidence_on = None

    def __call__(self, args):
        if self.fail_evidence_on and args[:2] == ['comments', 'add'] and args[2] == self.fail_evidence_on \
                and args[3].startswith('Kind: capability-acceptance-v1'):
            self.calls.append(list(args))
            raise ValueError('native comment failed')
        return super().__call__(args)


class CapabilityCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.native = CapabilityNative()
        clock = patch('time.gmtime', return_value=TODAY)
        clock.start()
        self.addCleanup(clock.stop)

    def propose(self, actor='alice', **extra):
        self.native.actor = actor
        return cr.apply_native(entry(**extra), actor, self.native, self.project)

    def sha(self, revision, key='review.structured-contribution'):
        row, _ = cr.anchor_for(cr.read_rows(self.native), key)
        return cr.existing_revisions(row)[revision]['sha256']

    def batch(self, items, operation_id='batch-1', actor=OPERATOR, operators=(OPERATOR,), decision='decision-7'):
        self.native.actor = actor
        payload = {'schema_version': 1, 'operation_id': operation_id, 'items': items,
                   'acceptance_state': 'accepted', 'acceptance': acceptance(decision_id=decision)}
        return cr.apply_batch(payload, actor, self.native, self.project, operators=list(operators))

    def item(self, key='review.structured-contribution', revision=1):
        return {'key': key, 'revision': revision, 'record_sha256': self.sha(revision, key)}

    def read(self, *args, operators=(OPERATOR,)):
        return cr.read(list(args), self.native, list(operators))

    def alias(self, key, phrase, actor='alice', operators=(OPERATOR,), evidence=None):
        self.native.actor = actor
        return cr.propose_alias(key, phrase, actor, self.native, list(operators), evidence=evidence)


class RecordTests(CapabilityCase):
    def test_propose_creates_a_closed_capability_anchor_and_a_draft(self):
        result = self.propose()
        row = self.native.row(result['native_id'])
        self.assertEqual(row['status'], 'closed')
        self.assertTrue({'capability', 'capability:draft', 'capability-key:review-structured-contribution'}
                        <= set(row['labels']))
        self.assertTrue(row['comments'][0]['text'].startswith('Kind: capability-entry-v1\n'))
        self.assertTrue(reserved_comments.is_record_anchor(row))
        self.assertEqual(reserved_comments.hide_records([row]), [])
        files = {'.capability-requests/' + path.name: json.loads(path.read_text(encoding='utf-8'))
                 for path in (self.project / '.capability-requests').glob('*.json')}
        admin.validate_coordination_files(files)
        view = self.read('get', 'review.structured-contribution')
        self.assertEqual((view['state'], view['trust'], view['proposed']['revision']), ('draft-only', 'draft', 1))
        self.assertEqual(view['proposed']['name']['text'], 'Structured contribution review')

    def test_fields_follow_the_closed_schema_and_pointer_rules(self):
        cases = [
            ({'code': ['../outside.py::x']}, 'invalid pointer'),
            ({'code': ['review_workflow.py']}, 'file::Qualified.name'),
            ({'anchors': ['docs/REVIEWS.md']}, 'file.md#anchor'),
            ({'tests': ['tests/x.py#heading']}, 'invalid pointer'),
            ({'code': ['a.py::f%d' % index for index in range(33)]}, 'at most 32'),
            ({'name': 'x' * 121}, 'at most 120'),
            ({'summary': 'x' * 1201}, 'at most 1200'),
            ({'aliases': ['a\tb']}, 'printable'),
            ({'aliases': ['Same', 'same']}, 'repeat'),
            ({'tags': ['x'] * 2}, 'unique'),
            ({'owner': 'person:session-4e40fde3'}, 'not a session actor'),
            ({'requirements': [{'key': 'R01'}]}, 'Unknown requirement link'),
            ({'requirements': [{'key': 'R01', 'extra': 1}]}, 'requirements entries'),
            ({'acceptance_state': 'accepted'}, 'never caller-supplied'),
            ({'labels': ['capability:accepted']}, 'Labels are controlled'),
            ({'verified_at': 'x'}, 'unknown field'),
            ({'status': 'ok'}, 'unknown field'),
        ]
        for extra, message in cases:
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, message):
                self.propose(**extra)
        self.assertEqual(self.native.writes(), [])
        # A draft needs no owner.
        self.assertEqual(self.propose(owner=None, operation_id='no-owner', key='a.no-owner')['state'], 'draft')

    def test_requirement_links_must_name_an_existing_requirement_revision(self):
        payload = {'kind': 'requirement', 'title': 'R01: Intent', 'key': 'R01', 'description': 'Statement.',
                   'acceptance_state': 'draft'}
        record = requirement_records.requirement_record(payload, 'req-9', 1)
        from export_requirements import revision_comment
        self.native.seed('req-9', labels=['requirement', 'requirement:draft'],
                         comments=[{'id': 'r1', 'text': revision_comment(record), 'author': 'alice'}])
        self.assertEqual(self.propose(requirements=[{'key': 'R01', 'revision': 1}])['state'], 'draft')
        with self.assertRaisesRegex(ValueError, 'Unknown requirement link.*R01'):
            self.propose(operation_id='cap-2', key='other.thing', requirements=[{'key': 'R01', 'revision': 2}])

    def reads(self):
        return [(call[0], len([token for token in call[1:] if not token.startswith('--')])
                 if call[0] == 'show' else 1)
                for call in self.native.calls if call[0] in ('list', 'show', 'export')]

    def test_reads_cost_what_they_touch_whatever_the_index_size(self):
        # The .66 review (01a0fbfd) found whole-catalog reads on get and on every write;
        # the capability paths share the layer, so they are pinned the same way.
        for index in range(240):
            key = 'bulk.k%03d' % index
            record = cr.entry_record(entry(key=key, name='Bulk %d' % index, aliases=[]), 1, 'draft')
            self.native.seed('bulk-%d' % index, labels=['capability', 'capability:draft', cr.key_label(key)],
                             status='closed', comments=[{'id': 'b%d' % index, 'text': cr.entry_comment(record),
                                                         'author': 'alice'}])
        self.native.calls = []
        self.assertEqual(self.read('get', 'bulk.k150')['state'], 'draft-only')
        # Its own key, then the (empty) list of retired keys for `replaces`.
        self.assertEqual(self.reads(), [('list', 1), ('show', 1), ('list', 1)])
        self.native.calls = []
        self.propose()
        self.assertEqual(self.reads(), [('list', 1), ('show', 1)])
        self.native.calls = []
        self.assertTrue(self.read('find', 'Bulk 7')['found'])
        self.assertEqual([call[0] for call in self.native.calls], ['list', 'export'])
        self.native.calls = []
        self.alias('bulk.k007', 'a phrase')
        self.assertEqual([call[0] for call in self.native.calls], ['list', 'export', 'comments'])

    def test_revise_is_compare_and_swap(self):
        self.propose()
        with self.assertRaisesRegex(ValueError, 'expected_sha256 does not match'):
            self.native.actor = 'alice'
            cr.apply_native(entry(operation='revise', operation_id='cap-r', revision=2, expected_sha256='f' * 64),
                            'alice', self.native, self.project)
        result = cr.apply_native(entry(operation='revise', operation_id='cap-r', revision=2,
                                       expected_sha256=self.sha(1), summary='Revised.'),
                                 'alice', self.native, self.project)
        self.assertEqual((result['revision'], result['state']), (2, 'draft'))


class BatchAcceptanceTests(CapabilityCase):
    def setUp(self):
        super().setUp()
        for index, key in enumerate(('a.one', 'a.two', 'a.three')):
            self.propose(operation_id='p-%d' % index, key=key, name='Thing %d' % index, aliases=[])

    def test_one_decision_accepts_a_batch_with_one_record_and_receipt_per_item(self):
        items = [self.item(key) for key in ('a.one', 'a.two', 'a.three')]
        self.native.calls = []
        result = self.batch(items)
        self.assertTrue(result['complete'])
        self.assertEqual([(item['key'], item['result'], item['revision']) for item in result['items']],
                         [('a.one', 'accepted', 2), ('a.two', 'accepted', 2), ('a.three', 'accepted', 2)])
        # One catalog read for the whole batch.
        reads = [call[0] for call in self.native.calls if call[0] in ('list', 'show')]
        self.assertEqual(reads, ['list', 'show'])
        for key in ('a.one', 'a.two', 'a.three'):
            view = self.read('get', key)
            self.assertEqual((view['state'], view['acceptance']['decision_id'], view['acceptance']['operator']),
                             ('accepted', 'decision-7', OPERATOR))
        receipts = list((self.project / '.capability-requests').glob('*.json'))
        operations = {json.loads(path.read_text(encoding='utf-8')).get('operation') for path in receipts}
        self.assertIn('apply-batch', operations)
        admin.validate_coordination_files({'.capability-requests/' + path.name:
                                           json.loads(path.read_text(encoding='utf-8')) for path in receipts})

    def test_an_uncertain_item_stops_the_batch_and_a_retry_resumes_it(self):
        items = [self.item(key) for key in ('a.one', 'a.two', 'a.three')]
        self.native.fail_evidence_on = cr.anchor_for(cr.read_rows(self.native), 'a.two')[0]['id']
        first = self.batch(items)
        self.assertEqual([item['result'] for item in first['items']], ['accepted', 'uncertain', 'not-run'])
        self.assertFalse(first['complete'])
        self.assertIn('capability-reconcile', first['items'][1]['reason'])
        self.native.fail_evidence_on = None
        self.native.calls = []
        again = self.batch(items)
        self.assertEqual([item['result'] for item in again['items']], ['already-accepted', 'accepted', 'accepted'])
        self.assertTrue(again['complete'])
        # The first item was not touched again.
        anchor_one = cr.anchor_for(cr.read_rows(self.native), 'a.one')[0]['id']
        self.assertFalse([call for call in self.native.writes() if anchor_one in call])

    def test_a_changed_list_a_stale_item_and_a_non_operator_are_refused(self):
        items = [self.item(key) for key in ('a.one', 'a.two')]
        self.batch(items)
        with self.assertRaisesRegex(ValueError, 'different batch'):
            self.batch(items + [self.item('a.three')])
        self.native.actor = 'alice'
        reviewed = self.item('a.three')
        cr.apply_native(entry(operation='revise', operation_id='newer', key='a.three', name='Thing 2', aliases=[],
                              revision=2, expected_sha256=self.sha(1, 'a.three')), 'alice', self.native, self.project)
        stale = self.batch([{'key': 'a.two', 'revision': 2, 'record_sha256': 'f' * 64}, reviewed,
                            self.item('a.one', 1)], operation_id='batch-2')
        self.assertEqual([item['result'] for item in stale['items']], ['refused', 'refused', 'already-accepted'])
        self.assertIn('record_sha256 does not match', stale['items'][0]['reason'])
        self.assertIn('not the newest', stale['items'][1]['reason'])
        before = len(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.batch([self.item('a.three', 2)], operation_id='batch-3', actor='mallory')
        self.assertEqual(len(self.native.writes()), before)
        for bad, message in (([], '1..100'), ([{'key': 'a.one'}], 'needs key, revision'),
                             ([self.item('a.three', 2), self.item('a.three', 2)], 'repeat')):
            with self.assertRaisesRegex(ValueError, message):
                self.batch(bad, operation_id='batch-x')

    def test_a_refused_item_does_not_stop_the_batch_and_a_rerun_writes_nothing(self):
        mixed = self.batch([{'key': 'a.one', 'revision': 1, 'record_sha256': 'f' * 64}, self.item('a.two'),
                            self.item('a.three')], operation_id='mixed')
        self.assertEqual([item['result'] for item in mixed['items']], ['refused', 'accepted', 'accepted'])
        self.assertTrue(mixed['complete'])   # only an uncertain write leaves a batch incomplete
        self.assertEqual(self.read('get', 'a.one')['state'], 'draft-only')
        # An identical re-run: the accepted items are reported from their receipts, with no
        # native write and no native read of their anchors beyond the one catalog snapshot.
        self.native.calls = []
        again = self.batch([{'key': 'a.one', 'revision': 1, 'record_sha256': 'f' * 64}, self.item('a.two'),
                            self.item('a.three')], operation_id='mixed')
        self.assertEqual([item['result'] for item in again['items']],
                         ['refused', 'already-accepted', 'already-accepted'])
        self.assertEqual(self.native.writes(), [])

    def test_reaccepting_an_accepted_revision_writes_a_new_revision_and_evidence(self):
        self.batch([self.item('a.one')])
        first = self.read('get', 'a.one')
        self.native.calls = []
        again = self.batch([self.item('a.one', 2)], operation_id='batch-again', decision='decision-8')
        self.assertEqual([(item['result'], item['revision']) for item in again['items']], [('accepted', 3)])
        writes = self.native.writes()
        self.assertTrue(writes[0][3].startswith('Kind: capability-acceptance-v1\n'))
        self.assertTrue(writes[1][3].startswith('Kind: capability-entry-v1\n'))
        view = self.read('get', 'a.one')
        self.assertEqual((view['record']['revision'], view['acceptance']['decision_id']), (3, 'decision-8'))
        self.assertNotEqual(view['record']['sha256'], first['record']['sha256'])
        self.assertEqual(view['record']['name'], first['record']['name'])

    def test_direct_accepted_revision_1_needs_an_owner(self):
        self.native.actor = OPERATOR
        payload = dict(entry(operation='draft', operation_id='direct', key='a.direct', owner=None),
                       acceptance_state='accepted', acceptance=acceptance())
        with self.assertRaisesRegex(ValueError, 'needs an owner'):
            cr.apply_native(payload, OPERATOR, self.native, self.project, operator=True, operators=[OPERATOR])
        result = cr.apply_native(dict(payload, owner='account:u-1'), OPERATOR, self.native, self.project,
                                 operator=True, operators=[OPERATOR])
        self.assertEqual((result['revision'], result['state']), (1, 'accepted'))


class RetireTests(CapabilityCase):
    def retire(self, key, successor, revision, operation_id='retire-1'):
        self.native.actor = OPERATOR
        payload = {'schema_version': 1, 'operation_id': operation_id, 'operation': 'retire', 'key': key,
                   'revision': revision, 'record_sha256': self.sha(revision, key), 'successor': successor,
                   'acceptance_state': 'superseded', 'acceptance': acceptance()}
        return cr.apply_native(payload, OPERATOR, self.native, self.project, operator=True, operators=[OPERATOR])

    def test_retire_supersedes_with_a_successor_and_replaces_is_derived(self):
        self.propose(key='a.old', name='Old')
        self.propose(operation_id='p2', key='a.new', name='New')
        with self.assertRaisesRegex(ValueError, 'Unknown successor'):
            self.retire('a.old', 'a.missing', 1)
        result = self.retire('a.old', 'a.new', 1)
        self.assertEqual((result['revision'], result['state']), (2, 'superseded'))
        old = self.read('get', 'a.old')
        self.assertEqual((old['state'], old['resolved']), ('superseded', 'a.new'))
        self.assertEqual(self.read('get', 'a.new')['replaces'], ['a.old'])
        self.assertIn('capability:superseded', self.native.row(old['native_id'])['labels'])
        with self.assertRaisesRegex(ValueError, 'retired'):
            self.native.actor = 'alice'
            cr.apply_native(entry(operation='revise', operation_id='late', key='a.old', name='Old', revision=3,
                                  expected_sha256=self.sha(2, 'a.old')), 'alice', self.native, self.project)
        with self.assertRaisesRegex(ValueError, 'retirement cycle'):
            self.retire('a.new', 'a.old', 1, operation_id='retire-2')
        with self.assertRaisesRegex(ValueError, 'operation must be one of propose, revise'):
            cr.apply_native(dict(entry(operation='retire', key='a.new')), 'alice', self.native, self.project)


class AliasTests(CapabilityCase):
    def setUp(self):
        super().setUp()
        self.propose()
        self.propose(operation_id='p2', key='merge.slot', name='Merge slot', aliases=['integration lock'],
                     summary='One integrator at a time.', code=['coordination.py::merge_acquire'], tags=[])

    def test_a_pending_alias_is_a_candidate_never_an_exact_match(self):
        result = self.alias('review.structured-contribution', 'reserved label guard')
        self.assertEqual((result['state'], result['identity']), ('proposed', 'unverified'))
        found = self.read('find', 'reserved label guard')
        self.assertFalse(found['found'])
        self.assertEqual(found['candidates'][0]['key'], 'review.structured-contribution')
        pending = found['candidates'][0]['aliases_pending'][0]
        self.assertEqual((pending['alias_state'], pending['submitter']['identity']), ('proposed', 'unverified'))
        self.assertIn('propose-alias', self.read('find', 'nothing like it at all')['hint'])

    def test_exact_matches_come_from_keys_names_and_accepted_aliases_and_drafts_are_trust_marked(self):
        for phrase in ('merge.slot', 'Merge slot', 'merge-slot'):
            found = self.read('find', phrase)
            self.assertTrue(found['found'], phrase)
            self.assertEqual((found['records'][0]['key'], found['records'][0]['trust']), ('merge.slot', 'draft'))
        # A draft's own alias is a candidate until the revision carrying it is accepted.
        self.assertFalse(self.read('find', 'integration lock')['found'])
        self.batch([self.item('merge.slot')])
        found = self.read('find', 'integration lock')
        self.assertEqual((found['found'], found['records'][0]['trust']), (True, 'accepted'))
        for bad in ('x' * 201, ''):
            with self.assertRaises(ValueError):
                self.read('find', bad)
        with self.assertRaisesRegex(ValueError, '1..20'):
            self.read('find', 'merge', '--limit', '21')

    def test_caps_are_keyed_on_the_person_read_from_the_native_author(self):
        self.alias('review.structured-contribution', 'first phrase', actor='alice')
        with self.assertRaisesRegex(ValueError, 'shared pool for unverified proposers'):
            self.alias('review.structured-contribution', 'second phrase', actor='bob')
        for index in range(3):
            self.assertEqual(self.alias('review.structured-contribution', 'operator phrase %d' % index,
                                        actor=OPERATOR)['identity'], 'verified')
        with self.assertRaisesRegex(ValueError, '3 per capability'):
            self.alias('review.structured-contribution', 'operator phrase 9', actor=OPERATOR)
        with patch.object(cr, 'CAP_UNVERIFIED_PROJECT', 2):
            self.alias('merge.slot', 'slot phrase', actor='carol')
            self.propose(operation_id='p3', key='third.thing', name='Third')
            with self.assertRaisesRegex(ValueError, 'per project'):
                self.alias('third.thing', 'another phrase', actor='dave')
        # A forged "verified" submitter written by a non-operator still counts as unverified.
        anchor = cr.anchor_for(cr.read_rows(self.native), 'merge.slot')[0]['id']
        record, body = cr.alias_record('merge.slot', 'forged phrase', 'propose', OPERATOR, [OPERATOR])
        self.native.add_comment(anchor, body, author='mallory')
        view = self.read('get', 'merge.slot')
        forged = [item for item in view['aliases_pending'] if item['alias']['text'] == 'forged phrase']
        self.assertEqual(forged[0]['submitter'], {'person': None, 'identity': 'unverified'})

    def test_collisions_idempotency_rejection_and_folding(self):
        with self.assertRaisesRegex(ValueError, 'collides'):
            self.alias('review.structured-contribution', 'merge slot')
        # A draft's alias is not protected; once accepted it is (.60 section 6).
        self.alias('review.structured-contribution', 'integration lock', actor=OPERATOR)
        self.batch([self.item('merge.slot')], operation_id='accept-merge')
        with self.assertRaisesRegex(ValueError, 'collides'):
            self.alias('review.structured-contribution', 'integration lock')
        with self.assertRaisesRegex(ValueError, 'already carries that phrase'):
            self.alias('review.structured-contribution', 'review workflow')
        first = self.alias('review.structured-contribution', 'reserved label guard')
        again = self.alias('review.structured-contribution', 'Reserved  label guard')
        self.assertEqual((again['reconciled'], again['comment_id']), (True, first['comment_id']))
        self.native.actor = OPERATOR
        rejected = cr.reject_alias({'schema_version': 1, 'key': 'review.structured-contribution',
                                    'alias': 'reserved label guard', 'reason': 'too generic'},
                                   OPERATOR, self.native, self.project, operators=[OPERATOR])
        self.assertEqual(rejected['state'], 'rejected')
        self.assertEqual([item['alias']['text'] for item in
                          self.read('get', 'review.structured-contribution')['aliases_pending']], ['integration lock'])
        self.assertNotIn('review.structured-contribution',
                         [item['key'] for item in self.read('find', 'reserved label guard')['candidates']
                          if item['score'] >= 0.95])
        with self.assertRaisesRegex(ValueError, 'rejected'):
            self.alias('review.structured-contribution', 'reserved label guard')
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            cr.reject_alias({'schema_version': 1, 'key': 'merge.slot', 'alias': 'x', 'reason': 'r'}, 'alice',
                            self.native, self.project, operators=[OPERATOR])
        # Folding: the next accepted revision carries the alias; it then matches exactly.
        self.alias('merge.slot', 'single integrator')
        self.native.actor = 'alice'
        cr.apply_native(entry(operation='revise', operation_id='fold', key='merge.slot', name='Merge slot',
                              aliases=['integration lock', 'single integrator'], summary='One integrator at a time.',
                              code=['coordination.py::merge_acquire'], tags=[], revision=3,
                              expected_sha256=self.sha(2, 'merge.slot')), 'alice', self.native, self.project)
        self.batch([self.item('merge.slot', 3)], operation_id='fold-accept')
        found = self.read('find', 'single integrator')
        self.assertTrue(found['found'])
        self.assertEqual(self.read('get', 'merge.slot')['aliases_pending'], [])

    def test_alias_text_and_targets_are_bounded(self):
        for phrase, message in (('x' * 81, 'at most 80'), ('a b', 'printable'), ('...', 'at least one word')):
            with self.assertRaisesRegex(ValueError, message):
                self.alias('merge.slot', phrase)
        with self.assertRaisesRegex(ValueError, 'Unknown capability key'):
            self.alias('no.such', 'phrase')
        with self.assertRaisesRegex(ValueError, 'invalid pointer'):
            self.alias('merge.slot', 'good phrase', evidence='../x.py')


class ListAndIsolationTests(CapabilityCase):
    def test_list_filters_and_a_malformed_entry_fails_only_itself(self):
        self.propose()
        self.propose(operation_id='p2', key='merge.slot', name='Merge slot', tags=['coordination'],
                     owner='person:alex')
        self.batch([self.item('merge.slot')])
        listing = self.read('list')
        self.assertEqual([(item['key'], item['state'], item['trust']) for item in listing['items']],
                         [('merge.slot', 'accepted', 'accepted'),
                          ('review.structured-contribution', 'draft-only', 'draft')])
        self.assertEqual(self.read('list', '--state', 'accepted')['total'], 1)
        self.assertEqual(self.read('list', '--tag', 'review')['items'][0]['key'], 'review.structured-contribution')
        self.assertEqual(self.read('list', '--owner', 'person:alex')['total'], 1)
        anchor = cr.anchor_for(cr.read_rows(self.native), 'review.structured-contribution')[0]['id']
        self.native.add_comment(anchor, 'Kind: capability-entry-v1\n{"bad": 1}')
        listing = self.read('list')
        self.assertEqual([item['key'] for item in listing['items']], ['merge.slot'])
        self.assertIn('1 entry skipped as malformed', listing['coverage'])
        self.assertEqual(self.read('get', 'review.structured-contribution')['state'], 'malformed')
        self.assertTrue(self.read('find', 'merge slot')['found'])
        self.assertEqual(self.read('--help')['action'], 'capability')


if __name__ == '__main__':
    unittest.main()
