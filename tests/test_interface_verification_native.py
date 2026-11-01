"""Pinned real bd + actual host verifier CLI + canonical HTTP compatibility reads."""
import copy
import datetime
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
KIT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(KIT),str(KIT/'tests')]
import test_interfaces_native as fixture
import interface_verification as checks
import reference_records as refs


class NativeVerificationTests(fixture.NativeInterfaceTests):
    test_http_interface_capability_v2_complete_retirement_and_host_read=None

    def test_host_checks_reports_replay_and_live_delegated_trust(self):
        cfg=self.root/'deployment.private.json';saved=cfg.read_bytes();self.addCleanup(cfg.write_bytes,saved)
        self.repo=Path(tempfile.mkdtemp(prefix='interface-schema-',dir=self.root));self.addCleanup(shutil.rmtree,self.repo)
        h=self.harness;verifier='schema-verifier';self.deployment(operators=['ops'],verifiers=[verifier],coordinator_writes=True,independent_approval=False,
            interface_schema_repositories={'area':str(self.repo)})
        self.git('init','-q')
        area=self.root/'projects'/'area';area.mkdir()
        init=subprocess.run([str(self.r.bd_path),'init','--server','--external','--server-host','127.0.0.1',
            '--server-port',str(self.runtime.port),'--server-user','root','--prefix','area','--database','area',
            '--skip-agents','--skip-hooks','--non-interactive'],cwd=area,
            env=fixture.native_fixture.admin.environment(self.root),capture_output=True,text=True,timeout=90)
        self.assertEqual(0,init.returncode,init.stderr)
        self.assertEqual(201,h.request('POST','/v1/projects',{'project_id':'area','name':'Schema source fixture'},token=self.owner).status)
        def native(project,args,actor='ops'):
            return fixture.native_fixture.admin.run_bd(self.root,project,['--actor',actor,*args])
        for project,key,text in [('pp','program.area.area','Area: area\nState: in'),('area','program.member-of','Program: pp\nState: in')]:
            decision=json.loads(native(project,['create','Join schema fixture','--type','decision','--json']))['id']
            body={'schema_version':1,'operation':'draft','operation_id':'link-'+key,'key':key,'title':'Link',
                'statement':text,'authority':{'type':'decision','id':decision},'owner':'person:example',
                'review_by':(datetime.date.today()+datetime.timedelta(days=90)).isoformat(),'tags':[],'decisions':[],
                'acceptance_state':'accepted','acceptance':fixture.acceptance(decision_id=decision)}
            refs.apply_native(body,'ops',lambda args,p=project:native(p,args),self.root/'projects'/project,operator=True,operators=['ops'])
        pointer=self.schema(4,'Initial')
        body=fixture.payload(owner_project='area',schema=pointer)
        for key in ('operation','operation_id','schema_version','revision','expected_sha256'):body.pop(key,None)
        path='/v1/projects/pp/interfaces/iface.quotes.v1'
        first=h.request('POST','/v1/projects/pp/interfaces',body,token=self.owner,key='schema-draft1')
        self.assertEqual(201,first.status,first.data)
        old=h.request('POST',path+'/accept',{'expected_revision':1,'record_sha256':first.data['sha256'],'evidence':'Schema review'},token=self.owner,key='schema-accept1')
        self.assertEqual(200,old.status,old.data)
        body['schema']=self.schema(4,'Equivalent annotation')
        draft=h.request('POST',path+'/revisions',dict(body,revision=3,expected_sha256=old.data['sha256'],compatibility={'rule':'additive-within-major','change':'additive'}),token=self.owner,key='schema-draft2')
        self.assertEqual(200,draft.status,draft.data)
        new=h.request('POST',path+'/accept',{'expected_revision':3,'record_sha256':draft.data['sha256'],'evidence':'Schema review'},token=self.owner,key='schema-accept2')
        self.assertEqual(200,new.status,new.data)
        payload={'operation_id':'native-check','key':'iface.quotes.v1','old_revision':old.data['revision'],
            'old_record_sha256':old.data['sha256'],'new_revision':new.data['revision'],
            'new_record_sha256':new.data['sha256'],'direction':'request','decision_id':None}
        def detail():
            result=h.request('GET',path,token=self.owner);self.assertEqual(200,result.status,result.data);return result.data
        def cli(value,actor=verifier,report=False,claimed='additive'):
            file=self.repo/'payload.json';file.write_text(json.dumps(value))
            argv=[sys.executable,str(KIT/'admin.py'),'--root',str(self.root),'interface-report' if report else 'interface-verify',
                'pp','--actor',actor,'--file',str(file)]
            if report:argv+=['--claimed',claimed]
            return subprocess.run(argv,env=fixture.native_fixture.admin.environment(self.root),capture_output=True,text=True,timeout=180)
        def ok(value,**options):
            result=cli(value,**options);self.assertEqual(0,result.returncode,result.stdout+result.stderr);return json.loads(result.stdout)
        before=native('pp',['export','--all'])
        denied=cli(payload,actor='unlisted');self.assertNotEqual(0,denied.returncode)
        self.assertIn('configured interface verifier',denied.stderr);self.assertEqual(before,native('pp',['export','--all']))
        self.assertEqual('compatibility-unverified',detail()['compatibility'])
        reported=ok(dict(payload,operation_id='contributor-pass'),actor='contributor',report=True)
        self.assertFalse(reported['record']['trusted']);self.assertEqual('compatibility-unverified',detail()['compatibility'])
        result=ok(payload);self.assertEqual('additive',result['record']['result']['classification'])
        self.assertEqual('compatibility-unverified',detail()['compatibility'])
        ok(dict(payload,operation_id='native-response',direction='response'))
        self.assertEqual('additive',detail()['compatibility']);self.assertNotIn('compatibility-unverified',detail()['flags'])
        before=native('pp',['export','--all']);self.assertTrue(ok(payload)['reconciled']);self.assertEqual(before,native('pp',['export','--all']))
        conflict=cli(dict(payload,direction='response'));self.assertNotEqual(0,conflict.returncode);self.assertIn('conflicts',conflict.stderr)
        self.assertEqual(before,native('pp',['export','--all']))
        visible=json.loads(native('pp',['create','Reserved comment guard fixture','--type','task','--json']))['id']
        forged=h.request('POST','/v1/projects/pp/tasks/'+visible+'/comments',
            {'text':checks.PREFIX+'{}'},token=self.owner,key='raw-verify-forgery')
        self.assertEqual(422,forged.status,forged.data);self.assertIn('interface compatibility verification',str(forged.data))
        # A lost native append acknowledgement is an injected direct-module failure,
        # not a process kill. The real CLI retry finds exactly that stored record.
        cut=dict(payload,operation_id='lost-check-ack')
        def lost_ack(args):
            answer=native('pp',args,verifier)
            if args[:2]==['comments','add']:raise RuntimeError('Injected acknowledgement loss')
            return answer
        with self.assertRaisesRegex(RuntimeError,'acknowledgement loss'):
            checks.record(cut,verifier,lost_ack,self.root/'projects'/'pp',operators=['ops'],verifiers=[verifier],repository=self.repo,trusted=True)
        before=native('pp',['export','--all']);self.assertTrue(ok(cut)['reconciled']);self.assertEqual(before,native('pp',['export','--all']))
        # An agent needs BOTH explicit configured verifier binding AND live interfaces delegation.
        agent=h.request('POST','/v1/agents',{'name':'Schema verifier','working_directory':'/synthetic/schema-verifier',
            'projects':['pp','area'],'scopes':['tasks','records']},token=self.owner)
        self.assertEqual(201,agent.status,agent.data);aid=agent.data['agent']['id']
        self.assertEqual(200,h.request('PUT','/v1/projects/pp/agents/'+aid+'/coordinator',{},token=self.owner).status)
        binding_issuer='agent-schema-verifier'
        def agent_configuration(bind=True):
            self.deployment(operators=['ops'],verifiers=[binding_issuer],coordinator_writes=True,independent_approval=False,
                interface_schema_repositories={'area':str(self.repo)},interface_verifier_bindings={aid:binding_issuer} if bind else {})
        agent_configuration()
        self.assertEqual('compatibility-unverified',detail()['compatibility'])
        delegation={'action':'grant','decision':None,'title':'Schema delegation','right':'record-acceptance','grantee':aid,
            'scope':['interfaces'],'escalate':'Everything beyond this check.','reason':'Synthetic schema verification.'}
        granted=h.request('POST','/v1/projects/pp/delegations',delegation,token=self.owner,key='schema-delegation')
        self.assertEqual(201,granted.status,granted.data)
        ap=dict(payload,operation_id='agent-check',decision_id=granted.data['decision'])
        agent_token=agent.data['credential']['secret']
        def web(value,key,token=agent_token):
            body={k:v for k,v in value.items() if k not in ('key','operation_id')}
            return h.request('POST',path+'/verify',body,token=token,key=key)
        # HTTP identities remain reserved to the service, even with valid config/state.
        before=native('pp',['export','--all']);forbidden_host=cli(ap,actor=aid)
        self.assertNotEqual(0,forbidden_host.returncode);self.assertEqual(before,native('pp',['export','--all']))
        # A hand-edited legacy allowlist still cannot turn --actor into an HTTP
        # impersonation route, for trusted checks or ordinary reported claims.
        self.deployment(operators=['ops'],verifiers=[binding_issuer,aid],coordinator_writes=True,independent_approval=False,
            interface_schema_repositories={'area':str(self.repo)},interface_verifier_bindings={aid:binding_issuer})
        for report in (False,True):
            refused=cli(ap,actor=aid,report=report)
            self.assertNotEqual(0,refused.returncode);self.assertIn('only the HTTP service acts under',refused.stderr)
            self.assertEqual(before,native('pp',['export','--all']))
        agent_configuration()
        no_binding=web(ap,'unconfigured-person',token=self.owner)
        self.assertEqual(403,no_binding.status,no_binding.data);self.assertEqual(before,native('pp',['export','--all']))
        checked=web(ap,'agent-check');self.assertEqual(200,checked.status,checked.data)
        checked=web(dict(ap,direction='response'),'agent-response');self.assertEqual(200,checked.status,checked.data)
        self.assertEqual('additive',detail()['compatibility'])
        before=native('pp',['export','--all']);replayed=web(ap,'agent-check')
        self.assertEqual(200,replayed.status,replayed.data);self.assertEqual(before,native('pp',['export','--all']))
        agent_configuration(False);self.assertEqual('compatibility-unverified',detail()['compatibility'])
        before=native('pp',['export','--all']);unbound=web(ap,'without-binding')
        self.assertEqual(403,unbound.status,unbound.data);self.assertEqual(before,native('pp',['export','--all']))
        agent_configuration();self.assertEqual('additive',detail()['compatibility'])
        import delegations
        at=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        revoke={'schema_version':1,'operation_id':'native-verifier-revoke','decision':ap['decision_id'],'title':None,
            'action':'revoke','right':'record-acceptance','grantee':aid,'scope':[],'escalate':'','reason':'Synthetic verifier revocation.'}
        revoked=delegations.apply(revoke,project='pp',journal=self.root/'projects'/'pp',run=lambda args:native('pp',args),
            recorded_by='ops',via='operator',account=None,at=at)
        self.assertEqual('compatibility-unverified',detail()['compatibility'])
        before=native('pp',['export','--all']);replay_denied=web(ap,'agent-check')
        self.assertEqual(403,replay_denied.status,replay_denied.data);self.assertEqual(before,native('pp',['export','--all']))
        receipt=self.root/'projects'/'pp'/delegations.JOURNAL_DIR/(revoked['record']['sha256']+'.json');receipt.unlink()
        self.assertEqual('compatibility-unverified',detail()['compatibility'])
        replay_denied=web(ap,'agent-check');self.assertEqual(403,replay_denied.status,replay_denied.data)
        self.assertEqual(before,native('pp',['export','--all']))
        # Explicit fresh owner decision recovers; the damaged decision remains inert.
        granted=h.request('POST','/v1/projects/pp/delegations',delegation,token=self.owner,key='schema-fresh-delegation')
        self.assertEqual(201,granted.status,granted.data)
        fresh=dict(ap,decision_id=granted.data['decision'])
        checked=web(fresh,'agent-fresh-request');self.assertEqual(200,checked.status,checked.data)
        checked=web(dict(fresh,direction='response'),'agent-fresh-response');self.assertEqual(200,checked.status,checked.data)
        self.assertEqual('additive',detail()['compatibility'])
        removed=h.request('DELETE','/v1/projects/pp/agents/'+aid+'/coordinator',token=self.owner)
        self.assertEqual(200,removed.status,removed.data)
        self.assertEqual('compatibility-unverified',detail()['compatibility'])
        before=native('pp',['export','--all']);denied=web(fresh,'agent-fresh-request')
        self.assertEqual(403,denied.status,denied.data);self.assertEqual(before,native('pp',['export','--all']))
        # Real breaking source bytes, then a reported pass: drift remains visible.
        self.deployment(operators=['ops'],verifiers=[verifier],coordinator_writes=True,independent_approval=False,
            interface_schema_repositories={'area':str(self.repo)},interface_verifier_bindings={})
        body['schema']=self.schema(3,'Breaking length restriction')
        draft=h.request('POST',path+'/revisions',dict(body,revision=5,expected_sha256=new.data['sha256'],compatibility={'rule':'additive-within-major','change':'additive'}),token=self.owner,key='breaking-draft')
        self.assertEqual(200,draft.status,draft.data)
        accepted=h.request('POST',path+'/accept',{'expected_revision':5,'record_sha256':draft.data['sha256'],'evidence':'Verifier will check the declared additive claim'},token=self.owner,key='breaking-accept')
        self.assertEqual(200,accepted.status,accepted.data)
        bp=dict(payload,operation_id='breaking-check',old_revision=new.data['revision'],old_record_sha256=new.data['sha256'],
            new_revision=accepted.data['revision'],new_record_sha256=accepted.data['sha256'])
        actual=ok(bp);self.assertEqual('breaking',actual['record']['result']['classification'])
        self.assertEqual('breaking-without-version',detail()['compatibility'])
        ok(dict(bp,operation_id='reported-break-pass'),actor='contributor',report=True)
        self.assertEqual('breaking-without-version',detail()['compatibility'])
        self.assert_contributor_http_report()

    def assert_contributor_http_report(self):
        h=self.harness;path='/v1/projects/pp/interfaces/iface.quotes.v1'
        def exported():return fixture.native_fixture.admin.run_bd(self.root,'pp',['--actor','ops','export','--all'])
        current=h.request('GET',path,token=self.owner);self.assertEqual(200,current.status,current.data)
        self.assertEqual('breaking-without-version',current.data['compatibility'])
        target=current.data['record']
        checks=[item['record'] for item in current.data['interface_verifications']
            if item['record']['trusted'] and item['record']['new_record_sha256']==target['sha256']]
        self.assertTrue(checks);check=checks[-1]
        body={key:check[key] for key in ('old_revision','old_record_sha256','new_revision','new_record_sha256','direction')}
        body.update(decision_id=None,claimed='additive')
        uid=h.create_account(self.owner,'supplemental-reporter','synthetic-report-password')
        membership=h.request('PUT','/v1/projects/pp/members/'+uid,{'role':'contributor'},token=self.owner)
        self.assertEqual(200,membership.status,membership.data)
        token=h.login('supplemental-reporter','synthetic-report-password')[0]
        answer=h.request('POST',path+'/report',body,token=token,key='supplemental-http-report')
        self.assertEqual(200,answer.status,answer.data)
        value=answer.data['record'];self.assertFalse(value['trusted'])
        self.assertEqual('additive',value['claimed']);self.assertEqual('unknown',value['result']['classification'])
        self.assertIsNone(value['old_source']);self.assertIsNone(value['new_source'])
        after=h.request('GET',path,token=self.owner);self.assertEqual(200,after.status,after.data)
        self.assertEqual('breaking-without-version',after.data['compatibility'])
        before=exported();replay=h.request('POST',path+'/report',body,token=token,key='supplemental-http-report')
        self.assertEqual(200,replay.status,replay.data);self.assertEqual(value,replay.data['record'])
        self.assertEqual(before,exported())
        changed=h.request('POST',path+'/report',dict(body,claimed='breaking'),token=token,key='supplemental-http-report')
        self.assertEqual(409,changed.status,changed.data);self.assertEqual(before,exported())
        forged=h.request('POST',path+'/report',dict(body,trusted=True),token=token,key='supplemental-forged-report')
        self.assertEqual(422,forged.status,forged.data);self.assertEqual(before,exported())
        # Membership of the program grants no mutation right in its area.
        import program_isolation
        for suffix in ('verify','report'):
            request=body if suffix=='report' else {k:v for k,v in body.items() if k!='claimed'}
            answers=[]
            before_area=fixture.native_fixture.admin.run_bd(self.root,'area',['--actor','ops','export','--all'])
            before_program=exported()
            for project in ('area','absentarea'):
                response=h.request('POST','/v1/projects/'+project+'/interfaces/iface.quotes.v1/'+suffix,
                    request,token=token,key='outsider-'+project+'-'+suffix)
                self.assertEqual(404,response.status,response.data)
                answers.append(program_isolation.without_request_id(response.data))
            self.assertEqual(answers[0],answers[1])
            self.assertEqual(before_area,fixture.native_fixture.admin.run_bd(self.root,'area',['--actor','ops','export','--all']))
            self.assertEqual(before_program,exported())
        demote=h.request('PUT','/v1/projects/pp/members/'+uid,{'role':'viewer'},token=self.owner)
        self.assertEqual(200,demote.status,demote.data)
        before=exported();revoked=h.request('POST',path+'/report',body,token=token,key='supplemental-http-report')
        self.assertEqual(403,revoked.status,revoked.data);self.assertNotIn('record',revoked.data)
        self.assertEqual(before,exported())

    def git(self,*args):
        return subprocess.check_output(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',*args],cwd=self.repo,stderr=subprocess.DEVNULL)

    def schema(self,maximum,message):
        value={'$schema':'https://json-schema.org/draft/2020-12/schema','type':'string','maxLength':maximum,'description':message}
        data=json.dumps(value,separators=(',',':')).encode();(self.repo/'schema.json').write_bytes(data)
        self.git('add','.');self.git('commit','-qm',message)
        return {'type':'repo-path','path':'schema.json','commit':self.git('rev-parse','HEAD').strip().decode(),
            'anchor':None,'media_type':'application/schema+json','sha256':hashlib.sha256(data).hexdigest()}
