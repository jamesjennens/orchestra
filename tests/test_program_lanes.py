"""kittrial-5bb.507: the lane rows behind the program view's counts (PROGRAM_DESIGN.md section 5).

GET /v1/projects/{program}/program shows, for each readable area, the lane rows behind its counts. Rules, each with a
test here that fails without it:

* the rows are the area's own Lanes page's rows for the same reader (the scope is the reader's own);
* an area the reader may not read, and a row hidden after the read, carry no row, no count and no hint;
* the idle count equals the rows flagged (rows and counts come from the one read);
* a removed member loses the rows at once;
* ONE exception: a coordinator or owner of the PROGRAM reads an area whole over a link that is live on both sides.

Real HTTP against the real service. The planted ages are offsets from the machine clock, never dates.
"""
import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import lane_state
import program_links as pl
import program_view
import test_bd_label_aliases as fixture
from requirements import canonical_bytes
from test_program_links import LinkCase, item, statement

MORE_PEOPLE = {'ivy': {'prog': 'viewer', 'north': 'contributor'},
               'jo': {'prog': 'viewer', 'north': 'contributor'}}
PROGRAM_VIEWERS = {'nora': 'viewer', 'sam': 'viewer'}


def ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class LanesCase(LinkCase):
    """LinkCase's prog with areas north and south, both linked. In north: nora owns it; ivy and jo hold a task each;
    vera and cleo are viewers (cleo coordinates prog). In south: sam owns it and holds a task nobody else may see."""

    def setUp(self):
        super().setUp()
        for area in ('north', 'south'):
            self.admit(area)
            self.join(area)
        for name, places in MORE_PEOPLE.items():
            self.ids[name] = self.create_account(self.admin, name, name + '-password-1')
            self.tokens[name] = self.login(name, name + '-password-1')[0]
            for pid, role in places.items():
                self.member(pid, name, role)
        for name, role in PROGRAM_VIEWERS.items():
            self.member('prog', name, role)
        self.member('north', 'cleo', 'viewer')
        self.tasks = {}
        for who, area, title in (('ivy', 'north', 'Ivy rollout plan'), ('jo', 'north', 'Jo migration plan'),
                                 ('sam', 'south', 'South secret plan')):
            made = self.create_task(self.tokens['nora' if area == 'north' else 'sam'], area, title)
            self.assertEqual(201, made.status, made.data)
            claimed = self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (area, made.data['id']), {},
                                   token=self.tokens[who])
            self.assertEqual(200, claimed.status, claimed.data)
            self.tasks[who] = made.data['id']

    # -- fixtures --------------------------------------------------------------
    def member(self, pid, name, role):
        done = self.request('PUT', '/v1/projects/%s/members/%s' % (pid, self.ids[name]), {'role': role},
                            token=self.admin)
        self.assertEqual(200, done.status, done.data)

    def unmember(self, pid, name):
        done = self.request('DELETE', '/v1/projects/%s/members/%s' % (pid, self.ids[name]), token=self.admin)
        self.assertIn(done.status, (200, 204), done.data)

    def age(self, who, seconds):
        """The task ``who`` holds was last touched ``seconds`` ago: a clock moved past --idle-minutes."""
        row = self.backend.state['tasks'][self.tasks[who]]
        row['updated_at'] = row['created_at'] = ago(seconds)

    def plant_wait(self, who, waiting_on, expect_by=None, age_seconds=2 * 86400):
        task = self.tasks[who]
        stamp = ago(age_seconds)
        comments = self.backend.state['tasks'][task].setdefault('comments', [])
        comments.append({'id': 'comment_checkpoint_' + who, 'issue_id': task, 'author': self.ids[who],
                         'text': 'a checkpoint', 'created_at': stamp})
        payload = {'schema_version': 1, 'task': task, 'checkpoint': 'comment_checkpoint_' + who,
                   'state': 'waiting-on-coordinator', 'waiting_on': waiting_on}
        if expect_by is not None:
            payload['expect_by'] = expect_by
        comments.append({'id': 'comment_state_' + who, 'issue_id': task, 'author': self.ids[who],
                         'text': lane_state.PREFIX + canonical_bytes(payload).decode(), 'created_at': stamp})

    def program(self, who, query=''):
        answer = self.request('GET', '/v1/projects/prog/program' + query, token=self.tokens[who])
        self.assertEqual(200, answer.status, answer.data)
        return answer

    def area(self, who, area='north', query=''):
        return {row['area']: row for row in self.program(who, query).data['areas']['items']}[area]

    def lanes_page(self, who, area='north', query=''):
        answer = self.request('GET', '/v1/projects/%s/lanes%s' % (area, query), token=self.tokens[who])
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    @staticmethod
    def shaped(rows):
        """The fields both views show of a lane, from either: the Lanes page's rows or the program's."""
        return sorted(({'display_name': r['display_name'], 'idle': r['idle'],
                        'waiting_too_long': r['waiting_too_long'], 'expect_by_passed': r['expect_by_passed'],
                        'your_move_idle': r['your_move_idle'],
                        'held': [{key: entry[key] for key in ('task', 'title', 'state', 'waiting_on_name', 'expect_by', 'flags')}
                                 for entry in r['held']]} for r in rows), key=lambda r: r['display_name'])


