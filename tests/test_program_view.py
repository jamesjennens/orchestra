"""kittrial-5bb.453: the program view and the Program page (PROGRAM_DESIGN.md sections 5 and 9.4).

GET /v1/projects/{program}/program and the client's `trace --program` read one page of a program's areas through the
endpoint's `program` action, with .449's readable-set rule. Permission comes before counts: an area the caller may
not read, and an area that does not exist, give the same row, "not visible to you", with no number. The interfaces
section reads `not-available` until .450; decisions and suggestions are kittrial-5bb.454's (test_program_decisions).
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import program_isolation
import program_links as pl
import program_view
import test_bd_label_aliases as fixture
import trace_read
from briefing import parse_context_pointers
from http_auth import forbidden
from test_program_links import LinkCase, item, statement


def pointer(text):
    found = parse_context_pointers(text)
    assert len(found) == 1, text
    return found[0]


# -- the rules, on their own ------------------------------------------------------------------------------------
class OpenTaskTests(unittest.TestCase):
    """Q4 at VERIFIED: open, in progress or blocked; not an epic, not a requirement record, and nothing the queue
    never lists as work (an event or gate row, the merge slot, a record anchor)."""

    def test_only_open_work_counts(self):
        rows = [{'id': 'p-1', 'status': 'open', 'issue_type': 'task'},
                {'id': 'p-2', 'status': 'in_progress', 'issue_type': 'bug'},
                {'id': 'p-3', 'status': 'blocked', 'issue_type': 'feature'},
                {'id': 'p-4', 'status': 'open', 'issue_type': 'decision'},
                {'id': 'p-5', 'status': 'closed', 'issue_type': 'task'},
                {'id': 'p-6', 'status': 'deferred', 'issue_type': 'task'},
                {'id': 'p-7', 'status': 'open', 'issue_type': 'epic'},
                {'id': 'p-8', 'status': 'open', 'issue_type': 'event'},
                {'id': 'p-9', 'status': 'open', 'issue_type': 'gate'},
                {'id': 'pp-merge-slot', 'status': 'open', 'issue_type': 'task'},
                {'id': 'p-10', 'status': 'open', 'issue_type': 'task', 'labels': ['requirement']},
                {'id': 'p-11', 'status': 'open', 'issue_type': 'task', 'labels': ['brd-section']},
                {'id': 'p-12', 'status': 'open', 'issue_type': 'task'},
                'not a row']
        with patch('reserved_comments.is_record_anchor', lambda row: row.get('id') == 'p-12'):
            counted = [row['id'] for row in rows if program_view.open_task(row)]
        self.assertEqual(['p-1', 'p-2', 'p-3', 'p-4'], counted)
        self.assertEqual(5, program_view.area_details(rows[:-1], {})['open_tasks'])   # p-12 is work without the patch


class RowTests(unittest.TestCase):

    def test_a_hidden_row_is_the_name_the_link_state_and_one_sentence(self):
        self.assertEqual({'area': 'south', 'state': 'linked', 'visible': False, 'note': 'not visible to you'},
                         program_view.area_row('south', 'linked'))

    def test_a_visible_row_carries_the_numbers(self):
        details = program_view.area_details([{'id': 'n-1', 'status': 'open', 'issue_type': 'task'}],
                                            {'waiting_too_long': 2, 'expect_by_passed': 1, 'your_move_idle': 5})
        self.assertEqual({'area': 'north', 'state': 'linked', 'visible': True, 'open_tasks': 1,
                          'lanes': {'waiting_too_long': 2, 'expect_by_passed': 1}, 'rows_read': 1,
                          'coordinators': None}, program_view.area_row('north', 'linked', details))

    def test_the_link_state_is_the_program_links_reads(self):
        owner, memberships, users = 'usr_' + 'a' * 16, {'prog': {}, 'north': {}}, {}
        memberships['prog']['usr_' + 'a' * 16] = 'owner'
        users[owner] = {'disabled': False}
        own = item('program.area.north', statement('Area', 'north'), owner)
        member = item(pl.MEMBER_OF, statement('Program', 'prog'), 'ops')
        self.assertEqual('linked', program_view.link_state('prog', 'north', own, member, memberships, users))
        self.assertEqual('one-sided', program_view.link_state('prog', 'north', own, None, memberships, users))
        left = item('program.area.north', statement('Area', 'north', state='left'), owner)
        self.assertEqual('left', program_view.link_state('prog', 'north', left, member, memberships, users))
        self.assertTrue(program_view.accepted_area([own], 'prog', memberships, users))
        self.assertFalse(program_view.accepted_area([left], 'prog', memberships, users))
        by_contributor = item('program.area.north', statement('Area', 'north'), 'usr_' + 'b' * 16)
        self.assertFalse(program_view.accepted_area([by_contributor], 'prog', memberships, users))


class FakePointers:
    """The program's ContextPointers, as far as the requirements section reads it."""

    def __init__(self, live, flagged=(), superseded=()):
        self.live, self.flagged, self.superseded = list(live), list(flagged), set(superseded)

    def requirement_keys(self):
        return sorted(self.live + self.flagged)

    def requirement(self, key):
        return {'state': 'live', 'flags': ['draft'] if key in self.flagged else [], 'label': 'Title ' + key,
                'reference': 'prog-' + key}

    def resolve(self, found):
        return {'flags': ['superseded'] if found['value'] in self.superseded else []}


