"""Host notes share .447's live store; the confined launcher is the only host identity source."""
import concurrent.futures
import contextlib
import copy
import hashlib
import io
import json
import os
import socket
import shutil
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import host_notes as hn
import http_service
import program_isolation
from test_coordinator_notes import NoteCase

TEXT = 'HOST-NOTE-CANARY: release requires a coordinator decision.'


@unittest.skipUnless(os.name == 'posix' and hasattr(socket, 'AF_UNIX'), 'private Linux host adapter')
class HostTests(NoteCase):
    @classmethod
    def setUpClass(cls):
        # Accounts/links are expensive; each test gets a restored independent state
        # snapshot and fresh host adapter, while sharing the immutable account fixture.
        cls.fixture = NoteCase(methodName='runTest')
        cls.fixture.setUp()
        cls.baseline = copy.deepcopy(cls.fixture.service.state)
        cls.addClassCleanup(cls.fixture.doCleanups)

    def setUp(self):
        for key, value in self.fixture.__dict__.items():
            if not key.startswith('_'): setattr(self, key, value)
        with self.service.store.lock:
            self.service.store.state = copy.deepcopy(self.baseline)
            self.service._note_times.clear(); self.service._note_throttle_audited.clear()
            self.service.store.save()
        self.root = self.tmp_path
        if hn.location(self.root).exists(): shutil.rmtree(hn.location(self.root))
        (self.root / 'deployment.private.json').write_text(json.dumps({'operators': ['operator']}))
        for p in ('prog', 'north', 'south'):
            path = self.root / 'projects' / p
            (path / '.beads').mkdir(parents=True, exist_ok=True)
            (path / '.beads/metadata.json').write_text('{}')
            (path / '.sessions.json').write_text(json.dumps({'schema_version': 1, 'records': {},
                       'owners': {'host-' + p: 'lane:' + p, 'operator':'person:operator'}}))
        for p in ('prog', 'north'):
            hn.enroll(self.root, p, 'lane:' + p, 'operator', approved_by='person:owner-' + p, approval='decision:owner-approved')
        self.running = hn.start(self.root, self.service, lambda a, b: http_service.note_pairing(self.service, self.backend, a, b))
        self.addCleanup(lambda: self.running.close() if self.running is not None else None)
        self.manager = self.service.host_notes_manager

    def envelope(self, args, project='prog', principal=None, text=None):
        attachments = {} if text is None else {'0': {'flag': '--file', 'text': text}}
        argv = list(args) + ([] if text is None else ['@attachment:0'])
        return {'project': project, 'principal': principal or 'lane:' + project, 'actor': 'host-' + project,
                'projects': [project], 'payload': hn.parse(argv, attachments)}

    def host(self, args, project='prog', text=None):
        return hn.call(self.root, self.envelope(args, project=project, text=text))

    def send_host(self, to='north', key='request-one', text=TEXT, project='prog'):
        return self.host(['send', '--to', to, '--request-id', key], project=project, text=text)

    def test_send_fetch_reply_and_noted_share_the_existing_store(self):
        sent = self.send_host(); nid = sent['note']['id']
        self.assertEqual('host', sent['note']['from']['kind'])
        self.assertTrue(sent['note']['delivery']['receiver_enrolled'])
        self.assertEqual(TEXT, self.box(self.tokens['nora'])['items'][0]['text'])
        fetched = self.host(['sent'])['items'][0]
        self.assertIn('fetched_at', fetched['delivery'])
        reply = self.host(['reply', nid, '--to', 'prog', '--request-id', 'reply-one'], project='north', text='Received')
        self.assertEqual(nid, reply['note']['reply_to'])
        self.host(['noted', reply['note']['id'], '--request-id', 'noted-one'])
        self.assertEqual('noted', self.host(['inbox'])['items'][0]['settled']['how'])
        self.assertEqual(3, len(self.service.state['host_note_requests']))
        self.assertEqual(0, len(self.events()))  # Host audit uses host.notes.*, with no text or receipt.
        self.assertEqual(3, len([e for e in self.service.state['audit'] if e['action'].startswith('host.notes.')]))

    def test_retries_keep_the_identity_and_changed_payload_conflicts(self):
        first = self.send_host(); again = self.send_host()
        self.assertEqual(first['note']['id'], again['note']['id']); self.assertTrue(again['reconciled'])
        with self.assertRaises(hn.Refusal) as e: self.send_host(text='Different')
        self.assertEqual(409, e.exception.status)
        self.assertEqual(1, len(self.service._notes()))

    def test_settling_a_note_never_wakes_the_coordinator_for_its_own_reply(self):
        note = self.send_host()['note']
        view = self.host(['wait', '--timeout', '0'], project='north')
        self.host(['noted', note['id'], '--request-id', 'noted'], project='north')
        again = self.host(['wait', '--timeout', '0', '--since', view['fingerprint']], project='north')
        self.assertEqual(view['fingerprint'], again['fingerprint'])
        self.assertFalse(again['changed']); self.assertTrue(again['timed_out'])
        self.assertEqual([], again['items'])

    def test_no_receiver_warning_follows_address_authorization(self):
        sent = self.send_host(to='south')
        self.assertFalse(sent['note']['delivery']['receiver_enrolled'])
        self.assertIn('warning', sent['note']['delivery'])
        for target in ('missing', 'unlinked'):
            with self.assertRaises(hn.Refusal) as e: self.send_host(to=target, key=target)
            self.assertEqual((404, 'No permitted coordinator note address'), (e.exception.status, str(e.exception)))

    def test_an_enrolled_host_is_a_receiver_even_without_any_enabled_web_agent(self):
        with self.service.store.lock:
            for agent in self.service.state['agents'].values():
                agent['enabled'] = False
            self.service.store.save()
        delivery = self.send_host()['note']['delivery']
        self.assertTrue(delivery['receiver_enrolled'])
        self.assertNotIn('warning', delivery)

    def test_operator_names_project_lists_and_other_actors_are_not_enrollment(self):
        for patch in ({'principal': 'person:operator', 'actor':'operator'}, {'principal': 'lane:south'}, {'actor': 'host-north'},
                      {'projects': []}, {'principal': 'lane:missing'}):
            req = self.envelope(['inbox']); req.update(patch)
            with self.assertRaises(hn.Refusal) as e: hn.call(self.root, req)
            self.assertEqual(403, e.exception.status)
        with self.assertRaises(hn.Refusal) as e: self.host(['inbox'],project='south')
        self.assertEqual(403,e.exception.status)

    def test_local_process_without_the_launcher_key_is_refused(self):
        code = '''import json,socket,sys
s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1]);f=s.makefile('rwb')
greeting=json.loads(f.readline());f.write((json.dumps({'request':json.loads(sys.argv[2]),'signature':'0'*64})+'\\n').encode());f.flush();print(f.readline().decode())
'''
        done = subprocess.run([sys.executable, '-c', code, str(hn.location(self.root) / 'service.sock'),
                               json.dumps(self.envelope(['inbox']))], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(403, json.loads(done.stdout)['status'])
        self.assertEqual([], self.service._notes())

    def test_signed_requests_cannot_be_replayed_on_another_connection(self):
        request = self.envelope(['inbox'])
        path = str(hn.location(self.root) / 'service.sock')
        def connect():
            s = socket.socket(socket.AF_UNIX);s.settimeout(3);s.connect(path)
            return s,s.makefile('rwb')
        s,f = connect()
        with s,f:
            greeting=hn.frame(f)
            captured={'request':request,'signature':hn.signature(hn.read_key(self.root),greeting['challenge'],request)}
            f.write(hn.encoded(captured)+b'\n');f.flush();self.assertEqual(200,hn.frame(f)['status'])
        s,f = connect()
        with s,f:
            self.assertNotEqual(greeting['challenge'],hn.frame(f)['challenge'])
            f.write(hn.encoded(captured)+b'\n');f.flush();self.assertEqual(403,hn.frame(f)['status'])

    def test_a_forged_service_gets_no_note_text_before_its_proof_is_verified(self):
        self.running.close();self.running=None
        path=hn.location(self.root)/'service.sock';received=[]
        fake=socket.socket(socket.AF_UNIX);fake.bind(str(path));fake.listen(1)
        def peer():
            connection,_=fake.accept()
            with connection:
                connection.sendall(hn.encoded({'challenge':'a'*64,'proof':'0'*64})+b'\n')
                received.append(connection.recv(hn.FRAME_MAX))
        thread=threading.Thread(target=peer);thread.start()
        try:
            with self.assertRaises(hn.Refusal) as e:self.send_host()
            self.assertEqual(503,e.exception.status)
            thread.join(timeout=3);self.assertFalse(thread.is_alive());self.assertEqual([b''],received)
        finally:
            fake.close();path.unlink()
            self.running=hn.start(self.root,self.service,self.manager.pairing);self.manager=self.service.host_notes_manager

    def test_revocation_ends_the_next_wait_pass_without_a_pointer(self):
        view = self.host(['wait', '--timeout', '0'])
        req = self.envelope(['wait', '--since', view['fingerprint'], '--timeout', '2'])
        with mock.patch.object(hn, 'WAIT_INTERVAL', 0.02), concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(hn.call, self.root, req)
            deadline = time.monotonic() + 2
            while not self.manager.waiters and time.monotonic() < deadline: time.sleep(0.005)
            hn.enroll(self.root, 'prog', 'lane:prog', 'operator', revoke=True)
            with self.assertRaises(hn.Refusal) as e: future.result(timeout=3)
            self.assertEqual(403, e.exception.status)

    def test_failure_matrix_hides_text_and_receipts_and_never_partially_writes(self):
        before = copy.deepcopy(self.service.state.get('coordinator_notes'))
        requests = [['send', '--to', 'north', '--request-id', 'failure-send'], ['inbox'], ['sent'],
                    ['reply', 'cnote-' + 'a' * 24, '--to', 'north', '--request-id', 'failure-reply'],
                    ['noted', 'cnote-' + 'a' * 24, '--request-id', 'failure-noted'], ['wait', '--timeout', '0']]
        enrollment = hn.location(self.root) / 'enrollment.json'
        saved_enrollment = enrollment.read_bytes()
        state_path = self.service.store.path; saved_state = state_path.read_bytes()
        for failure in ('enrollment', 'store', 'service'):
            if failure == 'enrollment': enrollment.write_text('{BROKEN-' + TEXT)
            elif failure == 'store': state_path.write_text('{BROKEN-' + TEXT)
            else: self.running.close(); self.running = None
            try:
                for args in requests:
                    with self.subTest(failure=failure, operation=args[0]), contextlib.redirect_stderr(io.StringIO()) as log:
                        with self.assertRaises(hn.Refusal) as e:
                            self.host(args, text=TEXT if args[0] in ('send', 'reply') else None)
                        self.assertEqual(503, e.exception.status)
                        self.assertNotIn(TEXT, str(e.exception)); self.assertNotIn(TEXT, log.getvalue())
                        self.assertNotIn(str(self.root), str(e.exception))
                        self.assertEqual(before, self.service.state.get('coordinator_notes'))
            finally:
                enrollment.write_bytes(saved_enrollment); state_path.write_bytes(saved_state)
                if self.running is None:
                    self.running = hn.start(self.root, self.service, self.manager.pairing)
                    self.manager = self.service.host_notes_manager
        # The unchanged id can be retried after every failure without inventing another identity.
        result = self.host(requests[0], text=TEXT)
        self.assertEqual(result['note']['id'], self.host(requests[0], text=TEXT)['note']['id'])

    def test_store_save_failure_does_not_leave_a_pending_note_for_a_later_save(self):
        with mock.patch.object(self.service.store, 'save', side_effect=OSError('CANARY-PRIVATE-SAVE')):
            with self.assertRaises(hn.Refusal) as e: self.send_host()
        self.assertEqual(503, e.exception.status)
        self.assertEqual([], self.service._notes())
        self.assertNotIn('CANARY', str(e.exception))
        result = self.send_host()
        self.assertEqual(1, len(self.service._notes()))
        self.assertEqual(result['note']['id'], self.send_host()['note']['id'])

    def test_restart_mid_wait_fails_closed_and_resumes_the_same_baseline(self):
        view = self.host(['wait', '--timeout', '0'])
        with concurrent.futures.ThreadPoolExecutor() as pool:
            f = pool.submit(self.host, ['wait', '--since', view['fingerprint'], '--timeout', '4'])
            deadline = time.monotonic() + 2
            while not self.manager.waiters and time.monotonic() < deadline: time.sleep(0.005)
            self.running.close(); self.running = None
            with self.assertRaises(hn.Refusal) as e: f.result(timeout=4)
            self.assertEqual(503, e.exception.status)
        self.running = hn.start(self.root, self.service, self.manager.pairing); self.manager = self.service.host_notes_manager
        self.assertEqual(view['fingerprint'], self.host(['wait', '--timeout', '0'])['fingerprint'])

    def test_an_endpoint_json_identity_override_is_refused(self):
        import endpoint
        request = {'project': 'prog', 'actor': 'host-prog', 'action': 'coordinator', 'args': ['notes', 'inbox'],
                   'principal': 'lane:north'}
        result = endpoint.execute(self.root, request, key_principal='lane:prog', key_projects=['prog'])
        self.assertEqual(422, result['host_notes_status'])
        request.pop('principal')
        self.assertEqual(0, endpoint.execute(self.root, request, key_principal='lane:prog', key_projects=['prog'])['returncode'])

    def test_note_text_remains_hidden_from_web_outsiders_and_other_host_projects(self):
        note = self.send_host()['note']
        program_isolation.assert_text_hidden(self, self.request, TEXT,
                    {name:self.tokens[name] for name in ('carl','vera','otto')}, ['prog', 'north'])
        req = self.envelope(['inbox'], project='south')
        with self.assertRaises(hn.Refusal): hn.call(self.root, req)
        self.assertEqual([], self.host(['inbox'])['items'])
        self.assertEqual(note['id'], self.host(['inbox'], project='north')['items'][0]['id'])


    def test_enrollment_is_local_operator_only_and_records_the_owner_approval(self):
        path = hn.location(self.root) / 'enrollment.json'; before = path.read_bytes()
        for operator, approver, approval in [('host-prog','person:owner','decision:approved'),
                                             ('operator',None,'decision:approved'),
                                             ('operator','lane:not-an-owner','decision:approved'),
                                             ('operator','person:owner',None)]:
            with self.subTest(operator=operator,approver=approver), self.assertRaises(ValueError):
                hn.enroll(self.root,'prog','lane:prog',operator,approved_by=approver,approval=approval)
            self.assertEqual(before,path.read_bytes())
        hn.enroll(self.root,'south','lane:south','operator',approved_by='person:south-owner',approval='decision:south-approval')
        grant = hn.read_enrollment(self.root)['grants']['south']['lane:south']
        self.assertEqual(('person:south-owner','decision:south-approval','operator'),
                         (grant['approved_by'],grant['approval'],grant['enrolled_by']))
        import endpoint
        result = endpoint.execute(self.root,{'project':'prog','actor':'host-prog','action':'coordinator',
                    'args':['notes','enroll','--principal=lane:other']},key_principal='lane:prog',key_projects=['prog'])
        self.assertEqual(422,result['host_notes_status'])

    def test_cached_notes_are_not_a_fallback_for_an_unreadable_store(self):
        note = self.send_host()['note']
        state = self.service.store.path; mode = state.stat().st_mode & 0o777
        state.chmod(0)
        try:
            operations = [['inbox'], ['sent'], ['wait', '--timeout', '0'],
                          ['send', '--to', 'prog', '--request-id', 'unreadable-send'],
                          ['reply', note['id'], '--to', 'prog', '--request-id', 'unreadable-reply'],
                          ['noted', note['id'], '--request-id', 'unreadable-noted']]
            for args in operations:
                with self.subTest(operation=args[0]), self.assertRaises(hn.Refusal) as e:
                    self.host(args, project='north', text=TEXT if args[0] in ('send', 'reply') else None)
                self.assertEqual(503,e.exception.status);self.assertNotIn(TEXT,str(e.exception))
        finally:state.chmod(mode)
        self.assertEqual(1, len(self.service._notes()))
        self.assertIsNone(self.service._notes()[0]['settled'])
        self.assertEqual(TEXT,self.host(['inbox'],project='north')['items'][0]['text'])

    def test_failure_logs_errors_audit_and_adapter_files_never_copy_text_or_receipts(self):
        note = self.send_host()['note']
        receipt = json.dumps(note['delivery'], sort_keys=True)
        with contextlib.redirect_stderr(io.StringIO()) as log, mock.patch.object(
                self.service.store, 'save', side_effect=OSError(TEXT + receipt)):
            with self.assertRaises(hn.Refusal) as e: self.host(['inbox'], project='north')
        self.assertEqual(503, e.exception.status)
        for output in (str(e.exception), log.getvalue(), json.dumps(self.service.state['audit'])):
            self.assertNotIn(TEXT, output); self.assertNotIn(receipt, output)
        for file in hn.location(self.root).iterdir():
            if file.is_file():
                self.assertNotIn(TEXT.encode(), file.read_bytes())
                self.assertNotIn(b'fetched_at', file.read_bytes())
        # Failed fetch is rolled back; later read obtains one legitimate receipt.
        self.assertNotIn('fetched_at', self.host(['sent'])['items'][0]['delivery'])
        self.assertIn('fetched_at', self.host(['inbox'], project='north')['items'][0]['delivery'])

    def test_a_sender_cannot_settle_its_own_outgoing_note_or_reply_to_an_unrelated_thread(self):
        note = self.send_host()['note']
        for args, project, text in ((['noted', note['id'], '--request-id', 'bad-noted'], 'prog', None),
                    (['reply', note['id'], '--to', 'north', '--request-id', 'bad-reply'], 'prog', TEXT)):
            with self.assertRaises(hn.Refusal) as e: self.host(args, project=project, text=text)
            self.assertEqual(404, e.exception.status)
        self.assertIsNone(self.host(['sent'])['items'][0]['settled'])

    def test_a_damaged_replay_reference_cannot_disclose_another_projects_note(self):
        first=self.send_host()['note']
        secret='FOREIGN-NORTH-SOUTH-NOTE'
        other=self.send_host(project='north',to='south',key='foreign',text=secret)['note']
        key=hashlib.sha256(hn.encoded(['lane:prog','prog','request-one'])).hexdigest()
        self.service.state['host_note_requests'][key]['note']=other['id'];self.service.store.save()
        with self.assertRaises(hn.Refusal) as e:self.send_host()
        self.assertEqual(503,e.exception.status);self.assertNotIn(secret,str(e.exception))
        self.assertNotEqual(first['id'],other['id'])

    def test_a_damaged_noted_replay_cannot_substitute_another_settled_note(self):
        first = self.send_host()['note']
        second = self.send_host(key='second')['note']
        for note, request in ((first, 'noted-first'), (second, 'noted-second')):
            self.host(['noted', note['id'], '--request-id', request], project='north')
        replay = ['noted', first['id'], '--request-id', 'noted-first']
        self.assertEqual(first['id'], self.host(replay, project='north')['note']['id'])
        key = hashlib.sha256(hn.encoded(['lane:north', 'north', 'noted-first'])).hexdigest()
        self.service.state['host_note_requests'][key]['note'] = second['id']
        self.service.store.save()
        with self.assertRaises(hn.Refusal) as e:
            self.host(replay, project='north')
        self.assertEqual(503, e.exception.status)
        self.assertEqual(2, len(self.service._notes()))

    def test_a_role_lost_during_a_web_fetch_returns_no_text_or_receipt(self):
        self.send_host()
        real=self.service.notes_for
        def demote(*args,**kwargs):
            result=real(*args,**kwargs)
            self.service.state['memberships']['north'][self.ids['nora']]='viewer';self.service.store.save()
            return result
        with mock.patch.object(self.service,'notes_for',side_effect=demote):
            response=self.request('GET','/v1/projects/north/notes',token=self.tokens['nora'])
        self.assertEqual(403,response.status);self.assertNotIn(TEXT,json.dumps(response.data))
        self.assertNotIn('fetched_at',self.host(['sent'])['items'][0]['delivery'])

    def test_the_host_surface_joins_the_program_isolation_harness(self):
        self.send_host()
        def read(principal, project, operation):
            args = [operation] + (['--timeout', '0'] if operation == 'wait' else [])
            import endpoint
            reply=endpoint.execute(self.root,{'project':project,'actor':'host-prog','action':'coordinator',
                        'args':['notes']+args},key_principal=principal,key_projects=[project])
            if reply['returncode']==0:return 200,json.loads(reply['stdout'])
            return reply['host_notes_status'],{'error':reply['stderr']}
        program_isolation.assert_host_notes_hidden(self, read, ['person:operator','lane:prog','lane:south'],
                                                    'north', 'missing', TEXT)

    def test_limits_are_bounded_and_an_unread_note_is_not_evicted(self):
        with mock.patch.object(self.service, 'NOTES_KEPT', 1):
            first = self.send_host()
            with self.assertRaises(hn.Refusal) as e: self.send_host(key='second')
            self.assertEqual(503, e.exception.status)
            self.assertEqual([first['note']['id']], [n['id'] for n in self.service._notes()])
        with mock.patch.object(hn, 'REQUESTS_MAX', 1):
            with self.assertRaises(hn.Refusal): self.send_host(key='full-journal')
        with self.manager.guard: self.manager.waiters.add(('lane:prog', 'prog'))
        try:
            with self.assertRaises(hn.Refusal) as e: self.host(['wait', '--timeout', '0'])
            self.assertEqual(429, e.exception.status)
            self.assertEqual(1, len(self.host(['sent'])['items']))
        finally:
            with self.manager.guard: self.manager.waiters.clear()

    def test_wait_releases_locks_so_a_writer_can_proceed(self):
        baseline = self.host(['wait', '--timeout', '0'], project='north')
        with mock.patch.object(hn, 'WAIT_INTERVAL', 0.02), concurrent.futures.ThreadPoolExecutor() as pool:
            f = pool.submit(self.host, ['wait', '--since', baseline['fingerprint'], '--timeout', '4'], project='north')
            deadline = time.monotonic() + 2
            while not self.manager.waiters and time.monotonic() < deadline: time.sleep(0.005)
            note = self.send_host()['note']
            self.assertEqual(note['id'], f.result(timeout=3)['items'][0]['note'])

    def test_socket_and_key_are_private_and_symlinks_cannot_replace_them(self):
        for name in ('adapter.key', 'service.sock'):
            self.assertEqual(0, (hn.location(self.root) / name).stat().st_mode & 0o077)
        with self.assertRaises(hn.Refusal): hn.start(self.root, self.service, self.manager.pairing)
        self.assertEqual([], self.host(['inbox'])['items'])  # refused duplicate start never replaces the live manager
        key = hn.location(self.root) / 'adapter.key'; saved = key.read_bytes(); key.unlink()
        other = self.root / 'other-key'; other.write_bytes(saved); other.chmod(0o600); key.symlink_to(other)
        try:
            with self.assertRaises(hn.Refusal): self.host(['inbox'])
        finally: key.unlink(); key.write_bytes(saved); key.chmod(0o600)

    def test_link_reads_never_hold_the_web_state_lock(self):
        original = self.backend.reference_links
        held = []
        def links(project):
            held.append(self.service.store.lock._is_owned())
            return original(project)
        with mock.patch.object(self.backend, 'reference_links', side_effect=links):
            first = self.send_host()['note']
            self.host(['reply', first['id'], '--to', 'prog', '--request-id', 'outside-lock-reply'],
                      project='north', text='Received without blocking web readers')
        self.assertGreaterEqual(len(held), 2)
        self.assertEqual([False] * len(held), held)

    def test_request_history_expires_after_a_day_without_extending_retries(self):
        moment = [self.service._now()]
        with mock.patch.object(self.manager, 'request_time', side_effect=lambda: moment[0], create=True), \
             mock.patch.object(hn, 'REQUESTS_MAX', 2):
            first = self.send_host()['note']
            self.send_host(key='second')
            moment[0] += 24 * 60 * 60 - 1
            replay = self.send_host()
            self.assertEqual(first['id'], replay['note']['id'])
            self.assertTrue(replay['reconciled'])
            with self.assertRaises(hn.Refusal) as full:
                self.send_host(key='third')
            self.assertEqual(503, full.exception.status)
            moment[0] += 2
            accepted = self.send_host(key='third')
            self.assertFalse(accepted['reconciled'])
            self.assertEqual(1, len(self.service.state['host_note_requests']))

    def test_full_request_history_names_the_retry_window(self):
        with mock.patch.object(hn, 'REQUESTS_MAX', 1):
            first = self.send_host()['note']
            attempts = ((['send', '--to', 'north', '--request-id', 'full-history'], 'prog', TEXT),
                        (['reply', first['id'], '--to', 'prog', '--request-id', 'full-reply'], 'north', TEXT),
                        (['noted', first['id'], '--request-id', 'full-noted'], 'north', None))
            for args, project, text in attempts:
                with self.subTest(operation=args[0]), self.assertRaises(hn.Refusal) as full:
                    self.host(args, project=project, text=text)
                self.assertEqual(503, full.exception.status)
                self.assertIn('request history', str(full.exception))
                self.assertIn('24-hour retry window', str(full.exception))
            self.assertIsNone(self.service._notes()[0]['settled'])
            self.assertEqual(first['id'], self.send_host()['note']['id'])

    def test_legacy_request_history_gets_a_persisted_upgrade_retry_window(self):
        first = self.send_host()['note']
        key = hashlib.sha256(hn.encoded(['lane:prog', 'prog', 'request-one'])).hexdigest()
        self.service.state['host_note_requests'][key].pop('at', None)
        self.service.store.save()
        moment = [self.service._now()]
        with mock.patch.object(self.manager, 'request_time', side_effect=lambda: moment[0], create=True):
            self.assertEqual(first['id'], self.send_host()['note']['id'])
            entry = self.service.state['host_note_requests'][key]
            self.assertIn('at', entry)
            self.assertEqual(entry, json.loads(self.service.store.path.read_text())['host_note_requests'][key])
            moment[0] += 24 * 60 * 60 - 1
            self.assertEqual(first['id'], self.send_host()['note']['id'])

    def test_sent_never_returns_another_projects_text_or_fetch_receipt(self):
        own = self.send_host()['note']
        secret = 'PRIVATE-NORTH-SOUTH-SENT-CANARY'
        foreign = self.send_host(project='north', to='south', key='foreign-sent', text=secret)['note']
        self.box(self.tokens['sam'], project='south')
        answer = self.host(['sent'])
        self.assertEqual((1, [own['id']]), (answer['total'], [n['id'] for n in answer['items']]))
        self.assertNotIn(secret, json.dumps(answer))
        self.assertNotIn(foreign['id'], json.dumps(answer))
        self.assertNotIn('fetched_at', answer['items'][0]['delivery'])

    def test_a_full_legacy_history_persists_its_grace_window_before_refusing(self):
        moment = [self.service._now()]
        with mock.patch.object(self.manager, 'request_time', side_effect=lambda: moment[0], create=True), \
             mock.patch.object(hn, 'REQUESTS_MAX', 1):
            self.send_host()
            key = next(iter(self.service.state['host_note_requests']))
            self.service.state['host_note_requests'][key].pop('at')
            self.service.store.save()
            with self.assertRaises(hn.Refusal) as full:
                self.send_host(key='after-legacy')
            self.assertEqual(503, full.exception.status)
            entry = self.service.state['host_note_requests'][key]
            self.assertEqual(moment[0], entry.get('at'))
            self.assertEqual(entry, json.loads(self.service.store.path.read_text())['host_note_requests'][key])
            moment[0] += 24 * 60 * 60 + 1
            self.assertFalse(self.send_host(key='after-legacy')['reconciled'])

    def test_an_evicted_note_keeps_its_retry_identity_until_the_window_ends(self):
        moment = [self.service._now()]
        with mock.patch.object(self.manager, 'request_time', side_effect=lambda: moment[0], create=True):
            first = self.send_host()['note']
            self.host(['noted', first['id'], '--request-id', 'settle-before-eviction'], project='north')
            with mock.patch.object(self.service, 'NOTES_KEPT', 1):
                replacement = self.send_host(key='replacement')['note']
            self.assertEqual([replacement['id']], [n['id'] for n in self.service._notes()])
            with self.assertRaises(hn.Refusal) as gone:
                self.send_host()
            self.assertEqual(410, gone.exception.status)
            self.assertEqual(1, len(self.service._notes()))
            moment[0] += 24 * 60 * 60 + 1
            accepted = self.send_host()
            self.assertFalse(accepted['reconciled'])
            self.assertNotEqual(first['id'], accepted['note']['id'])

    def test_enrollment_elsewhere_is_not_enrollment_here_even_with_an_owned_actor(self):
        incoming = self.send_host()['note']
        north = self.send_host(project='north', to='south', key='private-north', text='NORTH-PRIVATE')['note']
        path = self.root / 'projects/north/.sessions.json'
        saved = path.read_bytes()
        registry = json.loads(saved)
        registry['owners']['host-prog-in-north'] = 'lane:prog'
        path.write_text(json.dumps(registry))
        self.addCleanup(path.write_bytes, saved)
        from sessions import owners
        self.assertEqual('lane:prog', owners(path.parent)['host-prog-in-north'])
        for args in (['inbox'], ['sent'], ['wait', '--timeout', '0'],
                     ['noted', incoming['id'], '--request-id', 'foreign-noted'],
                     ['send', '--to', 'prog', '--request-id', 'foreign-send'],
                     ['reply', incoming['id'], '--to', 'prog', '--request-id', 'foreign-reply']):
            request = self.envelope(args, project='north', principal='lane:prog',
                                    text=TEXT if args[0] in ('send', 'reply') else None)
            request.update(actor='host-prog-in-north', projects=['prog', 'north'])
            with self.subTest(operation=args[0]), self.assertRaises(hn.Refusal) as denied:
                hn.call(self.root, request)
            self.assertEqual(403, denied.exception.status)
            self.assertNotIn('NORTH-PRIVATE', str(denied.exception))

    def test_a_web_service_caller_cannot_enter_the_host_namespace(self):
        import endpoint
        from http_authority import AuthorityConfig
        request = {'project': 'prog', 'actor': 'host-prog', 'action': 'coordinator',
                   'args': ['notes', 'inbox']}
        config = AuthorityConfig(str(self.service.store.path))
        answer = endpoint.execute(self.root, request, authority_config=config,
                                  key_principal='lane:prog', key_projects=['prog'])
        self.assertEqual((2, 403), (answer['returncode'], answer['host_notes_status']))

    def test_reply_after_the_program_link_is_removed_is_refused_without_settling(self):
        parent = self.send_host()['note']
        saved = copy.deepcopy(self.links)
        self.links['prog'] = []
        try:
            before = copy.deepcopy(self.service.state['coordinator_notes'])
            with self.assertRaises(hn.Refusal) as denied:
                self.host(['reply', parent['id'], '--to', 'prog', '--request-id', 'removed-link-reply'],
                          project='north', text='This address is no longer linked')
            self.assertEqual(404, denied.exception.status)
            self.assertEqual(before, self.service.state['coordinator_notes'])
        finally:
            self.links.clear(); self.links.update(saved)

    def test_losing_actor_ownership_ends_a_live_wait_without_pointers(self):
        baseline = self.host(['wait', '--timeout', '0'])
        first_pass = threading.Event()
        original = self.manager.pass_once
        def read(request):
            view = original(request)
            first_pass.set()
            return view
        path = self.root / 'projects/prog/.sessions.json'
        saved = path.read_bytes()
        try:
            with mock.patch.object(self.manager, 'pass_once', side_effect=read), \
                 mock.patch.object(hn, 'WAIT_INTERVAL', 0.02), concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(self.host, ['wait', '--since', baseline['fingerprint'], '--timeout', '2'])
                self.assertTrue(first_pass.wait(timeout=2))
                registry = json.loads(saved)
                registry['owners']['host-prog'] = 'lane:replacement'
                path.write_text(json.dumps(registry))
                with self.assertRaises(hn.Refusal) as denied:
                    future.result(timeout=3)
                self.assertEqual(403, denied.exception.status)
        finally:
            path.write_bytes(saved)

    def test_host_inbox_adds_fetch_receipts_only_for_the_requested_page(self):
        first = self.send_host()['note']
        second = self.send_host(key='second-page-note')['note']
        page = self.host(['inbox', '--offset=1', '--limit=1'], project='north')
        self.assertEqual((2, 1), (page['total'], len(page['items'])))
        selected = page['items'][0]['id']
        notes = {n['id']: n for n in self.host(['sent'])['items']}
        for nid in (first['id'], second['id']):
            self.assertEqual(nid == selected, 'fetched_at' in notes[nid]['delivery'])

    def test_damaged_enrollment_gives_web_senders_an_actionable_503_and_a_retry(self):
        path = hn.location(self.root) / 'enrollment.json'
        saved = path.read_bytes()
        before = len(self.service._notes())
        path.write_text('{BROKEN')
        try:
            refused = self.send(self.tokens['pat'], key='repair-enrollment-retry')
            self.assertEqual(503, refused.status, refused.data)
            self.assertIn('enrollment', refused.data['error']['message'].lower())
            self.assertIn('repair', refused.data['error']['message'].lower())
            self.assertEqual(before, len(self.service._notes()))
        finally:
            path.write_bytes(saved)
        repaired = self.send(self.tokens['pat'], key='repair-enrollment-retry')
        self.assertEqual(201, repaired.status, repaired.data)
        self.assertEqual(before + 1, len(self.service._notes()))

    def test_host_audit_contains_only_operation_metadata_and_rolls_back_failed_saves(self):
        raw_request = 'REQUEST-ID-PRIVATE-CANARY'
        note = self.send_host(key=raw_request)['note']
        events = self.events('host.notes.send')
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual(('prog', 'host-prog', 'accepted'),
                         (event['project_id'], event['actor'], event['outcome']))
        self.assertIsNone(event['reason'])
        serialized = json.dumps(event)
        for private in (TEXT, raw_request, json.dumps(note['delivery'], sort_keys=True)):
            self.assertNotIn(private, serialized)
        self.assertNotIn('fetched_at', serialized)
        before = copy.deepcopy(self.service.state['audit'])
        with mock.patch.object(self.service.store, 'save', side_effect=OSError('audit-save-failure')):
            with self.assertRaises(hn.Refusal):
                self.send_host(key='audit-failed')
        self.assertEqual(before, self.service.state['audit'])


@unittest.skipUnless(os.name == 'posix' and hasattr(socket, 'AF_UNIX'), 'private Linux host adapter')
class ProcessTests(unittest.TestCase):
    def test_an_abrupt_service_restart_keeps_note_and_request_identity_atomically(self):
        from http_auth import Store
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup); root = Path(temp.name)
        (root / 'deployment.private.json').write_text(json.dumps({'operators':['operator']}))
        for project in ('prog','north'):
            path = root / 'projects' / project; (path / '.beads').mkdir(parents=True)
            (path / '.beads/metadata.json').write_text('{}')
            (path / '.sessions.json').write_text(json.dumps({'schema_version':1,'records':{},
                'owners':{'host-'+project:'lane:'+project}}))
            hn.enroll(root,project,'lane:'+project,'operator',approved_by='person:owner',approval='decision:approved')
        Store(root / 'state.json').save()
        code = """import sys,time
from pathlib import Path
from http_auth import Store,Service
import host_notes
r=Path(sys.argv[1]);s=Service(Store(r/'state.json'))
h=host_notes.start(r,s,lambda a,b:a!=b and {a,b}=={'prog','north'})
print('READY',flush=True)
while True: time.sleep(1)
"""
        children=[]
        def start():
            child=subprocess.Popen([sys.executable,'-c',code,str(root)],cwd=str(KIT),stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,text=True);children.append(child)
            import select
            ready,_,_=select.select([child.stdout],[],[],10)
            self.assertTrue(ready,'service did not start')
            self.assertEqual('READY',child.stdout.readline().strip(), child.stderr.read() if child.poll() is not None else '')
            return child
        def cleanup():
            for child in children:
                if child.poll() is None:child.terminate()
                child.communicate(timeout=10)
        self.addCleanup(cleanup)
        child=start()
        def request(args,project='prog',text=None):
            attachments={} if text is None else {'0':{'flag':'--file','text':text}}
            payload=hn.parse(args+([] if text is None else ['@attachment:0']),attachments)
            return {'project':project,'principal':'lane:'+project,'actor':'host-'+project,'projects':[project],'payload':payload}
        sent=request(['send','--to','north','--request-id','restart-key'],text=TEXT)
        first=hn.call(root,sent)
        baseline=hn.call(root,request(['wait','--timeout','0'],'north'))
        with concurrent.futures.ThreadPoolExecutor() as pool:
            f=pool.submit(hn.call,root,request(['wait','--since',baseline['fingerprint'],'--timeout','4'],'north'))
            # Synchronize by a second call: the listener has accepted requests before termination.
            hn.call(root,request(['sent']))
            child.terminate();child.communicate(timeout=10)
            with self.assertRaises(hn.Refusal) as e:f.result(timeout=6)
            self.assertEqual(503,e.exception.status)
        start()  # recovers its private stale socket under the listener lease
        again=hn.call(root,sent)
        self.assertTrue(again['reconciled']);self.assertEqual(first['note']['id'],again['note']['id'])
        self.assertEqual(1,len(hn.call(root,request(['inbox'],'north'))['items']))


