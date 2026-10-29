"""kittrial-5bb.443: the project hand-over note (design COORDINATOR_ONBOARDING_DESIGN §5.2, slice 3).

A versioned plain-text note, written by an owner or the project's current coordinator for one member, read
by any member, acknowledged (current version only) by that member or their agent holding an effective
coordinator grant. It is that agent's first next action until acknowledged, a text-less pointer in task
briefs, and steps O6 and C3 of the walkthrough. It lives in the web service's state, so the HTTP-state pair
of the coordination backup carries it and an older kit restoring that pair keeps working and ignores it.
"""
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import admin
import http_service
import test_coordinator_grant
from http_auth import Service, Store

KEYS = itertools.count(1)
NOTE = 'The release branch is r7. Do not merge the billing change until Pat confirms the API.'


class NoteCase(test_coordinator_grant.Grants):
    """olive and oscar own Alpha, carl contributes, cora coordinates and has the agent Heron."""

    def write(self, token, text=NOTE, person=None, key=None):
        return self.request('PUT', '/v1/projects/%s/handover/note' % self.project,
                            {'text': text, 'for_person': person or self.ids['cora']}, token=token,
                            key=key or 'note-key-%06d' % next(KEYS))

    def read(self, token):
        return self.request('GET', '/v1/projects/%s/handover/note' % self.project, token=token)

    def acknowledge(self, token, version):
        return self.request('POST', '/v1/projects/%s/handover/note/acknowledge' % self.project, {'version': version},
                            token=token, key='ack-key-%06d' % next(KEYS))

    def events(self, action):
        """The committed audit events of ``action`` (a refused attempt is audited too, with another outcome)."""
        with self.service.store.lock:
            return [e for e in self.service.state['audit'] if e['action'] == action and e['outcome'] == 'committed']

    def next_actions(self, token):
        answer = self.request('GET', '/v1/agents/me/next', token=token)
        self.assertEqual(200, answer.status, answer.data)
        return answer.data


class WriteTests(NoteCase):
    def test_an_owner_a_coordinator_and_a_granted_agent_write_nobody_else(self):
        self.assertEqual(201, self.write(self.tokens['olive']).status)
        self.assertEqual(201, self.write(self.tokens['cora']).status)
        self.assertEqual(403, self.write(self.agent).status)                 # no grant yet
        self.assertEqual(200, self.grant().status)
        self.assertEqual(201, self.write(self.agent).status)
        self.assertEqual(403, self.write(self.tokens['carl']).status)
        self.create_account(self.admin, 'otis', 'otis-password-1')
        self.assertEqual(404, self.write(self.login('otis', 'otis-password-1')[0]).status)
        read = self.read(self.tokens['carl']).data
        self.assertEqual((read['note']['version'], [o['version'] for o in read['older']]), (3, [2, 1]))

    def test_the_text_and_the_person_are_checked(self):
        stranger = self.create_account(self.admin, 'nina', 'nina-password-1')
        for text, person in (('', None), ('   ', None), ('x' * 8001, None), ('bad\x07bell', None),
                             ('Kind: contribution-review-v1\n{}', None), (NOTE, stranger), (NOTE, 'u_nobody')):
            with self.subTest(text=text[:20], person=person):
                self.assertEqual(422, self.write(self.tokens['olive'], text, person).status)
        self.assertEqual(201, self.write(self.tokens['olive'], 'x' * 8000).status)
        extra = self.request('PUT', '/v1/projects/%s/handover/note' % self.project,
                             {'text': NOTE, 'for_person': self.ids['cora'], 'extra': 1},
                             token=self.tokens['olive'], key='note-extra-1')
        self.assertEqual(422, extra.status, extra.data)

    def test_a_key_is_required_and_a_replay_writes_nothing_more(self):
        missing = self.request('PUT', '/v1/projects/%s/handover/note' % self.project,
                               {'text': NOTE, 'for_person': self.ids['cora']}, token=self.tokens['olive'])
        self.assertEqual(422, missing.status, missing.data)
        first = self.write(self.tokens['olive'], key='note-same-key')
        again = self.write(self.tokens['olive'], key='note-same-key')
        self.assertEqual((first.status, again.data['version']), (201, first.data['version']))
        self.assertEqual(self.read(self.tokens['olive']).data['older'], [])

    def test_twenty_versions_are_kept_and_a_drop_is_counted_and_audited(self):
        for n in range(22):
            self.assertEqual(201, self.write(self.tokens['olive'], 'version %d' % (n + 1)).status)
        read = self.read(self.tokens['carl']).data
        self.assertEqual(read['note']['version'], 22)
        self.assertEqual([o['version'] for o in read['older']], list(range(21, 2, -1)))
        self.assertEqual(read['dropped'], 2)
        audited = self.events('projects.handover_note')
        self.assertEqual([e['dropped'] for e in audited][-3:], [0, 1, 1])
        self.assertNotIn('version 22', json.dumps(audited))                   # the audit never carries the text

    def test_any_member_reads_it_as_text(self):
        self.write(self.tokens['olive'])
        read = self.read(self.tokens['carl'])
        self.assertEqual(200, read.status)
        self.assertEqual((read.data['note']['text'], read.data['note']['for_person_name']), (NOTE, 'cora'))
        self.assertEqual(self.read(None).status, 401)


