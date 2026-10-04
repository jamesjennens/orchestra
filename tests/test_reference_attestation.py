"""An attestation authority for operational facts in the reference catalog (kittrial-5bb.98).

A revision whose authority is an attestation is `Kind: reference-entry-v2`; every other
revision stays v1. It is proposed by a contributor like any draft, accepted only by the
operator route with more asked of the operator, and marked on every read. The batch
accept is the shared `AnchoredKind.apply_batch`, which `capability-apply` already used.
"""
import contextlib
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import capability_records as cr
import keyed_entries
import recovery
import reference_records as rr
import reserved_comments
from requirements import canonical_bytes, content_hash
from test_record_voids import void_payload
from test_reference_records import OPERATOR, ReferenceCase, acceptance, entry

V1, V2 = 'Kind: reference-entry-v1\n', 'Kind: reference-entry-v2\n'
KEY = 'office.server.check'
DECISION = 'decision-42'


def attestation(**extra):
    value = {'type': 'attestation', 'basis': 'host-check', 'by': 'person:james', 'observed': '2026-09-20',
             'how': 'office-server-check.sh run on the office server; output read'}
    value.update(extra)
    return {name: item for name, item in value.items() if item is not ...}


def attested(**extra):
    fields = dict(key=KEY, title='Office server check', statement='The office server check passes.',
                  authority=attestation(), owner='person:james', review_by='2027-01-15', tags=['office'])
    fields.update(extra)
    return fields


class AttestationCase(ReferenceCase):
    def setUp(self):
        super().setUp()
        self.native.seed(DECISION, issue_type='decision', status='closed')

    def entry_comments(self, task='ref-1'):
        return [comment['text'] for comment in self.native.row(task)['comments']
                if comment['text'].startswith('Kind: reference-entry-')]

    def accept(self, revision, digest, key=KEY, **extra):
        return super().accept(revision, digest, key=key, **extra)

    def get(self, key=KEY, operators=(OPERATOR,)):
        return super().get(key, operators)

    def sha(self, revision, key=KEY):
        return super().sha(revision, key)