class RequirementsTests(unittest.TestCase):

    def test_a_requirement_no_area_cites_and_a_pin_of_a_superseded_revision(self):
        pins = [{'area': 'north', 'design': 'api', 'heading': 'h', 'token': 'req:prog/K1@1', 'local': 'K1@1'},
                {'area': 'north', 'design': 'api', 'heading': 'h', 'token': 'req:prog/K1@2', 'local': 'K1@2'}]
        section = program_view.requirements_section(FakePointers(['K1', 'K2'], flagged=['K3'], superseded={'K1@1'}),
                                                    {'K1': [{'area': 'north', 'task': 'north-1'}]}, pins, 0)
        self.assertEqual([('K2', ['no-area-cites']), ('K1', ['cites-superseded-revision'])],
                         [(row['key'], row['flags']) for row in section['items']])
        self.assertEqual(('north', 'api', 'h', 'req:prog/K1@1'),
                         tuple(section['items'][1][k] for k in ('area', 'design', 'heading', 'token')))
        self.assertEqual(program_view.REQUIREMENTS_SCOPE, section['scope'])

    def test_rows_page_fifty_at_a_time(self):
        pointers = FakePointers(['K%02d' % n for n in range(51)])
        first = program_view.requirements_section(pointers, {}, [], 0)
        self.assertEqual((50, 51, 50), (len(first['items']), first['total'], first['next_offset']))
        second = program_view.requirements_section(pointers, {}, [], 50)
        self.assertEqual((['K50'], None), ([row['key'] for row in second['items']], second['next_offset']))

    def test_what_an_area_cites_of_the_program(self):
        design = {'scope': 'api', 'index': {'headings': [{'anchor': 'h', 'tokens': ['req:prog/K5@2', 'req:K5']}]}}
        snap = SimpleNamespace(
            project='north',
            citations=lambda: {'north-1': [('description', pointer('req:prog/K1'))],
                               'north-2': [('description', pointer('req:K9'))],
                               'north-3': [('description', pointer('req:other/K2'))],
                               'north-4': [('description', pointer('design:prog/x#y'))]},
            designs=lambda: [design, {'scope': 'none', 'index': None}],
            section_tokens=lambda d, heading: [pointer(token) for token in heading['tokens']])
        cited, pins = program_view.citations_to(snap, 'prog', 'north')
        self.assertEqual({'K1': [{'area': 'north', 'task': 'north-1'}],
                          'K5': [{'area': 'north', 'design': 'api', 'heading': 'h'}]}, cited)
        self.assertEqual([{'area': 'north', 'design': 'api', 'heading': 'h', 'token': 'req:prog/K5@2',
                           'local': 'K5@2'}], pins)


class CoverageTests(unittest.TestCase):

    def test_only_visible_rows_count_and_a_cut_is_said(self):
        areas = [program_view.area_row('north', 'linked', {'open_tasks': 3, 'lanes': {}, 'rows_read': 1200}),
                 program_view.area_row('south', 'linked'), program_view.area_row('east', 'one-sided')]
        areas[0]['lanes'] = {'waiting_too_long': 0, 'expect_by_passed': 0}
        requirements = program_view.requirements_section(FakePointers([]), {}, [], 0)
        block = program_view.coverage(areas, 51, 0, requirements)
        self.assertEqual('1 of 2 linked areas readable; 1,200 rows read; areas 1-3 of 51 on this page', block['text'])
        self.assertEqual((2, 1, 3, 1, 51, 1200, {'areas': 48, 'requirements': 0}),
                         (block['linked'], block['linked_readable'], block['areas_on_page'], block['areas_readable'],
                          block['areas_total'], block['rows_read'], block['cut']))

    def test_the_sections_of_later_slices_say_so(self):
        answer = program_view.answer('prog', [], 0, 0, 50, program_view.requirements_section(FakePointers([]), {}, [], 0))
        self.assertEqual({'state': 'not-available', 'note': program_view.INTERFACES_NOT_AVAILABLE}, answer['interfaces'])
        self.assertEqual((program_view.DECISIONS_SCOPE, [], []),
                         (answer['decisions']['scope'], answer['decisions']['items'], answer['suggestions']['items']))

    def test_the_interfaces_section_pages_names_readable_areas_only_and_counts_none(self):
        def view(n, **more):
            return dict({'key': 'iface.k%03d.v1' % n, 'record': {'name': 'K%d' % n, 'revision': 2}, 'lifecycle': 'accepted',
                         'replacement': None}, **more)
        usage = [{'key': 'iface.k001.v1', 'role': 'provides', 'project': 'north', 'minimum': 2},
                 {'key': 'iface.k001.v1', 'role': 'consumes', 'project': 'north', 'minimum': 1}]
        views = [view(n) for n in range(60)] + [{'key': 'iface.draft.v1', 'proposed': {'name': 'D', 'revision': 1},
                                                  'lifecycle': 'draft', 'replacement': None}]
        first = program_view.interfaces_section(views, usage, False, lambda record: ['compatibility-unverified'], 0)
        self.assertEqual((50, 61, 50, 'ok'), (len(first['items']), first['total'], first['next_offset'], first['state']))
        self.assertEqual([{'area': 'north', 'minimum': 2}], [r for r in first['items'] if r['key'] == 'iface.k001.v1'][0]['providers'])
        self.assertEqual([{'area': 'north', 'minimum': 1}], [r for r in first['items'] if r['key'] == 'iface.k001.v1'][0]['consumers'])
        second = program_view.interfaces_section(views, usage, False, lambda record: [], 50)
        self.assertEqual((11, None), (len(second['items']), second['next_offset']))
        draft = [row for row in first['items'] if row['key'] == 'iface.draft.v1'][0]
        self.assertEqual(('draft', ['compatibility-unverified']), (draft['lifecycle'], draft['flags']))
        program_isolation.assert_interfaces_section_hidden(self, first, {'south', 'ghost'})
        self.assertFalse(first['complete'])
        unsettled = program_view.interfaces_unsettled()
        self.assertEqual(([], 'unsettled'), (unsettled['items'], unsettled['state']))
        program_isolation.assert_interfaces_section_hidden(self, unsettled, set())

    def test_the_arguments(self):
        self.assertEqual({'offset': 0, 'limit': 50, 'idle_minutes': 15, 'requirements_offset': 0, 'decisions_offset': 0,
                          'suggestions_offset': 0, 'interfaces_offset': 0},
                         program_view.parse_args([]))
        for bad in (['--limit', '51'], ['--limit', '0'], ['--offset', '-1'], ['--idle-minutes', '0'],
                    ['--requirements-offset', '-1'], ['--interfaces-offset', '-1'], ['--view', 'x']):
            with self.assertRaises(ValueError, msg=bad):
                program_view.parse_args(bad)