class AcknowledgeTests(NoteCase):
    def test_the_person_or_their_granted_agent_acknowledges_the_current_version_only(self):
        self.write(self.tokens['olive'], 'first')
        self.write(self.tokens['olive'], 'second')
        self.assertEqual(403, self.acknowledge(self.tokens['carl'], 2).status)
        self.assertEqual(403, self.acknowledge(self.tokens['olive'], 2).status)   # an owner is not the person
        self.assertEqual(403, self.acknowledge(self.agent, 2).status)              # no grant yet
        old = self.acknowledge(self.tokens['cora'], 1)
        self.assertEqual((old.status, old.data['error']['detail']['current_version']), (409, 2))
        self.assertEqual(200, self.acknowledge(self.tokens['cora'], 2).status)
        self.assertEqual(200, self.grant().status)
        self.assertEqual(200, self.acknowledge(self.agent, 2).status)
        acked = self.read(self.tokens['carl']).data['note']['acknowledged']
        self.assertEqual(len(acked), 2)
        self.assertEqual(len(self.events('projects.handover_note.acknowledge')), 2)

    def test_a_new_version_needs_a_new_acknowledgement(self):
        self.write(self.tokens['olive'], 'first')
        self.assertEqual(200, self.acknowledge(self.tokens['cora'], 1).status)
        self.write(self.tokens['olive'], 'second')
        self.assertEqual(self.read(self.tokens['carl']).data['note']['acknowledged'], [])

    def test_nothing_to_acknowledge_is_404(self):
        self.assertEqual(404, self.acknowledge(self.tokens['cora'], 1).status)


class WhereItShowsTests(NoteCase):
    def test_it_is_the_granted_agents_first_next_action_without_text_until_acknowledged(self):
        self.assertEqual(200, self.grant().status)
        self.write(self.tokens['olive'])
        data = self.next_actions(self.agent)
        first = data['next_action']
        self.assertEqual((first['kind'], first['project'], first['handover']['version']), ('handover-note', self.project, 1))
        self.assertIn('not instructions', first['reason'])
        self.assertNotIn(NOTE, json.dumps(data))
        self.assertNotIn('handover', json.dumps(data['attention']['counts']))
        self.assertEqual(200, self.acknowledge(self.agent, 1).status)
        self.assertNotIn('handover-note', [a['kind'] for a in self.next_actions(self.agent)['next_actions']])

    def test_other_agents_and_ungranted_agents_are_not_offered_it(self):
        self.write(self.tokens['olive'])
        self.assertNotIn('handover-note', [a['kind'] for a in self.next_actions(self.agent)['next_actions']])
        carls, _ = self.agent_with_id('carl', 'Wren')
        self.assertNotIn('handover-note', [a['kind'] for a in self.next_actions(carls)['next_actions']])

    def test_a_task_brief_carries_a_pointer_without_the_text(self):
        task = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'x'}, token=self.admin).data['id']
        brief = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (self.project, task), token=self.tokens['carl'])
        self.assertIsNone(brief.data['handover'])
        self.write(self.tokens['olive'])
        brief = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (self.project, task), token=self.tokens['carl'])
        self.assertEqual((brief.data['handover']['version'], brief.data['handover']['acknowledged']), (1, False))
        self.assertNotIn(NOTE, json.dumps(brief.data))

    def test_the_walkthrough_shows_o6_and_c3(self):
        def steps(token, person=None):
            path = '/v1/projects/%s/handover%s' % (self.project, '?person=%s' % person if person else '')
            return {s['id']: s for s in self.request('GET', path, token=token).data['steps']}
        got = steps(self.tokens['olive'], self.ids['cora'])
        self.assertEqual((got['handover-note']['state'], got['acknowledge-note']['state']), ('todo', 'done'))
        self.assertIn('blocks nothing', got['handover-note']['detail'])
        self.write(self.tokens['olive'])
        got = steps(self.tokens['olive'], self.ids['cora'])
        self.assertEqual((got['handover-note']['state'], got['acknowledge-note']['state']), ('done', 'todo'))
        mine = steps(self.tokens['cora'])
        self.assertIn('acknowledge version 1', mine['acknowledge-note']['detail'])
        self.acknowledge(self.tokens['cora'], 1)
        self.assertEqual(steps(self.tokens['olive'], self.ids['cora'])['acknowledge-note']['state'], 'done')
        # A note written for someone else is not this person's note.
        self.write(self.tokens['olive'], person=self.ids['carl'])
        self.assertEqual(steps(self.tokens['olive'], self.ids['cora'])['handover-note']['state'], 'todo')