class ParserTests(unittest.TestCase):
    def test_private_frames_are_bounded_complete_and_have_unique_fields(self):
        self.assertEqual({}, hn.frame(io.BytesIO(b'{}\n')))
        for wire, limit in ((b'{}    \n', 5), (b'{}', hn.FRAME_MAX),
                            (b'{"actor":"one","actor":"two"}\n', hn.FRAME_MAX)):
            with self.subTest(wire=wire), self.assertRaises(hn.Refusal):
                hn.frame(io.BytesIO(wire), limit)

    def test_wait_and_page_limits_reject_nonfinite_or_out_of_range_values(self):
        for flags in (['--timeout=-1'], ['--timeout=51'], ['--timeout=nan'],
                      ['--timeout=inf'], ['--offset=-1'], ['--limit=0'], ['--limit=51']):
            with self.subTest(flags=flags), self.assertRaises(hn.Refusal):
                hn.parse(['wait'] + flags, {})
        self.assertEqual(50, hn.parse(['wait', '--timeout=50', '--limit=50'], {})['limit'])

    def test_help_requires_no_config_or_transport(self):
        import client
        with mock.patch.object(sys,'argv',['client.py','coordinator','notes','--help']), \
                mock.patch.object(client,'request',side_effect=AssertionError('transport called')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0,client.main())
        self.assertIn('--request-id',output.getvalue());self.assertIn('docs host-notes',output.getvalue())

    @unittest.skipUnless(os.name == 'posix', 'Linux listener startup')
    def test_with_nobody_enrolled_no_listener_or_metadata_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(hn.start(Path(directory), None, None))
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_standalone_client_transports_each_file_spelling_without_opening_a_server_path(self):
        import client
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'note.txt';path.write_text(TEXT,encoding='utf-8')
            for flags in (['--file',str(path)],['--file='+str(path)],['-f',str(path)],
                          ['-f='+str(path)],['-f'+str(path)]):
                wire=json.loads(client._wire('prog','host-prog',
                     ['notes','send','--to=north','--request-id=one']+flags,'coordinator',None))
                parsed=hn.parse(wire['args'][1:],wire['attachments'])
                self.assertEqual(TEXT,parsed['text']);self.assertEqual('one',parsed['request_id'])
                self.assertNotIn(str(path),json.dumps(wire))

    def test_an_older_endpoint_gets_an_actionable_refusal(self):
        import client
        with tempfile.TemporaryDirectory() as directory:
            cfg=Path(directory)/'client.json';cfg.write_text(json.dumps(
                 {'transport':'ssh','host':'h','endpoint':'/e','root':'/r'}))
            with mock.patch.object(sys,'argv',['client.py','--config',str(cfg),'--project','prog',
                    '--actor','host-prog','--','coordinator','notes','inbox']), \
                 mock.patch.object(client,'request',return_value={'returncode':2,'stdout':'',
                                 'stderr':"Unknown coordinator command 'notes'"}), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(2,client.main())
            self.assertIn('updated endpoint',error.getvalue())

    def test_spaced_joined_and_short_file_inputs_and_unknown_flags(self):
        for args in (['send', '--to', 'north', '--request-id', 'r1'], ['send', '--to=north', '--request-id=r1']):
            self.assertEqual('north', hn.parse(args + ['@attachment:0'], {'0': {'flag': '-f', 'text': TEXT}})['to'])
        for args in (['enroll'], ['inbox', '--principal=lane:other'], ['send', '--to=north', '--request-id=x', '--file=/srv/private']):
            with self.assertRaises(hn.Refusal): hn.parse(args, {})


    def test_wait_does_not_start_a_pass_that_will_not_fit_the_read_cost_budget(self):
        clock, passes = [0.0], []
        class Stop:
            def is_set(self): return False
            def wait(self, seconds): clock[0] += seconds
        manager = hn.Manager(Path('.'), None, None)
        manager.stopped = Stop()
        def read(request):
            passes.append(clock[0])
            clock[0] += 0.04
            return {'fingerprint': 'same', 'items': [], 'total': 0}
        request = {'principal': 'lane:prog', 'project': 'prog',
                   'payload': {'timeout': 0.11, 'since': 'same'}}
        with mock.patch.object(hn, 'locked', return_value=contextlib.nullcontext()), \
             mock.patch.object(hn, 'authorize'), mock.patch.object(hn, 'WAIT_INTERVAL', 0.01), \
             mock.patch.object(hn.time, 'monotonic', side_effect=lambda: clock[0]), \
             mock.patch.object(manager, 'pass_once', side_effect=read):
            answer = manager.wait(request)
        self.assertTrue(answer['timed_out'])
        self.assertLessEqual(len(passes), 2)
        self.assertLessEqual(clock[0], 0.11 + 1e-9)


class StorageValidationTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)

    def test_preflight_refuses_damaged_note_shapes_without_changing_cache_or_disk(self):
        from http_auth import Store,Service
        service=Service(Store(self.root/'state.json'))
        manager=hn.Manager(self.root,service,None)
        for record in ({'notes':[]},{'notes':[],'dropped':None},{'notes':[],'dropped':[]}):
            with self.subTest(record=record):
                service.state['coordinator_notes']=copy.deepcopy(record);service.store.save()
                before=copy.deepcopy(service.state);disk=service.store.path.read_bytes()
                with self.assertRaises(hn.Refusal) as error:manager.preflight()
                self.assertEqual(503,error.exception.status)
                self.assertEqual(before,service.state)
                self.assertEqual(disk,service.store.path.read_bytes())

    def test_request_timestamps_refuse_booleans_nonfinite_and_unrepresentable_numbers(self):
        from http_auth import Store, Service
        service = Service(Store(self.root / 'timestamps.json'))
        manager = hn.Manager(self.root, service, None)
        for value in (True, float('inf'), float('nan'), 10 ** 400):
            with self.subTest(value=repr(value)):
                service.state['host_note_requests'] = {'a' * 64: {
                    'digest': 'b' * 64, 'note': 'cnote-' + 'c' * 24, 'at': value}}
                with self.assertRaises(hn.Refusal) as refused:
                    manager.request_history()
                self.assertEqual(503, refused.exception.status)

    def test_enrollment_counts_the_bytes_it_writes_and_keeps_the_old_file_on_refusal(self):
        (self.root/'deployment.private.json').write_text(json.dumps({'operators':['operator']}))
        project=self.root/'projects/pp/.beads';project.mkdir(parents=True)
        (project/'metadata.json').write_text('{}')
        hn.enroll(self.root,'pp','lane:pp','operator',approved_by='person:owner',approval='decision:initial')
        path=hn.location(self.root)/'enrollment.json';before=path.read_bytes()
        with mock.patch.object(hn,'ENROLLMENT_MAX',2000):
            with self.assertRaises(hn.Refusal) as error:
                hn.enroll(self.root,'pp','lane:pp','operator',approved_by='person:owner',approval='é'*240)
            self.assertEqual(503,error.exception.status)
        self.assertEqual(before,path.read_bytes())
        self.assertEqual('decision:initial',hn.read_enrollment(self.root)['grants']['pp']['lane:pp']['approval'])

    def test_revoke_validates_optional_metadata_before_changing_the_file(self):
        (self.root/'deployment.private.json').write_text(json.dumps({'operators':['operator']}))
        project=self.root/'projects/pp/.beads';project.mkdir(parents=True)
        (project/'metadata.json').write_text('{}')
        hn.enroll(self.root,'pp','lane:pp','operator',approved_by='person:owner',approval='decision:initial')
        path=hn.location(self.root)/'enrollment.json';before=path.read_bytes()
        for fields in ({'approval':''},{'approval':'x'*241},{'approved_by':''}):
            with self.subTest(fields=fields):
                path.write_bytes(before)
                with self.assertRaises(ValueError):
                    hn.enroll(self.root,'pp','lane:pp','operator',revoke=True,**fields)
                self.assertEqual(before,path.read_bytes())
                self.assertIn('lane:pp',hn.read_enrollment(self.root)['grants']['pp'])
        path.write_bytes(before)
        hn.enroll(self.root,'pp','lane:pp','operator',revoke=True,
                  approved_by='person:owner',approval='decision:revoke')
        document=hn.read_enrollment(self.root)
        self.assertNotIn('lane:pp',document['grants']['pp'])
        self.assertEqual('decision:revoke',document['audit'][-1]['approval'])
