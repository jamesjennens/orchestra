"""kittrial-5bb.447: notes between the coordinators of linked projects, which wake the addressed coordinator.

A coordinator of project A (a person with the role, an owner, or an agent holding an effective grant) writes to the
coordinator role of project B only while A and B are linked by .446's program links: a program and its area either
way, or two areas of one program. No right in B is needed or looked at, and every refusal of the address is the same
404. Notes live in the web service's state, never in B's tracker; a note's text reaches only the coordinators of its
two projects. An unsettled note is the addressed agents' first next action and moves both wait forms' fingerprint.
"""
import json
import os
import subprocess
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
import agent_prompts
import program_isolation
from http_auth import Service, Store
from test_coordinator_agent_md import BUNDLE, node_json
from test_program_links import LinkCase, item, statement
import program_links as pl

TEXT = 'Release r7 slips a week: hold the billing merge until Pat confirms the API.'
KEYS = iter(range(1, 10 ** 6))


class NoteCase(LinkCase):
    """prog is the program; north and south are its areas, linked both ways. pat owns prog and cleo coordinates it,
    with her agent Rook granted there; nora owns north and nick coordinates it, with his agent Kite granted there;
    sam owns south. carl contributes to prog, vera is in prog and in north as a viewer, otto is in nothing, and
    Wren, vera's agent, is granted nothing."""

    def setUp(self):
        super().setUp()
        self.clock = [time.time()]
        self.service._now = lambda: self.clock[0]
        for area in ('north', 'south'):
            self.admit(area)
            self.join(area)
        self.ids['nick'] = self.create_account(self.admin, 'nick', 'nick-password-1')
        self.assertEqual(200, self.request('PUT', '/v1/projects/north/members/%s' % self.ids['nick'],
                                           {'role': 'coordinator'}, token=self.admin).status)
        self.tokens['nick'] = self.login('nick', 'nick-password-1')[0]
        self.tokens['kite'] = self.granted_agent('nick', 'Kite', 'north', 'nora')
        self.tokens['rook'] = self.granted_agent('cleo', 'Rook', 'prog', 'pat')

    def granted_agent(self, person, name, project, owner):
        made = self.request('POST', '/v1/agents', {'name': name, 'working_directory': '/home/x/' + name,
                                                   'projects': [project]}, token=self.tokens[person])
        self.assertEqual(201, made.status, made.data)
        granted = self.request('PUT', '/v1/projects/%s/agents/%s/coordinator' % (project, made.data['agent']['id']),
                               {}, token=self.tokens[owner])
        self.assertEqual(200, granted.status, granted.data)
        return made.data['credential']['secret']

    def send(self, token, frm='prog', to='north', text=TEXT, reply_to=None, key=None):
        body = {'to_project': to, 'text': text}
        if reply_to is not None:
            body['reply_to'] = reply_to
        return self.request('POST', '/v1/projects/%s/notes' % frm, body, token=token,
                            key=key or 'note-key-%06d' % next(KEYS))

    def box(self, token, project='north', box='to', state='all'):
        answer = self.request('GET', '/v1/projects/%s/notes?box=%s&state=%s' % (project, box, state), token=token)
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    def events(self, action='notes.write'):
        with self.service.store.lock:
            return [e for e in self.service.state['audit'] if e['action'] == action]