# -- the web route ----------------------------------------------------------------------------------------------
class WebTests(LinkCase):
    """prog lists north and south (both linked) and ghost (no such project). vera reads north; carl, pat and Wren
    read neither area."""

    def setUp(self):
        super().setUp()
        for area in ('north', 'south'):
            self.admit(area)
            self.join(area)
        self.admit('ghost')

    def view(self, token, query=''):
        answer = self.request('GET', '/v1/projects/prog/program' + query, token=token)
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    def test_each_reader_sees_numbers_only_for_the_areas_it_reads(self):
        rows = {row['area']: row for row in self.view(self.tokens['vera'])['areas']['items']}
        self.assertTrue(rows['north']['visible'])
        self.assertEqual((0, {'waiting_too_long': 0, 'expect_by_passed': 0}),
                         (rows['north']['open_tasks'], rows['north']['lanes']))
        self.assertIn('nora', [p['display_name'] for p in rows['north']['coordinators']['people']])
        self.assertEqual(program_view.area_row('south', 'linked'), rows['south'])
        self.assertEqual(program_view.area_row('ghost', 'one-sided'), rows['ghost'])
        for name in ('carl', 'pat', 'wren'):
            rows = self.view(self.tokens[name])['areas']['items']
            self.assertEqual([False, False, False], [row['visible'] for row in rows], name)

    def test_the_link_states_are_the_program_links_reads(self):
        states = {row['area']: row['state'] for row in self.view(self.tokens['carl'])['areas']['items']}
        links, _ = self.rows(self.tokens['carl'])
        self.assertEqual({area: row['state'] for area, row in links.items()}, states)

    def test_the_readable_set_is_the_callers_own(self):
        seen = []
        inner = self.backend.program

        def program(*args, **kw):
            seen.append(sorted(kw['readable']))
            return inner(*args, **kw)
        self.backend.program = program
        self.view(self.tokens['vera'])
        self.view(self.tokens['carl'])
        self.assertEqual([['north', 'prog'], ['prog']], seen)

    def test_the_in_process_backend_applies_the_readable_set_itself(self):
        # The route re-checks each readable row through coordinators_of; the backend must not lean on that: given a
        # readable set, it shows numbers only inside it (P3).
        rows = {row['area']: row for row in self.backend.program('prog', readable=['prog', 'north'])['areas']['items']}
        self.assertTrue(rows['north']['visible'])
        self.assertEqual(program_view.area_row('south', 'linked'), rows['south'])
        rows = {row['area']: row for row in self.backend.program('prog', readable=['prog'])['areas']['items']}
        self.assertEqual([False, False, False], [row['visible'] for row in rows.values()])

    def test_a_hidden_row_never_gains_coordinators(self):
        # The endpoint hides an area it could not read (here: north as no host project, though vera reads it on the
        # web). The route adds coordinators to visible rows only, so the hidden row stays exactly as it came (P27).
        inner = self.backend.program

        def program(*args, **kw):
            answer = inner(*args, **kw)
            answer['areas']['items'] = [program_view.area_row(row['area'], row['state']) if row['area'] == 'north'
                                        else row for row in answer['areas']['items']]
            return answer
        self.backend.program = program
        rows = {row['area']: row for row in self.view(self.tokens['vera'])['areas']['items']}
        self.assertEqual(program_view.area_row('north', 'linked'), rows['north'])

    def test_a_proposed_or_left_area_is_never_read(self):
        # Review of .453, B1: the program's side first. east is proposed (no accepted revision) and west has left;
        # neither is read, by any caller, whatever it may read: a proposed entry tells nothing about a project.
        self.admit('east', state='draft-only')
        self.admit('west', text=statement('Area', 'west', state='left'))
        for name in ('vera', 'carl', 'pat'):
            self.reads.clear()
            rows = {row['area']: row for row in self.view(self.tokens[name])['areas']['items']}
            self.assertNotIn('east', self.reads, name)
            self.assertNotIn('west', self.reads, name)
            self.assertIn('north', self.reads, name)                  # an area whose program side is in
            self.assertEqual(program_view.area_row('east', 'proposed'), rows['east'], name)
            self.assertEqual(program_view.area_row('west', 'left'), rows['west'], name)

    def test_a_lost_right_drops_the_areas_citations(self):
        # Review of .453, B2 (N1), with injected rows: while north is readable, K1 is cited by it and north pins a
        # superseded revision; when vera loses north between the read and coordinators_of, the page is read again
        # without north, so K1 reads no-area-cites and no row names north's design.
        seen = []

        def program(project_id, offset=0, limit=50, idle_minutes=15, requirements_offset=0, readable=(), **_):
            seen.append(sorted(readable))
            north = 'north' in readable
            areas = [program_view.area_row('north', 'linked', {'open_tasks': 1, 'rows_read': 9,
                                                                'lanes': {'waiting_too_long': 0, 'expect_by_passed': 0}})
                     if north else program_view.area_row('north', 'linked')]
            rows = ([{'key': 'K1', 'token': 'req:prog/K1@1', 'area': 'north', 'design': 'api', 'heading': 'h',
                      'flags': ['cites-superseded-revision']}] if north
                    else [{'key': 'K1', 'title': 'Queue', 'reference': 'prog-1', 'flags': ['no-area-cites']}])
            requirements = {'items': rows, 'total': len(rows), 'next_offset': None,
                            'scope': program_view.REQUIREMENTS_SCOPE}
            return dict(program_view.answer(project_id, areas, 1, offset, limit, requirements), is_program=True)
        self.backend.program = program
        calls = []
        inner = self.service.coordinators_of

        def once(principal, project):
            calls.append(project)
            if len(calls) == 1:
                raise forbidden('no longer readable')
            return inner(principal, project)
        with patch.object(self.service, 'coordinators_of', once):
            answer = self.view(self.tokens['vera'])
        self.assertEqual([['north', 'prog'], ['prog']], seen)
        self.assertEqual(program_view.area_row('north', 'linked'), answer['areas']['items'][0])
        self.assertEqual([('K1', ['no-area-cites'])], [(row['key'], row['flags']) for row in answer['requirements']['items']])
        self.assertNotIn('north', json.dumps(answer['requirements']))

    def test_rights_that_keep_changing_show_no_requirement(self):
        # A backend that keeps answering north as readable, and a right that is lost on every read: after three
        # reads the row is hidden and the requirements section is unsettled, with no row at all.
        seen = []

        def program(project_id, offset=0, limit=50, idle_minutes=15, requirements_offset=0, readable=(), **_):
            seen.append(sorted(readable))
            areas = [program_view.area_row('north', 'linked', {'open_tasks': 1, 'rows_read': 9,
                                                                'lanes': {'waiting_too_long': 0, 'expect_by_passed': 0}})]
            requirements = {'items': [{'key': 'K1', 'area': 'north', 'flags': ['cites-superseded-revision']}],
                            'total': 1, 'next_offset': None, 'scope': program_view.REQUIREMENTS_SCOPE}
            return dict(program_view.answer(project_id, areas, 1, offset, limit, requirements), is_program=True)
        self.backend.program = program

        def refuse(principal, project):
            raise forbidden('no longer readable')
        with patch.object(self.service, 'coordinators_of', refuse):
            answer = self.view(self.tokens['vera'])
        self.assertEqual(3, len(seen))
        self.assertEqual(program_view.area_row('north', 'linked'), answer['areas']['items'][0])
        self.assertEqual(('unsettled', []), (answer['requirements']['state'], answer['requirements']['items']))

    def test_the_program_flag_never_reaches_a_non_member(self):
        # Review of .453, B2 (N2): the flag is set for prog, and no one outside prog sees it, in a list or a view.
        self.view(self.tokens['carl'])
        self.assertTrue(self.request('GET', '/v1/projects/prog', token=self.tokens['carl']).data['program'])
        north_only = self.request('POST', '/v1/agents', {'name': 'Gull', 'working_directory': '/home/x/gull',
                                                         'projects': ['north']}, token=self.tokens['vera'])
        self.assertEqual(201, north_only.status, north_only.data)
        callers = {'nobody': self.tokens['otto'], 'owner of north only': self.tokens['nora'],
                   'agent without prog': north_only.data['credential']['secret']}
        for label, token in callers.items():
            listed = self.request('GET', '/v1/projects', token=token)
            self.assertEqual(200, listed.status, (label, listed.data))
            self.assertNotIn('prog', [item.get('id') for item in listed.data['items']], label)
            self.assertFalse(any(item.get('program') for item in listed.data['items']), label)
            view = self.request('GET', '/v1/projects/prog', token=token)
            self.assertEqual(404, view.status, label)
            self.assertNotIn('program', json.dumps(view.data), label)

    def test_a_right_lost_after_the_read_hides_the_row(self):
        # coordinators_of checks the caller again: a role removed after the endpoint's read makes the row the
        # hidden one, and its rows no longer count.
        def refuse(principal, project):
            raise forbidden('no longer readable')
        with patch.object(self.service, 'coordinators_of', refuse):
            answer = self.view(self.tokens['vera'])
        rows = {row['area']: row for row in answer['areas']['items']}
        self.assertEqual(program_view.area_row('north', 'linked'), rows['north'])
        self.assertEqual((0, 0), (answer['coverage']['linked_readable'], answer['coverage']['rows_read']))

    def test_the_harness_hides_an_unreadable_area_like_a_missing_one(self):
        program_isolation.assert_area_rows_hidden(
            self, self.request, 'prog', 'south', 'ghost',
            {'program contributor': self.tokens['carl'], 'program owner': self.tokens['pat'],
             'reader of another area': self.tokens['vera'], 'agent without the area': self.tokens['wren']})
        self.assertIn(('program', '/v1/projects/{project}/program'), program_isolation.AREA_READS)
        program_isolation.assert_outsiders_see_nothing(
            self, self.request, 'north', 'nowhere',
            {'program member': self.tokens['carl'], 'nobody': self.tokens['otto'],
             'agent without the grant': self.tokens['wren']})

    def test_the_harness_shows_no_area_in_the_interfaces_section(self):
        program_isolation.assert_interfaces_hidden(
            self, self.request, 'prog', {'south', 'ghost'},
            {'program contributor': self.tokens['carl'], 'program owner': self.tokens['pat'],
             'reader of another area': self.tokens['vera'], 'agent without the area': self.tokens['wren']})
        data = self.view(self.tokens['vera'], '?interfaces_offset=0')
        self.assertEqual('not-available', data['interfaces']['state'])
        self.assertEqual(422, self.request('GET', '/v1/projects/prog/program?interfaces_offset=x',
                                           token=self.tokens['vera']).status)

    def test_the_sections_coverage_and_bounds(self):
        data = self.view(self.tokens['vera'])
        self.assertEqual('not-available', data['interfaces']['state'])
        self.assertEqual(([], []), (data['decisions']['items'], data['suggestions']['items']))
        self.assertEqual('1 of 2 linked areas readable; 0 rows read', data['coverage']['text'])
        for query in ('?limit=51', '?view=tasks', '?requirements_offset=x', '?idle_minutes=0'):
            self.assertIn(self.request('GET', '/v1/projects/prog/program' + query, token=self.tokens['vera']).status,
                          (400, 422), query)

    def test_fifty_areas_a_page(self):
        for n in range(48):
            self.admit('a%02d' % n)                               # 51 entries with north, south and ghost
        first = self.view(self.tokens['carl'])
        self.assertEqual((50, 51), (len(first['areas']['items']), first['areas']['total']))
        self.assertIn('areas 1-50 of 51', first['coverage']['text'])
        second = self.view(self.tokens['carl'], '?cursor=' + first['areas']['next_cursor'])
        self.assertEqual(['south'], [row['area'] for row in second['areas']['items']])
        self.assertIsNone(second['areas']['next_cursor'])

    def test_the_menu_flag_follows_the_program_read(self):
        project = lambda pid: self.request('GET', '/v1/projects/%s' % pid, token=self.tokens['vera']).data
        self.assertFalse(project('prog')['program'])                  # nothing has read it yet
        self.view(self.tokens['carl'])
        self.assertTrue(project('prog')['program'])
        self.assertFalse(project('north')['program'])
        self.links['prog'] = [item('program.area.north', statement('Area', 'north', state='left'), self.ids['pat'])]
        self.view(self.tokens['carl'])
        self.assertFalse(project('prog')['program'])

    def test_the_menu_flag_follows_a_web_accept_and_retire(self):
        self.backend.RECORDS = True
        self.backend.invoke = lambda route, principal, pid, payload, key, **kw: {'key': payload['body'].get('key')}
        accept = {'expected_revision': 1, 'record_sha256': 'a' * 64, 'evidence': 'The owner decided.',
                  'decision_id': 'prog-1'}
        self.assertFalse(self.request('GET', '/v1/projects/prog', token=self.tokens['pat']).data['program'])
        self.assertNotEqual(403, self.request('POST', '/v1/projects/prog/references/program.area.north/accept',
                                              accept, token=self.tokens['pat'], key='flag-accept-1').status)
        self.assertTrue(self.request('GET', '/v1/projects/prog', token=self.tokens['pat']).data['program'])
        self.links['prog'] = [item('program.area.north', statement('Area', 'north'), self.ids['pat'], state='superseded')]
        self.assertNotEqual(403, self.request('POST', '/v1/projects/prog/references/program.area.north/retire',
                                              {'successor': 'calendar.next'}, token=self.tokens['pat'],
                                              key='flag-retire-1').status)
        self.assertFalse(self.request('GET', '/v1/projects/prog', token=self.tokens['pat']).data['program'])