class RecordVersionTests(AttestationCase):
    def test_an_attested_revision_is_written_as_version_2_and_everything_else_stays_version_1(self):
        self.propose(**attested())
        self.propose(operation_id='plain')
        (body,) = self.entry_comments('ref-1')
        self.assertTrue(body.startswith(V2))
        record = rr.parse_entry(body)
        self.assertEqual((record['schema_version'], record['authority']['type'], record['acceptance_state']),
                         (2, 'attestation', 'draft'))
        (plain,) = self.entry_comments('ref-2')
        self.assertTrue(plain.startswith(V1))
        self.assertEqual(rr.parse_entry(plain)['schema_version'], 1)
        # This kit reads both versions as supported records, and a v2-only row is an anchor.
        self.assertEqual(reserved_comments.record_comment_kind(body), ('reference-entry', 2, 'supported'))
        self.assertEqual(reserved_comments.record_comment_kind('Kind: reference-entry-v4\n{}')[2], 'unsupported')
        self.assertEqual(reserved_comments.record_comment_kind('Kind: capability-entry-v2\n{}')[2], 'unsupported')
        self.assertTrue(reserved_comments.is_record_anchor(self.native.row('ref-1')))

    def test_one_form_per_content_the_version_and_the_authority_must_agree(self):
        self.propose(**attested())
        (body,) = self.entry_comments()
        record = rr.parse_entry(body)
        text = canonical_bytes(record).decode('utf-8')
        # The right record under the wrong kind line, either way round.
        self.assertIsNone(rr.parse_entry(V1 + text))
        self.propose(operation_id='plain')
        (plain,) = self.entry_comments('ref-2')
        self.assertIsNone(rr.parse_entry(V2 + plain[len(V1):]))
        # A record that claims the other version for its authority fails its own schema.
        for version, authority in ((1, attestation()), (2, {'type': 'repo-path', 'path': 'src/x.py'})):
            forged = dict(record, schema_version=version, authority=authority)
            forged['sha256'] = content_hash(forged)
            with self.assertRaisesRegex(ValueError, 'schema_version 2 is the entry record with an attestation'):
                rr.validate_entry(forged)
            prefix = V2 if version == 2 else V1
            self.assertIsNone(rr.parse_entry(prefix + canonical_bytes(forged).decode('utf-8')))

    def test_an_entry_may_mix_versions_across_its_revisions(self):
        # The 37 drafts on the trial project: written with a repo-path authority, revised to
        # an attestation, and (here) back again.
        self.propose(key=KEY, authority={'type': 'repo-path', 'path': 'HANDOVER.md', 'anchor': 'office'})
        self.revise(**attested(expected_sha256=self.sha(1)))
        self.assertEqual([body[:len(V1)] for body in self.entry_comments()], [V1, V2])
        view = self.get()
        self.assertEqual((view['state'], view['proposed']['revision'], view['proposed']['authority_kind']),
                         ('draft-only', 2, 'attested'))
        self.revise(operation_id='back', key=KEY, revision=3, expected_sha256=self.sha(2))
        self.assertEqual([body[:len(V1)] for body in self.entry_comments()], [V1, V2, V1])
        self.assertEqual(self.get()['proposed']['authority_kind'], 'repository')

    def test_field_refusals_write_nothing(self):
        cases = (
            (dict(basis='coordinator-practice'), 'authority.basis must be host-check'),
            (dict(basis=...), 'authority.basis must be host-check'),
            (dict(by='session-4e40fde3'), 'authority.by must be a durable identity'),
            (dict(by='person:session-4e40'), 'not a session actor'),
            (dict(by=...), 'authority.by must be a durable identity'),
            (dict(observed='last week'), 'authority.observed must be a YYYY-MM-DD date'),
            (dict(observed='2026-10-02'), 'authority.observed must not be later than today'),
            (dict(how=''), 'authority.how'),
            (dict(how=...), 'authority.how'),
            (dict(how='one\ntwo'), 'authority.how must be one line'),
            (dict(how='x' * 301), 'authority.how must be at most 300'),
            (dict(source='a\tb'), 'authority.source must be one line'),
            (dict(commit='0' * 40), 'authority has unknown field'),
        )
        for change, message in cases:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                self.propose(**attested(authority=attestation(**change)))
        with self.assertRaisesRegex(ValueError, 'review_by must be at most 6 months after authority.observed '
                                                r'\(2027-03-20\)'):
            self.propose(**attested(review_by='2027-03-21'))
        with self.assertRaisesRegex(ValueError, 'authority.type must be repo-path, url, attestation or decision'):
            self.propose(**attested(authority={'type': 'rumour'}))
        self.assertEqual(self.native.writes(), [])
        # The edge is allowed: review on the last day of the window, and an optional source.
        self.propose(**attested(review_by='2027-03-20', authority=attestation(source='handover, 2026-09-20 entry')))


