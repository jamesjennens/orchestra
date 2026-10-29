"""kittrial-5bb.446: program links, the nothing-granted rule and the isolation harness.

Slice 1 of docs/design/PROGRAM_DESIGN.md (sections 2, 8 and 9.1). A program is an ordinary project; an area joins
it through two reference entries with a decision authority, `program.area.<area>` in the program and
`program.member-of` in the area. Each side counts only when a person owner of its project (or an operator) accepted
it, checked live at every read. GET /v1/projects/{pid}/program-links reads them, and a link grants nothing.
"""
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import program_isolation
import program_links as pl
import reference_records as rr
import test_bd_label_aliases as fixture
import test_http_agents
from http_auth import HttpError, not_found
from test_reference_decision import RULE, DecisionCase
from test_reference_records import OPERATOR

AT = '2026-01-01T00:00:00Z'      # a stored acceptance time, passed through; never compared with a clock


def statement(label, project, state='in', rest='Joined by the owner.'):
    return '%s: %s\nState: %s\n\n%s' % (label, project, state, rest)


def item(key, text, by, state='accepted', authority=True, proposed=None):
    """One `ref links` item, as reference_records.links answers it."""
    record = None if state == 'draft-only' else {
        'revision': 2, 'statement': text, 'acceptance_state': state,
        'authority': {'type': 'decision', 'id': 'prog-1'} if authority else {'type': 'repo-path', 'path': 'x'}}
    return {'key': key, 'state': state, 'record': record, 'accepted_by': by if record else None,
            'accepted_at': AT if record else None, 'proposed_revision': proposed}


# -- the rules, on their own -----------------------------------------------------------------------------------
class WriteRuleTests(unittest.TestCase):
    """A program. key is written only as one of the two links, with its lines and a decision authority."""

    def payload(self, key, text, authority=True, operation='propose'):
        return {'operation': operation, 'key': key, 'statement': text,
                'authority': {'type': 'decision', 'id': RULE} if authority else {'type': 'repo-path', 'path': 'x'}}

    def test_each_bad_shape_is_refused_with_its_sentence(self):
        cases = [
            (self.payload('program.lead', statement('Area', 'north')), pl.KEYS_SENTENCE),
            (self.payload('program.area.n', statement('Area', 'n')), pl.KEYS_SENTENCE),          # not a host name
            (self.payload('program.area.north', statement('Area', 'north'), authority=False), pl.AUTHORITY_SENTENCE),
            (self.payload('program.area.north', 'Area: north'), 'opens with two lines'),
            (self.payload('program.area.north', statement('Area', 'south')), 'must name the area of the key, north'),
            (self.payload('program.area.north', statement('Area', 'north', state='maybe')), 'opens with two lines'),
            (self.payload('program.member-of', statement('Area', 'prog')), 'opens with two lines: "Program:'),
            (self.payload('program.member-of', 'State: in\nProgram: prog'), 'opens with two lines'),
        ]
        for payload, words in cases:
            problem = pl.write_problem(payload)
            self.assertIsNotNone(problem, payload)
            self.assertIn(words, problem, payload)

    def test_good_links_other_keys_and_accept_or_retire_pass(self):
        for payload in (self.payload('program.area.north', statement('Area', 'north')),
                        self.payload('program.area.north', statement('Area', 'north', state='left')),
                        self.payload('program.member-of', statement('Program', 'prog')),
                        self.payload('calendar.trading', 'anything', authority=False),
                        self.payload('program.lead', 'x', operation='accept'),
                        self.payload('program.area.north', 'x', operation='retire')):
            self.assertIsNone(pl.write_problem(payload), payload)


