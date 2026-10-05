"""Owned task recovery, owner policy and actual authenticated attention guards."""
import json
import unittest
from unittest.mock import patch
import test_agent_attention as a
import http_service as h


class EndpointFollowups(a.EndpointAttentionTests):
    def test_partial_large_snapshot_recovers_all_own_tasks_with_four_reads(self):
        actor=self.next()['agent']['actor']
        rows=[dict(task='free-%04d'%i,title='free',owner=None,status='open',review_state='none',open_items=0)
              for i in range(822)]
        rows += [dict(task='own-%04d'%i,title='own',owner=actor,status='in_progress',review_state='none',open_items=0)
                 for i in range(250)]
        calls=[]
        def run(action,project,reader,args,*rest,**kwargs):
            self.assertEqual(action,'work');offset=int(args[args.index('--offset')+1])
            owned='--owner' in args;calls.append((owned,offset))
            selected=[x for x in rows if x['owner']==actor] if owned else rows
            return dict(total=len(selected),items=selected[offset:offset+100],
                        next_offset=offset+100 if offset+100<len(selected) else None)
        with patch.object(self.backend,'_run',side_effect=run):
            data=self.next();view=data['attention']
        self.assertEqual(calls,[(False,0),(True,0),(True,100),(True,200)])
        self.assertEqual(view['counts']['claimed'],250)
        self.assertEqual(view['counts']['in_progress'],250)
        self.assertTrue(view['snapshot_truncated'])
        self.assertFalse(view['own_tasks_truncated'])
        self.assertTrue(view['actions_truncated'])
        self.assertTrue(view['truncated'])
        self.assertEqual(len(data['next_actions']),h.AGENT_ACTION_LIMIT)
        # Claimable count is a lower bound, explicitly distinguished from own completeness.
        self.assertEqual(view['counts']['claimable'],100)

    def test_single_agent_route_filters_snapshot_by_actual_actor(self):
        mine,other,free=self.tasks[:3];self.claim(mine)
        self.native('other-actor')(['update',other,'--assignee','other-actor','--status','in_progress'])
        data=self.next()
        self.assertEqual(data['attention']['counts']['claimed'],1)
        self.assertNotIn(other,[x['task'] for x in data['next_actions']])
        self.assertIn(('in-progress',mine),a.kinds(data))
        self.assertIn(('claimable-task',free),a.kinds(data))

    def test_error_only_route_names_error_and_priority_two(self):
        task=self.tasks[0];self.claim(task)
        self.native('reviewer')(['comments','add',task,'Kind: contribution-review-v1\n{'])
        data=self.next();action=next(x for x in data['next_actions'] if x['task']==task)
        self.assertEqual(action['priority'],2)
        self.assertEqual(action['kind'],'review-error')
        self.assertEqual(data['attention']['state'],'error')
        self.assertEqual(data['attention']['counts']['review_errors'],1)
        self.assertIn('review history error',data['attention']['summary'])

    def test_action_request_ids_are_capped_independently_of_valid_record_caps(self):
        task=self.tasks[0];self.claim(task);actor=self.next()['agent']['actor']
        row=dict(id=task,title='requests',assignee=actor,status='in_progress',review_state='changes-requested',
                 contribution_id='delivery',open_items=0,blocking_items=0,
                 pending_change_requests=['request-%02d'%i for i in range(21)])
        # Defensive action projection: valid protocol records cap at20, so this
        # deliberately injected row tests the last boundary without inventing writes.
        with patch.object(self.backend,'agent_tasks',return_value=dict(tasks=[row],complete=True)):
            action=self.next()['next_action']
        self.assertEqual(action['requests'],row['pending_change_requests'][:20])

    def test_only_blocker_dependency_kinds_block_and_delivery_takes_precedence(self):
        for kind in ('blocker','dependency','question','decision','correction'):
            task=self.create_task(self.people['blair'],self.project,'kind '+kind).data['id'];self.claim(task)
            self.checkpoint(task,[dict(id='item',kind=kind,text='waiting',source='test')])
            data=self.next();action=next(x for x in data['next_actions'] if x['task']==task)
            self.assertEqual(action['kind'],'blocked' if kind in ('blocker','dependency') else 'in-progress')
            row=next(x for x in self.backend.agent_tasks(self.project,data['agent']['actor'])['tasks'] if x['id']==task)
            self.assertEqual((row['open_items'],row['blocking_items']),(1,int(kind in ('blocker','dependency'))))
            con=self.contribute(task)
            action=next(x for x in self.next()['next_actions'] if x['task']==task)
            self.assertEqual(action['kind'],'awaiting-review')
            self.review(self.people['blair'],task,'approve',previous=con,contribution=con,summary='ok')
            action=next(x for x in self.next()['next_actions'] if x['task']==task)
            self.assertEqual(action['kind'],'awaiting-integration')

    def test_native_unattributed_edits_stay_quiet_and_reassigned_authors_wake(self):
        task=self.tasks[0];self.claim(task)
        self.checkpoint(task,[dict(id='block',kind='blocker',text='waiting',source='test')])
        self.native('unknown-editor')(['update',task,'--description','changed description','--title','changed title'])
        action=next(x for x in self.next()['next_actions'] if x['task']==task)
        self.assertFalse(action['newer_activity'])
        actor=self.next()['agent']['actor']
        # The old checkpoint author is no longer the assignee: own current
        # assignee comments remain quiet; that prior author's comment wakes.
        comments=json.loads(self.native(actor)(['comments',task,'--json']))
        old=next(c for c in comments if c['text'].startswith('Kind: task-checkpoint-v1\n'))
        record=json.loads(old['text'].split('\n',1)[1]);record['previous']=old['id']
        self.native('former-actor')(['comments','add',task,'Kind: task-checkpoint-v1\n'+json.dumps(record)])
        self.native('reviewer')(['update',task,'--assignee','former-actor'])
        self.native('reviewer')(['update',task,'--assignee',actor])
        self.native(actor)(['comments','add',task,'Current own comment'])
        self.assertFalse(next(x for x in self.next()['next_actions'] if x['task']==task)['newer_activity'])
        self.native('former-actor')(['comments','add',task,'Former owner instruction'])
        self.assertTrue(next(x for x in self.next()['next_actions'] if x['task']==task)['newer_activity'])

    def test_withdrawn_and_superseded_deliveries_keep_their_review_state(self):
        actor=self.next()['agent']['actor'];task=self.tasks[0]
        self.claim(task)
        for state in ('withdrawn','superseded'):
            row=dict(id=task,title='delivery',assignee=actor,status='in_progress',review_state=state,
                     contribution_id='delivery',open_items=1,blocking_items=1)
            with patch.object(self.backend,'agent_tasks',return_value=dict(tasks=[row],complete=True)):
                data=self.next();action=next(x for x in data['next_actions'] if x['task']==task)
            self.assertEqual((action['kind'],action['review_state']),('review-state',state))
            self.assertEqual(data['attention']['counts']['blocked'],0)