class SendTests(NoteCase):

    def test_a_coordinator_writes_to_a_linked_project_both_ways_and_needs_no_right_there(self):
        # cleo and Rook are in nothing of north's; nora is in nothing of prog's.
        for token, frm, to in ((self.tokens['cleo'], 'prog', 'north'), (self.tokens['rook'], 'prog', 'north'),
                               (self.tokens['pat'], 'prog', 'south'), (self.tokens['nora'], 'north', 'prog'),
                               (self.tokens['kite'], 'north', 'prog')):
            made = self.send(token, frm, to)
            self.assertEqual(201, made.status, (frm, to, made.data))
            self.assertEqual((made.data['from']['project'], made.data['to'], made.data['settled']),
                             (frm, {'project': to, 'role': 'coordinator', 'installation': None}, None))
            self.assertRegex(made.data['id'], r'^cnote-[0-9a-f]{24}$')

    def test_two_areas_of_one_program_write_to_each_other(self):
        self.assertEqual(201, self.send(self.tokens['nora'], 'north', 'south').status)
        self.assertEqual(201, self.send(self.tokens['sam'], 'south', 'north').status)
        # An area that joined but that the program never admitted is no sibling, and no area writes to itself.
        from test_reference_records import OPERATOR
        self.links['east'] = [item(pl.MEMBER_OF, statement('Program', 'prog'), OPERATOR)]
        for to in ('east', 'north'):
            self.assertEqual(404, self.send(self.tokens['nora'], 'north', to).status, to)

    def test_every_refusal_of_the_address_is_the_same_404_whatever_the_sender_can_read(self):
        for pid in ('east', 'west'):
            self.assertEqual(201, self.request('POST', '/v1/projects', {'project_id': pid, 'name': pid.title()},
                                               token=self.admin).status)
        # cleo is a member of east and can read it; she is in nothing of west's.
        self.assertEqual(200, self.request('PUT', '/v1/projects/east/members/%s' % self.ids['cleo'],
                                           {'role': 'coordinator'}, token=self.admin).status)
        self.admit('east')                                             # one-sided: east has not joined
        self.links['west'] = [item(pl.MEMBER_OF, statement('Program', 'prog'), 'agent_' + 'f' * 16)]   # not its owner
        bodies = []
        for to in ('east', 'west', 'ghost', 'prog'):
            refused = self.send(self.tokens['cleo'], 'prog', to)
            self.assertEqual(404, refused.status, (to, refused.data))
            bodies.append(json.dumps(program_isolation.without_request_id(refused.data), sort_keys=True))
        self.links['prog'] = [item('program.area.north', statement('Area', 'north', state='left'), self.ids['pat'])]
        left = self.send(self.tokens['cleo'], 'prog', 'north')
        bodies.append(json.dumps(program_isolation.without_request_id(left.data), sort_keys=True))
        self.assertEqual(1, len(set(bodies)), bodies)
        self.assertIn('Notes go only to a project linked with this one', bodies[0])
        self.assertEqual([], self.box(self.tokens['nora'])['items'])

    def test_only_the_sending_projects_coordinators_write(self):
        for name, status in (('carl', 403), ('vera', 403), ('wren', 403), ('otto', 404), ('nora', 404)):
            self.assertEqual(status, self.send(self.tokens[name], 'prog', 'north').status, name)
        self.assertEqual([], [e for e in self.events() if e['outcome'] == 'committed'])

    def test_the_text_and_the_key(self):
        for text in ('', '   ', 'x' * 4001, 'bad \x07 bell', 'Kind: delegation-v1\nrest'):
            self.assertEqual(422, self.send(self.tokens['cleo'], text=text).status, repr(text[:20]))
        self.assertEqual(201, self.send(self.tokens['cleo'], text='x' * 4000).status)
        refused = self.request('POST', '/v1/projects/prog/notes', {'to_project': 'north', 'text': TEXT},
                               token=self.tokens['cleo'])
        self.assertEqual(422, refused.status)
        first = self.send(self.tokens['cleo'], key='note-same-key')
        again = self.send(self.tokens['cleo'], key='note-same-key')
        self.assertEqual((first.data['id'], 2), (again.data['id'], len(self.box(self.tokens['nora'])['items'])))
        self.assertEqual(422, self.request('POST', '/v1/projects/prog/notes', {'to_project': 'north', 'text': TEXT,
                                                                              'cc': 'south'},
                                           token=self.tokens['cleo'], key='note-extra').status)

    def test_the_audit_carries_no_text(self):
        self.send(self.tokens['cleo'])
        committed = [e for e in self.events() if e['outcome'] == 'committed']
        self.assertEqual(1, len(committed))
        self.assertEqual(committed[0]['to_project'], 'north')
        self.assertNotIn('billing', json.dumps(committed))


