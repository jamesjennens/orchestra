"""Actual HTTP/EndpointBackend/pinned native assignment path on disposable state."""
import json
import sys
import unittest
from pathlib import Path
KIT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(KIT),str(KIT/'tests')]
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers
import test_web_merge_slot as slot_fixture
import task_assignment

@unittest.skipIf(fixture.endpoint is None,'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None,'no real bd binary (set ORCHESTRA_BD_BIN)')
class NativeAssignmentTests(RulesHelpers,unittest.TestCase):
    web=slot_fixture.NativeWebSlotTests.web

    def test_actual_assignment_notice_stale_cas_roles_and_replay_after_later_assignment(self):
        harness,owner=self.web()
        uid=harness.create_account(owner,'assignment-next','assignment-password-1')
        self.assertEqual(200,harness.request('PUT','/v1/projects/pp/members/'+uid,
                                           {'role':'contributor'},token=owner).status)
        contributor=harness.login('assignment-next','assignment-password-1')[0]
        old=harness.admin_user['id']
        tid=self.task(assignee=old)
        path='/v1/projects/pp/tasks/'+tid+'/assign'
        body=dict(expected_assignee=old,assignee=uid,reason='Next slice\nKeep review items',
                  allow_pending_review=False,allow_held_slot=False)
        before=self.rows()
        denied=harness.request('POST',path,body,token=contributor,key='denied-assignment-0001')
        self.assertEqual(403,denied.status,denied.data);self.assertEqual(before,self.rows())
        roster=harness.request('GET','/v1/projects/pp/assignment-roster?limit=1',token=owner)
        self.assertEqual(200,roster.status,roster.data)
        self.assertEqual(1,len(roster.data['items']));self.assertTrue(roster.data['next_cursor'])
        from urllib.parse import quote
        nextpage=harness.request('GET','/v1/projects/pp/assignment-roster?limit=1&cursor='+
                                 quote(roster.data['next_cursor'],safe=''),token=owner)
        self.assertEqual(200,nextpage.status,nextpage.data)
        self.assertNotEqual(roster.data['items'],nextpage.data['items'])
        for field in ('actor','assigned_by','project','task_id','capability','operation_id'):
            refused=harness.request('POST',path,dict(body,**{field:'forged'}),token=owner,key='forged-assignment-'+field)
            self.assertEqual(422,refused.status,refused.data)
        self.assertEqual(before,self.rows())
        harness.backend.faults['tasks.assign']=1
        lost=harness.request('POST',path,body,token=owner,key='actual-assignment-0001')
        self.assertEqual(503,lost.status,lost.data)
        first=harness.request('POST',path,body,token=owner,key='actual-assignment-0001')
        self.assertEqual(200,first.status,first.data)
        rows=self.rows();row=next(x for x in rows if x['id']==tid)
        self.assertEqual(uid,row['assignee'])
        prior=next(x for x in before if x['id']==tid)
        self.assertEqual(prior['status'],row['status'])
        notices=[c for c in row.get('comments',[]) if c['text'].startswith(task_assignment.PREFIX)]
        self.assertEqual(1,len(notices));notice=task_assignment.parse_comment(notices[0],project='pp',task=tid)
        self.assertIsNotNone(notice);self.assertEqual((old,uid,old),(notice['from'],notice['to'],notice['assigned_by']))
        # All three native readers consume the actual comment; the old holder's
        # work row stays visible only for the notice, with no ownership inference.
        import briefing,work,lanes
        for holder,message in ((old,'This task was taken from you by '),(uid,'Assigned to you by ')):
            brief=briefing.brief(rows,'pp',tid,journal=self.project,actor=holder)
            self.assertTrue(brief['assignment_notices']['items'][0]['message'].startswith(message))
            queue=work.queue(rows,holder,['--mine'],journal=self.project)
            item=next(x for x in queue['items'] if x['task']==tid)
            self.assertTrue(item['assignment_notices']['items'][0]['message'].startswith(message))
            self.assertEqual(uid,item['owner'])
            if holder==old:self.assertEqual('unknown',item['whose_move'])
            board=lanes.read(rows,holder,{'records':{}},[],journal=self.project)
            lane=next(x for x in board['items'] if x['actor']==holder)
            self.assertTrue(lane['assignment_notices']['items'][0]['message'].startswith(message))
        for token in (owner,contributor):
            page=harness.request('GET','/v1/projects/pp/tasks/'+tid+'/brief',token=token)
            self.assertEqual(200,page.status,page.data)
            self.assertTrue(page.data['assignment_notices']['items'])
        stable=self.rows()
        stale=harness.request('POST',path,body,token=owner,key='stale-assignment-0001')
        self.assertEqual(409,stale.status,stale.data);self.assertEqual(stable,self.rows())
        secondbody=dict(body,expected_assignee=uid,assignee=old,reason='Return to original holder')
        second=harness.request('POST',path,secondbody,token=owner,key='actual-assignment-0002')
        self.assertEqual(200,second.status,second.data)
        stable=self.rows()
        replay=harness.request('POST',path,body,token=owner,key='actual-assignment-0001')
        self.assertEqual(200,replay.status,replay.data);self.assertEqual(first.data,replay.data)
        self.assertEqual(stable,self.rows())
        self.assertEqual(old,next(x for x in self.rows() if x['id']==tid)['assignee'])

        blocked=self.r.bd('update',tid,'--status','blocked','--json');self.assertEqual(0,blocked.returncode,blocked.stderr)
        assigned_blocked=harness.request('POST',path,dict(body,reason='Assign a blocked task without reopening'),token=owner,key='blocked-native-assignment-0001')
        self.assertEqual(200,assigned_blocked.status,assigned_blocked.data)
        row=next(x for x in self.rows() if x['id']==tid);self.assertEqual('blocked',row['status']);self.assertEqual(uid,row['assignee'])
        returned=harness.request('POST',path,dict(secondbody,reason='Return blocked task'),token=owner,key='blocked-native-assignment-0002')
        self.assertEqual(200,returned.status,returned.data)
        closed=self.r.bd('update',tid,'--status','closed','--json');self.assertEqual(0,closed.returncode,closed.stderr)
        reassigned=harness.request('POST',path,dict(body,reason='Closed metadata assignment'),token=owner,key='closed-native-assignment-0001')
        self.assertEqual(200,reassigned.status,reassigned.data)
        closed_rows=self.rows();row=next(x for x in closed_rows if x['id']==tid)
        self.assertEqual('closed',row['status']);self.assertEqual(uid,row['assignee'])
        for holder in (old,uid):
            queue=work.queue(closed_rows,holder,['--mine'],journal=self.project)
            self.assertNotIn(tid,[x['task'] for x in queue['items']])
            self.assertTrue(any(x['task']==tid for x in queue['assignment_notices']['items']))

    def assignment_body(self, old, new, **changes):
        return dict(dict(expected_assignee=old,assignee=new,reason='Native boundary test',
                         allow_pending_review=False,allow_held_slot=False),**changes)

    def member(self,harness,owner,name,role='contributor'):
        uid=harness.create_account(owner,name,'native-assignment-password-1')
        member=harness.request('PUT','/v1/projects/pp/members/'+uid,{'role':role},token=owner)
        self.assertEqual(200,member.status,member.data)
        return uid,harness.login(name,'native-assignment-password-1')[0]

    def test_pending_review_and_held_slot_require_separate_flags_and_preserve_both(self):
        self.deployment(review_workflow_writes=True)
        harness,owner=self.web();target=harness.admin_user['id']
        for state in ('awaiting-review','changes-requested'):
            tid=self.task(assignee='worker-old')
            made=self.contribute(tid,'worker-old');self.assertEqual(0,made['returncode'],made)
            contribution=json.loads(made['stdout'])['comment_id']
            if state=='changes-requested':
                changed=self.post_review(tid,'ops',dict(schema_version=1,operation='request-changes',
                    operation_id='assignment-change-'+tid,task=tid,previous=contribution,
                    contribution=contribution,items=[dict(id='required-fix',text='Keep this pending item')]))
                self.assertEqual(0,changed['returncode'],changed)
            held=self.coordinate('worker-old','merge-acquire',tid,'main')
            self.assertTrue(json.loads(held['stdout'])['acquired'])
            try:
                path='/v1/projects/pp/tasks/'+tid+'/assign';base=self.assignment_body('worker-old',target)
                before=self.rows();context=(self.project/'.merge-context.json').read_bytes()
                for review_flag,slot_flag in ((False,False),(True,False),(False,True)):
                    refused=harness.request('POST',path,dict(base,allow_pending_review=review_flag,allow_held_slot=slot_flag),
                        token=owner,key='override-'+tid+'-'+str(review_flag)+'-'+str(slot_flag))
                    self.assertEqual(409,refused.status,refused.data)
                    self.assertIn('explicitly',refused.data['error']['message'])
                    self.assertEqual(before,self.rows());self.assertEqual(context,(self.project/'.merge-context.json').read_bytes())
                assigned=harness.request('POST',path,dict(base,allow_pending_review=True,allow_held_slot=True),
                                         token=owner,key='override-both-'+tid)
                self.assertEqual(200,assigned.status,assigned.data)
                self.assertTrue(assigned.data['review_items_retained']);self.assertTrue(assigned.data['slot_retained'])
                row=next(x for x in self.rows() if x['id']==tid);prior=next(x for x in before if x['id']==tid)
                self.assertEqual(prior.get('comments'),row.get('comments')[:-1]);self.assertEqual(target,row['assignee'])
                self.assertEqual(context,(self.project/'.merge-context.json').read_bytes())
                slot=json.loads(self.coordinate('ops','merge-check')['stdout']);self.assertEqual('worker-old',slot['holder'])
                from review_state import project
                review=project(row,journal=self.project,operators=['ops'])
                self.assertEqual(state,review['review_state'])
                if state=='changes-requested':self.assertEqual('required-fix',review['pending_requests'][0]['item'])
            finally:self.coordinate('worker-old','merge-release')

    def test_partial_native_write_keeps_unknown_identity_and_retry_never_writes_again(self):
        from unittest.mock import patch
        from http_authority import AuthorityConfig
        harness,owner=self.web();old=harness.admin_user['id'];new,_=self.member(harness,owner,'partial-target')
        tid=self.task(assignee=old);path='/v1/projects/pp/tasks/'+tid+'/assign';body=self.assignment_body(old,new)
        original=harness.backend._endpoint;native_run=fixture.endpoint.native.run;answers=[];writes=[];failed=[False]
        def dispatch(argv,*args,**kwargs):
            if 'update' in argv:writes.append('update')
            if 'comments' in argv and 'add' in argv:
                writes.append('comment')
                if not failed[0]:failed[0]=True;raise ValueError('synthetic interruption after native assignee update')
            return native_run(argv,*args,**kwargs)
        def intercepted(action,project,actor,args,attachments=None,**kwargs):
            if action!='task-assign':return original(action,project,actor,args,attachments,**kwargs)
            with harness.store.lock:harness.store.save()
            request=dict(project=project,actor=actor,action=action,args=args,attachments=attachments or {},
                         operation_id=kwargs['operation_id'],authority=kwargs['authority'],route=kwargs['route'])
            config=AuthorityConfig(harness.backend.authority_store,harness.backend.authority_lock,
                                   harness.backend.actor_namespace)
            with patch.object(fixture.endpoint.native,'run',side_effect=dispatch):
                answer=fixture.endpoint.execute(self.root,request,authority_config=config,require_authority=True)
            answers.append(answer);return answer
        before=self.rows()
        with patch.object(harness.backend,'_endpoint',side_effect=intercepted):
            lost=harness.request('POST',path,body,token=owner,key='partial-assignment-0001')
            self.assertEqual(503,lost.status,lost.data);self.assertEqual(124,answers[-1]['returncode'])
            partial=self.rows();row=next(x for x in partial if x['id']==tid)
            self.assertEqual(new,row['assignee']);self.assertEqual(0,row.get('comment_count',0))
            # A later external assignee change must never be compensated/replayed.
            changed=self.r.bd('update',tid,'--assignee',old,'--json');self.assertEqual(0,changed.returncode,changed.stderr)
            stable=self.rows()
            retry=harness.request('POST',path,body,token=owner,key='partial-assignment-0001')
            self.assertEqual(503,retry.status,retry.data);self.assertEqual(124,answers[-1]['returncode'])
            self.assertEqual(stable,self.rows())
        self.assertEqual(['update','comment'],writes)
        self.assertNotEqual(before,partial)

    def test_agent_grant_revocation_and_target_membership_loss_refuse_without_native_effects(self):
        harness,owner=self.web();coord,coordtoken=self.member(harness,owner,'assign-coordinator','coordinator')
        target,_=self.member(harness,owner,'eligible-target')
        created=harness.request('POST','/v1/agents',dict(name='Assignment coordinator',working_directory='/tmp/assignment-coordinator',projects=['pp']),token=coordtoken)
        self.assertEqual(201,created.status,created.data);aid=created.data['agent']['id'];agenttoken=created.data['credential']['secret']
        self.assertEqual(200,harness.request('PUT','/v1/projects/pp/agents/'+aid+'/coordinator',{},token=owner).status)
        tid=self.task(assignee=coord);path='/v1/projects/pp/tasks/'+tid+'/assign';body=self.assignment_body(coord,target)
        assigned=harness.request('POST',path,body,token=agenttoken,key='agent-assignment-0001')
        self.assertEqual(200,assigned.status,assigned.data)
        self.assertEqual(200,harness.request('DELETE','/v1/projects/pp/agents/'+aid+'/coordinator',token=owner).status)
        before=self.rows();retry=harness.request('POST',path,body,token=agenttoken,key='agent-assignment-0001')
        self.assertEqual(403,retry.status,retry.data);self.assertEqual(before,self.rows())
        self.assertEqual(200,harness.request('DELETE','/v1/projects/pp/members/'+target,token=owner).status)
        body=self.assignment_body(target,target)
        refused=harness.request('POST',path,body,token=owner,key='removed-target-noop-0001')
        self.assertEqual(409,refused.status,refused.data);self.assertEqual(before,self.rows())

    def test_assigning_to_own_party_agent_does_not_allow_approval_of_its_work(self):
        self.deployment(review_workflow_writes=True)
        harness,owner=self.web();harness.service.approval_by_another_party=True
        other,other_token=self.member(harness,owner,'independent-owner','owner')
        author,_=self.member(harness,owner,'outside-author')
        agent=harness.request('POST','/v1/agents',dict(name='Owner party agent',working_directory='/tmp/owner-party-agent',projects=['pp']),token=owner)
        self.assertEqual(201,agent.status,agent.data);aid=agent.data['agent']['id']
        tid=self.task(assignee='worker-author')
        made=self.contribute(tid,'worker-author');self.assertEqual(0,made['returncode'],made)
        contribution=json.loads(made['stdout'])['comment_id']
        assigned=harness.request('POST','/v1/projects/pp/tasks/'+tid+'/assign',
            self.assignment_body('worker-author',aid,allow_pending_review=True),token=owner,key='own-party-assignment-0001')
        self.assertEqual(200,assigned.status,assigned.data)
        body=dict(schema_version=1,operation='approve',operation_id='own-party-approval-0001',
                  previous=contribution,contribution=contribution,summary='Reviewed independently')
        before=self.rows();refused=harness.request('POST','/v1/projects/pp/tasks/'+tid+'/reviews',body,token=owner)
        self.assertEqual(403,refused.status,refused.data);self.assertIn('party',refused.data['error']['message'])
        self.assertEqual(before,self.rows())
        approved=harness.request('POST','/v1/projects/pp/tasks/'+tid+'/reviews',dict(body,operation_id='other-party-approval-0001'),token=other_token)
        self.assertEqual(201,approved.status,approved.data)

    def test_forged_canonical_block_and_unconfigured_ssh_cannot_assign(self):
        from unittest.mock import patch
        harness,owner=self.web();old=harness.admin_user['id'];target,_=self.member(harness,owner,'binding-target')
        tid=self.task(assignee=old);body=self.assignment_body(old,target);path='/v1/projects/pp/tasks/'+tid+'/assign'
        original=harness.backend._endpoint
        for field,value in (('capability','tasks.write'),('project','foreign')):
            before=self.rows()
            def forged(action,project,actor,args,attachments=None,**kwargs):
                if action=='task-assign':kwargs['authority']=dict(kwargs['authority'],**{field:value})
                return original(action,project,actor,args,attachments,**kwargs)
            with patch.object(harness.backend,'_endpoint',side_effect=forged):
                refused=harness.request('POST',path,body,token=owner,key='forged-binding-'+field)
            self.assertEqual(403,refused.status,refused.data);self.assertEqual(before,self.rows())
        before=self.rows();payload=dict(body,schema_version=1,task=tid,operation_id='ssh-unconfigured-assignment')
        reply=fixture.endpoint.execute(self.root,dict(project='pp',actor='worker-pretender',action='task-assign',
            args=[json.dumps(payload)],operation_id=payload['operation_id'],
            authority=dict(project='pp',capability='coordinate',via='session',user_id=old,store='/forged/store')))
        self.assertEqual(126,reply['returncode'],reply);self.assertEqual(before,self.rows())
        control=harness.request('POST',path,body,token=owner,key='correct-binding-control')
        self.assertEqual(200,control.status,control.data)

    def test_parallel_assignments_and_claim_race_have_one_native_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        harness,owner=self.web();left,_=self.member(harness,owner,'race-left');right,right_token=self.member(harness,owner,'race-right')
        for claim in (False,True):
            tid=self.task();barrier=Barrier(2);path='/v1/projects/pp/tasks/'+tid
            def assign():
                barrier.wait();return harness.request('POST',path+'/assign',self.assignment_body(None,left),
                                                      token=owner,key='race-left-'+tid)
            def contender():
                barrier.wait()
                if claim:return harness.request('POST',path+'/claim',{},token=right_token,key='race-claim-'+tid)
                return harness.request('POST',path+'/assign',self.assignment_body(None,right),token=owner,key='race-right-'+tid)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(assign),pool.submit(contender)];replies=[x.result(timeout=45) for x in futures]
            self.assertEqual([200,409],sorted(x.status for x in replies),[x.data for x in replies])
            row=next(x for x in self.rows() if x['id']==tid);self.assertIn(row['assignee'],(left,right))
            notices=[c for c in row.get('comments',[]) if c['text'].startswith(task_assignment.PREFIX)]
            if replies[0].status==200:self.assertEqual(1,len(notices));self.assertEqual(left,row['assignee'])
            elif claim:self.assertEqual(0,len(notices));self.assertEqual(right,row['assignee'])
            else:self.assertEqual(1,len(notices));self.assertEqual(right,row['assignee'])

    def test_real_assignment_page_and_copied_python_client_over_native_http(self):
        import shutil
        from http_client import Client
        from test_http_web import run_node_module
        node=shutil.which('node')
        if not node:self.skipTest('Node unavailable; actual page not exercised')
        harness,owner=self.web();old=harness.admin_user['id'];target,_=self.member(harness,owner,'page-target')
        tid=self.task(assignee=old);harness.backend.faults['tasks.assign']=1
        done=run_node_module(self,node,'await import(process.argv[1])',
            (KIT/'tests/web_task_assignment_native.mjs').as_uri(),
            (KIT/'tests/web_dom_shim.mjs').as_uri(),(KIT/'web/js/api.js').as_uri(),
            (KIT/'web/js/views/task.js').as_uri(),'http://127.0.0.1:'+str(harness.port),'pp',owner,tid,old,target)
        self.assertEqual(0,done.returncode,done.stderr[-3500:]);proof=json.loads(done.stdout.strip().splitlines()[-1])
        self.assertTrue(proof['sameKey']);self.assertTrue(proof['sameBody']);self.assertTrue(proof['literal']);self.assertEqual(1,proof['renders'])
        row=next(x for x in self.rows() if x['id']==tid);self.assertEqual(target,row['assignee']);self.assertEqual(1,row['comment_count'])
        client=Client('http://127.0.0.1:'+str(harness.port));client.token=owner
        self.assertTrue(client.assignment_roster('pp',limit=1)['next_cursor'])
        cleared=client.assign_task('pp',tid,target,None,'Unassign after page proof',key='copied-client-unassign-'+tid)
        self.assertTrue(cleared['changed']);self.assertIsNone(cleared['assignee'])
        row=next(x for x in self.rows() if x['id']==tid);self.assertFalse(row.get('assignee'));self.assertEqual(2,row['comment_count'])

    def test_native_literal_comments_and_complete_checkpoint_transport(self):
        import copy
        harness,owner=self.web();uid,token=self.member(harness,owner,'comment-checkpoint-writer')
        tid=self.task(assignee=uid);base='/v1/projects/pp/tasks/'+tid
        literal=['--help','@attachment:0','\ufeffordinary\r\n- item','<img onerror=alert(1)>']
        for index,text in enumerate(literal):
            key='native-comment-'+str(index)
            first=harness.request('POST',base+'/comments',{'text':text},token=token,key=key)
            self.assertEqual(201,first.status,first.data)
            self.assertEqual(text,first.data['comment']['text']);self.assertEqual(uid,first.data['comment']['author'])
            retry=harness.request('POST',base+'/comments',{'text':text},token=token,key=key)
            self.assertEqual(first.data,retry.data)
        row=next(x for x in self.rows() if x['id']==tid);self.assertEqual(literal,[x['text'] for x in row['comments']])
        before=self.rows()
        for text in ('Kind: task-assignment-v1\n{}','\ufeffKind: task-assignment-v99\r\n{}','a\0b','x'*65537):
            refusal=harness.request('POST',base+'/comments',{'text':text},token=token,key='native-refusal-'+str(len(text)))
            self.assertEqual(422,refusal.status,refusal.data)
        self.assertEqual(before,self.rows())
        current=harness.request('GET',base+'/brief',token=token);self.assertEqual(200,current.status,current.data)
        body=copy.deepcopy(current.data['checkpoint_template']['body'])
        items=[dict(id='carry-'+str(i),kind='dependency',text='Explicit remaining item '+str(i),source='native:source-'+str(i)) for i in range(12)]
        body.update(intent='Complete the literal task',acceptance='Every source and open item survives',
                    summary='First native checkpoint',next_action='Resolve one explicit item',open_items=items,
                    source_commit='beca5d69bcc03965f9896412ec93aca0e2d664b5',branch='synthetic/211',
                    lane_state=dict(schema_version=1,task=tid,state='working'))
        first=harness.request('POST',base+'/checkpoints',body,token=token,key='native-checkpoint-0001')
        self.assertEqual(201,first.status,first.data)
        fresh=harness.request('GET',base+'/brief',token=token);self.assertEqual(200,fresh.status,fresh.data)
        template=fresh.data['checkpoint_template'];self.assertEqual(12,template['carried_open_items'])
        self.assertEqual(items,template['body']['open_items'])
        carried=copy.deepcopy(template['body']);carried.update(summary='Resolve item with evidence',next_action='Finish other items')
        carried['open_items']=items[1:];carried['resolved']=[dict(id=items[0]['id'],reason='Checked explicitly',evidence='test:native-item-zero')]
        carried['lane_state']=dict(schema_version=1,task=tid,state='waiting-on-review',waiting_on='reviewer')
        second=harness.request('POST',base+'/checkpoints',carried,token=token,key='native-checkpoint-0002')
        self.assertEqual(201,second.status,second.data)
        replay=harness.request('POST',base+'/checkpoints',carried,token=token,key='native-checkpoint-0002')
        self.assertEqual(second.data,replay.data)
        stable=self.rows()
        same=harness.request('POST',base+'/checkpoints',body,token=token,key='native-old-identical-checkpoint')
        self.assertEqual(201,same.status,same.data);self.assertTrue(same.data['reconciled']);self.assertEqual(stable,self.rows())
        stale=harness.request('POST',base+'/checkpoints',dict(body,summary='Different intent on a stale previous checkpoint'),token=token,key='native-stale-checkpoint')
        self.assertIn(stale.status,(409,422),stale.data);self.assertEqual(stable,self.rows())
        fresh=harness.request('GET',base+'/brief',token=token);self.assertEqual(items[1:],fresh.data['checkpoint_template']['body']['open_items'])

    def test_agent_grant_revoked_after_service_authorization_is_refused_at_native_effect(self):
        from unittest.mock import patch
        harness,owner=self.web();coord,token=self.member(harness,owner,'effect-coordinator','coordinator')
        target,_=self.member(harness,owner,'effect-target')
        created=harness.request('POST','/v1/agents',dict(name='Effect-bound agent',working_directory='/tmp/effect-agent',projects=['pp']),token=token)
        self.assertEqual(201,created.status,created.data);aid=created.data['agent']['id'];secret=created.data['credential']['secret']
        self.assertEqual(200,harness.request('PUT','/v1/projects/pp/agents/'+aid+'/coordinator',{},token=owner).status)
        tid=self.task(assignee=coord);body=self.assignment_body(coord,target);path='/v1/projects/pp/tasks/'+tid+'/assign'
        original=harness.backend._endpoint;answers=[];revoked=[False]
        def revoke_before_effect(action,project,actor,args,attachments=None,**kwargs):
            if action=='task-assign' and not revoked[0]:
                revoked[0]=True
                removed=harness.request('DELETE','/v1/projects/pp/agents/'+aid+'/coordinator',token=owner)
                self.assertEqual(200,removed.status,removed.data)
            answer=original(action,project,actor,args,attachments,**kwargs)
            if action=='task-assign':answers.append(answer)
            return answer
        before=self.rows()
        with patch.object(harness.backend,'_endpoint',side_effect=revoke_before_effect):
            refused=harness.request('POST',path,body,token=secret,key='revoked-before-effect-0001')
        self.assertEqual(403,refused.status,refused.data);self.assertEqual(126,answers[-1]['returncode'])
        self.assertTrue(revoked[0]);self.assertEqual(before,self.rows())
        self.assertEqual(200,harness.request('PUT','/v1/projects/pp/agents/'+aid+'/coordinator',{},token=owner).status)
        control=harness.request('POST',path,body,token=secret,key='regranted-effect-control-0001')
        self.assertEqual(200,control.status,control.data)

    def test_released_older_reader_and_native_backup_restore_preserve_the_notice(self):
        import os,subprocess
        import admin
        old_kit=os.environ.get('ORCHESTRA_ASSIGNMENT_OLD_KIT')
        if not old_kit:self.skipTest('Released beca kit not configured; old-reader/restore proof is unverified')
        old_kit=Path(old_kit)
        head=subprocess.check_output(['git','-C',str(old_kit),'rev-parse','HEAD'],text=True).strip()
        self.assertEqual('beca5d69bcc03965f9896412ec93aca0e2d664b5',head)
        harness,owner=self.web();old=harness.admin_user['id'];target,_=self.member(harness,owner,'backup-target')
        tid=self.task(assignee=old)
        changed=harness.request('POST','/v1/projects/pp/tasks/'+tid+'/assign',self.assignment_body(old,target),
                                token=owner,key='backup-assignment-0001')
        self.assertEqual(200,changed.status,changed.data)
        before=self.rows();row=next(x for x in before if x['id']==tid)
        notice=next(x for x in row['comments'] if x['text'].startswith(task_assignment.PREFIX))
        request=dict(project='pp',actor='older-kit-reader',action='bd',args=['comments',tid,'--json'])
        old_read=subprocess.run([sys.executable,str(old_kit/'endpoint.py'),'--root',str(self.root)],
            input=json.dumps(request),env=admin.environment(self.root),capture_output=True,text=True,timeout=90)
        self.assertEqual(0,old_read.returncode,old_read.stderr)
        envelope=json.loads(old_read.stdout);self.assertEqual(0,envelope['returncode'],envelope)
        comments=json.loads(envelope['stdout']);self.assertTrue(any(c['text']==notice['text'] for c in comments))
        self.assertEqual(before,self.rows())
        backups=self.root/'backups';backups.mkdir(exist_ok=True)
        init=self.r.bd('backup','init',str(backups/'pp'));self.assertEqual(0,init.returncode,init.stderr)
        def command(script,*args):
            return subprocess.run([sys.executable,str(script),'--root',str(self.root),*args],
                env=admin.environment(self.root),capture_output=True,text=True,timeout=180)
        backup=command(KIT/'admin.py','backup','pp');self.assertEqual(0,backup.returncode,backup.stdout+backup.stderr)
        restore=command(KIT/'admin.py','restore-new','pp','assignmentcopy')
        self.assertEqual(0,restore.returncode,restore.stdout+restore.stderr)
        rows=[json.loads(line) for line in admin.run_bd(self.root,'assignmentcopy',['export','--all']).splitlines() if line.strip()]
        restored=next(x for x in rows if x['id']==tid)
        self.assertEqual(row['assignee'],restored['assignee']);self.assertEqual(row['status'],restored['status'])
        restored_notice=next(c for c in restored['comments'] if c['id']==notice['id'])
        self.assertEqual(notice,restored_notice)
        self.assertIsNone(task_assignment.parse_comment(restored_notice,project='assignmentcopy',task=tid))
        self.assertIsNotNone(task_assignment.parse_comment(restored_notice,project='pp',task=tid))
        self.assertEqual(before,self.rows())

if __name__=='__main__':unittest.main()
