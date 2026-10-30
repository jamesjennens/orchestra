"""kittrial-5bb.454: decisions across levels and suggestions (PROGRAM_DESIGN.md sections 6 and 9.5).

A program decision binds an area only when the area adopts it, and a row counts only with a live attestation of its
exact text (decision_attest.py; test_decision_attest.py proves the record and the gate). Here ``decision(id, author,
...)`` makes a row its ``author`` has attested, and the rules of section 6 are proved over those rows:
(a) every host write path refuses an HTTP-shaped actor and its look-alikes;
(c) authority is decided live at read: an attester who has lost the role stops counting;
(d) an agent never satisfies an owner-only rule, directly or through Per:.
"""
import copy
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import decision_attest as da
import program_decisions as pd
import program_isolation
import program_links as pl
import program_view
import test_bd_label_aliases as fixture
import trace_read
from reserved_comments import http_actor_like, refuse_http_actor
from test_program_links import LinkCase, item, statement

AT = '2026-01-01T00:00:00Z'      # stored grant times, passed through; never compared with a clock
HEX = 'a' * 16
P_OWNER, P_COORD = 'usr_' + 'f' * 16, 'usr_' + '9' * 16
OWNER, COORD, VIEWER, SUPER, CONTRIB = ('usr_' + c * 16 for c in 'abcde')
AGENT, SUPER_AGENT, P_AGENT = 'agent_' + '1' * 16, 'agent_' + '2' * 16, 'agent_' + '3' * 16
OPS = ['ops']


def web_state():
    """prog: P_OWNER owns it, P_COORD coordinates it with the agent P_AGENT granted. north: OWNER owns it, COORD
    coordinates it with AGENT granted, VIEWER views it, SUPER is a superuser who only views it (SUPER_AGENT granted),
    CONTRIB contributes."""
    users = {user: {'disabled': False} for user in (P_OWNER, P_COORD, OWNER, COORD, VIEWER, SUPER, CONTRIB)}
    users[SUPER]['superuser'] = True
    agents = {AGENT: {'id': AGENT, 'owner': COORD, 'enabled': True, 'projects': ['north']},
              SUPER_AGENT: {'id': SUPER_AGENT, 'owner': SUPER, 'enabled': True, 'projects': ['north']},
              P_AGENT: {'id': P_AGENT, 'owner': P_COORD, 'enabled': True, 'projects': ['prog']}}
    return {'users': users, 'agents': agents,
            'memberships': {'prog': {P_OWNER: 'owner', P_COORD: 'coordinator'},
                            'north': {OWNER: 'owner', COORD: 'coordinator', VIEWER: 'viewer', SUPER: 'viewer',
                                      CONTRIB: 'contributor'}},
            'coordinator_grants': {'north': {AGENT: {'granted_by': OWNER, 'granted_at': AT},
                                             SUPER_AGENT: {'granted_by': OWNER, 'granted_at': AT}},
                                   'prog': {P_AGENT: {'granted_by': P_OWNER, 'granted_at': AT}}}}


#: {issue: the attestation id of the row of that id attested last}, for the pins below.
_PINS = {}
#: {project: the directory its attestation journal is read from}; the rows of this file are attested into these.
_JOURNALS = tempfile.TemporaryDirectory()
J = {name: Path(_JOURNALS.name) / name for name in ('prog', 'north', 'south', 'east', 'west', 'ghost')}
for _path in J.values():
    _path.mkdir()
_SEQ = itertools.count(1)


def tearDownModule():
    _JOURNALS.cleanup()