class SideRuleTests(unittest.TestCase):
    """Who makes a side count (Q1 A), checked live, and what each side's statement must say."""

    OWNER, COORD, OUT = 'usr_' + 'a' * 16, 'usr_' + 'b' * 16, 'usr_' + 'c' * 16

    def setUp(self):
        self.members = {'prog': {self.OWNER: 'owner', self.COORD: 'coordinator'}}
        self.users = {self.OWNER: {'disabled': False}, self.COORD: {'disabled': False}, self.OUT: {'disabled': False}}

    def status(self, entry, project='prog', other='north'):
        return pl.side_status(entry, project, other, self.members, self.users)

    def test_only_a_live_person_owner_or_an_operator_makes_a_side_count(self):
        text = statement('Area', 'north')
        self.assertEqual(('in', None), self.status(item('program.area.north', text, self.OWNER)))
        self.assertEqual(('in', None), self.status(item('program.area.north', text, OPERATOR)))
        for by in (self.COORD, self.OUT, 'agent_' + 'd' * 16, self.OWNER + '/label', None):
            self.assertEqual((None, 'accepted-by-a-non-owner'), self.status(item('program.area.north', text, by)), by)
        self.users[self.OWNER]['disabled'] = True
        self.assertEqual((None, 'accepted-by-a-non-owner'), self.status(item('program.area.north', text, self.OWNER)))
        self.users[self.OWNER]['disabled'] = False
        self.members['prog'][self.OWNER] = 'coordinator'          # the role is read live
        self.assertEqual((None, 'accepted-by-a-non-owner'), self.status(item('program.area.north', text, self.OWNER)))

    def test_the_record_itself_is_checked_again_at_every_read(self):
        def entry(text, **extra):
            return item('program.area.north', text, self.OWNER, **extra)
        self.assertEqual((None, 'no-record'), self.status(None))
        self.assertEqual((None, 'not-accepted'), self.status(entry(statement('Area', 'north'), state='draft-only')))
        self.assertEqual((None, 'no-decision-authority'), self.status(entry(statement('Area', 'north'), authority=False)))
        self.assertEqual((None, 'statement'), self.status(entry('Area north')))
        self.assertEqual((None, 'names-another-project'), self.status(entry(statement('Area', 'south'))))
        self.assertEqual((None, 'links-itself'), self.status(item('program.area.prog', statement('Area', 'prog'),
                                                                   self.OWNER), other='prog'))
        self.assertEqual(('left', None), self.status(entry(statement('Area', 'north', state='left'))))
        self.assertEqual(('left', None), self.status(entry(statement('Area', 'north'), state='superseded')))

    def test_the_link_states(self):
        self.assertEqual([pl.link_state(own, other) for own, other in
                          (('in', True), ('in', False), ('left', True), ('left', False), (None, True), (None, False))],
                         ['linked', 'one-sided', 'left', 'left', 'proposed', 'proposed'])


# -- the records, through reference_records (the endpoint's `ref` code) ---------------------------------------
class RecordTests(DecisionCase):
    """The write refusal and `ref links`, on the reference records' own native store."""

    def link(self, key, text, operation_id, **extra):
        fields = dict(key=key, title='Program link', statement=text, authority={'type': 'decision', 'id': RULE},
                      owner='person:orchestra-coordinator', tags=['program'], operation_id=operation_id)
        fields.update(extra)
        return self.propose(**fields)

    def test_a_bad_program_key_is_refused_and_nothing_is_written(self):
        before = len(self.native.rows) if hasattr(self.native, 'rows') else None
        with self.assertRaises(ValueError) as refused:
            self.link('program.lead', statement('Area', 'north'), 'prog-bad-1')
        self.assertIn(pl.KEYS_SENTENCE, str(refused.exception))
        with self.assertRaises(ValueError) as refused:
            self.link('program.area.north', 'no lines here', 'prog-bad-2')
        self.assertIn('opens with two lines', str(refused.exception))
        if before is not None:
            self.assertEqual(before, len(self.native.rows))

    def test_ref_links_lists_only_the_link_keys_with_who_accepted_them(self):
        self.propose(key='calendar.trading', title='Not a link', statement='Ordinary.',
                     authority={'type': 'decision', 'id': RULE}, owner='person:orchestra-coordinator', tags=[],
                     operation_id='plain-1')
        self.link('program.area.north', statement('Area', 'north'), 'prog-1')
        self.link('program.member-of', statement('Program', 'prog'), 'prog-2')
        digest = self.sha(1, key='program.area.north')
        self.accept(1, digest, key='program.area.north', operation_id='prog-accept-1')
        listed = rr.read(['links'], self.native, [OPERATOR])
        by_key = {entry['key']: entry for entry in listed['items']}
        self.assertEqual(['program.area.north', 'program.member-of'], sorted(by_key))
        north = by_key['program.area.north']
        self.assertEqual((north['state'], north['accepted_by'], north['record']['statement']),
                         ('accepted', OPERATOR, statement('Area', 'north')))
        member = by_key['program.member-of']
        self.assertEqual((member['state'], member['record'], member['proposed_revision']), ('draft-only', None, 1))
        with self.assertRaises(ValueError):
            rr.read(['links', 'extra'], self.native, [OPERATOR])


