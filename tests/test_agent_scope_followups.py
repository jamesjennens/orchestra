"""Credential regressions through a real disposable loopback service."""
import copy
import contextlib
import io
import json
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import http_auth
import http_service
import test_http_agents
from test_http_agents import AgentHarness


class AgentScopeFollowupTests(AgentHarness):
    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.owner_id = self.create_account(self.admin, 'alex', 'alex-password-1')
        self.owner = self.login('alex', 'alex-password-1')[0]
        self.project = self.create_project(self.admin, 'Alpha')
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s' %
                         (self.project, self.owner_id), {'role': 'contributor'}, token=self.admin).status)

    def agent(self, scopes=None, name='Kestrel'):
        return self.agent_secret(self.owner, name=name, projects=[self.project],
                                 scopes=scopes or ['read', 'tasks'])

    def renew(self, aid, scopes=None, token=None, key=None, label='display only'):
        body = {'label': label}
        if scopes is not None:
            body['scopes'] = scopes
        return self.request('POST', '/v1/agents/%s/credentials' % aid, body,
                            token=token or self.owner, key=key)

    def legacy(self, aid):
        with self.store.lock:
            self.service.state['agents'][aid].pop('scopes')
            self.service.state['agents'][aid].pop('scopes_source')
            self.store.save()

    def test_nonowner_cannot_confirm_equal_narrower_or_wider_inferred_lists(self):
        aid, _, _ = self.agent()
        self.legacy(aid)
        before = copy.deepcopy(self.service.state['credentials'])
        for scopes in (['read', 'tasks'], ['read'], ['read', 'tasks', 'reviews']):
            with self.subTest(scopes=scopes):
                reply = self.renew(aid, scopes, token=self.admin)
                self.assertEqual(403, reply.status, reply.data)
                self.assertIn('Only this agent', reply.data['error']['message'])
                self.assertNotIn('credential', reply.data)
                self.assertNotIn('scopes', self.service.state['agents'][aid])
                self.assertEqual(before, self.service.state['credentials'])
        self.assertEqual(201, self.renew(aid, ['read']).status)

    def test_nonowner_cannot_choose_unknown_scopes(self):
        aid, _, made = self.agent()
        self.service.state['credentials'][made['credential']['id']]['revoked'] = True
        self.legacy(aid)
        plain = self.renew(aid, token=self.admin)
        self.assertEqual(409, plain.status, plain.data)
        self.assertIn('Ask that account', plain.data['error']['message'])
        self.assertEqual(403, self.renew(aid, ['read'], token=self.admin).status)
        self.assertNotIn('scopes', self.service.state['agents'][aid])
        self.assertEqual(201, self.renew(aid, ['read']).status)

    def test_newest_dead_credential_never_supplies_scopes(self):
        aid, _, made = self.agent(['read'])
        newest = self.renew(aid, ['read', 'tasks', 'reviews']).data['credential']
        with self.store.lock:
            # A positive epoch and no raw-lifetime fallback isolate absolute expiry.
            for cid in (made['credential']['id'], newest['id']):
                credential = self.service.state['credentials'][cid]
                credential['expires_at'] = time.time() - 10
                credential.pop('issued_raw', None)
            self.store.save()
        self.legacy(aid)
        seen = self.request('GET', '/v1/agents/%s' % aid, token=self.owner).data
        self.assertEqual((None, 'unknown'), (seen['scopes'], seen['scopes_source']))
        self.assertEqual([], seen['scopes_differ'])
        before = set(self.service.state['credentials'])
        self.assertEqual(409, self.renew(aid).status)
        self.assertEqual(before, set(self.service.state['credentials']))

    def test_stored_scopes_win_when_only_newest_dead_credential_is_wide(self):
        aid, _, made = self.agent(['read'])
        newest = self.renew(aid, ['read', 'tasks', 'reviews']).data['credential']
        with self.store.lock:
            self.service.state['agents'][aid]['scopes'] = ['read']
            for cid in (made['credential']['id'], newest['id']):
                self.service.state['credentials'][cid]['revoked'] = True
            self.service.state['credentials'][made['credential']['id']]['issued_raw'] = 1
            self.service.state['credentials'][newest['id']]['issued_raw'] = 2
            self.store.save()
        seen = self.request('GET', '/v1/agents/%s' % aid, token=self.owner).data
        self.assertEqual((['read'], 'set'), (seen['scopes'], seen['scopes_source']))
        renewed = self.renew(aid)
        self.assertEqual(201, renewed.status, renewed.data)
        self.assertEqual(['read'], renewed.data['credential']['scopes'])
        self.assertEqual(403, self.request('POST', '/v1/projects/%s/tasks' % self.project,
                                          {'title': 'must be denied'},
                                          token=renewed.data['credential']['secret']).status)

    def test_stored_inferred_source_does_not_become_confirmed_on_read(self):
        aid, _, _ = self.agent(['read'])
        self.service.state['agents'][aid]['scopes_source'] = 'inferred'
        self.store.save()
        seen = self.request('GET', '/v1/agents/%s' % aid, token=self.owner).data
        self.assertEqual((['read'], 'inferred'), (seen['scopes'], seen['scopes_source']))
        self.assertEqual(403, self.renew(aid, ['read'], token=self.admin).status)
        self.assertEqual('inferred', self.service.state['agents'][aid]['scopes_source'])

    def test_label_bounds_strip_and_omit_audit_payload(self):
        aid, _, _ = self.agent(['read'])
        for label in ('x' * 65, 'line\nbreak', 'bad\x00label'):
            with self.subTest(label=repr(label)):
                self.assertEqual(422, self.renew(aid, label=label).status)
        reply = self.renew(aid, label='  display text  ')
        self.assertEqual(201, reply.status, reply.data)
        self.assertEqual('display text', reply.data['credential']['label'])
        event = self.service.state['audit'][-1]
        self.assertNotIn('display text', json.dumps(event))
        self.assertNotIn('label', event)

    def test_refused_audit_stays_pending_when_save_is_busy(self):
        aid, _, _ = self.agent(['read'])
        before = copy.deepcopy(self.service.state['credentials'])
        write = self.store._write
        def busy():
            if self.service.state['audit'][-1]['action'] == 'agents.credentials.issue':
                raise TimeoutError('synthetic busy refusal save')
            return write()
        with mock.patch.object(self.store, '_write', side_effect=busy):
            reply = self.renew(aid, ['tasks'], token=self.admin)
        self.assertEqual(403, reply.status, reply.data)
        self.assertEqual(before, self.service.state['credentials'])
        self.assertIsNotNone(self.store.unsaved_since)
        self.assertEqual('denied', self.service.state['audit'][-1]['outcome'])
        self.store.save()
        self.assertIsNone(self.store.unsaved_since)
        disk = json.loads(self.store.path.read_text(encoding='utf-8'))
        self.assertEqual(reply.data['request_id'], disk['audit'][-1]['request_id'])

    def test_failed_issue_preserves_prior_pending_state_and_unrelated_audit(self):
        aid, _, _ = self.agent(['read'])
        before = copy.deepcopy(self.service.state['credentials'])
        with mock.patch.object(self.store, '_write', side_effect=TimeoutError('synthetic busy state')):
            reply = self.renew(aid, ['read', 'tasks'], key='failed-issue')
        self.assertEqual(503, reply.status, reply.data)
        self.assertIsNotNone(self.store.unsaved_since)
        self.assertEqual(before, self.service.state['credentials'])
        self.assertEqual(['read'], self.service.state['agents'][aid]['scopes'])
        self.assertFalse(any(event.get('scopes_after') == ['read', 'tasks']
                             for event in self.service.state['audit']))
        self.service.audit('unrelated-after', None, 'fixture', 'committed')
        self.store.save()
        self.assertEqual('unrelated-after', self.service.state['audit'][-1]['request_id'])

    def test_concurrent_audit_waits_for_failed_issue_and_survives(self):
        aid, _, _ = self.agent(['read'])
        write = self.store._write
        started, finished = threading.Event(), threading.Event()
        worker = []
        def other():
            started.set()
            self.service.audit('unrelated-concurrent', None, 'fixture', 'committed')
            finished.set()
        def fail():
            if self.service.state['audit'][-1].get('scopes_after') == ['read', 'tasks']:
                thread = threading.Thread(target=other)
                worker.append(thread)
                thread.start()
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.wait(.05))
                raise OSError('synthetic issue failure')
            return write()
        with mock.patch.object(self.store, '_write', side_effect=fail):
            reply = self.renew(aid, ['read', 'tasks'])
        self.assertEqual(500, reply.status, reply.data)
        self.assertEqual(1, len(worker))
        worker[0].join(timeout=3)
        self.assertTrue(finished.is_set())
        self.assertEqual('unrelated-concurrent', self.service.state['audit'][-1]['request_id'])
        self.assertEqual(['read'], self.service.state['agents'][aid]['scopes'])

    def test_full_audit_refusal_does_not_copy_or_compare_existing_entries(self):
        aid, _, _ = self.agent(['read'])
        counts = {'copy': 0, 'compare': 0}

        class AuditEntry(dict):
            def __deepcopy__(self, memo):
                counts['copy'] += 1
                return dict(self)

            def __eq__(self, other):
                counts['compare'] += 1
                return dict.__eq__(self, other)

        with self.store.lock:
            self.service.state['audit'] = [AuditEntry(action='fixture', outcome='committed',
                                                     request_id=str(i))
                                           for i in range(http_auth.AUDIT_LIMIT)]
            self.store.save()
        reply = self.renew(aid, ['tasks'], token=self.admin)
        self.assertEqual(403, reply.status, reply.data)
        self.assertEqual({'copy': 0, 'compare': 0}, counts)
        audit = self.service.state['audit']
        self.assertEqual(http_auth.AUDIT_LIMIT, len(audit))
        self.assertEqual('1', audit[0]['request_id'])
        self.assertEqual('denied', audit[-1]['outcome'])
        self.assertEqual(1, sum(event.get('request_id') == reply.data['request_id'] for event in audit))
        self.assertIsNone(self.store.unsaved_since)

    def test_receipt_is_committed_only_after_the_credential_is_on_disk(self):
        aid, _, _ = self.agent(['read'])
        commit = self.service.idempotency_commit
        seen = []

        def verify(digest, status, response, written_at=None):
            cid = response['credential']['id']
            disk = json.loads(self.store.path.read_text(encoding='utf-8'))
            seen.append(cid in disk['credentials'])
            return commit(digest, status, response, written_at=written_at)

        with mock.patch.object(self.service, 'idempotency_commit', side_effect=verify):
            reply = self.renew(aid, ['read', 'tasks'], key='durable-first')
        self.assertEqual(201, reply.status, reply.data)
        self.assertEqual([True], seen)
        replay = self.renew(aid, ['read', 'tasks'], key='durable-first')
        self.assertEqual(200, replay.status, replay.data)
        self.assertNotIn('secret', replay.data['credential'])

    def test_failed_receipt_keeps_saved_credential_and_reservation(self):
        aid, _, _ = self.agent(['read'])
        before = set(self.service.state['credentials'])
        with mock.patch.object(self.service, 'idempotency_commit', side_effect=OSError('synthetic receipt failure')):
            reply = self.renew(aid, ['read', 'tasks'], key='receipt-failed')
        self.assertEqual(500, reply.status, reply.data)
        added = set(self.service.state['credentials']) - before
        self.assertEqual(1, len(added))
        disk = json.loads(self.store.path.read_text(encoding='utf-8'))
        self.assertTrue(added <= set(disk['credentials']))
        self.assertEqual(409, self.renew(aid, ['read', 'tasks'], key='receipt-failed').status)
        self.assertEqual(before | added, set(self.service.state['credentials']))

    def test_subprocess_crash_before_state_or_receipt_never_replays_nonexistent_credential(self):
        for phase in ('state', 'receipt'):
            with self.subTest(phase=phase):
                aid, _, _ = self.agent(['read'], name='Crash ' + phase)
                before = set(self.service.state['credentials'])
                script = '''
import http.client, json, os, sys, threading
from http_auth import Store, Service
from http_service import create_server, InProcessBackend
store = Store(sys.argv[1]); service = Service(store)
count = len(store.state['credentials'])
write = store._write
def crash_write():
    if len(store.state['credentials']) > count: os._exit(23)
    return write()
if sys.argv[2] == 'state': store._write = crash_write
else: service.idempotency_commit = lambda *args, **kw: os._exit(23)
server = create_server(service, InProcessBackend(service), host='127.0.0.1', port=0)
threading.Thread(target=server.serve_forever, daemon=True).start()
client = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=15)
client.request('POST', '/v1/agents/' + sys.argv[3] + '/credentials',
               json.dumps({'label': 'display only', 'scopes': ['read', 'tasks']}),
               {'Authorization': 'Bearer ' + sys.argv[4], 'Content-Type': 'application/json',
                'Idempotency-Key': 'crash-' + sys.argv[2]})
client.getresponse()
'''
                done = subprocess.run([sys.executable, '-X', 'utf8', '-c', script,
                                       str(self.store.path), phase, aid, self.owner],
                                      cwd=test_http_agents.ROOT, capture_output=True, text=True, timeout=25)
                self.assertEqual(23, done.returncode, done.stderr)
                self._stop_server()
                self.store = http_auth.Store(self.store.path)
                self.service = http_auth.Service(self.store)
                self.backend = http_service.InProcessBackend(self.service)
                self.httpd = http_service.create_server(self.service, self.backend, host='127.0.0.1', port=0)
                self.port = self.httpd.server_address[1]
                self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
                self.thread.start()
                added = set(self.service.state['credentials']) - before
                self.assertEqual(0 if phase == 'state' else 1, len(added))
                retry = self.renew(aid, ['read', 'tasks'], key='crash-' + phase)
                self.assertEqual(409, retry.status, retry.data)
                self.assertIn('already in progress', retry.data['error']['message'])
                self.assertNotIn('credential', retry.data)
                self.assertEqual(before | added, set(self.service.state['credentials']))

    def test_node_absence_note_is_emitted_by_actual_unittest_runner(self):
        captured = io.StringIO()
        case = test_http_agents.AgentsPageTests('test_the_agents_page_under_node_against_the_real_service')
        with mock.patch('test_http_agents.shutil.which', return_value=None), contextlib.redirect_stderr(captured):
            result = unittest.TextTestRunner(stream=captured, verbosity=2).run(case)
        self.assertEqual((1, 1, 0, 0), (result.testsRun, len(result.skipped), len(result.errors), len(result.failures)))
        self.assertIn('NOTE: AgentsPageTests.test_the_agents_page_under_node', captured.getvalue())
        self.assertIn('was SKIPPED: node is not installed', captured.getvalue())

    def test_disable_routes_name_affected_agent_and_credential_ids(self):
        for operation in ('agent', 'update', 'account'):
            with self.subTest(operation=operation):
                aid, secret, made = self.agent(['read'], name='Disable ' + operation)
                cid = made['credential']['id']
                if operation == 'agent':
                    reply = self.request('POST', '/v1/agents/%s/disable' % aid, {}, token=self.owner)
                    action = 'agents.disable'
                elif operation == 'update':
                    reply = self.request('PATCH', '/v1/agents/%s' % aid, {'enabled': False}, token=self.owner)
                    action = 'agents.update'
                else:
                    reply = self.request('POST', '/v1/accounts/%s/disable' % self.owner_id, {}, token=self.admin)
                    action = 'accounts.disable'
                self.assertEqual(200, reply.status, reply.data)
                event = next(event for event in reversed(self.service.state['audit']) if event['action'] == action)
                self.assertIn(aid, event['agent_ids'])
                self.assertIn(cid, event['target_credential_ids'])
                self.assertEqual(401, self.request('GET', '/v1/agents/me', token=secret).status)
                self.assertNotIn(secret, json.dumps(event))

    def test_filtered_pages_are_bounded_owner_visible_and_query_bound(self):
        ids = []
        for i in range(5):
            aid, _, _ = self.agent(['read'], name='Page ' + str(i))
            ids.append(aid)
            if i != 0:
                self.legacy(aid)
        path = '/v1/agents?limit=2&unconfirmed=true'
        first = self.request('GET', path, token=self.admin)
        self.assertEqual(200, first.status, first.data)
        self.assertEqual(4, first.data['total'])
        self.assertEqual(2, len(first.data['items']))
        cursor = first.data['next_cursor']
        next_page = self.request('GET', path + '&cursor=' + cursor, token=self.admin)
        self.assertEqual(200, next_page.status, next_page.data)
        self.assertEqual(2, len(next_page.data['items']))
        self.assertIsNone(next_page.data['next_cursor'])
        self.assertEqual(set(ids[1:]), {item['id'] for item in first.data['items'] + next_page.data['items']})
        self.assertEqual(409, self.request('GET', path.replace('true', 'false') + '&cursor=' + cursor,
                                          token=self.admin).status)
        other_id = self.create_account(self.admin, 'blair', 'blair-password-1')
        other = self.login('blair', 'blair-password-1')[0]
        private = self.request('GET', path, token=other)
        self.assertEqual((0, []), (private.data['total'], private.data['items']))
        self.assertNotEqual(other_id, self.owner_id)

    def test_actual_page_requests_filtered_pages_and_previous_next(self):
        from test_http_web import run_node_module
        node = shutil.which('node')
        if not node:
            print('NOTE: agent pagination page SKIPPED: node is not installed.', file=sys.stderr)
            self.skipTest('node is not installed; agent pagination page not run')
        ids = []
        for i in range(5):
            aid, _, _ = self.agent(['read'], name='Rendered page ' + str(i))
            ids.append(aid)
            if i:
                self.legacy(aid)
        root = test_http_agents.ROOT
        done = run_node_module(self, node, 'await import(process.argv[1])',
                               (root / 'tests/web_agent_pages.mjs').as_uri(),
                               (root / 'tests/web_dom_shim.mjs').as_uri(),
                               (root / 'web/js/api.js').as_uri(), (root / 'web/js/views/agents.js').as_uri(),
                               'http://127.0.0.1:%d' % self.port, self.admin, self.owner_id)
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(2, len(seen['first']))
        self.assertEqual(2, len(seen['second']))
        self.assertFalse(set(seen['first']) & set(seen['second']))
        self.assertEqual(seen['first'], seen['back'])
        self.assertEqual(set(ids[1:]), set(seen['filteredFirst'] + seen['filteredSecond']))
        self.assertTrue(all('limit=2' in path for path in seen['reads']))
        self.assertTrue(any('unconfirmed=true' in path and 'cursor=' in path for path in seen['reads']))

    def test_failed_revocation_keeps_access_revoked_and_next_save_persists(self):
        aid, secret, made = self.agent(['read'])
        cid = made['credential']['id']
        write = self.store._write

        def fail():
            if self.service.state['credentials'][cid]['revoked']:
                raise OSError('synthetic failed revocation save')
            return write()

        with mock.patch.object(self.store, '_write', side_effect=fail):
            reply = self.request('POST', '/v1/agents/%s/credentials/%s/revoke' % (aid, cid),
                                 {}, token=self.owner, key='revoke-failed')
        self.assertEqual(500, reply.status, reply.data)
        self.assertTrue(self.service.state['credentials'][cid]['revoked'])
        self.assertIsNotNone(self.store.unsaved_since)
        self.assertEqual(401, self.request('GET', '/v1/agents/me', token=secret).status)
        self.store.save()
        disk = json.loads(self.store.path.read_text(encoding='utf-8'))
        self.assertTrue(disk['credentials'][cid]['revoked'])
        self.assertIsNone(self.store.unsaved_since)

    def test_issue_and_revoke_each_leave_one_private_metadata_event(self):
        aid, _, _ = self.agent(['read'])
        marker = len(self.service.state['audit'])
        reply = self.renew(aid, ['read', 'tasks'], label='PRIVATE LABEL')
        self.assertEqual(201, reply.status, reply.data)
        cid = reply.data['credential']['id']
        revoked = self.request('POST', '/v1/agents/%s/credentials/%s/revoke' % (aid, cid),
                               {}, token=self.owner)
        self.assertEqual(204, revoked.status, revoked.data)
        events = self.service.state['audit'][marker:]
        self.assertEqual(2, len(events), events)
        for event in events:
            self.assertTrue({'agent_id', 'target_credential_id', 'user_id', 'actor', 'request_id',
                             'time', 'credential_scopes', 'scopes_before', 'scopes_after',
                             'scopes_source_before', 'scopes_source_after'} <= set(event))
            self.assertEqual(aid, event['agent_id'])
            self.assertEqual(cid, event['target_credential_id'])
            self.assertEqual(self.owner_id, event['user_id'])
            self.assertTrue(event['actor'])
            self.assertTrue(event['request_id'])
            self.assertTrue(event['time'])
        self.assertEqual((['read'], ['read', 'tasks']),
                         (events[0]['scopes_before'], events[0]['scopes_after']))
        raw = json.dumps(events)
        for private in ['PRIVATE LABEL', reply.data['credential']['secret'], 'token_hash', 'working_directory']:
            self.assertNotIn(private, raw)