# -- the endpoint ------------------------------------------------------------------------------------------------
@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointArgumentTests(unittest.TestCase):

    def test_a_caller_other_than_the_web_service_cannot_give_a_readable_set(self):
        with self.assertRaises(ValueError) as refused:
            fixture.endpoint.program_answer(Path('/nonexistent-root'), Path('/nonexistent-root/projects/pp'), 'pp',
                                            ['--readable', 'qq'], 'op', key_projects=frozenset({'pp'}),
                                            by_service=False)
        self.assertEqual(trace_read.READABLE_REFUSED, str(refused.exception))

    def test_the_action_is_read_only(self):
        self.assertTrue(fixture.endpoint.READ_ONLY_ACTIONS['program']([], None))


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class NativeProgramTests(unittest.TestCase):
    """Two real projects on the real endpoint: pp is the program, qq its area; zz is entered but does not exist.
    The link records are injected (their own reads are .446's, tested there); the exports, the lanes, the open
    tasks and the citations are real."""

    @classmethod
    def setUpClass(cls):
        cls.runtime = type('ProgramRuntime', (fixture.RealBdLabelAliasTests,), {})
        cls.runtime.setUpClass()
        r = cls.runtime
        cls.root = r.root
        cls.qq = r.root / 'projects' / 'qq'
        cls.qq.mkdir(parents=True)
        init = subprocess.run([str(r.bd_path), 'init', '--server', '--external', '--server-host', '127.0.0.1',
                               '--server-port', str(r.port), '--server-user', 'root', '--prefix', 'qq',
                               '--database', 'qq', '--skip-agents', '--skip-hooks', '--non-interactive'],
                              cwd=str(cls.qq), env=fixture.admin.environment(r.root), capture_output=True,
                              text=True, timeout=300)
        if init.returncode:
            cls.runtime.tearDownClass()
            raise unittest.SkipTest('bd init of a second project failed: %s' % (init.stderr or init.stdout)[:200])

    @classmethod
    def tearDownClass(cls):
        cls.runtime.tearDownClass()

    def bd(self, project, *args):
        r = self.runtime
        return subprocess.run([str(r.bd_path), '--directory', str(self.root / 'projects' / project), '--sandbox',
                               '--actor', 'op', *args], env=fixture.admin.environment(self.root),
                              capture_output=True, text=True, timeout=120)

    def created(self, project, *args):
        made = self.bd(project, 'create', *args, '--json')
        self.assertEqual(0, made.returncode, made.stderr)
        return json.loads(made.stdout)['id']

    PP_ITEMS = [item('program.area.qq', statement('Area', 'qq'), 'ops'),
                item('program.area.zz', statement('Area', 'zz'), 'ops')]

    def fake_links(self, rows, operators):
        ids = {str(row.get('id', '')).split('-', 1)[0] for row in rows if isinstance(row, dict)}
        if 'pp' in ids:
            return {'items': list(getattr(self, 'pp_items', self.PP_ITEMS))}
        if 'qq' in ids:
            return {'items': [item(pl.MEMBER_OF, statement('Program', 'pp'), 'ops')]}
        return {'items': []}

    def run_view(self, args=(), **kw):
        reads, cited = [], []
        import read_cache
        inner_rows, inner_section = read_cache.cached_export_rows, program_view.requirements_section

        def counted(root, path, run, name=None):
            reads.append(name)
            return inner_rows(root, path, run, name)

        def section(pointers, found, pins, offset):
            cited.append(sorted(found))
            return inner_section(pointers, found, pins, offset)
        request = {'action': 'program', 'project': 'pp', 'actor': kw.pop('actor', 'op'), 'args': list(args)}
        self.resolved = []
        inner_dir = fixture.endpoint.project_dir

        def resolving(root, name):
            self.resolved.append(name)
            return inner_dir(root, name)
        with patch('reference_records.links', self.fake_links), patch('read_cache.cached_export_rows', counted), \
                patch('program_view.requirements_section', section), \
                patch.object(fixture.endpoint, 'project_dir', resolving):
            answer = fixture.endpoint.execute(self.root, request, **kw)
        return json.loads(answer['stdout']), reads, cited

    def test_native_a_proposed_or_left_area_is_never_read(self):
        # Review of .453, B1, on the real endpoint with a counting backend: qq exists but its entry is only proposed,
        # and zz has left; neither is resolved nor read, on any route, so the time of a read says nothing about them.
        self.created('pp', 'The program, again')
        self.pp_items = [item('program.area.qq', statement('Area', 'qq'), 'ops', state='draft-only'),
                         item('program.area.zz', statement('Area', 'zz', state='left'), 'ops')]
        try:
            config = fixture.endpoint.AuthorityConfig(str(self.root / 'unread-authority.json'))
            for args, kw in (((), {}), ((), {'key_projects': frozenset({'pp', 'qq', 'zz'})}),
                             (['--readable', 'qq', '--readable', 'zz'], {'actor': 'http/read', 'authority_config': config})):
                view, reads, _ = self.run_view(args, **kw)
                rows = {row['area']: row for row in view['areas']['items']}
                self.assertEqual((program_view.area_row('qq', 'proposed'), program_view.area_row('zz', 'left')),
                                 (rows['qq'], rows['zz']), kw)
                self.assertEqual(['pp'], reads, kw)
                self.assertEqual(['pp'], self.resolved, kw)
        finally:
            del self.pp_items

    def test_native_the_view_over_real_projects_on_every_route(self):
        self.created('pp', 'The program itself')
        for title, extra in (('Build the queue', ['--description', 'Implements req:pp/K1']),
                             ('Done already', []), ('The big plan', ['--type', 'epic']),
                             ('A requirement row', ['--labels', 'requirement'])):
            made = self.created('qq', title, *extra)
            if title == 'Done already':
                self.assertEqual(0, self.bd('qq', 'close', made).returncode)
        # An operator's unbound key reads every area: qq has its numbers, zz (no project) is hidden.
        view, reads, cited = self.run_view()
        rows = {row['area']: row for row in view['areas']['items']}
        self.assertEqual(('linked', True, 1), (rows['qq']['state'], rows['qq']['visible'], rows['qq']['open_tasks']))
        self.assertEqual(program_view.area_row('zz', 'one-sided'), rows['zz'])
        self.assertEqual([['K1']], cited)
        self.assertEqual(program_view.COORDINATORS_ON_THE_WEB, view['coordinators'])
        self.assertTrue(view['coverage']['rows_read'] > 0)
        # A key bound to pp alone reads nothing of qq: not even the one fact.
        view, reads, _ = self.run_view(key_projects=frozenset({'pp'}))
        rows = {row['area']: row for row in view['areas']['items']}
        self.assertEqual((program_view.area_row('qq', 'not-checked'), program_view.area_row('zz', 'not-checked')),
                         (rows['qq'], rows['zz']))
        self.assertEqual(['pp'], reads)
        # The web service's launch: its set decides; with qq outside it, qq gives the one fact and no number.
        config = fixture.endpoint.AuthorityConfig(str(self.root / 'unread-authority.json'))
        view, _, _ = self.run_view(['--readable', 'qq'], actor='http/read', authority_config=config)
        self.assertTrue({row['area']: row for row in view['areas']['items']}['qq']['visible'])
        view, _, _ = self.run_view(['--readable'], actor='http/read', authority_config=config)
        self.assertEqual(program_view.area_row('qq', 'linked'),
                         {row['area']: row for row in view['areas']['items']}['qq'])
        # Anyone else giving a readable set is refused before anything is read.
        with self.assertRaises(ValueError):
            self.run_view(['--readable', 'qq'], key_projects=frozenset({'pp'}))


