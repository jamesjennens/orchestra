"""Actual HTTP service + canonical endpoint + pinned bd interface/capability workflow."""
import datetime
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import test_program_links as fixture
import test_bd_label_aliases as native_fixture
import reference_records as refs
from test_reference_records import acceptance
from test_interfaces import payload
from test_web_records import _valid_cap_propose


@unittest.skipIf(native_fixture.endpoint is None,'native endpoint is POSIX-only')
@unittest.skipIf(native_fixture.BD is None,'requires pinned ORCHESTRA_BD_BIN')
class NativeInterfaceTests(fixture.NativeLinkTests):
    # Do not rerun the inherited unrelated native-link test.
    test_native_a_program_link_from_proposal_to_the_read=None

    def test_http_interface_capability_v2_complete_retirement_and_host_read(self):
        self.deployment(operators=['ops'],coordinator_writes=True,independent_approval=False)
        h=self.harness
        area=self.root/'projects'/'area';area.mkdir()
        init=subprocess.run([str(self.r.bd_path),'init','--server','--external','--server-host','127.0.0.1',
             '--server-port',str(self.runtime.port),'--server-user','root','--prefix','area','--database','area',
             '--skip-agents','--skip-hooks','--non-interactive'],cwd=area,env=native_fixture.admin.environment(self.root),capture_output=True,text=True,timeout=90)
        self.assertEqual(0,init.returncode,init.stderr)
        made=h.request('POST','/v1/projects',{'project_id':'area','name':'Synthetic area'},token=self.owner)
        self.assertEqual(201,made.status,made.data)
        for project,key,text in [('pp','program.area.area','Area: area\nState: in'),
                                 ('area','program.member-of','Program: pp\nState: in')]:
            run=lambda args,project=project:native_fixture.admin.run_bd(self.root,project,['--actor','ops',*args])
            decision=json.loads(run(['create','Join program','--type','decision','--json']))['id']
            body={'schema_version':1,'operation':'draft','operation_id':'native-'+key,'key':key,
                  'title':'Synthetic link','statement':text,'authority':{'type':'decision','id':decision},
                  'owner':'person:maintainer','review_by':(datetime.date.today()+datetime.timedelta(days=90)).isoformat(),
                  'tags':[],'decisions':[],'acceptance_state':'accepted','acceptance':acceptance(decision_id=decision)}
            refs.apply_native(body,'ops',run,self.root/'projects'/project,operator=True,operators=['ops'])
        body=payload(owner_project='area')
        for key in ('operation','operation_id','schema_version','revision','expected_sha256'):body.pop(key,None)
        proposed=h.request('POST','/v1/projects/pp/interfaces',body,token=self.owner,key='native-interface')
        self.assertEqual(201,proposed.status,proposed.data)
        accept={'expected_revision':1,'record_sha256':proposed.data['sha256'],'evidence':'Synthetic review'}
        selected=h.request('POST','/v1/projects/pp/interfaces/iface.quotes.v1/accept',accept,token=self.owner,key='native-interface-accept')
        self.assertEqual(200,selected.status,selected.data)
        read=h.request('GET','/v1/projects/pp/interfaces',token=self.owner)
        self.assertEqual(200,read.status,read.data)
        self.assertTrue(read.data['program_coverage']['complete'])
        self.assertEqual('compatibility-unverified',read.data['items'][0]['compatibility'])
        record=read.data['items'][0]['record']
        import http_client
        sdk=http_client.Client('http://127.0.0.1:%d'%h.port).use_credential(self.owner)
        self.assertEqual(record['sha256'],sdk.get_interface('pp','iface.quotes.v1')['record']['sha256'])
        self.assertEqual(1,len(sdk.list_interfaces('pp',limit=1)['items']))
        # A host key can read its project, but cannot forge the service's readable set.
        with self.assertRaisesRegex(ValueError,'web service'):
            native_fixture.endpoint.execute(self.root,{'project':'pp','actor':'worker','action':'interface',
                'args':['list','--readable','area']},key_projects=frozenset({'pp'}))

        # Interface G1 scope is explicit: an old references grant cannot accept it.
        agent=h.request('POST','/v1/agents',{'name':'Interface reviewer','working_directory':'/synthetic/reviewer',
            'projects':['pp','area'],'scopes':['tasks','records']},token=self.owner)
        self.assertEqual(201,agent.status,agent.data)
        aid=agent.data['agent']['id'];agent_token=agent.data['credential']['secret']
        self.assertEqual(200,h.request('PUT','/v1/projects/pp/agents/'+aid+'/coordinator',{},token=self.owner).status)
        alternate=dict(body,key='iface.other.v1',name='Other interface')
        draft=h.request('POST','/v1/projects/pp/interfaces',alternate,token=self.owner,key='agent-interface-draft')
        self.assertEqual(201,draft.status,draft.data)
        accept_agent={'expected_revision':1,'record_sha256':draft.data['sha256'],'evidence':'Synthetic delegated review'}
        path='/v1/projects/pp/interfaces/iface.other.v1/accept'
        before_agent=self.r.bd('export','--all').stdout
        denied=h.request('POST',path,accept_agent,token=agent_token,key='interface-no-delegation')
        self.assertEqual(403,denied.status,denied.data)
        self.assertEqual(before_agent,self.r.bd('export','--all').stdout)
        for scope,wanted in [(['references'],403),(['interfaces'],200)]:
            delegation={'action':'grant','decision':None,'title':'Synthetic '+scope[0]+' delegation',
                'right':'record-acceptance','grantee':aid,'scope':scope,'escalate':'Anything outside this synthetic test',
                'reason':'Synthetic explicit kind scope'}
            grant=h.request('POST','/v1/projects/pp/delegations',delegation,token=self.owner,key='scope-'+scope[0])
            self.assertEqual(201,grant.status,grant.data)
            result=h.request('POST',path,dict(accept_agent,decision_id=grant.data['decision']),token=agent_token,key='interface-delegation-'+scope[0])
            self.assertEqual(wanted,result.status,result.data)
            if wanted==403:
                # The only new native writes belong to the delegation, not interface acceptance.
                self.assertEqual('draft',h.request('GET','/v1/projects/pp/interfaces/iface.other.v1',token=self.owner).data['lifecycle'])

        cap=_valid_cap_propose('synthetic.consumer')
        cap['interfaces']=[{'iface':'iface:pp/iface.quotes.v1@2','role':'consumes'}]
        created=h.request('POST','/v1/projects/area/capabilities',cap,token=self.owner,key='native-consumer')
        self.assertEqual(201,created.status,created.data)
        accepted=h.request('POST','/v1/projects/area/capabilities/synthetic.consumer/accept',
            {'expected_revision':1,'record_sha256':created.data['sha256'],'evidence':'Synthetic consumer',
             'unverified_pointer_reason':'Synthetic test pointers; no checkout verification claimed'},
            token=self.owner,key='native-consumer-accept')
        self.assertEqual(200,accepted.status,accepted.data)
        # Retirement is a state-only draft reviewed through the normal accept route.
        retirement=dict(body,revision=3,expected_sha256=record['sha256'],state='retired')
        revised=h.request('POST','/v1/projects/pp/interfaces/iface.quotes.v1/revisions',retirement,
                          token=self.owner,key='native-retire-draft')
        self.assertEqual(200,revised.status,revised.data)
        before=self.r.bd('export','--all').stdout
        refused=h.request('POST','/v1/projects/pp/interfaces/iface.quotes.v1/accept',
                          {'expected_revision':3,'record_sha256':revised.data['sha256'],'evidence':'retire'},
                          token=self.owner,key='native-retire-consumer')
        self.assertEqual(409,refused.status,refused.data)
        self.assertIn('current consumers',refused.data['error']['message'])
        self.assertEqual(before,self.r.bd('export','--all').stdout)
        # A program coordinator who cannot read the linked area cannot make the complete-view claim.
        uid=h.create_account(self.owner,'iface-coord','iface-password-1')
        self.assertEqual(200,h.request('PUT','/v1/projects/pp/members/'+uid,{'role':'coordinator'},token=self.owner).status)
        limited=h.login('iface-coord','iface-password-1')[0]
        unauthorized=h.request('POST','/v1/projects/pp/interfaces',dict(body,key='iface.unauthorized.v1'),
                               token=limited,key='native-source-coordination-refused')
        self.assertEqual(409,unauthorized.status,unauthorized.data)
        self.assertIn('coordination',unauthorized.data['error']['message'])
        self.assertEqual(before,self.r.bd('export','--all').stdout)
        hidden=h.request('POST','/v1/projects/pp/interfaces/iface.quotes.v1/accept',
                         {'expected_revision':3,'record_sha256':revised.data['sha256'],'evidence':'hidden'},
                         token=limited,key='native-retire-hidden')
        self.assertEqual(409,hidden.status,hidden.data)
        self.assertEqual(before,self.r.bd('export','--all').stdout)
        # Removing the consumer through a v2 empty declaration allows complete retirement.
        capread=h.request('GET','/v1/projects/area/capabilities/synthetic.consumer',token=self.owner)
        current=capread.data['record']
        withdrawn=dict(cap,revision=current['revision']+1,expected_sha256=current['sha256'],interfaces=[])
        change=h.request('POST','/v1/projects/area/capabilities/synthetic.consumer/revisions',withdrawn,
                         token=self.owner,key='native-consumer-withdraw')
        self.assertEqual(200,change.status,change.data)
        accepted=h.request('POST','/v1/projects/area/capabilities/synthetic.consumer/accept',
             {'expected_revision':change.data['revision'],'record_sha256':change.data['sha256'],
              'evidence':'No consumer remains','unverified_pointer_reason':'Synthetic pointers'},
             token=self.owner,key='native-consumer-withdraw-accept')
        self.assertEqual(200,accepted.status,accepted.data)
        final=h.request('POST','/v1/projects/pp/interfaces/iface.quotes.v1/accept',
                        {'expected_revision':3,'record_sha256':revised.data['sha256'],'evidence':'complete'},
                        token=self.owner,key='native-retire-complete')
        self.assertEqual(200,final.status,final.data)
        self.assertEqual('retired',h.request('GET','/v1/projects/pp/interfaces/iface.quotes.v1',token=self.owner).data['lifecycle'])

        # The configured host operator route shares the native kind and F3 checks.
        decision=json.loads(self.r.bd('create','Synthetic operator interface review','--type','decision','--json').stdout)['id']
        operator_body=payload(operation='draft',operation_id='native-operator-interface',key='iface.operator.v1',
                              acceptance_state='accepted',acceptance=acceptance(decision_id=decision))
        file=self.root/'operator-interface.json';file.write_text(json.dumps(operator_body))
        command=[sys.executable,str(Path(__file__).resolve().parents[1]/'admin.py'),'--root',str(self.root),
                 'interface-apply','pp','--actor','ops','--file',str(file)]
        result=subprocess.run(command,env=native_fixture.admin.environment(self.root),capture_output=True,text=True,timeout=90)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual('accepted',json.loads(result.stdout)['lifecycle'])
        before_retry=self.r.bd('export','--all').stdout
        retry=subprocess.run(command,env=native_fixture.admin.environment(self.root),capture_output=True,text=True,timeout=90)
        self.assertEqual(0,retry.returncode,retry.stderr)
        self.assertEqual(before_retry,self.r.bd('export','--all').stdout)


