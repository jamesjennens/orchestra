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

AT = ['2026-01-01T00:00:%02dZ' % n for n in range(60)]      # stored server times, only ever ordered
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

    def write(self, native, author, digest=None, decision='north-1', state=None, operators=OPS):
        native.writer = author
        self.seq += 1
        return da.apply({'schema_version': 1, 'operation_id': 'op-%d' % self.seq, 'decision': decision,
                         'digest': digest or da.digest(native.rows['north-1'])}, project=PROJECT, journal=self.journal,
                        run=native, author=author, via=via_of(author), at=AT[self.seq],
                        state=web_state() if state is None else state, operators=operators)

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
        self.assertFalse(da.journal_in_use(self.journal))

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
        for text in ('Binds: north\nOwner-only: yes', 'Authorises: decision:prog/prog-1', 'Adopts: decision:prog/prog-1'):
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
                 (OWNER, 'Authorises: decision:prog/prog-1', True), (COORD, 'Authorises: decision:prog/prog-1', True),
                 (OWNER, 'Adopts: decision:prog/prog-1', True), ('ops', 'Adopts: decision:prog/prog-1', True),
                 (COORD, 'Adopts: decision:prog/prog-1', False), (OWNER, 'Plain prose.', False),
                 (COORD, 'Owner-only: maybe', True)]
        for author, text, expected in cases:
            with self.subTest(author=author, text=text):
                self.assertEqual(expected, self.write(Native(row_of(text)), author)['record']['owner_gated'])

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

    def test_a_row_with_no_honoured_attestation_is_not_gated(self):
        for row in (row_of(), row_of(issue_type='task')):
            for change in (self.STATUS, self.RETYPE):
                self.assertIsNone(self.gate(row, CONTRIB, change))
                self.assertIsNone(self.gate(row, 'w1', change, by_service=False))
        forged = row_of()
        self.attest(forged, OWNER, journal=False)
        self.assertIsNone(self.gate(forged, CONTRIB))

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
                task = self.make(kind='task')
                answer = self.host('w1', 'update', task, *args, '--json')
                self.assertEqual(0, answer['returncode'], answer)
                self.assertEqual('not-attested', self.read(task)['attestation'])

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
        labelled = self.make(kind='task')
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
        for change, seen in (('close', 'closed'), ('reopen', 'counts')):
            done = self.admin('decision-change', '--project', 'pp', '--actor', 'ops', '--decision', issue, '--change', change)
            self.assertEqual(0, done.returncode, done.stderr)
            self.assertEqual(seen, self.read(issue)['attestation'])
        refused = self.admin('decision-change', '--project', 'pp', '--actor', 'someone', '--decision', issue, '--change', 'close')
        self.assertNotEqual(0, refused.returncode)
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

    def setUp(self):
        import types
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
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

    def record(self):
        return da.make_record({'schema_version': 1, 'operation_id': 'op', 'decision': 'd-1', 'digest': 'a' * 64},
                              project='destination', author=OWNER, via='session', gated=False, at=AT[1])

    def test_a_restored_project_honours_the_attestation(self):
        record = self.record()
        da.publish(self.source, record)
        with patch.object(self.admin, 'run_bd', return_value='synced'):
            self.admin.backup_project(self.root, 'source')
        sidecar = json.loads(self.bundle.read_text(encoding='utf-8'))
        self.assertIn(da.JOURNAL_DIR + '/' + record['sha256'] + '.json', sidecar['files'])
        self.admin.restore_coordination(self.root, 'source', 'destination')
        self.assertTrue(da.journaled(self.destination, record))

    def test_a_bad_entry_is_refused_on_restore(self):
        record = self.record()
        da.publish(self.source, record)
        with patch.object(self.admin, 'run_bd', return_value='synced'):
            self.admin.backup_project(self.root, 'source')
        sidecar = json.loads(self.bundle.read_text(encoding='utf-8'))
        name = da.JOURNAL_DIR + '/' + record['sha256'] + '.json'
        sidecar['files'][name] = dict(sidecar['files'][name], digest='b' * 64)
        self.bundle.write_text(json.dumps(sidecar), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.admin.restore_coordination(self.root, 'source', 'destination')
        self.assertFalse(da.journaled(self.destination, record))

    def test_the_journal_is_validated_by_name(self):
        record = self.record()
        self.assertEqual(record, da.validate_entry(record, record['sha256'] + '.json'))
        with self.assertRaises(ValueError):
            da.validate_entry(record, 'other.json')


if __name__ == '__main__':
    unittest.main()