class ReaderMarkingTests(AttestationCase):
    def test_a_draft_attestation_is_marked_not_accepted_wherever_it_shows(self):
        self.propose(**attested())
        view = self.get()
        self.assertEqual((view['state'], view['record'], view['trust']), ('draft-only', None, 'draft'))
        proposed = view['proposed']
        self.assertEqual(proposed['authority_kind'], 'attested')
        self.assertEqual(proposed['authority_note'],
                         'NOT ACCEPTED. Attested by person:james on 2026-09-20 (host-check); no operator has '
                         'accepted it, so it is a lead and not authority. It cannot be checked against a repository.')
        (item,) = rr.read(['list'], self.native, [OPERATOR])['items']
        self.assertEqual((item['state'], item['authority_kind'], item['authority_accepted']),
                         ('draft-only', 'attested', False))
        self.assertTrue(item['authority_note'].startswith('NOT ACCEPTED. Attested by person:james'))
        # The free text of the attestation never enters the server-derived sentence.
        self.assertNotIn('office-server-check', item['authority_note'])

    def test_an_accepted_attestation_says_what_it_is_and_a_later_draft_beside_it_is_still_marked(self):
        self.propose(**attested())
        self.accept(1, self.sha(1))
        view = self.get()
        self.assertEqual((view['state'], view['trust'], view['record']['authority_kind']),
                         ('accepted', 'accepted', 'attested'))
        self.assertEqual(view['record']['authority_note'],
                         'Attested by person:james on 2026-09-20 (host-check), accepted by an operator. It is '
                         'provenance, not a pointer: it cannot be checked against a repository.')
        (item,) = rr.read(['list'], self.native, [OPERATOR])['items']
        self.assertEqual((item['authority_kind'], item['authority_accepted']), ('attested', True))
        self.revise(**attested(operation_id='again', revision=3, expected_sha256=self.sha(2),
                               authority=attestation(observed='2026-09-30')))
        view = self.get()
        self.assertFalse(view['record']['authority_note'].startswith('NOT ACCEPTED'))
        self.assertTrue(view['proposed']['authority_note'].startswith('NOT ACCEPTED. Attested by person:james on '
                                                                      '2026-09-30'))
        # An operator removed from the allowlist: the entry reads draft-only and says so.
        gone = self.get(operators=('someone-else',))
        self.assertEqual((gone['state'], gone['record']), ('draft-only', None))
        self.assertTrue(gone['proposed']['authority_note'].startswith('NOT ACCEPTED'))
        (item,) = rr.read(['list'], self.native, ['someone-else'])['items']
        self.assertEqual((item['authority_kind'], item['authority_accepted']), ('attested', False))

    def test_repository_and_url_entries_are_named_and_carry_no_note(self):
        self.propose(operation_id='plain')
        self.propose(operation_id='page', key='identity.registry',
                     authority={'type': 'url', 'url': 'https://example.invalid/page', 'retrieved': '2026-09-01'})
        self.propose(**attested())
        kinds = {item['key']: item['authority_kind'] for item in rr.read(['list'], self.native, [OPERATOR])['items']}
        self.assertEqual(kinds, {'calendar.trading': 'repository', 'identity.registry': 'url', KEY: 'attested'})
        self.assertNotIn('authority_note', self.get('calendar.trading')['proposed'])
        for kind, keys in (('attested', [KEY]), ('repository', ['calendar.trading']), ('url', ['identity.registry'])):
            listing = rr.read(['list', '--authority', kind], self.native, [OPERATOR])
            self.assertEqual([item['key'] for item in listing['items']], keys)
        with self.assertRaisesRegex(ValueError, '--authority must be repository, url, attested or decision'):
            rr.read(['list', '--authority', 'attestation'], self.native, [OPERATOR])
        self.assertIn('--authority repository|url|attested|decision', rr.help_payload()['usage'][1])

    def test_attention_items_carry_the_authority_kind(self):
        self.propose(**attested(review_by='2026-10-10'))
        self.accept(1, self.sha(1))
        rows = rr.read_rows(self.native)
        (item,) = rr.work_attention(rows, OPERATOR, [OPERATOR])['items']
        self.assertEqual((item['due'], item['authority_kind'], item['authority_accepted']), ('due-soon', 'attested', True))
        (brief,) = rr.brief_attention(rows, {'labels': []}, [OPERATOR])['attention']
        self.assertEqual((brief['authority_kind'], brief['text']),
                         ('attested', 'Attested reference %s is due for review by 2026-10-10.' % KEY))