class RowsMatchTheLanesPageTests(LanesCase):

    def setUp(self):
        super().setUp()
        self.plant_wait('ivy', 'coordinator', expect_by=ago(3600))
        self.plant_wait('jo', self.ids['ivy'])
        self.age('ivy', 7200)
        self.age('jo', 7200)

    def test_rows_equal_the_areas_own_lanes_page_for_a_coordinator_and_a_contributor(self):
        for who in ('nora', 'ivy', 'jo'):
            row = self.area(who)
            page = self.lanes_page(who)
            self.assertTrue(row['visible'], who)
            self.assertEqual(self.shaped(page['items']), self.shaped(row['lanes_rows']), who)
            self.assertEqual(page['total'], row['lanes_total'], who)
            self.assertEqual({k: page['flags'][k] for k in ('waiting_too_long', 'expect_by_passed', 'your_move_idle')},
                             row['lanes'], who)

    def test_a_contributor_sees_only_their_own_lane_and_never_another_members_thread(self):
        ivy = self.area('ivy')
        self.assertEqual(['Ivy rollout plan'], [e['title'] for lane in ivy['lanes_rows'] for e in lane['held']])
        self.assertNotIn('Jo migration plan', json.dumps(ivy))
        self.assertNotIn(self.tasks['jo'], json.dumps(ivy))
        self.assertEqual(2, len(self.area('nora')['lanes_rows']))

    def test_the_rows_name_their_holders_and_never_show_an_id(self):
        row = self.area('nora')
        text = json.dumps(row)
        names = {lane['display_name'] for lane in row['lanes_rows']}
        self.assertEqual({'ivy', 'jo'}, names)
        for lane in row['lanes_rows']:
            self.assertNotIn('actor', lane)
            self.assertEqual(lane['display_name'], lane['lane'])
        waits = {e['waiting_on_name'] for lane in row['lanes_rows'] for e in lane['held']}
        self.assertEqual({'coordinator', 'ivy'}, waits)
        for who in ('ivy', 'jo'):
            self.assertNotIn(self.ids[who], text)


class HiddenRowTests(LanesCase):

    def test_an_unreadable_area_shows_no_row_no_count_and_no_hint(self):
        # south and north hold tasks; carl reads neither. Nothing of them is in the answer, byte for byte.
        for who in ('carl',):
            answer = self.program(who)
            body = answer.data
            for row in body['areas']['items']:
                self.assertFalse(row['visible'], (who, row))
                for key in ('lanes_rows', 'lanes', 'lanes_total', 'lanes_more', 'open_tasks'):
                    self.assertNotIn(key, row, (who, key))
            text = json.dumps(body)
            for secret in ('Ivy rollout plan', 'Jo migration plan', 'South secret plan', *self.tasks.values(),
                           self.ids['ivy'], self.ids['jo'], self.ids['sam'], 'ivy', 'jo'):
                self.assertNotIn(secret, text, (who, secret))

    def test_a_reader_of_one_area_sees_nothing_of_the_other(self):
        text = json.dumps(self.program('nora').data)
        self.assertNotIn('South secret plan', text)
        self.assertNotIn(self.tasks['sam'], text)
        south = {row['area']: row for row in self.program('nora').data['areas']['items']}['south']
        self.assertEqual(program_view.area_row('south', 'linked'), south)

    def test_a_row_hidden_by_the_recheck_loses_its_lane_rows_with_its_counts(self):
        # A backend that keeps answering the first read whatever the readable set (rights "keep changing"): the
        # route's recheck refuses north each time, so the rows it hides are the ones it is left with.
        import copy
        from http_auth import forbidden
        inner = self.backend.program
        first = inner('prog', readable=['prog', 'north', 'south'], lanes_as=None)
        self.assertTrue(any(row.get('lanes_rows') for row in first['areas']['items']))
        self.backend.program = lambda *args, **kw: copy.deepcopy(first)

        def refuse(principal, project):
            raise forbidden('no longer readable')
        with patch.object(self.service, 'coordinators_of', refuse):
            answer = self.program('nora').data
        rows = {row['area']: row for row in answer['areas']['items']}
        self.assertEqual(program_view.area_row('north', 'linked'), rows['north'])
        self.assertEqual(program_view.area_row('south', 'linked'), rows['south'])
        text = json.dumps(answer)
        for secret in ('Ivy rollout plan', 'Jo migration plan', 'South secret plan', self.tasks['ivy']):
            self.assertNotIn(secret, text)

    def test_a_hidden_row_is_the_four_fields_only(self):
        hidden = program_view.area_row('south', 'linked')
        self.assertEqual({'area', 'state', 'visible', 'note'}, set(hidden))


