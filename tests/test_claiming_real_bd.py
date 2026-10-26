"""Real-bd end-to-end tests for the per-project claiming setting (kittrial-5bb.352).

Uses the same real web service + real endpoint + real bd harness as
``test_claim_held``: its mixins are reused (the module also brings the harness's
label-alias tests, which run here too), while these classes add the claiming tests.
The setting is set through the web route, and both claim routes (the HTTP route and
the host ``bd update --claim`` route), the listings that decide what is offered, and
the audit are read through the real stack.
"""
import json
import os
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import claim_limit
import claiming
import record_json
import work
import test_claim_held as held
import test_http_review_fixes as fixes
import test_bd_label_aliases as rb


class ClaimingRealBdTests(held.Members, fixes.Harness, rb.RealBdLabelAliasTests):
    """The real web service, the real endpoint.py, a real bd, project ``pp``."""

    def make_backend(self):
        return held.http_service.EndpointBackend(
            sys.executable, str(KIT / 'endpoint.py'), str(self.root), service=self.service)

    def setUp(self):
        fixes.Harness.setUp(self)
        admin = self.admin_token()
        registered = self.request('POST', '/v1/projects',
                                  {'project_id': 'pp', 'name': 'PP'}, token=admin)
        self.assertIn(registered.status, (200, 201, 409), registered.data)
        self.members('pp')
        self.project = self.root / 'projects' / 'pp'
        # Every test starts from the default, open.
        (self.project / claiming.FILENAME).unlink(missing_ok=True)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # kittrial-5bb.352 review item B1: a listed operator is one of the two callers
        # allowed to hand work out under 'assigned'. Add the name to this scratch
        # deployment only (nothing in the fixture asserts an empty allowlist).
        deployment = cls.root / 'deployment.private.json'
        configured = json.loads(deployment.read_text(encoding='utf-8'))
        configured['operators'] = ['session-operator']
        deployment.write_text(json.dumps(configured), encoding='utf-8')
        try:
            rb.admin.provision_merge_slot(cls.root, 'pp')
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        for signum, previous in getattr(cls, '_signals', {}).items():
            rb.signal.signal(signum, previous)
        cls._signals = {}
        rb.RealBdLabelAliasTests.__dict__['_stop_server'].__func__(cls)
        rb.remove_scratch(cls._tmp)

    def set_claiming(self, who, value, token=None):
        return self.request('PUT', '/v1/projects/pp/claiming', {'claiming': value},
                            token=token or self.tokens[who])

    def host(self, args, actor='session-worker'):
        return rb.endpoint.execute(self.root, {'project': 'pp', 'actor': actor,
                                               'action': 'bd', 'args': args,
                                               'attachments': {}})

    def bd_row(self, task):
        found = json.loads(self.bd('show', task, '--json').stdout)
        found = found[0] if isinstance(found, list) else found
        return found.get('status'), found.get('assignee')

    def work_page(self, actor='session-worker'):
        """The client's own work projection over a real export, with the project directory.

        ``work`` is run by the client, not the endpoint, so this calls the same library
        function the client does, over the exact rows a real ``bd export --all`` gives,
        with the project directory the setting lives in.
        """
        exported = self.bd('export', '--all')
        self.assertEqual(0, exported.returncode, exported)
        rows = record_json.loads_rows(exported.stdout)
        return work.queue(rows, actor, ['--claimable', '--json'], journal=self.project)

    def test_both_claim_routes_refuse_an_unassigned_task_then_the_assignee_starts_it(self):
        task = self.new('claiming gate')
        set_as = self.set_claiming('alex', 'assigned')
        self.assertEqual(200, set_as.status, set_as.data)
        self.assertEqual('assigned', set_as.data.get('claiming'))
        # The web/HTTP claim route.
        refused = self.claim('casey', task)
        self.assertEqual(409, refused.status, refused.data)
        self.assertEqual(claiming.CLAIM_REFUSAL, held.message(refused))
        self.assertEqual(('open', None), self.bd_row(task))
        # The host route, through the real endpoint. The guard refuses before bd is
        # called, so the direct caller sees the ValueError the client turns into its
        # refusal sentence.
        with self.assertRaises(ValueError) as caught:
            self.host(['update', task, '--claim', '--json'])
        self.assertIn(claiming.CLAIM_REFUSAL, str(caught.exception))
        self.assertEqual(('open', None), self.bd_row(task))
        # The coordinator assigns it to casey; starting it is then not the refused
        # "claim of an unassigned task".
        assigned = self.request('POST', '/v1/projects/pp/tasks/%s/assign' % task,
                                {'expected_assignee': None, 'assignee': self.ids['casey'],
                                 'reason': 'hand the task out',
                                 'allow_pending_review': False, 'allow_held_slot': False},
                                token=self.tokens['alex'], key='assign-claiming-1')
        self.assertIn(assigned.status, (200, 201), assigned.data)
        started = self.claim('casey', task)
        self.assertIn(started.status, (200, 409), started.data)
        self.assertEqual(('in_progress', self.ids['casey']), self.bd_row(task))
        audit = self.request('GET', '/v1/projects/pp/audit?limit=100', token=self.tokens['alex'])
        actions = [(item.get('action'), item.get('outcome')) for item in audit.data['items']]
        self.assertIn(('projects.claiming', 'committed'), actions)

    def test_the_listings_drop_unassigned_work_and_carry_the_empty_sentence(self):
        task = self.new('claiming listing')
        # First, open: the task is offered.
        before = self.work_page()
        self.assertTrue(any(item.get('task') == task for item in before.get('items') or []),
                        before)
        # Flip the setting with NO tracker write, then read again over the SAME kind of
        # export: the mode is read fresh from the project directory every call, so the
        # tracker read cache (keyed on the dolt commit) cannot hide the change.
        self.assertEqual(200, self.set_claiming('alex', 'assigned').status)
        after = self.work_page()
        self.assertEqual(('assigned', 0), (after.get('claiming'), after.get('total')))
        self.assertEqual(claiming.EMPTY_SENTENCE, after.get('empty_message'))
        self.assertFalse(any(item.get('task') == task for item in after.get('items') or []))
        ready = self.host(['ready', '--json'])
        self.assertEqual(0, ready['returncode'], ready)
        self.assertEqual([], json.loads(ready['stdout']))
        # Back to open, the same rows are offered again.
        self.assertEqual(200, self.set_claiming('alex', 'open').status)
        again = self.work_page()
        self.assertTrue(any(item.get('task') == task for item in again.get('items') or []),
                        again)

    def test_only_owners_and_coordinators_may_set_it(self):
        refused = self.set_claiming('casey', 'assigned')
        self.assertEqual(403, refused.status, refused.data)
        self.assertEqual('open', self.work_page().get('claiming'))
        # A coordinator (casey promoted by the owner) may set it.
        promoted = self.request('PUT', '/v1/projects/pp/members/%s' % self.ids['casey'],
                                {'role': 'coordinator'}, token=self.tokens['alex'])
        self.assertEqual(200, promoted.status, promoted.data)
        allowed = self.set_claiming('casey', 'assigned')
        self.assertEqual(200, allowed.status, allowed.data)
        project = self.request('GET', '/v1/projects/pp', token=self.tokens['casey'])
        self.assertEqual('assigned', project.data['claiming']['claiming'])
        # The record names the actor that made the change (the account id the kit uses
        # for attribution everywhere else).
        self.assertEqual(self.ids['casey'], project.data['claiming']['set_by'])
        self.assertTrue(project.data['can_coordinate'])

    def test_a_worker_credential_cannot_set_it(self):
        secret = self.issue_credential(self.tokens['alex'], 'pp', label='w', actor='worker-a')['secret']
        refused = self.request('PUT', '/v1/projects/pp/claiming', {'claiming': 'assigned'},
                               token=secret)
        self.assertEqual(403, refused.status, refused.data)

    def agent_secret(self):
        """A personal agent of the owner, granted this project (kittrial-5bb.352 S1/S2)."""
        made = self.request('POST', '/v1/agents',
                            {'name': 'Kestrel', 'projects': ['pp'],
                             'claim_limit': claim_limit.LIMIT_MAX},
                            token=self.tokens['alex'])
        self.assertEqual(201, made.status, made.data)
        return made.data['credential']['secret']

    def task_ids(self):
        listed = self.bd('list', '--json')
        self.assertEqual(0, listed.returncode, listed.stderr)
        return {row.get('id') for row in json.loads(listed.stdout)}

    def test_an_assignee_through_the_bd_passthrough_is_refused_under_assigned(self):
        # kittrial-5bb.352 review item B1: a plain worker must not be able to take an
        # unassigned task by naming itself as assignee, in any bd spelling, on update
        # or on create.
        task = self.new('assignee bypass')
        self.assertEqual(200, self.set_claiming('alex', 'assigned').status)
        attempts = (
            ['update', task, '--claim', '--assignee', 'session-worker', '--json'],
            ['update', task, '--assignee', 'session-worker', '--json'],
            ['update', task, '--status', 'in_progress', '--assignee', 'session-worker', '--json'],
            ['update', task, '--claim', '-a', 'session-worker', '--json'],
            ['update', task, '--assignee=session-worker', '--json'],
            ['update', task, '-a=session-worker', '--json'],
            ['update', task, '-asession-worker', '--json'],
            # pflag boolean clusters: -a at the end of a run of -q/-v/-h shorthands
            # (review item B1). Each consumed the next token or the rest of the token.
            ['update', task, '-qa', 'session-worker', '--json'],
            ['update', task, '-qaW', '--json'],
            ['update', task, '-va=W', '--json'],
            ['update', task, '-qva', 'W', '--json'],
            ['create', 'a created task', '--assignee', 'session-worker', '--json'],
            ['create', 'another created task', '-a', 'session-worker', '--json'],
            ['create', 'a clustered created task', '-qa', 'session-worker', '--json'],
        )
        before = self.task_ids()
        for args in attempts:
            with self.subTest(args=args):
                with self.assertRaises(ValueError) as caught:
                    self.host(args)
                self.assertIn(claiming.CLAIM_REFUSAL, str(caught.exception))
        self.assertEqual(('open', None), self.bd_row(task))
        after = self.task_ids()
        self.assertEqual(before, after)  # neither create wrote a row

    def test_create_child_assignment_uses_the_same_assigned_mode_guard(self):
        parent = self.new('assigned child parent')
        seeded = self.bd('update', parent, '--assignee', self.ids['casey'], '--json')
        self.assertEqual(0, seeded.returncode, seeded.stderr)
        self.assertEqual(200, self.set_claiming('alex', 'assigned').status)
        body = dict(operation='create-child', request_id='assigned-create-child',
                    parent=parent, title='Assigned child', description='', type='task',
                    assignee=self.ids['casey'], priority=1)
        def create(actor):
            return rb.endpoint.execute(self.root, {'project': 'pp', 'actor': actor,
                                                   'action': 'coordinate',
                                                   'args': [json.dumps(body)], 'attachments': {}})
        before = self.task_ids()
        with self.assertRaises(ValueError) as caught:
            create('session-worker')
        self.assertIn(claiming.CLAIM_REFUSAL, str(caught.exception))
        self.assertEqual(before, self.task_ids())
        self.assertFalse(any((self.project / '.coordination-requests').glob('*.json')))

    def test_a_status_shorthand_cannot_claim_an_unassigned_task_under_assigned(self):
        # kittrial-5bb.352 review item B2: `-s=in_progress`, `-sin_progress`,
        # `-qs in_progress` and `-qsin_progress` all move an unassigned task to
        # in_progress in real bd with no assignee, so each must be refused with the
        # setting sentence and leave the row open and unassigned.
        task = self.new('status shorthand bypass')
        self.assertEqual(200, self.set_claiming('alex', 'assigned').status)
        attempts = (
            ['update', task, '-s=in_progress', '--json'],
            ['update', task, '-sin_progress', '--json'],
            ['update', task, '-qs', 'in_progress', '--json'],
            ['update', task, '-qsin_progress', '--json'],
        )
        for args in attempts:
            with self.subTest(args=args):
                with self.assertRaises(ValueError) as caught:
                    self.host(args)
                self.assertIn(claiming.CLAIM_REFUSAL, str(caught.exception))
                self.assertEqual(('open', None), self.bd_row(task))

    def test_a_status_shorthand_cannot_bypass_the_open_dependency_guard(self):
        # kittrial-5bb.352 review item B2: the kittrial-5bb.293 open-dependency guard
        # reads status through the SAME `_claims_the_row`, so each shorthand must be
        # refused there too, naming the open dependency, with the blocked row unchanged.
        opener = self.new('open dependency')
        blocked = self.new('blocked work')
        added = self.bd('dep', 'add', blocked, opener)
        self.assertEqual(0, added.returncode, added.stderr)
        attempts = (
            ['update', blocked, '-s=in_progress', '--json'],
            ['update', blocked, '-sin_progress', '--json'],
            ['update', blocked, '-qs', 'in_progress', '--json'],
            ['update', blocked, '-qsin_progress', '--json'],
        )
        for args in attempts:
            with self.subTest(args=args):
                with self.assertRaises(ValueError) as caught:
                    self.host(args)
                said = str(caught.exception)
                self.assertIn('open dependency', said)
                self.assertIn(opener, said)
                self.assertEqual(('open', None), self.bd_row(blocked))

    def test_an_operator_may_still_hand_work_out_under_assigned(self):
        # B1: the host operator allowlist keeps its hand-out power.
        task = self.new('operator hand-out')
        self.assertEqual(200, self.set_claiming('alex', 'assigned').status)
        # An actor that has already acted here: the operator's value is resolved, not
        # rewritten, exactly as the assignment-time resolver requires (.333).
        answer = self.host(['update', task, '--assignee', self.ids['alex'], '--json'],
                           actor='session-operator')
        self.assertEqual(0, answer['returncode'], answer)
        self.assertEqual(('open', self.ids['alex']), self.bd_row(task))

    def test_the_agent_routes_carry_the_assigned_empty_answer(self):
        # kittrial-5bb.352 review item S1: /v1/agents/me/next must carry the sentence it
        # already computes, /v1/agents/me/wait must carry it too, and the attention loop
        # must stop offering unassigned work under 'assigned' (S2 mutant at the mode read).
        task = self.new('agent claiming read')
        secret = self.agent_secret()
        offered = self.request('GET', '/v1/agents/me/next', token=secret)
        self.assertEqual(200, offered.status, offered.data)
        self.assertIn(('claimable-task', task),
                      [(a['kind'], a['task']) for a in offered.data['next_actions']])
        self.assertEqual(200, self.set_claiming('alex', 'assigned').status)
        nxt = self.request('GET', '/v1/agents/me/next', token=secret)
        self.assertEqual(200, nxt.status, nxt.data)
        self.assertEqual(0, nxt.data['attention']['counts']['claimable'])
        self.assertEqual([], nxt.data['next_actions'])
        # The sentence travels inside the attention block, which is the set of keys
        # /v1/agents/me/next copies (review item S1): it used to be dropped there.
        self.assertEqual(claiming.EMPTY_SENTENCE, nxt.data['attention']['empty_message'])
        wait = self.request('GET', '/v1/agents/me/wait', token=secret)
        self.assertEqual(200, wait.status, wait.data)
        self.assertEqual([], wait.data['items'])
        self.assertEqual('assigned', wait.data['claiming'])
        self.assertEqual(claiming.EMPTY_SENTENCE, wait.data['empty_message'])
        # Back to open: the same task is offered again on both reads.
        self.assertEqual(200, self.set_claiming('alex', 'open').status)
        again = self.request('GET', '/v1/agents/me/next', token=secret)
        self.assertIn(('claimable-task', task),
                      [(a['kind'], a['task']) for a in again.data['next_actions']])

    def test_the_web_task_list_does_not_offer_unassigned_work_under_assigned(self):
        # kittrial-5bb.352 review item S2: _task_views must read the setting too, or the
        # web Tasks list calls an unassigned row claimable while the queue says 0.
        task = self.new('list claimability')
        self.assertEqual(200, self.set_claiming('alex', 'assigned').status)
        listed = self.request('GET', '/v1/projects/pp/tasks', token=self.tokens['alex'])
        self.assertEqual(200, listed.status, listed.data)
        row = next(item for item in listed.data['items'] if item['id'] == task)
        self.assertFalse(row['claimable'])
        self.assertEqual('coordinator', row['next_action']['who'])
        self.assertEqual(claiming.EMPTY_SENTENCE, row['next_action']['text'])
        self.assertEqual(200, self.set_claiming('alex', 'open').status)
        listed = self.request('GET', '/v1/projects/pp/tasks', token=self.tokens['alex'])
        row = next(item for item in listed.data['items'] if item['id'] == task)
        self.assertTrue(row['claimable'])


if __name__ == '__main__':
    unittest.main()
