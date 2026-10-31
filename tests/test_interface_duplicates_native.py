"""Real bd: duplicate release, hidden inert consumers, and safe repair controls."""
import datetime
import json
import subprocess
import sys
from pathlib import Path

KIT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(KIT),str(KIT/'tests')]
import test_interfaces_native as fixture
import capability_records as caps
import reference_records as refs
import keyed_records
from test_capability_records import entry as cap_entry


class NativeDuplicateInterfaceTests(fixture.NativeInterfaceTests):
    # Own runtime; do not duplicate the inherited interface/link workflow tests.
    test_http_interface_capability_v2_complete_retirement_and_host_read=None

    def test_duplicate_anchor_refusals_and_safe_operator_repair(self):
        self.deployment(operators=['ops','ops2'],coordinator_writes=True,independent_approval=False)
        h=self.harness
        area=self.root/'projects'/'area';area.mkdir()
        init=subprocess.run([str(self.r.bd_path),'init','--server','--external','--server-host','127.0.0.1',
            '--server-port',str(self.runtime.port),'--server-user','root','--prefix','area','--database','area',
            '--skip-agents','--skip-hooks','--non-interactive'],cwd=area,
            env=fixture.native_fixture.admin.environment(self.root),capture_output=True,text=True,timeout=90)
        self.assertEqual(0,init.returncode,init.stderr)
        made=h.request('POST','/v1/projects',{'project_id':'area','name':'Duplicate consumer fixture'},token=self.owner)
        self.assertEqual(201,made.status,made.data)

        def run(project,args,actor='ops'):
            return fixture.native_fixture.admin.run_bd(self.root,project,['--actor',actor,*args])

        def decision(project,title):
            return json.loads(run(project,['create',title,'--type','decision','--json']))['id']

        for project,key,text in [('pp','program.area.area','Area: area\nState: in'),
                                 ('area','program.member-of','Program: pp\nState: in')]:
            dec=decision(project,'Join duplicate fixture')
            body={'schema_version':1,'operation':'draft','operation_id':'link-'+key,'key':key,'title':'Link',
                  'statement':text,'authority':{'type':'decision','id':dec},'owner':'person:example',
                  'review_by':(datetime.date.today()+datetime.timedelta(days=90)).isoformat(),
                  'tags':[],'decisions':[],'acceptance_state':'accepted',
                  'acceptance':fixture.acceptance(decision_id=dec)}
            refs.apply_native(body,'ops',lambda args,p=project:run(p,args),self.root/'projects'/project,
                              operator=True,operators=['ops','ops2'])

        def graph(key):
            listed=h.request('GET','/v1/projects/pp/interfaces',token=self.owner)
            cap=h.request('GET','/v1/projects/area/capabilities/'+key,token=self.owner)
            self.assertEqual(200,listed.status,listed.data);self.assertEqual(200,cap.status,cap.data)
            return listed.data['program_coverage']['complete'],cap.data

        def release(issue,reason):
            return subprocess.run([sys.executable,str(KIT/'admin.py'),'--root',str(self.root),
                'anchor-release','area','--kind','capability','--issue-id',issue,'--actor','ops',
                '--reason',reason,'--duplicate','--set-aside-evidence'],
                env=fixture.native_fixture.admin.environment(self.root),capture_output=True,text=True,timeout=180)

        for case in ('release','equivalent','demotion'):
            with self.subTest(case=case):
                self.deployment(operators=['ops','ops2'],coordinator_writes=True,independent_approval=False)
                key='iface.'+case+'.v1';capkey='dp.'+case
                body=fixture.payload(key=key,owner_project='area')
                for field in ('operation','operation_id','schema_version','revision','expected_sha256'):
                    body.pop(field,None)
                draft=h.request('POST','/v1/projects/pp/interfaces',body,token=self.owner,key='iface-'+case)
                self.assertEqual(201,draft.status,draft.data)
                accepted=h.request('POST','/v1/projects/pp/interfaces/'+key+'/accept',
                    {'expected_revision':1,'record_sha256':draft.data['sha256'],'evidence':'Synthetic review'},
                    token=self.owner,key='iface-accept-'+case)
                self.assertEqual(200,accepted.status,accepted.data)
                cap_actor='ops' if case=='release' else 'ops2'
                dec=decision('area','Consumer '+case)
                declaration=[{'iface':'iface:pp/'+key+'@2','role':'consumes'}]
                cap=cap_entry(key=capkey,operation='draft',operation_id='consumer-'+case,
                    interfaces=declaration,acceptance_state='accepted',acceptance=fixture.acceptance(decision_id=dec))
                a=caps.apply_native(cap,cap_actor,lambda args:run('area',args,cap_actor),area,
                                    operator=True,operators=['ops','ops2'])
                # Plant only the duplicate through native bd: exact record + bound F3.
                # This is the crash/legacy shape, not a contributor endpoint bypass.
                b=json.loads(run('area',['create','Duplicate '+case,'--type','task','--labels',
                    ','.join(['capability','capability:accepted',caps.KIND.key_label(capkey)]),'--json']))['id']
                other=cap_entry(key=capkey)
                if case=='equivalent':other['interfaces']=declaration
                record=caps.entry_record(other,1,'accepted')
                bound=keyed_records.bind_acceptance(fixture.acceptance(decision_id=dec),record)
                _evidence,comment=caps.KIND.acceptance_evidence(bound,b,1,record,'ops')
                run('area',['comments','add',b,caps.KIND.entry_comment(record),'--json'])
                run('area',['comments','add',b,comment,'--json'])
                run('area',['update',b,'--status','closed','--json'])
                retirement=h.request('POST','/v1/projects/pp/interfaces/'+key+'/revisions',
                    dict(body,revision=3,expected_sha256=accepted.data['sha256'],state='retired'),
                    token=self.owner,key='retire-draft-'+case)
                self.assertEqual(200,retirement.status,retirement.data)
                accept={'expected_revision':3,'record_sha256':retirement.data['sha256'],'evidence':'Retirement review'}
                route='/v1/projects/pp/interfaces/'+key+'/accept'
                complete,entry=graph(capkey)
                self.assertFalse(complete);self.assertEqual('conflicted',entry['state'])
                denied=h.request('POST',route,accept,token=self.owner,key='before-'+case)
                self.assertEqual(409,denied.status,denied.data)

                if case=='release':
                    before=run('area',['export','--all'])
                    failed=release(a['native_id'],'Attempt to discard the consumer')
                    self.assertNotEqual(0,failed.returncode)
                    self.assertIn('retained approved interface declaration',failed.stderr)
                    self.assertEqual(before,run('area',['export','--all']))
                    complete,_=graph(capkey);self.assertFalse(complete)
                    safe=release(b,'Keep the approved declaration')
                    self.assertEqual(0,safe.returncode,safe.stdout+safe.stderr)
                    complete,_=graph(capkey);self.assertTrue(complete)
                    denied=h.request('POST',route,accept,token=self.owner,key='after-safe-'+case)
                    self.assertEqual(409,denied.status,denied.data)
                    self.assertIn('current consumers',denied.data['error']['message'])
                    # The documented recovery: key unique, normal reviewed withdrawal.
                    current=h.request('GET','/v1/projects/area/capabilities/'+capkey,token=self.owner).data['record']
                    withdrawal={k:v for k,v in cap.items() if k not in
                        ('operation','operation_id','schema_version','revision','expected_sha256','acceptance_state','acceptance')}
                    withdrawal.update(revision=current['revision']+1,expected_sha256=current['sha256'],interfaces=[])
                    revised=h.request('POST','/v1/projects/area/capabilities/'+capkey+'/revisions',withdrawal,
                                      token=self.owner,key='withdraw-draft-'+case)
                    self.assertEqual(200,revised.status,revised.data)
                    withdrawn=h.request('POST','/v1/projects/area/capabilities/'+capkey+'/accept',
                        {'expected_revision':revised.data['revision'],'record_sha256':revised.data['sha256'],
                         'evidence':'Explicit reviewed withdrawal','unverified_pointer_reason':'Synthetic data; no checkout claim'},
                        token=self.owner,key='withdraw-accept-'+case)
                    self.assertEqual(200,withdrawn.status,withdrawn.data)
                    allowed=h.request('POST',route,accept,token=self.owner,key='after-withdraw-'+case)
                    self.assertEqual(200,allowed.status,allowed.data)
                    detail=h.request('GET','/v1/projects/pp/interfaces/'+key,token=self.owner)
                    self.assertEqual('retired',detail.data['lifecycle'])
                else:
                    self.deployment(operators=['ops'],coordinator_writes=True,independent_approval=False)
                    complete,entry=graph(capkey)
                    self.assertEqual(b,entry['native_id']);self.assertFalse(complete)
                    before=run('pp',['export','--all'])
                    denied=h.request('POST',route,accept,token=self.owner,key='after-demotion-'+case)
                    self.assertEqual(409,denied.status,denied.data)
                    self.assertIn('incomplete',denied.data['error']['message'])
                    self.assertEqual(before,run('pp',['export','--all']))
                    if case=='equivalent':
                        safe=release(a['native_id'],'Preserve the matching approved declaration')
                        self.assertEqual(0,safe.returncode,safe.stdout+safe.stderr)
                        complete,_=graph(capkey);self.assertTrue(complete)
                        denied=h.request('POST',route,accept,token=self.owner,key='after-equivalent-repair')
                        self.assertEqual(409,denied.status,denied.data)
                        self.assertIn('current consumers',denied.data['error']['message'])
                        comments=json.loads(run('area',['comments',a['native_id'],'--json']))
                        self.assertTrue(any('interface declarations preserved' in c['text'] for c in comments))
                    else:
                        before_area=run('area',['export','--all'])
                        refused=release(a['native_id'],'An inert declaration is not absent')
                        self.assertNotEqual(0,refused.returncode)
                        self.assertIn('retained approved interface declaration',refused.stderr)
                        self.assertEqual(before_area,run('area',['export','--all']))
