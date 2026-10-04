"""A reference authority that points at a decision issue (kittrial-5bb.104).

`{"type": "decision", "id": ISSUE}` is for a rule the project set for itself: nobody
observed it and the owner did not state it. A revision with it is
`Kind: reference-entry-v3`. From version 3 on, an authority type this kit does not know
makes the entry read `unsupported`, never malformed, so a later type needs no new version.
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import recovery
import reference_records as rr
import reserved_comments
from requirements import canonical_bytes, content_hash
from test_record_voids import void_payload
from test_reference_attestation import AttestationCase, attestation, attested
from test_reference_records import OPERATOR, acceptance, entry

V1, V2, V3 = ('Kind: reference-entry-v%d\n' % number for number in (1, 2, 3))
KEY = 'release.backups.first'
RULE = 'decision-7'           # the decision that sets the rule
DECISION = 'decision-42'      # the acceptance block's default decision (seeded by AttestationCase)


def decided(**extra):
    fields = dict(key=KEY, title='Backups run before a release', statement='Take a backup before every release.',
                  authority={'type': 'decision', 'id': RULE}, owner='person:orchestra-coordinator',
                  review_by='2027-06-01', tags=['release'])
    fields.update(extra)
    return fields


class DecisionCase(AttestationCase):
    def setUp(self):
        super().setUp()
        self.native.seed(RULE, issue_type='decision', status='open')

    def accept(self, revision, digest, key=KEY, **extra):
        return super().accept(revision, digest, key=key, **extra)

    def get(self, key=KEY, operators=(OPERATOR,)):
        return super().get(key, operators)

    def sha(self, revision, key=KEY):
        return super().sha(revision, key)

    def lookups(self):
        return [call for call in self.native.calls if call[:1] == ['list'] and '--id' in call]


class TypeAndVersionTests(DecisionCase):
    def test_a_decision_revision_is_version_3_and_the_others_keep_theirs(self):
        self.propose(**decided())
        self.propose(**attested(operation_id='attested'))
        self.propose(operation_id='plain')
        bodies = [self.entry_comments('ref-%d' % number)[0] for number in (1, 2, 3)]
        self.assertEqual([body[:len(V1)] for body in bodies], [V3, V2, V1])
        record = rr.parse_entry(bodies[0])
        self.assertEqual((record['schema_version'], record['authority'], record['acceptance_state']),
                         (3, {'type': 'decision', 'id': RULE}, 'draft'))
        self.assertEqual(reserved_comments.record_comment_kind(bodies[0]), ('reference-entry', 3, 'supported'))
        self.assertEqual(reserved_comments.record_comment_kind('Kind: reference-entry-v4\n{}')[2], 'unsupported')
        self.assertTrue(reserved_comments.is_record_anchor(self.native.row('ref-1')))
        self.assertTrue(recovery.claims_kind(V3 + '{}', 'reference-entry'))
        self.assertFalse(recovery.claims_kind('Kind: reference-entry-v4\n{}', 'reference-entry'))

    def test_one_form_per_content(self):
        self.propose(**decided())
        record = rr.parse_entry(self.entry_comments()[0])
        text = canonical_bytes(record).decode('utf-8')
        for prefix in (V1, V2):
            self.assertIsNone(rr.parse_entry(prefix + text))
        for version, authority, message in (
                (1, {'type': 'decision', 'id': RULE}, 'schema_version 3 is the entry record with a decision'),
                (2, {'type': 'decision', 'id': RULE}, 'schema_version 2 is the entry record with an attestation'),
                (3, {'type': 'repo-path', 'path': 'src/x.py'}, 'schema_version 3 is the entry record with a decision'),
                (3, attestation(), 'schema_version 2 is the entry record with an attestation')):
            forged = dict(record, schema_version=version, authority=authority)
            forged['sha256'] = content_hash(forged)
            with self.subTest(version=version, type=authority['type']), self.assertRaisesRegex(ValueError, message):
                rr.validate_entry(forged)

    def test_the_five_drafts_shape_a_repo_path_draft_revised_to_a_decision(self):
        self.propose(key=KEY, authority={'type': 'repo-path', 'path': 'HANDOVER.md', 'anchor': 'releases'})
        self.revise(**decided(expected_sha256=self.sha(1)))
        self.assertEqual([body[:len(V1)] for body in self.entry_comments()], [V1, V3])
        view = self.get()
        self.assertEqual((view['state'], view['proposed']['revision'], view['proposed']['authority_kind']),
                         ('draft-only', 2, 'decision'))

    def test_field_refusals_and_an_unknown_decision_write_nothing(self):
        self.native.seed('task-9')
        self.native.seed('labelled-1', labels=['decision'])
        for authority, message in (
                ({'type': 'decision'}, 'authority.id must be the id of a native decision issue'),
                ({'type': 'decision', 'id': '--all'}, 'authority.id must be the id of a native decision issue'),
                ({'type': 'decision', 'id': 'a b'}, 'authority.id must be the id of a native decision issue'),
                ({'type': 'decision', 'id': RULE, 'note': 'x'}, 'authority has unknown field'),
                ({'type': 'decision', 'id': 'decision-404'}, 'authority.id decision-404 must name an existing issue '
                                                             'of type decision or labelled decision'),
                ({'type': 'decision', 'id': 'task-9'}, 'authority.id task-9 must name an existing issue')):
            with self.subTest(authority=authority), self.assertRaisesRegex(ValueError, message):
                self.propose(**decided(authority=authority))
        self.assertEqual(self.native.writes(), [])
        self.assertEqual(list(self.project.glob('.reference-requests/*.json')), [])
        # A malformed id never becomes a native argument.
        self.assertEqual([call[call.index('--id') + 1] for call in self.lookups()], ['decision-404', 'task-9'])
        # An issue that is only labelled decision is accepted as one, as for the decisions field.
        self.assertEqual(self.propose(**decided(authority={'type': 'decision', 'id': 'labelled-1'}))['state'], 'draft')
        # A revise is checked too.
        with self.assertRaisesRegex(ValueError, 'authority.id decision-404 must name an existing issue'):
            self.revise(**decided(expected_sha256=self.sha(1), authority={'type': 'decision', 'id': 'decision-404'}))


class AcceptanceTests(DecisionCase):
    def test_a_contributor_proposes_and_only_the_operator_route_accepts(self):
        self.propose(**decided())
        before = len(self.native.writes())
        payload = {'schema_version': 1, 'operation_id': 'sneaky', 'operation': 'accept', 'key': KEY, 'revision': 1,
                   'record_sha256': self.sha(1), 'acceptance_state': 'accepted', 'acceptance': acceptance()}
        with self.assertRaisesRegex(ValueError, 'operation must be one of propose, revise'):
            rr.apply_native(payload, 'alice', self.native, self.project)
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.accept(1, self.sha(1), actor='alice')
        self.assertEqual(len(self.native.writes()), before)
        self.native.calls = []
        result = self.accept(1, self.sha(1))
        self.assertEqual((result['revision'], result['state']), (2, 'accepted'))
        writes = self.native.writes()
        self.assertTrue(writes[0][3].startswith('Kind: reference-acceptance-v1\n'))
        self.assertTrue(writes[1][3].startswith(V3))
        before = len(self.native.writes())
        self.assertTrue(self.accept(1, self.sha(1))['reconciled'])
        self.assertEqual(len(self.native.writes()), before)

    def test_the_acceptance_decision_may_be_the_decision_the_entry_points_at(self):
        self.propose(**decided())
        self.native.calls = []
        self.assertEqual(self.accept(1, self.sha(1), acceptance=acceptance(decision_id=RULE))['state'], 'accepted')
        self.assertEqual(len(self.lookups()), 1)   # one issue, looked up once

    def test_both_decisions_must_exist_at_acceptance(self):
        self.propose(**decided())
        before = len(self.native.writes())
        self.native.seed('task-9')
        for number, (decision, message) in enumerate((
                ('decision-404', 'acceptance.decision_id must name an existing issue of type decision'),
                ('task-9', 'acceptance.decision_id must name an existing issue of type decision'),
                ('--all', 'acceptance.decision_id must be a native decision issue id'))):
            with self.subTest(decision=decision), self.assertRaisesRegex(ValueError, message):
                self.accept(1, self.sha(1), operation_id='apply-%d' % number,
                            acceptance=acceptance(decision_id=decision))
        with self.assertRaisesRegex(ValueError, 'acceptance.evidence for an entry with a decision authority must '
                                                'be one line'):
            self.accept(1, self.sha(1), operation_id='apply-lines', acceptance=acceptance(evidence='a\nb'))
        self.assertEqual(len(self.native.writes()), before)

    def test_a_decision_that_has_gone_or_changed_type_refuses_the_accept_and_the_revise(self):
        self.propose(**decided())
        digest = self.sha(1)
        for change in ('retyped', 'deleted'):
            if change == 'retyped':
                self.native.row(RULE)['issue_type'] = 'task'
            else:
                self.native.rows.remove(self.native.row(RULE))
            before = len(self.native.writes())
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, 'authority.id %s no longer names an issue of type decision '
                                                        'or labelled decision; revise the entry' % RULE):
                    self.accept(1, digest, operation_id='apply-' + change)
                with self.assertRaisesRegex(ValueError, 'authority.id %s must name an existing issue' % RULE):
                    self.revise(**decided(expected_sha256=digest, statement='Reworded.',
                                          operation_id='revise-' + change))
                self.assertEqual(len(self.native.writes()), before)
        # Reading is unaffected: a read never looks the decision up again.
        self.native.calls = []
        self.assertEqual(self.get()['proposed']['authority'], {'type': 'decision', 'id': RULE})
        self.assertEqual(self.lookups(), [])
        # The way out: revise it to point at the decision that sets the rule now.
        self.native.seed('decision-8', issue_type='decision')
        self.revise(**decided(expected_sha256=digest, authority={'type': 'decision', 'id': 'decision-8'}))
        self.assertEqual(self.accept(2, self.sha(2), operation_id='apply-new')['state'], 'accepted')

    def test_neither_closed_nor_operator_authored_is_required(self):
        # Stated in OPERATIONS: over SSH a contributor can create, close and sign an issue as
        # anyone, so neither would be a control (kittrial-5bb.87, kittrial-5bb.106).
        row = self.native.row(RULE)
        row.update(status='open', created_by='alice')
        self.propose(**decided())
        self.assertEqual(self.accept(1, self.sha(1))['state'], 'accepted')

    def test_no_date_rule_beyond_the_review_date(self):
        self.propose(**decided(review_by='2028-09-30'))            # 24 months ahead: the ordinary ceiling
        self.assertEqual(self.accept(1, self.sha(1))['state'], 'accepted')
        self.propose(**decided(operation_id='undated', key='release.ci.check', review_by=None))
        with self.assertRaisesRegex(ValueError, 'an accepted revision needs review_by'):
            self.accept(1, self.sha(1, 'release.ci.check'), key='release.ci.check', operation_id='apply-undated')

    def test_a_direct_accepted_revision_is_checked_before_its_anchor_is_created(self):
        self.native.actor = OPERATOR
        payload = dict(entry(operation='draft', operation_id='direct', **decided()), acceptance_state='accepted',
                       acceptance=acceptance(decision_id='decision-404'))
        with self.assertRaisesRegex(ValueError, 'acceptance.decision_id must name an existing issue'):
            rr.apply_native(payload, OPERATOR, self.native, self.project, operator=True, operators=[OPERATOR])
        self.assertEqual(self.native.writes(), [])
        payload['acceptance'] = acceptance()
        result = rr.apply_native(payload, OPERATOR, self.native, self.project, operator=True, operators=[OPERATOR])
        self.assertEqual((result['revision'], result['state'], result['created']), (1, 'accepted', True))

    def test_a_batch_accepts_decision_entries_with_the_others(self):
        self.propose(**decided())
        self.propose(**attested(operation_id='attested'))
        self.propose(**decided(operation_id='gone', key='release.ci.check',
                               authority={'type': 'decision', 'id': 'decision-42'}))
        self.native.rows.remove(self.native.row(DECISION))
        self.native.seed('decision-9', issue_type='decision')
        items = [{'key': key, 'revision': 1, 'record_sha256': self.sha(1, key)}
                 for key in (KEY, 'release.ci.check', 'office.server.check')]
        self.native.actor = OPERATOR
        result = rr.apply_batch({'schema_version': 1, 'operation_id': 'batch-1', 'items': items,
                                 'acceptance_state': 'accepted', 'acceptance': acceptance(decision_id='decision-9')},
                                OPERATOR, self.native, self.project, operators=[OPERATOR])
        self.assertEqual([(item['key'], item['result']) for item in result['items']],
                         [(KEY, 'accepted'), ('release.ci.check', 'refused'), ('office.server.check', 'accepted')])
        self.assertIn('no longer names an issue of type decision', result['items'][1]['reason'])
        self.assertEqual((result['accepted'], result['refused'], result['complete']), (2, 1, False))


class ReaderTests(DecisionCase):
    def test_a_draft_is_marked_not_accepted_and_an_accepted_one_says_what_it_is(self):
        self.propose(**decided())
        view = self.get()
        self.assertEqual((view['state'], view['record'], view['proposed']['authority_kind']),
                         ('draft-only', None, 'decision'))
        self.assertEqual(view['proposed']['authority_note'],
                         'NOT ACCEPTED. Points at decision %s; no operator has accepted it, so it is a lead and not '
                         'authority.' % RULE)
        (item,) = rr.read(['list', '--authority', 'decision'], self.native, [OPERATOR])['items']
        self.assertEqual((item['authority_kind'], item['authority_accepted']), ('decision', False))
        self.assertTrue(item['authority_note'].startswith('NOT ACCEPTED.'))
        self.accept(1, self.sha(1))
        view = self.get()
        self.assertEqual((view['state'], view['trust'], view['record']['authority_kind']),
                         ('accepted', 'accepted', 'decision'))
        self.assertEqual(view['record']['authority_note'],
                         'Set by decision %s, accepted by an operator. It is a rule this project set for itself, not '
                         'an observed fact: read the decision for the reasons.' % RULE)
        found = rr.read(['find', 'backups release'], self.native, [OPERATOR])['records'][0]
        self.assertEqual((found['key'], found['trust'], found['authority_kind']), (KEY, 'accepted', 'decision'))
        self.assertTrue(found['authority_note'].startswith('Set by decision'))
        # An operator removed from the allowlist: draft-only again, and marked.
        gone = self.get(operators=('someone-else',))
        self.assertTrue(gone['proposed']['authority_note'].startswith('NOT ACCEPTED.'))

    def test_past_its_review_date_the_note_says_so(self):
        self.propose(**decided(review_by='2026-10-10'))
        self.accept(1, self.sha(1))
        with patch('time.gmtime', return_value=time.struct_time((2026, 10, 11, 12, 0, 0, 6, 284, 0))):
            view = self.get()
        self.assertEqual((view['state'], view['due']), ('accepted', 'expired'))
        self.assertEqual(view['record']['authority_note'],
                         'Set by decision %s, accepted by an operator, and PAST ITS REVIEW DATE (2026-10-10): check '
                         'that the decision still stands before relying on it. It is a rule this project set for '
                         'itself, not an observed fact: read the decision for the reasons.' % RULE)

    def test_the_list_filter_names_all_four_kinds(self):
        self.propose(**decided())
        self.propose(**attested(operation_id='attested'))
        self.propose(operation_id='plain')
        kinds = {item['key']: item['authority_kind'] for item in rr.read(['list'], self.native, [OPERATOR])['items']}
        self.assertEqual(kinds, {KEY: 'decision', 'office.server.check': 'attested', 'calendar.trading': 'repository'})
        self.assertEqual(rr.read(['list', '--authority', 'attested'], self.native, [OPERATOR])['total'], 1)
        with self.assertRaisesRegex(ValueError, '--authority must be repository, url, attested or decision'):
            rr.read(['list', '--authority', 'rule'], self.native, [OPERATOR])
        usage = rr.help_payload()
        self.assertIn('--authority repository|url|attested|decision', usage['usage'][1])
        self.assertEqual(usage['authority']['types'], ['attestation', 'decision', 'repo-path', 'url'])

    def test_brief_and_work_carry_the_kind(self):
        self.propose(**decided(review_by='2026-10-10'))
        self.accept(1, self.sha(1))
        rows = rr.read_rows(self.native)
        (item,) = rr.work_attention(rows, OPERATOR, [OPERATOR])['items']
        self.assertEqual((item['authority_kind'], item['authority_accepted']), ('decision', True))
        (brief,) = rr.brief_attention(rows, {'labels': []}, [OPERATOR])['attention']
        self.assertEqual((brief['authority_kind'], brief['text']),
                         ('decision', 'Reference %s is due for review by 2026-10-10.' % KEY))


class LaterAuthorityTypeTests(DecisionCase):
    """Version 3 is the last bump an authority type needs: a type this kit does not know,
    inside a v3 record, reads `unsupported`."""

    def later_body(self, task='ref-1', rehash=True, prefix=V3, **change):
        """The text of a v3 revision as a later kit would write it: valid in every byte
        outside `authority`, with an authority type this kit has never heard of and
        fields of its own inside it. `change` alters the record before it is hashed."""
        record = rr.parse_entry(self.entry_comments(task)[0])
        record = dict(record, revision=record['revision'] + 1,
                      authority={'type': 'oracle', 'source': 'the next kit', 'confidence': 3})
        record.update(change)
        if rehash:
            record['sha256'] = content_hash(record)
        return prefix + canonical_bytes(record).decode('utf-8')

    def later(self, task='ref-1', author='ops-later', **change):
        return self.native.add_comment(task, self.later_body(task, **change), author=author)

    def test_an_unknown_authority_type_reads_unsupported_not_malformed(self):
        self.propose(**decided())
        self.accept(1, self.sha(1))
        self.propose(operation_id='plain')
        newer = self.later()
        self.assertEqual(rr.unsupported_reason(newer['text']), 'authority type oracle is newer than this kit')
        view = self.get()
        self.assertEqual((view['state'], view['record'], view['proposed']), ('unsupported', None, None))
        self.assertEqual(view['warnings'], [{'code': 'unsupported-record',
                                             'detail': 'authority type oracle is newer than this kit'}])
        listing = rr.read(['list'], self.native, [OPERATOR])
        self.assertEqual([item['key'] for item in listing['items']], ['calendar.trading'])
        self.assertIn('1 entry skipped as unsupported (ref-1)', listing['coverage'])
        self.assertNotIn('malformed', listing['coverage'])
        self.assertEqual(rr.work_attention(rr.read_rows(self.native), OPERATOR, [OPERATOR])['malformed'], 1)

    def test_it_cannot_be_voided_written_accepted_or_released(self):
        self.propose(**decided())
        newer = self.later()
        before = len(self.native.writes())
        self.native.actor = OPERATOR
        with self.assertRaisesRegex(ValueError, 'is one this kit does not support \\(authority type oracle is newer '
                                                'than this kit\\), so it cannot be judged here'):
            rr.KIND.apply_void(void_payload('ref-1', newer['id'], newer['text']), OPERATOR, self.native, [OPERATOR])
        for operation in (lambda: self.revise(**decided(expected_sha256='0' * 64, revision=3)),
                          lambda: self.accept(2, '0' * 64),
                          lambda: self.propose(**decided(operation_id='again'))):
            with self.assertRaises(ValueError) as refused:
                operation()
            self.assertNotIn('malformed', str(refused.exception))
        with self.assertRaisesRegex(ValueError, 'carries a record this kit does not support \\(authority type oracle'):
            self.revise(**decided(expected_sha256='0' * 64, revision=3))
        with self.assertRaisesRegex(ValueError, 'still holds a record'):
            rr.KIND.release(self.project, 'ref-1', OPERATOR, 'try', self.native, operators=[OPERATOR])
        self.assertEqual(len(self.native.writes()), before)
        # A void a later kit wrote for it is not applied by this one either.
        self.assertEqual(self.get()['state'], 'unsupported')

    def test_what_is_still_malformed(self):
        self.propose(**decided())
        for body in (V3 + '{"not": "valid"}', V3 + 'not json', V3 + '{"authority": {"type": "decision"}}',
                     V3 + '{"authority": {"type": 7}}', V2 + '{"authority": {"type": "oracle"}}',
                     V1 + '{"authority": {"type": "oracle"}}'):
            self.assertIsNone(rr.unsupported_reason(body), body)
        bad = self.native.add_comment('ref-1', V3 + '{"not": "valid"}', author='mallory')
        self.assertEqual(self.get()['state'], 'malformed')
        self.native.actor = OPERATOR
        rr.KIND.apply_void(void_payload('ref-1', bad['id'], bad['text']), OPERATOR, self.native, [OPERATOR])
        self.assertEqual(self.get()['state'], 'draft-only')
        # A well-formed decision record the entry reads is not a void target, as for v1 and v2.
        good = next(comment for comment in self.native.row('ref-1')['comments'] if comment['text'].startswith(V3))
        with self.assertRaisesRegex(ValueError, 'is a well-formed reference-entry record the entry reads'):
            rr.KIND.apply_void(void_payload('ref-1', good['id'], good['text'], operation_id='void-2'), OPERATOR,
                               self.native, [OPERATOR])

    def test_garbage_with_a_type_string_is_malformed_and_a_void_unfreezes_the_entry(self):
        # Review of 2d93a07: one host comment of a few bytes froze a genuine accepted entry.
        self.propose(operation_id='plain')
        self.accept(1, self.sha(1, 'calendar.trading'), key='calendar.trading', operation_id='apply-plain')
        self.assertEqual(self.get('calendar.trading')['state'], 'accepted')
        for number, garbage in enumerate(('{"authority":{"type":"zz"}}',          # garbage with a string type
                                          '{"authority":{"type":"Decision"}}',    # three typos of a known type
                                          '{"authority":{"type":""}}',
                                          '{"authority":{"type":"repo-path "}}')):
            with self.subTest(garbage=garbage):
                bad = self.native.add_comment('ref-1', V3 + garbage, author='mallory')
                self.assertIsNone(rr.unsupported_reason(bad['text']))
                view = self.get('calendar.trading')
                self.assertEqual((view['state'], view['warnings'][-1]['code']), ('malformed', 'malformed'))
                self.native.actor = OPERATOR
                rr.KIND.apply_void(void_payload('ref-1', bad['id'], bad['text'], operation_id='void-%d' % number),
                                   OPERATOR, self.native, [OPERATOR])
                self.assertEqual(self.get('calendar.trading')['state'], 'accepted')
        # The entry takes writes again.
        self.native.actor = 'alice'
        payload = entry(operation='revise', operation_id='after', revision=3, statement='Revised afterwards.',
                        expected_sha256=self.get('calendar.trading')['record']['sha256'])
        # The endpoint gives a contributor write the allowlist, so applied voids are left out.
        self.assertEqual(rr.apply_native(payload, 'alice', self.native, self.project,
                                         operators=[OPERATOR])['state'], 'draft')

    def test_only_a_record_valid_everywhere_outside_authority_is_an_unknown_type(self):
        self.propose(**decided())
        good = self.later_body()
        self.assertEqual(rr.unsupported_reason(good), 'authority type oracle is newer than this kit')
        # A later kit may put anything inside authority, and name its type as it likes.
        self.assertEqual(rr.unsupported_reason(self.later_body(authority={'type': 'oracle'})),
                         'authority type oracle is newer than this kit')
        self.assertEqual(rr.unsupported_reason(self.later_body(authority={'type': 'Bad Type\u202e', 'x': [1, {}]})),
                         'an authority type newer than this kit')
        malformed = {
            'a wrong content hash': self.later_body(rehash=False),
            'an empty type': self.later_body(authority={'type': ''}),
            'a type that is not a string': self.later_body(authority={'type': 7}),
            'no type': self.later_body(authority={'source': 'x'}),
            'authority not an object': self.later_body(authority='oracle'),
            'an extra top-level field': None,
            'a missing top-level field': None,
            'schema_version 2': self.later_body(schema_version=2),
            'a bad field outside authority (owner)': self.later_body(owner='session-4e40fde3'),
            'a bad field outside authority (tags)': self.later_body(tags=['Not A Slug']),
            'a bad field outside authority (state)': self.later_body(acceptance_state='maybe'),
            'not canonical bytes': good.replace('{"acceptance_state"', '{ "acceptance_state"'),
            'a BOM before the kind line': '\ufeff' + good,
            'a CRLF kind line': good.replace(V3, 'Kind: reference-entry-v3\r\n'),
            'the v2 kind line': self.later_body(prefix=V2),
        }
        record = rr.parse_entry(self.entry_comments()[0])
        extra = dict(record, authority={'type': 'oracle'}, confidence=3)
        extra['sha256'] = content_hash(extra)
        malformed['an extra top-level field'] = V3 + canonical_bytes(extra).decode('utf-8')
        missing = {name: value for name, value in dict(record, authority={'type': 'oracle'}).items() if name != 'tags'}
        missing['sha256'] = content_hash(missing)
        malformed['a missing top-level field'] = V3 + canonical_bytes(missing).decode('utf-8')
        for label, body in malformed.items():
            with self.subTest(label=label):
                self.assertIsNone(rr.unsupported_reason(body))
        # And through the reader: the wrong-hash record makes the entry malformed, never unsupported.
        self.native.add_comment('ref-1', malformed['a wrong content hash'], author='mallory')
        self.assertEqual(self.get()['state'], 'malformed')

    def test_a_write_on_an_entry_holding_one_is_refused_before_any_ledger_read_can_call_it_malformed(self):
        # The refusal comes from existing_revisions, the first thing every write reads.
        self.propose(**decided())
        self.later()
        with self.assertRaisesRegex(ValueError, 'Reference anchor ref-1 carries a record this kit does not support '
                                                '\\(authority type oracle is newer than this kit\\)'):
            rr.existing_revisions(self.native.row('ref-1'))
        self.assertEqual(rr.KIND.readable_revisions(self.native.row('ref-1'), [OPERATOR]), [])

    def test_a_capability_record_is_untouched_by_the_rule(self):
        import capability_records as cr
        self.assertIsNone(cr.KIND.unsupported_reason)
        self.assertIsNone(cr.KIND.unsupported_record('Kind: capability-entry-v1\n{"authority": {"type": "oracle"}}'))
        self.assertEqual(cr.KIND.unsupported_record('Kind: capability-entry-v2\n{}'),
                         'capability-entry-v2 is newer than this kit')


if __name__ == '__main__':
    unittest.main()
