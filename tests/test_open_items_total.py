"""kittrial-5bb.435, .436: open review items are counted before the 20 cap, and both backends cap at 20.

The lists stay cut to 20 (`open_items`, `open_review_items`); the count is taken first and travels beside them
(`open_items_total`, `open_review_items_total`), and every sentence ("N items of request R", "and N more")
uses it. Both the canonical record (`review_workflow.py:2319`) and the in-process HTTP backend
(`http_service.py`) refuse a `request-changes` that would leave more than 20 unresolved items; the helpers and
the contribute advice are given 25 directly to pin the slicing boundary.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import http_service
import review_state
import review_workflow as rw
import work
from test_briefing import TASK
from test_http_service import BASE, BUNDLE
from test_http_web import TeamHarness
import test_open_items_warning as base

FIRST = ['a%d' % i for i in range(13)]
SECOND = ['b%d' % i for i in range(12)]


def pending(first=FIRST, second=SECOND):
    """A projection's pending_requests with 25 open blocking items across two requests."""
    return ([dict(request='feedback', item=i, contribution='delivery') for i in first]
            + [dict(request='feedback-2', item=i, contribution='delivery') for i in second])


class HelperTests(unittest.TestCase):
    def test_every_open_item_is_returned_for_counting(self):
        self.assertEqual(len(review_state.open_review_items(pending())), 25)

    def test_each_requests_warning_counts_all_its_items(self):
        warnings = review_state.open_items_warnings(review_state.open_review_items(pending()), TASK)
        self.assertEqual(len(warnings), 2)
        self.assertTrue(warnings[0].startswith('13 items of request feedback are still open (%s):' % ', '.join(FIRST)))
        self.assertTrue(warnings[1].startswith('12 items of request feedback-2 are still open (%s):' % ', '.join(SECOND)))

    def test_and_n_more_counts_the_true_total(self):
        items = review_state.open_review_items(pending())
        self.assertIn('a0, a1, a2, a3, a4 of request feedback and 20 more.', review_state.open_items_sentence(items, TASK))
        # A list already cut to 20 (an HTTP row) counts from the total it carries.
        self.assertIn('and 20 more.', review_state.open_items_sentence(items[:20], TASK, total=25))
        # A total that cannot be right (fewer than the list holds, or not a count) is not believed.
        for wrong in (3, None, '25', -1):
            with self.subTest(total=wrong):
                self.assertIn('and 15 more.', review_state.open_items_sentence(items[:20], TASK, total=wrong))

    def test_the_move_sentence_counts_from_the_total(self):
        items = review_state.open_review_items(pending())[:20]
        _, text = work.review_move('changes-requested', TASK, True, pending_items=25, open_items=items,
                                   open_items_total=25)
        self.assertIn(' and 20 more. Answer each with', text)


class ContributeAdviceTests(unittest.TestCase):
    def test_the_list_is_cut_to_20_and_the_total_is_25(self):
        # open_items_advice is what the CLI's review write applies; it is given 25 directly, because the canonical
        # chain refuses more than 20 unresolved items.
        answer = rw.open_items_advice({'comment_id': 'x'}, 'contribute', {'pending_requests': pending()}, TASK)
        self.assertEqual(len(answer['open_items']), 20)
        self.assertEqual(answer['open_items_total'], 25)
        self.assertEqual(answer['open_items'][-1], dict(request='feedback-2', item='b6', contribution='delivery'))
        self.assertEqual([w.split(' of ')[0] for w in answer['warnings']], ['13 items', '12 items'])

    def test_nothing_open_answers_a_zero_total(self):
        answer = rw.open_items_advice({}, 'contribute', {'pending_requests': []}, TASK)
        self.assertEqual((answer['open_items'], answer['open_items_total']), ([], 0))
        self.assertNotIn('warnings', answer)


