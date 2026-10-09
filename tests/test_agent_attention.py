"""Agent attention reports the agent's own states on BOTH backends (kittrial-5bb.114).

On the endpoint backend ``GET /v1/agents/me/next`` read the ``bd list`` snapshot, whose
rows carry no review state and no checkpoint, so an agent with changes requested was told
there was nothing to do. These tests drive the trial's sequence over the strict canonical
stub (the real ``work.queue``, ``review_workflow`` and ``briefing``) and, in a shorter
form, over the in-process backend.
"""
import json
import secrets
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import test_http_agents
import test_http_proposals
import test_http_review_fixes as fixes

COMMIT2 = 'd' * 40


def kinds(data):
    return [(action['kind'], action['task']) for action in data['next_actions']]


class EndpointAttentionTests(fixes.EndpointCase):
    """The web service on the endpoint backend, with a personal agent."""

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.project = self.create_project(self.admin, 'Alpha')
        self.people = {}
        for name, role in (('alex', 'contributor'), ('blair', 'owner')):
            user_id = self.create_account(self.admin, name, name + '-password-1')
            added = self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, user_id), {'role': role},
                                 token=self.admin)
            self.assertIn(added.status, (200, 201), added.data)
            self.people[name] = self.login(name, name + '-password-1')[0]
        made = self.request('POST', '/v1/agents', {'name': 'Kestrel', 'working_directory': '/home/alex/kestrel',
                                                   'projects': [self.project]}, token=self.people['alex'])
        self.assertEqual(201, made.status, made.data)
        self.agent_id, self.secret = made.data['agent']['id'], made.data['credential']['secret']
        self.tasks = [self.create_task(self.people['blair'], self.project, 'task %d' % index).data['id']
                      for index in range(4)]
        self.calls = []
        run = self.backend._run

        def recording(action, project_id, actor, args, *rest, **kwargs):
            self.calls.append((action, list(args)))
            return run(action, project_id, actor, args, *rest, **kwargs)
        self.backend._run = recording

    def base(self, task):
        return '/v1/projects/%s/tasks/%s' % (self.project, task)

    def next(self, token=None):
        answer = self.request('GET', '/v1/agents/me/next', token=token or self.secret)
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    def claim(self, task):
        self.assertEqual(200, self.request('POST', self.base(task) + '/claim', {}, token=self.secret).status)

    def review(self, token, task, operation, **fields):
        body = {'operation': operation, 'schema_version': 1, 'operation_id': 'op-' + secrets.token_hex(6)}
        body.update(fields)
        answer = self.request('POST', self.base(task) + '/reviews', body, token=token)
        self.assertEqual(201, answer.status, answer.data)
        return answer.data.get('comment_id') or (answer.data.get('contribution') or {}).get('comment_id')

    def contribute(self, task, commit=fixes.COMMIT):
        return self.review(self.secret, task, 'contribute', previous=None, **dict(fixes.CONTRIBUTION, commit=commit))

    def checkpoint(self, task, open_items):
        history = self.request('GET', self.base(task) + '/history?limit=5', token=self.secret).data
        answer = self.request('POST', self.base(task) + '/checkpoints', {
            'schema_version': 1, 'previous': None, 'activity_cursor': history['activity_cursor'],
            'source_commit': '', 'branch': '', 'intent': 'do the task', 'acceptance': 'the checks pass',
            'summary': 'stopped', 'next_action': 'wait for the key', 'open_items': open_items, 'resolved': []},
            token=self.secret)
        self.assertEqual(201, answer.status, answer.data)

    def native(self, actor):
        return test_http_proposals.stub_module().Canonical(self.canonical_root, self.project, actor=actor).run

    def test_the_trial_sequence_reports_every_own_state_in_order(self):
        first, second, third, spare = self.tasks
        idle = self.next()
        self.assertEqual((idle['attention']['state'], idle['attention']['counts']['claimable']), ('idle', 4))
        self.assertEqual({kind for kind, _ in kinds(idle)}, {'claimable-task'})

        # Claimed and not delivered: each is the agent's own work, ahead of claimable work.
        for task in (first, second, third):
            self.claim(task)
        data = self.next()
        self.assertEqual(kinds(data), [('in-progress', task) for task in sorted((first, second, third))]
                         + [('claimable-task', spare)])
        counts = data['attention']['counts']
        self.assertEqual((counts['claimed'], counts['in_progress'], counts['claimable'], counts['changes_requested'],
                          counts['awaiting_review'], counts['blocked'], counts['awaiting_integration']),
                         (3, 3, 1, 0, 0, 0, 0))
        self.assertEqual(data['attention']['state'], 'working')
        self.assertEqual([action['priority'] for action in data['next_actions']], [3, 3, 3, 4])

        # A contribution waits for a reviewer: counted, and listed LAST (the agent cannot move it).
        contribution = self.contribute(first)
        data = self.next()
        self.assertEqual(kinds(data), [('in-progress', task) for task in sorted((second, third))]
                         + [('claimable-task', spare), ('awaiting-review', first)])
        self.assertEqual((data['attention']['counts']['awaiting_review'], data['attention']['counts']['in_progress']),
                         (1, 2))

        # The reviewer requests changes, two items: the agent is told, first, with the request id.
        request = self.review(self.people['blair'], first, 'request-changes', previous=contribution,
                              contribution=contribution,
                              items=[{'id': 'tests', 'text': 'Add a test.'}, {'id': 'docs', 'text': 'Say why.'}])
        data = self.next()
        self.assertEqual(kinds(data)[0], ('changes-requested', first))
        action = data['next_actions'][0]
        self.assertEqual((action['priority'], action['requests'], action['review_state']),
                         (1, [request], 'changes-requested'))
        self.assertEqual((data['attention']['state'], data['attention']['counts']['changes_requested'],
                          data['attention']['counts']['awaiting_review']), ('changes-requested', 1, 0))
        self.assertIn('1 contribution(s) have changes requested', data['attention']['summary'])
        # The task list route agrees (it always did; the attention read is what was wrong).
        listed = {task['id']: task['review_state'] for task in
                  self.request('GET', '/v1/projects/%s/tasks' % self.project, token=self.secret).data['items']}
        self.assertEqual(listed[first], 'changes-requested')

        # A checkpoint that names a blocker: blocked, second, with when and whether anything is newer.
        self.checkpoint(second, [{'id': 'key', 'kind': 'blocker', 'text': 'No key for the registry.',
                                  'source': 'the build log'}])
        data = self.next()
        self.assertEqual(kinds(data), [('changes-requested', first), ('blocked', second), ('in-progress', third),
                                       ('claimable-task', spare)])
        blocked = data['next_actions'][1]
        self.assertEqual((blocked['priority'], blocked['open_items'], blocked['newer_activity']), (2, 1, False))
        self.assertTrue(blocked['blocked_since'])
        self.assertEqual((data['attention']['counts']['blocked'], data['attention']['counts']['in_progress']), (1, 1))
        # Somebody answers on the task: the blocked task now has something new.
        self.native('blair')(['comments', 'add', second, 'The key is in the vault now.'])
        blocked = self.next()['next_actions'][1]
        self.assertEqual((blocked['kind'], blocked['newer_activity']), ('blocked', True))

        # Approved and waiting for integration: counted, listed last, nothing for the agent to do.
        delivered = self.contribute(third, commit=COMMIT2)
        self.review(self.people['blair'], third, 'approve', previous=delivered, contribution=delivered,
                    summary='accepted')
        data = self.next()
        self.assertEqual(kinds(data), [('changes-requested', first), ('blocked', second), ('claimable-task', spare),
                                       ('awaiting-integration', third)])
        self.assertEqual((data['attention']['counts']['awaiting_integration'], data['attention']['counts']['claimed']),
                         (1, 3))
        self.assertEqual(data['next_actions'][-1]['priority'], 5)

        # With the request answered and the blocker resolved only waiting work is left.
        self.review(self.secret, first, 'respond', previous=request, contribution=contribution,
                    resolutions=[{'request': request, 'item': 'tests', 'reason': 'added', 'evidence': 'commit'},
                                 {'request': request, 'item': 'docs', 'reason': 'said', 'evidence': 'commit'}])
        data = self.next()
        self.assertEqual((data['attention']['state'], kinds(data)[0]), ('blocked', ('blocked', second)))
        self.assertEqual(sorted(kinds(data)[-2:]), [('awaiting-integration', third), ('awaiting-review', first)])
        self.assertEqual([action['priority'] for action in data['next_actions']], [2, 4, 5, 5])

    def test_one_unfiltered_work_read_supplies_own_and_claimable_tasks(self):
        self.claim(self.tasks[0])
        actor = self.next()['agent']['actor']
        self.calls.clear()
        self.next()
        self.assertEqual(len(self.calls),1,self.calls)
        self.assertEqual(self.calls[0][0],'work')
        self.assertNotIn('--owner',self.calls[0][1])
        self.assertEqual(self.calls[0][1],['--limit','100','--offset','0','--json'])

    def test_attention_pages_over_100_owned_tasks_and_reports_page_bound(self):
        actor = self.next()['agent']['actor']
        rows = [dict(task='owned-%03d' % i, title='owned', owner=actor,
                     status='in_progress', review_state='none', open_items=0)
                for i in range(130)]
        # Feedback beyond the first page must affect the aggregate even when the
        # returned action list is capped. These are canonical work page fixtures.
        rows[-1].update(review_state='changes-requested', contribution_id='delivery',
                        pending_change_requests=['request'], pending_review_items=1)
        calls = []

        def paged(action, project, reader, args, *rest, **kwargs):
            self.assertEqual(action, 'work')
            offset = int(args[args.index('--offset') + 1])
            calls.append((offset,'--owner' in args))
            return {'items': rows[offset:offset + 100],
                    'next_offset': offset + 100 if offset + 100 < len(rows) else None}

        with patch.object(self.backend, '_run', side_effect=paged):
            data = self.next()
            self.assertEqual(calls, [(0,False), (100,False)])
            self.assertEqual(data['attention']['counts']['claimed'], 130)
            self.assertEqual(data['attention']['counts']['changes_requested'], 1)
            self.assertEqual(kinds(data)[0], ('changes-requested', 'owned-129'))
            self.assertTrue(data['attention']['truncated'])
            calls.clear()
            with patch.object(self.backend, 'QUEUE_MAX_PAGES', 1):
                bounded = self.next()
            self.assertEqual(calls, [(0,False),(0,True)])
            self.assertEqual(bounded['attention']['counts']['claimed'], 100)
            self.assertTrue(bounded['attention']['truncated'])
            self.assertTrue(bounded['attention']['snapshot_truncated'])
            self.assertTrue(bounded['attention']['own_tasks_truncated'])
            self.assertTrue(bounded['attention']['actions_truncated'])
            self.assertIn('at least 100 claimed', bounded['attention']['summary'])

    def test_conflicting_checkpoint_history_keeps_unknown_and_operator_action(self):
        task=self.tasks[0];self.claim(task)
        self.checkpoint(task,[])
        run=self.native('blair');comments=json.loads(run(['comments',task,'--json']))
        cp=next(c for c in comments if c['text'].startswith('Kind: task-checkpoint-v1\n'))
        # A second valid root is an unreadable branch, not a claim of zero blockers.
        run(['comments','add',task,cp['text']])
        action=next(x for x in self.next()['next_actions'] if x['task']==task)
        self.assertEqual((action['kind'],action['open_items'],action['who']),('checkpoint-error',None,'operator'))

    def test_closed_delivered_task_disappears_from_attention(self):
        task=self.tasks[0];self.claim(task);self.contribute(task)
        self.native('blair')(['update',task,'--status','closed'])
        data=self.next()
        self.assertNotIn(task,[x['task'] for x in data['next_actions']])
        self.assertEqual(data['attention']['counts']['claimed'],0)

    def test_own_records_stay_quiet_but_other_actor_comment_wakes(self):
        task=self.tasks[0];self.claim(task)
        self.checkpoint(task,[dict(id='q',kind='blocker',text='Need a key.',source='build')])
        actor=self.next()['agent']['actor']
        self.native(actor)(['comments','add',task,'Own progress.'])
        action=next(x for x in self.next()['next_actions'] if x['task']==task)
        self.assertFalse(action['newer_activity'])
        self.native('blair')(['comments','add',task,'Here is the key.'])
        action=next(x for x in self.next()['next_actions'] if x['task']==task)
        self.assertTrue(action['newer_activity'])

    def test_malformed_review_reports_operator_action_instead_of_empty_work(self):
        task=self.tasks[0];self.claim(task)
        self.native('blair')(['comments','add',task,'Kind: contribution-review-v1\n{'])
        data=self.next()
        action=next(x for x in data['next_actions'] if x['task']==task)
        self.assertEqual((action['kind'],action['review_state'],action['who']),('review-error','error','operator'))
        self.assertIn('reconcile',action['reason'])
        self.assertEqual((data['attention']['counts']['claimed'],data['attention']['counts']['in_progress']),(1,0))

    def test_redelivery_does_not_answer_a_review_request(self):
        task=self.tasks[0];self.claim(task);old=self.contribute(task)
        request=self.review(self.people['blair'],task,'request-changes',previous=old,contribution=old,
                            items=[{'id':'fix','text':'Fix this.'}])
        new=self.review(self.secret,task,'contribute',previous=request,
                        **dict(fixes.CONTRIBUTION,commit=COMMIT2,supersedes=old))
        data=self.next();action=next(x for x in data['next_actions'] if x['task']==task)
        self.assertEqual((action['kind'],action['requests']),('changes-requested',[request]))
        self.review(self.secret,task,'respond',previous=new,contribution=new,
                    resolutions=[{'request':request,'item':'fix','reason':'Fixed.','evidence':COMMIT2}])
        self.assertIn(('awaiting-review',task),kinds(self.next()))

    def test_the_owners_list_reads_each_project_once_for_all_its_agents(self):
        for name in ('Merlin', 'Osprey'):
            made = self.request('POST', '/v1/agents', {'name': name, 'working_directory': '/home/alex/' + name,
                                                       'projects': [self.project]}, token=self.people['alex'])
            self.assertEqual(201, made.status, made.data)
        self.claim(self.tasks[0])
        self.calls.clear()
        listing = self.request('GET', '/v1/agents', token=self.people['alex'])
        self.assertEqual((200, 3), (listing.status, listing.data['total']))
        work = [args for action, args in self.calls if action == 'work']
        self.assertEqual(len(work), 1, self.calls)
        self.assertNotIn('--owner', work[0])
        # My work asks for the queue itself; the agent prompts in it cost no further work read.
        self.calls.clear()
        self.assertEqual(200, self.request('GET', '/v1/me/work', token=self.people['alex']).status)
        self.assertEqual(len([args for action, args in self.calls if action == 'work']), 1, self.calls)
        states = {item['name']: (item['attention']['state'], item['attention']['counts']['in_progress'])
                  for item in listing.data['items']}
        self.assertEqual(states, {'Kestrel': ('working', 1), 'Merlin': ('idle', 0), 'Osprey': ('idle', 0)})
        # The single-agent read and the list agree.
        self.assertEqual(self.next()['attention']['counts'],
                         next(item for item in listing.data['items'] if item['name'] == 'Kestrel')['attention']['counts'])

    def test_a_task_the_agent_was_only_named_to_review_is_not_its_own(self):
        # `work --owner X` also lists tasks X was named to review; those rows belong to
        # somebody else and must not count as the agent's claimed work.
        read = self.backend.agent_tasks(self.project, 'somebody-else')
        self.assertEqual(read, {'tasks': [], 'complete': True})
        self.assertEqual(self.backend.agent_tasks(self.project, '--help'), {'tasks': [], 'complete': True})
        # The canonical view as it answers when `me` holds one task and was named to
        # review another (writing a review request needs the write switch, so the page
        # is given directly), across two pages.
        pages = [{'items': [{'task': 'mine', 'title': 'm', 'owner': 'me', 'status': 'in_progress',
                             'review_state': 'none', 'contribution_id': None, 'pending_change_requests': [],
                             'open_items': 2, 'checkpoint_at': '2026-10-04T10:00:00Z', 'newer_activity': True},
                            {'task': 'named', 'title': 'n', 'owner': 'someone', 'status': 'in_progress',
                             'review_state': 'awaiting-review', 'contribution_id': 'c-1', 'review_request': True}],
                  'next_offset': 100},
                 {'items': [{'task': 'unowned', 'title': 'u', 'owner': None, 'status': 'open',
                             'review_state': 'none'}, 'not a row'], 'next_offset': None}]
        asked = []

        def run(action, project_id, actor, args, *rest, **kwargs):
            asked.append(list(args))
            return pages[len(asked) - 1]
        self.backend._run = run
        read = self.backend.agent_tasks(self.project, 'me')
        self.assertEqual(([task['id'] for task in read['tasks']], read['complete']), (['mine'], True))
        self.assertEqual(read['tasks'][0], {
            'id': 'mine', 'title': 'm', 'status': 'in_progress', 'assignee': 'me', 'review_state': 'none',
            'contribution_id': None, 'pending_change_requests': [], 'open_items': 2, 'blocking_items':2,
            'checkpoint_at': '2026-10-04T10:00:00Z', 'newer_activity': True})
        self.assertEqual(asked, [['--owner', 'me', '--limit', '100', '--offset', '0', '--json'],
                                 ['--owner', 'me', '--limit', '100', '--offset', '100', '--json']])
        # Unfiltered (the owners' list): every HELD task, never an unassigned one, from the
        # review queue read (the same view), given or read here.
        asked.clear()
        read = self.backend.agent_tasks(self.project)
        self.assertEqual(sorted(task['id'] for task in read['tasks']), ['mine', 'named'])
        self.assertTrue(all('--owner' not in args for args in asked))
        mine = next(task for task in read['tasks'] if task['id'] == 'mine')
        self.assertEqual((mine['open_items'], mine['checkpoint_at'], mine['newer_activity'], mine['assignee']),
                         (2, '2026-10-04T10:00:00Z', True, 'me'))
        given = {'items': [{'id': 'x', 'title': 't', 'status': 'open', 'assignee': 'me', 'review_state': 'none',
                            'contribution': None, 'attention': None},
                           {'id': 'y', 'assignee': None}], 'complete': False}
        asked.clear()
        read = self.backend.agent_tasks(self.project, None, queue=given)
        self.assertEqual(([task['id'] for task in read['tasks']], read['complete'], asked), (['x'], False, []))
        self.assertEqual((read['tasks'][0]['open_items'], read['tasks'][0]['pending_change_requests']), (0, []))
        # The page bound is reported, not read past.
        asked.clear()
        self.backend._run = lambda *a, **k: (asked.append(1), {'items': [], 'next_offset': 100})[1]
        read = self.backend.agent_tasks(self.project, 'me')
        self.assertEqual((read['complete'], len(asked)), (False, self.backend.QUEUE_MAX_PAGES))


