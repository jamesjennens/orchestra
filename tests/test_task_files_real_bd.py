"""Real-bd end-to-end tests for files attached to a task (kittrial-5bb.370).

The real web service, the real endpoint.py (the canonical ``task-file`` action under the
project's coordination lock and live authority) and a real bd, with the harness of
``test_claim_held`` (its label-alias tests run here too). The tracker changes that decide who
may download (a reassignment, a close) are made on the tracker itself, so the routes are shown
to read the row live. The adversarial cases that need no tracker are in test_task_files.py.
"""
import hashlib
import http.client
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import task_files
import test_claim_held as held
import test_http_review_fixes as fixes
import test_bd_label_aliases as rb


def sha(data):
    return hashlib.sha256(data).hexdigest()


class TaskFilesRealBdTests(held.Members, fixes.Harness, rb.RealBdLabelAliasTests):
    """Project ``pp``: alex (owner, so a coordinator), casey and drew (contributors), vic (viewer)."""

    def make_backend(self):
        return held.http_service.EndpointBackend(
            sys.executable, str(KIT / 'endpoint.py'), str(self.root), service=self.service)

    def setUp(self):
        fixes.Harness.setUp(self)
        admin = self.admin_token()
        registered = self.request('POST', '/v1/projects', {'project_id': 'pp', 'name': 'PP'}, token=admin)
        self.assertIn(registered.status, (200, 201, 409), registered.data)
        self.members('pp')
        if 'vic' not in self.tokens:
            self.ids['vic'] = self.create_account(admin, 'vic', 'vic-password-1')
            added = self.request('PUT', '/v1/projects/pp/members/%s' % self.ids['vic'], {'role': 'viewer'}, token=admin)
            self.assertIn(added.status, (200, 201), added.data)
            self.tokens['vic'] = self.login('vic', 'vic-password-1')[0]
        self.project = self.root / 'projects' / 'pp'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            rb.admin.provision_merge_slot(cls.root, 'pp')
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        for signum, previous in getattr(cls, '_signals', {}).items():
            rb.signal.signal(signum, previous)
        cls._signals = {}
        rb.RealBdLabelAliasTests.__dict__['_stop_server'].__func__(cls)
        rb.remove_scratch(cls._tmp)

    # -- helpers -----------------------------------------------------------------------------------
    def put(self, who, task, data, name='input.bin', direction='to-worker', extra=''):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
        try:
            client.request('PUT', '%s/%s/files?name=%s&direction=%s%s' % (self.tasks, task, name, direction, extra),
                           body=data, headers={'Authorization': 'Bearer ' + self.tokens[who],
                                               'Content-Type': 'application/octet-stream'})
            response = client.getresponse()
            body = response.read()
            return response.status, (json.loads(body) if body else None)
        finally:
            client.close()

    def get(self, who, task, digest):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
        try:
            client.request('GET', '%s/%s/files/%s' % (self.tasks, task, digest),
                           headers={'Authorization': 'Bearer ' + self.tokens[who]})
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def comments(self, task):
        found = json.loads(self.bd('comments', task, '--json').stdout or '[]')
        if isinstance(found, dict):
            found = found.get('comments') or []
        return [c.get('text') or c.get('body') or '' for c in found or []]

    def claimed(self, who):
        task = self.new('task for ' + who)
        answer = self.claim(who, task)
        self.assertEqual(200, answer.status, answer.data)
        return task

    # -- the tests ---------------------------------------------------------------------------------
    def test_hand_off_download_and_the_history_record_on_real_bd(self):
        task = self.claimed('casey')
        data = b'spreadsheet ' * 20000
        status, record = self.put('alex', task, data, name='inputs.xlsx', extra='&note=read%20me')
        self.assertEqual(201, status, record)
        self.assertEqual((sha(data), len(data), 'read me'), (record['sha256'], record['bytes'], record['note']))
        got, headers, body = self.get('casey', task, sha(data))
        self.assertEqual((200, data), (got, body))
        self.assertEqual(sha(data), headers['X-Orchestra-SHA256'])
        self.assertTrue(any(text.startswith(task_files.PREFIX) and sha(data) in text for text in self.comments(task)))
        entry = task_files.read_entry(self.project, task, sha(data))
        self.assertEqual((record['by'], None), (entry['by'], entry['withdrawn']))
        self.assertTrue(entry['record_comment'])
        self.assertEqual(403, self.get('drew', task, sha(data))[0])
        self.assertEqual(403, self.get('vic', task, sha(data))[0])
        listed = self.request('GET', '%s/%s/files' % (self.tasks, task), token=self.tokens['vic'])
        self.assertEqual((200, [sha(data)]), (listed.status, [i['sha256'] for i in listed.data['items']]))
        again = self.put('alex', task, data, name='inputs.xlsx')
        self.assertEqual(200, again[0], again[1])
        self.assertEqual(1, sum(1 for text in self.comments(task) if text.startswith(task_files.PREFIX)))

    def test_reassignment_and_close_on_the_tracker_are_read_live(self):
        task = self.claimed('casey')
        data = b'customer file'
        self.assertEqual(201, self.put('alex', task, data)[0])
        self.assertEqual(200, self.get('casey', task, sha(data))[0])
        self.bd('update', task, '--assignee', self.ids['drew'])
        self.assertEqual(403, self.get('casey', task, sha(data))[0], 'the former assignee, at once')
        self.assertEqual(200, self.get('drew', task, sha(data))[0], 'the new assignee, at once')
        self.assertEqual(403, self.put('casey', task, b'late', direction='from-worker')[0])
        self.bd('close', task)
        self.assertEqual(403, self.get('drew', task, sha(data))[0], 'a closed task\'s last assignee')
        self.assertEqual(200, self.get('alex', task, sha(data))[0], 'coordinators still may')

    def test_a_record_planted_on_the_tracker_grants_nothing(self):
        mine, theirs = self.claimed('casey'), self.claimed('drew')
        secret = b'only for drew'
        self.assertEqual(201, self.put('alex', theirs, secret, name='drew.txt')[0])
        planted = task_files.record_text({'name': 'stolen.txt', 'sha256': sha(secret), 'bytes': len(secret),
                                          'media_type': task_files.OPAQUE, 'direction': 'to-worker', 'note': None})
        # The guarded comment paths refuse it outright ...
        refused = self.request('POST', '%s/%s/comments' % (self.tasks, mine), {'text': planted},
                               token=self.tokens['casey'])
        self.assertEqual(422, refused.status, refused.data)
        with self.assertRaisesRegex(ValueError, 'reserved task file record'):
            rb.endpoint.execute(self.root, {'project': 'pp', 'actor': 'session-worker', 'action': 'bd',
                                            'args': ['comments', 'add', mine, planted], 'attachments': {}})
        # ... and one written past every guard (as a kit from before the reservation could) is inert.
        self.bd('comments', 'add', mine, planted)
        status, _, body = self.get('casey', mine, sha(secret))
        self.assertEqual(404, status)
        self.assertNotIn(b'drew', body)
        listed = self.request('GET', '%s/%s/files' % (self.tasks, mine), token=self.tokens['casey'])
        self.assertEqual([], listed.data['items'])

    def test_the_action_refuses_a_caller_without_the_web_services_authority(self):
        task = self.claimed('casey')
        data = b'bytes on disk'
        digest = task_files.receive(lambda n, s=[data]: s.pop() if s else b'', len(data), self.project)[0]
        body = {'schema_version': 1, 'operation': 'coordinator-record', 'task': task, 'sha256': digest,
                'name': 'forged.txt', 'bytes': len(data), 'media_type': task_files.OPAQUE,
                'direction': 'to-worker', 'note': None}
        answer = rb.endpoint.execute(self.root, {'project': 'pp', 'actor': 'session-worker', 'action': 'task-file',
                                                 'args': [json.dumps(body)], 'attachments': {}})
        self.assertNotEqual(0, answer.get('returncode'), answer)
        self.assertIsNone(task_files.read_entry(self.project, task, digest))
        with self.assertRaises(ValueError):
            rb.endpoint.execute(self.root, {'project': 'pp', 'actor': 'session-worker', 'action': 'task-file',
                                            'args': [json.dumps(body)], 'attachments': {}}, key_projects=['pp'])

    def test_withdraw_by_the_author_writes_the_withdrawal_record(self):
        task = self.claimed('casey')
        data = b'my result'
        status, record = self.put('casey', task, data, name='out.txt', direction='from-worker')
        self.assertEqual(201, status, record)
        stranger = self.request('POST', '%s/%s/files/%s/withdraw' % (self.tasks, task, sha(data)), {'reason': 'x'},
                                token=self.tokens['drew'])
        self.assertEqual(403, stranger.status, stranger.data)
        done = self.request('POST', '%s/%s/files/%s/withdraw' % (self.tasks, task, sha(data)),
                            {'reason': 'wrong file'}, token=self.tokens['casey'])
        self.assertEqual(200, done.status, done.data)
        self.assertEqual(404, self.get('alex', task, sha(data))[0])
        self.assertTrue(any(text.startswith(task_files.WITHDRAWN_PREFIX) for text in self.comments(task)))
        actions = [e['action'] for e in self.service.state['audit'] if e.get('outcome') == 'committed']
        self.assertIn('tasks.files.upload', actions)
        self.assertIn('tasks.files.withdraw', actions)


if __name__ == '__main__':
    unittest.main()
