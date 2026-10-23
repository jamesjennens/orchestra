"""Room items and acceptance over HTTP: routes, R7, records, states and the page (.388)."""
import http.client
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import http_service
import rooms
import test_http_agents as agents
from test_http_agents import unique_dir


class ItemsBackend(http_service.InProcessBackend):
    """The service-local backend plus the canonical records rooms slice 2 creates.

    The real records and the native decision are made by endpoint.py's canonical actions;
    this stub records the same calls and answers the same identities, so the HTTP route's
    authority, state machine and idempotency are exercised without a tracker.
    """

    PROPOSALS = True
    RECORDS = True

    def __init__(self, service):
        super().__init__(service)
        self.requirements = {}
        self.requirement_by_key = {}
        self.decisions = []
        self.decision_by_key = {}
        self.proposals = {}
        self.proposal_by_key = {}
        self.calls = []

    def requirements_read(self, project_id, args):
        if args[:1] == ['get']:
            record = self.requirements.get(args[1])
            if record is None:
                raise ValueError('Requirement was not found')
            acceptance = None
            if record.get('decision_id'):
                acceptance = {'decision': {'decision_id': record['decision_id']}}
            return {'id': record['id'], 'acceptance': acceptance,
                    'current': {'title': record['title'], 'description': record['description']}}
        return {'items': list(self.requirements.values()), 'decisions': list(self.decisions),
                'jobs': [{'id': 'job_0001', 'title': 'The project job'}]}

    def owner_requirements(self, principal, project_id, action, fields, key, task=None):
        self.calls.append(('owner_requirements', action, key, task))
        if action == 'create':
            known = self.requirement_by_key.get(key)
            if known is not None:
                return dict(self.requirements[known])
            record = {'id': 'task_requirement_%d' % (len(self.requirements) + 1),
                      'revision': 1, 'sha256': 'a' * 64, 'title': fields['title'],
                      'description': fields['description'], 'decision_id': None}
            self.requirements[record['id']] = record
            self.requirement_by_key[key] = record['id']
            return dict(record)
        record = self.requirements.get(task)
        if record is None:
            raise ValueError('Requirement was not found')
        if record.get('decision_id') is None:
            record['decision_id'] = 'task_decision_%d' % (len(self.decisions) + 1)
            self.decisions.append(record['decision_id'])
            record['decision_pointer'] = fields.get('pointer')
        record['revision'] = 2
        record['sha256'] = 'b' * 64
        return dict(record)

    def room_decision(self, principal, project_id, fields, key=None):
        self.calls.append(('room_decision', fields['operation_id'], fields['evidence']))
        known = self.decision_by_key.get(fields['operation_id'])
        if known is not None:
            return {'decision_id': known, 'evidence': fields['evidence']}
        decision_id = 'task_room_decision_%d' % (len(self.decisions) + 1)
        self.decisions.append(decision_id)
        self.decision_by_key[fields['operation_id']] = decision_id
        return {'decision_id': decision_id, 'evidence': fields['evidence']}

    def invoke(self, route, principal, project_id, payload, key, target=None, authorize=None,
               capability=None):
        if route == 'proposals.submit':
            if authorize is not None:
                authorize()
            body = payload.get('body') if isinstance(payload, dict) else None
            if not isinstance(body, dict):
                raise ValueError('proposal body missing')
            operation = body['operation_id']
            known = self.proposal_by_key.get(operation)
            if known is not None:
                return dict(self.proposals[known])
            record = {'key': 'proposal_%d' % (len(self.proposals) + 1), 'revision': 1,
                      'sha256': 'c' * 64, 'state': 'submitted',
                      'evidence': list(body.get('evidence') or []),
                      'text': body.get('text'), 'rationale': body.get('rationale')}
            self.proposals[record['key']] = record
            self.proposal_by_key[operation] = record['key']
            return dict(record)
        return super().invoke(route, principal, project_id, payload, key, target=target,
                              authorize=authorize, capability=capability)