class IdleCountTests(LanesCase):

    def test_the_idle_count_equals_the_rows_flagged_when_the_clock_moves_past_the_threshold(self):
        for who in ('nora', 'ivy'):
            row = self.area(who)
            self.assertEqual(0, row['lanes']['your_move_idle'], who)
        self.age('ivy', 7200)                          # two hours of silence against the default fifteen minutes
        self.age('jo', 7200)
        for who, expected in (('nora', 2), ('ivy', 1), ('jo', 1)):
            row = self.area(who)
            flagged = sum(1 for lane in row['lanes_rows'] if lane['your_move_idle'])
            self.assertEqual(expected, row['lanes']['your_move_idle'], who)
            self.assertEqual(flagged, row['lanes']['your_move_idle'], who)
            self.assertEqual(expected, sum(1 for lane in row['lanes_rows'] for e in lane['held']
                                           if 'your_move_idle' in e['flags']), who)
        wide = self.area('nora', query='?idle_minutes=10080')
        self.assertEqual(0, wide['lanes']['your_move_idle'])

    def test_a_contributors_count_is_their_own_lane_not_the_areas(self):
        self.age('jo', 7200)
        self.assertEqual(1, self.area('nora')['lanes']['your_move_idle'])
        self.assertEqual(0, self.area('ivy')['lanes']['your_move_idle'])
        self.assertEqual([], [lane for lane in self.area('ivy')['lanes_rows'] if lane['your_move_idle']])


class RemovedMemberTests(LanesCase):

    def test_a_removed_member_loses_the_rows_at_once(self):
        self.assertTrue(self.area('ivy')['visible'])
        self.unmember('north', 'ivy')
        row = self.area('ivy')
        self.assertEqual(program_view.area_row('north', 'linked'), row)
        self.assertNotIn('Ivy rollout plan', json.dumps(self.program('ivy').data))

    def test_removal_between_the_backend_read_and_the_recheck_hides_rows_and_counts(self):
        inner = self.backend.program
        done = []

        def program(*args, **kw):
            answer = inner(*args, **kw)
            if not done:
                done.append(True)
                self.unmember('north', 'ivy')          # after the read, before the route's recheck
            return answer
        self.backend.program = program
        answer = self.program('ivy').data
        north = {row['area']: row for row in answer['areas']['items']}['north']
        self.assertEqual(program_view.area_row('north', 'linked'), north)
        self.assertEqual(0, answer['coverage']['linked_readable'])
        self.assertNotIn('Ivy rollout plan', json.dumps(answer))