class CanonicalBoundaryTests(base.Chain):
    def test_twenty_open_answer_a_total_of_twenty_and_a_twenty_first_is_refused(self):
        ids = ['i%d' % n for n in range(20)]
        base.add(self.data, base.request('review-1', 'delivery', ids), 'feedback', 'reviewer')
        answer = self.revise('feedback')
        self.assertEqual((len(answer['open_items']), answer['open_items_total']), (20, 20))
        self.assertTrue(answer['warnings'][0].startswith('20 items of request feedback are still open'))
        row = self.item()
        self.assertEqual((len(row['open_review_items']), row['open_review_items_total']), (20, 20))
        self.assertIn('i0, i1, i2, i3, i4 of request feedback and 15 more.', row['next'])
        # A 21st unresolved item is refused at write, as before: the canonical list is never cut.
        one_more = dict(base.request('review-2', 'delivery-2', ['x']), contribution='delivery-2')
        with self.assertRaisesRegex(ValueError, 'At most 20 unresolved review items'):
            rw.execute(self.data, TASK, 'reviewer', one_more, self.native('feedback-2'), review_writes=True)

    def test_a_row_counts_every_open_item_and_lists_20(self):
        # The canonical chain cannot hold 25, so the row is given a projection that does: the row must count
        # all of them, cut only its list, and say "and 20 more".
        base.add(self.data, base.request('review-1', 'delivery', ['i0']), 'feedback', 'reviewer')
        with patch.object(review_state, 'open_review_items', return_value=review_state.open_review_items(pending())):
            row = self.item()
        self.assertEqual((len(row['open_review_items']), row['open_review_items_total']), (20, 25))
        self.assertEqual(row['open_review_items'][-1], dict(request='feedback-2', item='b6'))
        self.assertIn('a0, a1, a2, a3, a4 of request feedback and 20 more.', row['next'])

    def test_a_malformed_row_carries_a_zero_total(self):
        self.data.append({'id': 'broken', 'malformed': True})
        rows = work.queue(self.data, base.ALICE, [])['items']
        self.assertEqual(next(r for r in rows if r['task'] == 'broken')['open_review_items_total'], 0)