def attest(row, author):
    """``author``'s record over the row's text now, as its comment and its journal entry. The row's project is its id's
    first part. A worker's name has no shape a record can carry: it stays unattested."""
    via = 'session' if author.startswith('usr_') else 'agent' if author.startswith('agent_') else 'operator'
    if '/' in author:
        return row
    seq = next(_SEQ)
    project = row['id'].split('-', 1)[0]
    at = '2026-01-01T%02d:%02d:%02dZ' % (seq // 3600, seq // 60 % 60, seq % 60)
    record = da.make_record({'schema_version': 1, 'operation_id': 'op-%d' % seq, 'decision': row['id'],
                             'digest': da.digest(row)}, project=project, author=author, via=via,
                            gated=False, at=at)
    row.setdefault('comments', []).append({'id': 'c%06d' % seq, 'author': author, 'text': da.body(record)})
    da.publish(J[project], record)
    _PINS[row['id']] = record['sha256']
    return row


def _pinned(line):
    """An Adopts: or Authorises: line written here with no pin gets the attestation id of the row of that id attested last,
    the line an adopter copies from the attestation read. A test about pins writes its own."""
    if re.fullmatch(r'(Adopts|Authorises): decision:[^/@]+/[^/@]+', line) is None:
        return line
    return '%s@%s' % (line, _PINS.get(line.rsplit('/', 1)[1], '0' * 64))


def bare(issue, *lines, first=None, **extra):
    """A decision row nobody has attested."""
    description = '\n'.join(([first] if first else []) + [_pinned(line) for line in lines])
    return dict({'id': issue, 'issue_type': 'decision', 'status': 'open', 'description': description,
                 'title': 'Decision ' + issue, 'comments': []}, **extra)


def decision(issue, author, *lines, first=None, **extra):
    """A decision row ``author`` has attested. Its ``created_by`` is someone else's on purpose: it is never read."""
    return attest(bare(issue, *lines, first=first, created_by='someone-who-made-the-row', **extra), author)


# -- the lines -------------------------------------------------------------------------------------------------
class LinesTests(unittest.TestCase):

    def test_each_line_reads_exactly(self):
        pin = 'a' * 64
        found = pd.lines(decision('prog-1', P_OWNER, 'Some prose.', 'Binds: north, south', 'Owner-only: yes',
                                  'Adopts: decision:prog/prog-0@' + pin, 'Per: decision:north/north-2',
                                  'Authorises: decision:prog/prog-0@' + pin))
        self.assertEqual((['north', 'south'], True, ('prog', 'prog-0', pin), ('north', 'north-2'),
                          ('prog', 'prog-0', pin), []),
                         (found['binds'], found['owner_only'], found['adopts'], found['per'], found['authorises'],
                          found['problems']))

    def test_a_repeat_or_a_malformed_line_is_a_problem_and_a_line_with_a_lead_is_prose(self):
        for text in ('Binds: north\nBinds: south', 'Binds: North', 'Binds: north,south', 'Binds: north, north',
                     'Owner-only: maybe', 'Owner-only: yes\nOwner-only: no', 'Adopts: decision:prog-0',
                     'Adopts: decision:/prog-0', 'Per: decision:north/', 'Per: task:north/x',
                     # A pin is the whole digest, and a line without one does not read.
                     'Adopts: decision:prog/prog-0', 'Authorises: decision:prog/prog-0',
                     'Adopts: decision:prog/prog-0@abc', 'Adopts: decision:prog/prog-0@' + 'A' * 64,
                     'Authorises: decision:prog/prog-0@' + 'a' * 63):
            with self.subTest(text=text):
                self.assertTrue(pd.lines({'description': text})['problems'])
        self.assertEqual((None, []), (pd.lines({'description': ' Binds: north'})['binds'],
                                      pd.lines({'description': ' Binds: north'})['problems']))

    def test_an_event_row_labelled_decision_is_no_decision(self):
        self.assertFalse(pd.is_decision({'id': 'x', 'issue_type': 'event', 'labels': ['decision']}))
        self.assertTrue(pd.is_decision({'id': 'x', 'issue_type': 'task', 'labels': ['decision']}))


# -- who wrote it, decided live ---------------------------------------------------------------------------------
class AuthorityTests(unittest.TestCase):

    def test_each_kind_of_author(self):
        state = web_state()
        cases = [(OWNER, 'owner'), (COORD, 'coordinator'), (VIEWER, None), (SUPER, None), (CONTRIB, None),
                 (AGENT, 'agent-coordinator'), (SUPER_AGENT, None), (OWNER + '/lane', None), ('ops', None),
                 ('Ops', None), ('opsx', None), ('USR_' + 'a' * 16, None), ('usr_' + 'A' * 16, None), ('', None),
                 (None, None), (P_OWNER, None)]
        for author, expected in cases:
            with self.subTest(author=author):
                self.assertEqual(expected, da.authority(author, 'north', state))

    def test_a_grant_counts_only_while_it_holds(self):
        state = web_state()
        for change in ('revoke', 'disable-agent', 'disable-owner', 'demote-owner', 'drop-project', 'issuer-demoted'):
            with self.subTest(change=change):
                changed = copy.deepcopy(state)
                if change == 'revoke':
                    del changed['coordinator_grants']['north'][AGENT]
                elif change == 'disable-agent':
                    changed['agents'][AGENT]['enabled'] = False
                elif change == 'disable-owner':
                    changed['users'][COORD]['disabled'] = True
                elif change == 'demote-owner':
                    changed['memberships']['north'][COORD] = 'viewer'
                elif change == 'drop-project':
                    changed['agents'][AGENT]['projects'] = []
                else:
                    changed['memberships']['north'][OWNER] = 'viewer'       # the grant's issuer is no owner now
                self.assertIsNone(da.authority(AGENT, 'north', changed))

    def test_a_disabled_or_demoted_person_stops_counting(self):
        # (c) live at read: the same author, before and after.
        state = web_state()
        self.assertEqual('owner', da.authority(OWNER, 'north', state))
        state['memberships']['north'][OWNER] = 'coordinator'
        self.assertEqual('coordinator', da.authority(OWNER, 'north', state))
        state['users'][OWNER]['disabled'] = True
        self.assertIsNone(da.authority(OWNER, 'north', state))


# -- program decisions and adoptions ------------------------------------------------------------------------------
class DecisionTests(unittest.TestCase):

    def test_who_may_write_a_program_decision(self):
        state = web_state()
        owner_only = ('Binds: north', 'Owner-only: yes')
        other = ('Binds: north', 'Owner-only: no')
        cases = [(P_OWNER, owner_only, []), ('ops', owner_only, []),
                 (P_COORD, owner_only, ['owner-only-not-by-owner']), (P_AGENT, owner_only, ['owner-only-not-by-owner']),
                 (P_COORD, other, []), (P_AGENT, other, []), ('ops', other, []),
                 (OWNER, other, ['attester-not-authorised']), ('someone', other, ['attester-not-authorised'])]
        for author, lines, flags in cases:
            with self.subTest(author=author, lines=lines):
                self.assertEqual(flags, pd.program_decision(decision('prog-1', author, *lines), 'prog', state,
                                                            OPS, J)['flags'])
        self.assertEqual(['malformed-lines'], pd.program_decision(decision('prog-2', P_OWNER, 'Binds: north'), 'prog',
                                                                  state, OPS, J)['flags'])
        # Nobody attested it: it binds nothing, whatever its native author says.
        self.assertEqual(['not-attested'], pd.program_decision(bare('prog-2', 'Binds: north', 'Owner-only: no',
                                                                    created_by=P_OWNER), 'prog', state, OPS, J)['flags'])
        # Q1: a repeat or a line that does not read binds nothing, though both lines are there (mutant D13).
        for lines in (('Binds: north', 'Owner-only: no', 'Owner-only: yes'), ('Binds: north', 'Binds: south',
                                                                              'Owner-only: no'),
                      ('Binds: north', 'Owner-only: no', 'Per: decision:north')):
            with self.subTest(lines=lines):
                found = pd.program_decision(decision('prog-6', P_OWNER, *lines), 'prog', state, OPS, J)
                self.assertEqual(['malformed-lines'], found['flags'])
                section = pd.decisions_section([decision('prog-6', P_OWNER, *lines)], 'prog', {'north': 'in'},
                                               {'north': [decision('north-1', OWNER, 'Adopts: decision:prog/prog-6')]},
                                               state, OPS, journals=J)
                self.assertEqual({}, section['items'][0]['areas'])
        self.assertIsNone(pd.program_decision(decision('prog-3', P_OWNER, 'Owner-only: yes'), 'prog', state, OPS, J))
        self.assertIsNone(pd.program_decision({'id': 'prog-4', 'issue_type': 'task', 'created_by': P_OWNER,
                                               'description': 'Binds: north\nOwner-only: no'}, 'prog', state, OPS, J))

    def adopt(self, decision_row, adoption_row, *more, state=None):
        program = pd.program_decision(decision_row, 'prog', state or web_state(), OPS, J)
        return pd.adoption(adoption_row, program, 'prog', 'north', [adoption_row, *more], state or web_state(), OPS, J)

    def test_every_row_of_the_adoption_table(self):
        owner_only = decision('prog-1', P_OWNER, 'Binds: north', 'Owner-only: yes')
        other = decision('prog-2', P_COORD, 'Binds: north', 'Owner-only: no')
        adopts = 'Adopts: decision:prog/prog-1'
        authorises = 'Authorises: decision:prog/prog-1'
        by_owner = decision('north-9', OWNER, 'The owner decided.', authorises)
        by_operator = decision('north-5', 'ops', 'An operator decided.', authorises)
        by_agent = decision('north-8', AGENT, 'An agent decided.', authorises)
        by_coord = decision('north-7', COORD, 'A coordinator decided.', authorises)
        cases = [
            ('owner adopts owner-only', owner_only, decision('north-1', OWNER, adopts), [], 'adopted'),
            ('operator adopts owner-only', owner_only, decision('north-1', 'ops', adopts), [], 'adopted'),
            ('coordinator with an owner Per:', owner_only,
             decision('north-1', COORD, adopts, 'Per: decision:north/north-9'), [by_owner], 'adopted'),
            ('agent with an owner Per:', owner_only,
             decision('north-1', AGENT, adopts, 'Per: decision:north/north-9'), [by_owner], 'adopted'),
            ('coordinator with an operator Per:', owner_only,
             decision('north-1', COORD, adopts, 'Per: decision:north/north-5'), [by_operator], 'adopted'),
            ('coordinator without Per:', owner_only, decision('north-1', COORD, adopts), [], 'adoption-invalid'),
            ('agent alone', owner_only, decision('north-1', AGENT, adopts), [], 'adoption-invalid'),
            ('agent with an agent Per:', owner_only,
             decision('north-1', AGENT, adopts, 'Per: decision:north/north-8'), [by_agent], 'adoption-invalid'),
            ('coordinator with a coordinator Per:', owner_only,
             decision('north-1', COORD, adopts, 'Per: decision:north/north-7'), [by_coord], 'adoption-invalid'),
            ('Per: names another project', owner_only,
             decision('north-1', COORD, adopts, 'Per: decision:south/north-9'), [by_owner], 'adoption-invalid'),
            ('Per: names no decision', owner_only,
             decision('north-1', COORD, adopts, 'Per: decision:north/north-6'),
             [{'id': 'north-6', 'issue_type': 'task', 'created_by': OWNER}], 'adoption-invalid'),
            ('superuser viewer adopts owner-only', owner_only, decision('north-1', SUPER, adopts), [],
             'attester-not-authorised'),
            ('nobody attested the adoption', owner_only, bare('north-1', adopts, created_by=OWNER), [], 'not-attested'),
            ('a closed adoption', owner_only, decision('north-1', OWNER, adopts, status='closed'), [], 'closed'),
            ('coordinator adopts other', other, decision('north-1', COORD, 'Adopts: decision:prog/prog-2'), [],
             'adopted'),
            ('agent adopts other', other, decision('north-1', AGENT, 'Adopts: decision:prog/prog-2'), [], 'adopted'),
            ('contributor adopts other', other, decision('north-1', CONTRIB, 'Adopts: decision:prog/prog-2'), [],
             'attester-not-authorised'),
            ('malformed adoption', other, decision('north-1', COORD, 'Adopts: decision:prog/prog-2',
                                                   'Per: decision:north'), [], 'adoption-invalid'),
        ]
        for label, program_row, adoption_row, more, expected in cases:
            with self.subTest(label):
                verdict, reason = self.adopt(program_row, adoption_row, *more)
                self.assertEqual(expected, verdict, reason)
                self.assertEqual(expected != 'adopted', bool(reason))

    def test_per_has_to_name_this_decision(self):
        # b2, the named tests: the lunch decision, and an owner decision authorising a different program decision.
        owner_only = decision('prog-1', P_OWNER, 'Binds: north', 'Owner-only: yes')
        adopts = 'Adopts: decision:prog/prog-1'
        lunch = decision('north-9', OWNER, 'Lunch is at noon')
        elsewhere = decision('north-8', OWNER, 'Authorises: decision:prog/prog-2')
        another_program = decision('north-7', OWNER, 'Authorises: decision:other/prog-1')
        for adopter in (COORD, AGENT):
            for cited in (lunch, elsewhere, another_program):
                with self.subTest(adopter=adopter, cited=cited['id']):
                    row = decision('north-1', adopter, adopts, 'Per: decision:north/' + cited['id'])
                    verdict, reason = self.adopt(owner_only, row, cited)
                    self.assertEqual('adoption-invalid', verdict)
                    self.assertIn('Authorises: decision:prog/prog-1', reason)
        matching = decision('north-6', OWNER, 'Authorises: decision:prog/prog-1')
        row = decision('north-1', COORD, adopts, 'Per: decision:north/north-6')
        self.assertEqual('adopted', self.adopt(owner_only, row, matching)[0])

    def test_per_stops_counting_with_the_owners_decision(self):
        owner_only = decision('prog-1', P_OWNER, 'Binds: north', 'Owner-only: yes')
        row = decision('north-1', COORD, 'Adopts: decision:prog/prog-1', 'Per: decision:north/north-6')
        cited = decision('north-6', OWNER, 'Authorises: decision:prog/prog-1')
        self.assertEqual('adopted', self.adopt(owner_only, row, cited)[0])
        for change, word in (({'description': 'Authorises: decision:prog/prog-1\nAn edit.'}, 'attestation-stale'),
                             ({'status': 'closed'}, 'closed')):
            verdict, reason = self.adopt(owner_only, row, dict(cited, **change))
            self.assertEqual('adoption-invalid', verdict, change)
            self.assertIn(word, reason)
        unattested = bare('north-6', 'Authorises: decision:prog/prog-1', created_by=OWNER)
        self.assertEqual('adoption-invalid', self.adopt(owner_only, row, unattested)[0])
        # P11: an attested row that is no decision any more is no owner's decision.
        not_one = decision('north-6', OWNER, 'Authorises: decision:prog/prog-1', labels=['decision'], issue_type='task')
        self.assertEqual('adopted', self.adopt(owner_only, row, not_one)[0])
        verdict, reason = self.adopt(owner_only, row, dict(not_one, labels=[]))
        self.assertEqual('adoption-invalid', verdict)
        self.assertIn('names no decision', reason)
        twice = decision('north-6', OWNER, 'Authorises: decision:prog/prog-1', 'Authorises: decision:prog/prog-2')
        verdict, reason = self.adopt(owner_only, row, twice)
        self.assertEqual('adoption-invalid', verdict)
        self.assertIn('do not read', reason)

    def test_an_adoption_pins_the_text_it_saw(self):
        # The coordinator's answer to the limit r2 reported: re-attested text leaves the area adoption-stale until it adopts again.
        first = decision('prog-1', P_OWNER, 'Binds: north', 'Owner-only: no')
        adopted = decision('north-1', COORD, 'Adopts: decision:prog/prog-1')
        self.assertEqual('adopted', self.adopt(first, adopted)[0])
        changed = attest(dict(first, description='Binds: north\nOwner-only: no\nOther text.', comments=list(first['comments'])),
                         P_OWNER)
        verdict, reason = self.adopt(changed, adopted)
        self.assertEqual('adoption-stale', verdict)
        self.assertIn('adopts again', reason)
        section = pd.decisions_section([changed], 'prog', {'north': 'in'}, {'north': [adopted]}, web_state(), OPS, journals=J)
        self.assertEqual('adoption-stale', section['items'][0]['areas']['north']['state'])
        # The area adopts again, and it binds.
        again = decision('north-1', COORD, 'Adopts: decision:prog/prog-1')
        self.assertEqual('adopted', self.adopt(changed, again)[0])

    def test_a_stale_pin_binds_nothing_whoever_attested_it(self):
        wrong = 'Adopts: decision:prog/prog-1@' + 'b' * 64
        for program_row in (decision('prog-1', P_OWNER, 'Binds: north', 'Owner-only: yes'),
                            decision('prog-1', P_COORD, 'Binds: north', 'Owner-only: no')):
            for adopter in (OWNER, 'ops', COORD, AGENT):
                with self.subTest(adopter=adopter, owner_only=program_row['description']):
                    self.assertEqual('adoption-stale', self.adopt(program_row, decision('north-1', adopter, wrong))[0])

    def test_another_attester_of_the_same_words_leaves_the_adoption_in_force(self):
        program_row = decision('prog-1', P_COORD, 'Binds: north', 'Owner-only: no')
        adopted = decision('north-1', COORD, 'Adopts: decision:prog/prog-1')
        pin = _PINS['prog-1']
        attest(program_row, P_OWNER)
        self.assertEqual('owner', pd.program_decision(program_row, 'prog', web_state(), OPS, J)['attested_as'])
        self.assertEqual(pin, pd.program_decision(program_row, 'prog', web_state(), OPS, J)['attestation_id'])
        self.assertEqual('adopted', self.adopt(program_row, adopted)[0])

    def test_an_authorisation_pins_the_text_the_owner_saw(self):
        # The same for Authorises: and its Per:: the adoption pins this text, the owner's line pinned other text.
        owner_only = decision('prog-1', P_OWNER, 'Binds: north', 'Owner-only: yes')
        row = decision('north-1', COORD, 'Adopts: decision:prog/prog-1', 'Per: decision:north/north-6')
        cited = decision('north-6', OWNER, 'Authorises: decision:prog/prog-1@' + 'b' * 64)
        verdict, reason = self.adopt(owner_only, row, cited)
        self.assertEqual('adoption-stale', verdict)
        self.assertIn('authorises it again', reason)
        cited = decision('north-6', OWNER, 'Authorises: decision:prog/prog-1')
        self.assertEqual('adopted', self.adopt(owner_only, row, cited)[0])

    def test_a_contributors_edit_of_an_owners_decision_binds_nothing(self):
        # b1 at the level of section 6: the program decision and the adoption, each edited.
        program_row = decision('prog-1', P_OWNER, 'Binds: north', 'Owner-only: no')
        edited = dict(program_row, description='Binds: north\nOwner-only: yes')
        found = pd.program_decision(edited, 'prog', web_state(), OPS, J)
        self.assertEqual(['attestation-stale'], found['flags'])
        row = decision('north-1', OWNER, 'Adopts: decision:prog/prog-1')
        self.assertEqual('attestation-stale', self.adopt(program_row, dict(row, description=row['description'] + '\nx'))[0])
        section = pd.decisions_section([edited], 'prog', {'north': 'in'}, {'north': [row]}, web_state(), OPS, journals=J)
        self.assertEqual({}, section['items'][0]['areas'])

    def test_an_agent_never_satisfies_owner_only(self):
        # (d) directly, through Per:, and as the program decision's own author.
        owner_only = decision('prog-1', P_OWNER, 'Binds: north', 'Owner-only: yes')
        for agent in (AGENT, SUPER_AGENT):
            verdict, _ = self.adopt(owner_only, decision('north-1', agent, 'Adopts: decision:prog/prog-1'))
            self.assertNotEqual('adopted', verdict, agent)
            for per_author in (AGENT, SUPER_AGENT):
                verdict, reason = self.adopt(owner_only, decision('north-1', agent, 'Adopts: decision:prog/prog-1',
                                                                  'Per: decision:north/north-8'),
                                             decision('north-8', per_author, 'Decided.',
                                                      'Authorises: decision:prog/prog-1'))
                self.assertNotEqual('adopted', verdict, (agent, per_author))
        self.assertEqual(['owner-only-not-by-owner'],
                         pd.program_decision(decision('prog-5', P_AGENT, 'Binds: north', 'Owner-only: yes'), 'prog',
                                             web_state(), OPS, J)['flags'])

    def test_a_demoted_author_stops_counting_at_the_next_read(self):
        # (c) the program decision's author and the adoption's author and the Per: author, each demoted in turn.
        owner_only = decision('prog-1', P_OWNER, 'Binds: north', 'Owner-only: yes')
        adoption = decision('north-1', COORD, 'Adopts: decision:prog/prog-1', 'Per: decision:north/north-9')
        per = decision('north-9', OWNER, 'The owner decided.', 'Authorises: decision:prog/prog-1')
        state = web_state()
        self.assertEqual('adopted', self.adopt(owner_only, adoption, per, state=state)[0])
        demoted = copy.deepcopy(state)
        demoted['memberships']['north'][OWNER] = 'coordinator'
        self.assertEqual('adoption-invalid', self.adopt(owner_only, adoption, per, state=demoted)[0])
        demoted = copy.deepcopy(state)
        demoted['memberships']['north'][COORD] = 'viewer'
        self.assertEqual('attester-not-authorised', self.adopt(owner_only, adoption, per, state=demoted)[0])
        demoted = copy.deepcopy(state)
        demoted['memberships']['prog'][P_OWNER] = 'coordinator'
        self.assertEqual(['owner-only-not-by-owner'], pd.program_decision(owner_only, 'prog', demoted, OPS, J)['flags'])


class SectionTests(unittest.TestCase):

    def test_the_decisions_section_and_its_area_states(self):
        state = web_state()
        program_rows = [decision('prog-1', P_OWNER, 'Binds: north, south, east, west', 'Owner-only: yes'),
                        decision('prog-2', P_COORD, 'Binds: north', 'Owner-only: no',
                                 first='Supersedes: decision:prog-0'),
                        decision('prog-0', P_COORD, 'Binds: north', 'Owner-only: no'),
                        decision('prog-3', P_COORD, 'No binding at all.')]
        north = [decision('north-1', OWNER, 'Adopts: decision:prog/prog-1'),
                 decision('north-2', COORD, 'Adopts: decision:prog/prog-0')]
        section = pd.decisions_section(program_rows, 'prog', {'north': 'in', 'south': 'in', 'east': 'not-checked'},
                                       {'north': north}, state, OPS, journals=J)
        by_id = {item['id']: item for item in section['items']}
        self.assertEqual(['prog-0', 'prog-1', 'prog-2'], sorted(by_id))
        self.assertEqual({'north': {'state': 'adopted', 'adoption': 'north-1'}, 'south': {'state': 'not-visible'},
                          'east': {'state': 'not-checked'}, 'west': {'state': 'not-in-program'}},
                         by_id['prog-1']['areas'])
        self.assertEqual({'north': {'state': 'superseded-upstream', 'adopts': 'prog-0'}}, by_id['prog-2']['areas'])
        self.assertTrue(by_id['prog-0']['superseded'])

    def test_a_row_that_does_not_count_marks_nothing_superseded(self):
        # b4: stale, closed, unattested and by someone without standing, each naming prog-0.
        old = decision('prog-0', P_COORD, 'Binds: north', 'Owner-only: no')
        says = 'Supersedes: decision:prog-0'
        fresh = decision('prog-2', P_COORD, 'Binds: north', 'Owner-only: no', first=says)
        for label, row in (('stale', dict(fresh, title='Edited')), ('closed', dict(fresh, status='closed')),
                           ('not attested', bare('prog-2', 'Binds: north', 'Owner-only: no', first=says)),
                           ('no standing', decision('prog-2', CONTRIB, 'Binds: north', 'Owner-only: no', first=says)),
                           ('malformed', decision('prog-2', P_COORD, 'Binds: north', first=says))):
            with self.subTest(label):
                section = pd.decisions_section([old, row], 'prog', {}, {}, web_state(), OPS, journals=J)
                self.assertFalse({i['id']: i for i in section['items']}['prog-0']['superseded'])
        section = pd.decisions_section([old, fresh], 'prog', {}, {}, web_state(), OPS, journals=J)
        self.assertTrue({i['id']: i for i in section['items']}['prog-0']['superseded'])

    def test_a_chain_does_not_pass_through_a_row_that_does_not_count(self):
        oldest = decision('prog-0', P_COORD, 'Binds: north', 'Owner-only: no')
        middle = bare('prog-1', 'Binds: north', 'Owner-only: no', first='Supersedes: decision:prog-0')
        newest = decision('prog-2', P_COORD, 'Binds: north', 'Owner-only: no', first='Supersedes: decision:prog-1')
        north = [decision('north-1', COORD, 'Adopts: decision:prog/prog-0')]
        section = pd.decisions_section([oldest, middle, newest], 'prog', {'north': 'in'}, {'north': north}, web_state(),
                                       OPS, journals=J)
        by_id = {i['id']: i for i in section['items']}
        self.assertEqual('not-adopted', by_id['prog-2']['areas']['north']['state'])
        self.assertFalse(by_id['prog-0']['superseded'])

    def test_only_a_counting_adoption_of_an_older_decision_is_superseded_upstream(self):
        old = decision('prog-0', P_COORD, 'Binds: north', 'Owner-only: no')
        new = decision('prog-2', P_COORD, 'Binds: north', 'Owner-only: no', first='Supersedes: decision:prog-0')
        for row, expected in ((decision('north-1', COORD, 'Adopts: decision:prog/prog-0'), 'superseded-upstream'),
                              (bare('north-1', 'Adopts: decision:prog/prog-0'), 'not-adopted'),
                              (decision('north-1', COORD, 'Adopts: decision:prog/prog-0', status='closed'), 'not-adopted')):
            with self.subTest(expected=expected, status=row['status']):
                section = pd.decisions_section([old, new], 'prog', {'north': 'in'}, {'north': [row]}, web_state(), OPS,
                                               journals=J)
                self.assertEqual(expected, {i['id']: i for i in section['items']}['prog-2']['areas']['north']['state'])

    def test_a_closed_decision_or_adoption_binds_nothing(self):
        # b4: state closed, for each.
        adopts = decision('north-1', OWNER, 'Adopts: decision:prog/prog-1')
        closed = decision('prog-1', P_OWNER, 'Binds: north', 'Owner-only: yes', status='closed')
        section = pd.decisions_section([closed], 'prog', {'north': 'in'}, {'north': [adopts]}, web_state(), OPS, journals=J)
        self.assertEqual((['closed'], 'closed', {}), (section['items'][0]['flags'], section['items'][0]['attestation'],
                                                      section['items'][0]['areas']))
        program_row = decision('prog-1', P_OWNER, 'Binds: north', 'Owner-only: yes')
        retired = decision('north-1', OWNER, 'Adopts: decision:prog/prog-1', status='closed')
        section = pd.decisions_section([program_row], 'prog', {'north': 'in'}, {'north': [retired]}, web_state(), OPS,
                                       journals=J)
        self.assertEqual('closed', section['items'][0]['areas']['north']['state'])
        # Another adoption still in force wins; one that does not count is said before a closed one.
        section = pd.decisions_section([program_row], 'prog', {'north': 'in'}, {'north': [retired, decision(
            'north-2', OWNER, 'Adopts: decision:prog/prog-1')]}, web_state(), OPS, journals=J)
        self.assertEqual({'state': 'adopted', 'adoption': 'north-2'}, section['items'][0]['areas']['north'])
        section = pd.decisions_section([program_row], 'prog', {'north': 'in'}, {'north': [retired, bare(
            'north-2', 'Adopts: decision:prog/prog-1')]}, web_state(), OPS, journals=J)
        self.assertEqual('not-attested', section['items'][0]['areas']['north']['state'])

    def test_an_area_outside_this_read_is_never_looked_at(self):
        # not-in-program and not-visible come from the program's records and the read set alone.
        class Untouchable(dict):
            def __getitem__(self, key):
                raise AssertionError('read %s' % key)
        section = pd.decisions_section([decision('prog-1', P_OWNER, 'Binds: south, west', 'Owner-only: yes')], 'prog',
                                       {'south': 'in'}, Untouchable(), web_state(), OPS, journals=J)
        self.assertEqual({'south': {'state': 'not-visible'}, 'west': {'state': 'not-in-program'}},
                         section['items'][0]['areas'])

    def test_pages_of_fifty(self):
        rows = [decision('prog-%03d' % n, P_COORD, 'Binds: north', 'Owner-only: no') for n in range(51)]
        first = pd.decisions_section(rows, 'prog', {}, {}, web_state(), OPS, journals=J)
        self.assertEqual((50, 51, 50), (len(first['items']), first['total'], first['next_offset']))
        self.assertEqual(['prog-050'], [i['id'] for i in pd.decisions_section(rows, 'prog', {}, {}, web_state(), OPS,
                                                                            50, J)['items']])

    def test_suggestions_open_oldest_first_with_their_from_line(self):
        rows = [{'id': 'prog-5', 'issue_type': 'task', 'labels': ['suggestion'], 'status': 'open',
                 'created_at': '2026-03-02T00:00:00Z', 'title': 'Later', 'description': 'From: decision:north/north-1'},
                {'id': 'prog-4', 'issue_type': 'task', 'labels': ['suggestion'], 'status': 'in_progress',
                 'created_at': '2026-03-01T00:00:00Z', 'title': 'Earlier', 'description': 'No source line'},
                {'id': 'prog-6', 'issue_type': 'task', 'labels': ['suggestion'], 'status': 'closed',
                 'created_at': '2026-02-01T00:00:00Z', 'title': 'Closed'},
                {'id': 'prog-7', 'issue_type': 'task', 'labels': [], 'status': 'open',
                 'created_at': '2026-01-01T00:00:00Z', 'title': 'Not one'},
                {'id': 'prog-8', 'issue_type': 'event', 'labels': ['suggestion'], 'status': 'open',
                 'created_at': '2026-01-01T00:00:00Z', 'title': 'An event'}]
        section = pd.suggestions_section(rows)
        self.assertEqual([('prog-4', None), ('prog-5', 'decision:north/north-1')],
                         [(s['id'], s['from']) for s in section['items']])
        many = [dict(rows[0], id='prog-%03d' % n, created_at='2026-03-%02dT00:00:00Z' % (n % 28 + 1)) for n in range(51)]
        self.assertEqual((50, 51), (len(pd.suggestions_section(many)['items']), pd.suggestions_section(many)['total']))


class TraceTests(unittest.TestCase):
    """Q6: an area's trace marks each Adopts: decision by the same rules, when the caller may read the program."""

    def snap(self, program_rows):
        north = [decision('north-1', OWNER, 'Adopts: decision:prog/prog-1'),
                 decision('north-2', AGENT, 'Adopts: decision:prog/prog-1'),
                 decision('north-3', COORD, 'Plain decision.')]
        others = (lambda project: SimpleNamespace(loaded=lambda: program_rows)) if program_rows is not None \
            else (lambda project: None)
        return trace_read.Snapshot(north, 'north', now=datetime(2026, 1, 2, tzinfo=timezone.utc), operators=OPS,
                                   others=others, authority_state=web_state(), journal_of=J.get)

    def test_the_trace_marks_adoptions(self):
        rows = {r['id']: r for r in trace_read.decisions_view(self.snap(
            [decision('prog-1', P_OWNER, 'Binds: north', 'Owner-only: yes')])) if r['kind'] == 'decision'}
        self.assertEqual('adopted', rows['north-1']['adopts']['state'])
        self.assertEqual('adoption-invalid', rows['north-2']['adopts']['state'])
        self.assertNotIn('adopts', rows['north-3'])

    def test_the_trace_uses_the_same_attestation_rule(self):
        unattested = {r['id']: r for r in trace_read.decisions_view(self.snap(
            [bare('prog-1', 'Binds: north', 'Owner-only: yes', created_by=P_OWNER)])) if r['kind'] == 'decision'}
        self.assertEqual('decision-does-not-count', unattested['north-1']['adopts']['state'])
        # No journal to read: nothing is honoured.
        program_row = decision('prog-1', P_OWNER, 'Binds: north', 'Owner-only: yes')
        no_journal = trace_read.Snapshot([decision('north-1', OWNER, 'Adopts: decision:prog/prog-1')], 'north',
                                         now=datetime(2026, 1, 2, tzinfo=timezone.utc), operators=OPS,
                                         others=lambda project: SimpleNamespace(loaded=lambda: [program_row]),
                                         authority_state=web_state())
        rows = {r['id']: r for r in trace_read.decisions_view(no_journal) if r['kind'] == 'decision'}
        self.assertEqual('decision-does-not-count', rows['north-1']['adopts']['state'])

    def test_without_the_program_an_adoption_reads_not_visible_and_nothing_else(self):
        rows = {r['id']: r for r in trace_read.decisions_view(self.snap(None)) if r['kind'] == 'decision'}
        self.assertEqual({'pointer': 'decision:prog/prog-1', 'state': 'not-visible'}, rows['north-1']['adopts'])
        self.assertEqual(rows['north-1']['adopts'], rows['north-2']['adopts'])


# -- (a) the trust boundary: every host write path ---------------------------------------------------------------
LOOKALIKES = ['usr_' + HEX, 'agent_' + HEX, 'usr_' + HEX + '/lane', 'usr_' + HEX + '/', 'USR_' + HEX, 'Agent_' + HEX,
              'usr_' + 'A' * 16, ' usr_' + HEX, 'usr_' + HEX + ' ', '\tusr_' + HEX, 'usr_' + HEX + '\n',
              'ｕｓｒ_' + HEX, 'uѕr_' + HEX, 'аgent_' + HEX, 'usr​_' + HEX, 'usr_x',
              'usr_', 'agent_smith', 'USR_x', 'usr_' + 'a' * 15, 'usr_' + 'g' * 16, 'usr_' + HEX + 'ff']
PLAIN = ['op', 'ops', 'session-a6fe9e84', 'codex-goomba', 'usr', 'users_x', 'my_usr_x', 'agentx', 'lane@host']


class HostActorTests(unittest.TestCase):

    def test_every_look_alike_is_refused_off_the_service_and_plain_names_pass(self):
        for actor in LOOKALIKES:
            with self.subTest(actor=ascii(actor)):
                with self.assertRaises(ValueError):
                    refuse_http_actor(actor, False)
        for actor in PLAIN:
            with self.subTest(actor=actor):
                refuse_http_actor(actor, False)
                self.assertFalse(http_actor_like(actor))
        refuse_http_actor('usr_' + HEX, True)      # the service's own launch: its descriptor rule applies instead

    def test_the_trust_rule_is_exact_whatever_reaches_it(self):
        # Even a look-alike that got into a row would not count: the person and agent shapes are exact.
        for actor in LOOKALIKES[2:]:
            with self.subTest(actor=ascii(actor)):
                self.assertIsNone(da.authority(actor, 'north', web_state()))

    def test_a_malformed_id_in_the_state_never_counts(self):
        # The person and agent shapes are exact even against a damaged or foreign state document that holds a
        # look-alike key with an owner's role, an enabled account, an agent and a grant (mutant D1).
        state = web_state()
        for odd in ('USR_' + HEX, 'usr_' + HEX + '/lane', 'usr_x', 'Agent_' + 'b' * 16, 'agent_x'):
            with self.subTest(odd=odd):
                state['users'][odd] = {'disabled': False}
                state['memberships']['north'][odd] = 'owner'
                state['agents'][odd] = {'id': odd, 'owner': OWNER, 'enabled': True, 'projects': ['north']}
                state['coordinator_grants']['north'][odd] = {'granted_by': OWNER, 'granted_at': AT}
                self.assertIsNone(da.authority(odd, 'north', state))

    def test_admin_refuses_a_look_alike_before_any_command(self):
        with tempfile.TemporaryDirectory() as root:
            for actor in ('usr_' + HEX, 'USR_' + HEX, 'usr_x', 'uѕr_' + HEX):
                with self.subTest(actor=ascii(actor)):
                    done = subprocess.run([sys.executable, str(KIT / 'admin.py'), '--root', root, 'handoff', 'alpha',
                                           '--actor', actor, '--file', 'missing.json'], capture_output=True, text=True,
                                          encoding='utf-8', errors='replace', env=dict(os.environ, PYTHONUTF8='1'))
                    self.assertNotEqual(0, done.returncode)
                    self.assertIn('Invalid operator identity: Actor', done.stderr)
            # The command's own --actor option, however it is spelled (an abbreviation reaches the same option), and
            # never a positional name: `verifiers add NAME` names a verifier, whose own refusal answers for it.
            abbreviated = subprocess.run([sys.executable, str(KIT / 'admin.py'), '--root', root, 'handoff', 'alpha',
                                          '--acto', 'usr_' + HEX, '--file', 'missing.json'], capture_output=True,
                                         text=True, encoding='utf-8', errors='replace', env=dict(os.environ, PYTHONUTF8='1'))
            self.assertIn('Invalid operator identity: Actor', abbreviated.stderr)
            named = subprocess.run([sys.executable, str(KIT / 'admin.py'), '--root', root, 'verifiers', 'add',
                                    'usr_' + HEX, '--actor', 'ops'], capture_output=True, text=True, encoding='utf-8',
                                   errors='replace', env=dict(os.environ, PYTHONUTF8='1'))
            self.assertNotIn('Invalid operator identity: Actor', named.stderr)


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointActorTests(unittest.TestCase):

    def test_every_action_on_an_unbound_and_a_bound_key_refuses_a_look_alike_first(self):
        for actor in LOOKALIKES:
            for key_projects in (None, frozenset({'alpha'})):
                for action in ('bd', 'ref', 'program', 'trace', 'session', 'work', 'review', 'capability',
                               'create-project', 'delivery'):
                    with self.subTest(actor=ascii(actor), key=key_projects, action=action):
                        with self.assertRaises(ValueError) as refused:
                            fixture.endpoint.execute(Path('/nonexistent-root'),
                                                     {'project': 'alpha', 'actor': actor, 'action': action,
                                                      'args': ['create', 'x']}, key_projects=key_projects)
                        message = str(refused.exception)
                        self.assertTrue('HTTP account' in message or 'plain ASCII' in message
                                        or 'only to the web service' in message, message)

    def test_the_dispatch_and_the_forced_commands_launch_refuse_it(self):
        import ssh_forced_command
        with tempfile.TemporaryDirectory() as root:
            for argv in ([sys.executable, str(KIT / 'endpoint.py'), '--root', root],
                         ssh_forced_command.endpoint_argv(sys.executable, str(KIT / 'endpoint.py'), root, ['alpha'],
                                                          'lane:alpha')):
                for actor in ('usr_' + HEX, 'USR_' + HEX, 'usr_x', ' usr_' + HEX, 'uѕr_' + HEX):
                    with self.subTest(argv=argv[3:], actor=ascii(actor)):
                        request = {'project': 'alpha', 'actor': actor, 'action': 'bd', 'args': ['create', 'x']}
                        done = subprocess.run(argv, input=json.dumps(request), capture_output=True, text=True,
                                              encoding='utf-8', timeout=120)
                        answer = json.loads(done.stdout)
                        self.assertEqual(2, answer['returncode'], answer)
                        self.assertTrue('HTTP account' in answer['stderr'] or 'plain ASCII' in answer['stderr'],
                                        answer['stderr'])


# -- (b) created_by cannot be changed after creation -----------------------------------------------------------
@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class CreatedByFlagTests(unittest.TestCase):

    def test_no_identity_flag_reaches_a_contributor_write(self):
        from reserved_comments import operator_only_in_args
        for args in (['create', 't', '--actor', 'usr_' + HEX], ['create', 't', '--actor=usr_' + HEX],
                     ['update', 'x-1', '--actor', 'ops'], ['comments', 'add', 'x-1', 'body', '--author', 'ops'],
                     ['close', 'x-1', '--actor', 'ops'], ['update', 'x-1', '--db', 'other']):
            with self.subTest(args=args):
                self.assertIsNotNone(operator_only_in_args(args))
        for flag in ('--actor', '--author', '--db', '--directory'):
            self.assertIn(flag, fixture.endpoint.FORBIDDEN)


# -- the web route ------------------------------------------------------------------------------------------------
class WebTests(LinkCase):
    """prog lists north and south (both linked) and ghost (no project). pat owns prog, cleo coordinates it; nora owns
    north and nick coordinates it with his agent Kite granted; vera reads north. Rows are given to the in-process
    backend (native_rows); the authors are the real web ids, decided against the service's own state."""

    def setUp(self):
        super().setUp()
        for area in ('north', 'south'):
            self.admit(area)
            self.join(area)
        self.admit('ghost')
        self.ids['nick'] = self.create_account(self.admin, 'nick', 'nick-password-1')
        self.assertEqual(200, self.request('PUT', '/v1/projects/north/members/%s' % self.ids['nick'],
                                           {'role': 'coordinator'}, token=self.admin).status)
        self.tokens['nick'] = self.login('nick', 'nick-password-1')[0]
        made = self.request('POST', '/v1/agents', {'name': 'Kite', 'working_directory': '/home/x/kite',
                                                   'projects': ['north']}, token=self.tokens['nick'])
        self.assertEqual(201, made.status, made.data)
        self.ids['kite'] = made.data['agent']['id']
        self.assertEqual(200, self.request('PUT', '/v1/projects/north/agents/%s/coordinator' % self.ids['kite'], {},
                                           token=self.tokens['nora']).status)
        self.backend.journals = J
        self.backend.native_rows = {
            'prog': [decision('prog-1', self.ids['pat'], 'Binds: north, south, ghost', 'Owner-only: yes')],
            'north': [decision('north-1', self.ids['nora'], 'Adopts: decision:prog/prog-1')]}

    def decisions(self, name):
        answer = self.request('GET', '/v1/projects/prog/program', token=self.tokens[name])
        self.assertEqual(200, answer.status, answer.data)
        return {item['id']: item for item in answer.data['decisions']['items']}

    def test_each_reader_sees_adoptions_only_where_it_reads(self):
        self.assertEqual({'north': {'state': 'adopted', 'adoption': 'north-1'}, 'south': {'state': 'not-visible'},
                          'ghost': {'state': 'not-visible'}}, self.decisions('vera')['prog-1']['areas'])
        self.assertEqual({'north': {'state': 'not-visible'}, 'south': {'state': 'not-visible'},
                          'ghost': {'state': 'not-visible'}}, self.decisions('carl')['prog-1']['areas'])

    def test_the_harness_hides_an_unreadable_areas_adoption_like_a_missing_ones(self):
        program_isolation.assert_adoptions_hidden(
            self, self.request, 'prog', 'south', 'ghost',
            {'program contributor': self.tokens['carl'], 'program owner': self.tokens['pat'],
             'reader of another area': self.tokens['vera'], 'agent without the area': self.tokens['wren']})

    def test_a_demoted_owner_stops_counting_at_the_next_read(self):
        # (c) through the route: nora is demoted in north, then pat in prog.
        self.assertEqual('adopted', self.decisions('vera')['prog-1']['areas']['north']['state'])
        self.assertEqual(200, self.request('PUT', '/v1/projects/north/members/%s' % self.ids['nora'],
                                           {'role': 'viewer'}, token=self.admin).status)
        self.assertEqual('attester-not-authorised', self.decisions('vera')['prog-1']['areas']['north']['state'])
        self.assertEqual(200, self.request('PUT', '/v1/projects/prog/members/%s' % self.ids['pat'],
                                           {'role': 'coordinator'}, token=self.admin).status)
        found = self.decisions('vera')['prog-1']
        self.assertEqual((['owner-only-not-by-owner'], {}), (found['flags'], found['areas']))

    def test_an_agent_never_satisfies_owner_only_through_the_route(self):
        # (d): Kite (granted in north) adopts alone, then with a Per: decision Kite wrote, then with one nora wrote.
        kite = self.ids['kite']
        for rows, expected in (
                ([decision('north-1', kite, 'Adopts: decision:prog/prog-1')], 'adoption-invalid'),
                ([decision('north-1', kite, 'Adopts: decision:prog/prog-1', 'Per: decision:north/north-2'),
                  decision('north-2', kite, 'Decided.', 'Authorises: decision:prog/prog-1')], 'adoption-invalid'),
                ([decision('north-1', kite, 'Adopts: decision:prog/prog-1', 'Per: decision:north/north-2'),
                  decision('north-2', self.ids['nora'], 'Lunch is at noon')], 'adoption-invalid'),
                ([decision('north-1', kite, 'Adopts: decision:prog/prog-1', 'Per: decision:north/north-2'),
                  decision('north-2', self.ids['nora'], 'Authorises: decision:prog/prog-1')], 'adopted')):
            self.backend.native_rows['north'] = rows
            self.assertEqual(expected, self.decisions('vera')['prog-1']['areas']['north']['state'])

    def test_suggestions_through_the_route(self):
        self.backend.native_rows['prog'].append({'id': 'prog-2', 'issue_type': 'task', 'labels': ['suggestion'],
                                                 'status': 'open', 'created_at': '2026-03-01T00:00:00Z',
                                                 'title': 'Raise the limit', 'description': 'From: decision:north/north-1'})
        answer = self.request('GET', '/v1/projects/prog/program', token=self.tokens['carl']).data
        self.assertEqual([('prog-2', 'decision:north/north-1')], [(s['id'], s['from'])
                                                                  for s in answer['suggestions']['items']])


# -- native: the program view's decisions on the real endpoint ----------------------------------------------------
@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class NativeDecisionTests(unittest.TestCase):
    """pp is the program, qq its area and rr an area the program only proposed (link records injected, as in .453's
    native tests); the decisions and adoptions are real rows written through the endpoint, and an operator attests
    them with the library call admin.py makes."""

    @classmethod
    def setUpClass(cls):
        cls.runtime = type('DecisionRuntime', (fixture.RealBdLabelAliasTests,), {})
        cls.runtime.setUpClass()
        r = cls.runtime
        cls.root = r.root
        qq = r.root / 'projects' / 'qq'
        qq.mkdir(parents=True)
        init = subprocess.run([str(r.bd_path), 'init', '--server', '--external', '--server-host', '127.0.0.1',
                               '--server-port', str(r.port), '--server-user', 'root', '--prefix', 'qq',
                               '--database', 'qq', '--skip-agents', '--skip-hooks', '--non-interactive'],
                              cwd=str(qq), env=fixture.admin.environment(r.root), capture_output=True, text=True,
                              timeout=300)
        if init.returncode:
            cls.runtime.tearDownClass()
            raise unittest.SkipTest('bd init of a second project failed: %s' % (init.stderr or init.stdout)[:200])

    @classmethod
    def tearDownClass(cls):
        cls.runtime.tearDownClass()

    def write(self, project, actor, *args):
        answer = fixture.endpoint.execute(self.root, {'project': project, 'actor': actor, 'action': 'bd',
                                                      'args': list(args)})
        self.assertEqual(0, answer['returncode'], answer)
        return json.loads(answer['stdout'])['id'] if '--json' in args else None

    def fake_links(self, rows, operators):
        ids = {str(row.get('id', '')).split('-', 1)[0] for row in rows if isinstance(row, dict)}
        if 'pp' in ids:
            return {'items': [item('program.area.qq', statement('Area', 'qq'), 'ops'),
                              item('program.area.rr', statement('Area', 'rr'), 'ops', state='draft-only')]}
        if 'qq' in ids:
            return {'items': [item(pl.MEMBER_OF, statement('Program', 'pp'), 'ops')]}
        return {'items': []}

    def attest_as_operator(self, project, issue):
        def run(argv):
            return fixture.admin.run_bd(self.root, project, ['--actor', 'ops', *argv])
        row = da.read_issue(run, issue)
        return da.apply({'schema_version': 1, 'operation_id': 'op-' + issue, 'decision': issue, 'digest': da.digest(row)},
                        project=project, journal=self.root / 'projects' / project, run=run, author='ops', via='operator',
                        at='2026-01-01T00:00:00Z', state=None, operators=['ops'])

    def view(self, **options):
        with patch('reference_records.links', self.fake_links), \
                patch.object(fixture.endpoint, 'configured_operators', lambda root: ['ops']):
            answer = fixture.endpoint.execute(self.root, {'action': 'program', 'project': 'pp', 'actor': 'ops',
                                                          'args': []}, **options)
        return {d['id']: d for d in json.loads(answer['stdout'])['decisions']['items']}, json.loads(answer['stdout'])

    def test_native_decisions_and_adoptions_on_the_real_endpoint(self):
        listed = self.write('pp', 'ops', 'create', 'Use the broker', '--type', 'decision', '--description',
                            'Binds: qq\nOwner-only: yes', '--json')
        self.write('pp', 'opx', 'create', 'Raise the limit', '--labels', 'suggestion', '--description',
                   'From: decision:qq/x-1', '--json')
        pinned = self.attest_as_operator('pp', listed)['record']['digest']
        adoption = self.write('qq', 'ops', 'create', 'We adopt it', '--type', 'decision', '--description',
                              'Adopts: decision:pp/%s@%s' % (listed, pinned), '--json')
        before = self.view()[0][listed]
        self.assertEqual('not-attested', before['areas']['qq']['state'])
        self.attest_as_operator('qq', adoption)
        items, whole = self.view()
        self.assertEqual({'state': 'adopted', 'adoption': adoption}, items[listed]['areas']['qq'])
        self.assertEqual(('counts', 'operator'), (items[listed]['attestation'], items[listed]['attested_as']))
        self.assertEqual(['From: decision:qq/x-1'.split(': ', 1)[1]], [s['from'] for s in whole['suggestions']['items']])
        # A key bound to pp alone: qq is not-checked, and nothing of it is read.
        self.assertEqual({'state': 'not-checked'}, self.view(key_projects=frozenset({'pp'}))[0][listed]['areas']['qq'])

    def test_an_actor_named_ops_over_the_endpoint_counts_for_nothing(self):
        # b3, the named test: an unbound key sends actor ops, for a program decision and for an adoption.
        program_row = self.write('pp', 'ops', 'create', 'From a host actor', '--type', 'decision', '--description',
                                 'Binds: qq\nOwner-only: yes', '--json')
        found = self.view()[0][program_row]
        self.assertEqual((['not-attested'], {}), (found['flags'], found['areas']))
        pinned = self.attest_as_operator('pp', program_row)['record']['digest']
        self.write('qq', 'ops', 'create', 'From a host actor', '--type', 'decision', '--description',
                   'Adopts: decision:pp/%s@%s' % (program_row, pinned), '--json')
        self.assertEqual('not-attested', self.view()[0][program_row]['areas']['qq']['state'])

    def test_an_area_whose_program_side_is_not_in_is_not_in_program(self):
        # Review mutant M7: the program's side alone decides; an entry only proposed is never treated as an area to
        # read, so it reads not-in-program, not not-visible or not-checked.
        issue = self.write('pp', 'ops', 'create', 'Binds a proposed area', '--type', 'decision', '--description',
                           'Binds: qq, rr\nOwner-only: no', '--json')
        self.attest_as_operator('pp', issue)
        areas = self.view()[0][issue]['areas']
        self.assertEqual({'state': 'not-in-program'}, areas['rr'])
        self.assertEqual('not-adopted', areas['qq']['state'])


if __name__ == '__main__':
    unittest.main()