class ProgramCoordinatorTests(LanesCase):
    """The one exception: the program's coordinator reads a linked area whole, on the area owner's consent."""

    def test_a_program_coordinator_who_is_only_a_viewer_in_the_area_sees_the_full_rows(self):
        cleo, nora = self.area('cleo'), self.area('nora')
        self.assertTrue(cleo['visible'])
        self.assertEqual(2, len(cleo['lanes_rows']))
        self.assertEqual(self.shaped(nora['lanes_rows']), self.shaped(cleo['lanes_rows']))
        self.assertEqual(nora['lanes'], cleo['lanes'])
        # and the area's own Lanes page does not show her that much: this is the exception, not the rule
        self.assertEqual([], self.lanes_page('cleo')['items'])

    def test_the_same_person_without_the_program_role_sees_only_their_own_scope(self):
        self.member('prog', 'cleo', 'contributor')
        row = self.area('cleo')
        self.assertTrue(row['visible'])
        self.assertEqual([], row['lanes_rows'])
        self.assertEqual(0, row['lanes_total'])
        self.assertEqual({'waiting_too_long': 0, 'expect_by_passed': 0, 'your_move_idle': 0}, row['lanes'])
        self.assertNotIn('Ivy rollout plan', json.dumps(self.program('cleo').data))

    def test_losing_the_program_role_ends_the_full_rows_on_the_next_read(self):
        self.assertEqual(2, len(self.area('cleo')['lanes_rows']))
        self.member('prog', 'cleo', 'viewer')
        self.assertEqual([], self.area('cleo')['lanes_rows'])

    def test_a_role_lost_between_the_read_and_the_recheck_hides_the_row(self):
        inner = self.backend.program
        done = []

        def program(*args, **kw):
            answer = inner(*args, **kw)
            if not done:
                done.append(True)
                self.member('prog', 'cleo', 'viewer')
            return answer
        self.backend.program = program
        answer = self.program('cleo').data
        self.assertNotIn('Ivy rollout plan', json.dumps(answer))
        self.assertNotIn('Jo migration plan', json.dumps(answer))

    def test_a_link_that_is_proposed_but_not_accepted_gives_no_all(self):
        # the area's side was never accepted by its owner: the link is one-sided
        self.links['north'] = [item(pl.MEMBER_OF, statement('Program', 'prog'), None, state='draft-only')]
        row = self.area('cleo')
        self.assertEqual('one-sided', row['state'])
        self.assertTrue(row['visible'])
        self.assertEqual([], row['lanes_rows'])
        self.assertEqual(0, row['lanes_total'])
        self.assertNotIn('Ivy rollout plan', json.dumps(self.program('cleo').data))
        # the program's side only proposed: the row is the hidden one
        self.links['prog'] = [item('program.area.north', statement('Area', 'north'), self.ids['pat'],
                                   state='draft-only', proposed=1), self.links['prog'][1]]
        self.links['north'] = [item(pl.MEMBER_OF, statement('Program', 'prog'), self.ids['nora'])]
        self.assertEqual(program_view.area_row('north', 'proposed'), self.area('cleo'))

    def test_a_revoked_link_removes_the_full_rows_on_the_next_read(self):
        self.assertEqual(2, len(self.area('cleo')['lanes_rows']))
        self.links['north'] = [item(pl.MEMBER_OF, statement('Program', 'prog', state='left'), self.ids['nora'])]
        row = self.area('cleo')
        self.assertEqual('one-sided', row['state'])
        self.assertEqual([], row['lanes_rows'])
        self.links['north'] = [item(pl.MEMBER_OF, statement('Program', 'prog'), self.ids['nora'])]
        self.assertEqual(2, len(self.area('cleo')['lanes_rows']))
        self.links['prog'][0] = item('program.area.north', statement('Area', 'north', state='left'), self.ids['pat'])
        self.assertEqual(program_view.area_row('north', 'left'), self.area('cleo'))

    def test_the_exception_needs_the_area_to_be_readable_at_all(self):
        # a coordinator of the program who is no member of south reads nothing of it, however linked
        row = {r['area']: r for r in self.program('cleo').data['areas']['items']}['south']
        self.assertEqual(program_view.area_row('south', 'linked'), row)
        self.assertNotIn('South secret plan', json.dumps(self.program('cleo').data))


class ScopeRuleTests(unittest.TestCase):

    def test_an_area_the_service_did_not_name_is_read_as_the_programs_own_lane_only(self):
        found = {'a': ('all', False, 'me')}
        self.assertEqual(('own', 'prog-actor'), program_view.lanes_scope_for_area(found, 'b', 'linked',
                                                                                  'prog-actor', True))
        self.assertEqual(('own', 'prog-actor'), program_view.lanes_scope_for_area(None, 'b', 'linked',
                                                                                  'prog-actor', True))
        self.assertEqual(('all', 'prog-actor'), program_view.lanes_scope_for_area(None, 'b', 'linked',
                                                                                  'prog-actor', False))

    def test_own_widens_only_for_the_program_coordinator_over_a_linked_area(self):
        for link, elevated, expected in (('linked', True, 'all'), ('linked', False, 'own'),
                                         ('one-sided', True, 'own'), ('proposed', True, 'own'),
                                         ('left', True, 'own')):
            got = program_view.lanes_scope_for_area({'a': ('own', elevated, 'me')}, 'a', link, 'p', True)
            self.assertEqual((expected, 'me'), got, (link, elevated))

    def test_the_arguments(self):
        rest, found = program_view.split_lanes_as(['--limit', '5', '--lanes-as', 'north|own|1|usr_x'])
        self.assertEqual((['--limit', '5'], {'north': ('own', True, 'usr_x')}), (rest, found))
        for bad in (['--lanes-as'], ['--lanes-as', 'north|some|0|x'], ['--lanes-as', 'north|own|2|x'],
                    ['--lanes-as', 'north|own']):
            with self.assertRaises(ValueError):
                program_view.split_lanes_as(bad)

    def test_a_visible_row_carries_rows_and_a_cut_is_said(self):
        lanes = [{'lane': 'l%02d' % n, 'actor': 'a%02d' % n, 'idle': False, 'held': [], 'waiting_too_long': False,
                  'expect_by_passed': False, 'your_move_idle': False} for n in range(60)]
        details = program_view.area_details([], {}, {'items': lanes, 'total': 60})
        row = program_view.area_row('north', 'linked', details)
        self.assertEqual((50, 60), (len(row['lanes_rows']), row['lanes_total']))
        self.assertIn('50 of 60', row['lanes_more'])