class AcceptanceRuleTests(AttestationCase):
    def test_a_contributor_proposes_and_only_the_operator_route_accepts(self):
        self.propose(**attested())
        payload = {'schema_version': 1, 'operation_id': 'sneaky', 'operation': 'accept', 'key': KEY, 'revision': 1,
                   'record_sha256': self.sha(1), 'acceptance_state': 'accepted', 'acceptance': acceptance()}
        before = len(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'operation must be one of propose, revise'):
            rr.apply_native(payload, 'alice', self.native, self.project)
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.accept(1, self.sha(1), actor='alice')
        self.assertEqual(len(self.native.writes()), before)

    def test_accept_writes_evidence_then_a_version_2_revision_then_the_label(self):
        self.propose(**attested())
        self.native.calls = []
        result = self.accept(1, self.sha(1))
        self.assertEqual((result['revision'], result['state']), (2, 'accepted'))
        writes = self.native.writes()
        self.assertTrue(writes[0][3].startswith('Kind: reference-acceptance-v1\n'))
        self.assertTrue(writes[1][3].startswith(V2))
        self.assertEqual(writes[2:], [['update', 'ref-1', '--remove-label', 'reference:draft', '--json'],
                                      ['update', 'ref-1', '--add-label', 'reference:accepted', '--json']])
        # Idempotent: the same accept again reads and writes nothing.
        before = len(self.native.writes())
        self.assertTrue(self.accept(1, self.sha(1))['reconciled'])
        self.assertEqual(len(self.native.writes()), before)

    def test_the_decision_must_be_an_existing_decision_issue(self):
        self.propose(**attested())
        self.native.seed('task-9')
        self.native.seed('labelled-1', labels=['decision'])
        before = len(self.native.writes())
        for decision in ('decision-404', 'task-9'):
            with self.subTest(decision=decision), self.assertRaisesRegex(
                    ValueError, 'acceptance.decision_id must name an existing issue of type decision'):
                self.accept(1, self.sha(1), operation_id='apply-' + decision, acceptance=acceptance(decision_id=decision))
        # An id that is not an issue id never becomes a native argument: it is refused by its
        # shape, before the lookup.
        for number, decision in enumerate(('--all', 'a b', '-', 'x' * 162)):
            lookups = [call for call in self.native.calls if '--id' in call]
            with self.subTest(decision=decision[:8]), self.assertRaisesRegex(
                    ValueError, 'acceptance.decision_id must be a native decision issue id'):
                self.accept(1, self.sha(1), operation_id='apply-shape-%d' % number,
                            acceptance=acceptance(decision_id=decision))
            self.assertEqual([call for call in self.native.calls if '--id' in call], lookups)
        self.assertEqual(len(self.native.writes()), before)
        self.accept(1, self.sha(1), acceptance=acceptance(decision_id='labelled-1'))
        # A repository entry keeps today's rule: its decision id is not looked up.
        self.propose(operation_id='plain')
        self.assertEqual(self.accept(1, self.sha(1, 'calendar.trading'), key='calendar.trading',
                                     operation_id='apply-plain',
                                     acceptance=acceptance(decision_id='decision-404'))['state'], 'accepted')

    def test_an_owner_statement_needs_its_author_among_the_acceptance_owners(self):
        self.propose(**attested(authority=attestation(basis='owner-statement', how='said in the planning thread')))
        before = len(self.native.writes())
        with self.assertRaisesRegex(ValueError, r'the person who made the statement \(authority.by\) among '
                                                'acceptance.owners'):
            self.accept(1, self.sha(1))
        self.assertEqual(len(self.native.writes()), before)
        owners = acceptance(owners=['account:u-0001', 'person:james'], approvers=['person:james'])
        self.assertEqual(self.accept(1, self.sha(1), operation_id='apply-owner', acceptance=owners)['state'],
                         'accepted')
        # A host check is not the owner's statement: its `by` need not be an owner.
        self.propose(**attested(operation_id='host', key='office.other'))
        self.assertEqual(self.accept(1, self.sha(1, 'office.other'), key='office.other',
                                     operation_id='apply-host')['state'], 'accepted')

    def test_the_operator_evidence_is_one_line(self):
        self.propose(**attested())
        for evidence in ('re-ran the check\nand it passed', 'x' * 2001):
            with self.assertRaisesRegex(ValueError, 'acceptance.evidence for an attested entry must be one line'):
                self.accept(1, self.sha(1), acceptance=acceptance(evidence=evidence))
        with self.assertRaises(ValueError):
            self.accept(1, self.sha(1), acceptance=acceptance(evidence=''))

    def test_an_observation_older_than_the_window_is_not_accepted(self):
        # Whatever review date such a draft carries is in the past too; the refusal names
        # the cause, the stale observation.
        self.propose(**attested(authority=attestation(observed='2026-03-31'), review_by='2026-09-30'))
        with self.assertRaisesRegex(ValueError, 'authority.observed 2026-03-31 is more than 6 months old; check '
                                                'the fact again'):
            self.accept(1, self.sha(1))
        with patch('time.gmtime', return_value=time.struct_time((2026, 9, 29, 12, 0, 0, 1, 272, 0))):
            self.assertEqual(self.accept(1, self.sha(1), operation_id='in-time')['state'], 'accepted')
        # The common case: a stale draft with no review date. The operator is told to check
        # the fact again, not only that a review date is missing (review of 28c6d95).
        self.propose(**attested(operation_id='stale', key='office.stale', review_by=None,
                                authority=attestation(observed='2026-01-10')))
        with self.assertRaisesRegex(ValueError, 'authority.observed 2026-01-10 is more than 6 months old; check '
                                                'the fact again'):
            self.accept(1, self.sha(1, 'office.stale'), key='office.stale', operation_id='apply-stale')
        # A fresh observation with no review date still gets the schema's own message.
        self.propose(**attested(operation_id='undated', key='office.undated', review_by=None))
        with self.assertRaisesRegex(ValueError, 'an accepted revision needs review_by'):
            self.accept(1, self.sha(1, 'office.undated'), key='office.undated', operation_id='apply-undated')

    def test_today_is_the_utc_date_and_the_refusal_says_so(self):
        with self.assertRaisesRegex(ValueError, r'authority.observed must not be later than today \(the UTC date, '
                                                r'2026-10-01\)'):
            self.propose(**attested(authority=attestation(observed='2026-10-02')))
        with self.assertRaisesRegex(ValueError, r'authority.retrieved must not be later than today \(the UTC date, '
                                                r'2026-10-01\)'):
            self.propose(operation_id='page', key='identity.registry', authority={
                'type': 'url', 'url': 'https://example.invalid/page', 'retrieved': '2026-10-02'})

    def test_an_accepted_attestation_past_its_review_date_says_so_in_its_note(self):
        self.propose(**attested(review_by='2026-10-10'))
        self.accept(1, self.sha(1))
        self.assertNotIn('PAST ITS REVIEW DATE', self.get()['record']['authority_note'])
        with patch('time.gmtime', return_value=time.struct_time((2026, 10, 11, 12, 0, 0, 6, 284, 0))):
            view = self.get()
            self.assertEqual((view['state'], view['due']), ('accepted', 'expired'))
            self.assertEqual(view['record']['authority_note'],
                             'Attested by person:james on 2026-09-20 (host-check), accepted by an operator, and '
                             'PAST ITS REVIEW DATE (2026-10-10): check the fact again before relying on it. It is '
                             'provenance, not a pointer: it cannot be checked against a repository.')
            (item,) = rr.read(['list'], self.native, [OPERATOR])['items']
            self.assertIn('PAST ITS REVIEW DATE (2026-10-10)', item['authority_note'])
            self.assertIn('PAST ITS REVIEW DATE', rr.read(['find', KEY], self.native, [OPERATOR])['records'][0][
                'authority_note'])

    def test_a_direct_accepted_attestation_is_checked_before_its_anchor_is_created(self):
        self.native.actor = OPERATOR
        payload = dict(entry(operation='draft', operation_id='direct', **attested()), acceptance_state='accepted',
                       acceptance=acceptance(decision_id='decision-404'))
        with self.assertRaisesRegex(ValueError, 'must name an existing issue of type decision'):
            rr.apply_native(payload, OPERATOR, self.native, self.project, operator=True, operators=[OPERATOR])
        self.assertEqual(self.native.writes(), [])
        self.assertEqual(list(self.project.glob('.reference-requests/*.json')), [])
        payload['acceptance'] = acceptance()
        result = rr.apply_native(payload, OPERATOR, self.native, self.project, operator=True, operators=[OPERATOR])
        self.assertEqual((result['revision'], result['state'], result['created']), (1, 'accepted', True))
        self.assertTrue(self.entry_comments()[0].startswith(V2))


