"""kittrial-5bb.435: open review items are counted before the 20 cap.

The lists stay cut to 20 (`open_items`, `open_review_items`); the count is taken first and travels beside them
(`open_items_total`, `open_review_items_total`), and every sentence ("N items of request R", "and N more")
uses it. The canonical record keeps at most 20 unresolved items (review_workflow.py: a request-changes past
that is refused), so 25 open items occur on the in-process backend, which caps each request at 20 but not
their sum; the helpers and the contribute advice are given 25 directly.
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
    """Over HTTP on the in-process backend, which can hold 25: 13 and 12 in two requests."""

    def latest(self):
        return self.request('GET', self.path('/tasks/%s/brief' % self.task), token=self.carl).data['review']['latest_id']

    def test_a_revision_with_25_open_answers_20_listed_a_total_of_25_and_true_counts(self):
        self.claim(self.carl)
        first = self.contribute(self.carl)
        self.assertEqual(201, first.status, first.data)
        for ids in (FIRST, SECOND):
            asked = self.review(self.olive, 'request-changes', contribution=first.data['contribution']['id'],
                                previous=self.latest(), items=[{'id': i, 'text': 'Fix ' + i} for i in ids])
            self.assertEqual(201, asked.status, asked.data)
        body = {'operation': 'contribute', 'commit': 'd' * 40, 'base_commit': BASE, 'bundle_sha256': BUNDLE,
                'summary': 'revised'}
        path = self.path('/tasks/%s/reviews' % self.task)
        revised = self.request('POST', path, body, token=self.carl, key='open-items-total-1')
        self.assertEqual(201, revised.status, revised.data)
        self.assertEqual((len(revised.data['open_items']), revised.data['open_items_total']), (20, 25))
        route = '/v1/projects/%s/tasks/%s' % (self.project, self.task)
        self.assertEqual([w.split(' of ')[0] for w in revised.data['warnings']], ['13 items', '12 items'])
        for warning in revised.data['warnings']:
            self.assertIn('POST `%s/reviews`' % route, warning)
            self.assertNotIn('--file respond.json', warning)
        self.assertIn('(%s)' % ', '.join(SECOND), revised.data['warnings'][1])
        again = self.request('POST', path, body, token=self.carl, key='open-items-total-1')
        self.assertEqual(again.data, revised.data)


class EndpointTotalTests(base.EndpointOpenItemsTests):
    """Over HTTP on the canonical binding: the total travels, and next-actions counts from it."""

    # The inherited .431 test runs once, in test_open_items_warning.
    test_a_contribute_over_http_with_open_items_answers_the_warning_and_ids = None

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
