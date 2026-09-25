"""Regression tests for the structured stale-previous refusal.

The pilot reported that a review write must be preceded by a read of
`latest_comment_id`, and that a stale write was refused without telling the caller
what the current value is — costing a blind extra round trip per rejected write.

These tests pin the contract added by kittrial-5bb.27:

1. A stale `previous` on any of `contribute`, `request-changes`, `respond` and
   `approve` is refused with a `StaleReviewPrevious` whose structured detail names
   the *current* head (`latest_comment_id`) and `review_state`.
2. The refusal writes nothing, and the human wording stays recognisable.
3. The retry with the returned head succeeds, so one read is enough.
"""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_workflow as w


class StaleReviewPreviousTests(unittest.TestCase):
    def setUp(self):
        self.issue = dict(id='task-1', assignee='worker', status='in_progress', comments=[])
        self.count = 0

    def payload(self, op, **extra):
        self.count += 1
        return dict(schema_version=1, operation=op, operation_id='op-'+str(self.count),
                    task='task-1', previous=w.project(self.issue)['latest_comment_id'], **extra)

    def contribution(self, **extra):
        p = dict(repository='ssh://git.example/project', commit='a'*40, base_commit='b'*40,
                 delivery=dict(kind='bundle', path='koopa:/deliveries/revision-1.bundle', sha256='c'*64),
                 summary='Implementation and test evidence',
                 supersedes=(w.project(self.issue)['contribution'] or {}).get('comment_id'))
        p.update(extra)
        return self.payload('contribute', **p)

    def run_native(self, args):
        self.assertEqual(args[:3], ['comments', 'add', 'task-1'])
        cid = str(len(self.issue['comments'])+1)
        self.issue['comments'].append(dict(id=cid, text=args[3], author='worker',
                                           created_at='2026-09-16T00:00:00Z'))
        return json.dumps({'id': cid})

    def send(self, p, actor='worker'):
        return w.execute([self.issue], 'task-1', actor, p, self.run_native)

    def chain_with_request(self):
        """contribute -> request-changes; returns the stale pre-request head."""
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-changes', contribution=contribution,
                               items=[dict(id='fix', text='Correct the edge case')]), 'reviewer')
        return contribution

    def assert_structured(self, caught, stale_previous, expected_head, expected_state):
        """The refusal exposes machine-readable head values and writes nothing."""
        exc = caught.exception
        self.assertIsInstance(exc, w.StaleReviewPrevious)
        self.assertEqual(exc.detail['code'], 'stale-previous')
        self.assertEqual(exc.detail['http_status'], 409)
        self.assertEqual(exc.detail['task'], 'task-1')
        self.assertEqual(exc.detail['supplied_previous'], stale_previous)
        self.assertEqual(exc.detail['latest_comment_id'], expected_head)
        self.assertEqual(exc.detail['review_state'], expected_state)
        # The current head is what the caller needs to retry; it must not echo the
        # stale value back as if it were current.
        self.assertEqual(exc.latest_comment_id, expected_head)
        self.assertEqual(exc.review_state, expected_state)
        lines = str(exc).splitlines()
        self.assertIn('Stale review workflow previous', lines[0])
        self.assertEqual(json.loads(lines[1]), exc.detail)
        self.assertEqual(json.loads(lines[1])['latest_comment_id'], expected_head)

    def test_contribute_with_stale_previous_returns_current_head(self):
        stale = self.chain_with_request()
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'changes-requested')
        before = copy.deepcopy(self.issue['comments'])
        # `supersedes` names the current contribution, so only `previous` is stale.
        payload = self.payload('contribute', supersedes=state['contribution']['comment_id'],
                               repository='ssh://git.example/project', commit='d'*40, base_commit='e'*40,
                               delivery=dict(kind='bundle', path='koopa:/deliveries/revision-2.bundle',
                                             sha256='f'*64),
                               summary='Second revision')
        payload['previous'] = stale
        with self.assertRaises(w.StaleReviewPrevious) as caught:
            self.send(payload)
        self.assert_structured(caught, stale, state['latest_comment_id'], 'changes-requested')
        self.assertEqual(self.issue['comments'], before, 'a stale contribute must not write')
        # One read is enough: the retry with the returned head succeeds.
        payload['previous'] = caught.exception.latest_comment_id
        payload['operation_id'] = 'op-retry-contribute'
        self.assertEqual(self.send(payload)['comment_id'], '3')

    def test_request_changes_with_stale_previous_returns_current_head(self):
        stale = self.chain_with_request()
        state = w.project(self.issue)
        before = copy.deepcopy(self.issue['comments'])
        payload = self.payload('request-changes', contribution=state['contribution']['comment_id'],
                               items=[dict(id='again', text='One more item')])
        payload['previous'] = stale
        with self.assertRaises(w.StaleReviewPrevious) as caught:
            self.send(payload, 'reviewer')
        self.assert_structured(caught, stale, state['latest_comment_id'], 'changes-requested')
        self.assertEqual(self.issue['comments'], before)
        payload['previous'] = caught.exception.latest_comment_id
        payload['operation_id'] = 'op-retry-request'
        self.send(payload, 'reviewer')
        self.assertEqual(w.project(self.issue)['review_state'], 'changes-requested')

    def test_respond_with_stale_previous_returns_current_head(self):
        stale = self.chain_with_request()
        state = w.project(self.issue)
        request = state['latest_comment_id']
        before = copy.deepcopy(self.issue['comments'])
        payload = self.payload('respond', contribution=state['contribution']['comment_id'],
                               resolutions=[dict(request=request, item='fix', reason='Fixed',
                                                 evidence='commit')])
        payload['previous'] = stale
        with self.assertRaises(w.StaleReviewPrevious) as caught:
            self.send(payload)
        self.assert_structured(caught, stale, state['latest_comment_id'], 'changes-requested')
        self.assertEqual(self.issue['comments'], before)
        payload['previous'] = caught.exception.latest_comment_id
        payload['operation_id'] = 'op-retry-respond'
        self.send(payload)
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-review')

    def test_approve_with_stale_previous_returns_current_head(self):
        contribution = self.send(self.contribution())['comment_id']
        approval = self.send(self.payload('approve', contribution=contribution,
                                         summary='Reviewed'), 'reviewer')['comment_id']
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'awaiting-integration')
        before = copy.deepcopy(self.issue['comments'])
        payload = self.payload('approve', contribution=contribution, summary='Reviewed again')
        payload['previous'] = contribution  # the head before the recorded approval
        with self.assertRaises(w.StaleReviewPrevious) as caught:
            self.send(payload, 'reviewer')
        self.assert_structured(caught, contribution, approval, 'awaiting-integration')
        self.assertEqual(self.issue['comments'], before)
        payload['previous'] = caught.exception.latest_comment_id
        payload['operation_id'] = 'op-retry-approve'
        self.send(payload, 'reviewer')
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')

    def test_stale_previous_before_any_record_reports_the_null_head(self):
        before = copy.deepcopy(self.issue['comments'])
        payload = self.payload('contribute', supersedes=None,
                               repository='ssh://git.example/project', commit='a'*40, base_commit='b'*40,
                               delivery=dict(kind='bundle', path='koopa:/deliveries/revision-1.bundle',
                                             sha256='c'*64),
                               summary='First revision')
        payload['previous'] = '1'
        with self.assertRaises(w.StaleReviewPrevious) as caught:
            self.send(payload)
        # A null head is the actionable value for the first write, not an omission.
        self.assert_structured(caught, '1', None, 'none')
        self.assertIn('no review record exists yet', str(caught.exception))
        self.assertEqual(self.issue['comments'], before)

    def test_stale_refusal_stays_a_value_error_and_keeps_the_stale_wording(self):
        """Existing callers that catch ValueError (endpoint) and read the wording
        must keep working; only the added structured detail is new."""
        self.chain_with_request()
        payload = self.payload('contribute', supersedes='1',
                               repository='ssh://git.example/project', commit='d'*40, base_commit='e'*40,
                               delivery=dict(kind='bundle', path='koopa:/deliveries/revision-2.bundle',
                                             sha256='f'*64),
                               summary='Second revision')
        payload['previous'] = '1'
        with self.assertRaises(ValueError) as caught:
            self.send(payload)
        self.assertIsInstance(caught.exception, w.StaleReviewPrevious)
        self.assertIn('Stale review workflow previous; reread brief', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