class NativeInterfaceAuthorityTests(NativeInterfaceTests):
    test_http_interface_capability_v2_complete_retirement_and_host_read=None

    def test_http_accept_rechecks_live_stored_proposer_and_blocks_payload_owner_takeover(self):
        self.deployment(operators=['ops'],coordinator_writes=True,independent_approval=False)
        h=self.harness
        area=self.root/'projects'/'area';area.mkdir()
        init=subprocess.run([str(self.r.bd_path),'init','--server','--external','--server-host','127.0.0.1',
             '--server-port',str(self.runtime.port),'--server-user','root','--prefix','area','--database','area',
             '--skip-agents','--skip-hooks','--non-interactive'],cwd=area,env=native_fixture.admin.environment(self.root),capture_output=True,text=True,timeout=90)
        self.assertEqual(0,init.returncode,init.stderr)
        made=h.request('POST','/v1/projects',{'project_id':'area','name':'Synthetic area'},token=self.owner)
        self.assertEqual(201,made.status,made.data)
        for project,key,text in [('pp','program.area.area','Area: area\nState: in'),
                                 ('area','program.member-of','Program: pp\nState: in')]:
            run=lambda args,project=project:native_fixture.admin.run_bd(self.root,project,['--actor','ops',*args])
            decision=json.loads(run(['create','Join program','--type','decision','--json']))['id']
            body={'schema_version':1,'operation':'draft','operation_id':'native-'+key,'key':key,
                  'title':'Synthetic link','statement':text,'authority':{'type':'decision','id':decision},
                  'owner':'person:maintainer','review_by':(datetime.date.today()+datetime.timedelta(days=90)).isoformat(),
                  'tags':[],'decisions':[],'acceptance_state':'accepted','acceptance':acceptance(decision_id=decision)}
            refs.apply_native(body,'ops',run,self.root/'projects'/project,operator=True,operators=['ops'])
        body=payload(owner_project='area')
        for key in ('operation','operation_id','schema_version','revision','expected_sha256'):body.pop(key,None)

        uid=h.create_account(self.owner,'stored-proposer','stored-password-1')
        for project in ('pp','area'):
            self.assertEqual(200,h.request('PUT','/v1/projects/'+project+'/members/'+uid,
                {'role':'coordinator'},token=self.owner).status)
        token=h.login('stored-proposer','stored-password-1')[0]
        proposed=h.request('POST','/v1/projects/pp/interfaces',body,token=token,key='live-stored-proposer')
        self.assertEqual(201,proposed.status,proposed.data)
        before=h.request('GET','/v1/projects/pp/interfaces/iface.quotes.v1',token=self.owner).data
        stolen=dict(body,revision=2,expected_sha256=proposed.data['sha256'],owner_project='pp')
        refused=h.request('POST','/v1/projects/pp/interfaces/iface.quotes.v1/revisions',stolen,
            token=self.owner,key='take-over-owner')
        self.assertEqual(409,refused.status,refused.data)
        after=h.request('GET','/v1/projects/pp/interfaces/iface.quotes.v1',token=self.owner).data
        self.assertEqual(before['proposed'],after['proposed'])
        self.assertEqual(200,h.request('PUT','/v1/projects/area/members/'+uid,
            {'role':'contributor'},token=self.owner).status)
        accept={'expected_revision':1,'record_sha256':proposed.data['sha256'],'evidence':'live check'}
        refused=h.request('POST','/v1/projects/pp/interfaces/iface.quotes.v1/accept',accept,
            token=self.owner,key='demoted-source-proposer')
        self.assertEqual(409,refused.status,refused.data)
        self.assertIn('proposer',str(refused.data))
        self.assertEqual('draft',h.request('GET','/v1/projects/pp/interfaces/iface.quotes.v1',token=self.owner).data['lifecycle'])
        self.assertEqual(200,h.request('PUT','/v1/projects/area/members/'+uid,
            {'role':'coordinator'},token=self.owner).status)
        accepted=h.request('POST','/v1/projects/pp/interfaces/iface.quotes.v1/accept',accept,
            token=self.owner,key='restored-source-proposer')
        self.assertEqual(200,accepted.status,accepted.data)