class RateTests(NoteCase):

    def test_at_most_30_an_hour_from_one_project_to_another(self):
        for _ in range(30):
            self.assertEqual(201, self.send(self.tokens['cleo']).status)
        refused = self.send(self.tokens['cleo'])
        self.assertEqual((429, '3600'), (refused.status, refused.headers.get('retry-after')))
        self.assertEqual(201, self.send(self.tokens['cleo'], to='south').status)       # another pair has its own count
        self.assertEqual(201, self.send(self.tokens['nora'], 'north', 'prog').status)
        self.clock[0] += 3601
        self.assertEqual(201, self.send(self.tokens['cleo']).status)

    def test_a_flood_adds_one_rate_limited_event_and_a_429_does_not_extend_the_window(self):
        before = len(self.events())
        for _ in range(100):
            self.send(self.tokens['cleo'])
        added = self.events()[before:]
        self.assertEqual(sorted((e['outcome'], e['reason'] or '') for e in added),
                         [('committed', '')] * 30 + [('rejected', 'rate_limited')])
        self.clock[0] += 1800
        for _ in range(30):
            self.assertEqual(429, self.send(self.tokens['cleo']).status)
        self.clock[0] += 1801                                          # an hour after the notes, not after the 429s
        self.assertEqual(201, self.send(self.tokens['cleo']).status)
        self.clock[0] -= 7200                                          # the clock steps back
        for _ in range(29):
            self.send(self.tokens['cleo'])
        self.assertEqual('3600', self.send(self.tokens['cleo']).headers.get('retry-after'))


class ReadTests(NoteCase):

    def test_each_project_reads_only_its_own_notes(self):
        sent = self.send(self.tokens['cleo']).data['id']
        other = self.send(self.tokens['cleo'], to='south').data['id']
        self.assertEqual([sent], [n['id'] for n in self.box(self.tokens['nora'])['items']])
        self.assertEqual([other], [n['id'] for n in self.box(self.tokens['sam'], 'south')['items']])
        self.assertEqual({sent, other}, {n['id'] for n in self.box(self.tokens['cleo'], 'prog', 'sent')['items']})
        self.assertEqual([], self.box(self.tokens['cleo'], 'prog', 'to')['items'])
        self.assertEqual(TEXT, self.box(self.tokens['nora'])['items'][0]['text'])

    def test_the_text_reaches_only_the_coordinators_of_its_two_projects(self):
        # The addition at VERIFIED, through the harness: every area read of the three projects and every personal
        # read, for everyone who is not a coordinator (owners included) of prog or north.
        secret = 'only-for-the-coordinators-of-prog-and-north'
        self.assertEqual(201, self.send(self.tokens['cleo'], text=secret).status)
        self.assertEqual(201, self.send(self.tokens['nora'], 'north', 'prog', text=secret).status)
        program_isolation.assert_text_hidden(
            self, self.request, secret,
            {'prog contributor': self.tokens['carl'], 'prog contributor, north viewer': self.tokens['vera'],
             'nobody': self.tokens['otto'], 'agent without a grant': self.tokens['wren'],
             'a sibling area\'s owner': self.tokens['sam']},
            ('prog', 'north', 'south'))
        for name, project, box in (('pat', 'prog', 'sent'), ('cleo', 'prog', 'sent'), ('rook', 'prog', 'sent'),
                                   ('nora', 'north', 'to'), ('nick', 'north', 'to'), ('kite', 'north', 'to')):
            self.assertIn(secret, json.dumps(self.box(self.tokens[name], project, box)), name)

    def test_notes_to_me_across_the_projects_i_coordinate(self):
        self.send(self.tokens['cleo'])
        self.send(self.tokens['sam'], 'south', 'north')
        mine = self.request('GET', '/v1/me/notes', token=self.tokens['nick']).data
        self.assertEqual((['north'], 2), (mine['projects'], mine['unsettled']))
        self.assertIsNotNone(mine['notes_marker'])
        self.assertEqual(0, self.request('GET', '/v1/me/notes', token=self.tokens['carl']).data['unsettled'])

    def test_pages_of_50(self):
        with patch.object(Service, 'NOTE_RATE', 100):
            for _ in range(51):
                self.send(self.tokens['cleo'])
        first = self.box(self.tokens['nora'])
        self.assertEqual((50, 51), (len(first['items']), first['total']))
        self.assertEqual(422, self.request('GET', '/v1/projects/north/notes?limit=51', token=self.tokens['nora']).status)


