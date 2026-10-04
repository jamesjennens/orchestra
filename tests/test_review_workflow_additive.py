"""Additive review-workflow operations (kittrial-5bb.94).

Covers the four scope items and the tolerant-reader rule:

1. withdraw/supersede by the author or a coordinator, and a closed task no longer
   offered as awaiting-review;
2. item severity (blocking|note) and requester self-resolution of their own item;
3. an optional bounded summary on request-changes;
4. first-class request-review, visible in the named reviewer's queue and closed by
   their approve or request-changes, with the self-approval refusal kept.

Every new field is optional: a chain written before these operations existed keeps
validating and reads with the additive keys empty.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_workflow as w
import work


def row(comments=None, assignee='worker', status='in_progress'):
    return dict(id='task-1', title='A task', issue_type='task', status=status,
                assignee=assignee, description='Intent', acceptance_criteria='Acceptance',
                labels=[], comments=list(comments or []))


class Base(unittest.TestCase):
    def setUp(self):
        self.issue = row()
        self.actor = 'worker'
        self.count = 0

    def payload(self, op, **extra):
        self.count += 1
        return dict(schema_version=1, operation=op, operation_id='op-' + str(self.count),
                    task='task-1', previous=w.project(self.issue)['latest_comment_id'], **extra)

    def contribution(self, **extra):
        prior = w.project(self.issue)['contribution']
        p = dict(repository='ssh://git.example/project', commit='a' * 40, base_commit='b' * 40,
                 delivery=dict(kind='bundle', path='reviewer:/deliveries/revision-1.bundle',
                               sha256='c' * 64),
                 summary='Implementation and test evidence',
                 supersedes=prior['comment_id'] if prior else None)
        p.update(extra)
        return self.payload('contribute', **p)

    def run_native(self, args):
        self.assertEqual(args[:3], ['comments', 'add', 'task-1'])
        cid = str(len(self.issue['comments']) + 1)
        self.issue['comments'].append(dict(id=cid, text=args[3], author=self.actor,
                                           created_at='2026-09-16T00:00:00Z'))
        return json.dumps({'id': cid})

    def send(self, p, actor='worker', operators=None):
        self.actor = actor
        return w.execute([self.issue], 'task-1', actor, p, self.run_native, operators=operators)

    def review_count(self):
        return len([c for c in self.issue['comments'] if c['text'].startswith(w.PREFIX)])


class OldShapeTests(Base):
    def test_records_without_the_new_fields_still_parse_and_read_empty_additions(self):
        contribution = self.send(self.contribution())['comment_id']
        # An item written before severity existed has exactly {id, text}.
        self.send(self.payload('request-changes', contribution=contribution,
                               items=[dict(id='fix', text='Correct the edge case')]), 'reviewer')
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'changes-requested')
        # Absent severity defaults to blocking, so the item still holds up approval.
        self.assertEqual(state['pending_requests'][0]['severity'], 'blocking')
        # Every additive key is present and empty on an old-shaped chain.
        self.assertEqual(state['note_requests'], [])
        self.assertEqual(state['pending_review_requests'], [])
        self.assertIsNone(state['withdrawal'])

    def test_validate_accepts_legacy_item_and_rejects_bad_severity(self):
        base = dict(schema_version=1, operation='request-changes', operation_id='op-validate',
                    task='task-1', previous=None, contribution='1',
                    items=[dict(id='fix', text='Fix')])
        w.validate(base, 'task-1')
        bad = dict(base, items=[dict(id='fix', text='Fix', severity='blocker')])
        with self.assertRaisesRegex(ValueError, 'severity'):
            w.validate(bad, 'task-1')


class WithdrawTests(Base):
    def test_author_withdraw_clears_review_and_keeps_the_record(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-changes', contribution=contribution,
                               items=[dict(id='fix', text='Fix')]), 'reviewer')
        self.assertEqual(w.project(self.issue)['review_state'], 'changes-requested')
        receipt = self.send(self.payload('withdraw', contribution=contribution,
                                         reason='Re-scoped; a different change is needed'))
        self.assertEqual(receipt['review_state'], 'withdrawn')
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'withdrawn')
        self.assertEqual(state['pending_requests'], [])
        self.assertEqual(state['withdrawal']['reason'], 'Re-scoped; a different change is needed')
        self.assertEqual(state['withdrawal']['disposition'], 'withdrawn')
        # The whole chain stays in history: contribute + request-changes + withdraw.
        self.assertEqual(self.review_count(), 3)

    def test_withdraw_refuses_a_stranger_and_allows_a_coordinator(self):
        contribution = self.send(self.contribution())['comment_id']
        with self.assertRaisesRegex(ValueError, 'contribution author or a configured coordinator'):
            self.send(self.payload('withdraw', contribution=contribution, reason='no'), 'stranger')
        receipt = self.send(self.payload('withdraw', contribution=contribution,
                                         reason='Superseded by a re-scope', disposition='superseded'),
                            'coordinator', operators=('coordinator',))
        self.assertEqual(receipt['review_state'], 'superseded')
        self.assertEqual(w.project(self.issue)['withdrawal']['disposition'], 'superseded')

    def test_withdraw_refuses_a_bad_disposition_and_a_stale_contribution(self):
        contribution = self.send(self.contribution())['comment_id']
        with self.assertRaisesRegex(ValueError, 'disposition'):
            self.send(self.payload('withdraw', contribution=contribution, reason='x',
                                   disposition='cancelled'))
        with self.assertRaisesRegex(ValueError, 'current contribution'):
            self.send(self.payload('withdraw', contribution='unknown', reason='x'))

    def test_a_new_contribution_restarts_review_after_a_withdraw(self):
        first = self.send(self.contribution())['comment_id']
        self.send(self.payload('withdraw', contribution=first, reason='Restart'))
        second = self.send(self.contribution())['comment_id']
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertIsNone(state['withdrawal'])
        self.assertEqual(state['contribution']['comment_id'], second)


class SeverityTests(Base):
    def test_a_note_never_blocks_approval_but_a_blocking_item_does(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-changes', contribution=contribution, summary='One note',
                               items=[dict(id='n1', text='Consider a clearer name', severity='note')]),
                  'reviewer')
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertEqual(state['pending_requests'], [])
        self.assertEqual([i['item'] for i in state['note_requests']], ['n1'])
        # A note does not hold up approval.
        self.send(self.payload('approve', contribution=contribution, summary='Approved'), 'reviewer2')
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')

    def test_a_blocking_item_holds_up_approval(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-changes', contribution=contribution,
                               items=[dict(id='b1', text='Must be fixed', severity='blocking')]),
                  'reviewer')
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            self.send(self.payload('approve', contribution=contribution, summary='Approved'),
                      'reviewer2')
        self.assertEqual(self.review_count(), 2)

    def test_requester_resolves_their_own_item_and_others_cannot(self):
        contribution = self.send(self.contribution())['comment_id']
        request = self.send(self.payload('request-changes', contribution=contribution,
                                         items=[dict(id='fix', text='Fix')]), 'reviewer')['comment_id']
        with self.assertRaisesRegex(ValueError, 'requester'):
            self.send(self.payload('resolve-item', contribution=contribution, request=request,
                                   item='fix', reason='nope'), 'worker')
        self.assertEqual(w.project(self.issue)['review_state'], 'changes-requested')
        self.send(self.payload('resolve-item', contribution=contribution, request=request,
                               item='fix', reason='Not reproducible on this branch'), 'reviewer')
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertEqual(state['pending_requests'], [])

    def test_requester_can_downgrade_a_blocking_item_to_a_note(self):
        contribution = self.send(self.contribution())['comment_id']
        request = self.send(self.payload('request-changes', contribution=contribution,
                                         items=[dict(id='fix', text='Fix', severity='blocking')]),
                            'reviewer')['comment_id']
        self.send(self.payload('resolve-item', contribution=contribution, request=request,
                               item='fix', reason='Lower priority than the release', disposition='note'),
                  'reviewer')
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertEqual(state['pending_requests'], [])
        self.assertEqual([i['item'] for i in state['note_requests']], ['fix'])


class RequestChangesSummaryTests(Base):
    def test_summary_is_optional_and_bounded(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-changes', contribution=contribution,
                               summary='The reasoning the reviewer wants linked',
                               items=[dict(id='fix', text='Fix')]), 'reviewer')
        self.assertEqual(w.project(self.issue)['pending_requests'][0]['text'], 'Fix')
        with self.assertRaises(ValueError):
            self.send(self.payload('request-changes', contribution=contribution, summary='x' * 1201,
                                   items=[dict(id='other', text='Fix')]), 'reviewer')


class RequestReviewTests(Base):
    def test_pending_request_is_visible_in_the_reviewers_queue_and_closed_by_review(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-review', contribution=contribution, reviewer='alice',
                               summary='Please review the boundary handling'))
        state = w.project(self.issue)
        self.assertEqual([r['reviewer'] for r in state['pending_review_requests']], ['alice'])
        self.assertEqual(state['review_state'], 'awaiting-review')
        # The task shows in the NAMED reviewer's queue, not in another actor's.
        page = work.queue([self.issue], 'alice', ['--mine'])
        self.assertEqual([i['task'] for i in page['items']], ['task-1'])
        self.assertEqual(work.queue([self.issue], 'bob', ['--mine'])['total'], 0)
        # The reviewer closes their own request by requesting changes.
        self.send(self.payload('request-changes', contribution=contribution,
                               items=[dict(id='fix', text='Fix')]), 'alice')
        self.assertEqual(w.project(self.issue)['pending_review_requests'], [])

    def test_request_review_refuses_the_contribution_author_and_a_stranger(self):
        contribution = self.send(self.contribution())['comment_id']
        with self.assertRaisesRegex(ValueError, 'reviewer other than the contribution author'):
            self.send(self.payload('request-review', contribution=contribution, reviewer='worker'))
        with self.assertRaisesRegex(ValueError, 'assigned owner or a configured coordinator'):
            self.send(self.payload('request-review', contribution=contribution, reviewer='alice'),
                      'stranger')

    def test_request_review_is_closed_by_the_named_reviewers_approval(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-review', contribution=contribution, reviewer='alice'))
        self.send(self.payload('approve', contribution=contribution, summary='Reviewed'), 'alice')
        state = w.project(self.issue)
        self.assertEqual(state['pending_review_requests'], [])
        self.assertEqual(state['review_state'], 'awaiting-integration')


class ClosedQueueTests(Base):
    def _reviewed(self):
        contribution = self.send(self.contribution())['comment_id']
        self.issue['comments'].append(dict(id='ck', author='worker', created_at='now',
                                           text='Kind: task-checkpoint-v1\n{"summary":"Ready"}'))
        return contribution

    def test_closing_a_task_clears_its_awaiting_review_queue_entry(self):
        self._reviewed()
        self.issue['status'] = 'closed'
        self.assertEqual(work.queue([self.issue], 'worker', ['--mine'])['total'], 0)
        self.assertEqual(work.queue([self.issue], 'worker', ['--mine', '--state', 'awaiting-review'])['total'], 0)
        # The record stays in history and stays readable.
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-review')

    def test_closed_task_with_requested_changes_stays_discoverable(self):
        contribution = self._reviewed()
        self.send(self.payload('request-changes', contribution=contribution,
                               items=[dict(id='fix', text='Fix')]), 'reviewer')
        self.issue['status'] = 'closed'
        page = work.queue([self.issue], 'worker', ['--mine'])
        self.assertEqual([i['review_state'] for i in page['items']], ['changes-requested'])


if __name__ == '__main__':
    unittest.main()