class NativeInterfaceConcurrencyTests(NativeInterfaceTests):
    test_http_interface_capability_v2_complete_retirement_and_host_read=None

    def test_http_interface_and_v1_capability_routes_finish_in_seconds_in_both_orders(self):
        self.deployment(operators=['ops'],coordinator_writes=True,independent_approval=False)
        h=self.harness
        area=self.root/'projects'/'area';area.mkdir()
        init=subprocess.run([str(self.r.bd_path),'init','--server','--external','--server-host','127.0.0.1',
             '--server-port',str(self.runtime.port),'--server-user','root','--prefix','area','--database','area',
             '--skip-agents','--skip-hooks','--non-interactive'],cwd=area,env=native_fixture.admin.environment(self.root),capture_output=True,text=True,timeout=90)
        self.assertEqual(0,init.returncode,init.stderr)
        made=h.request('POST','/v1/projects',{'project_id':'area','name':'Synthetic area'},token=self.owner)
        self.assertEqual(201,made.status,made.data)
        for project,key,text in [('pp','program.area.area','Area: area\nState: in'),
                                 ('area','program.member-of','Program: pp\nState: in')]:
            run=lambda args,project=project:native_fixture.admin.run_bd(self.root,project,['--actor','ops',*args])
            decision=json.loads(run(['create','Join program','--type','decision','--json']))['id']
            body={'schema_version':1,'operation':'draft','operation_id':'native-'+key,'key':key,
                  'title':'Synthetic link','statement':text,'authority':{'type':'decision','id':decision},
                  'owner':'person:maintainer','review_by':(datetime.date.today()+datetime.timedelta(days=90)).isoformat(),
                  'tags':[],'decisions':[],'acceptance_state':'accepted','acceptance':acceptance(decision_id=decision)}
            refs.apply_native(body,'ops',run,self.root/'projects'/project,operator=True,operators=['ops'])
        body=payload(owner_project='area')
        for key in ('operation','operation_id','schema_version','revision','expected_sha256'):body.pop(key,None)

        import concurrent.futures,time,threading
        for number,first in enumerate(('interface','capability')):
            with self.subTest(first=first):
                interface_body=dict(body,key='iface.concurrent%d.v1'%number)
                cap_body=_valid_cap_propose('synthetic.concurrent%d'%number)
                self.assertNotIn('interfaces',cap_body)
                started=threading.Event()
                def post(kind):
                    if kind==first:started.set()
                    if kind=='interface':
                        return h.request('POST','/v1/projects/pp/interfaces',interface_body,
                            token=self.owner,key='concurrent-interface-'+str(number))
                    return h.request('POST','/v1/projects/area/capabilities',cap_body,
                        token=self.owner,key='concurrent-capability-'+str(number))
                before=time.monotonic()
                with concurrent.futures.ThreadPoolExecutor(2) as pool:
                    one=pool.submit(post,first);self.assertTrue(started.wait(2))
                    two=pool.submit(post,'capability' if first=='interface' else 'interface')
                    responses=[one.result(timeout=15),two.result(timeout=15)]
                self.assertLess(time.monotonic()-before,15,'the two routes formed a lock cycle')
                for response in responses:self.assertEqual(201,response.status,response.data)