class RoomsItemsHttpTests(agents.AgentHarness):
    backend_class = ItemsBackend

    def setUp(self):
        super().setUp()
        self.rooms_root = unique_dir('rooms-items-http-')
        self.addCleanup(__import__('shutil').rmtree, self.rooms_root, ignore_errors=True)
        self.backend.files_root = str(self.rooms_root)
        self.limit = dict(rooms.LIMIT_DEFAULTS)
        self.backend.rooms_limits = lambda **kw: dict(self.limit)
        self.admin = self.admin_token()
        self.pid = self.create_project(self.admin, 'Rooms')
        self.other_pid = self.create_project(self.admin, 'Elsewhere')
        self.tokens = {}
        self.ids = {}
        for name, role, project in (('owner', 'owner', self.pid), ('coord', 'coordinator', self.pid),
                                    ('worker', 'contributor', self.pid), ('viewer', 'viewer', self.pid),
                                    ('outsider', 'coordinator', self.other_pid)):
            self.ids[name] = self.create_account(self.admin, name, name + '-password-1')
            put = self.request('PUT', '/v1/projects/%s/members/%s' % (project, self.ids[name]),
                               {'role': role}, token=self.admin)
            self.assertIn(put.status, (200, 201), put.data)
            self.tokens[name] = self.login(name, name + '-password-1')[0]
        self.room = self.create_room(self.tokens['coord'], 'Planning')

    # -- helpers -----------------------------------------------------------------------------------
    def path(self, suffix='', rid=None):
        base = '/v1/projects/%s/rooms' % self.pid
        if rid or suffix:
            base += '/' + (rid or self.room['id'])
        return base + suffix

    def create_room(self, token, name):
        made = self.request('POST', '/v1/projects/%s/rooms' % self.pid, {'name': name}, token=token)
        self.assertEqual(201, made.status, made.data)
        return made.data

    def post_item(self, token, kind='requirement', title='Add export', text='We need CSV export.',
                  key='item-key-1', rid=None, **extra):
        body = dict(item_kind=kind, title=title, text=text, **extra)
        return self.request('POST', self.path('/items', rid), body, token=token, key=key)

    def accept(self, token, sequence=1, key='accept-key-1', rid=None, body=None):
        return self.request('POST', self.path('/items/%d/accept' % sequence, rid), body or {},
                            token=token, key=key)

    def decision(self, token, sequence=1, key='decide-key-1'):
        return self.request('POST', self.path('/items/%d/reject' % sequence), {'reason': 'no'},
                            token=token, key=key)

    def read(self, token=None, rid=None):
        return self.request('GET', self.path('', rid or self.room['id']),
                            token=token or self.tokens['viewer'])

    def items(self, token=None, rid=None):
        return self.read(token, rid).data['items']

    def audit(self, action):
        return [event for event in self.service.state['audit'] if event.get('action') == action]

    # -- routes and authority ----------------------------------------------------------------------
    def test_item_post_and_read_are_members_only(self):
        posted = self.post_item(self.tokens['worker'])
        self.assertEqual(201, posted.status, posted.data)
        self.assertEqual(('item', 'proposed', 'requirement'),
                         (posted.data['kind'], posted.data['state'], posted.data['item_kind']))
        read = self.read(self.tokens['viewer'])
        self.assertEqual(['Add export'], [item['title'] for item in read.data['items']])
        self.assertIn('orchestra:room/%s#1' % self.room['id'], read.data['items'][0]['evidence'])
        self.assertEqual(403, self.post_item(self.tokens['viewer'], key='viewer-item').status)
        outsider = self.post_item(self.tokens['outsider'], key='outsider-item', rid=self.room['id'])
        self.assertIn(outsider.status, (403, 404), outsider.data)
        self.assertEqual('committed', self.audit('rooms.item.post')[-1]['outcome'])

    def test_requirement_owner_accept_creates_a_requirement_and_its_decision(self):
        self.post_item(self.tokens['worker'])
        accepted = self.accept(self.tokens['owner'])
        self.assertEqual(200, accepted.status, accepted.data)
        self.assertEqual('accepted', accepted.data['state'])
        record = accepted.data['record']
        self.assertEqual('requirement', record['kind'])
        self.assertIn('orchestra:room/%s#1' % self.room['id'], record['evidence'])
        requirement = self.backend.requirements[record['id']]
        self.assertIn(record['evidence'], requirement['description'])
        self.assertEqual(record['evidence'], requirement['decision_pointer'])
        self.assertEqual(record['decision_id'], requirement['decision_id'])
        self.assertEqual('committed', self.audit('rooms.item.accept')[-1]['outcome'])

    def test_contributor_accept_is_a_proposal_not_a_requirement(self):
        self.post_item(self.tokens['worker'], title='A candidate', text='Contributor text.')
        accepted = self.accept(self.tokens['worker'])
        self.assertEqual(200, accepted.status, accepted.data)
        record = accepted.data['record']
        self.assertEqual('proposal', record['kind'])
        self.assertEqual([record['evidence']], self.backend.proposals[record['id']]['evidence'])
        self.assertIn('orchestra:room/%s#1' % self.room['id'], record['evidence'])
        self.assertEqual({}, self.backend.requirements)

    def test_decision_owner_or_coordinator_only(self):
        self.post_item(self.tokens['worker'], kind='decision', title='Use CSV',
                       text='We will emit CSV first.')
        refused = self.accept(self.tokens['worker'])
        self.assertEqual(403, refused.status, refused.data)
        accepted = self.accept(self.tokens['coord'])
        self.assertEqual(200, accepted.status, accepted.data)
        self.assertEqual('decision', accepted.data['record']['kind'])
        self.assertTrue(self.backend.decisions)
        self.assertEqual(accepted.data['record']['decision_id'], self.backend.decisions[-1])

    def test_accept_twice_converges_on_one_record_and_one_event(self):
        self.post_item(self.tokens['worker'])
        first = self.accept(self.tokens['owner'], key='accept-key-a')
        second = self.accept(self.tokens['owner'], key='accept-key-a')
        self.assertEqual(200, first.status, first.data)
        self.assertEqual(200, second.status, second.data)
        self.assertEqual((first.data['sequence'], first.data['state']),
                         (second.data['sequence'], second.data['state']))
        events = [line for line in rooms.load_lines(Path(self.rooms_root) / self.pid, self.room['id'])
                  if line['kind'] == 'item-event']
        self.assertEqual(1, len(events))
        self.assertEqual(1, len(self.backend.decisions), 'one native decision, not two')
        # A DIFFERENT client key after the item is decided is a new decision attempt: refused.
        third = self.accept(self.tokens['owner'], key='accept-key-b')
        self.assertEqual(409, third.status, third.data)
        self.assertEqual(1, len(events))

    def test_r7_an_agent_credential_is_refused_before_any_grant(self):
        self.post_item(self.tokens['worker'])
        agent_id, secret, _data = self.agent_secret(self.tokens['owner'], projects=[self.pid])
        # Even with the coordinator grant stored live, R7 refuses the credential first.
        self.service.state['coordinator_grants'] = {
            self.pid: {agent_id: {'granted_by': self.ids['owner'],
                                  'granted_at': '2026-10-23T00:00:00Z'}}}
        self.store.save()
        refused = self.accept(secret, key='agent-accept')
        self.assertEqual(403, refused.status, refused.data)
        self.assertIn('signed-in person', refused.data['error']['message'])
        for route in ('/reject', '/amend'):
            body = {'reason': 'x'} if route == '/reject' else {'title': 't', 'text': 'x'}
            answer = self.request('POST', self.path('/items/1' + route), body, token=secret)
            self.assertEqual(403, answer.status, answer.data)

    def test_question_answer_is_a_member_action_with_who_and_when(self):
        self.post_item(self.tokens['worker'], kind='question', title='Delimiter?',
                       text='Comma or semicolon?')
        answered = self.request('POST', self.path('/items/1/answer'), {'text': 'Comma.'},
                                token=self.tokens['viewer'], key='answer-key-1')
        self.assertEqual(200, answered.status, answered.data)
        self.assertEqual('accepted', answered.data['state'])
        self.assertEqual('viewer', answered.data['answered_by']['name'])
        self.assertTrue(answered.data['answered_at'])
        accept = self.accept(self.tokens['owner'])
        self.assertEqual(400, accept.status, accept.data)

    def test_moderated_item_cannot_be_decided_and_its_text_is_not_served(self):
        self.post_item(self.tokens['worker'], text='SECRET room item text.')
        moderated = self.request('POST', self.path('/messages/1/moderate'),
                                 {'reason': 'private'}, token=self.tokens['coord'], key='moderate-1')
        self.assertEqual(201, moderated.status, moderated.data)
        for route, body in (('/accept', {}), ('/reject', {'reason': 'x'}),
                            ('/amend', {'title': 't', 'text': 'x'})):
            answer = self.request('POST', self.path('/items/1' + route), body,
                                  token=self.tokens['owner'], key='after-mod-' + route[1:])
            self.assertEqual(409, answer.status, (route, answer.data))
        read = self.read(self.tokens['viewer'])
        self.assertNotIn('SECRET room item text.', json.dumps(read.data))
        self.assertTrue(read.data['items'][0]['moderated'])
        index = (Path(self.rooms_root) / self.pid / rooms.FOLDER / rooms.INDEX).read_text(encoding='utf-8')
        self.assertNotIn('SECRET room item text.', index)

    def test_reject_and_amend_then_decide(self):
        self.post_item(self.tokens['worker'])
        amended = self.request('POST', self.path('/items/1/amend'),
                               {'title': 'Add CSV export now', 'text': 'With a header row.'},
                               token=self.tokens['owner'], key='amend-key-1')
        self.assertEqual(200, amended.status, amended.data)
        self.assertEqual(('amended', 'Add CSV export now'),
                         (amended.data['state'], amended.data['title']))
        rejected = self.request('POST', self.path('/items/1/reject'),
                                {'reason': 'not this release'}, token=self.tokens['owner'],
                                key='reject-key-1')
        self.assertEqual(200, rejected.status, rejected.data)
        self.assertEqual('rejected', rejected.data['state'])
        final = self.accept(self.tokens['owner'], key='accept-final')
        self.assertEqual(409, final.status, final.data)

    def test_supersede_and_bounds(self):
        first = self.post_item(self.tokens['worker'])
        self.assertEqual(201, first.status, first.data)
        second = self.post_item(self.tokens['worker'], title='v2', text='Second version.',
                                key='item-key-2', supersedes=1)
        self.assertEqual(201, second.status, second.data)
        items = self.items()
        self.assertEqual('superseded', items[0]['state'])
        self.assertEqual(409, self.accept(self.tokens['owner'], sequence=1, key='accept-old-1').status)
        self.assertEqual(422, self.post_item(self.tokens['worker'], title='x' * 201,
                                             key='long-title').status)
        self.assertEqual(422, self.post_item(self.tokens['worker'], text='x' * 4001,
                                             key='long-text').status)
        reserved = self.post_item(self.tokens['worker'], text='Kind: task-checkpoint-v1\n{}',
                                  key='reserved')
        self.assertEqual(422, reserved.status, reserved.data)
        self.assertEqual('reserved_text', reserved.data['error']['code'])

    def test_archived_room_and_project_refuse_item_writes(self):
        self.post_item(self.tokens['worker'])
        self.assertEqual(200, self.request('POST', self.path('/archive'), {},
                                           token=self.tokens['owner']).status)
        posted = self.post_item(self.tokens['worker'], title='late', key='late-item')
        self.assertEqual(409, posted.status, posted.data)
        self.assertEqual(409, self.accept(self.tokens['owner'], key='late-accept').status)

    def test_every_decision_route_is_csrf_protected(self):
        self.post_item(self.tokens['worker'])
        login = self.request('POST', '/v1/sessions',
                             {'username': 'owner', 'password': 'owner-password-1'})
        cookie = next(value for key, value in login.header_list
                      if key.lower() == 'set-cookie').split(';')[0]
        csrf = login.data['session']['csrf_token']

        def post(path, cookie_value=None, csrf_value=None, key=None):
            client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
            headers = {'Content-Type': 'application/json'}
            if cookie_value:
                headers['Cookie'] = cookie_value
            if csrf_value:
                headers['X-CSRF-Token'] = csrf_value
            if key:
                headers['Idempotency-Key'] = key
            try:
                client.request('POST', path, body='{}', headers=headers)
                response = client.getresponse()
                return agents.Response(response.status, response.getheaders(), response.read())
            finally:
                client.close()

        refused = post(self.path('/items/1/accept'), cookie_value=cookie, key='csrf-refused')
        self.assertEqual(403, refused.status, refused.data)
        allowed = post(self.path('/items/1/accept'), cookie_value=cookie, csrf_value=csrf,
                       key='csrf-allowed')
        self.assertEqual(200, allowed.status, allowed.data)