# -- the route ------------------------------------------------------------------------------------------------
class LinkCase(test_http_agents.AgentHarness):
    """prog is the program, north and south are areas. pat owns prog, nora owns north, sam owns south; cleo
    coordinates prog; carl contributes to prog only; vera is in prog and in north; otto is in nothing. Wren is
    vera's agent, granted prog only. The link records come from an injected `reference_links`."""

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        for pid in ('prog', 'north', 'south'):
            made = self.request('POST', '/v1/projects', {'project_id': pid, 'name': pid.title()}, token=self.admin)
            self.assertEqual(201, made.status, made.data)
        self.ids, self.tokens = {}, {}
        roles = {'pat': {'prog': 'owner'}, 'nora': {'north': 'owner'}, 'sam': {'south': 'owner'},
                 'cleo': {'prog': 'coordinator'}, 'carl': {'prog': 'contributor'},
                 'vera': {'prog': 'contributor', 'north': 'viewer'}, 'otto': {}}
        for name, places in roles.items():
            self.ids[name] = self.create_account(self.admin, name, name + '-password-1')
            for pid, role in places.items():
                self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s' % (pid, self.ids[name]),
                                                   {'role': role}, token=self.admin).status)
            self.tokens[name] = self.login(name, name + '-password-1')[0]
        wren = self.request('POST', '/v1/agents', {'name': 'Wren', 'working_directory': '/home/x/wren',
                                                   'projects': ['prog']}, token=self.tokens['vera'])
        self.assertEqual(201, wren.status, wren.data)
        self.tokens['wren'] = wren.data['credential']['secret']
        self.links, self.reads = {}, []
        self.backend.reference_links = self.fake_links

    def fake_links(self, project_id):
        self.reads.append(project_id)
        if project_id not in self.links:
            raise not_found('Unknown or uninitialized project')
        return {'schema_version': 1, 'items': list(self.links[project_id])}

    def admit(self, area, by=None, text=None, **extra):
        self.links.setdefault('prog', []).append(
            item('program.area.' + area, text or statement('Area', area), by or self.ids['pat'], **extra))

    def join(self, area, by=None, program='prog', **extra):
        self.links.setdefault(area, []).append(
            item(pl.MEMBER_OF, statement('Program', program), by or self.ids['nora' if area == 'north' else 'sam'],
                 **extra))

    def read(self, token=None, project='prog', query=''):
        return self.request('GET', '/v1/projects/%s/program-links%s' % (project, query),
                            token=token or self.tokens['carl'])

    def rows(self, token=None, project='prog'):
        answer = self.read(token, project)
        self.assertEqual(200, answer.status, answer.data)
        return {row['area']: row for row in answer.data['as_program']['items']}, answer.data


