"""kittrial-5bb.454 r2: a decision counts only with a live server-written attestation of its exact text.

Review 01a18cbd: ``created_by`` says who made a row, not who wrote the text in it. So the record (decision_attest.py) is
proved here on its own, the gate on status, type and label changes (Q1 at VERIFIED) by every spelling, and both on a
real bd behind the real web service: a contributor's edit, a host actor's retype, an unbound key claiming ``ops``.
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import decision_attest as da
import reserved_comments
import test_bd_label_aliases as fixture

AT = ['2026-01-01T00:%02d:%02dZ' % (n // 60, n % 60) for n in range(600)]      # stored server times, only ever ordered
PIN = '@' + 'c' * 64
OWNER, COORD, CONTRIB, VIEWER = ('usr_' + c * 16 for c in 'abcd')
AGENT = 'agent_' + '1' * 16
OPS = ['ops']
PROJECT = 'north'


def web_state():
    users = {user: {'disabled': False} for user in (OWNER, COORD, CONTRIB, VIEWER)}
    return {'users': users,
            'agents': {AGENT: {'id': AGENT, 'owner': COORD, 'enabled': True, 'projects': [PROJECT]}},
            'memberships': {PROJECT: {OWNER: 'owner', COORD: 'coordinator', CONTRIB: 'contributor', VIEWER: 'viewer'}},
            'coordinator_grants': {PROJECT: {AGENT: {'granted_by': OWNER, 'granted_at': AT[0]}}}}


def row_of(description='Some text.', **extra):
    return dict({'id': 'north-1', 'issue_type': 'decision', 'status': 'open', 'title': 'A decision', 'labels': [],
                 'description': description, 'comments': []}, **extra)


def via_of(author):
    return 'session' if author.startswith('usr_') else 'agent' if author.startswith('agent_') else 'operator'


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.journal = Path(temp.name)
        self.seq = 0

    def attest(self, row, author, *, gated=False, journal=True, digest=None, project=PROJECT):
        """A record of ``author`` over the row's text now, as its comment and (unless told not to) its journal entry."""
        self.seq += 1
        record = da.make_record({'schema_version': 1, 'operation_id': 'op-%d' % self.seq, 'decision': row['id'],
                                 'digest': digest or da.digest(row)}, project=project, author=author,
                                via=via_of(author), gated=gated, at=AT[self.seq])
        row['comments'].append({'id': 'c%03d' % self.seq, 'author': author, 'text': da.body(record)})
        if journal:
            da.publish(self.journal, record)
        return record

    def reads(self, row, state=None, project=PROJECT):
        return da.standing(row, project, web_state() if state is None else state, OPS, self.journal)


# -- the record --------------------------------------------------------------------------------------------------
class RecordTests(Case):

    def test_the_digest_covers_the_exact_type_title_and_description_and_nothing_else(self):
        base = row_of('Text.')
        for change in ({'issue_type': 'task'}, {'title': 'Another'}, {'description': 'Text. '}, {'description': 'text.'},
                       {'description': ''}):
            with self.subTest(change=change):
                self.assertNotEqual(da.digest(base), da.digest(dict(base, **change)))
        for change in ({'status': 'closed'}, {'labels': ['x']}, {'created_by': OWNER}, {'comments': [{'id': 'x'}]},
                       {'priority': 1}):
            with self.subTest(change=change):
                self.assertEqual(da.digest(base), da.digest(dict(base, **change)))
        self.assertEqual(da.digest(row_of('')), da.digest({k: v for k, v in row_of('').items() if k != 'description'}))

    def test_a_record_is_bound_to_its_comment_author_project_and_issue(self):
        row = row_of()
        record = self.attest(row, OWNER)
        comment = row['comments'][0]
        self.assertEqual(record, da.parse_comment(comment, project=PROJECT, decision='north-1'))
        self.assertIsNone(da.parse_comment(comment, project='south', decision='north-1'))
        self.assertIsNone(da.parse_comment(comment, project=PROJECT, decision='north-2'))
        self.assertIsNone(da.parse_comment(dict(comment, author=COORD), project=PROJECT, decision='north-1'))
        self.assertIsNone(da.parse_comment(dict(comment, text=comment['text'] + ' '), project=PROJECT, decision='north-1'))

    def test_every_field_is_checked(self):
        good = da.make_record({'schema_version': 1, 'operation_id': 'op', 'decision': 'north-1', 'digest': 'a' * 64},
                              project=PROJECT, author=OWNER, via='session', gated=False, at=AT[1])
        for change in ({'digest': 'x'}, {'via': 'host'}, {'via': 'agent'}, {'via': 'operator'}, {'recorded_by': COORD},
                       {'author': 'ops'}, {'owner_gated': 'no'}, {'at': 'soon'}, {'at': '2026-01-01T00:00:00'},
                       {'extra': 1}, {'schema_version': 2}, {'schema_version': True}):
            with self.subTest(change=change):
                bad = dict(good, **change)
                bad['sha256'] = da.content_hash(bad)
                with self.assertRaises(ValueError):
                    da._validate_record(bad)
        with self.assertRaises(ValueError):
            da._validate_record(dict(good, sha256='0' * 64))
        # An operator's record never carries a web identity's shape.
        for author in (OWNER, AGENT):
            with self.assertRaises(ValueError):
                da.make_record({'schema_version': 1, 'operation_id': 'op', 'decision': 'north-1', 'digest': 'a' * 64},
                               project=PROJECT, author=author, via='operator', gated=False, at=AT[1])

    def test_a_copied_record_is_not_honoured_on_another_issue_or_project(self):
        row = row_of()
        self.attest(row, OWNER)
        self.assertEqual('counts', self.reads(row)['state'])
        other = row_of(id='north-2', comments=copy.deepcopy(row['comments']))
        self.assertEqual('not-attested', self.reads(other)['state'])
        self.assertEqual('not-attested', self.reads(row, project='south')['state'])

    def test_a_record_without_its_journal_entry_is_not_honoured(self):
        row = row_of()
        self.attest(row, OWNER, journal=False)
        self.assertEqual('not-attested', self.reads(row)['state'])
        self.assertEqual('not-attested', da.standing(row, PROJECT, web_state(), OPS, None)['state'])

    def test_the_pin_cannot_be_derived_from_the_text(self):
        # A reader of the area sees the pin and not the text: knowing or guessing the exact text gives them nothing.
        row = row_of('Binds: north\nOwner-only: yes')
        request = {'schema_version': 1, 'operation_id': 'op', 'decision': row['id'], 'digest': da.digest(row)}
        made = [da.make_record(request, project=PROJECT, author=OWNER, via='session', gated=True, at=AT[1]) for _ in range(3)]
        self.assertEqual(3, len({record['sha256'] for record in made}), 'the same text, author, time and operation')
        self.assertEqual(3, len({record['nonce'] for record in made}))
        for record in made:
            self.assertEqual(32, len(record['nonce']))
            self.assertNotEqual(da.digest(row), record['sha256'])
            # Every field a guesser could know, without the nonce, does not give the id.
            known = {key: value for key, value in record.items() if key not in ('nonce', 'sha256')}
            self.assertNotEqual(da.content_hash(known), record['sha256'])

    def test_the_pin_changes_with_new_text_and_not_with_another_attester_of_the_same(self):
        row = row_of('First.')
        first = self.attest(row, COORD)
        self.assertEqual(first['sha256'], self.reads(row)['id'])
        self.attest(row, OWNER)
        self.assertEqual(first['sha256'], self.reads(row)['id'])
        row['description'] = 'Second.'
        self.assertIsNone(self.reads(row)['id'])                # stale: nothing to pin
        second = self.attest(row, OWNER)
        self.assertEqual(second['sha256'], self.reads(row)['id'])
        row['description'] = 'First.'
        third = self.attest(row, OWNER)
        self.assertEqual(third['sha256'], self.reads(row)['id'], 'going back to old text is a new attestation')
        self.assertNotEqual(first['sha256'], third['sha256'])

    def test_the_prefix_is_reserved(self):
        self.assertEqual('Kind: decision-attest-v1\n', reserved_comments.DECISION_ATTEST_PREFIX)
        self.assertEqual(da.PREFIX, reserved_comments.DECISION_ATTEST_PREFIX)
        self.assertIsNotNone(reserved_comments.reserved_match(da.PREFIX + '{}'))