class ThreadTests(NoteCase):

    def test_a_reply_threads_and_settles_and_noted_settles_once(self):
        note = self.send(self.tokens['cleo']).data
        reply = self.send(self.tokens['nora'], 'north', 'prog', text='Held; Pat confirmed.', reply_to=note['id'])
        self.assertEqual(201, reply.status, reply.data)
        self.assertEqual((reply.data['thread'], reply.data['reply_to']), (note['id'], note['id']))
        settled = self.box(self.tokens['nora'])['items'][0]['settled']
        self.assertEqual((settled['how'], settled['reply']), ('replied', reply.data['id']))
        other = self.send(self.tokens['cleo']).data
        self.assertEqual(200, self.request('POST', '/v1/projects/north/notes/%s/noted' % other['id'], {},
                                           token=self.tokens['kite'], key='noted-key-000001').status)
        twice = self.request('POST', '/v1/projects/north/notes/%s/noted' % other['id'], {}, token=self.tokens['nick'],
                             key='noted-key-000002')
        self.assertEqual((409, 'noted'), (twice.status, twice.data['error']['detail']['settled']['how']))

    def test_a_reply_only_to_a_note_addressed_here_from_that_project(self):
        note = self.send(self.tokens['cleo']).data
        for frm, to, token in (('south', 'prog', self.tokens['sam']), ('north', 'south', self.tokens['nora'])):
            refused = self.send(token, frm, to, reply_to=note['id'])
            self.assertEqual(404, refused.status, (frm, to))
        self.assertEqual(404, self.request('POST', '/v1/projects/south/notes/%s/noted' % note['id'], {},
                                           token=self.tokens['sam'], key='noted-key-000003').status)


class WakeTests(NoteCase):

    def next_(self, token):
        answer = self.request('GET', '/v1/agents/me/next', token=token)
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    def wait(self, token, form=None):
        answer = self.request('GET', '/v1/agents/me/wait' + ('?form=coordinator' if form else ''), token=token)
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    def test_the_addressed_agent_is_woken_first_without_the_text(self):
        quiet = (self.next_(self.tokens['kite'])['coordinator_notes_marker'], self.wait(self.tokens['kite'])['fingerprint'],
                 self.wait(self.tokens['kite'], 'coordinator')['fingerprint'])
        self.assertIsNone(quiet[0])
        note = self.send(self.tokens['cleo']).data
        woken = self.next_(self.tokens['kite'])
        self.assertEqual(('coordinator-note', note['id']), (woken['next_action']['kind'],
                                                             woken['next_action']['coordinator_note']['note']))
        for answer in (woken, self.wait(self.tokens['kite']), self.wait(self.tokens['kite'], 'coordinator')):
            self.assertNotIn('billing', json.dumps(answer))
        loud = (woken['coordinator_notes_marker'], self.wait(self.tokens['kite'])['fingerprint'],
                self.wait(self.tokens['kite'], 'coordinator')['fingerprint'])
        self.assertTrue(all(a != b for a, b in zip(quiet, loud)), (quiet, loud))
        # Settled, the wake goes and both fingerprints return to what they were with no note.
        self.request('POST', '/v1/projects/north/notes/%s/noted' % note['id'], {}, token=self.tokens['nick'],
                     key='noted-wake')
        self.assertEqual(quiet, (self.next_(self.tokens['kite'])['coordinator_notes_marker'],
                                 self.wait(self.tokens['kite'])['fingerprint'],
                                 self.wait(self.tokens['kite'], 'coordinator')['fingerprint']))

    def test_no_other_agent_is_woken(self):
        # A note to north wakes nobody of prog's; a note to prog wakes Rook (granted there) and never Wren, who is
        # in prog without a grant.
        self.send(self.tokens['cleo'])
        for name in ('rook', 'wren'):
            body = self.next_(self.tokens[name])
            self.assertIsNone(body['coordinator_notes_marker'], name)
        self.send(self.tokens['nora'], 'north', 'prog')
        self.assertIsNotNone(self.next_(self.tokens['rook'])['coordinator_notes_marker'])
        for name in ('wren',):
            body = self.next_(self.tokens[name])
            self.assertIsNone(body['coordinator_notes_marker'], name)
            self.assertNotIn('coordinator-note', [a['kind'] for a in body['next_actions']], name)


