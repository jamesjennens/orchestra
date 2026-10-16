"""A per-agent limit on claimed, undelivered tasks (kittrial-5bb.276).

The owner's office incident: a new agent claimed about fifteen open tasks in a few
minutes. The bridge extension refused a claim while it already held an undelivered
one, but that is one client; any agent using curl was unlimited. These tests drive
the service's own gate over the real HTTP route, on the canonical in-process
backend, for both an agent credential and a person.

What counts is stated by the task: a claimed task whose status is ``open`` or
``in_progress`` and whose review state is ``none`` or ``changes-requested``. A
contribution that awaits review has moved to the reviewer's move, so it does not
count. The tests below cover: the default limit of one; a delivered claim not
counting; a task with changes requested still counting; a person unlimited by
default; an owner-set limit; the setting's validation and its read-back on the
agent and on /v1/agents/me/next; and that a held set which cannot be read fails
closed.
"""
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import claim_limit
import task_assignment
import test_http_agents
import test_claim_held as held_stack
import test_http_review_fixes as fixes

COMMIT = 'a' * 40
BASE = 'b' * 40
BUNDLE = 'c' * 64


class ClaimLimitUnitTests(unittest.TestCase):
    """The module's own rules, without a service: bounds, default and counting."""

    def test_the_default_is_one_and_a_hand_edited_value_is_clamped(self):
        self.assertEqual(1, claim_limit.default_limit({'id': 'agent_x'}))
        self.assertEqual(1, claim_limit.default_limit(None))
        self.assertEqual(1, claim_limit.default_limit({'claim_limit': 'three'}))
        self.assertEqual(1, claim_limit.default_limit({'claim_limit': True}))
        self.assertEqual(claim_limit.LIMIT_MIN, claim_limit.default_limit({'claim_limit': 0}))
        self.assertEqual(claim_limit.LIMIT_MAX, claim_limit.default_limit({'claim_limit': 10 ** 9}))
        self.assertEqual(4, claim_limit.default_limit({'claim_limit': 4}))

    def test_only_open_undelivered_rows_are_counted(self):
        rows = [
            {'id': 'a', 'status': 'open', 'review_state': 'none'},
            {'id': 'b', 'status': 'in_progress', 'review_state': None},
            {'id': 'c', 'status': 'in_progress', 'review_state': 'changes-requested'},
            {'id': 'd', 'status': 'in_progress', 'review_state': 'awaiting-review'},
            {'id': 'e', 'status': 'closed', 'review_state': 'none'},
            {'id': 'f', 'status': 'open', 'review_state': 'integrated'},
        ]
        self.assertEqual(['a', 'b', 'c'], [row['id'] for row in claim_limit.held_undelivered(rows)])

    def test_a_blocked_task_does_not_count_but_an_unknown_count_is_not_blocked(self):
        rows = [
            {'id': 'a', 'status': 'in_progress', 'review_state': None, 'blocking_items': 0},
            {'id': 'b', 'status': 'in_progress', 'review_state': None, 'blocking_items': 1},
            {'id': 'c', 'status': 'in_progress', 'review_state': None, 'blocking_items': None},
        ]
        self.assertFalse(claim_limit.blocked(rows[0]))
        self.assertTrue(claim_limit.blocked(rows[1]))
        self.assertFalse(claim_limit.blocked(rows[2]), 'an unknown count is never read as blocked')
        self.assertEqual(['a', 'b', 'c'],
                         [row['id'] for row in claim_limit.held_undelivered(rows)],
                         'a release still sees every undelivered claim')
        self.assertEqual(['a', 'c'],
                         [row['id'] for row in claim_limit.held_against_limit(rows)])

    def test_the_refusal_names_the_limit_and_what_holds_it(self):
        sentence = claim_limit.refusal(1, [{'id': 'kittrial-5bb.5'}])
        self.assertIn('limit is 1', sentence)
        self.assertIn('kittrial-5bb.5', sentence)

    def test_the_setting_is_validated_inside_its_bounds(self):
        self.assertEqual(3, claim_limit.validate_limit(3))
        self.assertIsNone(claim_limit.validate_limit(None))
        for bad in (0, claim_limit.LIMIT_MAX + 1, -1, 'two', True, 1.5):
            with self.assertRaises(ValueError):
                claim_limit.validate_limit(bad)

    def test_a_refused_raise_names_who_may_raise_it_and_where(self):
        """The sentence names the owner's account or a superuser and the My agents page."""
        sentence = claim_limit.raise_refusal(2, 5)
        self.assertIn('5', sentence)
        self.assertIn('2', sentence)
        self.assertIn("owner's account", sentence)
        self.assertIn('superuser', sentence)
        self.assertIn('My agents', sentence)
        self.assertIn(claim_limit.RAISE_BY, sentence)