# -- how a row reads ---------------------------------------------------------------------------------------------
class StandingTests(Case):

    def test_who_made_the_row_is_never_read(self):
        # b1: the row says an owner made it, and nobody attested: it binds nothing.
        self.assertEqual('not-attested', self.reads(row_of(created_by=OWNER))['state'])
        # A coordinator attested a row an owner made: it counts as the coordinator's, whoever made it.
        row = row_of(created_by=OWNER)
        self.attest(row, COORD)
        self.assertEqual(('counts', 'coordinator'), (self.reads(row)['state'], self.reads(row)['role']))
        # And the other way about.
        row = row_of(created_by=CONTRIB)
        self.attest(row, OWNER)
        self.assertEqual('owner', self.reads(row)['role'])

    def test_an_edit_reads_stale_and_binds_nothing_then_the_owner_attests_again(self):
        # b1, the named test: the edit, then the re-attestation by the owner.
        for field, value in (('description', 'Binds: north'), ('title', 'Another title'), ('issue_type', 'task')):
            with self.subTest(field=field):
                row = row_of()
                self.attest(row, OWNER)
                self.assertEqual('counts', self.reads(row)['state'])
                row[field] = value
                self.assertEqual('attestation-stale', self.reads(row)['state'])
                self.assertIsNone(self.reads(row)['role'])
                self.attest(row, OWNER)
                self.assertEqual(('counts', 'owner'), (self.reads(row)['state'], self.reads(row)['role']))

    def test_going_back_to_replaced_text_stays_stale(self):
        row = row_of('First.')
        self.attest(row, OWNER)
        row['description'] = 'Second.'
        self.attest(row, OWNER)
        row['description'] = 'First.'
        self.assertEqual('attestation-stale', self.reads(row)['state'])

    def test_the_order_is_by_the_records_time_not_by_how_comments_are_listed(self):
        row = row_of('First.')
        self.attest(row, OWNER)
        row['description'] = 'Second.'
        self.attest(row, OWNER)
        row['comments'].reverse()
        self.assertEqual('counts', self.reads(row)['state'])

    def test_a_closed_row_reads_closed_before_anything_else(self):
        row = row_of(status='closed')
        self.assertEqual('closed', self.reads(row)['state'])
        self.attest(row, OWNER)
        self.assertEqual('closed', self.reads(row)['state'])
        row['status'] = 'open'
        self.assertEqual('counts', self.reads(row)['state'])

    def test_a_coordinator_on_the_same_text_cannot_lower_an_owner(self):
        row = row_of()
        self.attest(row, OWNER)
        self.attest(row, COORD)
        self.assertEqual(('owner', OWNER), (self.reads(row)['role'], self.reads(row)['author']))

    def test_only_the_unbroken_newest_run_counts(self):
        row = row_of('First.')
        self.attest(row, OWNER)
        row['description'] = 'Second.'
        self.attest(row, COORD)
        row['description'] = 'First.'
        self.attest(row, COORD)
        # The owner's attestation of this text was broken by a record over other text.
        self.assertEqual('coordinator', self.reads(row)['role'])

    def test_authority_is_live_at_read(self):
        row = row_of()
        self.attest(row, OWNER)
        for change in ('demote', 'disable', 'remove'):
            with self.subTest(change=change):
                state = web_state()
                if change == 'demote':
                    state['memberships'][PROJECT][OWNER] = 'viewer'
                elif change == 'disable':
                    state['users'][OWNER]['disabled'] = True
                else:
                    del state['memberships'][PROJECT][OWNER]
                self.assertEqual('attester-not-authorised', self.reads(row, state)['state'])
        # A web state that cannot be read counts nobody.
        self.assertEqual('attester-not-authorised', da.standing(row, PROJECT, None, OPS, self.journal)['state'])

    def test_a_contributor_or_viewer_record_counts_for_nothing(self):
        for author in (CONTRIB, VIEWER):
            row = row_of()
            self.attest(row, author)
            self.assertEqual('attester-not-authorised', self.reads(row)['state'], author)

    def test_an_operator_counts_while_its_exact_name_is_listed(self):
        row = row_of()
        self.attest(row, 'ops')
        self.assertEqual(('counts', 'operator'), (self.reads(row)['state'], self.reads(row)['role']))
        for listed in ([], ['opsx'], ['Ops'], None, 'ops'):
            with self.subTest(listed=listed):
                self.assertEqual('attester-not-authorised',
                                 da.standing(row, PROJECT, web_state(), listed, self.journal)['state'])

    def test_an_agent_counts_as_coordinator_never_owner_and_only_while_its_grant_holds(self):
        # Q4 at VERIFIED.
        row = row_of()
        self.attest(row, AGENT)
        self.assertEqual('agent-coordinator', self.reads(row)['role'])
        state = web_state()
        del state['coordinator_grants'][PROJECT][AGENT]
        self.assertEqual('attester-not-authorised', self.reads(row, state)['state'])

    def test_a_bare_name_is_no_authority(self):
        # b3: the branch that took a bare allowlist name is gone.
        for name in ('ops', 'Ops', 'someone', OWNER + '/lane', '', None):
            with self.subTest(name=name):
                self.assertIsNone(da.authority(name, PROJECT, web_state()))


# -- writing one -------------------------------------------------------------------------------------------------
class Native:
    def __init__(self, row):
        self.rows = {row['id']: row}
        self.writer = None
        self.seq = 0

    def __call__(self, argv):
        if argv[0] == 'list':
            wanted = argv[argv.index('--id') + 1]
            return json.dumps([{k: v for k, v in r.items() if k != 'comments'} for i, r in self.rows.items() if i == wanted])
        if argv[0] == 'show':
            return json.dumps([self.rows[argv[1]]])
        if argv[:2] == ['comments', 'add']:
            self.seq += 1
            comment = {'id': 'n%03d' % self.seq, 'author': self.writer, 'text': argv[3]}
            self.rows[argv[2]]['comments'].append(comment)
            return json.dumps(comment)
        raise AssertionError(argv)


class ApplyTests(Case):

    def write(self, native, author, digest=None, decision='north-1', state=None, operators=OPS, pinned=None):
        native.writer = author
        self.seq += 1
        # Unless a test is about them: every pin on the row is read, and is what the program decision has now.
        if pinned is None:
            pinned = {pin[:2]: {'pin': pin[2], 'state': 'counts'} for pin in da.pins(native.rows['north-1'])}
        return da.apply({'schema_version': 1, 'operation_id': 'op-%d' % self.seq, 'decision': decision,
                         'digest': digest or da.digest(native.rows['north-1'])}, project=PROJECT, journal=self.journal,
                        run=native, author=author, via=via_of(author), at=AT[self.seq],
                        state=web_state() if state is None else state, operators=operators, pinned=pinned)

    def test_each_standing_writes_a_record_that_counts(self):
        for author, role in ((OWNER, 'owner'), (COORD, 'coordinator'), (AGENT, 'agent-coordinator'), ('ops', 'operator')):
            with self.subTest(author=author):
                native = Native(row_of())
                record = self.write(native, author)['record']
                self.assertEqual((author, author, via_of(author)), (record['author'], record['recorded_by'], record['via']))
                self.assertTrue(da.journaled(self.journal, record))
                self.assertEqual(role, self.reads(native.rows['north-1'])['role'])

    def test_no_standing_is_refused_and_writes_nothing(self):
        # Q3 at VERIFIED: a contributor at the route, so a newer record cannot push out an owner's.
        for author in (CONTRIB, VIEWER, 'usr_' + 'e' * 16, 'opsx'):
            with self.subTest(author=author):
                native = Native(row_of())
                with self.assertRaises(da.AttestRefusal) as refused:
                    self.write(native, author)
                self.assertEqual(403, refused.exception.status)
                self.assertEqual([], native.rows['north-1']['comments'])

    def test_the_text_that_changed_since_it_was_read_is_refused(self):
        native = Native(row_of())
        read = da.digest(native.rows['north-1'])
        native.rows['north-1']['description'] = 'An edit landed.'
        with self.assertRaises(da.AttestRefusal) as refused:
            self.write(native, OWNER, digest=read)
        self.assertEqual(409, refused.exception.status)
        self.assertEqual([], native.rows['north-1']['comments'])
        self.assertEqual([], list((self.journal / da.JOURNAL_DIR).glob('*.json')))

    def test_only_a_decision_can_be_attested(self):
        for row in (row_of(issue_type='task'), row_of(issue_type='event', labels=['decision'])):
            native = Native(row)
            with self.assertRaises(da.AttestRefusal) as refused:
                self.write(native, OWNER)
            self.assertEqual(404, refused.exception.status)
        with self.assertRaises(da.AttestRefusal) as refused:
            self.write(Native(row_of()), OWNER, decision='north-404')
        self.assertEqual(404, refused.exception.status)

    def test_a_coordinator_cannot_replace_an_owners_level(self):
        for text in ('Binds: north\nOwner-only: yes', 'Authorises: decision:prog/prog-1' + PIN,
                     'Adopts: decision:prog/prog-1' + PIN):
            with self.subTest(text=text):
                native = Native(row_of(text))
                self.write(native, OWNER)
                # An edit by the coordinator, then their attestation of it: refused; the owner's still decides.
                native.rows['north-1']['description'] = 'Binds: north\nOwner-only: no'
                with self.assertRaises(da.AttestRefusal) as refused:
                    self.write(native, COORD)
                self.assertEqual(403, refused.exception.status)
                self.assertEqual('attestation-stale', self.reads(native.rows['north-1'])['state'])
                self.write(native, OWNER)
                self.assertEqual('owner', self.reads(native.rows['north-1'])['role'])

    def test_a_coordinator_may_replace_a_coordinators_and_attest_a_draft_for_an_owner(self):
        native = Native(row_of('Binds: north\nOwner-only: no'))
        self.write(native, COORD)
        native.rows['north-1']['description'] = 'Binds: north, south\nOwner-only: no'
        self.write(native, AGENT)
        self.assertEqual('agent-coordinator', self.reads(native.rows['north-1'])['role'])
        draft = Native(row_of('Binds: north\nOwner-only: yes'))
        self.assertTrue(self.write(draft, COORD)['record']['owner_gated'])
        self.write(draft, OWNER)
        self.assertEqual('owner', self.reads(draft.rows['north-1'])['role'])

    def test_what_needs_an_owner_is_recorded(self):
        cases = [(OWNER, 'Owner-only: yes', True), (COORD, 'Owner-only: yes', True), (OWNER, 'Owner-only: no', False),
                 (OWNER, 'Authorises: decision:prog/prog-1' + PIN, True), (COORD, 'Authorises: decision:prog/prog-1' + PIN, True),
                 (OWNER, 'Adopts: decision:prog/prog-1' + PIN, True), ('ops', 'Adopts: decision:prog/prog-1' + PIN, True),
                 (COORD, 'Adopts: decision:prog/prog-1' + PIN, False), (OWNER, 'Plain prose.', False),
                 (COORD, 'Owner-only: maybe', True)]
        for author, text, expected in cases:
            with self.subTest(author=author, text=text):
                self.assertEqual(expected, self.write(Native(row_of(text)), author)['record']['owner_gated'])

    def test_a_pinned_decision_has_to_be_read_and_to_be_what_it_says_now(self):
        for line in ('Adopts: decision:prog/prog-1' + PIN, 'Authorises: decision:prog/prog-1' + PIN):
            key, fact = ('prog', 'prog-1'), {'pin': 'c' * 64, 'state': 'counts'}
            for pinned, status, words in (({key: None}, 403, 'it does not exist, or you may not read it'),
                                          ({}, 409, 'changed since you read it'),
                                          ({key: dict(fact, pin='d' * 64)}, 409, 'other text now'),
                                          ({key: dict(fact, state='attestation-stale')}, 409, 'does not count now'),
                                          ({key: dict(fact, state='closed')}, 409, 'does not count now')):
                with self.subTest(line=line, pinned=pinned):
                    native = Native(row_of(line))
                    with self.assertRaises(da.AttestRefusal) as refused:
                        self.write(native, OWNER, pinned=pinned)
                    self.assertEqual(status, refused.exception.status)
                    self.assertIn(words, str(refused.exception))
                    self.assertEqual([], native.rows['north-1']['comments'])
            self.assertTrue(self.write(Native(row_of(line)), OWNER, pinned={key: fact})['record'])

    def test_an_unreadable_program_and_a_missing_one_get_the_same_words(self):
        said = set()
        for pinned in ({('prog', 'prog-1'): None}, {('prog', 'prog-1'): None}):
            with self.assertRaises(da.AttestRefusal) as refused:
                self.write(Native(row_of('Adopts: decision:prog/prog-1' + PIN)), OWNER, pinned=pinned)
            said.add((refused.exception.status, str(refused.exception)))
        self.assertEqual(1, len(said))

    def test_the_pins_are_read_for_each_line_and_a_row_without_one_reads_nothing(self):
        seen = []

        def read(program, issue):
            seen.append((program, issue))
            return {'pin': 'x', 'state': 'counts'}
        self.assertEqual({}, da.read_pins(row_of('Plain prose.'), read))
        self.assertEqual([], seen)
        found = da.read_pins(row_of('Adopts: decision:prog/prog-1' + PIN + '\nAuthorises: decision:prog/prog-2' + PIN), read)
        self.assertEqual({('prog', 'prog-1'), ('prog', 'prog-2')}, set(found))
        self.assertIsNone(da.pin_fact(row_of(issue_type='task'), 'prog', web_state(), OPS, self.journal))
        self.assertEqual({'pin': None, 'state': 'not-attested'},
                         da.pin_fact(row_of(), 'prog', web_state(), OPS, self.journal))

    def test_a_journal_that_cannot_be_used_stops_it_before_the_comment(self):
        native = Native(row_of())
        (self.journal / da.JOURNAL_DIR).write_text('a file', encoding='utf-8')
        with self.assertRaises(da.AttestRefusal) as refused:
            self.write(native, OWNER)
        self.assertEqual(503, refused.exception.status)
        self.assertEqual([], native.rows['north-1']['comments'])

    def test_the_read_shows_the_exact_text_its_digest_and_how_it_reads(self):
        row = row_of('Exact <b>text</b> ')
        answer = da.read_answer(row, PROJECT, web_state(), OPS, self.journal)
        self.assertEqual(('Exact <b>text</b> ', da.digest(row), 'not-attested', None),
                         (answer['description'], answer['digest'], answer['attestation'], answer['attested_as']))
        self.assertEqual((None, None, None), (answer['attestation_id'], answer['adopt_line'], answer['authorise_line']))
        record = self.attest(row, OWNER)
        answer = da.read_answer(row, PROJECT, web_state(), OPS, self.journal)
        self.assertEqual('Adopts: decision:north/north-1@' + record['sha256'], answer['adopt_line'])
        self.assertEqual('Authorises: decision:north/north-1@' + record['sha256'], answer['authorise_line'])
        for bad in (None, row_of(issue_type='task')):
            with self.assertRaises(da.AttestRefusal) as refused:
                da.read_answer(bad, PROJECT, web_state(), OPS, self.journal)
            self.assertEqual(404, refused.exception.status)