class StoreTests(NoteCase):

    def test_the_oldest_settled_notes_are_dropped_first_and_counted(self):
        with patch.object(Service, 'NOTES_KEPT', 2):
            first = self.send(self.tokens['cleo']).data['id']
            second = self.send(self.tokens['cleo']).data['id']
            self.request('POST', '/v1/projects/north/notes/%s/noted' % second, {}, token=self.tokens['nick'],
                         key='noted-drop')
            third = self.send(self.tokens['cleo'])
            self.assertEqual(201, third.status)
        listed = self.box(self.tokens['nora'])
        self.assertEqual(({first, third.data['id']}, 1), ({n['id'] for n in listed['items']}, listed['dropped']))
        dropped = [e for e in self.events() if e['outcome'] == 'committed' and e.get('dropped')]
        self.assertEqual(1, len(dropped))

    def test_a_damaged_record_reads_as_none_and_refuses_a_write(self):
        with self.service.store.lock:
            self.service.state['coordinator_notes'] = {'notes': ['junk', {'id': 'cnote-' + '0' * 24}], 'dropped': 7}
        self.assertEqual([], self.box(self.tokens['nora'])['items'])
        self.assertEqual(201, self.send(self.tokens['cleo']).status)            # the list is fine, so a write works
        with self.service.store.lock:
            self.service.state['coordinator_notes'] = 'not a record'
        self.assertEqual([], self.box(self.tokens['nora'])['items'])
        self.assertEqual(503, self.send(self.tokens['cleo']).status)


class BackupTests(unittest.TestCase):
    """The HTTP-state pair carries the notes; an older kit restoring it keeps them."""

    NOTE = {'schema_version': 1, 'id': 'cnote-' + 'a' * 24, 'text': TEXT, 'created_at': '2026-01-01T00:00:00Z',
            'from': {'project': 'prog', 'user_id': 'usr_' + '1' * 16, 'agent_id': None, 'kind': 'person',
                     'actor': 'usr_' + '1' * 16, 'name': 'cleo'},
            'to': {'project': 'north', 'role': 'coordinator', 'installation': None},
            'reply_to': None, 'thread': 'cnote-' + 'a' * 24, 'settled': None}

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'backups').mkdir()
        store = Store(str(self.root / 'http-state.json'))
        Service.bootstrap_superuser(store, 'admin', 'admin-password-1')
        with store.lock:
            store.state['coordinator_notes'] = {'notes': [dict(self.NOTE)], 'dropped': {}}
            store.save()

    def test_backup_and_restore_carry_the_notes(self):
        self.assertEqual('complete', admin.backup_http_state(self.root)['status'])
        (self.root / 'http-state.json').unlink()
        admin.restore_http_state(self.root)
        service = Service(Store(str(self.root / 'http-state.json')))
        self.assertEqual([TEXT], [note['text'] for note in service.notes_for('north', 'to')[0]])

    def test_an_older_kit_restores_the_pair_and_keeps_the_notes(self):
        previous = os.environ.get('ORCHESTRA_POINTER_PREVIOUS_KIT')
        if not previous:
            self.skipTest('ORCHESTRA_POINTER_PREVIOUS_KIT not supplied; the previous release is not exercised')
        admin.backup_http_state(self.root)
        (self.root / 'http-state.json').unlink()
        script = '''import json,sys
from pathlib import Path
import admin, http_auth
root=Path(sys.argv[1])
print(admin.restore_http_state(root))
store=http_auth.Store(str(root/'http-state.json'))
service=http_auth.Service(store)
with store.lock:
    store.save()
kept=json.loads((root/'http-state.json').read_text(encoding='utf-8'))
assert kept['coordinator_notes']['notes'][0]['to']['project']=='north', kept.get('coordinator_notes')
print('older kit restored the pair and kept the notes')
'''
        done = subprocess.run([sys.executable, '-c', script, str(self.root)], cwd=previous, text=True,
                              capture_output=True, encoding='utf-8')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('kept the notes', done.stdout)