class ClaimLimitTests(test_http_agents.AgentHarness):
    """The gate over the real HTTP claim route, per role."""

    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.create_account(admin, 'blair', 'blair-password-1')
        self.alex = self.login('alex', 'alex-password-1')[0]
        self.blair = self.login('blair', 'blair-password-1')[0]
        self.project = self.create_project(self.alex, 'Alpha')
        self.tasks = [self.create_task(self.alex, self.project, 'task-%d' % index).data['id']
                      for index in range(5)]

    # -- fixtures --------------------------------------------------------------
    def new_agent(self, **payload):
        payload.setdefault('projects', [self.project])
        return self.agent_secret(self.alex, **payload)

    def base(self, task):
        return '/v1/projects/%s/tasks/%s' % (self.project, task)

    def claim(self, task, token):
        return self.request('POST', self.base(task) + '/claim', {}, token=token)

    def contribute(self, task, token):
        """Deliver one contribution and answer its record's comment id."""
        copied = dict(fixes.CONTRIBUTION)
        copied['commit'] = COMMIT
        copied['base_commit'] = BASE
        body = {'operation': 'contribute', 'schema_version': 1,
                'operation_id': 'op-claim-limit-' + task, 'previous': None, **copied}
        answer = self.request('POST', self.base(task) + '/reviews', body, token=token)
        self.assertEqual(201, answer.status, answer.data)
        # The in-process backend's contribution record id is what later review
        # operations name; on the canonical backend it is the record's comment id.
        contribution = answer.data.get('contribution') or {}
        return contribution.get('id') or contribution.get('comment_id')

    def next_of(self, token):
        answer = self.request('GET', '/v1/agents/me/next', token=token)
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    # -- the gate --------------------------------------------------------------
    def test_an_agent_is_refused_a_second_claim_by_default(self):
        agent_id, secret, _ = self.new_agent()
        first, second = self.tasks[0], self.tasks[1]
        self.assertEqual(200, self.claim(first, secret).status)
        refused = self.claim(second, secret)
        self.assertEqual(409, refused.status, refused.data)
        self.assertIn('limit is 1', refused.data['error']['message'])
        self.assertIn(first, refused.data['error']['message'])
        self.assertEqual(1, refused.data['error']['detail']['claim_limit'])
        self.assertEqual([first], refused.data['error']['detail']['held'])
        # The refused claim left the task exactly as it was.
        shown = self.request('GET', self.base(second), token=self.alex).data
        self.assertIsNone(shown['assignee'])

    def test_a_delivered_claim_does_not_count(self):
        agent_id, secret, _ = self.new_agent()
        first, second = self.tasks[0], self.tasks[1]
        self.assertEqual(200, self.claim(first, secret).status)
        self.contribute(first, secret)
        self.assertEqual(200, self.claim(second, secret).status,
                         'a contribution awaiting review is not the agent\'s to move')

    def test_a_task_with_changes_requested_still_counts(self):
        agent_id, secret, _ = self.new_agent()
        first, second = self.tasks[0], self.tasks[1]
        changed = self.request('PATCH', '/v1/agents/%s' % agent_id, {'claim_limit': 2},
                               token=self.alex)
        self.assertEqual(200, changed.status, changed.data)
        self.assertEqual(200, self.claim(first, secret).status)
        contribution = self.contribute(first, secret)
        self.assertTrue(contribution, 'a contribution record carries a comment id')
        self.assertEqual(200, self.claim(second, secret).status)
        requested = self.request('POST', self.base(first) + '/reviews',
                                 {'operation': 'request-changes', 'schema_version': 1,
                                  'operation_id': 'op-claim-limit-rc',
                                  'contribution': contribution,
                                  'items': [{'id': 'fix-one', 'text': 'Fix the one thing'}]},
                                 token=self.alex)
        self.assertEqual(201, requested.status, requested.data)
        refused = self.claim(self.tasks[2], secret)
        self.assertEqual(409, refused.status, refused.data)
        self.assertEqual(sorted([first, second]),
                         sorted(refused.data['error']['detail']['held']))

    def test_a_person_is_not_limited_by_default(self):
        for task in self.tasks[:3]:
            self.assertEqual(200, self.claim(task, self.alex).status,
                             'a person carries no per-agent limit')

    def test_a_task_that_is_not_the_agents_own_is_not_this_agents_problem(self):
        """Only the claiming agent's own held set is judged; another agent's work is not."""
        first_agent, first_secret, _ = self.new_agent(name='Kestrel')
        second = self.request('POST', '/v1/agents',
                              {'name': 'Osprey', 'working_directory': '/home/alex/osprey',
                               'projects': [self.project]}, token=self.alex)
        self.assertEqual(201, second.status, second.data)
        second_secret = second.data['credential']['secret']
        self.assertEqual(200, self.claim(self.tasks[0], first_secret).status)
        self.assertEqual(200, self.claim(self.tasks[1], second_secret).status,
                         'a second agent has its own, empty held set')

    def test_a_blocked_task_does_not_hold_the_agents_claim_slot(self):
        """The owner's decision: a BLOCKED task does not count, so the order works."""
        agent_id, secret, _ = self.new_agent()
        first, second = self.tasks[0], self.tasks[1]
        self.assertEqual(200, self.claim(first, secret).status)
        made = self.request('POST', self.base(first) + '/checkpoints',
                            {'previous': None, 'summary': 'stuck',
                             'open_items': [{'kind': 'blocker', 'text': 'which page size?'}]},
                            token=secret)
        self.assertIn(made.status, (200, 201), made.data)
        self.assertEqual(200, self.claim(second, secret).status,
                         'the documented order claims other work while a checkpoint leaves a blocker')

    def test_an_agent_granted_two_projects_holding_nothing_can_claim(self):
        """The phantom held task is gone: the gate and /agents/me/next agree."""
        other = self.create_project(self.alex, 'Beta')
        agent_id, secret, _ = self.agent_secret(self.alex, name='Two projects',
                                                projects=[self.project, other])
        shown = self.next_of(secret)['claim_limit']
        self.assertEqual({'limit': 1, 'held': 0, 'available': 1, 'counts_task': 0,
                          'raise_by': claim_limit.RAISE_BY}, shown)
        self.assertEqual(200, self.claim(self.tasks[0], secret).status,
                         'an agent on two projects holding nothing must be able to claim')

    # -- the setting -----------------------------------------------------------
    def test_the_owner_sets_the_limit_and_an_agent_can_then_hold_two(self):
        agent_id, secret, _ = self.new_agent()
        changed = self.request('PATCH', '/v1/agents/%s' % agent_id, {'claim_limit': 2},
                               token=self.alex)
        self.assertEqual(200, changed.status, changed.data)
        self.assertEqual(2, changed.data['claim_limit'])
        self.assertEqual(200, self.claim(self.tasks[0], secret).status)
        self.assertEqual(200, self.claim(self.tasks[1], secret).status)
        third = self.claim(self.tasks[2], secret)
        self.assertEqual(409, third.status, third.data)
        self.assertIn('limit is 2', third.data['error']['message'])
        self.assertEqual(2, third.data['error']['detail']['claim_limit'])

    def test_the_agent_is_created_with_the_limit_its_owner_asks_for(self):
        agent_id, secret, data = self.new_agent(claim_limit=3)
        self.assertEqual(3, data['agent']['claim_limit'])
        self.assertEqual(3, self.request('GET', '/v1/agents/%s' % agent_id,
                                         token=self.alex).data['claim_limit'])

    def test_the_limit_is_validated_and_cleared_back_to_the_default(self):
        agent_id, secret, _ = self.new_agent()
        for bad in (0, claim_limit.LIMIT_MAX + 1, 'two'):
            refused = self.request('PATCH', '/v1/agents/%s' % agent_id,
                                   {'claim_limit': bad}, token=self.alex)
            self.assertEqual(422, refused.status, (bad, refused.data))
        cleared = self.request('PATCH', '/v1/agents/%s' % agent_id, {'claim_limit': None},
                               token=self.alex)
        self.assertEqual(200, cleared.status, cleared.data)
        self.assertEqual(claim_limit.DEFAULT_LIMIT, cleared.data['claim_limit'])

    def test_an_unknown_field_is_still_refused(self):
        agent_id, secret, _ = self.new_agent()
        refused = self.request('PATCH', '/v1/agents/%s' % agent_id,
                               {'claim_limit': 2, 'surprise': 1}, token=self.alex)
        self.assertEqual(422, refused.status, refused.data)

    # -- the agent's own limit (kittrial-5bb.302) ------------------------------
    def my_limit(self, value, token):
        """PATCH /v1/agents/me, the agent's own route."""
        return self.request('PATCH', '/v1/agents/me', {'claim_limit': value}, token=token)

    def test_an_agent_lowers_its_own_limit_and_can_then_hold_that_many(self):
        agent_id, secret, _ = self.new_agent()
        raised = self.request('PATCH', '/v1/agents/%s' % agent_id, {'claim_limit': 3},
                              token=self.alex)
        self.assertEqual(3, raised.data['claim_limit'])
        lowered = self.my_limit(2, secret)
        self.assertEqual(200, lowered.status, lowered.data)
        self.assertEqual(2, lowered.data['agent']['claim_limit'])
        self.assertEqual(3, lowered.data['old_limit'])
        self.assertEqual(2, lowered.data['new_limit'])
        self.assertIn('My agents', lowered.data['raise_by'])
        self.assertEqual(200, self.claim(self.tasks[0], secret).status)
        self.assertEqual(200, self.claim(self.tasks[1], secret).status)
        third = self.claim(self.tasks[2], secret)
        self.assertEqual(409, third.status, third.data)
        self.assertIn('limit is 2', third.data['error']['message'])

    def test_an_agent_may_not_raise_its_own_limit(self):
        agent_id, secret, _ = self.new_agent()
        refused = self.my_limit(2, secret)
        self.assertEqual(403, refused.status, refused.data)
        sentence = refused.data['error']['message']
        self.assertIn("owner's account", sentence)
        self.assertIn('superuser', sentence)
        self.assertIn('My agents', sentence)
        self.assertEqual(1, refused.data['error']['detail']['claim_limit'])
        self.assertEqual(2, refused.data['error']['detail']['requested'])
        # Nothing changed: the agent still holds the default of one.
        shown = self.request('GET', '/v1/agents/%s' % agent_id, token=self.alex).data
        self.assertEqual(1, shown['claim_limit'])
        self.assertEqual(200, self.claim(self.tasks[0], secret).status)
        self.assertEqual(409, self.claim(self.tasks[1], secret).status)

    def test_the_agents_own_limit_is_bounded_and_leaves_its_record_alone(self):
        agent_id, secret, _ = self.new_agent(claim_limit=4)
        for bad in (0, claim_limit.LIMIT_MAX + 1, 'two', 1.5):
            refused = self.my_limit(bad, secret)
            self.assertEqual(422, refused.status, (bad, refused.data))
            shown = self.request('GET', '/v1/agents/%s' % agent_id, token=self.alex).data
            self.assertEqual(4, shown['claim_limit'], 'a refused change left the limit alone')
        # ``null`` is the OWNER's way to clear a setting; the agent route takes the number.
        refused = self.my_limit(None, secret)
        self.assertEqual(422, refused.status, refused.data)

    def test_the_agents_own_route_takes_only_claim_limit(self):
        agent_id, secret, _ = self.new_agent(claim_limit=3)
        refused = self.request('PATCH', '/v1/agents/me',
                               {'claim_limit': 2, 'enabled': False}, token=secret)
        self.assertEqual(422, refused.status, refused.data)
        empty = self.request('PATCH', '/v1/agents/me', {}, token=secret)
        self.assertEqual(422, empty.status, empty.data)
        self.assertEqual(3, self.request('GET', '/v1/agents/%s' % agent_id,
                                         token=self.alex).data['claim_limit'])

    def test_a_person_and_a_worker_credential_may_not_use_the_agents_own_route(self):
        agent_id, secret, _ = self.new_agent()
        person = self.my_limit(1, self.alex)
        self.assertEqual(403, person.status, person.data)
        issued = self.request('POST', '/v1/projects/%s/worker-credentials' % self.project,
                              {'label': 'w', 'actor': 'worker-a'}, token=self.alex)
        self.assertEqual(201, issued.status, issued.data)
        external = self.my_limit(1, issued.data['credential']['secret'])
        self.assertEqual(403, external.status, external.data)
        self.assertEqual(200, self.claim(self.tasks[0], self.alex).status,
                         'a person is not limited and is untouched by this route')

    def test_an_agent_cannot_change_another_agents_limit(self):
        first_id, first_secret, _ = self.new_agent(name='Kestrel')
        second = self.request('POST', '/v1/agents',
                              {'name': 'Osprey', 'working_directory': '/home/alex/osprey',
                               'projects': [self.project], 'claim_limit': 5}, token=self.alex)
        self.assertEqual(201, second.status, second.data)
        second_id = second.data['agent']['id']
        refused = self.request('PATCH', '/v1/agents/%s' % second_id, {'claim_limit': 1},
                               token=first_secret)
        self.assertIn(refused.status, (403, 404), refused.data)
        self.assertEqual(5, self.request('GET', '/v1/agents/%s' % second_id,
                                         token=self.alex).data['claim_limit'])
        # Its own route still only ever touches itself.
        lowered = self.my_limit(1, first_secret)
        self.assertEqual(200, lowered.status, lowered.data)
        self.assertEqual(first_id, lowered.data['agent']['id'])

    def test_a_granted_agent_still_cannot_change_another_agents_limit(self):
        granted = self.request('POST', '/v1/agents',
                               {'name': 'Coord', 'working_directory': '/home/alex/coord',
                                'projects': [self.project], 'claim_limit': 3}, token=self.alex)
        self.assertEqual(201, granted.status, granted.data)
        granted_id = granted.data['agent']['id']
        granted_secret = granted.data['credential']['secret']
        made = self.request('PUT', '/v1/projects/%s/agents/%s/coordinator'
                            % (self.project, granted_id), {}, token=self.alex)
        self.assertIn(made.status, (200, 201), made.data)
        other = self.request('POST', '/v1/agents',
                             {'name': 'Osprey', 'working_directory': '/home/alex/osprey',
                              'projects': [self.project], 'claim_limit': 4}, token=self.alex)
        other_id = other.data['agent']['id']
        refused = self.request('PATCH', '/v1/agents/%s' % other_id, {'claim_limit': 1},
                               token=granted_secret)
        self.assertIn(refused.status, (403, 404), refused.data)
        self.assertEqual(4, self.request('GET', '/v1/agents/%s' % other_id,
                                         token=self.alex).data['claim_limit'])

    def test_the_agents_own_lowering_is_recorded_with_who_and_both_values(self):
        agent_id, secret, _ = self.new_agent(claim_limit=4)
        lowered = self.my_limit(2, secret)
        self.assertEqual(200, lowered.status, lowered.data)
        events = [event for event in self.service.state['audit']
                  if event['action'] == 'agents.claim_limit.lower'
                  and event['outcome'] == 'committed']
        self.assertTrue(events, 'the change must be recorded')
        event = events[-1]
        self.assertEqual(agent_id, event['agent'])
        self.assertEqual(4, event['old_limit'])
        self.assertEqual(2, event['new_limit'])
        self.assertTrue(event['credential_id'], 'who: the credential that made the change')

    def test_both_of_the_agents_own_reads_report_the_limit_and_who_may_raise_it(self):
        agent_id, secret, _ = self.new_agent(claim_limit=2)
        me = self.request('GET', '/v1/agents/me', token=secret)
        self.assertEqual(200, me.status, me.data)
        self.assertEqual({'limit': 2, 'held': 0, 'available': 2, 'counts_task': 0,
                          'raise_by': claim_limit.RAISE_BY}, me.data['claim_limit'])
        self.assertEqual(200, self.claim(self.tasks[0], secret).status)
        held = self.next_of(secret)['claim_limit']
        self.assertEqual((2, 1, 1), (held['limit'], held['held'], held['available']))
        self.assertEqual(claim_limit.RAISE_BY, held['raise_by'])
        again = self.request('GET', '/v1/agents/me', token=secret).data['claim_limit']
        self.assertEqual((2, 1, 1), (again['limit'], again['held'], again['available']))
        self.assertEqual(claim_limit.RAISE_BY, again['raise_by'])

    # -- what the agent reads --------------------------------------------------
    def test_the_agent_is_told_its_limit_and_what_holds_it(self):
        agent_id, secret, _ = self.new_agent()
        idle = self.next_of(secret)
        self.assertEqual({'limit': 1, 'held': 0, 'available': 1,
                          'counts_task': 0,
                          'raise_by': claim_limit.RAISE_BY}, idle['claim_limit'])
        self.assertEqual(200, self.claim(self.tasks[0], secret).status)
        held = self.next_of(secret)['claim_limit']
        self.assertEqual(1, held['limit'])
        self.assertEqual(1, held['held'])
        self.assertEqual(0, held['available'])

    def test_the_owner_reads_the_limit_on_my_agents(self):
        agent_id, secret, _ = self.new_agent(claim_limit=4)
        listing = self.request('GET', '/v1/agents', token=self.alex)
        self.assertEqual(4, listing.data['items'][0]['claim_limit'])

    # -- release from the web --------------------------------------------------
    def release(self, agent_id, **body):
        return self.request('POST', '/v1/agents/%s/release-claims' % agent_id, body,
                            token=self.alex, key='release-' + agent_id)

    def test_release_takes_no_field_at_all(self):
        agent_id, secret, _ = self.new_agent()
        refused = self.release(agent_id, project=self.project)
        self.assertEqual(422, refused.status, refused.data)
        self.assertIn('release', refused.data['error']['message'].lower())

    def test_release_needs_the_canonical_assignment_route(self):
        """The in-process backend has no ``tasks.assign``: the release says so, never pretends."""
        agent_id, secret, _ = self.new_agent()
        self.assertEqual(200, self.claim(self.tasks[0], secret).status)
        answer = self.release(agent_id)
        self.assertEqual(501, answer.status, answer.data)
        # And the claim it would have released is untouched.
        shown = self.request('GET', self.base(self.tasks[0]), token=self.alex).data
        self.assertEqual(agent_id, shown['assignee'])

    def test_release_refuses_an_unknown_agent(self):
        agent_id, secret, _ = self.new_agent()
        answer = self.release('agent_' + '0' * 16)
        self.assertEqual(404, answer.status, answer.data)

    def test_release_dry_run_writes_nothing(self):
        agent_id, secret, _ = self.new_agent()
        self.assertEqual(200, self.claim(self.tasks[0], secret).status)
        answer = self.release(agent_id, dry_run=True)
        self.assertIn(answer.status, (200, 501), answer.data)
        shown = self.request('GET', self.base(self.tasks[0]), token=self.alex).data
        self.assertEqual(agent_id, shown['assignee'])

    # -- failing closed --------------------------------------------------------
    def test_an_agent_cannot_even_be_granted_a_project_its_owner_cannot_read(self):
        """The release's own authority rule, enforced earlier by the grant guard (kittrial-5bb.84/.90)."""
        other = self.create_project(self.blair, 'Blair only')
        refused = self.create_agent(self.alex, projects=[self.project, other])
        self.assertEqual(404, refused.status, refused.data)

    def test_a_release_cannot_touch_an_agent_of_another_owner(self):
        other = self.create_project(self.blair, 'Blair only')
        made = self.request('POST', '/v1/agents',
                            {'name': 'Osprey', 'working_directory': '/home/blair/osprey',
                             'projects': [other]}, token=self.blair)
        self.assertEqual(201, made.status, made.data)
        answer = self.release(made.data['agent']['id'])
        self.assertEqual(404, answer.status, answer.data)

    def test_an_unreadable_held_set_refuses_the_claim(self):
        agent_id, secret, _ = self.new_agent()
        raised = {}

        def broken(project_id, actor=None, queue=None):
            raised['called'] = True
            raise RuntimeError('the tracker read failed')

        original = self.backend.agent_tasks
        self.backend.agent_tasks = broken
        try:
            answer = self.claim(self.tasks[0], secret)
        finally:
            self.backend.agent_tasks = original
        self.assertTrue(raised.get('called'), 'the gate must try to read the held set')
        self.assertEqual(409, answer.status, answer.data)
        self.assertIn('could not be read', answer.data['error']['message'])

    def test_an_unclaimed_task_is_untouched_by_the_refusal(self):
        agent_id, secret, _ = self.new_agent()
        self.assertEqual(200, self.claim(self.tasks[0], secret).status)
        self.assertEqual(409, self.claim(self.tasks[1], secret).status)
        self.assertEqual(200, self.claim(self.tasks[1], self.alex).status,
                         'the task the agent could not take is still claimable by a person')


