"""Owned task recovery, owner policy and actual authenticated attention guards."""
import json
import os
import secrets
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import test_agent_attention as a
import http_service as h


class EndpointFollowups(a.EndpointAttentionTests):
    def large_work(self, review_rows, free_rows):
        rows = [dict(task='review-%04d'%i, title='other delivery', owner='other-actor',
                     status='in_progress', review_state='awaiting-review', contribution_id='delivery',
                     open_items=0, blocking_items=0) for i in range(review_rows)]
        rows += [dict(task='free-%04d'%i, title='free', owner=None, status='open',
                      review_state='none', open_items=0, blocking_items=0) for i in range(free_rows)]
        calls=[]
        def run(action, project, reader, args, *rest, **kwargs):
            self.assertEqual(action, 'work');offset=int(args[args.index('--offset')+1])
            owned='--owner' in args;calls.append((owned,offset))
            selected=[] if owned else rows
            return dict(total=len(selected),items=selected[offset:offset+100],
                        next_offset=offset+100 if offset+100<len(selected) else None)
        return run,calls

    def test_review_heavy_first_page_recovers_hidden_claimable_suggestions(self):
        run,calls=self.large_work(100, 1520)
        with patch.object(self.backend,'_run',side_effect=run):
            data=self.next();view=data['attention']
        self.assertEqual(calls,[(False,0),(False,100),(True,0)])
        self.assertEqual(view['counts']['claimed'],0)
        self.assertEqual(view['counts']['claimable'],100)
        self.assertEqual(view['state'],'unknown')
        self.assertIn('at least 100 claimable',view['summary'])
        self.assertTrue(view['snapshot_truncated'])
        self.assertFalse(view['own_tasks_truncated'])
        self.assertTrue(view['actions_truncated'])
        self.assertTrue(any(x['kind']=='claimable-task' for x in data['next_actions']))
        self.assertFalse(any(x['task'].startswith('review-') and x['kind']=='in-progress'
                             for x in data['next_actions']))

    def test_bound_without_observed_work_is_unknown_not_idle(self):
        run,calls=self.large_work(1000,720)
        with patch.object(self.backend,'_run',side_effect=run):
            data=self.next();view=data['attention']
        self.assertEqual(len(calls),11)
        self.assertEqual(view['state'],'unknown')
        self.assertIn('at least 0 claimable',view['summary'])
        self.assertNotIn('Idle:',view['summary'])
        self.assertTrue(view['snapshot_truncated'])
        self.assertFalse(view['own_tasks_truncated'])
        self.assertTrue(view['actions_truncated'])  # Other deliveries exceed the review-action cap.

    def test_over_bound_partial_claimables_continue_until_suggestions_fill(self):
        run,calls=self.large_work(99,1521)
        with patch.object(self.backend,'_run',side_effect=run):
            data=self.next();view=data['attention']
        self.assertEqual(calls,[(False,0),(False,100),(True,0)])
        self.assertEqual(view['counts']['claimable'],101)
        self.assertTrue(any(x['kind']=='claimable-task' for x in data['next_actions']))
        self.assertIn('at least 101 claimable',view['summary'])

    def test_legacy_work_fields_keep_blocked_attention_and_my_work(self):
        task=self.tasks[0];self.claim(task)
        self.checkpoint(task,[dict(id='waiting',kind='blocker',text='waiting',source='fixture')])
        human=self.tasks[1];token=self.people['alex']
        self.assertEqual(200,self.request('POST',self.base(human)+'/claim',{},token=token).status)
        history=self.request('GET',self.base(human)+'/history?limit=5',token=token).data
        written=self.request('POST',self.base(human)+'/checkpoints',dict(
            schema_version=1,previous=None,activity_cursor=history['activity_cursor'],source_commit='',branch='',
            intent='fixture',acceptance='legacy blocked state preserved',summary='waiting',next_action='wait',
            open_items=[dict(id='waiting',kind='blocker',text='waiting',source='fixture')],resolved=[]),token=token)
        self.assertEqual(201,written.status,written.data)
        original=self.backend._run
        def legacy(action,*args,**kw):
            value=original(action,*args,**kw)
            if action=='work':
                for row in value.get('items',[]):row.pop('blocking_items',None)
            return value
        with patch.object(self.backend,'_run',side_effect=legacy):
            view=self.next()['attention']
            self.assertEqual(view['state'],'blocked')
            self.assertEqual(view['counts']['checkpoint_errors'],0)
            self.assertEqual(view['counts']['blocked'],1)
            work=self.request('GET','/v1/me/work',token=self.people['alex'])
        self.assertEqual(work.status,200,work.data)
        self.assertTrue(next(x for x in work.data['assigned'] if x['id']==human)['blocked'])
        # The consumer also accepts a projected legacy queue with the field absent.
        queue=self.backend.review_queue
        def legacy_queue(project):
            value=queue(project)
            for row in value['items']:row['attention'].pop('blocking_items',None)
            return value
        with patch.object(self.backend,'READ_CACHE_SECONDS',0), \
                patch.object(self.backend,'review_queue',side_effect=legacy_queue):
            work=self.request('GET','/v1/me/work',token=self.people['alex'])
        self.assertEqual(work.status,200,work.data)
        self.assertTrue(next(x for x in work.data['assigned'] if x['id']==human)['blocked'])

    def test_explicit_unreadable_blocking_count_does_not_use_legacy_fallback(self):
        task=self.tasks[0];self.claim(task);actor=self.next()['agent']['actor']
        row=dict(id=task,title='unreadable',assignee=actor,status='in_progress',review_state='none',
                 open_items=1,blocking_items=None)
        with patch.object(self.backend,'agent_tasks',return_value=dict(tasks=[row],complete=True)):
            view=self.next()['attention']
        self.assertEqual(view['state'],'error')
        self.assertEqual(view['counts']['checkpoint_errors'],1)
        self.assertEqual(view['counts']['blocked'],0)

    def test_failed_authorized_work_read_is_error_with_incomplete_counts(self):
        task=self.tasks[0];self.claim(task)
        original=self.backend._run
        def failed(action,*args,**kw):
            if action=='work':raise h.uncertain('synthetic canonical work failure')
            return original(action,*args,**kw)
        with patch.object(self.backend,'_run',side_effect=failed):
            view=self.next()['attention']
        self.assertEqual(view['state'],'error')
        self.assertEqual(view['counts']['read_errors'],1)
        self.assertIn('work read(s) failed',view['summary'])
        self.assertTrue(view['truncated'])
        self.assertTrue(view['snapshot_truncated'])
        self.assertTrue(view['own_tasks_truncated'])
        self.assertFalse(view['actions_truncated'])

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
        # A second request-changes on one contribution is refused. This injected
        # oversized row defends projection; no reachable real-write sequence is claimed.
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
    def test_failed_project_retains_successful_project_actions(self):
        second=self.create_project(self.alex,'Beta')
        self.agent_id,self.secret,_=self.agent_secret(self.alex,name='Two projects',projects=[self.project,second])
        task=self.task('own successful work')
        self.assertEqual(200,self.request('POST','/v1/projects/%s/tasks/%s/claim'%(self.project,task),
                                         token=self.secret).status)
        original=self.backend.review_queue
        def failed(project_id):
            if project_id==second:raise h.uncertain('synthetic unavailable project')
            return original(project_id)
        with patch.object(self.backend,'review_queue',side_effect=failed):
            data=self.next();view=data['attention']
        self.assertEqual(view['state'],'error')
        self.assertEqual(view['counts']['claimed'],1)
        self.assertEqual(view['counts']['read_errors'],1)
        self.assertIn(('in-progress',task),a.kinds(data))
        self.assertFalse(any(x['project']==second for x in data['next_actions']))

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


