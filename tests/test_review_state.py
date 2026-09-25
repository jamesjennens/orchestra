"""`review`, `brief` and `work` share one integration-aware review projection.

The reported defects: raw `review` had no integrated state at all, and the
integrated overlay in `brief`/`work` consulted only the LATEST lifecycle scope,
so recording a later scope (release, live-verified) returned settled work to the
awaiting-integration queue. These fixtures pin the shared projection: the raw
workflow state stays separate as `workflow_state`, and a contribution counts as
integrated when ANY scope whose `source_commit` equals its full commit records
`integrated=passed`, regardless of scope order.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import briefing
import review_state
import review_workflow as rw
import work
from requirements import canonical_bytes
from test_briefing import PROJECT, TASK
from test_lifecycle import NativeStore, payload, scope

COMMIT = 'a' * 40
OTHER = 'e' * 40


def add(data, p, cid, author='alice/session'):
    data[0].setdefault('comments', []).append(
        dict(id=cid, author=author, created_at='2026-09-16T00:00:00Z',
             text=rw.PREFIX + canonical_bytes(p).decode()))


def contribute(commit=COMMIT, cid='delivery', opid='send-1'):
    return dict(schema_version=1, operation='contribute', operation_id=opid, task=TASK,
                previous=None, repository='ssh://git/example', commit=commit,
                base_commit='b' * 40, delivery=dict(kind='bundle', path='server:/rev.bundle',
                sha256='c' * 64), summary='Contribution delivered', supersedes=None)


def approve(cid='delivery', opid='approve-1'):
    return dict(schema_version=1, operation='approve', operation_id=opid, task=TASK,
                previous=cid, contribution=cid, summary='Reviewed and approved')


def runner(rows):
    return lambda args: '\n'.join(json.dumps(r) for r in rows)


def reviewed(commit=COMMIT, scope_commit=COMMIT, dimension='integrated', later_scope=None):
    """Task with an approved contribution and scoped integration evidence."""
    store = NativeStore()
    seeded = scope(source_commit=scope_commit)
    store.record(payload('lifecycle-scope', operation='scope-1', scope=seeded))
    store.record(payload(dimension, operation='fact-' + dimension, scope=seeded))
    if later_scope is not None:
        store.record(payload('lifecycle-scope', operation='scope-2', scope=later_scope))
    store.rows[0]['comments'] = []
    store.rows[0]['assignee'] = 'alice/session'
    add(store.rows, contribute(commit), 'delivery')
    add(store.rows, approve(), 'approval', 'reviewer')
    return store


def surfaces(store):
    """The three read surfaces under test, as the CLI exposes them."""
    review = work.execute(Path('.'), 'alice/session', 'review', [TASK], {}, runner(store.rows))
    brief = briefing.brief(store.rows, PROJECT, TASK)
    item = work.queue(store.rows, 'alice/session', ['--mine'])['items'][0]
    return review, brief, item


class SharedProjectionTests(unittest.TestCase):
    def test_review_brief_and_work_agree_on_integrated(self):
        review, brief, item = surfaces(reviewed())
        self.assertEqual(review['review_state'], 'integrated')
        self.assertEqual(brief['review']['review_state'], 'integrated')
        self.assertEqual(item['review_state'], 'integrated')
        self.assertEqual(result_states(review, brief, item), ['integrated'] * 3)

    def test_raw_workflow_state_stays_separate_and_additive(self):
        review, brief, item = surfaces(reviewed())
        self.assertEqual(review['workflow_state'], 'awaiting-integration')
        self.assertEqual(brief['review']['workflow_state'], 'awaiting-integration')
        self.assertEqual(item['workflow_state'], 'awaiting-integration')
        for block in (review['integration'], brief['review']['integration'], item['integration']):
            self.assertEqual(block['fact'], 'passed')
            self.assertEqual(block['source_commit'], COMMIT)
            self.assertEqual(block['integration_commit'], 'merge-a')
            self.assertTrue(block['matches_contribution'])
            self.assertEqual(block['scope']['source_commit'], COMMIT)

    def test_awaiting_integration_when_no_scope_matches_the_contribution(self):
        review, brief, item = surfaces(reviewed(commit=OTHER))
        self.assertEqual(result_states(review, brief, item), ['awaiting-integration'] * 3)
        self.assertFalse(review['integration']['matches_contribution'])
        self.assertEqual(review['integration']['fact'], 'unknown')
        self.assertIsNone(review['integration']['scope'])

    def test_later_scope_does_not_return_settled_work_to_the_queue(self):
        later = scope(source_commit='d' * 40, release_id='release-2', environment='production')
        store = reviewed(later_scope=later)
        review, brief, item = surfaces(store)
        self.assertEqual(result_states(review, brief, item), ['integrated'] * 3)
        # The newest-scope lifecycle view still reports unknown; the shared
        # projection is what makes the earlier matching scope authoritative.
        self.assertEqual(brief['lifecycle']['integrated']['value'], 'unknown')
        self.assertEqual(item['lifecycle']['integrated'], 'unknown')
        self.assertEqual(brief['lifecycle_scope']['source_commit']['text'], 'd' * 40)
        self.assertEqual(review['integration']['source_commit'], COMMIT)
        self.assertTrue(brief['lifecycle_matches_contribution'])

    def test_scope_order_does_not_change_the_integration_answer(self):
        seeded = scope(source_commit=COMMIT)
        other = scope(source_commit='d' * 40)
        first = NativeStore()
        first.record(payload('lifecycle-scope', operation='s1', scope=seeded))
        first.record(payload('integrated', operation='i1', scope=seeded))
        first.record(payload('lifecycle-scope', operation='s2', scope=other))
        second = NativeStore()
        second.record(payload('lifecycle-scope', operation='s1', scope=other))
        second.record(payload('lifecycle-scope', operation='s2', scope=seeded))
        second.record(payload('integrated', operation='i1', scope=seeded))
        for store in (first, second):
            block = review_state.integration(contribute(), integration(store))
            self.assertTrue(block['matches_contribution'])
            self.assertEqual(block['fact'], 'passed')
            self.assertEqual(block['source_commit'], COMMIT)
        self.assertEqual(integration(first)[0]['scope']['source_commit'], 'd' * 40)

    def test_newer_manual_assertion_cannot_reuse_an_older_structured_pass(self):
        store = reviewed()
        store.run(['set-state', TASK, 'integrated=pending', '--reason', 'manual change', '--json'])
        self.assertIsNone(integration(store)[0]['integrated'])
        review, brief, item = surfaces(store)
        self.assertEqual(result_states(review, brief, item), ['awaiting-integration'] * 3)
        self.assertTrue(review['integration']['matches_contribution'])
        self.assertEqual(review['integration']['fact'], 'unknown')

    def test_no_contribution_keeps_scope_match_unknown(self):
        store = NativeStore().seed('integrated')
        store.rows[0]['comments'] = []
        store.rows[0]['assignee'] = 'alice/session'
        review, brief, item = surfaces(store)
        self.assertEqual(result_states(review, brief, item), ['none'] * 3)
        self.assertIsNone(brief['lifecycle_matches_contribution'])
        self.assertIsNone(item['lifecycle_matches_contribution'])
        self.assertFalse(review['integration']['matches_contribution'])


def integration(store):
    from lifecycle import integration_evidence
    return next(r['scopes'] for r in integration_evidence(store.rows) if r['id'] == TASK)


def result_states(review, brief, item):
    return [review['review_state'], brief['review']['review_state'], item['review_state']]


if __name__ == '__main__':
    unittest.main()