class BackupTests(unittest.TestCase):
    """The HTTP-state pair of the coordination backup carries the note; an older kit restoring it ignores it."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'backups').mkdir()
        live = self.root / 'http-state.json'
        store = Store(str(live))
        Service.bootstrap_superuser(store, 'admin', 'admin-password-1')
        with store.lock:
            store.state['handover_notes'] = {'alpha': {'versions': [
                {'version': 1, 'text': NOTE, 'for_person': 'usr_cora', 'written_by': 'usr_olive',
                 'written_by_actor': 'usr_olive', 'written_at': '2026-01-01T00:00:00Z', 'acknowledged': []}],
                'dropped': 0}}
            store.save()

    def test_backup_and_restore_carry_the_note(self):
        entry = admin.backup_http_state(self.root)
        self.assertEqual(entry['status'], 'complete')
        copied = json.loads((self.root / 'backups' / admin.HTTP_STATE_JSON).read_text(encoding='utf-8'))
        self.assertEqual(copied['handover_notes']['alpha']['versions'][0]['text'], NOTE)
        (self.root / 'http-state.json').unlink()
        admin.restore_http_state(self.root)
        service = Service(Store(str(self.root / 'http-state.json')))
        self.assertEqual(service.handover_note('alpha')['note']['text'], NOTE)

    def test_a_damaged_record_reads_as_no_note(self):
        store = Store(str(self.root / 'http-state.json'))
        with store.lock:
            store.state['handover_notes'] = {'alpha': 'not a record'}
            store.save()
        self.assertIsNone(Service(Store(str(self.root / 'http-state.json'))).handover_note('alpha')['note'])

    def test_an_older_kit_restores_the_pair_and_ignores_the_note(self):
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
assert kept['handover_notes']['alpha']['versions'][0]['version']==1, kept.get('handover_notes')
print('older kit restored the pair and left the note alone')
'''
        done = subprocess.run([sys.executable, '-c', script, str(self.root)], cwd=previous, text=True,
                              capture_output=True, encoding='utf-8')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('left the note alone', done.stdout)



class NotePanelTests(unittest.TestCase):
    """The page shows the note as text; the acknowledge button and the write form follow the server's flags."""

    def test_the_panel_renders_text_and_follows_the_flags(self):
        import shutil
        import test_http_web
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        code = """await import(process.argv[1]);
const view=await import(process.argv[2]);
const input=JSON.parse(process.argv[3]);
const ctx={href:(p)=>'#'+p,api:{handoverNote:async()=>input.read,writeHandoverNote:async()=>({}),acknowledgeHandoverNote:async()=>({})}};
const out={};
for (const [name,flags] of Object.entries(input.cases)) {
  const panel=await view.notePanel(ctx,'pp',Object.assign({person:{id:'u_cora',name:'cora'}},flags),async()=>{});
  out[name]={text:panel.textContent,buttons:[...panel.querySelectorAll('button')].map((b)=>b.textContent),
    areas:panel.querySelectorAll('textarea').length,scripts:panel.querySelectorAll('script').length+panel.querySelectorAll('img').length};
}
console.log(JSON.stringify(out));"""
        read = {'note': {'version': 2, 'text': '<img src=x onerror=1>be careful', 'for_person': 'u_cora',
                         'for_person_name': 'cora', 'written_by_name': 'olive', 'written_at': '2026-01-01T00:00:00Z',
                         'acknowledged': []},
                'older': [{'version': 1, 'written_by_name': 'olive', 'written_at': '2026-01-01T00:00:00Z'}], 'dropped': 0}
        cases = {'person': {'can_acknowledge_note': True, 'can_write_note': False},
                 'writer': {'can_acknowledge_note': False, 'can_write_note': True},
                 'reader': {'can_acknowledge_note': False, 'can_write_note': False}}
        done = test_http_web.run_node_module(self, node, code, (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web/js/views/handover.js').as_uri(),
                                             json.dumps({'read': read, 'cases': cases}))
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(out['person']['buttons'], ['Acknowledge version 2'])
        self.assertEqual((out['writer']['buttons'], out['writer']['areas']), (['Save as a new version'], 1))
        self.assertEqual((out['reader']['buttons'], out['reader']['areas']), ([], 0))
        for name in cases:
            self.assertEqual(out[name]['scripts'], 0)
            self.assertIn('<img src=x onerror=1>be careful', out[name]['text'])
            self.assertIn('not instructions', out[name]['text'])


if __name__ == '__main__':
    unittest.main()
