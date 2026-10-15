"""Actual project-file and live-authority adapter checks; HTTP/native qualification is separate."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import guidance
import http_authority as auth
import onboarding
import web_guidance as web
from test_endpoint_authority_block import (authority_state, session_block, agent_block,
                                           OWNER, COORDINATOR, CONTRIBUTOR, AGENT, CREDENTIAL, PROJECT)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='web-guidance-files-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'pp';self.project.mkdir()
        self.store = self.root / 'authority.json'
        self.config = auth.AuthorityConfig(str(self.store))
        self.state = authority_state();self.save()

    def save(self):
        self.store.write_text(json.dumps(self.state), encoding='utf-8')

    def request(self, operation, actor=OWNER, key=None, **fields):
        block = agent_block(web.CAPABILITIES[operation]) if actor == AGENT else session_block(actor, web.CAPABILITIES[operation])
        return dict(action='web-guidance', project=PROJECT, actor=actor,
                    args=[json.dumps(dict(operation=operation, **fields))], authority=block,
                    attachments={}, operation_id=key)

    def call(self, request):
        return web.web_action(self.project, PROJECT, request, self.config)

    def data(self, request):
        reply=self.call(request);self.assertEqual(reply['returncode'],0,reply)
        return json.loads(reply['stdout'])

    def set(self, text='Current instructions', key='set-1', actor=OWNER):
        request=self.request('set',actor,key);request['attachments']={'text':dict(flag='--file',text=text)}
        return self.data(request)

    def files(self):
        return {p.name:p.read_bytes() for p in self.project.iterdir() if p.is_file() and not p.name.startswith('.http-')}

    def test_onboarding_partial_copy_failure_stays_unknown_and_never_repeats(self):
        (self.project/'ONBOARDING.md').write_bytes(b'Operator entry\n')
        request = self.request('set', OWNER, 'onboarding-partial')
        request.update(action='set-onboarding', args=['set'], attachments={'text': dict(flag='--file', text='New entry')})
        original = onboarding.write_project
        def fail_after_copy(target, text):
            if Path(target).name == 'ONBOARDING.md': raise ValueError('Synthetic target fault after retained copy')
            return original(target, text)
        with patch.object(onboarding, 'write_project', side_effect=fail_after_copy) as writer:
            result = onboarding.web_action(self.project, PROJECT, request, self.config)
            self.assertEqual(result['returncode'], 124, result); self.assertEqual(writer.call_count, 2)
            replay = onboarding.web_action(self.project, PROJECT, request, self.config)
            self.assertEqual(replay['returncode'], 124, replay); self.assertEqual(writer.call_count, 2)
        self.assertEqual((self.project/onboarding.OPERATOR_COPY).read_bytes(), b'Operator entry\n')
        self.assertEqual((self.project/'ONBOARDING.md').read_bytes(), b'Operator entry\n')

    def test_write_attributes_actual_web_identity_and_host_reads_unchanged(self):
        made=self.set();self.assertEqual(made['set_by'],OWNER)
        self.assertEqual(guidance.read(self.project,['get'],'host')['set_by'],OWNER)
        self.assertEqual(guidance.status(self.project,'ops',['ops'],host=True)['set_by'],OWNER)
        with self.assertRaises(ValueError):guidance.status(self.project,OWNER,['ops'])
        guidance.write_guidance(self.project,'Host replacement','ops')
        self.assertEqual(self.data(self.request('read'))['set_by'],'ops')

    def test_member_reads_never_acknowledge_and_explicit_exact_version_ack_retries(self):
        made=self.set();before=self.files();value=self.data(self.request('read',CONTRIBUTOR))
        self.assertFalse(value['acknowledged']);self.assertEqual(before,self.files())
        request=self.request('ack',CONTRIBUTOR,'ack-1',version=made['version'])
        first=self.data(request);snapshot=self.files();second=self.data(request)
        self.assertEqual(first,second);self.assertEqual(snapshot,self.files())
        self.assertTrue(self.data(self.request('read',CONTRIBUTOR))['acknowledged'])

    def test_stale_and_unbound_ack_refuse_before_file_effect(self):
        old=self.set();self.set('Replacement','set-2');before=self.files()
        with self.assertRaises(ValueError):self.call(self.request('ack',CONTRIBUTOR,'stale',version=old['version']))
        self.assertEqual(before,self.files())
        (self.project/'GUIDANCE.md').write_text('A hand edit',encoding='utf-8')
        value=self.data(self.request('read',CONTRIBUTOR));self.assertIsNone(value['text']);self.assertIsNone(value['version']);self.assertTrue(value['unbound'])
        before=self.files()
        with self.assertRaises(ValueError):self.call(self.request('ack',CONTRIBUTOR,'unbound',version=guidance.version_of('A hand edit')))
        self.assertEqual(before,self.files())

    def test_shape_actor_project_and_capability_forgery(self):
        self.set();before=self.files()
        request=self.request('read');request['actor']=CONTRIBUTOR
        self.assertEqual(self.call(request).get('authority_status'),403)
        request=self.request('read');request['authority']['project']='pq'
        self.assertEqual(self.call(request).get('authority_status'),403)
        request=self.request('read');request['authority']['capability']=auth.CAP_COORDINATE
        self.assertEqual(self.call(request).get('authority_status'),403)
        request=self.request('read');request['authority']['user_id']=CONTRIBUTOR
        self.assertNotEqual(self.call(request)['returncode'],0)
        for body in ({'operation':'ack','version':'a'*64,'actor':OWNER},{'operation':'set','path':'/tmp/other'},{'operation':'other'}):
            with self.subTest(body=body),self.assertRaises(ValueError):web.validate(body)
        self.assertEqual(before,self.files())

    def test_ordinary_member_cannot_set_but_agent_coordinate_grant_is_current(self):
        request=self.request('set',CONTRIBUTOR,'denied');request['attachments']={'text':dict(flag='--file',text='Not allowed')}
        self.assertEqual(self.call(request).get('authority_status'),403)
        request=self.request('set',AGENT,'agent-set');request['attachments']={'text':dict(flag='--file',text='Agent instructions')}
        self.assertEqual(self.call(request).get('authority_status'),403)
        self.state['coordinator_grants']={PROJECT:{AGENT:dict(granted_by=OWNER,granted_at='2026-10-01T00:00:00Z')}};self.save()
        made=self.data(request);self.assertEqual(made['set_by'],AGENT)
        before=self.files();self.state['coordinator_grants'][PROJECT].pop(AGENT);self.save()
        self.assertEqual(self.call(request).get('authority_status'),403);self.assertEqual(before,self.files())

    def test_worker_read_and_ack_have_only_its_authenticated_namespace(self):
        made=self.set();credential=dict(id='worker',user_id=OWNER,actor='worker-lane',project_id=PROJECT,revoked=False,expires_at=time.time()+3600,scopes=['tasks'])
        self.state['credentials']['worker']=credential;self.save()
        request=self.request('ack',OWNER,'worker-ack',version=made['version']);request['actor']='worker-lane';request['authority']=dict(via='credential',user_id=OWNER,credential_id='worker',session_hash=None,project=PROJECT,capability=auth.CAP_READ)
        self.assertTrue(self.data(request)['acknowledged'])
        request['actor']='someone-else';self.assertEqual(self.call(request).get('authority_status'),403)
        request['actor']='worker-lane';request['args']=[json.dumps(dict(operation='clear'))];request['authority']['capability']=auth.CAP_COORDINATE
        self.assertEqual(self.call(request).get('authority_status'),403)

    def test_worker_without_distinct_actor_cannot_acknowledge_for_issuer(self):
        made = self.set()
        for actor in (None, OWNER):
            with self.subTest(actor=actor):
                credential = dict(id='worker', user_id=OWNER, project_id=PROJECT,
                                  revoked=False, expires_at=time.time()+3600, scopes=['tasks'])
                if actor is not None:
                    credential['actor'] = actor
                self.state['credentials']['worker'] = credential
                self.save()
                request = self.request('ack', OWNER, 'issuer-ack-'+str(actor), version=made['version'])
                request['authority'] = dict(via='credential', user_id=OWNER, credential_id='worker',
                                            session_hash=None, project=PROJECT, capability=auth.CAP_READ)
                before = {p.name: p.read_bytes() for p in self.project.iterdir() if p.is_file()}
                denied = self.call(request)
                self.assertEqual(denied.get('authority_status'), 403, denied)
                self.assertIn('distinct worker actor', denied['stderr'])
                self.assertEqual(before, {p.name: p.read_bytes() for p in self.project.iterdir() if p.is_file()})
                self.assertFalse(guidance.read(self.project, ['get'], OWNER)['acknowledged'])
        # An authenticated human still acknowledges their own read explicitly.
        self.assertTrue(self.data(self.request('ack', OWNER, 'human-ack', version=made['version']))['acknowledged'])

    def test_completed_replay_reauthorizes_and_conflicting_body_refuses(self):
        self.set();before=self.files();request=self.request('set',OWNER,'set-1');request['attachments']={'text':dict(flag='--file',text='Different')}
        self.assertEqual(self.call(request)['returncode'],2);self.assertEqual(before,self.files())
        self.state['memberships'][PROJECT][OWNER]='viewer';self.save();request['attachments']['text']['text']='Current instructions'
        self.assertEqual(self.call(request).get('authority_status'),403);self.assertEqual(before,self.files())

    def test_clear_after_partial_unlink_is_unknown_and_never_blindly_retried(self):
        self.set();(self.project/'.guidance.json').unlink();(self.project/'.guidance.json').mkdir()
        request=self.request('clear',OWNER,'partial-clear');first=self.call(request)
        self.assertEqual(first['returncode'],124);self.assertFalse((self.project/'GUIDANCE.md').exists())
        self.assertEqual(self.call(request)['returncode'],124)

    def test_after_text_write_metadata_failure_is_unknown_not_validation_or_success(self):
        request=self.request('set',OWNER,'cut-set');request['attachments']={'text':dict(flag='--file',text='Written before cut')}
        with patch.object(guidance,'atomic',side_effect=OSError('synthetic metadata cut')):
            self.assertEqual(self.call(request)['returncode'],124)
        self.assertEqual((self.project/'GUIDANCE.md').read_text(),'Written before cut')
        self.assertEqual(self.call(request)['returncode'],124)
        self.assertTrue(self.data(self.request('read'))['unbound'])

    def test_status_is_project_filtered_bounded_and_discloses_retention(self):
        made=self.set();guidance.acknowledge(self.project,OWNER,made['version']);guidance.acknowledge(self.project,CONTRIBUTOR,made['version']);guidance.acknowledge(self.project,'foreign-actor',made['version'])
        value=self.data(self.request('status',offset=0,limit=1));self.assertEqual(value['acknowledged_total'],2);self.assertEqual(len(value['acknowledged']),1);self.assertEqual(value['next_offset'],1);self.assertIn('not a complete historical',value['retention'])
        second=self.data(self.request('status',offset=1,limit=1));self.assertIsNone(second['next_offset']);self.assertEqual({value['acknowledged'][0]['actor'],second['acknowledged'][0]['actor']},{OWNER,CONTRIBUTOR})
        self.assertNotIn('foreign-actor',json.dumps(value)+json.dumps(second))
        self.assertEqual(self.call(self.request('status',CONTRIBUTOR,offset=0,limit=10))['authority_status'],403)

    def test_whole_operator_and_web_onboarding_inclusion_with_safe_refusals(self):
        self.assertEqual(self.data(self.request('onboarding',CONTRIBUTOR))['state'],'not-set')
        target=self.project/'ONBOARDING.md';target.write_bytes(b'Operator instructions\n')
        value=self.data(self.request('onboarding',CONTRIBUTOR));self.assertEqual(value['source'],'operator');self.assertEqual(value['text'],'Operator instructions\n')
        document=onboarding.web_document('Web project information');target.write_bytes(document.encode('utf-8'));value=self.data(self.request('onboarding',CONTRIBUTOR));self.assertEqual(value['source'],'web');self.assertEqual(value['text'],document)
        target.unlink();target.mkdir()
        with self.assertRaises(ValueError):self.call(self.request('onboarding',CONTRIBUTOR))
        target.rmdir();target.write_text('x'*(onboarding.PROJECT_LIMIT+1))
        with self.assertRaises(ValueError):self.call(self.request('onboarding',CONTRIBUTOR))

    def test_archived_and_removed_membership_read_refuse_current_state(self):
        self.set();self.state['projects'][PROJECT]['archived']=True;self.save();self.assertEqual(self.call(self.request('read'))['authority_status'],409)
        self.state['projects'][PROJECT]['archived']=False;self.state['memberships'][PROJECT].pop(CONTRIBUTOR);self.save();self.assertEqual(self.call(self.request('read',CONTRIBUTOR))['authority_status'],404)

    def test_reads_use_common_authority_lock_and_keep_closed_operation_validation(self):
        self.set()
        before = self.files()
        for operation in web.READ_OPERATIONS:
            fields = {'offset': 0, 'limit': 20} if operation == 'status' else {}
            request = self.request(operation, **fields)
            self.assertEqual(web.request_operation(request), operation)
            with patch.object(auth, 'file_lock', wraps=auth.file_lock) as common_lock:
                self.data(request)
            common_lock.assert_called_once_with(self.config.lock)
        self.assertEqual(self.files(), before)
        for request in ({'args': []}, {'args': ['{"operation":"read"}'],
                                      'attachments': {'text': {'text': 'not accepted', 'flag': '--file'}}}):
            with self.assertRaises(ValueError): web.request_operation(request)

    def test_closed_attachment_and_pagination_shapes(self):
        for attachments in ([],{'text':{'flag':'--file','text':'x'},'extra':{}},{'text':{'flag':'--file','text':'x','path':'/tmp/no'}}):
            with self.subTest(attachments=attachments),self.assertRaises(ValueError):web.validate(dict(operation='set'),attachments)
        for field,value in [('offset',True),('offset',501),('limit',0),('limit',101)]:
            body=dict(operation='status',offset=0,limit=20);body[field]=value
            with self.subTest(body=body),self.assertRaises(ValueError):web.validate(body)




# These exercise endpoint.execute and its authority table with the fixture's stub
# tracker. They are separate from the still-required real HTTP/native qualification.
from test_endpoint_authority_block import EndpointHarness, POSIX, endpoint


@POSIX
class EndpointTests(EndpointHarness):
    def request(self, operation, actor=OWNER, key=None, **fields):
        return dict(project=PROJECT, actor=actor, action='web-guidance',
                    args=[json.dumps(dict(operation=operation,**fields))],
                    attachments={},operation_id=key)

    def test_guidance_reads_obey_common_project_policy_at_the_dispatcher(self):
        for operation in web.READ_OPERATIONS:
            fields = {'offset': 0, 'limit': 20} if operation == 'status' else {}
            for shared in (False, True):
                request = self.request(operation, **fields)
                with self.subTest(operation=operation, shared=shared), \
                        patch.object(endpoint, 'coordination_lock_shared', return_value=shared) as policy, \
                        patch.object(endpoint, 'held_coordination_lock', wraps=endpoint.held_coordination_lock) as held:
                    reply = self.web(request, session_block(OWNER, web.CAPABILITIES[operation]))
                self.assertEqual(reply['returncode'], 0, reply)
                self.assertEqual(policy.call_count, 1)
                self.assertEqual(policy.call_args.args[1:], ('web-guidance', request['args'], operation))
                self.assertEqual(held.call_args.kwargs, {'shared': shared})

    def test_ack_set_and_clear_still_wait_for_shared_project_readers(self):
        import concurrent.futures
        import fcntl
        import threading
        path = self.root / 'projects' / PROJECT
        made = guidance.write_guidance(path, 'Before write', 'ops')
        for operation in ('ack', 'set', 'clear'):
            with self.subTest(operation=operation):
                request = self.request(operation, OWNER, 'exclusive-' + operation,
                                       **({'version': made['version']} if operation == 'ack' else {}))
                if operation == 'set':
                    request['attachments'] = {'text': {'flag': '--file', 'text': 'After write'}}
                before = (path / 'GUIDANCE.md').read_bytes()
                attempted = threading.Event()
                real_flock = fcntl.flock
                def observe(fd, mode):
                    if mode == fcntl.LOCK_EX:
                        attempted.set()
                    return real_flock(fd, mode)
                with (path / '.coordination.lock').open('a') as lock:
                    real_flock(lock, fcntl.LOCK_SH)
                    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    try:
                        with patch.object(fcntl, 'flock', side_effect=observe):
                            future = pool.submit(self.web, request, session_block(OWNER, web.CAPABILITIES[operation]))
                            self.assertTrue(attempted.wait(5), 'Writer never reached the project lock')
                            self.assertFalse(future.done(), 'Writer passed an active shared project reader')
                            self.assertEqual((path / 'GUIDANCE.md').read_bytes(), before)
                            real_flock(lock, fcntl.LOCK_UN)
                            reply = future.result(timeout=5)
                    finally:
                        real_flock(lock, fcntl.LOCK_UN)
                        pool.shutdown(wait=True)
                self.assertEqual(reply['returncode'], 0, reply)

    def test_service_dispatch_uses_web_setter_and_original_host_reader(self):
        request=self.request('set',COORDINATOR,'coordinator-set');request['attachments']={'text':dict(flag='--file',text='Web coordinator instructions')}
        made=self.web(request,session_block(COORDINATOR,auth.CAP_COORDINATE));self.assertEqual(made['returncode'],0,made)
        path=self.root/'projects'/PROJECT
        self.assertEqual(guidance.read(path,['get'],'host')['set_by'],COORDINATOR)
        self.assertEqual(self.web(self.request('read',CONTRIBUTOR),session_block(CONTRIBUTOR,auth.CAP_READ))['returncode'],0)
        self.assertFalse(guidance.read(path,['get'],CONTRIBUTOR)['acknowledged'])
        self.assertEqual(self.tracker.writes,[])

    def test_host_and_confined_keys_cannot_enter_service_only_action(self):
        request=self.request('set','host','host-set');request['attachments']={'text':dict(flag='--file',text='forbidden')}
        self.assertNotEqual(endpoint.execute(self.root,request)['returncode'],0)
        for options in (dict(key_projects=[PROJECT]),dict(key_principal='lane:host')):
            with self.subTest(options=options),self.assertRaisesRegex(ValueError,'only to the web service'):
                endpoint.execute(self.root,request,**options)
        self.assertFalse((self.root/'projects'/PROJECT/'GUIDANCE.md').exists())
        self.assertEqual(self.tracker.calls,[])

    def test_capability_and_project_binding_refuse_before_any_tracker_effect(self):
        request=self.request('set',OWNER,'forged');request['attachments']={'text':dict(flag='--file',text='forbidden')}
        for block in (session_block(OWNER,auth.CAP_READ),session_block(OWNER,auth.CAP_COORDINATE,'pq')):
            answer=self.web(request,block);self.assertEqual(answer['returncode'],126,answer)
        self.assertEqual(self.tracker.calls,[])
        self.assertFalse((self.root/'projects'/PROJECT/'.http-operations.sqlite3').exists())

    def test_granted_agent_write_then_revoked_replay_has_no_second_file_effect(self):
        self.grant();request=self.request('set',AGENT,'agent-set');request['attachments']={'text':dict(flag='--file',text='Agent instructions')}
        first=self.web(request,agent_block(auth.CAP_COORDINATE));self.assertEqual(first['returncode'],0,first)
        path=self.root/'projects'/PROJECT;before=(path/'GUIDANCE.md').read_bytes(),(path/'.guidance.json').read_bytes()
        self.state['coordinator_grants'][PROJECT].pop(AGENT);self.save()
        self.assertRefused(self.web(request,agent_block(auth.CAP_COORDINATE)))
        self.assertEqual(before,((path/'GUIDANCE.md').read_bytes(),(path/'.guidance.json').read_bytes()))


class NamedGuidanceClientTests(unittest.TestCase):
    def test_connection_loss_keeps_generated_and_explicit_identity(self):
        from http_client import Client, UncertainOutcome
        client = Client('https://example.invalid')
        for key in (None, 'guidance-explicit-retry'):
            with patch.object(client, 'request', side_effect=OSError('lost')) as request:
                with self.assertRaises(UncertainOutcome) as caught:
                    client.set_guidance('a/b', 'Exact body', key=key)
            used = request.call_args.kwargs['key']
            self.assertTrue(used); self.assertEqual(caught.exception.key, used)
            if key: self.assertEqual(used, key)
            self.assertEqual(request.call_args.args, ('PUT', '/v1/projects/a%2Fb/guidance', {'text': 'Exact body'}))


if __name__ == '__main__': unittest.main()