# -- the page ----------------------------------------------------------------------------------------------------
class PageTests(unittest.TestCase):

    def test_the_page_shows_server_text_only_and_the_rail_follows_the_flag(self):
        import shutil
        import test_http_web
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        code = """await import(process.argv[1]);
const view=await import(process.argv[2]);
const read=JSON.parse(process.argv[3]);
const nodes=view.view(read);
const host=document.createElement('div');
for (const node of nodes) host.appendChild(node);
console.log(JSON.stringify({text:host.textContent,
  markup:host.querySelectorAll('img').length+host.querySelectorAll('script').length,
  rows:[...host.querySelectorAll('tr')].filter((tr)=>tr.attributes['data-area']).map((tr)=>tr.textContent),
  flags:[view.showsProgram({program:true}),view.showsProgram({program:false}),view.showsProgram(null),
         view.showsProgram({program:'yes'})]}));"""
        read = {'coverage': {'text': '1 of 2 linked areas readable; 12 rows read'},
                'areas': {'items': [
                    {'area': 'north', 'state': 'linked', 'visible': True, 'open_tasks': 3,
                     'lanes': {'waiting_too_long': 1, 'expect_by_passed': 0}, 'rows_read': 12,
                     'coordinators': {'people': [{'display_name': '<img src=x onerror=1>Nora', 'role': 'owner'}],
                                      'agents': []}},
                    {'area': 'south', 'state': 'linked', 'visible': False, 'note': 'not visible to you'}]},
                'interfaces': {'state': 'not-available', 'note': program_view.INTERFACES_NOT_AVAILABLE},
                'requirements': {'items': [{'key': 'K2', 'title': 'Queue', 'flags': ['no-area-cites']}],
                                 'scope': program_view.REQUIREMENTS_SCOPE},
                'decisions': {'items': [], 'scope': program_view.DECISIONS_SCOPE},
                'suggestions': {'items': []}}
        done = test_http_web.run_node_module(self, node, code, (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web/js/views/program.js').as_uri(), json.dumps(read))
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(0, out['markup'])
        self.assertEqual(['northlinked310<img src=x onerror=1>Nora (owner)', 'southlinkednot visible to you'],
                         out['rows'])
        for words in ('1 of 2 linked areas readable', program_view.INTERFACES_NOT_AVAILABLE,
                      program_view.DECISIONS_SCOPE, 'no-area-cites'):
            self.assertIn(words, out['text'])
        self.assertEqual([True, False, False, False], out['flags'])

    def test_the_interfaces_table_shows_the_server_rows_as_text(self):
        import shutil
        import test_http_web
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        code = """await import(process.argv[1]);
const view=await import(process.argv[2]);
const read=JSON.parse(process.argv[3]);
const host=document.createElement('div');
host.appendChild(view.interfacesTable(read.ok));
const empty=document.createElement('div');
empty.appendChild(view.interfacesTable(read.empty));
const note=document.createElement('div');
note.appendChild(view.interfacesTable(read.unsettled));
const rows=Array.from(host.querySelectorAll('tr')).map((row)=>row.textContent);
const made=['img','script','b','svg'].map((tag)=>Array.from(host.querySelectorAll(tag)).length).reduce((a,b)=>a+b,0);
console.log(JSON.stringify({rows,made,text:host.textContent,empty:empty.textContent,note:note.textContent}));"""
        hostile = '<img src=x onerror=1>\u202eQuotes'
        ok = {'state': 'ok', 'scope': program_view.INTERFACES_SCOPE, 'complete': False, 'total': 1, 'next_offset': None,
              'items': [{'key': 'iface.quotes.v1', 'name': hostile, 'lifecycle': 'accepted', 'revision': 2,
                         'replacement': None, 'flags': ['compatibility-unverified'],
                         'providers': [{'area': 'north', 'minimum': 2}],
                         'consumers': [{'area': 'east', 'minimum': 1}, {'area': 'west', 'minimum': 2}]}]}
        read = {'ok': ok, 'empty': dict(ok, items=[], total=0), 'unsettled': program_view.interfaces_unsettled()}
        done = test_http_web.run_node_module(self, node, code, (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web/js/views/program.js').as_uri(), json.dumps(read))
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(0, out['made'])
        self.assertIn(hostile, out['text'])
        self.assertIn('iface.quotes.v1' + hostile + 'accepted r2north@2east@1, west@2compatibility-unverified',
                      out['rows'])
        self.assertIn(program_view.INTERFACES_SCOPE, out['text'])
        self.assertIn('The register holds no interface.', out['empty'])
        self.assertIn(program_view.INTERFACES_UNSETTLED, out['note'])

    def test_a_superseded_decision_still_shows_its_area_states(self):
        # Review of .454 r2, b1: it still binds in the data, so the page must not show only the word superseded.
        import shutil
        import test_http_web
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        code = """await import(process.argv[1]);
const view=await import(process.argv[2]);
const node=view.decisionsTable(JSON.parse(process.argv[3]));
const host=document.createElement('div');
host.appendChild(node);
console.log(JSON.stringify({text:host.textContent}));"""
        read = {'items': [{'id': 'p-1', 'title': 'T', 'owner_only': True, 'superseded': True, 'flags': [],
                           'areas': {'north': {'state': 'adopted', 'adoption': 'n-1'}}},
                          {'id': 'p-2', 'title': 'U', 'flags': ['supersedes-owner-level'], 'areas': {}}]}
        done = test_http_web.run_node_module(self, node, code, (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web/js/views/program.js').as_uri(), json.dumps(read))
        self.assertEqual(done.returncode, 0, done.stderr)
        text = json.loads(done.stdout.strip().splitlines()[-1])['text']
        self.assertIn('superseded; north: adopted', text)
        self.assertIn('supersedes-owner-level', text)

    def test_markup_in_a_name_renders_as_literal_text(self):
        # Review of .453, B2 (3): every name the page shows is set as text. A person's, an agent's, its owner's and a
        # requirement's title, each holding markup, come out literally, and no element is made from them. Needs node.
        import shutil
        import test_http_web
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        code = """await import(process.argv[1]);
const view=await import(process.argv[2]);
const read=JSON.parse(process.argv[3]);
const host=document.createElement('div');
for (const node of view.view(read)) host.appendChild(node);
const made=['img','script','b','svg','iframe'].map((tag)=>host.querySelectorAll(tag).length).reduce((a,b)=>a+b,0);
console.log(JSON.stringify({text:host.textContent,made}));"""
        names = {'person': '<b>Nora</b>', 'agent': '<img src=x onerror=alert(1)>', 'owner': '<script>x()</script>',
                 'title': '<svg onload=1>Queue'}
        read = {'coverage': {'text': 'c'},
                'areas': {'items': [{'area': 'north', 'state': 'linked', 'visible': True, 'open_tasks': 0,
                                     'lanes': {'waiting_too_long': 0, 'expect_by_passed': 0}, 'rows_read': 0,
                                     'coordinators': {'people': [{'display_name': names['person'], 'role': 'owner'}],
                                                      'agents': [{'name': names['agent'],
                                                                  'owner_display_name': names['owner']}]}}]},
                'interfaces': {'state': 'not-available', 'note': 'n'},
                'requirements': {'items': [{'key': 'K1', 'title': names['title'], 'flags': ['no-area-cites']}],
                                 'scope': 's'},
                'decisions': {'items': [{'id': 'p-1', 'title': names['title'], 'owner_only': True, 'flags': [],
                                         'areas': {'north': {'state': 'adoption-invalid', 'reason': names['person']}}}]},
                'suggestions': {'items': [{'id': 'p-2', 'title': names['agent'], 'from': names['owner'],
                                           'created_at': 'T'}]}}
        done = test_http_web.run_node_module(self, node, code, (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web/js/views/program.js').as_uri(), json.dumps(read))
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(0, out['made'])
        for label, name in names.items():
            self.assertIn(name, out['text'], label)

    def test_the_rail_shows_program_only_by_the_flag(self):
        source = (KIT / 'web/js/app.js').read_text(encoding='utf-8')
        self.assertIn("program.showsProgram(current) ? link('/p/' + pid + '/program', 'Program') : null", source)
        self.assertIn("['program', '/p/{pid}/program']", (KIT / 'web/js/routes.js').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