class StateTests(LinkCase):

    def test_linked_only_when_both_owners_accepted(self):
        self.admit('north')
        self.join('north')
        rows, data = self.rows()
        self.assertEqual(('linked', {'counts': True}), (rows['north']['state'], rows['north']['area_side']))
        self.assertEqual(data['as_program']['counts'], {'linked': 1, 'one-sided': 0, 'left': 0, 'proposed': 0})
        self.assertEqual(rows['north']['program_side']['accepted_by_name'], 'pat')

    def test_one_sided_is_shown_and_never_counted_as_a_member(self):
        self.admit('north')
        for area_side in ([], [item(pl.MEMBER_OF, statement('Program', 'prog'), None, state='draft-only')],
                          [item(pl.MEMBER_OF, statement('Program', 'prog'), self.ids['vera'])],     # not north's owner
                          [item(pl.MEMBER_OF, statement('Program', 'prog'), 'agent_' + 'e' * 16)]):
            self.links['north'] = area_side
            rows, data = self.rows()
            self.assertEqual(('one-sided', {'counts': False}), (rows['north']['state'], rows['north']['area_side']))
            self.assertEqual(0, data['as_program']['counts']['linked'])

    def test_left_from_the_program_side_and_one_sided_when_the_area_leaves(self):
        self.admit('north', text=statement('Area', 'north', state='left'))
        self.join('north')
        rows, _ = self.rows()
        self.assertEqual(('left', None), (rows['north']['state'], rows['north']['area_side']))
        self.links['prog'] = [item('program.area.north', statement('Area', 'north'), self.ids['pat'], state='superseded')]
        self.assertEqual('left', self.rows()[0]['north']['state'])
        self.links['prog'] = [item('program.area.north', statement('Area', 'north'), self.ids['pat'])]
        self.links['north'] = [item(pl.MEMBER_OF, statement('Program', 'prog', state='left'), self.ids['nora'])]
        self.assertEqual('one-sided', self.rows()[0]['north']['state'])

    def test_proposed_reads_no_area(self):
        self.admit('north', state='draft-only', proposed=1)
        self.admit('south', by=self.ids['cleo'])                     # a coordinator accepted it: it does not count
        self.join('north')
        self.join('south')
        rows, data = self.rows()
        self.assertEqual([(rows[a]['state'], rows[a]['program_side']['reason']) for a in ('north', 'south')],
                         [('proposed', 'not-accepted'), ('proposed', 'accepted-by-a-non-owner')])
        self.assertEqual(['prog'], self.reads)
        self.assertEqual(2, data['as_program']['counts']['proposed'])

    def test_the_owner_is_checked_live_at_every_read(self):
        self.admit('north')
        self.join('north')
        self.assertEqual('linked', self.rows()[0]['north']['state'])
        self.assertEqual(200, self.request('PUT', '/v1/projects/north/members/%s' % self.ids['nora'],
                                           {'role': 'viewer'}, token=self.admin).status)
        self.assertEqual('one-sided', self.rows()[0]['north']['state'])
        self.assertEqual(200, self.request('PUT', '/v1/projects/prog/members/%s' % self.ids['pat'],
                                           {'role': 'contributor'}, token=self.admin).status)
        self.assertEqual(('proposed', 'accepted-by-a-non-owner'),
                         (self.rows()[0]['north']['state'], self.rows()[0]['north']['program_side']['reason']))

    def test_an_operator_acceptance_counts(self):
        self.admit('north', by=OPERATOR)
        self.join('north', by=OPERATOR)
        self.assertEqual('linked', self.rows()[0]['north']['state'])

    def test_a_program_cannot_link_itself(self):
        self.admit('prog')
        rows, _ = self.rows()
        self.assertEqual(('proposed', 'links-itself'), (rows['prog']['state'], rows['prog']['program_side']['reason']))

    def test_the_area_sees_its_own_side_and_one_fact_about_the_program(self):
        self.join('north')
        answer = self.read(self.tokens['nora'], project='north')
        self.assertEqual(200, answer.status, answer.data)
        self.assertEqual((answer.data['as_area']['program'], answer.data['as_area']['state'],
                          answer.data['as_area']['program_side']), ('prog', 'one-sided', {'counts': False}))
        self.admit('north')
        answer = self.read(self.tokens['nora'], project='north')
        self.assertEqual(('linked', {'counts': True}), (answer.data['as_area']['state'],
                                                       answer.data['as_area']['program_side']))
        self.assertEqual([], answer.data['as_program']['items'])