# The optional cases use the same isolated server-mode tracker as the claim
# tests. No deployment outside that fixture is read or changed.
import test_bd_label_aliases as rb
import test_claim_held as held_stack


@unittest.skipIf(rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class NativeAttentionFollowups(held_stack.RealStackTests):
    def setUp(self):
        super().setUp()
        self.secret = self.agent('Attention')

    def test_failed_work_read_on_real_tracker_is_error_not_idle(self):
        task=self.new('owned before failed read');self.own(task)
        self.assertEqual(self.next()['attention']['counts']['claimed'],1)
        original=self.backend._run
        def failed(action,*args,**kw):
            if action=='work':raise h.uncertain('synthetic failed canonical read')
            return original(action,*args,**kw)
        with patch.object(self.backend,'_run',side_effect=failed):
            view=self.next()['attention']
        self.assertEqual(view['state'],'error')
        self.assertEqual(view['counts']['read_errors'],1)
        self.assertTrue(view['snapshot_truncated'])
        self.assertTrue(view['own_tasks_truncated'])
        self.assertIn('counts are incomplete',view['summary'])

    def agent(self, name):
        made = self.request('POST', '/v1/agents',
                            dict(name=name, working_directory='/scratch/agent', projects=['pp']),
                            token=self.tokens['casey'])
        self.assertEqual(201, made.status, made.data)
        return made.data['credential']['secret']

    def next(self, token=None):
        reply = self.request('GET', '/v1/agents/me/next', token=token or self.secret)
        self.assertEqual(200, reply.status, reply.data)
        return reply.data

    def base(self, task):
        return self.tasks + '/' + task

    def own(self, task, token=None):
        reply = self.request('POST', self.base(task) + '/claim', {}, token=token or self.secret)
        self.assertEqual(200, reply.status, reply.data)

    def checkpoint(self, task, items):
        history = self.request('GET', self.base(task) + '/history?limit=5', token=self.secret)
        self.assertEqual(200, history.status, history.data)
        reply = self.request('POST', self.base(task) + '/checkpoints',
                             dict(schema_version=1, previous=None,
                                  activity_cursor=history.data['activity_cursor'], source_commit='', branch='',
                                  intent='verify', acceptance='assertions pass', summary='paused',
                                  next_action='read feedback', open_items=items, resolved=[]), token=self.secret)
        self.assertEqual(201, reply.status, reply.data)

    def review(self, task, operation, token=None, **fields):
        reply = self.request('POST', self.base(task) + '/reviews',
                             dict(operation=operation, schema_version=1,
                                  operation_id='op-' + secrets.token_hex(6), **fields),
                             token=token or self.secret)
        self.assertEqual(201, reply.status, reply.data)
        return reply.data.get('comment_id') or (reply.data.get('contribution') or {}).get('comment_id')

    def action(self, task, token=None):
        return next(row for row in self.next(token)['next_actions'] if row['task'] == task)

    def native_ok(self, *args):
        done = self.bd(*args)
        self.assertEqual(0, done.returncode, done.stderr)
        return done.stdout

    def test_checkpoint_kinds_delivery_and_withdrawal_on_real_tracker(self):
        import test_http_review_fixes as fixes
        for kind in ('blocker', 'dependency', 'question', 'decision', 'correction'):
            with self.subTest(kind=kind):
                task = self.new('policy ' + kind)
                self.own(task)
                self.checkpoint(task, [dict(id='waiting', kind=kind, text='waiting', source='fixture')])
                self.assertEqual('blocked' if kind in ('blocker', 'dependency') else 'in-progress',
                                 self.action(task)['kind'])
                contribution = self.review(task, 'contribute', previous=None, **fixes.CONTRIBUTION)
                self.assertEqual('awaiting-review', self.action(task)['kind'])
                self.review(task, 'approve', token=self.tokens['alex'], previous=contribution,
                            contribution=contribution, summary='checked')
                self.assertEqual('awaiting-integration', self.action(task)['kind'])
        config_path = self.root / 'deployment.private.json'
        config = json.loads(config_path.read_text())
        config.update(review_workflow_writes=True, operators=['scratch-operator'])
        config_path.write_text(json.dumps(config), encoding='utf-8')
        for disposition in ('withdrawn', 'superseded'):
            task = self.new(disposition)
            self.own(task)
            self.checkpoint(task, [dict(id='waiting', kind='blocker', text='waiting', source='fixture')])
            contribution = self.review(task, 'contribute', previous=None, **fixes.CONTRIBUTION)
            record = dict(schema_version=1, operation='withdraw', operation_id='op-' + secrets.token_hex(6),
                          task=task, previous=contribution, contribution=contribution,
                          reason='fixture rescope', disposition=disposition)
            result = rb.endpoint.execute(self.root, dict(project='pp', actor='scratch-operator', action='review',
                         args=[task, '@attachment:0'], attachments={'0': {'flag': '--file', 'text': json.dumps(record)}}))
            self.assertEqual(0, result['returncode'], result)
            action = self.action(task)
            self.assertEqual(('review-state', disposition), (action['kind'], action['review_state']))
        self.assertEqual(0, self.next()['attention']['counts']['blocked'])

    def test_native_edits_reassignment_isolation_and_error_summary(self):
        task = self.new('quiet checkpoint')
        self.own(task)
        self.checkpoint(task, [dict(id='waiting', kind='blocker', text='waiting', source='fixture')])
        self.native_ok('update', task, '--title', 'edited', '--description', 'native change')
        self.assertFalse(self.action(task)['newer_activity'])
        comments = json.loads(self.native_ok('comments', task, '--json'))
        old = next(c for c in comments if c['text'].startswith('Kind: task-checkpoint-v1\n'))
        record = json.loads(old['text'].split('\n', 1)[1]); record['previous'] = old['id']
        self.native_ok('comments', 'add', task, 'Kind: task-checkpoint-v1\n' + json.dumps(record),
                       '--author', 'former-owner')
        actor = self.next()['agent']['actor']
        for owner in ('former-owner', actor):
            self.native_ok('update', task, '--assignee', owner)
        self.native_ok('comments', 'add', task, 'current owner comment', '--author', actor)
        self.assertFalse(self.action(task)['newer_activity'])
        time.sleep(1.1)  # Native comment timestamps have second precision.
        self.native_ok('comments', 'add', task, 'former owner instruction', '--author', 'former-owner')
        self.assertTrue(self.action(task)['newer_activity'])
        other = self.agent('Other')
        held = self.new('other actor task'); self.own(held, other)
        self.assertNotIn(held, [row['task'] for row in self.next()['next_actions']])
        error = self.agent('Error'); broken = self.new('review history error'); self.own(broken, error)
        self.native_ok('comments', 'add', broken, 'Kind: contribution-review-v1\n{', '--author', 'reviewer')
        data = self.next(error); action = self.action(broken, error)
        self.assertEqual(('review-error', 2), (action['kind'], action['priority']))
        self.assertEqual('error', data['attention']['state'])
        self.assertIn('review history error', data['attention']['summary'])

    @unittest.skipUnless(os.environ.get('ORCHESTRA_ATTENTION_COMPARE_KIT'),
                         'set ORCHESTRA_ATTENTION_COMPARE_KIT to an isolated prior source checkout')
    def test_prior_writer_current_reader_and_current_writer_prior_reader(self):
        import test_http_review_fixes as fixes
        prior = Path(os.environ['ORCHESTRA_ATTENTION_COMPARE_KIT']) / 'endpoint.py'
        self.assertTrue(prior.is_file())
        task = self.new('record compatibility'); self.own(task)
        current = self.backend.endpoint
        try:
            self.backend.endpoint = str(prior)
            self.checkpoint(task, [])
            self.backend.endpoint = current
            self.assertEqual('in-progress', self.action(task)['kind'])
            self.assertEqual(0, self.action(task)['open_items'])
            self.review(task, 'contribute', previous=None, **fixes.CONTRIBUTION)
            # The shared native fixture may already exceed the snapshot bound.
            # This checks record compatibility, so read the task directly in
            # both directions rather than require it in a bounded suggestion list.
            brief = self.request('GET', self.base(task) + '/brief', token=self.secret)
            self.assertEqual(200, brief.status, brief.data)
            self.assertEqual('awaiting-review', brief.data['review']['state'])
            self.backend.endpoint = str(prior)
            # An older attention view may omit delivered rows at its page bound.
            # Record compatibility is established by that task's direct brief.
            brief = self.request('GET', self.base(task) + '/brief', token=self.secret)
            self.assertEqual(200, brief.status, brief.data)
            self.assertEqual('awaiting-review', brief.data['review']['state'])
        finally:
            self.backend.endpoint = current

    def test_conflicting_native_checkpoint_roots_stay_unknown(self):
        task = self.new('conflicting checkpoint roots'); self.own(task); self.checkpoint(task, [])
        comments = json.loads(self.native_ok('comments', task, '--json'))
        original = next(row for row in comments if row['text'].startswith('Kind: task-checkpoint-v1\n'))
        self.native_ok('comments', 'add', task, original['text'], '--author', 'conflicting-author')
        action = self.action(task)
        self.assertEqual(('checkpoint-error', 2, 'operator', None),
                         (action['kind'], action['priority'], action['who'], action['open_items']))
        self.assertEqual('error', self.next()['attention']['state'])

    def seed_native_scale(self):
        actor = self.next()['agent']['actor']; owned = []
        for index in range(1070):
            args = ['create', '--title', 'Scale %04d' % index, '--json']
            if index < 250:
                args += ['--assignee', actor]
            row = json.loads(self.native_ok(*args))
            if index < 250:
                owned.append(row['id'])
        for offset in range(0, 250, 50):
            self.native_ok('update', *owned[offset:offset + 50], '--status', 'in_progress')
        rows = self.export_rows()
        self.assertGreaterEqual(len(rows), 1070)
        self.assertEqual(250, sum(row.get('assignee') == actor and row.get('status') != 'closed' for row in rows))
        return rows

    def native_scale_reads(self, rows):
        run = self.backend._run; calls = []
        def recording(action, project, reader, args, *rest, **kwargs):
            calls.append(dict(action=action, args=args))
            return run(action, project, reader, args, *rest, **kwargs)
        self.backend._run = recording
        try:
            for label in ('first', 'repeat'):
                calls.clear(); started = time.monotonic(); data = self.next(); elapsed = time.monotonic() - started
                view = data['attention']
                print('NATIVE_ATTENTION_SCALE ' + json.dumps(dict(read=label, seconds=elapsed, rows=len(rows),
                      claimed=view['counts']['claimed'], calls=calls, snapshot_truncated=view.get('snapshot_truncated'),
                      own_tasks_truncated=view.get('own_tasks_truncated'), actions_truncated=view.get('actions_truncated'))),
                      file=sys.stderr, flush=True)
                yield data, list(calls)
        finally:
            self.backend._run = run

    @unittest.skipUnless(os.environ.get('ORCHESTRA_ATTENTION_SCALE') == '1',
                         'set ORCHESTRA_ATTENTION_SCALE=1 for the large native fixture')
    def test_large_native_snapshot_recovers_250_owned_rows(self):
        for data, calls in self.native_scale_reads(self.seed_native_scale()):
            view = data['attention']
            self.assertEqual((250, 250), (view['counts']['claimed'], view['counts']['in_progress']))
            self.assertEqual(4, len(calls))
            self.assertEqual([False, True, True, True], ['--owner' in row['args'] for row in calls])
            self.assertTrue(view['snapshot_truncated']); self.assertFalse(view['own_tasks_truncated'])
            self.assertTrue(view['actions_truncated']); self.assertEqual(h.AGENT_ACTION_LIMIT, len(data['next_actions']))


for base in (rb.RealBdLabelAliasTests, held_stack.RealStackTests):
    for name in unittest.defaultTestLoader.getTestCaseNames(base):
        if name not in NativeAttentionFollowups.__dict__:
            setattr(NativeAttentionFollowups, name, None)


class NativeAttentionHiddenScale(NativeAttentionFollowups):
    """A separate real tracker: earlier cases must not alter the first-page inventory."""

    @unittest.skipUnless(os.environ.get('ORCHESTRA_ATTENTION_R4_SCALE'),
                         'set ORCHESTRA_ATTENTION_R4_SCALE for review-heavy native paging')
    def test_review_heavy_native_first_page_recovers_unclaimed_work(self):
        import test_http_review_fixes as fixes
        other=self.agent('Other deliveries')
        for index in range(100):
            task=self.new('other delivered %03d'%index);self.own(task,other)
            self.review(task,'contribute',token=other,previous=None,**fixes.CONTRIBUTION)
        for index in range(920):self.new('unclaimed %04d'%index)
        original=self.backend._run;calls=[];first=[]
        def recording(action,project,reader,args,*rest,**kwargs):
            value=original(action,project,reader,args,*rest,**kwargs)
            if action=='work':
                calls.append(list(args))
                if '--owner' not in args and args[args.index('--offset')+1]=='0':
                    first.extend(value['items'])
            return value
        with patch.object(self.backend,'_run',side_effect=recording):
            data=self.next();view=data['attention']
        self.assertEqual(len(first),100)
        self.assertTrue(all(x['review_state']=='awaiting-review' for x in first))
        self.assertEqual([int(x[x.index('--offset')+1]) for x in calls if '--owner' not in x],[0,100])
        self.assertEqual(sum('--owner' in x for x in calls),1)
        self.assertEqual(view['counts']['claimed'],0)
        self.assertGreaterEqual(view['counts']['claimable'],30)
        self.assertEqual(view['state'],'unknown')
        self.assertIn('at least',view['summary'])
        self.assertTrue(view['snapshot_truncated'])
        self.assertFalse(view['own_tasks_truncated'])
        self.assertTrue(view['actions_truncated'])
        self.assertTrue(all(x['kind']=='claimable-task' for x in data['next_actions']))
        print('NATIVE_R4_HIDDEN',json.dumps(dict(rows=len(self.export_rows()),work_calls=calls,
              claimed=view['counts']['claimed'],claimable=view['counts']['claimable'],
              state=view['state'],summary=view['summary'],snapshot_truncated=view['snapshot_truncated'],
              own_tasks_truncated=view['own_tasks_truncated'],actions_truncated=view['actions_truncated'])),flush=True)


# Borrow the fixture and helpers, while running only this isolated inventory case.
for name in unittest.defaultTestLoader.getTestCaseNames(NativeAttentionFollowups):
    if name not in NativeAttentionHiddenScale.__dict__:
        setattr(NativeAttentionHiddenScale, name, None)