# -- the gate on status, type and labels ---------------------------------------------------------------------------
class GateParseTests(unittest.TestCase):

    def test_every_spelling_that_moves_a_status(self):
        for args in (['close', 'x-1'], ['reopen', 'x-1'], ['close', 'x-1', '--reason', 'done', '--force'],
                     ['update', 'x-1', '--status', 'closed'], ['update', 'x-1', '--status=open'],
                     ['update', 'x-1', '-s', 'open'], ['update', 'x-1', '-s=open'], ['update', 'x-1', '-sopen'],
                     ['update', 'x-1', '--claim'], ['update', 'x-1', '--defer', 'tomorrow']):
            with self.subTest(args=args):
                command, targets, change = da.gated_change(args)
                self.assertEqual((['x-1'], True), (targets, change['status']))

    def test_every_spelling_of_a_type_or_label_change(self):
        cases = [(['update', 'x-1', '--type', 'task'], 'type', 'task'), (['update', 'x-1', '--type=task'], 'type', 'task'),
                 (['update', 'x-1', '-t', 'task'], 'type', 'task'), (['update', 'x-1', '-ttask'], 'type', 'task'),
                 (['update', 'x-1', '--add-label', 'decision'], 'add', ['decision']),
                 (['update', 'x-1', '--add-label=a,decision'], 'add', ['a', 'decision']),
                 (['update', 'x-1', '--remove-label', 'decision'], 'remove', ['decision']),
                 (['update', 'x-1', '--set-labels', 'a,b'], 'set', ['a', 'b'])]
        for args, key, value in cases:
            with self.subTest(args=args):
                self.assertEqual(value, da.gated_change(args)[2][key])
                self.assertFalse(da.gated_change(args)[2]['status'])

    def test_what_is_not_gated(self):
        for args in (['update', 'x-1', '--title', 'A title'], ['update', 'x-1', '--description', 'Text'],
                     ['update', 'x-1', '@attachment:0'], ['update', 'x-1', '--assignee', 'w1'], ['update', 'x-1', '-a', 'w1'],
                     ['update', 'x-1', '--claim=false'], ['comments', 'add', 'x-1', 'A comment'], ['show', 'x-1'],
                     # No other label starts or stops a decision, so an ordinary relabel reads nothing for the gate.
                     ['update', 'x-1', '--add-label', 'bug'], ['update', 'x-1', '--remove-label', 'bug'],
                     ['update', 'x-1', '--add-label=a,b'],
                     ['create', 'Title', '--type', 'decision'], ['dep', 'add', 'x-1', 'x-2'], [], None):
            with self.subTest(args=args):
                self.assertIsNone(da.gated_change(args))

    def test_an_invocation_naming_no_exact_issue_has_no_targets(self):
        for args in (['close'], ['reopen'], ['update', '--status', 'closed'], ['update', '--type', 'task']):
            with self.subTest(args=args):
                self.assertIsNone(da.gated_change(args)[1])

    def test_a_change_that_starts_or_stops_a_decision(self):
        none = {'status': False, 'type': None, 'add': [], 'remove': [], 'set': None}
        typed = row_of()
        labelled = row_of(issue_type='task', labels=['decision', 'x'])
        plain = row_of(issue_type='task')
        for row, change, expected in ((typed, {'type': 'task'}, True), (typed, {'type': 'decision'}, False),
                                      (typed, {'add': ['decision']}, False), (typed, {'type': 'event'}, True),
                                      (labelled, {'remove': ['decision']}, True), (labelled, {'set': ['x']}, True),
                                      (labelled, {'set': ['decision']}, False), (labelled, {'type': 'bug'}, False),
                                      (plain, {'add': ['decision']}, True), (plain, {'type': 'decision'}, True),
                                      (plain, {'set': ['decision']}, True), (plain, {'add': ['x']}, False),
                                      (typed, {'type': 'task', 'add': ['decision']}, False)):
            with self.subTest(row=row['issue_type'], change=change):
                self.assertEqual(expected, da.changes_decision(row, dict(none, **change)))


