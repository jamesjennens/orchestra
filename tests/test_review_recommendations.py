"""A reviewer's recommendation: a record beside the review chain, never a decision (kittrial-5bb.115)."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reserved_comments
import review_recommendations as rec
import review_workflow as w
import work
from requirements import canonical_bytes

TASK = 'task-1'
COMMIT = 'a' * 40


class Harness(unittest.TestCase):
    def setUp(self):
        self.issue = dict(id=TASK, title='A task', assignee='worker', status='in_progress', issue_type='task',
                          comments=[])
        self.actor = 'worker'
        self.count = 0
        self.clock = 0

    def run_native(self, args):
        self.assertEqual(args[:3], ['comments', 'add', TASK])
        self.clock += 1
        cid = 'c%d' % (len(self.issue['comments']) + 1)
        self.issue['comments'].append(dict(id=cid, text=args[3], author=self.actor,
                                           created_at='2026-10-04T00:00:%02dZ' % self.clock))
        return json.dumps({'id': cid, 'created_at': '2026-10-04T00:00:%02dZ' % self.clock})

    def chain(self, operation, actor, **extra):
        self.count += 1
        self.actor = actor
        payload = dict(schema_version=1, operation=operation, operation_id='op-%d' % self.count, task=TASK,
                       previous=w.project(self.issue)['latest_comment_id'], **extra)
        return w.execute([self.issue], TASK, actor, payload, self.run_native)['comment_id']

    def contribute(self, commit=COMMIT, actor='worker'):
        current = w.project(self.issue)['contribution']
        return self.chain('contribute', actor, repository='ssh://git.example/project', commit=commit,
                          base_commit='b' * 40, summary='Delivered',
                          delivery=dict(kind='remote', remote='ssh://git.example/project', branch='fix'),
                          supersedes=current['comment_id'] if current else None)

    def payload(self, contribution, **changes):
        self.count += 1
        payload = dict(schema_version=1, operation='recommend', operation_id='rec-%d' % self.count, task=TASK,
                       contribution=contribution, commit=COMMIT, verdict='approve',
                       summary='Read the diff and ran the tests; did not check the docs.', items=[])
        payload.update(changes)
        return payload

    def recommend(self, contribution, actor='reviewer', **changes):
        self.actor = actor
        return rec.execute([self.issue], TASK, actor, self.payload(contribution, **changes), self.run_native)

    def view(self):
        return work.workflow(self.issue)


class WriteRuleTests(Harness):
    def test_a_recommendation_is_recorded_beside_the_chain_and_changes_nothing_in_it(self):
        contribution = self.contribute()
        before = w.project(self.issue)
        result = self.recommend(contribution, items=[dict(id='naming', text='Consider a clearer name.')])
        self.assertFalse(result['reconciled'])
        stored = self.issue['comments'][-1]
        self.assertTrue(stored['text'].startswith('Kind: review-recommendation-v1\n'))
        self.assertEqual(json.loads(stored['text'].split('\n', 1)[1])['verdict'], 'approve')
        after = w.project(self.issue)
        # The chain is exactly what it was: same head, same state, same pending items.
        for key in ('latest_comment_id', 'review_state', 'pending_requests', 'note_requests', 'contribution'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after['latest_comment_id'], contribution)
        view = self.view()
        self.assertTrue(view['recommended'])
        self.assertEqual(view['recommendation'], {
            'comment_id': stored['id'], 'author': 'reviewer', 'timestamp': stored['created_at'],
            'contribution': contribution, 'commit': COMMIT, 'verdict': 'approve',
            'summary': 'Read the diff and ran the tests; did not check the docs.',
            'items': [{'id': 'naming', 'text': 'Consider a clearer name.'}]})
        # The newest few are listed in full (FULL_MAX), so a reader that leaves one out has the next.
        self.assertEqual(view['recommendations'], [view['recommendation']])
        self.assertEqual(result['recommendation'], view['recommendation'])

    def test_it_is_never_an_approval_and_never_makes_an_approve_stale(self):
        contribution = self.contribute()
        self.recommend(contribution)
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-review')
        # The owner approves naming the contribution as `previous`, as before the recommendation.
        self.actor = 'owner'
        approved = w.execute([self.issue], TASK, 'owner', dict(
            schema_version=1, operation='approve', operation_id='approve-1', task=TASK, previous=contribution,
            contribution=contribution, summary='Accepted'), self.run_native)
        self.assertEqual(approved['review_state'], 'awaiting-integration')
        # A recommendation is not accepted as an approval by the chain's own writer either.
        with self.assertRaises(ValueError):
            w.execute([self.issue], TASK, 'reviewer', self.payload(contribution), self.run_native)

    def test_nobody_recommends_their_own_contribution(self):
        contribution = self.contribute()
        before = len(self.issue['comments'])
        for actor in ('worker', 'Worker', 'worker/sub-agent', ' worker '):
            with self.subTest(actor=actor), self.assertRaisesRegex(ValueError, 'Nobody recommends their own'):
                self.recommend(contribution, actor=actor)
        # The task's assignee is refused too, when the contribution's author was someone else.
        self.issue['assignee'] = 'new-owner'
        with self.assertRaisesRegex(ValueError, 'Nobody recommends their own'):
            self.recommend(contribution, actor='new-owner')
        with self.assertRaisesRegex(ValueError, 'Nobody recommends their own'):
            self.recommend(contribution, actor='worker')
        self.assertEqual(len(self.issue['comments']), before)

    def test_only_the_current_contribution_while_it_awaits_review(self):
        with self.assertRaisesRegex(ValueError, 'no contribution to recommend'):
            self.recommend('c9')
        first = self.contribute()
        with self.assertRaisesRegex(ValueError, 'not the task\'s current contribution'):
            self.recommend('c9')
        with self.assertRaisesRegex(ValueError, 'not the current contribution\'s commit'):
            self.recommend(first, commit='e' * 40)
        request = self.chain('request-changes', 'owner', contribution=first, items=[dict(id='fix', text='Fix it')])
        with self.assertRaisesRegex(ValueError, 'reads changes-requested'):
            self.recommend(first)
        self.chain('respond', 'worker', contribution=first,
                   resolutions=[dict(request=request, item='fix', reason='Fixed', evidence='commit')])
        self.assertFalse(self.recommend(first)['reconciled'])
        self.chain('approve', 'owner', contribution=first, summary='Accepted')
        with self.assertRaisesRegex(ValueError, 'reads awaiting-integration'):
            self.recommend(first)
        self.issue['status'] = 'closed'
        with self.assertRaisesRegex(ValueError, 'closed'):
            self.recommend(first)

    def test_an_exact_retry_reconciles_and_a_changed_one_is_refused(self):
        contribution = self.contribute()
        payload = self.payload(contribution)
        self.actor = 'reviewer'
        first = rec.execute([self.issue], TASK, 'reviewer', copy.deepcopy(payload), self.run_native)
        count = len(self.issue['comments'])
        again = rec.execute([self.issue], TASK, 'reviewer', copy.deepcopy(payload), self.run_native)
        self.assertEqual((again['reconciled'], again['comment_id'], len(self.issue['comments'])),
                         (True, first['comment_id'], count))
        with self.assertRaisesRegex(ValueError, 'Operation ID already used'):
            rec.execute([self.issue], TASK, 'reviewer', dict(payload, summary='Something else'), self.run_native)
        with self.assertRaisesRegex(ValueError, 'Operation ID already used'):
            rec.execute([self.issue], TASK, 'someone-else', copy.deepcopy(payload), self.run_native)

    def test_the_record_shape_and_its_limits(self):
        contribution = self.contribute()
        good = self.payload(contribution, summary='x' * 1200,
                            items=[dict(id='n%d' % index, text='y' * 1000) for index in range(10)])
        rec.validate(good, TASK)
        refused = {
            'another verdict': dict(verdict='reject'), 'no verdict': dict(verdict=None),
            'a long summary': dict(summary='x' * 1201), 'an empty summary': dict(summary='  '),
            'a summary that is not text': dict(summary=7), 'too many notes': dict(
                items=[dict(id='n%d' % index, text='t') for index in range(21)]),
            'a long note': dict(items=[dict(id='n', text='y' * 1001)]),
            'a note with a severity': dict(items=[dict(id='n', text='t', severity='blocking')]),
            'duplicate note ids': dict(items=[dict(id='n', text='a'), dict(id='n', text='b')]),
            'notes that are not a list': dict(items='none'), 'a short commit': dict(commit='abc'),
            'another task': dict(task='task-2'), 'another operation': dict(operation='approve'),
            'a bad version': dict(schema_version=2), 'an extra field': dict(previous=None),
            'a bad contribution id': dict(contribution='not an id'),
        }
        for label, changes in refused.items():
            with self.subTest(refused=label), self.assertRaises(ValueError):
                rec.validate(dict(good, **changes), TASK)
        missing = dict(good); del missing['items']
        with self.assertRaises(ValueError):
            rec.validate(missing, TASK)
        with self.assertRaisesRegex(ValueError, r'summary: 1201 characters, the limit is 1200'):
            rec.validate(dict(good, summary='x' * 1201), TASK)
        with self.assertRaisesRegex(ValueError, r'items\[1\] \(id b\) text: 1001 characters, the limit is 1000'):
            rec.validate(dict(good, items=[dict(id='a', text='ok'), dict(id='b', text='y' * 1001)]), TASK)
        # Literal limits, as the help states them.
        self.assertEqual((rec.SUMMARY_MAX, rec.ITEM_TEXT_MAX, rec.ITEMS_MAX, rec.READ_MAX), (1200, 1000, 20, 20))

    def test_the_plain_text_rule_applies_to_the_summary_and_the_notes(self):
        contribution = self.contribute()
        before = len(self.issue['comments'])
        for label, changes in (('summary', dict(summary='Looks fine‮')), ('summary', dict(summary='ok\x1b[31m')),
                               ('note', dict(items=[dict(id='n', text='hidden​text')]))):
            with self.subTest(field=label), self.assertRaisesRegex(ValueError, 'plain text'):
                self.recommend(contribution, **changes)
        self.assertEqual(len(self.issue['comments']), before)


class ReaderRuleTests(Harness):
    def test_a_new_revision_clears_it_without_any_write(self):
        first = self.contribute()
        self.recommend(first)
        self.assertTrue(self.view()['recommended'])
        second = self.contribute(commit='d' * 40)
        view = self.view()
        self.assertEqual((view['recommended'], view['recommendation'], view['recommendations']), (False, None, []))
        self.assertEqual(view['contribution']['comment_id'], second)
        # The old record is still in the task, unchanged; it just no longer stands.
        self.assertEqual(len(rec._records(self.issue)[0]), 1)

    def test_a_later_decision_makes_it_lapse_and_a_new_one_can_follow(self):
        contribution = self.contribute()
        self.recommend(contribution)
        request = self.chain('request-changes', 'owner', contribution=contribution, items=[dict(id='fix', text='Fix')])
        self.assertFalse(self.view()['recommended'])
        self.chain('respond', 'worker', contribution=contribution,
                   resolutions=[dict(request=request, item='fix', reason='Fixed', evidence='commit')])
        # Back to awaiting review: the recommendation written BEFORE the request does not come back.
        view = self.view()
        self.assertEqual((view['review_state'], view['recommended']), ('awaiting-review', False))
        self.recommend(contribution, actor='second-reviewer')
        self.assertEqual([entry['author'] for entry in self.view()['recommendations']], ['second-reviewer'])

    def test_the_newest_by_one_actor_replaces_their_earlier_one(self):
        contribution = self.contribute()
        self.recommend(contribution, summary='First reading.')
        self.recommend(contribution, actor='other', summary='Another reviewer.')
        self.recommend(contribution, summary='Second reading, after running it.')
        view = self.view()
        self.assertEqual([(entry['author']) for entry in view['recommendations']], ['reviewer', 'other'])
        self.assertEqual(view['recommendation']['summary'], 'Second reading, after running it.')
        self.assertEqual(len(rec._records(self.issue)[0]), 3)
        # `Reviewer/sub` is the same actor by the name rule and replaces it too.
        self.recommend(contribution, actor='Reviewer/sub', summary='Third.')
        self.assertEqual([entry['author'] for entry in self.view()['recommendations']], ['Reviewer/sub', 'other'])

    def test_at_most_twenty_are_read(self):
        contribution = self.contribute()
        for index in range(25):
            self.recommend(contribution, actor='reviewer-%02d' % index)
        view = self.view()
        self.assertEqual(len(view['recommendations']), 20)
        self.assertEqual(view['recommendation']['author'], 'reviewer-24')
        # Five in full, newest first; the rest by name and time only.
        self.assertEqual([sorted(entry) == ['author', 'comment_id', 'timestamp'] for entry in view['recommendations']],
                         [False] * rec.FULL_MAX + [True] * (20 - rec.FULL_MAX))
        self.assertEqual([entry['author'] for entry in view['recommendations']][:6],
                         ['reviewer-%02d' % index for index in range(24, 18, -1)])
        self.assertEqual(view['recommendations'][1]['summary'], view['recommendation']['summary'])

    def test_a_record_with_a_hidden_character_is_never_displayed_whoever_wrote_it(self):
        """The reader holds a record to the plain-text rule, as the writer does (review of the first delivery)."""
        contribution = self.contribute()
        for index, summary in enumerate(('escape \x1b[31m here', 'a bidi \u202e override', 'a zero\u200bwidth space')):
            with self.subTest(summary=ascii(summary)):
                payload = self.payload(contribution, summary=summary)
                rec.validate(payload, TASK)                      # the shape is fine: only the text rule fails
                self.issue['comments'].append(dict(
                    id='raw%d' % index, author='independent-%d' % index, created_at='2026-10-04T00:01:00Z',
                    text=rec.PREFIX + canonical_bytes(payload).decode()))
                view = self.view()
                self.assertEqual((view['recommended'], view['recommendation'], view['recommendations']),
                                 (False, None, []))
        note = self.payload(contribution, items=[{'id': 'n', 'text': 'hidden\u2066 text'}])
        self.issue['comments'].append(dict(id='raw9', author='independent-9', created_at='2026-10-04T00:01:00Z',
                                           text=rec.PREFIX + canonical_bytes(note).decode()))
        self.assertFalse(self.view()['recommended'])
        # A clean one beside them is shown.
        self.recommend(contribution, actor='honest')
        self.assertEqual([entry['author'] for entry in self.view()['recommendations']], ['honest'])

    def test_a_reassignment_does_not_hide_an_honest_recommendation(self):
        """The assignee is refused at write time and not re-checked on read; the author always is."""
        contribution = self.contribute()
        self.recommend(contribution, actor='reviewer')
        self.issue['assignee'] = 'reviewer'                     # reassigned to someone who had already recommended
        view = self.view()
        self.assertEqual([entry['author'] for entry in view['recommendations']], ['reviewer'])
        # Writing one as the assignee is still refused.
        with self.assertRaises(ValueError) as caught:
            self.recommend(contribution, actor='reviewer')
        self.assertIn('Nobody recommends their own contribution', str(caught.exception))
        # And a record by the contribution's AUTHOR never displays, however it got there.
        forged = self.payload(contribution)
        self.issue['comments'].append(dict(id='forged', author='worker', created_at='2026-10-04T00:02:00Z',
                                           text=rec.PREFIX + canonical_bytes(forged).decode()))
        self.assertEqual([entry['author'] for entry in self.view()['recommendations']], ['reviewer'])

    def test_the_work_row_names_who_delivered(self):
        contribution = self.contribute()
        self.recommend(contribution)
        item = work.queue([self.issue], 'owner', [])['items'][0]
        self.assertEqual((item['contribution_author'], item['recommended_by']), ('worker', ['reviewer']))
        for name in ('recommended', 'recommended_by', 'contribution_author'):       # and the help lists them
            self.assertIn(name, work.help_payload('work')['output']['item_fields'])

    def test_a_raw_comment_with_the_prefix_never_displays_unless_it_passes_every_rule(self):
        # What a kit that did not reserve the prefix, or a host write, could leave behind.
        contribution = self.contribute()
        good = self.payload(contribution)

        def raw(author, payload, canonical=True, position=None):
            text = rec.PREFIX + (rec.canonical_bytes(payload).decode() if canonical else json.dumps(payload, indent=1))
            self.clock += 1
            comment = dict(id='raw%d' % self.clock, text=text, author=author,
                           created_at='2026-10-04T00:01:%02dZ' % self.clock)
            self.issue['comments'].insert(len(self.issue['comments']) if position is None else position, comment)
            return comment

        cases = {
            'by the contribution\'s own author': ('worker', good, True, None),
            'by the same actor under a sub name': ('Worker/x', good, True, None),
            'naming another contribution': ('reviewer', dict(good, contribution='c99'), True, None),
            'naming another commit': ('reviewer', dict(good, commit='e' * 40), True, None),
            'not the canonical bytes': ('reviewer', good, False, None),
            'an unknown field': ('reviewer', dict(good, approved=True), True, None),
            'another verdict': ('reviewer', dict(good, verdict='approved'), True, None),
            'written before the contribution': ('reviewer', dict(good, operation_id='early'), True, 0),
            'no author': ('', dict(good, operation_id='anon'), True, None),
        }
        for label, (author, payload, canonical, position) in cases.items():
            with self.subTest(raw=label):
                comment = raw(author, payload, canonical, position)
                view = self.view()
                self.assertEqual((view['recommended'], view['recommendation']), (False, None), label)
                self.issue['comments'].remove(comment)
        for text in ('Kind: review-recommendation-v1\nnot json', 'Kind: review-recommendation-v1\n' + '[' * 5000,
                     'Kind: review-recommendation-v1\n{}'):
            self.issue['comments'].append(dict(id='junk%d' % len(self.issue['comments']), text=text, author='x',
                                               created_at='2026-10-04T00:02:00Z'))
        view = self.view()
        self.assertEqual((view['recommended'], view['review_state']), (False, 'awaiting-review'))
        # A valid one from a real reviewer still displays among all that.
        raw('reviewer', dict(good, operation_id='real'))
        self.assertEqual(self.view()['recommendation']['author'], 'reviewer')

    def test_a_withdrawn_contribution_shows_none_and_takes_none(self):
        contribution = self.contribute()
        self.recommend(contribution)
        self.count += 1
        self.actor = 'worker'
        w.execute([self.issue], TASK, 'worker', dict(
            schema_version=1, operation='withdraw', operation_id='op-%d' % self.count, task=TASK,
            previous=w.project(self.issue)['latest_comment_id'], contribution=contribution, reason='Re-scoped'),
            self.run_native, review_writes=True)
        view = self.view()
        self.assertNotEqual(view['review_state'], 'awaiting-review')
        self.assertEqual((view['recommended'], view['recommendation'], view['recommendations']), (False, None, []))
        with self.assertRaises(ValueError):
            self.recommend(contribution, actor='second-reviewer')

    def test_a_closed_task_and_a_task_without_a_contribution_show_none(self):
        self.assertEqual(rec.block(self.issue, w.project(self.issue)),
                         {'recommendation': None, 'recommendations': [], 'recommended': False})
        contribution = self.contribute()
        self.recommend(contribution)
        self.issue['status'] = 'closed'
        self.assertFalse(self.view()['recommended'])

    def test_the_work_queue_marks_a_recommended_contribution(self):
        contribution = self.contribute()
        other = dict(id='task-2', title='Another', assignee='worker', status='in_progress', issue_type='task',
                     comments=[])
        page = work.queue([self.issue, other], 'owner', ['--json'])
        self.assertEqual({item['task']: (item['recommended'], item['recommended_by']) for item in page['items']},
                         {TASK: (False, []), 'task-2': (False, [])})
        self.recommend(contribution)
        self.recommend(contribution, actor='other')
        page = work.queue([self.issue, other], 'owner', ['--json'])
        self.assertEqual({item['task']: (item['recommended'], item['recommended_by']) for item in page['items']},
                         {TASK: (True, ['other', 'reviewer']), 'task-2': (False, [])})


class TransportTests(Harness):
    def test_the_prefix_is_reserved_on_the_raw_comment_path_in_every_version(self):
        self.assertEqual(reserved_comments.RECOMMENDATION_PREFIX, rec.PREFIX)
        self.assertIn(rec.PREFIX, reserved_comments.PREFIXES)
        for body in (rec.PREFIX + '{}', 'Kind: review-recommendation-v2\n{}', 'Kind: review-recommendation-v17\n{}'):
            with self.subTest(body=body[:34]):
                match = reserved_comments.reserved_match(body)
                self.assertIsNotNone(match)
                self.assertEqual(match[1], 'review recommendation')
        # With a BOM or CRLF in front it is still the reserved prefix; a different word is not.
        self.assertIsNotNone(reserved_comments.reserved_match('﻿' + rec.PREFIX + '{}'))
        self.assertIsNotNone(reserved_comments.reserved_match(rec.PREFIX.replace('\n', '\r\n') + '{}'))
        self.assertIsNone(reserved_comments.reserved_match('Kind: review-recommendations\n{}'))

    def test_the_help_lists_the_operation_and_its_limits(self):
        payload = work.help_payload('review')
        self.assertIn('recommend', payload['operations'])
        self.assertEqual(payload['limits']['recommend summary'], '1..1200 characters')
        self.assertEqual(payload['limits']['recommend items'], '0..20 notes, each text <= 1000 characters')
        self.assertEqual(payload['limits']['recommend verdict'], 'approve')
        # Beside the chain's own limits (kittrial-5bb.97), not instead of them.
        self.assertEqual(payload['limits']['review text'], '<= 1000 characters')
        self.assertTrue(any('never an approval' in note for note in payload['notes']))


if __name__ == '__main__':
    unittest.main()
