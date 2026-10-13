"""`review`, `brief` and `work` share one integration-aware review projection.

The reported defects: raw `review` had no integrated state at all, and the
integrated overlay in `brief`/`work` consulted only the LATEST lifecycle scope,
so recording a later scope (release, live-verified) returned settled work to the
awaiting-integration queue. These fixtures pin the shared projection: the raw
workflow state stays separate as `workflow_state`, and a contribution counts as
integrated when ANY scope whose `source_commit` equals its full commit records
`integrated=passed`, regardless of scope order.

Review round 2 pinned these further:

* `lifecycle_matches_contribution` keeps its ORIGINAL newest-scope meaning; the
  ANY-scope answer is only the additive `integration.matches_contribution`.
* the any-pass-wins rule is deliberate and auditable through `newest_fact`.
* a recurring scope token keeps its NEWEST recording position.
* `project_facts` and `integration` apply the same trust rule to a tampered
  lifecycle-scope label.
* the review write receipt reports the same effective state as the reads.
* `integration_evidence` hashes each event once (linear, not quadratic).
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import briefing
import lifecycle
import review_state
import review_workflow as rw
import work
from lifecycle import integration_evidence, project_facts
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


def reviewed(commit=COMMIT, scope_commit=COMMIT, dimension='integrated', later_scope=None, approval=True):
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
    if approval:
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
        # The newest-scope field keeps its original meaning: scope D, not C.
        self.assertFalse(brief['lifecycle_matches_contribution'])
        self.assertFalse(item['lifecycle_matches_contribution'])
        self.assertTrue(review['integration']['matches_contribution'])

    def test_later_scope_recording_facts_keeps_lifecycle_match_false(self):
        """Probe 8: a newer scope D records deployed/live-verified passed."""
        store = reviewed()
        later = scope(source_commit='d' * 40, release_id='release-2', environment='production')
        store.record(payload('lifecycle-scope', operation='scope-2', scope=later))
        store.record(payload('deployed', operation='fact-deployed', scope=later))
        store.record(payload('live-verified', operation='fact-live', scope=later))
        review, brief, item = surfaces(store)
        # A client that applies the newest-scope `lifecycle` values to the current
        # contribution is warned by the unchanged meaning of this field.
        self.assertFalse(brief['lifecycle_matches_contribution'])
        self.assertFalse(item['lifecycle_matches_contribution'])
        self.assertTrue(review['integration']['matches_contribution'])
        self.assertEqual(brief['lifecycle_scope']['source_commit']['text'], 'd' * 40)
        self.assertEqual(brief['lifecycle']['deployed']['value'], 'passed')
        self.assertEqual(brief['lifecycle']['live-verified']['value'], 'passed')
        self.assertEqual(item['lifecycle']['deployed'], 'passed')
        self.assertEqual(item['lifecycle_scope']['source_commit'], 'd' * 40)
        # The effective integrated state still comes from the earlier matching scope.
        self.assertEqual(result_states(review, brief, item), ['integrated'] * 3)

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


class ProjectionEdgeCaseTests(unittest.TestCase):
    def test_newer_failed_scope_does_not_undo_an_older_pass_for_the_same_commit(self):
        """Probe 7a: any-pass-wins is deliberate and `newest_fact` exposes it."""
        store = reviewed()
        second = scope(source_commit=COMMIT, integration_commit='merge-b', release_id='release-2')
        store.record(payload('lifecycle-scope', operation='scope-2', scope=second))
        store.record(payload('integrated', 'failed', 'fact-2', scope=second,
                             evidence=['report:newer-failure']))
        review, brief, item = surfaces(store)
        block = review['integration']
        self.assertTrue(block['matches_contribution'])
        self.assertEqual(block['fact'], 'passed')
        self.assertEqual(block['integration_commit'], 'merge-a')
        self.assertEqual(block['newest_fact'], 'failed')
        self.assertEqual(result_states(review, brief, item), ['integrated'] * 3)

    def test_recurring_scope_token_reports_the_newest_recording(self):
        """Probe 6b: a re-recorded scope token must not keep its oldest position."""
        store = NativeStore()
        first = scope(source_commit=COMMIT, integration_commit='merge-a')
        other = scope(source_commit=COMMIT, integration_commit='merge-d', release_id='release-d')
        store.record(payload('lifecycle-scope', operation='scope-1', scope=first))
        store.record(payload('integrated', operation='fact-a', scope=first))
        store.record(payload('lifecycle-scope', operation='scope-2', scope=other))
        store.record(payload('integrated', operation='fact-d', scope=other))
        store.record(payload('lifecycle-scope', operation='scope-3', scope=first))
        store.rows[0]['comments'] = []
        store.rows[0]['assignee'] = 'alice/session'
        add(store.rows, contribute(COMMIT), 'delivery')
        add(store.rows, approve(), 'approval', 'reviewer')
        review, brief, item = surfaces(store)
        scopes = integration(store)
        self.assertEqual([entry['scope']['integration_commit'] for entry in scopes],
                         ['merge-a', 'merge-d'])
        self.assertEqual(scopes[0]['order'], max(entry['order'] for entry in scopes))
        self.assertEqual(review['integration']['scope']['integration_commit'], 'merge-a')
        self.assertEqual(review['integration']['scope_token'], scopes[0]['scope_token'])
        self.assertEqual(result_states(review, brief, item), ['integrated'] * 3)

    def test_tampered_scope_label_keeps_project_facts_and_integration_consistent(self):
        """Probe 9: one trust rule for the same events, decided in the projection."""
        store = NativeStore().seed('integrated')
        self.assertEqual(integration(store)[0]['integrated']['value'], 'passed')
        store.rows[0]['labels'][0] = 'lifecycle-scope:unverified'
        facts = next(r for r in project_facts(store.rows) if r['id'] == TASK)
        self.assertIsNone(facts['scope'])
        self.assertEqual(facts['facts']['integrated']['value'], 'unknown')
        self.assertIsNone(integration(store)[0]['integrated'])

    def test_approve_receipt_matches_the_reads_when_evidence_predates_approval(self):
        """The receipt must not say awaiting-integration while the reads say integrated."""
        store = reviewed(approval=False)

        def native(args):
            if args[0] == 'export':
                return '\n'.join(json.dumps(r) for r in store.rows)
            self.assertEqual(args[:3], ['comments', 'add', TASK])
            store.rows[0].setdefault('comments', []).append(
                dict(id='approval', text=args[3], author='reviewer',
                     created_at='2026-09-16T00:00:00Z'))
            return json.dumps({'id': 'approval'})

        receipt = rw.execute(store.rows, TASK, 'reviewer', approve(), native)
        review, brief, item = surfaces(store)
        self.assertEqual(receipt['review_state'], 'integrated')
        self.assertEqual(receipt['review_state'], review['review_state'])
        self.assertEqual(receipt['review_state'], brief['review']['review_state'])
        self.assertEqual(receipt['review_state'], item['review_state'])
        self.assertEqual(receipt['workflow_state'], 'awaiting-integration')
        self.assertTrue(receipt['integration']['matches_contribution'])
        self.assertEqual(receipt['integration']['fact'], 'passed')


class ProjectionCostTests(unittest.TestCase):
    def test_integration_evidence_hashes_each_event_once(self):
        """800 scopes + 800 integrated events must be linear, not quadratic."""
        count = 800
        rows = scaled_rows(count)
        calls = []
        original = lifecycle.content_hash

        def counted(value):
            calls.append(1)
            return original(value)

        lifecycle.content_hash = counted
        try:
            evidence = integration_evidence(rows)
        finally:
            lifecycle.content_hash = original
        self.assertEqual(len(evidence[0]['scopes']), count)
        self.assertEqual({entry['integrated']['value'] for entry in evidence[0]['scopes']},
                         {'passed'})
        self.assertLessEqual(len(calls), 2 * count + 50)


def scaled_rows(count):
    """`count` scopes and `count` scoped integrated events, built without writes."""
    prefix = lifecycle.PREFIX
    rows = [dict(id=TASK, title='Synthetic task', issue_type='task', status='open',
                 labels=[], assignee='alice/session')]

    def event(dimension, value, sc):
        eid = TASK + '.' + str(len(rows))
        body = payload(dimension, value, 'op-' + eid, scope=sc)
        rows.append(dict(id=eid, issue_type='event',
                         title='State change: ' + dimension + ' \u2192 ' + str(body['value']),
                         description='Set ' + dimension + ' to ' + str(body['value'])
                                     + '\n\nReason: ' + prefix + canonical_bytes(body).decode(),
                         status='closed', created_by='alice/session',
                         created_at='2026-09-15T10:08:43Z',
                         dependencies=[dict(issue_id=eid, depends_on_id=TASK, type='parent-child')]))
        return body['value']

    tokens = []
    for index in range(count):
        sc = scope(source_commit=COMMIT, integration_commit='merge-%d' % index,
                   release_id='release-%d' % index, environment='env-%d' % index)
        tokens.append(event('lifecycle-scope', 'passed', sc))
        rows[0]['labels'] = [v for v in rows[0]['labels'] if not v.startswith('lifecycle-scope:')] \
                            + ['lifecycle-scope:' + tokens[-1]]
    for index in range(count):
        sc = scope(source_commit=COMMIT, integration_commit='merge-%d' % index,
                   release_id='release-%d' % index, environment='env-%d' % index)
        event('integrated', 'passed', sc)
        rows[0]['labels'] = [v for v in rows[0]['labels'] if not v.startswith('integrated:')] \
                            + ['integrated:passed']
    add(rows, contribute(COMMIT), 'delivery')
    add(rows, approve(), 'approval', 'reviewer')
    return rows


def integration(store):
    return next(r['scopes'] for r in integration_evidence(store.rows) if r['id'] == TASK)


def result_states(review, brief, item):
    return [review['review_state'], brief['review']['review_state'], item['review_state']]


if __name__ == '__main__':
    unittest.main()