@unittest.skipIf(held_stack.rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(held_stack.rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class RealStackClaimLimitTests(held_stack.RealStackTests):
    """The gate on the real web service, the real endpoint and a real bd.

    The delivery's own tests ran the in-process backend only, which is how a 500 on
    the endpoint and a race that needs real concurrency both slipped through. These
    run the real stack: the race (review item 2a), the self-assignment bypass (5a),
    the release on the endpoint (3a) and the per-project authority pre-check that
    mutant G5 removes (5b).
    """

    def new_agent(self, name, token=None, **extra):
        body = {'name': name, 'working_directory': '/home/x/' + name.lower().replace(' ', '-'),
                'projects': ['pp']}
        body.update(extra)
        made = self.request('POST', '/v1/agents', body, token=token or self.tokens['alex'])
        self.assertEqual(201, made.status, made.data)
        return made.data['agent']['id'], made.data['credential']['secret']

    def test_one_lock_serializes_two_concurrent_claims_by_one_agent(self):
        for round_ in range(5):
            # A fresh agent each round: the round before leaves one task held.
            agent_id, secret = self.new_agent('Racer %d' % round_)
            left, right = self.new('race a %d' % round_), self.new('race b %d' % round_)
            answers = {}

            def go(slot, task):
                answers[slot] = self.claim(None, task, token=secret,
                                           key='race-%d-%d' % (round_, slot))

            threads = [threading.Thread(target=go, args=(0, left)),
                       threading.Thread(target=go, args=(1, right))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(240)
            statuses = sorted(answer.status for answer in answers.values())
            held = [task for task in (left, right) if self.held(task)[1] == agent_id]
            self.assertEqual([200, 409], statuses,
                             {slot: (answer.status, answer.data)
                              for slot, answer in answers.items()})
            self.assertEqual(1, len(held), 'a limit of one holds exactly one task')

    def test_an_agent_assigning_a_task_to_itself_is_gated_but_a_person_is_not(self):
        agent_id, secret = self.new_agent('Coord')
        granted = self.request('PUT', '/v1/projects/pp/agents/%s/coordinator' % agent_id, {},
                               token=self.tokens['alex'])
        self.assertIn(granted.status, (200, 201), granted.data)
        first, second = self.new('hold one'), self.new('hold two')
        self.assertEqual(200, self.claim(None, first, token=secret).status)
        body = {'expected_assignee': None, 'assignee': agent_id, 'reason': 'self',
                'allow_pending_review': False, 'allow_held_slot': False}
        refused = self.request('POST', '%s/%s/assign' % (self.tasks, second), dict(body),
                               token=secret, key='self-assign-1')
        self.assertEqual(409, refused.status, refused.data)
        self.assertIsNone(self.held(second)[1], 'the refused self-assignment wrote nothing')
        allowed = self.request('POST', '%s/%s/assign' % (self.tasks, second),
                               dict(body, reason='by the owner'), token=self.tokens['alex'],
                               key='person-assign-1')
        self.assertIn(allowed.status, (200, 201), allowed.data)
        self.assertEqual(agent_id, self.held(second)[1],
                         'a person assigning to an agent at its limit is the owner\'s choice')

    def test_release_claims_on_the_real_endpoint_backend_releases_and_retries(self):
        agent_id, secret = self.new_agent('Releasable', claim_limit=2)
        first, second = self.new('rel one'), self.new('rel two')
        self.assertEqual(200, self.claim(None, first, token=secret).status)
        self.assertEqual(200, self.claim(None, second, token=secret).status)
        released = self.request('POST', '/v1/agents/%s/release-claims' % agent_id, {},
                                token=self.tokens['alex'], key='release-real-1')
        self.assertEqual(200, released.status, released.data)
        self.assertEqual(sorted([first, second]),
                         sorted(row['task'] for row in released.data['released']))
        self.assertIsNone(self.held(first)[1])
        self.assertIsNone(self.held(second)[1])
        again = self.request('POST', '/v1/agents/%s/release-claims' % agent_id, {},
                             token=self.tokens['alex'], key='release-real-1')
        self.assertEqual(200, again.status, again.data)
        self.assertEqual([], again.data['released'])

    def test_release_claims_needs_the_coordinate_right_in_every_project(self):
        """Kills mutant G5: without the per-project pre-check this release succeeds."""
        agent_id, secret = self.new_agent('Caseys', token=self.tokens['casey'])
        first = self.new('casey claim')
        self.assertEqual(200, self.claim(None, first, token=secret).status)
        refused = self.request('POST', '/v1/agents/%s/release-claims' % agent_id, {},
                               token=self.tokens['casey'], key='release-g5-1')
        self.assertEqual(409, refused.status, refused.data)
        self.assertIn('pp', refused.data['error']['message'])
        self.assertEqual(agent_id, self.held(first)[1], 'nothing was released')

    def test_an_agent_lowers_its_own_limit_on_the_real_stack(self):
        agent_id, secret = self.new_agent('Lowerer', claim_limit=3)
        lowered = self.request('PATCH', '/v1/agents/me', {'claim_limit': 2}, token=secret)
        self.assertEqual(200, lowered.status, lowered.data)
        self.assertEqual(2, lowered.data['agent']['claim_limit'])
        self.assertEqual((3, 2), (lowered.data['old_limit'], lowered.data['new_limit']))
        refused = self.request('PATCH', '/v1/agents/me', {'claim_limit': 3}, token=secret)
        self.assertEqual(403, refused.status, refused.data)
        self.assertIn('My agents', refused.data['error']['message'])
        shown = self.request('GET', '/v1/agents/me', token=secret)
        self.assertEqual(200, shown.status, shown.data)
        self.assertEqual(2, shown.data['claim_limit']['limit'])


class ClaimReleaseDeliveredWorkUnitTests(unittest.TestCase):
    """The canonical decision a release makes about delivered work (kittrial-5bb.301).

    A release is a facade over the canonical assignment, so what it may release is decided
    from the canonical review snapshot in ``task_assignment.check_change``. Every state that
    means live delivered work must refuse it, not only awaiting review and changes
    requested; an unreadable chain must fail closed. This runs without a service, so the
    decision is pinned for each state a real stack would have to work hard to set up
    (approved and integrated).
    """

    TASK = 'kittrial-5bb.301'

    def payload(self, assignee='agent-x'):
        return {'schema_version': 1, 'operation_id': 'op-release-unit-1', 'task': self.TASK,
                'expected_assignee': assignee, 'assignee': None, 'reason': 'taking other work',
                'allow_pending_review': False, 'allow_held_slot': False}

    def row(self, assignee='agent-x', status='in_progress'):
        return {'id': self.TASK, 'assignee': assignee, 'status': status,
                'labels': [], 'comments': []}

    def review(self, state, contribution=None):
        return {'review_state': state, 'contribution': contribution}

    def slot(self, holder=None, context=None):
        return {'available': holder is None, 'holder': holder, 'context': context}

    def check(self, review, *, slot=None, claim='agent-x'):
        return task_assignment.check_change(self.payload(claim), self.row(claim), review,
                                            slot or self.slot(), lambda target: True,
                                            refuse_delivered=True)

    def test_every_state_of_delivered_work_refuses_a_release(self):
        contribution = {'comment_id': '01a00000-0000-7000-8000-000000000000'}
        for state in ('awaiting-review', 'changes-requested', 'awaiting-integration',
                      'held', 'integrated', 'reconciliation-needed', 'legacy-review-ready'):
            with self.subTest(state=state):
                with self.assertRaises(task_assignment.AssignmentError) as caught:
                    self.check(self.review(state, contribution))
                self.assertEqual(409, caught.exception.status)

    def test_an_undelivered_or_withdrawn_claim_is_releasable(self):
        for state, contribution in (('none', None), ('withdrawn', {'comment_id': 'c'}),
                                    ('superseded', {'comment_id': 'c'})):
            with self.subTest(state=state):
                change = self.check(self.review(state, contribution))
                self.assertTrue(change['changed'])
                self.assertIsNone(change['assignee'])
                self.assertFalse(change['displaced_pending_review'])

    def test_an_unreadable_review_chain_fails_closed(self):
        for review in (None, {'review_state': 'something-else', 'contribution': {'comment_id': 'c'}}):
            with self.subTest(review=review):
                with self.assertRaises(task_assignment.AssignmentError) as caught:
                    self.check(review)
                self.assertEqual(503, caught.exception.status)

    def test_a_holder_that_holds_the_merge_slot_cannot_release(self):
        context = {'holder': 'agent-x', 'task': self.TASK}
        with self.assertRaises(task_assignment.AssignmentError) as caught:
            self.check(self.review('none', None), slot=self.slot('agent-x', context))
        self.assertEqual(409, caught.exception.status)
        self.assertIn('merge slot', str(caught.exception).lower())

    def test_a_self_release_notice_says_the_holder_released_it(self):
        notice = task_assignment.make_notice(self.payload(), project='pp',
                                             assigned_by='agent-x', at='2026-10-16T19:00:00Z')
        self.assertEqual('You released this task: taking other work',
                         task_assignment.holder_message(notice, 'agent-x'))
        # Somebody else taking the same claim still reads as taken from its holder.
        taken = dict(self.payload(assignee='agent-x'), assignee='coordinator-y')
        other = task_assignment.make_notice(taken, project='pp', assigned_by='coordinator-y',
                                            at='2026-10-16T19:00:00Z')
        self.assertEqual('This task was taken from you by coordinator-y: taking other work',
                         task_assignment.holder_message(other, 'agent-x'))


class ClaimReleaseInProcessTests(test_http_agents.AgentHarness):
    """The release route's own body rules, on the disposable in-process backend.

    The in-process backend has no canonical ``tasks.assign`` (kittrial-5bb.276), so a
    well-formed release answers 501 there and changes nothing. This class pins the
    refusals the route makes BEFORE it asks the backend, and that it never pretends on
    a backend that cannot carry a release out. What a carried-out release does is the
    real-stack class below (kittrial-5bb.301).
    """

    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.alex = self.login('alex', 'alex-password-1')[0]
        self.project = self.create_project(self.alex, 'Alpha')
        self.task = self.create_task(self.alex, self.project, 'task-one').data['id']

    def release(self, body, key='release-key-1'):
        return self.request('POST', '/v1/projects/%s/tasks/%s/release' % (self.project, self.task),
                            body, token=self.alex, key=key)

    def test_a_release_takes_one_short_reason_and_nothing_else(self):
        for body in ({}, {'reason': ''}, {'reason': '   '}, {'reason': 'x' * 121},
                     {'reason': 'taking other work', 'assignee': None}):
            with self.subTest(body=body):
                answer = self.release(body)
                self.assertEqual(422, answer.status, answer.data)
                self.assertEqual('invalid_payload', answer.data['error']['code'])

    def test_a_release_needs_an_idempotency_key(self):
        answer = self.release({'reason': 'taking other work'}, key=None)
        self.assertEqual(422, answer.status, answer.data)
        self.assertIn('Idempotency-Key', answer.data['error']['message'])

    def test_a_release_needs_the_canonical_assignment_route(self):
        """No canonical assignment: say so (501), never pretend the claim was released."""
        answer = self.release({'reason': 'taking other work'})
        self.assertEqual(501, answer.status, answer.data)
        self.assertEqual('not_implemented', answer.data['error']['code'])


def borrow_real_stack_runtime(cls):
    """This class uses the real stack's runtime, not its tests (see test_claim_held).

    ``RealStackTests`` carries tests of its own; a class that only borrows its runtime must
    not run them a second time, and must not leave the tasks it creates behind for them.
    """
    for name in dir(held_stack.RealStackTests):
        if name.startswith('test_') and name not in cls.__dict__:
            setattr(cls, name, None)
    return held_stack.without_inherited_tests(cls)


@unittest.skipIf(held_stack.rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(held_stack.rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@borrow_real_stack_runtime
class RealStackClaimReleaseTests(held_stack.RealStackTests):
    """Releasing your own claim on the real service, the real endpoint and a real bd.

    kittrial-5bb.301: the holder may release its own claimed, undelivered task with a
    short reason. The task returns to open and unassigned, its checkpoint and open items
    stay, the claim stops counting against ``claim_limit`` at once, and another agent may
    then claim it. Another actor's claim, a delivered claim and an unheld task are
    refused. The release is recorded as the canonical assignment notice: who, when, why.
    """

    def new_agent(self, name, token=None, **extra):
        body = {'name': name, 'working_directory': '/home/x/' + name.lower().replace(' ', '-'),
                'projects': ['pp']}
        body.update(extra)
        made = self.request('POST', '/v1/agents', body, token=token or self.tokens['alex'])
        self.assertEqual(201, made.status, made.data)
        return made.data['agent']['id'], made.data['credential']['secret']

    def release(self, task, token, reason='releasing to take other work', key=None):
        return self.request('POST', '%s/%s/release' % (self.tasks, task), {'reason': reason},
                            token=token, key=key or ('release-%s' % task))

    def deliver(self, task, token, operation_id):
        copied = dict(fixes.CONTRIBUTION)
        copied['commit'] = COMMIT
        copied['base_commit'] = BASE
        body = {'operation': 'contribute', 'schema_version': 1,
                'operation_id': operation_id, 'previous': None, **copied}
        answer = self.request('POST', '%s/%s/reviews' % (self.tasks, task), body, token=token)
        self.assertEqual(201, answer.status, answer.data)
        return answer.data.get('contribution') or {}

    def test_an_agent_releases_its_own_claim_and_the_slot_is_free_at_once(self):
        agent_id, secret = self.new_agent('Releaser')
        first, second = self.new('to release'), self.new('next')
        self.assertEqual(200, self.claim(None, first, token=secret, key='claim-rel-1').status)
        self.assertEqual(('in_progress', agent_id), self.held(first))
        refused = self.claim(None, second, token=secret, key='claim-rel-2')
        self.assertEqual(409, refused.status, refused.data)
        released = self.release(first, secret, reason='need to take other work')
        self.assertEqual(200, released.status, released.data)
        self.assertEqual(('open', None), self.held(first),
                         'the task returns to open and unassigned')
        self.assertEqual(200, self.claim(None, second, token=secret, key='claim-rel-3').status,
                         'the release freed the claim slot at once')

    def test_the_released_task_is_claimable_by_another_agent(self):
        task = self.new('shared')
        first_id, first_secret = self.new_agent('First')
        second_id, second_secret = self.new_agent('Second')
        self.assertEqual(200, self.claim(None, task, token=first_secret, key='claim-share-1').status)
        self.assertEqual(200, self.release(task, first_secret).status)
        self.assertIsNone(self.held(task)[1])
        self.assertEqual(200, self.claim(None, task, token=second_secret, key='claim-share-2').status)
        self.assertEqual(('in_progress', second_id), self.held(task))

    def test_an_unheld_task_and_another_actors_claim_are_refused(self):
        held = self.new('mine')
        free = self.new('nobody holds this')
        owner_id, owner_secret = self.new_agent('Owner agent')
        other_id, other_secret = self.new_agent('Other agent')
        self.assertEqual(200, self.claim(None, held, token=owner_secret, key='claim-mine-1').status)
        refused = self.release(held, other_secret)
        self.assertEqual(409, refused.status, refused.data)
        self.assertIn('not claimed by you', refused.data['error']['message'])
        self.assertEqual(('in_progress', owner_id), self.held(held))
        unheld = self.release(free, other_secret)
        self.assertEqual(409, unheld.status, unheld.data)
        self.assertEqual(('open', None), self.held(free))

    def test_a_delivered_claim_is_refused(self):
        task = self.new('delivered')
        agent_id, secret = self.new_agent('Deliverer')
        self.assertEqual(200, self.claim(None, task, token=secret, key='claim-del-1').status)
        self.deliver(task, secret, 'op-release-deliver-1')
        refused = self.release(task, secret)
        self.assertEqual(409, refused.status, refused.data)
        self.assertEqual(('in_progress', agent_id), self.held(task),
                         'delivered work is not a claim to release')

    def test_the_released_client_releases_through_its_own_method(self):
        """The client surface (http_client.Client.release_task) over the real service."""
        from http_client import Client
        task = self.new('via the client')
        agent_id, secret = self.new_agent('Client agent')
        self.assertEqual(200, self.claim(None, task, token=secret, key='claim-client-1').status)
        worker = Client('http://127.0.0.1:%d' % self.port).use_credential(secret)
        released = worker.release_task('pp', task, 'taking other work', key='release-client-1')
        self.assertEqual(agent_id, released['previous_assignee'])
        self.assertIsNone(released['assignee'])
        self.assertTrue(released['reopened'])
        self.assertEqual(('open', None), self.held(task))

    def test_the_release_keeps_the_checkpoint_and_records_who_and_why(self):
        task = self.new('checkpointed')
        agent_id, secret = self.new_agent('Checker')
        self.assertEqual(200, self.claim(None, task, token=secret, key='claim-cp-1').status)
        before = self.request('GET', '%s/%s/brief' % (self.tasks, task), token=secret)
        self.assertEqual(200, before.status, before.data)
        made = self.request('POST', '%s/%s/checkpoints' % (self.tasks, task),
                            {'schema_version': 1, 'previous': None,
                             'activity_cursor': before.data.get('activity_cursor'),
                             'source_commit': '', 'branch': '',
                             'intent': 'check the release keeps a checkpoint',
                             'acceptance': 'the open item survives the release',
                             'summary': 'stopping here', 'next_action': 'release the claim',
                             'open_items': [{'id': 'page-size', 'kind': 'blocker', 'source': 'test',
                                             'text': 'which page size?'}],
                             'resolved': []},
                            token=secret)
        self.assertIn(made.status, (200, 201), made.data)
        self.assertEqual(200, self.release(task, secret, reason='handing back for now').status)
        brief = self.request('GET', '%s/%s/brief' % (self.tasks, task), token=self.tokens['alex'])
        self.assertEqual(200, brief.status, brief.data)
        self.assertIn('which page size?', json.dumps(brief.data),
                      'a release keeps the checkpoint and its open items')
        # The notice is the canonical record: who released it, when, and why.
        captured = self.bd('comments', task, '--json')
        self.assertEqual(0, captured.returncode, captured.stderr)
        comments = json.loads(captured.stdout)
        if isinstance(comments, dict):
            comments = comments.get('comments') or []
        notices = [row.get('text') for row in comments
                   if isinstance(row.get('text'), str)
                   and row['text'].startswith('Kind: task-assignment-v1\n')]
        self.assertTrue(notices, comments)
        record = json.loads(notices[-1][len('Kind: task-assignment-v1\n'):])
        self.assertEqual(agent_id, record['assigned_by'])
        self.assertEqual(agent_id, record['from'])
        self.assertIsNone(record['to'])
        self.assertEqual('handing back for now', record['reason'])
        self.assertTrue(record['at'], 'the notice records when the release happened')

    def test_a_lost_response_is_answered_from_the_stored_result(self):
        """Same key and body after a committed release replays the stored 200 (item 2)."""
        task = self.new('retry after a lost answer')
        agent_id, secret = self.new_agent('Retrier')
        self.assertEqual(200, self.claim(None, task, token=secret, key='claim-retry-1').status)
        path = '%s/%s/release' % (self.tasks, task)
        body = {'reason': 'taking other work'}
        first = self.request('POST', path, body, token=secret, key='release-retry-1')
        self.assertEqual(200, first.status, first.data)
        again = self.request('POST', path, body, token=secret, key='release-retry-1')
        self.assertEqual(200, again.status, again.data)
        self.assertEqual(first.data, again.data, 'the stored answer, not a fresh write')
        self.assertEqual(('open', None), self.held(task))

    def test_the_same_key_with_another_body_is_a_key_reuse_refusal(self):
        task = self.new('retry with another body')
        agent_id, secret = self.new_agent('Reuser')
        self.assertEqual(200, self.claim(None, task, token=secret, key='claim-reuse-1').status)
        path = '%s/%s/release' % (self.tasks, task)
        self.assertEqual(200, self.request('POST', path, {'reason': 'first reason'},
                                           token=secret, key='release-reuse-1').status)
        reused = self.request('POST', path, {'reason': 'a different reason'},
                              token=secret, key='release-reuse-1')
        self.assertEqual(409, reused.status, reused.data)
        self.assertIn('reused with a different request', reused.data['error']['message'])
        self.assertEqual(('open', None), self.held(task))

    def test_a_holder_that_also_holds_the_merge_slot_cannot_release(self):
        """Mutant R3: the slot refusal had no test, so a held slot was allowed."""
        task = self.new('claim and the merge slot')
        alex = self.tokens['alex']
        self.assertEqual(200, self.claim('alex', task, key='claim-slot-1').status)
        acquired = self.request('POST', '/v1/projects/pp/merge-slot/acquire',
                                {'task_id': task, 'target': 'main'}, token=alex, key='acquire-slot-1')
        self.assertEqual(200, acquired.status, acquired.data)
        try:
            refused = self.release(task, alex)
            self.assertEqual(409, refused.status, refused.data)
            self.assertIn('merge slot', str(refused.data['error']).lower())
            self.assertEqual(('in_progress', self.ids['alex']), self.held(task),
                             'the slot holder keeps its claim')
        finally:
            # The project has ONE merge slot; leave it free for the sibling tests.
            freed = self.request('POST', '/v1/projects/pp/merge-slot/release', {},
                                 token=alex, key='release-slot-1')
            self.assertIn(freed.status, (200, 409), freed.data)


if __name__ == '__main__':
    unittest.main()