class PageTests(unittest.TestCase):

    def test_the_page_lists_lane_rows_in_a_collapsed_details_and_a_hidden_area_has_none(self):
        import shutil
        import test_http_web
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        code = """await import(process.argv[1]);
const view=await import(process.argv[2]);
const read=JSON.parse(process.argv[3]);
const host=document.createElement('div');
host.appendChild(view.areasTable(read.areas));
const find=(tag)=>{const found=[];const walk=(n)=>{if(n.tagName&&n.tagName.toLowerCase()===tag)found.push(n);
  for(const c of (n.childNodes||[]))walk(c);};walk(host);return found;};
console.log(JSON.stringify({details:find('details').length,links:find('a').map((a)=>a.getAttribute('href')),
  text:host.textContent,open:find('details').map((d)=>d.getAttribute('open')!==null),
  markup:find('img').length+find('script').length}));"""
        lane = {'lane': 'ivy', 'display_name': '<img src=x onerror=1>Ivy', 'idle': False, 'held': [
            {'task': 'north-1', 'title': 'Ivy rollout plan', 'state': 'waiting-on-coordinator',
             'waiting_on': 'coordinator', 'waiting_on_name': 'coordinator', 'expect_by': '2026-01-01T00:00:00Z',
             'flags': ['expect_by_passed']}]}
        read = {'areas': {'items': [
            {'area': 'north', 'state': 'linked', 'visible': True, 'open_tasks': 1,
             'lanes': {'waiting_too_long': 1, 'expect_by_passed': 1, 'your_move_idle': 0}, 'rows_read': 1,
             'coordinators': None, 'lanes_rows': [lane], 'lanes_total': 3, 'lanes_more': 'Showing 1 of 3 lanes.'},
            {'area': 'south', 'state': 'linked', 'visible': False, 'note': 'not visible to you'}]}}
        done = test_http_web.run_node_module(self, node, code, (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web/js/views/program.js').as_uri(), json.dumps(read))
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual((1, ['#/p/north/lanes'], [False], 0), (out['details'], out['links'], out['open'], out['markup']))
        for words in ('Lanes (3)', 'north-1 Ivy rollout plan', 'waiting-on-coordinator', 'expect_by_passed',
                      '<img src=x onerror=1>Ivy', 'Showing 1 of 3 lanes.', 'Lanes with an idle move'):
            self.assertIn(words, out['text'])
        self.assertNotIn('south-', out['text'])


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class RealBdEndpointBackendTests(unittest.TestCase):
    """pp is the program and qq its area, both real bd projects; two lanes hold a task each in qq. The real
    EndpointBackend builds the request and the real endpoint reads qq; only the link records are injected (their own
    reads are .446's, tested there)."""

    @classmethod
    def setUpClass(cls):
        cls.runtime = type('LanesRuntime', (fixture.RealBdLabelAliasTests,), {})
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

    def fake_links(self, rows, operators):
        ids = {str(row.get('id', '')).split('-', 1)[0] for row in rows if isinstance(row, dict)}
        if 'pp' in ids:
            return {'items': [item('program.area.qq', statement('Area', 'qq'), 'ops')]}
        if 'qq' in ids:
            return {'items': [item(pl.MEMBER_OF, statement('Program', 'pp'), 'ops')]}
        return {'items': []}

    def backend(self):
        config = fixture.endpoint.AuthorityConfig(str(self.root / 'unread-authority.json'))
        sent = []

        def runner(action, project, actor, args, attachments, operation_id, authority):
            sent.append(list(args))
            return fixture.endpoint.execute(self.root, {'action': action, 'project': project, 'actor': actor,
                                                        'args': list(args)}, authority_config=config)
        backend = http_service.EndpointBackend(
            sys.executable, str(KIT / 'endpoint.py'), str(self.root),
            service=SimpleNamespace(store=SimpleNamespace(unsaved_since=None, path=self.root / 'state.json')),
            runner=runner)
        return backend, sent

    def read(self, **kw):
        backend, sent = self.backend()
        with patch('reference_records.links', self.fake_links), \
                patch.object(http_service, 'project_unusable', lambda service, project: None):
            return backend.program('pp', readable=['qq'], **kw), sent

    def rows(self, answer):
        return {row['area']: row for row in answer['areas']['items']}['qq']

    def test_real_bd_the_counts_are_over_the_scope_the_rows_are(self):
        # A clock two hours on (program_answer's own `now`): every lane that holds a task is idle on its move. The
        # count is over the lanes the scope shows, so it equals the rows flagged, and the whole area's is more.
        from datetime import datetime, timedelta, timezone
        self.created('pp', 'The program itself, for counts')
        for assignee in ('carol', 'dave'):
            self.created('qq', assignee.title() + ' counted task', '--assignee', assignee)
        config = fixture.endpoint.AuthorityConfig(str(self.root / 'unread-authority.json'))
        later = datetime.now(timezone.utc) + timedelta(hours=2)

        def read(scope):
            with patch('reference_records.links', self.fake_links):
                view, _ = fixture.endpoint.program_answer(
                    self.root, self.root / 'projects' / 'pp', 'pp',
                    ['--readable', 'qq', '--lanes-as', 'qq|%s|0|carol' % scope], 'http/read',
                    authority_config=config, by_service=True,
                    project_path=lambda other: fixture.endpoint.project_dir(self.root, other), now=later)
            return {row['area']: row for row in view['areas']['items']}['qq']
        own, whole = read('own'), read('all')
        self.assertEqual(['carol'], [lane['lane'] for lane in own['lanes_rows']])
        self.assertEqual(1, own['lanes']['your_move_idle'])
        self.assertEqual(sum(1 for lane in whole['lanes_rows'] if lane['your_move_idle']),
                         whole['lanes']['your_move_idle'])
        self.assertGreaterEqual(whole['lanes']['your_move_idle'], 2)

    def test_real_bd_the_endpoint_reads_the_scope_the_service_names(self):
        self.created('pp', 'The program itself')
        for assignee, title in (('alice', 'Alice real task'), ('bob', 'Bob real task')):
            self.created('qq', title, '--assignee', assignee)
        lanes = lambda row: sorted(lane['lane'] for lane in row['lanes_rows'] if lane['lane'] in ('alice', 'bob'))
        answer, sent = self.read(lanes_as={'qq': ['all', False, 'alice']})
        self.assertEqual(['alice', 'bob'], lanes(self.rows(answer)))
        self.assertIn('qq|all|0|alice', sent[0])
        # the caller's own scope: alice's lane only, and the counts are over that lane
        answer, _ = self.read(lanes_as={'qq': ['own', False, 'alice']})
        row = self.rows(answer)
        self.assertEqual(['alice'], lanes(row))
        self.assertEqual(1, row['lanes_total'])
        self.assertNotIn('Bob real task', json.dumps(answer))
        # a program coordinator over a link that is linked on both sides: the whole area
        answer, _ = self.read(lanes_as={'qq': ['own', True, 'carol']})
        self.assertEqual(['alice', 'bob'], lanes(self.rows(answer)))
        # an area the service did not name is read as the program's own lane only: nobody here
        answer, _ = self.read()
        self.assertEqual([], self.rows(answer)['lanes_rows'])
        self.assertEqual(0, self.rows(answer)['lanes_total'])
        self.assertNotIn('Alice real task', json.dumps(answer))
        # a caller that is not the web service cannot name a scope
        with self.assertRaises(ValueError):
            fixture.endpoint.program_answer(Path('/nonexistent-root'), Path('/nonexistent-root/projects/pp'), 'pp',
                                            ['--lanes-as', 'qq|all|0|x'], 'op', key_projects=frozenset({'pp'}),
                                            by_service=False)


if __name__ == '__main__':
    unittest.main()
