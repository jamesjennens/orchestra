"""Project rooms slice 1 over HTTP: routes, authority, tombstones, export and audit (kittrial-5bb.385)."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rooms
import test_http_agents as agents
from test_http_agents import unique_dir


class RoomsHttpTests(agents.AgentHarness):
    def setUp(self):
        super().setUp()
        self.rooms_root = unique_dir('rooms-http-')
        self.addCleanup(__import__('shutil').rmtree, self.rooms_root, ignore_errors=True)
        self.backend.files_root = str(self.rooms_root)
        self.limit = dict(rooms.LIMIT_DEFAULTS)
        self.backend.rooms_limits = lambda **kw: dict(self.limit)
        self.admin = self.admin_token()
        self.pid = self.create_project(self.admin, 'Rooms')
        self.other_pid = self.create_project(self.admin, 'Elsewhere')
        self.tokens = {}
        for name, role, project in (('owner', 'owner', self.pid), ('coord', 'coordinator', self.pid),
                                    ('worker', 'contributor', self.pid), ('viewer', 'viewer', self.pid),
                                    ('outsider', 'coordinator', self.other_pid)):
            uid = self.create_account(self.admin, name, name + '-password-1')
            put = self.request('PUT', '/v1/projects/%s/members/%s' % (project, uid),
                               {'role': role}, token=self.admin)
            self.assertIn(put.status, (200, 201), put.data)
            self.tokens[name] = self.login(name, name + '-password-1')[0]
        self.room = self.create_room(self.tokens['coord'], 'Planning')

    def rooms_path(self, rid=None, suffix=''):
        path = '/v1/projects/%s/rooms' % self.pid
        if rid:
            path += '/' + rid
        return path + suffix

    def create_room(self, token, name):
        made = self.request('POST', self.rooms_path(), {'name': name}, token=token)
        self.assertEqual(201, made.status, made.data)
        return made.data

    def post(self, token, text, key='msg-key-1', rid=None):
        return self.request('POST', self.rooms_path(rid or self.room['id'], '/messages'),
                            {'text': text}, token=token, key=key)

    def audit(self, action):
        return [event for event in self.service.state['audit'] if event.get('action') == action]

    def store_bytes(self):
        """Every byte the service's durable store holds (state JSON and its SQLite sidecar)."""
        blob = b''
        for path in sorted(self.tmp_path.glob(self.store.path.name + '*')):
            if path.is_file():
                blob += path.read_bytes()
        return blob

    def test_roles_create_post_read_and_refuse(self):
        listed = self.request('GET', self.rooms_path(), token=self.tokens['viewer'])
        self.assertEqual(200, listed.status, listed.data)
        self.assertEqual([self.room['id']], [item['id'] for item in listed.data['items']])
        self.assertFalse(listed.data['can_post'])
        posted = self.post(self.tokens['worker'], 'hello from a contributor')
        self.assertEqual(201, posted.status, posted.data)
        self.assertEqual(1, posted.data['sequence'])
        viewer_post = self.post(self.tokens['viewer'], 'viewer text')
        self.assertEqual(403, viewer_post.status, viewer_post.data)
        outsider = self.post(self.tokens['outsider'], 'not a member')
        # A non-member is refused without confirming the room exists (403 or the same 404).
        self.assertIn(outsider.status, (403, 404), outsider.data)
        contributor_create = self.request('POST', self.rooms_path(), {'name': 'Nope'},
                                          token=self.tokens['worker'])
        self.assertEqual(403, contributor_create.status, contributor_create.data)
        read = self.request('GET', self.rooms_path(self.room['id']), token=self.tokens['viewer'])
        self.assertEqual(200, read.status, read.data)
        self.assertEqual(['hello from a contributor'], [item['text'] for item in read.data['messages']])
        self.assertEqual('committed', self.audit('rooms.post')[-1]['outcome'])

    def test_markup_and_javascript_links_are_text_not_elements(self):
        text = '<img src=x onerror=alert(1)> and [x](javascript:alert(1))'
        sent = self.post(self.tokens['worker'], text)
        self.assertEqual(201, sent.status, sent.data)
        read = self.request('GET', self.rooms_path(self.room['id']), token=self.tokens['viewer'])
        self.assertEqual(text, read.data['messages'][0]['text'])
        exported = self.request('GET', self.rooms_path(self.room['id'], '/export?format=markdown'),
                                token=self.tokens['viewer'])
        self.assertEqual(200, exported.status, exported.data)
        self.assertIn(text, exported.data['text'])

    def test_oversize_and_reserved_prefix_are_refused(self):
        huge = self.post(self.tokens['worker'], 'x' * 4001, key='huge')
        self.assertEqual(422, huge.status, huge.data)
        reserved = self.post(self.tokens['worker'], 'Kind: task-checkpoint-v1\n{}', key='reserved')
        self.assertEqual(422, reserved.status, reserved.data)
        self.assertEqual('reserved_text', reserved.data['error']['code'])

    def test_archive_and_moderation_authority_and_effects(self):
        sent = self.post(self.tokens['worker'], 'remove this original')
        self.assertEqual(201, sent.status, sent.data)
        worker_moderate = self.request('POST', self.rooms_path(self.room['id'], '/messages/1/moderate'),
                                       {'reason': 'off topic'}, token=self.tokens['worker'], key='moderate-key-1')
        self.assertEqual(403, worker_moderate.status, worker_moderate.data)
        moderated = self.request('POST', self.rooms_path(self.room['id'], '/messages/1/moderate'),
                                 {'reason': 'off topic'}, token=self.tokens['coord'], key='moderate-key-2')
        self.assertEqual(201, moderated.status, moderated.data)
        read = self.request('GET', self.rooms_path(self.room['id']), token=self.tokens['worker'])
        self.assertEqual('tombstone', read.data['messages'][0]['kind'])
        self.assertNotIn('remove this original', json.dumps(read.data))
        self.assertIn('removed by coord', read.data['messages'][0]['text'])
        raw = self.request('GET', self.rooms_path(self.room['id'], '/export?format=jsonl'),
                           token=self.tokens['worker'])
        self.assertNotIn('remove this original', raw.data['text'])
        self.assertIn('removed by coord', raw.data['text'])
        self.assertEqual('committed', self.audit('rooms.moderate')[-1]['outcome'])
        worker_archive = self.request('POST', self.rooms_path(self.room['id'], '/archive'), {},
                                      token=self.tokens['worker'], key='archive-key-1')
        self.assertEqual(403, worker_archive.status, worker_archive.data)
        archived = self.request('POST', self.rooms_path(self.room['id'], '/archive'), {},
                                token=self.tokens['owner'], key='archive-key-2')
        self.assertEqual(200, archived.status, archived.data)
        self.assertEqual('archived', archived.data['state'])
        refused = self.post(self.tokens['worker'], 'too late', key='late-key-1')
        self.assertEqual(409, refused.status, refused.data)
        self.assertEqual('archived', refused.data['error']['code'])
        listed = self.request('GET', self.rooms_path(), token=self.tokens['viewer'])
        self.assertEqual([], listed.data['items'])
        all_rooms = self.request('GET', self.rooms_path() + '?archived=1', token=self.tokens['viewer'])
        self.assertEqual([self.room['id']], [item['id'] for item in all_rooms.data['items']])

    def test_retried_post_with_the_same_key_is_one_line(self):
        first = self.request('POST', self.rooms_path(self.room['id'], '/messages'),
                             {'text': 'only once'}, token=self.tokens['worker'], key='same-key')
        second = self.request('POST', self.rooms_path(self.room['id'], '/messages'),
                              {'text': 'only once'}, token=self.tokens['worker'], key='same-key')
        self.assertEqual(201, first.status, first.data)
        self.assertEqual(201, second.status, second.data)
        self.assertEqual(first.data['sequence'], second.data['sequence'])
        read = self.request('GET', self.rooms_path(self.room['id']), token=self.tokens['viewer'])
        self.assertEqual(1, len(read.data['messages']))
        changed = self.request('POST', self.rooms_path(self.room['id'], '/messages'),
                               {'text': 'different'}, token=self.tokens['worker'], key='same-key')
        self.assertEqual(409, changed.status, changed.data)

    def test_a_moderated_posts_text_is_not_kept_for_a_retry(self):
        # Review b1: the stored answer of a post must not keep its text for 24 h, or a
        # replay after moderation answers 201 with the original.
        secret = 'b1 secret original text'
        first = self.post(self.tokens['worker'], secret, key='b1-key-0001')
        self.assertEqual(201, first.status, first.data)
        moderated = self.request('POST', self.rooms_path(self.room['id'], '/messages/1/moderate'),
                                 {'reason': 'off topic'}, token=self.tokens['coord'], key='b1-mod-0001')
        self.assertEqual(201, moderated.status, moderated.data)
        replay = self.post(self.tokens['worker'], secret, key='b1-key-0001')
        self.assertEqual(201, replay.status, replay.data)
        self.assertEqual(first.data['sequence'], replay.data['sequence'])
        self.assertNotIn(secret, json.dumps(replay.data))
        self.assertNotIn(secret.encode('utf-8'), self.store_bytes())
        read = self.request('GET', self.rooms_path(self.room['id']), token=self.tokens['viewer'])
        self.assertNotIn(secret, json.dumps(read.data))

    def test_a_non_member_cannot_list_read_or_export_rooms(self):
        # Review s7: the project-membership check on all three read routes.
        for path in (self.rooms_path(),
                     self.rooms_path(self.room['id']),
                     self.rooms_path(self.room['id'], '/export?format=markdown')):
            refused = self.request('GET', path, token=self.tokens['outsider'])
            self.assertIn(refused.status, (403, 404), (path, refused.data))

    def test_an_agent_credential_cannot_read_rooms(self):
        # Review s3: CAP_READ alone must not let an agent credential read room text (R1).
        _agent_id, secret, _data = self.agent_secret(self.tokens['coord'], projects=[self.pid])
        for path in (self.rooms_path(),
                     self.rooms_path(self.room['id']),
                     self.rooms_path(self.room['id'], '/export?format=markdown')):
            refused = self.request('GET', path, token=secret)
            self.assertEqual(403, refused.status, (path, refused.data))
        self.assertEqual(403, self.post(secret, 'credential post', key='cred-post').status)

    def test_an_archived_project_refuses_room_writes_but_still_reads(self):
        # Review s7: the archived-project refusal in _rooms_write.
        self.service.state['projects'][self.pid]['archived'] = True
        self.store.save()
        made = self.request('POST', self.rooms_path(), {'name': 'No'}, token=self.tokens['coord'])
        self.assertEqual(409, made.status, made.data)
        sent = self.post(self.tokens['worker'], 'no', key='archived-project')
        self.assertEqual(409, sent.status, sent.data)
        read = self.request('GET', self.rooms_path(self.room['id']), token=self.tokens['viewer'])
        self.assertEqual(200, read.status, read.data)

    def test_open_room_bound_is_a_setting(self):
        self.limit = dict(self.limit, max_open_rooms=2)
        self.create_room(self.tokens['coord'], 'Second')
        third = self.request('POST', self.rooms_path(), {'name': 'Third'}, token=self.tokens['coord'])
        self.assertEqual(409, third.status, third.data)
        self.assertEqual('rooms_full', third.data['error']['code'])

    def test_another_projects_room_is_not_found(self):
        made = self.request('POST', '/v1/projects/%s/rooms' % self.other_pid, {'name': 'Elsewhere'},
                            token=self.tokens['outsider'])
        self.assertEqual(201, made.status, made.data)
        read = self.request('GET', self.rooms_path(made.data['id']), token=self.tokens['outsider'])
        self.assertEqual(404, read.status, read.data)