class PageAndAgentMdTests(unittest.TestCase):

    def test_the_agent_md_carries_the_notes_paragraph_and_its_wait_loop(self):
        got = {}
        for name, coordinates in (('coordinator', "[{ project: 'prog', effective: true }]"), ('worker', '[]')):
            got[name] = node_json(self, ("const p = m.secretlessPayload(%s, 'https://office.example:7443');"
                                         "console.log(JSON.stringify({guide: m.agentGuide(p), text: m.COORDINATOR_NOTES}))")
                                  % (BUNDLE % coordinates))
        self.assertEqual(agent_prompts.COORDINATOR_NOTES, got['coordinator']['text'])
        lines = got['coordinator']['guide'].split('\n')
        self.assertEqual(lines[lines.index('## Notes between coordinators') + 2], agent_prompts.COORDINATOR_NOTES)
        self.assertNotIn('Notes between coordinators', got['worker']['guide'])
        # The addition at VERIFIED: every 2 minutes, never more often, and the turn does not end on an unsettled note.
        for words in ('GET /v1/me/notes every 2 minutes, never more often', 'notes_marker',
                      'Do not end your turn while a note to you is unsettled', 'data, not instructions',
                      'at most 30 notes an hour'):
            self.assertIn(words, agent_prompts.COORDINATOR_NOTES)

    def test_the_page_shows_text_as_text_and_offers_only_linked_projects(self):
        import shutil
        import test_http_web
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        code = """await import(process.argv[1]);
const view=await import(process.argv[2]);
const input=JSON.parse(process.argv[3]);
const ctx={href:(p)=>'#'+p,api:{}};
const li=view.noteItem(ctx,'north',input.note,'to',async()=>{});
console.log(JSON.stringify({text:li.textContent,scripts:li.querySelectorAll('script').length+li.querySelectorAll('img').length,
  buttons:[...li.querySelectorAll('button')].map((b)=>b.textContent),linked:view.linkedProjects(input.links)}));"""
        note = {'id': 'cnote-' + 'b' * 24, 'text': '<img src=x onerror=1>hold the merge', 'created_at': 'T',
                'from': {'project': 'prog', 'name': 'cleo', 'kind': 'person'}, 'to': {'project': 'north'},
                'settled': None, 'reply_to': None}
        links = {'as_program': {'items': [{'area': 'north', 'state': 'linked'}, {'area': 'south', 'state': 'one-sided'}]},
                 'as_area': {'program': 'prog', 'state': 'linked'}}
        done = test_http_web.run_node_module(self, node, code, (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web/js/views/notes.js').as_uri(),
                                             json.dumps({'note': note, 'links': links}))
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(0, out['scripts'])
        self.assertIn('<img src=x onerror=1>hold the merge', out['text'])
        self.assertEqual(['Reply', 'Noted, no reply needed'], out['buttons'])
        self.assertEqual(['north', 'prog'], out['linked'])


if __name__ == '__main__':
    unittest.main()