# These subclasses reuse the real harness, not its existing test inventory.
for name in unittest.defaultTestLoader.getTestCaseNames(a.EndpointAttentionTests):
    if name not in EndpointFollowups.__dict__:setattr(EndpointFollowups,name,None)


class InProcessFollowups(a.InProcessAttentionTests):
    def test_kinds_and_delivered_precedence_match_endpoint_policy(self):
        task=self.task('policy');base='/v1/projects/%s/tasks/%s'%(self.project,task)
        self.request('POST',base+'/claim',token=self.secret)
        for kind in ('blocker','dependency','question','decision','correction'):
            self.backend.state.setdefault('checkpoints',{})[task]=[dict(created_at='2026-10-05T00:00:00Z',
                actor='author',open_items=[dict(id='item',kind=kind,text='waiting')])]
            data=self.next();action=next(x for x in data['next_actions'] if x['task']==task)
            self.assertEqual(action['kind'],'blocked' if kind in ('blocker','dependency') else 'in-progress')
        self.backend.state['checkpoints'][task][-1]['open_items'][0]['kind']='blocker'
        result=self.request('POST',base+'/reviews',dict(operation='contribute',commit=a.test_http_agents.COMMIT,
            base_commit=a.test_http_agents.BASE,bundle_sha256=a.test_http_agents.BUNDLE,summary='delivered'),token=self.secret)
        self.assertEqual(result.status,201,result.data)
        data=self.next();self.assertEqual(data['next_action']['kind'],'awaiting-review')
        self.assertEqual(data['attention']['counts']['blocked'],0)

for name in unittest.defaultTestLoader.getTestCaseNames(a.InProcessAttentionTests):
    if name not in InProcessFollowups.__dict__:setattr(InProcessFollowups,name,None)