class VoidTests(AttestationCase):
    def void(self, payload):
        self.native.actor = OPERATOR
        return rr.KIND.apply_void(payload, OPERATOR, self.native, [OPERATOR])

    def test_a_malformed_version_2_record_can_be_voided_and_a_well_formed_one_cannot(self):
        self.propose(**attested())
        good = next(comment for comment in self.native.row('ref-1')['comments'] if comment['text'].startswith(V2))
        with self.assertRaisesRegex(ValueError, 'is a well-formed reference-entry record the entry reads'):
            self.void(void_payload('ref-1', good['id'], good['text']))
        bad = self.native.add_comment('ref-1', V2 + '{"not": "valid"}', author='mallory')
        self.assertEqual(self.get()['state'], 'malformed')
        self.void(void_payload('ref-1', bad['id'], bad['text'], operation_id='void-2'))
        self.assertEqual(self.get()['state'], 'draft-only')
        self.assertTrue(recovery.claims_kind(V2 + '{}', 'reference-entry'))
        self.assertFalse(recovery.claims_kind('Kind: reference-entry-v4\n{}', 'reference-entry'))
        self.assertFalse(recovery.claims_kind('Kind: capability-entry-v2\n{}', 'capability-entry'))


class BatchTests(AttestationCase):
    def batch(self, items, operation_id='batch-1', actor=OPERATOR, operators=(OPERATOR,), lock=None, **extra):
        self.native.actor = actor
        payload = {'schema_version': 1, 'operation_id': operation_id, 'items': items,
                   'acceptance_state': 'accepted', 'acceptance': acceptance()}
        payload.update(extra)
        return rr.apply_batch(payload, actor, self.native, self.project, operators=list(operators), lock=lock)

    def item(self, key, revision=1):
        return {'key': key, 'revision': revision, 'record_sha256': self.sha(revision, key)}

    def seed_three(self):
        self.propose(**attested())
        self.propose(operation_id='plain')
        self.propose(**attested(operation_id='stmt', key='owner.instruction.proceed',
                                authority=attestation(basis='owner-statement', by='person:pat',
                                                      how='said in the planning thread')))

    def test_a_batch_accepts_each_item_on_its_own_and_one_refusal_does_not_stop_the_others(self):
        self.seed_three()
        items = [self.item(KEY), self.item('owner.instruction.proceed'), self.item('calendar.trading')]
        result = self.batch(items)
        self.assertEqual([(item['key'], item['result']) for item in result['items']],
                         [(KEY, 'accepted'), ('owner.instruction.proceed', 'refused'),
                          ('calendar.trading', 'accepted')])
        self.assertIn('among acceptance.owners', result['items'][1]['reason'])
        self.assertEqual((result['complete'], result['stopped'], result['accepted'], result['refused'],
                          result['decision_id']), (False, False, 2, 1, DECISION))
        self.assertEqual([self.get(key)['state'] for key in (KEY, 'owner.instruction.proceed', 'calendar.trading')],
                         ['accepted', 'draft-only', 'accepted'])
        # A retry of the same batch writes nothing for the accepted items.
        before = len(self.native.writes())
        again = self.batch(items)
        self.assertEqual([item['result'] for item in again['items']], ['already-accepted', 'refused',
                                                                       'already-accepted'])
        self.assertEqual(len(self.native.writes()), before)
        # Each item has its own receipt, keyed (operation id, key), beside the batch receipt.
        receipts = [keyed_entries.load_json(path) for path in self.project.glob('.reference-requests/*.json')]
        self.assertEqual(sorted(receipt.get('operation') for receipt in receipts),
                         ['accept', 'accept', 'apply-batch', 'propose', 'propose', 'propose'])

    def test_batch_refusals_before_any_item(self):
        self.seed_three()
        items = [self.item(KEY)]
        before = len(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.batch(items, actor='alice')
        for change, message in ((dict(items=[]), 'items must be a list of 1..100'),
                                (dict(items=items * 2), 'items must not repeat a key'),
                                (dict(items=[dict(items[0], labels=['x'])]), 'unknown field'),
                                (dict(acceptance_state='draft'), 'reference-apply writes accepted revisions'),
                                (dict(operation='accept'), 'reference-apply payload has unknown field')):
            with self.subTest(change=sorted(change)), self.assertRaisesRegex(ValueError, message):
                self.batch(change.pop('items', items), **change)
        self.assertEqual(len(self.native.writes()), before)
        self.batch(items)
        with self.assertRaisesRegex(ValueError, 'Operation ID already used for a different batch'):
            self.batch([self.item('calendar.trading')])

    def test_the_lock_is_taken_once_per_item_and_released_between_items(self):
        self.seed_three()
        events = []

        @contextlib.contextmanager
        def lock():
            events.append('lock')
            before = len(self.native.writes())
            try:
                yield
            finally:
                events.append('unlock after %d write(s)' % (len(self.native.writes()) - before))

        with patch.object(keyed_entries.time, 'sleep') as sleep:
            result = self.batch([self.item(KEY), self.item('calendar.trading')], lock=lock)
        self.assertEqual((result['complete'], result['accepted'], result['refused']), (True, 2, 0))
        # The batch receipt, each item, the batch receipt again: four holds, never nested.
        self.assertEqual(events, ['lock', 'unlock after 0 write(s)', 'lock', 'unlock after 4 write(s)',
                                  'lock', 'unlock after 4 write(s)', 'lock', 'unlock after 0 write(s)'])
        sleep.assert_called_once_with(keyed_entries.BATCH_YIELD_SECONDS)

    def test_the_capability_batch_is_the_same_shared_batch(self):
        self.assertEqual((cr.BATCH_MAX, cr.BATCH_YIELD_SECONDS), (keyed_entries.BATCH_MAX,
                                                                  keyed_entries.BATCH_YIELD_SECONDS))
        with self.assertRaisesRegex(ValueError, 'capability-apply payload must be an object'):
            cr.validate_batch([])
        with self.assertRaisesRegex(ValueError, 'reference-apply payload must be an object'):
            rr.KIND.validate_batch([])


if __name__ == '__main__':
    unittest.main()
