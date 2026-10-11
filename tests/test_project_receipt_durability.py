"""Own crash probes: real loopback HTTP and SQLite, synthetic endpoint stub.

The child exits at an actual JSON/receipt boundary. Its state and receipt are
then read by a fresh service; no private reviewer probe is used.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import http_auth
import http_service
from test_http_project_register import RegisterHarness
from test_http_review_fixes import STUB

CRASH = '''
import http.client,json,os,sys,threading
from http_auth import Store,Service
from http_service import EndpointBackend,create_server
path,root,stub,token,operation,phase,key = sys.argv[1:]
store=Store(path); service=Service(store)
backend=EndpointBackend(sys.executable,stub,root,service=service)
write=store._write; commit=service.idempotency_commit
field='confirmed_by' if operation=='confirm' else 'registered_by'
def crash_write():
    if phase=='state' and field in store.state['projects'].get('alpha',{}): os._exit(23)
    return write()
def crash_commit(*args,**kwargs):
    if phase=='receipt': os._exit(23)
    result=commit(*args,**kwargs)
    if phase=='final': os._exit(23)
    return result
store._write=crash_write; service.idempotency_commit=crash_commit
server=create_server(service,backend,host='127.0.0.1',port=0)
threading.Thread(target=server.serve_forever,daemon=True).start()
client=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=15)
route='/v1/projects/alpha/confirm' if operation=='confirm' else '/v1/projects'
body={} if operation=='confirm' else {'project_id':'alpha','name':'Alpha'}
client.request('POST',route,json.dumps(body),{'Authorization':'Bearer '+token,
    'Content-Type':'application/json','Idempotency-Key':key})
response=client.getresponse(); print(response.status,response.read().decode(),flush=True)
server.shutdown(); server.server_close()
'''


class ProjectReceiptDurabilityTests(RegisterHarness):
    def fixture(self, operation):
        admin, alex = self.alex()
        self.initialize_canonical('alpha')
        if operation == 'confirm':
            creator = self.request('GET', '/v1/sessions/current', token=alex).data['user']['id']
            self.service.state['projects']['alpha'] = dict(id='alpha', name='Alpha',
                created_by=creator, created_at='2026-01-01T00:00:00Z', archived=False)
            self.service.state['memberships']['alpha'] = {creator: 'owner'}
            self.store.save()
        return admin

    def invoke(self, operation, admin, key='crash-key', body=None):
        return self.request('POST', '/v1/projects/alpha/confirm' if operation == 'confirm' else '/v1/projects',
            ({} if operation == 'confirm' else {'project_id': 'alpha', 'name': 'Alpha'}) if body is None else body,
            token=admin, key=key)

    def reload(self):
        service = http_auth.Service(http_auth.Store(self.store.path))
        backend = http_service.EndpointBackend(sys.executable, str(STUB), str(self.canonical_root), service=service)
        self.restart(service, backend)

    def crash(self, operation, phase):
        admin = self.fixture(operation)
        self._stop_server()
        done = subprocess.run([sys.executable, '-X', 'utf8', '-c', CRASH, str(self.store.path),
            str(self.canonical_root), str(STUB), admin, operation, phase, 'crash-key'],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(23, done.returncode, done.stdout + done.stderr)
        self.reload()
        return admin

    def check_mark(self, operation):
        record = self.service.state['projects']['alpha']
        self.assertEqual(self.admin_user['id'], record.get('registered_by'))
        if operation == 'confirm':
            self.assertEqual(self.admin_user['id'], record.get('confirmed_by'))
            self.assertTrue(record['confirmed_at'])
        # Registration/confirmation remains usable after the registrant's demotion.
        self.service.state['users'][self.admin_user['id']]['superuser'] = False
        self.store.save()
        self.assertIsNone(http_service.project_unusable(self.service, 'alpha'))

    def check_crash(self, operation, phase):
        admin = self.crash(operation, phase)
        reply = self.invoke(operation, admin)
        if phase == 'final':
            self.assertEqual(200 if operation == 'confirm' else 201, reply.status, reply.data)
            if operation == 'confirm':
                self.assertEqual(reply.data['confirmed_at'], self.service.state['projects']['alpha'].get('confirmed_at'))
            self.check_mark(operation)
        elif phase == 'receipt':
            self.assertEqual(409, reply.status, reply.data)
            self.assertIn('already in progress', reply.data['error']['message'])
            self.check_mark(operation)
        else:
            self.assertEqual(409, reply.status, reply.data)
            field = 'confirmed_by' if operation == 'confirm' else 'registered_by'
            self.assertNotIn(field, self.service.state['projects'].get('alpha', {}))

    def test_confirmation_killed_before_state(self): self.check_crash('confirm', 'state')
    def test_confirmation_killed_before_receipt(self): self.check_crash('confirm', 'receipt')
    def test_confirmation_killed_after_receipt(self): self.check_crash('confirm', 'final')
    def test_registration_killed_before_mark_save(self): self.check_crash('register', 'state')
    def test_registration_killed_before_receipt(self): self.check_crash('register', 'receipt')
    def test_registration_killed_after_receipt(self): self.check_crash('register', 'final')

    def test_the_registration_mark_is_written_in_the_same_save_as_the_project(self):
        """kittrial-5bb.255 U1: no save writes the project record without its mark, so
        no kill or failed save can leave a project on disk without registered_by."""
        admin = self.fixture('register')
        write = self.store._write
        marked = []

        def watch():
            record = self.service.state['projects'].get('alpha')
            if record is not None:
                marked.append(record.get('registered_by'))
            return write()

        with mock.patch.object(self.store, '_write', side_effect=watch):
            reply = self.invoke('register', admin)
        self.assertEqual(201, reply.status, reply.data)
        self.assertTrue(marked)
        self.assertEqual([self.admin_user['id']] * len(marked), marked)
        self.assertEqual(self.admin_user['id'], self.service.state['projects']['alpha']['registered_by'])

    def test_confirmation_failed_effect_save_releases_key_and_restart_can_retry(self):
        admin = self.fixture('confirm')
        write = self.store._write
        def fail():
            if 'confirmed_by' in self.service.state['projects']['alpha']:
                raise OSError('synthetic confirmation save failure')
            return write()
        with mock.patch.object(self.store, '_write', side_effect=fail):
            reply = self.invoke('confirm', admin)
        self.assertEqual(500, reply.status, reply.data)
        disk = json.loads(self.store.path.read_text(encoding='utf-8'))
        self.assertNotIn('confirmed_by', disk['projects']['alpha'])
        self.reload()
        self.assertEqual(200, self.invoke('confirm', admin).status)
        self.assertEqual(200, self.invoke('confirm', admin).status)

    def test_confirmation_failed_effect_save_undoes_the_fields_without_a_restart(self):
        """kittrial-5bb.255 R1: a failed effect save must not leave an unaudited
        confirmation in memory for the next save of anything to make durable."""
        admin = self.fixture('confirm')
        write = self.store._write

        def fail():
            if 'confirmed_by' in self.service.state['projects']['alpha']:
                raise OSError('synthetic confirmation save failure')
            return write()
        with mock.patch.object(self.store, '_write', side_effect=fail):
            reply = self.invoke('confirm', admin)
        self.assertEqual(500, reply.status, reply.data)
        record = self.service.state['projects']['alpha']
        # No restart: the callback put the fields back, so nothing unsaved remains.
        self.assertNotIn('confirmed_by', record)
        self.assertNotIn('confirmed_at', record)
        self.assertNotIn('registered_by', record)
        # The next save of anything writes no confirmation and no registration mark.
        self.store.save()
        disk = json.loads(self.store.path.read_text(encoding='utf-8'))
        self.assertNotIn('confirmed_by', disk['projects']['alpha'])
        self.assertNotIn('registered_by', disk['projects']['alpha'])
        self.assertEqual([], [event for event in self.service.state['audit']
                              if event['action'] == 'projects.confirm' and event['outcome'] == 'committed'])
        # The key was released, so the same key confirms without a restart.
        done = self.invoke('confirm', admin)
        self.assertEqual(200, done.status, done.data)
        self.assertEqual(self.admin_user['id'], self.service.state['projects']['alpha']['confirmed_by'])

    def test_confirmation_conflicting_key_and_no_key_retry_stay_refused(self):
        admin = self.fixture('confirm')
        self.assertEqual(200, self.invoke('confirm', admin).status)
        self.assertEqual(200, self.invoke('confirm', admin).status)
        # A changed body cannot inherit the first receipt, even if it has no route fields.
        self.assertEqual(409, self.invoke('confirm', admin, body={'unused': True}).status)
        self.assertEqual(409, self.invoke('confirm', admin, key=None).status)


if __name__ == '__main__':
    unittest.main()