class GateTests(Case):
    STATUS = {'status': True, 'type': None, 'add': [], 'remove': [], 'set': None}
    RETYPE = {'status': False, 'type': 'task', 'add': [], 'remove': [], 'set': None}
    RELABEL = {'status': False, 'type': None, 'add': [], 'remove': ['decision'], 'set': None}
    HARMLESS = {'status': False, 'type': None, 'add': ['note'], 'remove': [], 'set': None}

    def gate(self, row, actor, change=None, by_service=True, state=None):
        return da.gate(row, project=PROJECT, journal=self.journal, actor=actor, state=web_state() if state is None else state,
                       by_service=by_service, command='update', change=change or self.STATUS)

    def test_standing_on_the_web(self):
        cases = [(False, OWNER, True), (False, COORD, True), (False, AGENT, True), (False, CONTRIB, False),
                 (False, VIEWER, False), (False, 'ops', False), (False, OWNER + '/lane', False),
                 (True, OWNER, True), (True, COORD, False), (True, AGENT, False), (True, CONTRIB, False)]
        for gated, actor, allowed in cases:
            for change in (self.STATUS, self.RETYPE):
                with self.subTest(gated=gated, actor=actor, change=change):
                    row = row_of()
                    self.attest(row, OWNER, gated=gated)
                    denied = self.gate(row, actor, change)
                    self.assertEqual(allowed, denied is None)
                    if denied is not None:
                        self.assertEqual(403, denied.status)

    def test_every_host_route_refuses_everyone_an_operator_included(self):
        for actor in ('ops', OWNER, 'w1'):
            for change, row in ((self.STATUS, row_of()), (self.RETYPE, row_of()),
                                (self.RELABEL, row_of(labels=['decision'], issue_type='task'))):
                with self.subTest(actor=actor, change=change):
                    self.attest(row, OWNER)
                    denied = self.gate(row, actor, change, by_service=False)
                    self.assertEqual(403, denied.status)
                    self.assertIn('decision-change', str(denied))

    def test_a_row_with_no_record_and_none_of_the_lines_is_not_gated(self):
        for row in (row_of(), row_of(issue_type='task')):
            for change in (self.STATUS, self.RETYPE):
                self.assertIsNone(self.gate(row, CONTRIB, change))
                self.assertIsNone(self.gate(row, 'w1', change, by_service=False))

    def test_a_decision_row_carrying_any_of_the_lines_is_gated_attested_or_not(self):
        # Review of r2: a row reading not-attested stood open, so a host actor closed a restored decision for good.
        lines = ['Binds: north', 'Adopts: decision:prog/prog-1' + PIN, 'Owner-only: no', 'Per: decision:north/north-2',
                 'Authorises: decision:prog/prog-1' + PIN, 'Binds: Not A Project', 'Owner-only: maybe', 'Per: decision:north',
                 'Adopts: decision:', 'Authorises: decision:prog/prog-1']
        for line in lines:
            with self.subTest(line=line):
                row = row_of('Some prose.\n' + line)
                self.assertEqual(403, self.gate(row, CONTRIB).status)
                self.assertEqual(403, self.gate(row, 'ops', by_service=False).status)
                self.assertIsNone(self.gate(row, OWNER))
                self.assertEqual(403, self.gate(row, 'w1', self.RETYPE, by_service=False).status)

    def test_prose_that_starts_like_a_line_is_not_one(self):
        # Review of r3, c1: an ordinary decision that says this closes as it always did, on the host too.
        for text in ('Per: the meeting of Tuesday', 'Adopts: the queue from last year', 'Authorises: nothing yet',
                     'Per decision:north/north-2', 'per: decision:north/north-2'):
            with self.subTest(text=text):
                self.assertFalse(da.carries_lines(row_of(text)))
                self.assertIsNone(self.gate(row_of(text), 'w1', by_service=False))
                self.assertIsNone(self.gate(row_of(text), CONTRIB))
        # An attestation of such a row is still an attestation.
        attested = row_of('Per: the meeting of Tuesday')
        self.attest(attested, OWNER, gated=True)
        self.assertEqual(403, self.gate(attested, 'w1', by_service=False).status)

    def test_an_attestation_comment_no_journal_honours_gates_the_row_at_an_owners_level(self):
        # Review of r3, b1: with the journal gone the lines can be edited out, and the row must still not reopen.
        row = row_of('Plain prose, with the lines edited out.', status='closed')
        self.attest(row, OWNER, journal=False)
        self.assertEqual('closed', self.reads(row)['state'])
        self.assertTrue(da.carries_attestation_comment(row))
        self.assertFalse(da.carries_lines(row))
        self.assertEqual(403, self.gate(row, 'plain', by_service=False).status)
        self.assertEqual(403, self.gate(row, 'ops', by_service=False).status)
        for actor in (CONTRIB, COORD, AGENT):
            self.assertEqual(403, self.gate(row, actor).status, actor)
        self.assertIsNone(self.gate(row, OWNER))
        # A comment that only quotes the words, not at its start, is not one; a task's is the gate's only as a decision.
        self.assertFalse(da.carries_attestation_comment(row_of(comments=[{'id': 'c', 'text': 'see ' + da.PREFIX}])))
        task = row_of('Prose.', issue_type='task', comments=row['comments'])
        self.assertIsNone(self.gate(task, 'plain', by_service=False))
        start = {'status': False, 'type': 'decision', 'add': [], 'remove': [], 'set': None}
        self.assertEqual(403, self.gate(task, 'plain', start, by_service=False).status)

    def test_the_level_of_a_row_with_no_record_comes_from_its_text(self):
        for text, coordinator in (('Binds: north\nOwner-only: yes', 403), ('Authorises: decision:prog/prog-1' + PIN, 403),
                                  ('Owner-only: maybe', 403), ('Binds: north\nOwner-only: no', None),
                                  ('Adopts: decision:prog/prog-1' + PIN, None), ('Per: decision:north/north-2', None)):
            with self.subTest(text=text):
                denied = self.gate(row_of(text), COORD)
                self.assertEqual(coordinator, denied.status if denied else None)
                self.assertIsNone(self.gate(row_of(text), OWNER))

    def test_a_task_with_the_lines_is_gated_only_as_a_decision(self):
        # Only a decision row binds, so a task's own status is its own affair; these lines cannot make it a decision unasked.
        task = row_of('Binds: north\nOwner-only: no', issue_type='task')
        self.assertIsNone(self.gate(task, 'w1', by_service=False))
        self.assertIsNone(self.gate(task, CONTRIB))
        start = {'status': False, 'type': 'decision', 'add': [], 'remove': [], 'set': None}
        self.assertEqual(403, self.gate(task, 'w1', start, by_service=False).status)
        self.assertEqual(403, self.gate(task, CONTRIB, dict(start, type=None, add=['decision'])).status)
        self.assertIsNone(self.gate(task, OWNER, start))

    def test_a_label_change_that_leaves_it_a_decision_is_not_gated(self):
        row = row_of()
        self.attest(row, OWNER)
        self.assertIsNone(self.gate(row, CONTRIB, self.HARMLESS))
        self.assertIsNone(self.gate(row, 'w1', self.RELABEL, by_service=False))     # its type still makes it one

    def test_the_level_comes_from_the_record_not_from_the_text_now(self):
        # A coordinator cannot take Owner-only: yes out of the text and then close it, or reopen and restore it.
        row = row_of('Binds: north\nOwner-only: yes')
        self.attest(row, OWNER, gated=True)
        row['description'] = 'Binds: north\nOwner-only: no'
        self.assertEqual('attestation-stale', self.reads(row)['state'])
        self.assertEqual(403, self.gate(row, COORD).status)
        self.assertIsNone(self.gate(row, OWNER))

    def test_a_coordinator_on_the_same_text_keeps_the_owners_level(self):
        row = row_of()
        self.attest(row, OWNER, gated=True)
        self.attest(row, COORD, gated=False)
        self.assertEqual(403, self.gate(row, COORD).status)

    def test_a_retired_decision_comes_back_only_by_someone_with_standing(self):
        # Q1 at VERIFIED: the row is closed; its text never changed, so a reopen would otherwise restore it.
        row = row_of('Binds: north\nOwner-only: no', status='closed')
        self.attest(row, OWNER)
        self.assertEqual('closed', self.reads(row)['state'])
        self.assertEqual(403, self.gate(row, CONTRIB).status)
        self.assertEqual(403, self.gate(row, 'ops', by_service=False).status)
        self.assertIsNone(self.gate(row, OWNER))
        row['status'] = 'open'
        self.assertEqual('counts', self.reads(row)['state'])

    def test_a_lost_role_loses_the_gate(self):
        row = row_of()
        self.attest(row, OWNER)
        state = web_state()
        state['memberships'][PROJECT][OWNER] = 'viewer'
        self.assertEqual(403, self.gate(row, OWNER, state=state).status)