class InProcessTotalTests(TeamHarness):
    """Over HTTP on the in-process backend: 20 open items answer normally and a 21st is refused (kittrial-5bb.436)."""

    def latest(self, task=None):
        tid = task or self.task
        return self.request('GET', self.path('/tasks/%s/brief' % tid), token=self.carl).data['review']['latest_id']

    def test_twenty_open_answers_normally_and_twenty_first_is_refused(self):
        self.claim(self.carl)
        first = self.contribute(self.carl)
        self.assertEqual(201, first.status, first.data)
        cid = first.data['contribution']['id']
        seven = SECOND[:7]
        for ids in (FIRST, seven):
            asked = self.review(self.olive, 'request-changes', contribution=cid,
                                previous=self.latest(), items=[{'id': i, 'text': 'Fix ' + i} for i in ids])
            self.assertEqual(201, asked.status, asked.data)
        over = self.review(self.olive, 'request-changes', contribution=cid,
                           previous=self.latest(), items=[{'id': 'b7', 'text': 'Fix b7'}])
        self.assertEqual(422, over.status, over.data)
        self.assertEqual('At most 20 unresolved review items are allowed', over.data['error']['message'])
        body = {'operation': 'contribute', 'commit': 'd' * 40, 'base_commit': BASE, 'bundle_sha256': BUNDLE,
                'summary': 'revised'}
        path = self.path('/tasks/%s/reviews' % self.task)
        revised = self.request('POST', path, body, token=self.carl, key='open-items-total-1')
        self.assertEqual(201, revised.status, revised.data)
        self.assertEqual((len(revised.data['open_items']), revised.data['open_items_total']), (20, 20))
        route = '/v1/projects/%s/tasks/%s' % (self.project, self.task)
        self.assertEqual([w.split(' of ')[0] for w in revised.data['warnings']], ['13 items', '7 items'])
        for warning in revised.data['warnings']:
            self.assertIn('POST `%s/reviews`' % route, warning)
            self.assertNotIn('--file respond.json', warning)
        self.assertIn('(%s)' % ', '.join(seven), revised.data['warnings'][1])
        again = self.request('POST', path, body, token=self.carl, key='open-items-total-1')
        self.assertEqual(again.data, revised.data)

    def test_items_answered_by_respond_do_not_count(self):
        self.claim(self.carl)
        cid = self.contribute(self.carl).data['contribution']['id']
        first_req = self.review(self.olive, 'request-changes', contribution=cid,
                                previous=self.latest(), items=[{'id': i, 'text': 'Fix ' + i} for i in FIRST])
        self.assertEqual(201, first_req.status, first_req.data)
        req_id = first_req.data['review']['id']
        second_req = self.review(self.olive, 'request-changes', contribution=cid,
                                 previous=self.latest(), items=[{'id': i, 'text': 'Fix ' + i} for i in SECOND[:7]])
        self.assertEqual(201, second_req.status, second_req.data)
        answered = self.review(
            self.carl, 'respond', contribution=cid, previous=self.latest(),
            resolutions=[{'request': req_id, 'item': i, 'reason': 'Fixed', 'evidence': 'commit'} for i in FIRST[:5]])
        self.assertEqual(201, answered.status, answered.data)
        more = self.review(self.olive, 'request-changes', contribution=cid,
                           previous=self.latest(), items=[{'id': 'c%d' % n, 'text': 'Fix c%d' % n} for n in range(5)])
        self.assertEqual(201, more.status, more.data)
        too_many = self.review(self.olive, 'request-changes', contribution=cid,
                               previous=self.latest(), items=[{'id': 'c5', 'text': 'Fix c5'}])
        self.assertEqual(422, too_many.status, too_many.data)
        self.assertEqual('At most 20 unresolved review items are allowed', too_many.data['error']['message'])

    def test_notes_never_count(self):
        self.claim(self.carl)
        cid = self.contribute(self.carl).data['contribution']['id']
        notes_req = self.review(
            self.olive, 'request-changes', contribution=cid, previous=self.latest(),
            items=[{'id': 'n%d' % n, 'text': 'Note %d' % n, 'severity': 'note'} for n in range(10)])
        self.assertEqual(201, notes_req.status, notes_req.data)
        rec = self.request('POST', self.path('/tasks/%s/reviews' % self.task), {
            'operation': 'recommend', 'contribution': cid, 'commit': 'a' * 40, 'verdict': 'approve',
            'summary': 'Looks good with notes', 'items': [{'id': 'rn%d' % n, 'text': 'Rec %d' % n} for n in range(10)]
        }, token=self.olive)
        self.assertEqual(201, rec.status, rec.data)
        blocking_20 = self.review(
            self.olive, 'request-changes', contribution=cid, previous=self.latest(),
            items=[{'id': 'blk%d' % n, 'text': 'Block %d' % n, 'severity': 'blocking'} for n in range(20)])
        self.assertEqual(201, blocking_20.status, blocking_20.data)
        more_notes = self.review(
            self.olive, 'request-changes', contribution=cid, previous=self.latest(),
            items=[{'id': 'n_after', 'text': 'Another note', 'severity': 'note'}])
        self.assertEqual(201, more_notes.status, more_notes.data)
        blocking_21 = self.review(
            self.olive, 'request-changes', contribution=cid, previous=self.latest(),
            items=[{'id': 'blk20', 'text': 'Block 20', 'severity': 'blocking'}])
        self.assertEqual(422, blocking_21.status, blocking_21.data)
        self.assertEqual('At most 20 unresolved review items are allowed', blocking_21.data['error']['message'])

    def test_an_exact_idempotent_replay_of_an_accepted_request_still_succeeds(self):
        self.claim(self.carl)
        cid = self.contribute(self.carl).data['contribution']['id']
        path = self.path('/tasks/%s/reviews' % self.task)
        body = {'operation': 'request-changes', 'operation_id': 'op-twenty', 'contribution': cid,
                'previous': self.latest(), 'items': [{'id': 'i%d' % n, 'text': 'Fix %d' % n} for n in range(20)]}
        accepted = self.request('POST', path, body, token=self.olive, key='req-twenty-key')
        self.assertEqual(201, accepted.status, accepted.data)
        replay_key = self.request('POST', path, body, token=self.olive, key='req-twenty-key')
        self.assertIn(replay_key.status, (200, 201), replay_key.data)
        self.assertEqual(accepted.data, replay_key.data)
        replay_op = self.request('POST', path, body, token=self.olive, key='req-twenty-key-2')
        self.assertIn(replay_op.status, (200, 201), replay_op.data)
        self.assertEqual(accepted.data['review'], replay_op.data['review'])
        brief = self.request('GET', self.path('/tasks/%s/brief' % self.task), token=self.carl).data
        self.assertEqual(20, brief['review']['open_requests'])

    def test_a_request_on_another_task_with_20_open_is_unaffected(self):
        self.claim(self.carl)
        cid = self.contribute(self.carl).data['contribution']['id']
        full = self.review(self.olive, 'request-changes', contribution=cid, previous=self.latest(),
                           items=[{'id': 'i%d' % n, 'text': 'Fix %d' % n} for n in range(20)])
        self.assertEqual(201, full.status, full.data)
        second = self.create_task(self.olive, self.project, 'second task').data['id']
        self.claim(self.carl, task=second)
        other_cid = self.contribute(self.carl, task=second).data['contribution']['id']
        other_req = self.review(
            self.olive, 'request-changes', task=second, contribution=other_cid,
            previous=self.latest(second), items=[{'id': 'o%d' % n, 'text': 'Other %d' % n} for n in range(20)])
        self.assertEqual(201, other_req.status, other_req.data)
        other_over = self.review(
            self.olive, 'request-changes', task=second, contribution=other_cid,
            previous=self.latest(second), items=[{'id': 'o20', 'text': 'Other 20'}])
        self.assertEqual(422, other_over.status, other_over.data)
        self.assertEqual('At most 20 unresolved review items are allowed', other_over.data['error']['message'])


