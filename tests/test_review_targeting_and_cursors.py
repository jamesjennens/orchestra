"""Regression tests for review-operation targeting and opaque cursor round-trip.

Context: the pilot reported that `approve` supplied with a task's
`latest_comment_id` instead of the contribution record's `comment_id` "returned
success and changed nothing", and that opaque cursor tokens arrived display-elided
so a checkpoint rejected a cursor copied from `brief`.

These tests pin the guarantees the investigation established at base
20aefa0/20aefa090e895bc5435b1faefa45f72f08bcf1bc:

1. A new review operation that names the wrong contribution revision is refused
   with an error that identifies the supplied id, the current contribution id and
   the misleading id it was probably confused with, and writes nothing.
2. An exact historical retry still reconciles without a second native write.
3. Activity and history cursor tokens are emitted as complete strings at every
   read surface and survive canonical re-encoding byte-identically.
"""
import base64
import copy
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import briefing
import review_workflow as w


class WrongContributionIdTests(unittest.TestCase):
    """A review operation must never silently target the wrong revision."""

    def setUp(self):
        self.issue = dict(id='task-1', assignee='worker', status='in_progress', comments=[])
        self.count = 0

    def payload(self, op, **extra):
        self.count += 1
        return dict(schema_version=1, operation=op, operation_id='op-'+str(self.count),
                    task='task-1', previous=w.project(self.issue)['latest_comment_id'], **extra)

    def run_native(self, args):
        self.assertEqual(args[:3], ['comments', 'add', 'task-1'])
        cid = str(len(self.issue['comments'])+1)
        self.issue['comments'].append(dict(id=cid, text=args[3], author='worker',
                                           created_at='2026-09-16T00:00:00Z'))
        return json.dumps({'id': cid})

    def send(self, p, actor='worker'):
        return w.execute([self.issue], 'task-1', actor, p, self.run_native)

    def contribute(self, **extra):
        p = dict(repository='ssh://git.example/project', commit='a'*40, base_commit='b'*40,
                 delivery=dict(kind='bundle', path='koopa:/deliveries/revision-1.bundle', sha256='c'*64),
                 summary='Implementation and test evidence',
                 supersedes=(w.project(self.issue)['contribution'] or {}).get('comment_id'))
        p.update(extra)
        return self.payload('contribute', **p)

    def test_approve_with_latest_comment_id_is_refused_and_writes_nothing(self):
        """The reported case: the caller passes latest_comment_id, not the contribution id."""
        self.send(self.contribute())
        state = w.project(self.issue)
        contribution_id = state['contribution']['comment_id']
        latest = state['latest_comment_id']
        # The reported mistake: for a single-comment history the two ids coincide, so
        # add a reviewer direction first to make latest_comment_id differ.
        self.send(self.payload('request-changes', contribution=contribution_id,
                              items=[dict(id='fix', text='Correct the edge case')]), 'reviewer')
        state = w.project(self.issue)
        latest = state['latest_comment_id']
        self.assertNotEqual(latest, state['contribution']['comment_id'])
        before = copy.deepcopy(self.issue['comments'])
        with self.assertRaises(ValueError) as caught:
            self.send(self.payload('approve', contribution=latest, summary='Reviewed'), 'reviewer')
        message = str(caught.exception)
        # The error must name the operation, the supplied id and the expected id.
        self.assertIn('approve', message)
        self.assertIn(latest, message)
        self.assertIn(state['contribution']['comment_id'], message)
        self.assertEqual(self.issue['comments'], before, 'a refused approve must not write')

    def test_wrong_contribution_error_names_the_misused_latest_comment_id(self):
        """When the supplied id is the task's latest comment, say so explicitly."""
        self.send(self.contribute())
        first = w.project(self.issue)['latest_comment_id']
        self.send(self.payload('request-changes', contribution=first,
                              items=[dict(id='fix', text='Correct the edge case')]), 'reviewer')
        state = w.project(self.issue)
        latest = state['latest_comment_id']
        before = copy.deepcopy(self.issue['comments'])
        # respond is owner-only, so exercise it under the owning actor; the reviewer
        # operations are exercised under the reviewer.
        cases = [
            ('approve', 'reviewer', dict(summary='Reviewed')),
            ('request-changes', 'reviewer', dict(items=[dict(id='again', text='Another item')])),
            ('respond', 'worker', dict(resolutions=[dict(request=latest, item='fix', reason='done', evidence='commit')])),
        ]
        for operation, actor, extra in cases:
            with self.assertRaises(ValueError) as caught:
                self.send(self.payload(operation, contribution=latest, **extra), actor)
            message = str(caught.exception)
            self.assertIn(operation, message, operation)
            self.assertIn(latest, message, operation)
            self.assertIn('latest comment', message, operation)
            self.assertEqual(self.issue['comments'], before)

    def test_unknown_contribution_id_is_refused_with_context(self):
        """An id that is neither the current contribution nor the latest comment."""
        self.send(self.contribute())
        before = copy.deepcopy(self.issue['comments'])
        with self.assertRaises(ValueError) as caught:
            self.send(self.payload('approve', contribution='not-a-real-id', summary='Reviewed'), 'reviewer')
        message = str(caught.exception)
        self.assertIn('approve', message)
        self.assertIn('not-a-real-id', message)
        self.assertIn(w.project(self.issue)['contribution']['comment_id'], message)
        self.assertEqual(self.issue['comments'], before)

    def test_approve_before_any_contribution_says_so(self):
        before = copy.deepcopy(self.issue['comments'])
        with self.assertRaises(ValueError) as caught:
            self.send(self.payload('approve', contribution='anything', summary='Reviewed'), 'reviewer')
        message = str(caught.exception)
        self.assertIn('approve', message)
        self.assertIn('anything', message)
        self.assertIn('no current contribution has been recorded yet', message)
        self.assertEqual(self.issue['comments'], before)

    def test_exact_historical_retry_still_reconciles_without_a_second_write(self):
        """Reconciliation must not be broken by the stricter new-operation check."""
        p = self.contribute()
        receipt = self.send(p)
        self.assertEqual(receipt['reconciled'], False)
        written = len(self.issue['comments'])
        retry = self.send(p)
        self.assertEqual(retry['comment_id'], receipt['comment_id'])
        self.assertTrue(retry['reconciled'])
        self.assertEqual(len(self.issue['comments']), written)

    def test_correct_contribution_id_still_approves(self):
        """The fix must not reject the correct id."""
        self.send(self.contribute())
        contribution_id = w.project(self.issue)['contribution']['comment_id']
        self.send(self.payload('approve', contribution=contribution_id, summary='Reviewed'), 'reviewer')
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')