class InProcessAttentionTests(test_http_agents.AgentHarness):
    """The same rules on the in-process backend."""

    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.alex = self.login('alex', 'alex-password-1')[0]
        self.project = self.create_project(self.alex, 'Alpha')
        self.agent_id, self.secret, _ = self.agent_secret(self.alex, projects=[self.project])

    def task(self, title):
        created = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': title}, token=self.alex)
        self.assertEqual(201, created.status, created.data)
        return created.data['id']

    def next(self):
        return self.request('GET', '/v1/agents/me/next', token=self.secret).data

    def test_in_progress_blocked_and_the_order(self):
        first, second, spare = self.task('first'), self.task('second'), self.task('spare')
        for task in (first, second):
            self.assertEqual(200, self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (self.project, task),
                                               token=self.secret).status)
        data = self.next()
        self.assertEqual(kinds(data), [('in-progress', task) for task in sorted((first, second))]
                         + [('claimable-task', spare)])
        self.assertEqual(data['attention']['state'], 'working')
        # A blocker recorded in the latest checkpoint.
        self.backend.state.setdefault('checkpoints', {})[second] = [
            {'created_at': '2026-10-04T10:00:00Z', 'open_items': [{'id': 'key', 'kind':'blocker', 'text': 'no key'}]}]
        data = self.next()
        self.assertEqual(kinds(data), [('blocked', second), ('in-progress', first), ('claimable-task', spare)])
        blocked = data['next_actions'][0]
        self.assertEqual((blocked['blocked_since'], blocked['open_items'], blocked['newer_activity']),
                         ('2026-10-04T10:00:00Z', 1, False))
        self.assertEqual(data['attention']['counts']['blocked'], 1)
        # A contribution waiting for review goes last.
        contributed = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (self.project, first),
                                   {'operation': 'contribute', 'commit': test_http_agents.COMMIT,
                                    'base_commit': test_http_agents.BASE, 'bundle_sha256': test_http_agents.BUNDLE,
                                    'summary': 'delivered'}, token=self.secret)
        self.assertEqual(201, contributed.status, contributed.data)
        data = self.next()
        self.assertEqual(kinds(data), [('blocked', second), ('claimable-task', spare), ('awaiting-review', first)])
        self.assertEqual(data['attention']['state'], 'blocked')

    def test_both_backends_return_the_same_fields_for_an_own_task(self):
        task = self.task('first')
        self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (self.project, task), token=self.secret)
        actor = self.next()['agent']['actor']
        row = self.backend.agent_tasks(self.project, actor)['tasks'][0]
        self.assertEqual(sorted(row), ['assignee', 'blocking_items', 'checkpoint_at', 'contribution_id', 'id', 'newer_activity',
                                       'open_items', 'pending_change_requests', 'review_state', 'status', 'title'])
        self.assertEqual(self.backend.agent_tasks(self.project)['tasks'], [row])   # unfiltered: every held task

    def test_actor_filter_excludes_somebody_elses_work(self):
        task=self.task('somebody else')
        self.backend.state['tasks'][task]['assignee']='someone-else'
        self.assertEqual(self.backend.agent_tasks(self.project,'me')['tasks'],[])
        self.assertEqual([x['id'] for x in self.backend.agent_tasks(self.project,'someone-else')['tasks']],[task])

    def test_closed_delivered_task_disappears_from_attention(self):
        task=self.task('delivered')
        self.request('POST','/v1/projects/%s/tasks/%s/claim'%(self.project,task),token=self.secret)
        self.request('POST','/v1/projects/%s/tasks/%s/reviews'%(self.project,task),
                     {'operation':'contribute','commit':test_http_agents.COMMIT,'base_commit':test_http_agents.BASE,
                      'bundle_sha256':test_http_agents.BUNDLE,'summary':'done'},token=self.secret)
        self.backend.state['tasks'][task]['status']='closed'
        self.assertNotIn(task,[x['task'] for x in self.next()['next_actions']])

    def test_unreadable_checkpoint_does_not_become_zero_open_items(self):
        task=self.task('unknown checkpoint')
        self.request('POST','/v1/projects/%s/tasks/%s/claim'%(self.project,task),token=self.secret)
        self.backend.state.setdefault('checkpoints',{})[task]=['not a checkpoint']
        action=next(x for x in self.next()['next_actions'] if x['task']==task)
        self.assertEqual((action['kind'],action['open_items'],action['who']),('checkpoint-error',None,'operator'))

    def test_own_review_stays_quiet_then_owner_edit_wakes(self):
        task=self.task('blocker');base='/v1/projects/%s/tasks/%s'%(self.project,task)
        self.request('POST',base+'/claim',token=self.secret)
        cp=self.request('POST',base+'/checkpoints',
                        {'previous':None,'summary':'blocked','open_items':[{'id':'key','kind':'blocker','text':'Need a key.'}]},
                        token=self.secret)
        self.assertEqual(cp.status,201,cp.data)
        self.request('POST',base+'/reviews',
                     {'operation':'contribute','commit':test_http_agents.COMMIT,'base_commit':test_http_agents.BASE,
                      'bundle_sha256':test_http_agents.BUNDLE,'summary':'partial'},token=self.secret)
        action=next(x for x in self.next()['next_actions'] if x['task']==task)
        self.assertEqual(action['kind'],'awaiting-review')
        self.assertFalse(self.backend.agent_tasks(self.project)['tasks'][0]['newer_activity'])
        changed=self.request('PATCH',base,{'description':'The key is available.',
                             'version':self.backend.state['tasks'][task]['version']},token=self.alex)
        self.assertEqual(changed.status,200,changed.data)
        action=next(x for x in self.next()['next_actions'] if x['task']==task)
        self.assertTrue(self.backend.agent_tasks(self.project)['tasks'][0]['newer_activity'])

    def test_every_other_open_review_state_names_who_acts_next(self):
        task=self.task('held');actor=self.next()['agent']['actor']
        self.request('POST','/v1/projects/%s/tasks/%s/claim'%(self.project,task),token=self.secret)
        row=dict(id=task,title='held',status='in_progress',assignee=actor,
                 contribution_id='delivery',open_items=0,pending_change_requests=[])
        for state,who in [('legacy-review-ready','owner'),('integrated','owner'),
                          ('withdrawn','assignee'),('superseded','assignee'),
                          ('future-state','owner'),('error','operator')]:
            with self.subTest(state=state),patch.object(self.backend,'agent_tasks',
                    return_value={'tasks':[dict(row,review_state=state)],'complete':True}):
                action=next(x for x in self.next()['next_actions'] if x['task']==task)
                self.assertEqual((action['review_state'],action['who']),(state,who))
                self.assertIn(state,action['reason'])

    def test_independent_counts_and_work_before_waiting(self):
        actor=self.next()['agent']['actor']
        rows=[dict(id='closed',title='closed',status='closed',assignee=actor,review_state='changes-requested',
                   contribution_id='a',open_items=1,pending_change_requests=['request-closed']),
              dict(id='blocked',title='blocked',status='open',assignee=actor,review_state='changes-requested',
                   contribution_id='b',open_items=1,pending_change_requests=['request-open']),
              dict(id='integrating',title='integrating',status='open',assignee=actor,
                   review_state='awaiting-integration',contribution_id='c',open_items=0),
              dict(id='review',title='review',status='open',assignee=actor,
                   review_state='awaiting-review',contribution_id='d',open_items=0),
              dict(id='working',title='working',status='in_progress',assignee=actor,
                   review_state='none',contribution_id=None,open_items=0)]
        with patch.object(self.backend,'agent_tasks',return_value={'tasks':rows,'complete':True}):
            d=self.next();counts=d['attention']['counts']
            self.assertEqual((counts['blocked'],counts['changes_requested'],counts['awaiting_review'],
                              counts['awaiting_integration'],counts['in_progress']),(0,2,1,1,1))
            self.assertEqual(d['attention']['state'],'changes-requested')
            # Once feedback and the blocker have gone, own implementation outranks waiting.
            rows[:]=rows[2:];d=self.next()
            self.assertEqual(d['attention']['state'],'working')
            self.assertEqual(kinds(d)[0],('in-progress','working'))


if __name__ == '__main__':
    unittest.main()