class DisclosureTests(LinkCase):

    def test_the_answer_is_the_same_for_readers_with_and_without_the_area(self):
        # vera can read north, carl cannot: the program-links answer is byte-identical for both (Q2).
        self.admit('north')
        self.join('north')
        answers = []
        for name in ('vera', 'carl', 'pat'):
            body = program_isolation.without_request_id(self.read(self.tokens[name]).data)
            body.pop('generated_at')
            for row in body['as_program']['items']:
                self.assertEqual(set(row['area_side']), {'counts'})          # one fact, no revision or author
            answers.append(json.dumps(body, sort_keys=True))
        self.assertEqual(1, len(set(answers)))

    def test_a_missing_an_unreadable_and_an_unrelated_area_answer_alike(self):
        self.admit('ghost')                                            # no such project
        self.admit('north')                                            # exists, has not joined
        self.admit('south')
        self.join('south', program='other')                            # joined another program
        rows, _ = self.rows()
        shapes = {area: (row['state'], row['area_side'], sorted(row['program_side']))
                  for area, row in rows.items()}
        self.assertEqual(1, len(set(json.dumps(shape) for shape in shapes.values())), shapes)
        self.assertEqual('one-sided', rows['ghost']['state'])

    def test_outsiders_learn_nothing_about_an_area(self):
        # The harness of sections 2.3 and 8: a program member who is not in the area, someone in nothing, and an
        # agent whose owner is in the area but whose grant list omits it all get the answer for no project at all.
        self.admit('north')
        self.join('north')
        program_isolation.assert_outsiders_see_nothing(
            self, self.request, 'north', 'nowhere',
            {'program member': self.tokens['carl'], 'nobody': self.tokens['otto'], 'agent without the grant':
             self.tokens['wren']})

    def test_a_non_member_of_the_program_reads_nothing_and_causes_no_area_read(self):
        self.admit('north')
        self.join('north')
        for token in (self.tokens['otto'], self.tokens['nora']):
            answer = self.read(token)
            self.assertEqual(404, answer.status)
        self.assertEqual([], self.reads)


class BoundTests(LinkCase):

    def test_a_page_reads_at_most_50_areas(self):
        # The addition at VERIFIED: 51 admitted areas make two pages, and no page reads more than 50 areas.
        areas = ['area%02d' % n for n in range(51)]
        for area in areas:
            self.admit(area)
        answer = self.read()
        self.assertEqual(200, answer.status, answer.data)
        self.assertEqual((50, 51), (len(answer.data['as_program']['items']), answer.data['as_program']['total']))
        self.assertEqual(['prog'] + areas[:50], self.reads)
        cursor = answer.data['as_program']['next_cursor']
        self.assertIsNotNone(cursor)
        self.reads.clear()
        second = self.read(query='?cursor=' + cursor)
        self.assertEqual([areas[50]], [row['area'] for row in second.data['as_program']['items']])
        self.assertEqual(['prog', areas[50]], self.reads)
        self.assertIsNone(second.data['as_program']['next_cursor'])
        self.assertEqual(422, self.read(query='?limit=51').status)