class EndpointTotalTests(base.EndpointOpenItemsTests):
    """Over HTTP on the canonical binding: the total travels, and next-actions counts from it."""

    # The inherited .431 test runs once, in test_open_items_warning.
    test_a_contribute_over_http_with_open_items_answers_the_warning_and_ids = None

    def test_twenty_open_answers_normally_and_twenty_first_is_refused_over_http(self):
        old = self.review(self.secret, 'contribute', previous=None, **base.fixes.CONTRIBUTION).data['comment_id']
        first_req = self.review(self.people['blair'], 'request-changes', previous=old, contribution=old,
                                items=[{'id': i, 'text': 'Fix ' + i} for i in FIRST])
        second_req = self.review(self.people['blair'], 'request-changes', previous=first_req.data['comment_id'],
                                 contribution=old, items=[{'id': i, 'text': 'Fix ' + i} for i in SECOND[:7]])
        over = self.request('POST', self.base + '/reviews', {
            'operation': 'request-changes', 'schema_version': 1, 'operation_id': 'op-over-21',
            'previous': second_req.data['comment_id'], 'contribution': old,
            'items': [{'id': 'b7', 'text': 'Fix b7'}]}, token=self.people['blair'])
        self.assertEqual(422, over.status, over.data)
        self.assertEqual('At most 20 unresolved review items are allowed', over.data['error']['message'])
        new = self.review(self.secret, 'contribute', previous=second_req.data['comment_id'],
                          **dict(base.fixes.CONTRIBUTION, commit='d' * 40, supersedes=old))
        self.assertEqual((len(new.data['open_items']), new.data['open_items_total']), (20, 20))

    def test_the_canonical_rows_total_is_carried_type_checked(self):
        rows = [dict(request='feedback', item='i%d' % n) for n in range(20)]
        fields = http_service.EndpointBackend._attention_fields({'open_review_items': rows, 'open_review_items_total': 25})
        self.assertEqual((len(fields['open_review_items']), fields['open_review_items_total']), (20, 25))
        for wrong in (None, '25', -1, True):
            with self.subTest(total=wrong):
                self.assertIsNone(http_service.EndpointBackend._attention_fields(
                    {'open_review_items': rows, 'open_review_items_total': wrong})['open_review_items_total'])

    def test_the_total_rides_the_answer_and_the_next_actions_sentence(self):
        old = self.review(self.secret, 'contribute', previous=None, **base.fixes.CONTRIBUTION).data['comment_id']
        asked = self.review(self.people['blair'], 'request-changes', previous=old, contribution=old,
                            items=[{'id': 'fix', 'text': 'Fix it.'}, {'id': 'doc', 'text': 'Document it.'}])
        new = self.review(self.secret, 'contribute', previous=asked.data['comment_id'],
                          **dict(base.fixes.CONTRIBUTION, commit='d' * 40, supersedes=old))
        self.assertEqual(new.data['open_items_total'], 2)
        # A row whose list was cut carries the true total; next-actions counts "and N more" from it, never
        # from the list it can see.
        real = http_service.EndpointBackend._attention_fields

        def cut(row):
            fields = real(row)
            if fields.get('open_review_items'):
                fields['open_review_items_total'] = 25
            return fields
        with patch.object(http_service.EndpointBackend, '_attention_fields', staticmethod(cut)):
            data = self.request('GET', '/v1/agents/me/next', token=self.secret).data
        action = next(x for x in data['next_actions'] if x['task'] == self.task)
        self.assertIn('Open: fix, doc of request %s and 23 more.' % asked.data['comment_id'], action['next'])


if __name__ == '__main__':
    unittest.main()
