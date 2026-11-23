"""kittrial-5bb.540 on the real stack: the widened basis gate and the queue hint, through the real endpoint and a real bd.

The stack of tests/test_agent_approval_integration_560.py (the real web service, the real endpoint.py and a
real bd): cora's agent Heron holds the coordinator grant, Egret is cora's second agent and cora issues the
worker credentials.

- The gate: an approval whose summary carries a basis text (an old or a new sentence, label or the
  "Approval basis:" line) is refused 422 and bd gains no comment; a clean summary is written under Heron's
  name, the basis as the record's ``approval_basis`` field (the endpoint path adds no sentence).
- The hint: the queue names the basis the approval record then holds, shape by shape, with the task's
  assignee moved in bd itself.

Skipped where the endpoint cannot run (it imports fcntl: Windows) or there is no real bd. The same rules are
tested everywhere by tests/test_approval_basis_540.py.
"""
import json
import secrets
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import test_agent_approval_integration_560 as integration
import http_service
import test_approval_basis_540 as basis

rb = integration.rb
AGENT_HINT = 'A contribution by another agent of your person awaits review.'
CREDENTIAL_HINT = 'A contribution under a worker credential of your person awaits review.'
GATE_TEXTS = ('old agent sentence', 'new credential sentence', 'old credential label without Allowed by',
              'brief label spaced and cased')


def only_its_own_tests(cls):
    for base in integration.AgentApprovalIntegration.__mro__:
        for name in dir(base):
            if name.startswith('test_') and name not in cls.__dict__:
                setattr(cls, name, None)
    return cls