class AcceptGateTests(LinkCase):
    """Q1 A: the web accept and retire of a program. key are a person owner's own act; the write is never sent."""

    def setUp(self):
        super().setUp()
        self.invoked = []
        self.backend.RECORDS = True
        self.backend.invoke = lambda route, principal, pid, payload, key, **kw: (
            self.invoked.append((route, payload['body'].get('key'))) or {'key': payload['body'].get('key')})
        agent = self.request('POST', '/v1/agents', {'name': 'Rook', 'working_directory': '/home/x/rook',
                                                    'projects': ['prog']}, token=self.tokens['cleo'])
        self.assertEqual(201, agent.status, agent.data)
        self.tokens['rook'] = agent.data['credential']['secret']
        self.assertEqual(200, self.request('PUT', '/v1/projects/prog/agents/%s/coordinator' % agent.data['agent']['id'],
                                           {}, token=self.tokens['pat']).status)

    def accept(self, token, key='program.area.north', what='accept'):
        body = ({'expected_revision': 1, 'record_sha256': 'a' * 64, 'evidence': 'The owner decided.',
                 'decision_id': 'prog-1'} if what == 'accept' else {'successor': 'calendar.next'})
        return self.request('POST', '/v1/projects/prog/references/%s/%s' % (key, what), body, token=token,
                            key='gate-%s-%d' % (what, len(self.invoked) + id(token) % 997))

    def test_only_a_person_owner_accepts_or_retires_a_program_link(self):
        for name in ('cleo', 'rook'):
            for what in ('accept', 'retire'):
                refused = self.accept(self.tokens[name], what=what)
                self.assertEqual(403, refused.status, (name, what, refused.data))
                self.assertIn('Only a person owner of this project accepts or retires a program link',
                              refused.data['error']['message'])
        self.assertEqual([], self.invoked)
        self.assertEqual(404, self.accept(self.tokens['otto']).status)       # an outsider: the usual 404 first
        made = self.accept(self.tokens['pat'])
        self.assertNotEqual(403, made.status, made.data)
        self.assertEqual([('references.accept', 'program.area.north')], self.invoked)

    def test_other_keys_keep_their_rules(self):
        made = self.accept(self.tokens['cleo'], key='calendar.trading')
        self.assertNotEqual(403, made.status, made.data)
        self.assertEqual([('references.accept', 'calendar.trading')], self.invoked)


# -- end to end, with the real endpoint.py and a real bd (POSIX) -----------------------------------------------
from test_coordinator_rules_endpoint import RulesHelpers  # noqa: E402
import test_http_review_fixes as fixes  # noqa: E402
from test_web_records import _review_by  # noqa: E402


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class NativeLinkTests(RulesHelpers, unittest.TestCase):
    """One program project on the real endpoint: the write refusal, `ref links` through the endpoint, the owner-only
    acceptance and the read. (The two-project link is read through the same code with an injected read above.)"""

    def setUp(self):
        super().setUp()
        root = self.root

        class Harness(fixes.Harness):
            def make_backend(h):
                return http_service.EndpointBackend(sys.executable, str(KIT / 'endpoint.py'), str(root),
                                                    service=h.service)

        self.harness = Harness(methodName='runTest')
        self.harness.setUp()
        self.addCleanup(self.harness._stop_server)
        self.owner = self.harness.admin_token()
        made = self.harness.request('POST', '/v1/projects', {'project_id': 'pp', 'name': 'Native Program'},
                                    token=self.owner)
        self.assertEqual(201, made.status, made.data)

    def test_native_a_program_link_from_proposal_to_the_read(self):
        self.deployment(operators=['ops'], coordinator_writes=True, independent_approval=False)
        h = self.harness
        created = self.r.bd('create', 'Admit north', '--type', 'decision', '--json', actor='op')
        self.assertEqual(0, created.returncode, created.stderr)
        decision = json.loads(created.stdout)['id']
        uid = h.create_account(self.owner, 'pl-coord', 'pl-password-1')
        self.assertEqual(200, h.request('PUT', '/v1/projects/pp/members/%s' % uid, {'role': 'coordinator'},
                                        token=self.owner).status)
        coord = h.login('pl-coord', 'pl-password-1')[0]
        refs = '/v1/projects/pp/references'
        body = {'key': 'program.lead', 'title': 'Not a link', 'statement': 'Area: north\nState: in',
                'authority': {'type': 'decision', 'id': decision}, 'owner': 'person:maintainer',
                'review_by': _review_by(), 'tags': ['program'], 'decisions': []}
        refused = h.request('POST', refs, body, token=coord, key='pl-bad-1')
        self.assertEqual(422, refused.status, refused.data)
        self.assertIn('kept for program links', refused.data['error']['message'])
        body.update(key='program.area.north', statement='Area: north\nState: in\n\nAdmitted by the owner.')
        proposed = h.request('POST', refs, body, token=coord, key='pl-propose-1')
        self.assertEqual(201, proposed.status, proposed.data)
        read = h.request('GET', '/v1/projects/pp/program-links', token=coord)
        self.assertEqual(200, read.status, read.data)
        self.assertEqual(['proposed'], [row['state'] for row in read.data['as_program']['items']])
        accept = {'expected_revision': 1, 'record_sha256': proposed.data['sha256'],
                  'evidence': 'The owner decided to admit north.', 'decision_id': decision}
        gate = h.request('POST', refs + '/program.area.north/accept', accept, token=coord, key='pl-accept-coord')
        self.assertEqual(403, gate.status, gate.data)
        accepted = h.request('POST', refs + '/program.area.north/accept', accept, token=self.owner, key='pl-accept-1')
        self.assertEqual(200, accepted.status, accepted.data)
        read = h.request('GET', '/v1/projects/pp/program-links', token=coord)
        row = read.data['as_program']['items'][0]
        # north is no project here, so its side cannot count: one-sided, and the program side is the owner's.
        self.assertEqual(('north', 'one-sided', True, {'counts': False}),
                         (row['area'], row['state'], row['program_side']['counts'], row['area_side']))