class OpaqueCursorRoundTripTests(unittest.TestCase):
    """Opaque tokens must be emitted whole and survive re-encoding exactly."""

    def setUp(self):
        self.rows = [dict(id='p-task', title='Task', status='open', assignee='worker',
                          updated_at='2026-09-01T00:00:00Z',
                          comments=[dict(id='1', created_at='2026-09-10T12:00:00Z', author='alice',
                                         text='First finding'),
                                    dict(id='2', created_at='2026-09-10T13:00:00Z', author='bob',
                                         text='Second finding — non-ascii: café 漢字')]),
                     dict(id='p-task.1', title='State change', status='open', issue_type='event',
                          created_at='2026-09-10T14:00:00Z', created_by='worker',
                          description='Set implemented to passed',
                          dependencies=[dict(depends_on_id='p-task', type='parent-child')])]

    def token_payload(self, token):
        padded = token + '='*(-len(token) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))

    def encode(self, payload):
        return base64.urlsafe_b64encode(briefing.canonical_bytes(payload)).decode().rstrip('=')

    def assert_complete_token(self, token, expected):
        """A complete token is exactly the canonical encoding of its payload."""
        self.assertIsInstance(token, str)
        self.assertNotIn('...', token)
        self.assertTrue(re.fullmatch(r'[A-Za-z0-9_-]+', token), token)
        self.assertEqual(self.encode(self.token_payload(token)), token)
        self.assertEqual(self.token_payload(token), expected)

    def test_activity_cursor_is_complete_and_reencodes_identically(self):
        result = briefing.brief(self.rows, 'proj', 'p-task')
        payload = self.token_payload(result['activity_cursor'])
        self.assertEqual(payload['kind'], 'activity')
        self.assertEqual(payload['task'], 'p-task')
        self.assertEqual(payload['project'], 'proj')
        self.assertEqual(payload['v'], 1)
        self.assertTrue(re.fullmatch('[a-f0-9]{64}', payload['sha256']))
        self.assert_complete_token(result['activity_cursor'], payload)

    def test_text_brief_emits_the_same_complete_token_as_json(self):
        """format_brief must not clip the opaque token it prints."""
        result = briefing.brief(self.rows, 'proj', 'p-task')
        text = briefing.format_brief(result)
        lines = [line for line in text.splitlines() if line.startswith('Current activity cursor: ')]
        self.assertEqual(len(lines), 1)
        printed = lines[0].split(': ', 1)[1]
        self.assertEqual(printed, result['activity_cursor'])

    def test_checkpoint_accepts_the_cursor_emitted_by_brief(self):
        """The token a worker copies from brief must validate for a checkpoint."""
        result = briefing.brief(self.rows, 'proj', 'p-task')
        p = dict(schema_version=1, task='p-task', previous=None,
                 activity_cursor=result['activity_cursor'], source_commit='', branch='',
                 intent='Intent', acceptance='Acceptance', summary='Summary',
                 next_action='Next', open_items=[], resolved=[])
        briefing.validate_checkpoint(p, 'p-task')
        # The canonical bytes that a checkpoint transport carries are stable.
        self.assertEqual(briefing.canonical_bytes(p) == briefing.canonical_bytes(dict(p)),
                         True)

    def test_history_cursors_are_complete_and_reencode_identically(self):
        data = briefing.snapshot(self.rows, 'proj', 'p-task')
        page = briefing.history_page(data, 'proj', 'p-task', limit=1)
        self.assertIsNotNone(page['next_cursor'])
        payload = self.token_payload(page['next_cursor'])
        self.assertEqual(payload['kind'], 'history')
        self.assertEqual(payload['snapshot'], page['snapshot'])
        self.assert_complete_token(page['next_cursor'], payload)
        self.assert_complete_token(page['activity_cursor'],
                                  self.token_payload(page['activity_cursor']))
        # Round-trip: the emitted cursor continues the same immutable snapshot.
        following = briefing.history_page(data, 'proj', 'p-task', limit=1, cursor=page['next_cursor'])
        self.assertEqual(following['total_entries'], page['total_entries'])
        self.assertNotEqual([e['entry_id'] for e in following['entries']],
                            [e['entry_id'] for e in page['entries']])

    def test_cursor_from_one_task_is_not_accepted_for_another(self):
        other = briefing.brief(self.rows, 'proj', 'p-task')['activity_cursor']
        p = dict(schema_version=1, task='p-task.1', previous=None, activity_cursor=other,
                 source_commit='', branch='', intent='Intent', acceptance='Acceptance',
                 summary='Summary', next_action='Next', open_items=[], resolved=[])
        with self.assertRaises(ValueError):
            briefing.validate_checkpoint(p, 'p-task.1')

    def test_truncated_and_malformed_tokens_fail_cleanly(self):
        """Display truncation must surface as a clear refusal, never a wrong read."""
        result = briefing.brief(self.rows, 'proj', 'p-task')
        token = result['activity_cursor']
        for broken in ('eyJraW...oxfQ', token[:20], token[:-3], token+'*', '', 'a'*5000):
            with self.assertRaisesRegex(ValueError, 'Invalid cursor'):
                briefing.untoken(broken)
        p = dict(schema_version=1, task='p-task', previous=None, activity_cursor=token[:20],
                 source_commit='', branch='', intent='Intent', acceptance='Acceptance',
                 summary='Summary', next_action='Next', open_items=[], resolved=[])
        with self.assertRaises(ValueError):
            briefing.validate_checkpoint(p, 'p-task')

    def test_elided_cursor_cannot_silently_resolve_to_the_right_snapshot(self):
        """A clipped token must never hash to the same payload it was clipped from."""
        result = briefing.brief(self.rows, 'proj', 'p-task')
        token = result['activity_cursor']
        clipped = token[:len(token)//2]
        self.assertNotEqual(clipped, token)
        with self.assertRaises(ValueError):
            briefing.untoken(clipped)


if __name__ == '__main__':
    unittest.main()