class RoomsPageTests(unittest.TestCase):
    def test_room_page_renders_text_only_and_shows_controls_by_role(self):
        import shutil
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        kit = Path(__file__).resolve().parents[1]
        done = test_http_web.run_node_module(
            self, node, 'await import(process.argv[1])',
            (kit / 'tests' / 'web_rooms_page.mjs').as_uri(),
            (kit / 'tests' / 'web_dom_shim.mjs').as_uri(),
            (kit / 'web' / 'js' / 'views' / 'rooms.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        # Markup and a javascript: link stay literal text and make no element.
        self.assertIn('<img src=x onerror=alert(1)>', seen['contributor']['text'])
        self.assertIn('javascript:alert(1)', seen['contributor']['text'])
        # Review s5: each message shows its sequence, and the page opens at the newest
        # 100-message window (last_sequence 150 -> start at 50), not at message 1.
        self.assertIn('#150', seen['contributor']['text'])
        self.assertEqual(1, len(seen['contributor']['roomCalls']))
        self.assertEqual(50, seen['contributor']['roomCalls'][0]['since'])
        self.assertEqual(0, seen['contributor']['images'])
        self.assertEqual([], seen['contributor']['js_links'])
        # A viewer sees no write form; a contributor sees the post and item forms; a
        # coordinator sees the moderation forms too.
        self.assertEqual(0, seen['viewer']['forms'])
        self.assertEqual(2, seen['contributor']['forms'])
        self.assertGreaterEqual(seen['coordinator']['forms'], 3)
        self.assertIn('Planning', seen['list']['text'])
        self.assertEqual(1, seen['list']['forms'])


if __name__ == '__main__':
    unittest.main()