# -- the docs an agent reads ----------------------------------------------------------------------------------
class AgentMdTests(unittest.TestCase):
    """The coordinator AGENT.md and the site's coordinator prompt say the same about programs, word for word."""

    def test_the_coordinator_reads_the_program_paragraph_and_a_worker_does_not(self):
        import agent_prompts
        from test_coordinator_agent_md import BUNDLE, node_json
        got = {}
        for name, coordinates in (('coordinator', "[{ project: 'prog', effective: true }]"), ('worker', '[]')):
            got[name] = node_json(self, ("const p = m.secretlessPayload(%s, 'https://office.example:7443');"
                                         "console.log(JSON.stringify({guide: m.agentGuide(p), text: m.PROGRAM_LINKS}))")
                                  % (BUNDLE % coordinates))
        self.assertEqual(agent_prompts.PROGRAM_LINKS, got['coordinator']['text'])
        lines = got['coordinator']['guide'].split('\n')
        self.assertEqual(lines[lines.index('## If this project is in a program') + 2], agent_prompts.PROGRAM_LINKS)
        self.assertNotIn('program link', got['worker']['guide'])
        for words in ('grants nothing', 'only a person owner of each project accepts them', 'program.member-of',
                      '"State: in" or "State: left"', '/v1/projects/PROJECT/program-links'):
            self.assertIn(words, agent_prompts.PROGRAM_LINKS)


# -- the harness itself -----------------------------------------------------------------------------------------
class HarnessTests(unittest.TestCase):

    def test_every_route_that_mentions_program_is_in_the_harness(self):
        # The addition at VERIFIED: any route whose pattern contains "program", not only those under /program.
        self.assertEqual(sorted(program_isolation.PROGRAM_ROUTES), program_isolation.program_routes(http_service.ROUTES))
        self.assertTrue(set(program_isolation.PROGRAM_ROUTES.values()) <= program_isolation.covered_reads())

    @unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
    def test_a_host_key_without_the_area_is_refused_as_for_no_project(self):
        refusals = []
        for project in ('north', 'nowhere'):
            with self.assertRaises(ValueError) as refused:
                fixture.endpoint.key_project_refusal({'action': 'ref', 'project': project, 'args': ['links']}, ['prog'])
            refusals.append(str(refused.exception).replace(project, 'PROJECT'))
        self.assertEqual(refusals[0], refusals[1])


if __name__ == '__main__':
    unittest.main()