# -- a real bd behind the real endpoint and the real web service -------------------------------------------------
@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class WebRouteTests(unittest.TestCase):
    """pp on a real bd. pat owns it, cleo coordinates it with the granted agent Kite, carl contributes, vera views."""

    @classmethod
    def setUpClass(cls):
        cls.runtime = type('AttestRuntime', (fixture.RealBdLabelAliasTests,), {})
        cls.runtime.setUpClass()
        cls.root = cls.runtime.root
        cls.project = cls.runtime.root / 'projects' / 'pp'
        bound = cls.runtime(methodName='runTest')
        for key, value in fixture.admin.PROJECT_SETTINGS:
            assert bound.bd('config', 'set', key, value).returncode == 0
        assert bound.bd('create', 'Merge Slot', '--id', 'pp-merge-slot', '--labels', 'gt:slot', '--json').returncode == 0
        document = json.loads((cls.root / 'deployment.private.json').read_text(encoding='utf-8'))
        document['operators'] = ['ops']
        (cls.root / 'deployment.private.json').write_text(json.dumps(document), encoding='utf-8')
        import http_service
        import test_http_review_fixes as fixes
        root = cls.root

        class HttpHarness(fixes.Harness):
            def make_backend(harness_self):
                return http_service.EndpointBackend(sys.executable, str(KIT / 'endpoint.py'), str(root),
                                                    service=harness_self.service)
        cls.web = HttpHarness(methodName='runTest')
        cls.web.setUp()
        admin = cls.web.admin_token()
        cls.ids = {name: cls.web.create_account(admin, name, name + '-password-1')
                   for name in ('pat', 'cleo', 'carl', 'vera')}
        made = cls.web.request('POST', '/v1/projects', {'project_id': 'pp', 'name': 'Attest'}, token=admin)
        assert made.status == 201, made.data
        for name, role in (('pat', 'owner'), ('cleo', 'coordinator'), ('carl', 'contributor'), ('vera', 'viewer')):
            assert cls.web.request('PUT', '/v1/projects/pp/members/' + cls.ids[name], {'role': role}, token=admin).status == 200
        cls.tokens = {name: cls.web.login(name, name + '-password-1')[0] for name in cls.ids}
        kite = cls.web.request('POST', '/v1/agents', {'name': 'Kite', 'working_directory': '/tmp/synthetic-kite',
                                                      'projects': ['pp']}, token=cls.tokens['cleo'])
        assert kite.status == 201, kite.data
        cls.ids['kite'] = kite.data['agent']['id']
        cls.tokens['kite'] = kite.data['credential']['secret']
        assert cls.web.request('PUT', '/v1/projects/pp/agents/%s/coordinator' % cls.ids['kite'], {},
                               token=cls.tokens['pat']).status == 200

    @classmethod
    def tearDownClass(cls):
        cls.web._stop_server()
        cls.runtime.tearDownClass()

    def host(self, actor, *args, **options):
        return fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': actor, 'action': 'bd', 'args': list(args)},
                                        **options)

    def make(self, text='Binds: pp\nOwner-only: no', kind='decision', actor='ops'):
        answer = self.host(actor, 'create', 'A decision ' + uuid.uuid4().hex[:6], '--type', kind, '--description', text,
                           '--json')
        self.assertEqual(0, answer['returncode'], answer)
        return json.loads(answer['stdout'])['id']

    def read(self, issue, name='vera'):
        answer = self.web.request('GET', '/v1/projects/pp/decisions/%s/attestation' % issue, token=self.tokens[name])
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    def attest(self, issue, name, digest=None, expected=201):
        answer = self.web.request('POST', '/v1/projects/pp/decisions/%s/attest' % issue,
                                  {'digest': digest or self.read(issue)['digest']}, token=self.tokens[name],
                                  key='attest-' + uuid.uuid4().hex)
        self.assertEqual(expected, answer.status, answer.data)
        return answer.data

    def patch_task(self, issue, name, body, expected=200):
        answer = self.web.request('PATCH', '/v1/projects/pp/tasks/%s' % issue, body, token=self.tokens[name],
                                  key='patch-' + uuid.uuid4().hex)
        self.assertEqual(expected, answer.status, answer.data)
        return answer.data

    def status(self, issue):
        return self.read(issue)['status']

    # -- b1 ------------------------------------------------------------------------------------------------------
    def test_a_contributors_patch_of_an_owners_decision_reads_stale_then_the_owner_attests_again(self):
        issue = self.make()
        self.assertEqual('not-attested', self.read(issue)['attestation'])
        made = self.attest(issue, 'pat')
        self.assertEqual(('session', self.ids['pat']), (made['record']['via'], made['record']['author']))
        self.assertEqual(('counts', 'owner'), (self.read(issue)['attestation'], self.read(issue)['attested_as']))
        self.patch_task(issue, 'carl', {'description': 'Binds: pp\nOwner-only: yes\nThe contributor wrote this.'})
        self.assertEqual('attestation-stale', self.read(issue)['attestation'])
        self.attest(issue, 'pat')
        self.assertEqual(('counts', 'owner'), (self.read(issue)['attestation'], self.read(issue)['attested_as']))
        self.patch_task(issue, 'carl', {'title': 'Retitled by a contributor'})
        self.assertEqual('attestation-stale', self.read(issue)['attestation'])

    def test_a_host_actors_retype_or_relabel_of_a_task_counts_for_nothing(self):
        for args in (('--type', 'decision'), ('--add-label', 'decision')):
            with self.subTest(args=args):
                # A plain task may be retyped, and reads not-attested, with the lines added after it or not.
                task = self.make('Plain prose.', kind='task')
                answer = self.host('w1', 'update', task, *args, '--json')
                self.assertEqual(0, answer['returncode'], answer)
                self.assertEqual('not-attested', self.read(task)['attestation'])
                self.assertEqual(0, self.host('w1', 'update', task, '--description', 'Binds: pp\nOwner-only: no', '--json')['returncode'])
                self.assertEqual('not-attested', self.read(task)['attestation'])
                # A task that already says it binds is the gate's.
                task = self.make(kind='task')
                answer = self.host('w1', 'update', task, *args, '--json')
                self.assertEqual((2, 403), (answer['returncode'], answer['delegation_status']), answer)

    def test_a_host_actor_cannot_close_an_unattested_decision_with_the_lines(self):
        # Review of r2: reading not-attested, restored or with its journal entry lost, the row stands gated.
        issue = self.make()
        self.assertEqual('not-attested', self.read(issue)['attestation'])
        for actor in ('w1', 'ops'):
            answer = self.host(actor, 'close', issue, '--json')
            self.assertEqual((2, 403), (answer['returncode'], answer['delegation_status']), answer)
        self.patch_task(issue, 'carl', {'status': 'closed'}, expected=403)
        self.assertEqual('open', self.status(issue))
        self.patch_task(issue, 'cleo', {'status': 'closed'})
        self.assertEqual('closed', self.status(issue))
        # A plain decision issue, with none of the lines, is as it always was.
        plain = self.make('Plain prose.')
        self.assertEqual(0, self.host('w1', 'close', plain, '--json')['returncode'])

    def test_the_digest_read_before_an_edit_is_refused(self):
        issue = self.make()
        read = self.read(issue)['digest']
        self.patch_task(issue, 'carl', {'description': 'An edit landed first.'})
        self.attest(issue, 'pat', digest=read, expected=409)
        self.assertEqual('not-attested', self.read(issue)['attestation'])

    def test_who_may_write_one(self):
        issue = self.make()
        self.attest(issue, 'carl', expected=403)
        self.attest(issue, 'vera', expected=403)
        self.assertEqual('not-attested', self.read(issue)['attestation'])
        made = self.attest(issue, 'kite')
        self.assertEqual(('agent', self.ids['kite']), (made['record']['via'], made['record']['author']))
        self.assertEqual('agent-coordinator', self.read(issue)['attested_as'])
        self.attest(issue, 'cleo')
        self.assertEqual('coordinator', self.read(issue)['attested_as'])

    def test_a_raw_comment_with_the_prefix_is_refused_on_both_routes(self):
        issue = self.make()
        text = da.PREFIX + '{}'
        with self.assertRaises(ValueError):
            self.host('w1', 'comments', 'add', issue, text)
        answer = self.web.request('POST', '/v1/projects/pp/tasks/%s/comments' % issue, {'text': text},
                                  token=self.tokens['pat'], key='raw-' + uuid.uuid4().hex)
        self.assertNotIn(answer.status, (200, 201), answer.data)
        self.assertEqual('not-attested', self.read(issue)['attestation'])

    def test_a_worker_credential_cannot_write_one_even_an_owners(self):
        # E1: an owner's own worker credential holds the route's capability and is still not the owner's own session.
        issue = self.make()
        issued = self.web.issue_credential(self.tokens['pat'], 'pp', label='lane')
        answer = self.web.request('POST', '/v1/projects/pp/decisions/%s/attest' % issue,
                                  {'digest': self.read(issue)['digest']}, token=issued['secret'],
                                  key='cred-' + uuid.uuid4().hex)
        self.assertEqual(403, answer.status, answer.data)
        self.assertIn('own web session or agent', answer.data['error']['message'])
        self.assertEqual('not-attested', self.read(issue)['attestation'])

    def test_the_route_takes_the_digest_and_nothing_else(self):
        # E6: the author, how it came and the time are the server's: naming them is refused, not ignored.
        issue = self.make()
        digest = self.read(issue)['digest']
        for body in ({'digest': digest, 'author': self.ids['pat']}, {'digest': digest, 'via': 'operator'}, {}, {'digest': 'x'}):
            with self.subTest(body=body):
                answer = self.web.request('POST', '/v1/projects/pp/decisions/%s/attest' % issue, body,
                                          token=self.tokens['carl'], key='body-' + uuid.uuid4().hex)
                self.assertIn(answer.status, (400, 422), answer.data)
        self.assertEqual('not-attested', self.read(issue)['attestation'])

    def test_the_action_is_the_web_services_and_binds_its_operation_identity(self):
        # E2 and E4.
        self.assertIn('decision-attest', fixture.endpoint.SERVICE_ONLY_ACTIONS)
        issue = self.make()
        body = json.dumps({'schema_version': 1, 'operation_id': 'op-body', 'decision': issue,
                           'digest': self.read(issue)['digest']})
        answer = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': 'ops', 'action': 'decision-attest',
                                                      'args': ['attest', body], 'operation_id': 'op-envelope'})
        self.assertEqual((2, 422), (answer['returncode'], answer['delegation_status']), answer)

    def test_an_unbound_key_cannot_write_or_read_through_the_action(self):
        issue = self.make()
        row = self.read(issue)
        body = json.dumps({'schema_version': 1, 'operation_id': 'op-x', 'decision': issue, 'digest': row['digest']})
        answer = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': 'ops', 'action': 'decision-attest',
                                                      'args': ['read', issue]})
        self.assertEqual((2, 403), (answer['returncode'], answer['delegation_status']), answer)
        answer = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': 'ops', 'action': 'decision-attest',
                                                      'args': ['attest', body], 'operation_id': 'op-x'})
        self.assertNotEqual(0, answer['returncode'], answer)
        with self.assertRaises(ValueError):
            fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': 'ops', 'action': 'decision-attest',
                                                 'args': ['attest', body], 'operation_id': 'op-x'},
                                     key_principal='person:ops', key_projects=['pp'])
        self.assertEqual('not-attested', self.read(issue)['attestation'])

    # -- Q1: the gate --------------------------------------------------------------------------------------------
    def test_a_contributors_reopen_and_close_are_refused_on_the_web_and_an_owners_restores_it(self):
        issue = self.make()
        self.attest(issue, 'pat')
        self.patch_task(issue, 'carl', {'status': 'closed'}, expected=403)
        self.assertEqual('open', self.status(issue))
        self.patch_task(issue, 'cleo', {'status': 'closed'})
        self.assertEqual('closed', self.read(issue)['attestation'])
        self.patch_task(issue, 'carl', {'status': 'open'}, expected=403)
        self.assertEqual('closed', self.status(issue))
        self.patch_task(issue, 'pat', {'status': 'open'})
        self.assertEqual('counts', self.read(issue)['attestation'])

    def test_an_owner_only_text_needs_a_person_owner_on_the_web(self):
        issue = self.make('Binds: pp\nOwner-only: yes')
        self.attest(issue, 'pat')
        for name in ('cleo', 'kite', 'carl'):
            self.patch_task(issue, name, {'status': 'closed'}, expected=403)
        self.patch_task(issue, 'pat', {'status': 'closed'})
        self.patch_task(issue, 'cleo', {'status': 'open'}, expected=403)
        self.assertEqual('closed', self.read(issue)['attestation'])

    def test_a_claim_is_a_status_change_on_an_attested_row(self):
        issue = self.make()
        self.attest(issue, 'pat')
        answer = self.web.request('POST', '/v1/projects/pp/tasks/%s/claim' % issue, {}, token=self.tokens['carl'],
                                  key='claim-' + uuid.uuid4().hex)
        self.assertEqual(403, answer.status, answer.data)

    def test_a_host_key_is_refused_every_change_an_operator_included(self):
        issue = self.make()
        self.attest(issue, 'pat')
        for actor in ('w1', 'ops'):
            for args in (('close', issue), ('update', issue, '--status', 'closed'), ('update', issue, '-s', 'closed'),
                         ('update', issue, '--type', 'task'), ('update', issue, '-t', 'task'),
                         ('update', issue, '--claim')):
                with self.subTest(actor=actor, args=args):
                    answer = self.host(actor, *args, '--json')
                    self.assertEqual((2, 403), (answer['returncode'], answer['delegation_status']), answer)
        self.assertEqual(('open', 'decision'), (self.status(issue), self.read(issue)['type']))
        labelled = self.make('Plain prose.', kind='task')
        self.assertEqual(0, self.host('w1', 'update', labelled, '--add-label', 'decision', '--json')['returncode'])
        self.attest(labelled, 'pat')
        answer = self.host('w1', 'update', labelled, '--remove-label', 'decision', '--json')
        self.assertEqual((2, 403), (answer['returncode'], answer['delegation_status']), answer)
        self.assertEqual('counts', self.read(labelled)['attestation'])
        # A reopen, a type or a label starting the row again: the same refusal, from the other side.
        closer = self.make()
        self.attest(closer, 'pat')
        self.patch_task(closer, 'pat', {'status': 'closed'})
        answer = self.host('w1', 'reopen', closer, '--json')
        self.assertEqual((2, 403), (answer['returncode'], answer['delegation_status']), answer)

    def test_a_host_write_naming_no_issue_is_refused_once_a_project_has_attestations(self):
        issue = self.make()
        self.attest(issue, 'pat')
        # Refused earlier on this route (bd would act on the last touched issue, kittrial-5bb.97's rule); the guard says
        # the same on its own, so it cannot depend on where it is called.
        with self.assertRaises(ValueError):
            self.host('w1', 'update', '--type', 'task', '--json')
        answer = fixture.endpoint._guard_attested_decision(self.root, self.project, 'pp', ['update', '--type', 'task', '--json'],
                                                           'w1', None)
        self.assertEqual((2, 422), (answer['returncode'], answer['delegation_status']), answer)

    def test_prose_on_a_decision_closes_on_the_host_as_it_always_did(self):
        issue = self.make('Per: the meeting of Tuesday')
        self.assertEqual(0, self.host('w1', 'close', issue, '--json')['returncode'])
        self.assertEqual(0, self.host('w1', 'reopen', issue, '--json')['returncode'])

    def test_journal_loss_does_not_open_the_gate_to_the_edit_and_reopen_sequence(self):
        # Review of r3, b1, the reviewer's sequence on real bd: the owner retires a decision, the journal goes, a plain host
        # actor edits the lines out, reopens and puts the text back, and the journal returns.
        text = 'Binds: pp\nOwner-only: yes'
        issue = self.make(text)
        self.attest(issue, 'pat')
        self.patch_task(issue, 'pat', {'status': 'closed'})
        journal = self.project / da.JOURNAL_DIR
        aside = self.project / (da.JOURNAL_DIR + '-aside')
        journal.rename(aside)
        self.addCleanup(lambda: aside.rename(journal) if aside.exists() else None)
        self.assertEqual(0, self.host('plain', 'update', issue, '--description', 'Prose only.', '--json')['returncode'])
        for args in (('reopen', issue), ('update', issue, '--status', 'open'), ('update', issue, '--type', 'task')):
            answer = self.host('plain', *args, '--json')
            self.assertEqual((2, 403), (answer['returncode'], answer['delegation_status']), (args, answer))
        self.assertEqual(0, self.host('plain', 'update', issue, '--description', text, '--json')['returncode'])
        aside.rename(journal)
        found = self.read(issue)
        self.assertEqual(('closed', 'closed'), (found['status'], found['attestation']))
        self.patch_task(issue, 'pat', {'status': 'open'})
        self.assertEqual('counts', self.read(issue)['attestation'])

    def test_a_bound_key_is_refused_like_an_unbound_one(self):
        # Reviewer's note: bound keys were not in their matrix. A key bound to a person and one bound to a lane, each with its
        # own actor in this project's registry, on an attested row and on an unattested one with the lines.
        registry = self.project / '.sessions.json'
        registry.write_text(json.dumps({'schema_version': 1, 'records': {},
                                        'owners': {'ops': 'person:ops', 'w1': 'lane:w1'}}), encoding='utf-8')
        self.addCleanup(lambda: registry.unlink() if registry.exists() else None)
        attested = self.make()
        self.attest(attested, 'pat')
        for issue in (attested, self.make()):
            for actor, principal in (('ops', 'person:ops'), ('w1', 'lane:w1')):
                for args in (('close', issue), ('update', issue, '--status', 'closed'), ('update', issue, '--type', 'task')):
                    with self.subTest(actor=actor, args=args):
                        answer = self.host(actor, *args, '--json', key_principal=principal, key_projects=['pp'])
                        self.assertEqual((2, 403), (answer['returncode'], answer['delegation_status']), answer)
        self.assertEqual('open', self.status(attested))

    def test_a_write_the_gate_cannot_stop_reads_no_row_for_it(self):
        with tempfile.TemporaryDirectory() as empty, patch.object(fixture.endpoint, '_native_anchor_rows',
                                                                  side_effect=AssertionError('read a row')):
            for args in (['update', 'pp-1', '--title', 'A title'], ['update', 'pp-1', '--assignee', 'w1'],
                         ['update', 'pp-1', '--add-label', 'bug'], ['comments', 'add', 'pp-1', 'A comment']):
                self.assertIsNone(fixture.endpoint._guard_attested_decision(self.root, Path(empty), 'pp', args, 'w1', None))

    def test_a_status_write_reads_its_row_once_for_both_guards(self):
        # The status guard's read is the gate's: a gated close costs no second read.
        issue = self.make()
        real = fixture.endpoint._native_anchor_rows
        with patch.object(fixture.endpoint, '_native_anchor_rows', side_effect=real) as read:
            answer = self.host('w1', 'close', issue, '--json')
        self.assertEqual((2, 403), (answer['returncode'], answer['delegation_status']), answer)
        self.assertEqual(1, read.call_count)
        # A retype has no status guard's read to use, so it reads for itself, and still answers.
        with patch.object(fixture.endpoint, '_native_anchor_rows', side_effect=real) as read:
            answer = self.host('w1', 'update', issue, '--type', 'task', '--json')
        self.assertEqual((2, 403), (answer['returncode'], answer['delegation_status']), answer)
        self.assertEqual(1, read.call_count)

    def test_a_text_edit_stays_open_to_a_contributor_and_an_ordinary_write_is_untouched(self):
        issue = self.make()
        self.attest(issue, 'pat')
        answer = self.host('w1', 'update', issue, '--description', 'A host edit.', '--json')
        self.assertEqual(0, answer['returncode'], answer)
        self.assertEqual('attestation-stale', self.read(issue)['attestation'])
        task = self.make(kind='task')
        for args in (('update', task, '--status', 'in_progress'), ('close', task), ('reopen', task),
                     ('update', task, '--type', 'bug')):
            with self.subTest(args=args):
                self.assertEqual(0, self.host('w1', *args, '--json')['returncode'])

    # -- pins, across two real projects ---------------------------------------------------------------------------
    def second_project(self):
        """oo: pat owns it too; vera does not belong to it. A decision of pp pins one of oo."""
        if getattr(type(self), 'second', None):
            return
        path = self.root / 'projects' / 'oo'
        path.mkdir(parents=True)
        init = subprocess.run([str(self.runtime.bd_path), 'init', '--server', '--external', '--server-host', '127.0.0.1',
                               '--server-port', str(self.runtime.port), '--server-user', 'root', '--prefix', 'oo',
                               '--database', 'oo', '--skip-agents', '--skip-hooks', '--non-interactive'],
                              cwd=str(path), env=fixture.admin.environment(self.root), capture_output=True, text=True, timeout=300)
        self.assertEqual(0, init.returncode, init.stderr or init.stdout)
        admin = self.web.admin_token()
        made = self.web.request('POST', '/v1/projects', {'project_id': 'oo', 'name': 'Other'}, token=admin)
        self.assertEqual(201, made.status, made.data)
        self.assertEqual(200, self.web.request('PUT', '/v1/projects/oo/members/' + self.ids['pat'], {'role': 'owner'},
                                               token=admin).status)
        for key, value in fixture.admin.PROJECT_SETTINGS:
            subprocess.run([str(self.runtime.bd_path), '--directory', str(path), '--sandbox', 'config', 'set', key, value],
                           env=fixture.admin.environment(self.root), capture_output=True, text=True, timeout=120)
        subprocess.run([str(self.runtime.bd_path), '--directory', str(path), '--sandbox', 'create', 'Merge Slot', '--id',
                        'oo-merge-slot', '--labels', 'gt:slot', '--json'], env=fixture.admin.environment(self.root),
                       capture_output=True, text=True, timeout=120)
        type(self).second = True

    def program_decision(self, text='Binds: pp\nOwner-only: no'):
        """A decision of oo, attested by pat, and the line its attestation read gives."""
        self.second_project()
        answer = fixture.endpoint.execute(self.root, {'project': 'oo', 'actor': 'ops', 'action': 'bd',
                                                      'args': ['create', 'A program decision ' + uuid.uuid4().hex[:6], '--type',
                                                               'decision', '--description', text, '--json']})
        self.assertEqual(0, answer['returncode'], answer)
        issue = json.loads(answer['stdout'])['id']
        read = self.web.request('GET', '/v1/projects/oo/decisions/%s/attestation' % issue, token=self.tokens['pat'])
        self.assertEqual(200, read.status, read.data)
        made = self.web.request('POST', '/v1/projects/oo/decisions/%s/attest' % issue, {'digest': read.data['digest']},
                                token=self.tokens['pat'], key='attest-' + uuid.uuid4().hex)
        self.assertEqual(201, made.status, made.data)
        read = self.web.request('GET', '/v1/projects/oo/decisions/%s/attestation' % issue, token=self.tokens['pat'])
        self.assertNotIn(read.data['digest'], read.data['adopt_line'])
        return issue, read.data['adopt_line'], read.data['authorise_line']

    def test_someone_who_cannot_read_the_program_cannot_attest_an_adoption(self):
        # cleo coordinates pp and is not a member of oo; the agent Kite holds only pp.
        _, adopt_line, authorise_line = self.program_decision()
        for line in (adopt_line, authorise_line):
            for name in ('cleo', 'kite'):
                with self.subTest(line=line[:10], name=name):
                    issue = self.make(line)
                    answer = self.attest(issue, name, expected=403)
                    self.assertIn('it does not exist, or you may not read it', answer['error']['message'])
                    self.assertEqual('not-attested', self.read(issue)['attestation'])
        # The owner of both can.
        issue = self.make(adopt_line)
        self.attest(issue, 'pat')
        self.assertEqual('counts', self.read(issue)['attestation'])

    def test_an_unreadable_program_a_missing_one_and_a_missing_decision_answer_alike(self):
        _, adopt_line, _ = self.program_decision()
        pin = adopt_line.rsplit('@', 1)[1]
        said = set()
        for program, issue_id in (('oo', 'oo-missing'), ('nosuchproject', 'nosuchproject-1')):
            issue = self.make('Adopts: decision:%s/%s@%s' % (program, issue_id, pin))
            answer = self.attest(issue, 'cleo', expected=403)
            said.add(answer['error']['message'].replace('decision:%s/%s' % (program, issue_id), 'X').replace(program, 'Y'))
        issue = self.make(adopt_line)
        answer = self.attest(issue, 'cleo', expected=403)
        said.add(answer['error']['message'].replace(adopt_line.split(' ')[1].split('@')[0], 'X').replace('oo', 'Y'))
        self.assertEqual(1, len(said), said)

    def test_a_reader_gets_a_conflict_for_other_text_and_the_right_digest_writes(self):
        program, adopt_line, _ = self.program_decision()
        wrong = self.make(adopt_line.rsplit('@', 1)[0] + '@' + 'd' * 64)
        answer = self.attest(wrong, 'pat', expected=409)
        self.assertIn('other text now', answer['error']['message'])
        # A program decision that does not count now cannot be adopted.
        self.assertEqual(0, fixture.endpoint.execute(self.root, {'project': 'oo', 'actor': 'w1', 'action': 'bd',
                                                                 'args': ['update', program, '--description', 'Edited.', '--json']})['returncode'])
        answer = self.attest(self.make(adopt_line), 'pat', expected=409)
        self.assertIn('does not count now', answer['error']['message'])

    def test_a_program_read_lost_later_keeps_the_adoption(self):
        # It was read at write. cleo reads oo for long enough to adopt, and is then removed from it.
        _, adopt_line, _ = self.program_decision()
        admin = self.web.admin_token()
        self.assertEqual(200, self.web.request('PUT', '/v1/projects/oo/members/' + self.ids['cleo'], {'role': 'viewer'},
                                               token=admin).status)
        adoption = self.make(adopt_line)
        self.attest(adoption, 'cleo')
        gone = self.web.request('DELETE', '/v1/projects/oo/members/' + self.ids['cleo'], token=admin)
        self.assertIn(gone.status, (200, 204), gone.data)
        self.assertEqual('counts', self.read(adoption)['attestation'])
        self.attest(self.make(adopt_line), 'cleo', expected=403)

    def test_the_operators_route_checks_the_pin_too(self):
        program, adopt_line, _ = self.program_decision()
        wrong = self.make(adopt_line.rsplit('@', 1)[0] + '@' + 'd' * 64)
        read = json.loads(self.admin('decision-attest-read', '--project', 'pp', '--decision', wrong).stdout)
        state = str(self.web.tmp / 'state.json')
        attest = ('decision-attest', '--project', 'pp', '--actor', 'ops', '--decision')
        refused = self.admin(*attest, wrong, '--digest', read['digest'], '--state', state)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn('other text now', refused.stderr)
        right = self.make(adopt_line)
        read = json.loads(self.admin('decision-attest-read', '--project', 'pp', '--decision', right).stdout)
        # Without the web state the program decision a person attested is not known to count: nothing is written.
        refused = self.admin(*attest, right, '--digest', read['digest'])
        self.assertNotEqual(0, refused.returncode)
        self.assertIn('does not count now', refused.stderr)
        self.assertEqual('not-attested', self.read(right)['attestation'])
        done = self.admin(*attest, right, '--digest', read['digest'], '--state', state)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual('counts', self.read(right)['attestation'])
        # The operator's true pin of a decision an operator attested needs no web state.
        answer = fixture.endpoint.execute(self.root, {'project': 'oo', 'actor': 'ops', 'action': 'bd',
                                                      'args': ['create', 'An operator decision', '--type', 'decision',
                                                               '--description', 'Binds: pp\nOwner-only: yes', '--json']})
        decided = json.loads(answer['stdout'])['id']
        read = json.loads(self.admin('decision-attest-read', '--project', 'oo', '--decision', decided).stdout)
        done = self.admin('decision-attest', '--project', 'oo', '--actor', 'ops', '--decision', decided, '--digest', read['digest'])
        self.assertEqual(0, done.returncode, done.stderr)
        line = json.loads(self.admin('decision-attest-read', '--project', 'oo', '--decision', decided).stdout)['adopt_line']
        adoption = self.make(line)
        read = json.loads(self.admin('decision-attest-read', '--project', 'pp', '--decision', adoption).stdout)
        done = self.admin(*attest, adoption, '--digest', read['digest'])
        self.assertEqual(0, done.returncode, done.stderr)

    # -- restore, through the real command -------------------------------------------------------------------------
    def test_restore_new_refuses_the_same_name_and_a_new_name_fails_closed(self):
        # Review of r3, b2: what the documents say, through admin.py itself. An attested row of pp, a backup, and restore-new.
        issue = self.make('Binds: pp\nOwner-only: no')
        made = self.attest(issue, 'pat')['record']
        # The scratch deployment has never been backed up: its destination, as add-project records it (admin.py).
        (self.root / 'backups').mkdir(exist_ok=True)
        init = subprocess.run([str(self.runtime.bd_path), '--directory', str(self.project), '--sandbox', 'backup', 'init',
                               str(self.root / 'backups' / 'pp')], env=fixture.admin.environment(self.root),
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(0, init.returncode, init.stderr)
        done = self.admin('backup', 'pp')
        self.assertEqual(0, done.returncode, done.stderr)
        same = self.admin('restore-new', 'pp', 'pp')
        self.assertNotEqual(0, same.returncode)
        self.assertIn('different destination', same.stderr)
        done = self.admin('restore-new', 'pp', 'pq')
        self.assertEqual(0, done.returncode, done.stderr)
        restored = self.root / 'projects' / 'pq'
        self.assertTrue(da.journaled(restored, made), 'the entry came back')
        shown = subprocess.run([str(self.runtime.bd_path), '--directory', str(restored), '--sandbox', 'show', issue, '--json',
                                '--include-comments'], env=fixture.admin.environment(self.root), capture_output=True, text=True,
                               timeout=120)
        row = next(item for item in json.loads(shown.stdout) if item.get('id') == issue)
        self.assertTrue(da.carries_attestation_comment(row), 'the comment came back')
        self.assertEqual('not-attested', da.standing(row, 'pq', None, ['ops'], restored)['state'])
        # And it stands gated, for everyone on a host route.
        answer = fixture.endpoint.execute(self.root, {'project': 'pq', 'actor': 'w1', 'action': 'bd',
                                                      'args': ['close', issue, '--json']})
        self.assertEqual((2, 403), (answer['returncode'], answer['delegation_status']), answer)

    # -- the operator's own route ---------------------------------------------------------------------------------
    def admin(self, *args):
        return subprocess.run([sys.executable, str(KIT / 'admin.py'), '--root', str(self.root), *args],
                              capture_output=True, text=True, encoding='utf-8', timeout=180,
                              env=dict(os.environ, PYTHONPATH=str(KIT)))

    def test_an_operator_attests_and_changes_through_admin_and_nobody_else_does(self):
        issue = self.make()
        read = json.loads(self.admin('decision-attest-read', '--project', 'pp', '--decision', issue).stdout)
        self.assertEqual('not-attested', read['attestation'])
        done = self.admin('decision-attest', '--project', 'pp', '--actor', 'ops', '--decision', issue, '--digest',
                          read['digest'])
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(('operator', 'ops'), (json.loads(done.stdout)['record']['via'],
                                               json.loads(done.stdout)['record']['author']))
        self.assertEqual(('counts', 'operator'), (self.read(issue)['attestation'], self.read(issue)['attested_as']))
        refused = self.admin('decision-attest', '--project', 'pp', '--actor', 'someone', '--decision', issue, '--digest',
                             read['digest'])
        self.assertNotEqual(0, refused.returncode)
        self.assertIn('configured operator', refused.stderr)        # O1: before anything is read or written
        for change, seen in (('close', 'closed'), ('reopen', 'counts')):
            done = self.admin('decision-change', '--project', 'pp', '--actor', 'ops', '--decision', issue, '--change', change)
            self.assertEqual(0, done.returncode, done.stderr)
            self.assertEqual(seen, self.read(issue)['attestation'])
        refused = self.admin('decision-change', '--project', 'pp', '--actor', 'someone', '--decision', issue, '--change', 'close')
        self.assertNotEqual(0, refused.returncode)
        self.assertIn('configured operator', refused.stderr)
        self.assertEqual('counts', self.read(issue)['attestation'])
        done = self.admin('decision-change', '--project', 'pp', '--actor', 'ops', '--decision', issue, '--change', 'type-task')
        self.assertEqual(0, done.returncode, done.stderr)
        # A task now, so the read says no such decision; with the type back its text matches its record again.
        answer = self.web.request('GET', '/v1/projects/pp/decisions/%s/attestation' % issue, token=self.tokens['vera'])
        self.assertEqual(404, answer.status, answer.data)
        done = self.admin('decision-change', '--project', 'pp', '--actor', 'ops', '--decision', issue, '--change', 'type-decision')
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual('counts', self.read(issue)['attestation'])


# -- the page --------------------------------------------------------------------------------------------------------
class PageTests(unittest.TestCase):

    def panel(self, read, attest=True):
        import shutil
        import test_http_web
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        code = """await import(process.argv[1]);
const view=await import(process.argv[2]);
const [read,attest]=JSON.parse(process.argv[3]);
const sent=[];
const node=view.attestationPanel(read,attest?async (digest)=>{sent.push(digest);}:null);
const host=document.createElement('div');
host.appendChild(node);
const button=host.querySelector('button');
if (button) { await button.listeners.click[0](); }
console.log(JSON.stringify({text:host.textContent,sent,button:!!button,
  markup:host.querySelectorAll('img').length+host.querySelectorAll('script').length,
  pre:host.querySelector('pre').textContent}));"""
        done = test_http_web.run_node_module(self, node, code, (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web/js/views/decision_attest.js').as_uri(), json.dumps([read, attest]))
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_the_panel_shows_the_exact_text_as_text_and_sends_the_digest_it_read(self):
        text = '<img src=x onerror=1>Binds: north\n  Owner-only: yes '
        out = self.panel({'type': 'decision', 'title': '<script>x</script>', 'description': text, 'digest': 'a' * 64,
                          'attestation': 'attestation-stale', 'attested_as': None})
        self.assertEqual((0, text, True, ['a' * 64]), (out['markup'], out['pre'], out['button'], out['sent']))
        self.assertIn('<script>x</script>', out['text'])
        self.assertIn('Stale', out['text'])

    def test_the_lines_to_copy_show_only_for_a_decision_that_counts(self):
        read = {'type': 'decision', 'title': 'T', 'description': '', 'digest': 'a' * 64, 'attestation': 'counts',
                'attested_as': 'owner', 'adopt_line': 'Adopts: decision:p/1@' + 'a' * 64,
                'authorise_line': 'Authorises: decision:p/1@' + 'a' * 64}
        out = self.panel(read, attest=False)
        self.assertIn(read['adopt_line'], out['text'])
        self.assertIn(read['authorise_line'], out['text'])
        self.assertNotIn(read['adopt_line'], self.panel(dict(read, attestation='attestation-stale'), attest=False)['text'])

    def test_each_state_reads_in_words_and_says_who(self):
        for state, word in (('counts', 'Counts'), ('closed', 'Closed'), ('not-attested', 'Not attested'),
                            ('attester-not-authorised', 'no longer holds')):
            with self.subTest(state=state):
                out = self.panel({'type': 'decision', 'title': 'T', 'description': '', 'digest': 'a' * 64,
                                  'attestation': state, 'attested_as': 'owner' if state == 'counts' else None}, attest=False)
                self.assertIn(word, out['text'])
                self.assertFalse(out['button'])
        self.assertIn('attested as owner', self.panel(
            {'type': 'decision', 'title': 'T', 'description': '', 'digest': 'a' * 64, 'attestation': 'counts',
             'attested_as': 'owner'}, attest=False)['text'])


# -- backups -------------------------------------------------------------------------------------------------------
class BackupTests(unittest.TestCase):
    """The real backup and restore. A record binds its project's name: the same name honours it, and a new name is a new
    project, which fails closed until an owner attests again (review of r2, b3)."""

    def setUp(self):
        import types
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'one'
        self.source = self.root / 'projects' / 'source'
        self.destination = self.root / 'projects' / 'destination'
        self.source.mkdir(parents=True)
        self.destination.mkdir()
        (self.root / 'backups' / 'source').mkdir(parents=True)
        self.bundle = self.root / 'backups' / 'source.coordination.json'
        fake = types.SimpleNamespace(flock=lambda *a, **k: None, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        patcher = patch.dict(sys.modules, {'fcntl': fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        import admin
        self.admin = admin
        for name, value in (('backup_lifecycle_void_family', {}), ('restore_lifecycle_void_family', False)):
            patcher = patch.object(admin, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # A real record of the source project, on its row as the backup carries it.
        self.row = {'id': 'source-1', 'issue_type': 'decision', 'status': 'open', 'title': 'A decision',
                    'description': 'Binds: north\nOwner-only: yes', 'labels': [], 'comments': []}
        self.record = da.make_record({'schema_version': 1, 'operation_id': 'op', 'decision': 'source-1',
                                      'digest': da.digest(self.row)}, project='source', author=OWNER, via='session',
                                     gated=True, at=AT[1])
        self.row['comments'].append({'id': 'c1', 'author': OWNER, 'text': da.body(self.record)})
        da.publish(self.source, self.record)
        with patch.object(self.admin, 'run_bd', return_value='synced'):
            self.admin.backup_project(self.root, 'source')

    def state_of(self, project):
        state = web_state()
        state['memberships'][project] = state['memberships'][PROJECT]
        return state

    def test_the_backup_carries_the_journal(self):
        sidecar = json.loads(self.bundle.read_text(encoding='utf-8'))
        self.assertIn(da.JOURNAL_DIR + '/' + self.record['sha256'] + '.json', sidecar['files'])

    def test_a_new_name_fails_closed_and_an_owner_attests_again(self):
        self.admin.restore_coordination(self.root, 'source', 'destination')
        self.assertTrue(da.journaled(self.destination, self.record), 'the entry came back')
        row = copy.deepcopy(self.row)
        self.assertEqual('counts', da.standing(row, 'source', self.state_of('source'), OPS, self.source)['state'])
        # The comment and the entry are both there, and they name another project.
        self.assertEqual('not-attested', da.standing(row, 'destination', self.state_of('destination'), OPS,
                                                     self.destination)['state'])
        # It stands gated, not open, and its level comes from its text.
        change = {'status': True, 'type': None, 'add': [], 'remove': [], 'set': None}
        for actor, by_service in (('w1', False), ('ops', False), (COORD, True), (CONTRIB, True)):
            self.assertEqual(403, da.gate(row, project='destination', journal=self.destination, actor=actor,
                                          state=self.state_of('destination'), by_service=by_service, command='close',
                                          change=change).status)
        self.assertIsNone(da.gate(row, project='destination', journal=self.destination, actor=OWNER,
                                  state=self.state_of('destination'), by_service=True, command='close', change=change))
        # An owner of the new project attests it again, and it counts.
        native = Native(row)
        native.writer = OWNER
        da.apply({'schema_version': 1, 'operation_id': 'op-again', 'decision': 'source-1', 'digest': da.digest(row)},
                 project='destination', journal=self.destination, run=native, author=OWNER, via='session', at=AT[2],
                 state=self.state_of('destination'), operators=OPS)
        self.assertEqual(('counts', 'owner'), tuple(da.standing(row, 'destination', self.state_of('destination'), OPS,
                                                                self.destination)[key] for key in ('state', 'role')))

    def test_the_library_honours_a_record_under_its_own_name(self):
        # No command does this today: restore-new refuses the same name (test_restore_new_refuses_the_same_name_...). This is
        # the record's own rule, that it is its project's name that decides, for whatever route ever restores one.
        other = Path(self.temp.name) / 'two'
        (other / 'projects' / 'source').mkdir(parents=True)
        (other / 'backups').mkdir()
        (other / 'backups' / 'source.coordination.json').write_bytes(self.bundle.read_bytes())
        self.admin.restore_coordination(other, 'source', 'source')
        restored = other / 'projects' / 'source'
        self.assertTrue(da.journaled(restored, self.record))
        found = da.standing(copy.deepcopy(self.row), 'source', self.state_of('source'), OPS, restored)
        self.assertEqual(('counts', 'owner', self.record['sha256']), (found['state'], found['role'], found['id']))

    def test_a_bad_entry_is_refused_on_restore(self):
        sidecar = json.loads(self.bundle.read_text(encoding='utf-8'))
        name = da.JOURNAL_DIR + '/' + self.record['sha256'] + '.json'
        sidecar['files'][name] = dict(sidecar['files'][name], digest='b' * 64)
        self.bundle.write_text(json.dumps(sidecar), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.admin.restore_coordination(self.root, 'source', 'destination')
        self.assertFalse(da.journaled(self.destination, self.record))

    def test_the_journal_is_validated_by_name(self):
        self.assertEqual(self.record, da.validate_entry(self.record, self.record['sha256'] + '.json'))
        with self.assertRaises(ValueError):
            da.validate_entry(self.record, 'other.json')


@unittest.skipUnless(os.name == 'posix', 'admin.py takes an absolute Linux --root')
class AdminRaceTests(unittest.TestCase):
    """An operator taken off the list between the first check and the lock: the check under the lock is what stops them."""

    def setUp(self):
        import contextlib
        import types
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        project = self.root / 'projects' / 'pp'
        (project / '.beads').mkdir(parents=True)
        (project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        fake = types.SimpleNamespace(flock=lambda *a, **k: None, LOCK_EX=2, LOCK_SH=1, LOCK_NB=4, LOCK_UN=8)
        patcher = patch.dict(sys.modules, {'fcntl': fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        import admin
        self.admin = admin
        self.listed = ['ops']
        self.calls = []
        row = {'id': 'pp-1', 'issue_type': 'decision', 'status': 'open', 'title': 'T', 'description': 'Text', 'labels': [],
               'comments': []}
        self.digest = da.digest(row)

        def run_bd(root, name, argv):
            argv = [word for word in argv if word not in ('--actor', 'ops')]
            self.calls.append(argv[0])
            if argv[0] == 'list':
                return json.dumps([row])
            if argv[0] == 'show':
                return json.dumps([row])
            return json.dumps({'id': 'c1'})

        @contextlib.contextmanager
        def config_lock(root):
            self.listed.clear()         # the operator is taken off the list while this waited for the lock
            yield
        for name, value in (('run_bd', run_bd), ('deployment_config_lock', config_lock),
                            ('operators', lambda root, strict=False: list(self.listed)),
                            ('decision_web_state', lambda root, named: None)):
            patcher = patch.object(admin, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_admin(self, *args):
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *args]):
            self.admin.main()

    def test_an_attestation_is_refused_under_the_lock(self):
        with self.assertRaises(ValueError) as refused:
            self.run_admin('decision-attest', '--project', 'pp', '--actor', 'ops', '--decision', 'pp-1', '--digest', self.digest)
        self.assertEqual([], self.listed, 'it reached the lock')
        self.assertIn('Only an owner or a coordinator', str(refused.exception))
        self.assertNotIn('comments', self.calls)
        self.assertEqual([], list((self.root / 'projects' / 'pp' / da.JOURNAL_DIR).glob('*.json')))

    def test_a_change_is_refused_under_the_lock(self):
        with self.assertRaises(ValueError) as refused:
            self.run_admin('decision-change', '--project', 'pp', '--actor', 'ops', '--decision', 'pp-1', '--change', 'close')
        self.assertEqual([], self.listed, 'it reached the lock')
        self.assertIn('operator', str(refused.exception))
        self.assertEqual([], [call for call in self.calls if call in ('close', 'reopen', 'update')])


if __name__ == '__main__':
    unittest.main()