@unittest.skipIf(rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@only_its_own_tests
class ApprovalBasisRealBd(integration.AgentApprovalIntegration):

    def approve_saying(self, task, contribution, summary):
        self.count += 1
        return self.request('POST', '%s/%s/reviews' % (self.tasks, task), dict(
            operation='approve', schema_version=1, operation_id='op-approve-540-%d' % self.count,
            previous=contribution, contribution=contribution, summary=summary), token=self.agent)

    def actor_of(self, who, actor=None):
        """The name bd holds as assignee when ``who`` claims."""
        task = self.new('who is %s' % who)
        claimed = self.request('POST', '%s/%s/claim' % (self.tasks, task), {'actor': actor} if actor else {},
                               token=self.tokens[who])
        self.assertEqual(200, claimed.status, claimed.data)
        return self.bd_row(task)[1]

    def hint(self, task):
        nxt = self.request('GET', '/v1/agents/me/next', token=self.agent)
        self.assertEqual(200, nxt.status, nxt.data)
        return next(a['reason'] for a in nxt.data['next_actions'] if a['kind'] == 'to-review' and a['task'] == task)

    def reassign(self, task, actor):
        self.bd('update', task, '--assignee', actor, '--json')
        self.assertEqual(actor, self.bd_row(task)[1])

    def test_a_basis_text_in_the_summary_is_refused_and_bd_gains_no_comment(self):
        task, contribution = self.deliver('egret')
        before = self.comments(task)
        for name in GATE_TEXTS:
            with self.subTest(text=name):
                refused = self.approve_saying(task, contribution, 'Looks good. ' + basis.REFUSED[name])
                self.assertEqual(422, refused.status, refused.data)
                self.assertEqual(before, self.comments(task))
                self.assertEqual('awaiting-review', self.review(task)['state'])
        approved = self.approve_saying(task, contribution, 'Looks good.')
        self.assertEqual(201, approved.status, approved.data)
        written = self.comments(task)[len(before):]
        self.assertEqual([self.agent_actor], [author for author, _ in written], written)
        self.assertIn('"approval_basis":"same-person-agent"', written[0][1])
        self.assertNotIn(basis.AGENT_SENTENCE, written[0][1])

    def test_an_agent_delivers_and_a_credential_is_assigned_the_hint_and_the_record_say_another_agent(self):
        self.worker('lane', 'lane-c')
        lane = self.actor_of('lane')
        task, contribution = self.deliver('egret')
        self.reassign(task, lane)
        self.assertTrue(self.hint(task).startswith(AGENT_HINT), self.hint(task))
        self.assertEqual((201, ''), self.route(task, contribution))
        self.assertIn('"approval_basis":"same-person-agent"', self.comments(task)[-1][1])

    def test_a_credential_delivers_and_an_agent_is_assigned_the_hint_and_the_record_say_a_worker_credential(self):
        self.worker('lane', 'lane-c')
        egret = self.actor_of('egret')
        task, contribution = self.deliver('lane')
        self.reassign(task, egret)
        self.assertTrue(self.hint(task).startswith(CREDENTIAL_HINT), self.hint(task))
        self.assertEqual((201, ''), self.route(task, contribution))
        self.assertIn('"approval_basis":"same-person-worker-credential"', self.comments(task)[-1][1])


    # -- r2 (retry-test): the three retry paths after a moved basis, and the two controls -------------------
    def approve_as(self, task, contribution, operation_id, key):
        return self.request('POST', '%s/%s/reviews' % (self.tasks, task), dict(
            operation='approve', schema_version=1, operation_id=operation_id, previous=contribution,
            contribution=contribution, summary='Accepted'), token=self.agent, key=key)

    def lost(self, call):
        """``call()`` with the endpoint's answer to the approve write lost AFTER the real write: the 503 an endpoint
        timeout after the write becomes (http_service.py:3394-3403), so the service keeps no result."""
        real = http_service.EndpointBackend._run
        armed = [True]

        def run(backend, action, *args, **kwargs):
            reply = real(backend, action, *args, **kwargs)
            if armed[0] and action == 'review' and kwargs.get('operation_id'):
                armed[0] = False
                raise http_service.HttpError(503, 'unavailable', 'The endpoint did not answer in time')
            return reply
        with mock.patch.object(http_service.EndpointBackend, '_run', run):
            answer = call()
        self.assertFalse(armed[0], 'the approve write was not reached')
        return answer

    def approvals(self, task):
        return [json.loads(text.split('\n', 1)[1]) for _author, text in self.comments(task)
                if text.startswith('Kind: contribution-review-v1') and '"operation":"approve"' in text.replace(' ', '')]

    def held_by_lane(self):
        """A task casey delivers, then held by cora's worker credential: the assignee decides worker-credential."""
        task, contribution = self.deliver('casey')
        self.reassign(task, self.lane_actor)
        return task, contribution

    def test_the_retry_paths_after_a_moved_basis_and_the_first_basis_stands(self):
        self.worker('lane', 'lane-c')
        self.lane_actor, egret = self.actor_of('lane'), self.actor_of('egret')
        first_basis = 'same-person-worker-credential'
        # 1. Same key, the service kept the answer: 201, the very same body.
        task, contribution = self.held_by_lane()
        op, key = 'op-r-' + secrets.token_hex(4), 'key-' + secrets.token_hex(6)
        first = self.approve_as(task, contribution, op, key)
        self.assertEqual(201, first.status, first.data)
        self.reassign(task, egret)                                   # the basis would now be 'agent'
        again = self.approve_as(task, contribution, op, key)
        self.assertEqual((201, first.data), (again.status, again.data))
        # 3. The same operation_id under a new key: 422, the canonical writer's refusal.
        other = self.approve_as(task, contribution, op, 'key-' + secrets.token_hex(6))
        self.assertEqual(422, other.status, other.data)
        self.assertIn('Operation ID already used with different payload or actor', json.dumps(other.data))
        self.assertEqual([first_basis], [record.get('approval_basis') for record in self.approvals(task)])
        # 2. Same key after the answer was lost: 503, then 422, the journal's refusal.
        task, contribution = self.held_by_lane()
        op, key = 'op-r-' + secrets.token_hex(4), 'key-' + secrets.token_hex(6)
        lost = self.lost(lambda: self.approve_as(task, contribution, op, key))
        self.assertEqual(503, lost.status, lost.data)
        self.reassign(task, egret)
        again = self.approve_as(task, contribution, op, key)
        self.assertEqual(422, again.status, again.data)
        self.assertIn('Operation identity reused with a different request', json.dumps(again.data))
        self.assertEqual([first_basis], [record.get('approval_basis') for record in self.approvals(task)])

    def test_the_controls_without_a_moved_basis_are_answered(self):
        self.worker('lane', 'lane-c')
        self.lane_actor = self.actor_of('lane')
        # Same key after a lost answer, nothing moved: 201 from the journal's stored answer.
        task, contribution = self.held_by_lane()
        op, key = 'op-c-' + secrets.token_hex(4), 'key-' + secrets.token_hex(6)
        self.assertEqual(503, self.lost(lambda: self.approve_as(task, contribution, op, key)).status)
        again = self.approve_as(task, contribution, op, key)
        self.assertEqual(201, again.status, again.data)
        self.assertEqual(1, len(self.approvals(task)))
        # The same operation_id under a new key, nothing moved: 201, reconciled.
        task, contribution = self.held_by_lane()
        op = 'op-c-' + secrets.token_hex(4)
        self.assertEqual(201, self.approve_as(task, contribution, op, 'key-' + secrets.token_hex(6)).status)
        again = self.approve_as(task, contribution, op, 'key-' + secrets.token_hex(6))
        self.assertEqual(201, again.status, again.data)
        self.assertTrue(again.data.get('reconciled'), again.data)
        self.assertEqual(1, len(self.approvals(task)))


if __name__ == '__main__':
    unittest.main()