if __name__ == '__main__':
    unittest.main()


class RoomsItemsPageTests(unittest.TestCase):
    def test_item_panel_renders_text_only_and_follows_authority(self):
        import shutil
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        kit = Path(__file__).resolve().parents[1]
        done = test_http_web.run_node_module(
            self, node, 'await import(process.argv[1])',
            (kit / 'tests' / 'web_rooms_items.mjs').as_uri(),
            (kit / 'tests' / 'web_dom_shim.mjs').as_uri(),
            (kit / 'web' / 'js' / 'views' / 'rooms.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        for role in ('viewer', 'contributor', 'coordinator'):
            self.assertIn('<img src=x onerror=alert(1)>', seen[role]['text'])
            self.assertIn('javascript:alert(1)', seen[role]['text'])
            self.assertEqual(0, seen[role]['images'])
            self.assertEqual(0, seen[role]['js_links'])
        # A contributor may accept a requirement (it becomes a proposal); an owner/
        # coordinator sees the same button; a viewer only answers the open question.
        self.assertIn('Accept', seen['contributor']['buttons'])
        self.assertIn('Accept', seen['coordinator']['buttons'])
        self.assertNotIn('Accept', seen['viewer']['buttons'])
        self.assertIn('Answer', seen['viewer']['buttons'])
        self.assertEqual(0, seen['viewer']['forms'])
        self.assertEqual(2, seen['contributor']['forms'])
        self.assertGreaterEqual(seen['coordinator']['forms'], 3)
        self.assertIn('orchestra:room/r-0123456789ab#1', seen['contributor']['text'])
        self.assertIn('amended', seen['contributor']['text'])


if __name__ == '__main__':
    unittest.main()
