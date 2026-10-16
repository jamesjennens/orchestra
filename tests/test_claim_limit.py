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
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import claim_limit
import test_http_agents
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

    # -- what the agent reads --------------------------------------------------
    def test_the_agent_is_told_its_limit_and_what_holds_it(self):
        agent_id, secret, _ = self.new_agent()
        idle = self.next_of(secret)
        self.assertEqual({'limit': 1, 'held': 0, 'available': 1,
                          'counts_task': 0}, idle['claim_limit'])
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


if __name__ == '__main__':
    unittest.main()
