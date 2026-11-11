"""Refuse same-commit contribute while the latest delivery has an un-superseded request-changes (kittrial-5bb.556)."""
import copy
import io
import json
import os
import secrets
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))

import recovery
import review_workflow as w
import test_http_agents
import test_http_review_fixes as fixes
import work
from requirements import canonical_bytes

COMMIT_A = 'a' * 40
COMMIT_B = 'd' * 40
BASE_1 = 'b' * 40
BASE_2 = 'e' * 40
BUNDLE_1 = 'c' * 64
BUNDLE_2 = 'f' * 64
STAMP = '2026-01-01T00:00:00Z'
SENTENCE = (
    'this commit was already reviewed and changes were requested; '
    'answer the review with respond, or deliver a new commit'
)
ANSWERED_SENTENCE = (
    'this commit was already reviewed and its requested changes were answered; '
    'wait for the reviewer to review it again, or deliver a new commit'
)


class CanonicalSameCommitRefusalTests(unittest.TestCase):
    """Canonical review_workflow, work, and endpoint wire coverage (cases 1-12 + mutants)."""

    def setUp(self):
        self.issue = dict(
            id='task-1', title='Task one', issue_type='task',
            assignee='worker', status='in_progress', labels=[], comments=[]
        )
        self.other = dict(
            id='task-2', title='Task two', issue_type='task',
            assignee='worker', status='in_progress', labels=[], comments=[]
        )
        self.rows = [self.issue, self.other]
        self.seq = 0

    def next_op(self, prefix='op'):
        self.seq += 1
        return '%s-%d' % (prefix, self.seq)

    def _row(self, task_id):
        return next(r for r in self.rows if r['id'] == task_id)

    def run_native(self, actor='worker'):
        def runner(args):
            self.assertEqual(args[:2], ['comments', 'add'])
            row = self._row(args[2])
            cid = 'c-%d' % (len(row['comments']) + 1)
            row['comments'].append(dict(id=cid, text=args[3], author=actor, created_at=STAMP))
            return json.dumps({'id': cid})
        return runner

    def send(self, payload, actor='worker', review_writes=True, operators=None):
        return w.execute(
            self.rows, payload['task'], actor, payload,
            self.run_native(actor), operators=operators, review_writes=review_writes
        )

    def payload(self, op, task='task-1', operators=None, **extra):
        state = w.project(self._row(task), operators=operators)
        body = dict(
            schema_version=1, operation=op, operation_id=self.next_op(op),
            task=task, previous=state['latest_comment_id']
        )
        body.update(extra)
        return body

    def contribute_payload(self, commit=COMMIT_A, task='task-1', operators=None, **extra):
        state = w.project(self._row(task), operators=operators)
        current = (state['contribution'] or {}).get('comment_id')
        body = dict(
            repository='ssh://git.example/project',
            commit=commit,
            base_commit=BASE_1,
            delivery=dict(kind='bundle', path='koopa:/deliveries/r1.bundle', sha256=BUNDLE_1),
            summary='Implementation and test evidence',
            supersedes=current,
        )
        body.update(extra)
        return self.payload('contribute', task=task, operators=operators, **body)

    def request_changes(self, contribution_id, items=None, actor='reviewer', task='task-1', operators=None, **extra):
        if items is None:
            items = [dict(id='fix-1', text='Add the missing check')]
        return self.send(
            self.payload('request-changes', task=task, operators=operators,
                         contribution=contribution_id, items=items, **extra),
            actor=actor, operators=operators
        )['comment_id']

    def assert_refused_409(self, exc, expected_review_id, task='task-1', answered=False):
        expected_sentence = ANSWERED_SENTENCE if answered else SENTENCE
        self.assertIsInstance(exc, w.SameCommitChangesRequested)
        self.assertEqual(exc.http_status, 409)
        self.assertEqual(exc.review_id, expected_review_id)
        self.assertEqual(exc.answered, answered)
        self.assertEqual(exc.detail['http_status'], 409)
        self.assertEqual(exc.detail['review_id'], expected_review_id)
        self.assertEqual(exc.detail['task'], task)
        self.assertEqual(exc.detail['answered'], answered)
        self.assertIn(expected_sentence, str(exc))
        self.assertIn(expected_review_id, str(exc))
        self.assertEqual(str(exc), '%s (review %s)' % (expected_sentence, expected_review_id))

    def work_queue_snapshot(self, tmp_dir):
        def fake_run(argv):
            if argv[:1] == ['export']:
                return '\n'.join(json.dumps(r) for r in self.rows) + '\n'
            raise AssertionError('unexpected native call: %r' % (argv,))
        mine = work.execute(Path(tmp_dir), 'worker', 'work', ['--mine'], {}, fake_run)
        reviewer_q = work.execute(Path(tmp_dir), 'reviewer', 'work', ['--state', 'awaiting-review'], {}, fake_run)
        return mine, reviewer_q

    def test_01_same_commit_open_changes_requested_refused_409_nothing_recorded_queue_unchanged(self):
        c1 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        req_id = self.request_changes(c1)
        before_comments = copy.deepcopy(self.issue['comments'])
        with tempfile.TemporaryDirectory() as tmp:
            before_queue = self.work_queue_snapshot(tmp)
            with self.assertRaises(w.SameCommitChangesRequested) as caught:
                self.send(self.contribute_payload(COMMIT_A))
            self.assert_refused_409(caught.exception, req_id)
            self.assertEqual(self.issue['comments'], before_comments)
            self.assertEqual(w.project(self.issue)['review_state'], 'changes-requested')
            after_queue = self.work_queue_snapshot(tmp)
            self.assertEqual(after_queue, before_queue)
            self.assertEqual(after_queue[1]['items'], [])

    def test_02_same_commit_with_different_base_commit_branch_or_summary_still_refused(self):
        c1 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        req_id = self.request_changes(c1)
        before_comments = copy.deepcopy(self.issue['comments'])
        variants = (
            dict(base_commit=BASE_2),
            dict(delivery=dict(kind='remote', remote='ssh://git.example/other', branch='feature/new-branch')),
            dict(delivery=dict(kind='bundle', path='koopa:/deliveries/r2.bundle', sha256=BUNDLE_2)),
            dict(summary='Completely rewritten summary text', repository='ssh://git.example/renamed'),
            dict(commit=COMMIT_A.upper(), base_commit=BASE_2, summary='Uppercase SHA and new base'),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                with self.assertRaises(w.SameCommitChangesRequested) as caught:
                    self.send(self.contribute_payload(**variant))
                self.assert_refused_409(caught.exception, req_id)
                self.assertEqual(self.issue['comments'], before_comments)

    def test_03_new_commit_after_changes_requested_accepted_as_today(self):
        c1 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        self.request_changes(c1)
        second = self.send(self.contribute_payload(COMMIT_B))
        state = w.project(self.issue)
        self.assertEqual(state['contribution']['comment_id'], second['comment_id'])
        self.assertEqual(state['contribution']['commit'], COMMIT_B)
        self.assertEqual(state['contribution']['supersedes'], c1)
        self.assertEqual(second['open_items_total'], 1)
        self.assertEqual(state['review_state'], 'changes-requested')

    def test_04_same_commit_after_approval_and_after_rejection_keeps_todays_behaviour(self):
        # (a) Direct approve -> same commit accepted and resets review_state to awaiting-review.
        c1 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        self.send(self.payload('approve', contribution=c1, summary='Approved'), actor='reviewer')
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')
        c2 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-review')
        self.assertEqual(w.project(self.issue)['contribution']['comment_id'], c2)

        # (b) request-changes -> respond -> approve -> same commit accepted (kills Mutant c).
        r1 = self.request_changes(c2)
        self.send(self.payload('respond', contribution=c2, resolutions=[
            dict(request=r1, item='fix-1', reason='Explained', evidence='commit:' + COMMIT_A)
        ]))
        self.send(self.payload('approve', contribution=c2, summary='Re-reviewed and approved'), actor='reviewer')
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')
        c3 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-review')
        self.assertEqual(w.project(self.issue)['contribution']['comment_id'], c3)

        # (c) Rejection/withdrawal (both dispositions) after request-changes -> same commit accepted,
        # carrying open requested-change items forward so approve remains gated.
        for index, disposition in enumerate(('withdrawn', 'superseded'), start=1):
            with self.subTest(disposition=disposition):
                current = w.project(self.issue)['contribution']['comment_id']
                req = self.request_changes(current, items=[dict(id='w-%d' % index, text='Still open')])
                self.send(
                    self.payload('withdraw', contribution=current, disposition=disposition, reason='Rejected revision'),
                    actor='coordinator', operators='coordinator'
                )
                self.assertEqual(w.project(self.issue)['review_state'], disposition)
                revived = self.send(self.contribute_payload(COMMIT_A))['comment_id']
                state = w.project(self.issue)
                self.assertEqual(state['contribution']['comment_id'], revived)
                self.assertEqual(state['review_state'], 'changes-requested')
                # Clean up open item so next subTest starts clean on `revived`.
                self.send(self.payload('respond', contribution=revived, resolutions=[
                    dict(request=req, item='w-%d' % index, reason='Addressed', evidence='commit:' + COMMIT_A)
                ]))

    def test_05_same_commit_after_respond_before_rereview_refused_409(self):
        c1 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        r1 = self.request_changes(c1, items=[dict(id='i1', text='First fix')])
        r2 = self.request_changes(c1, items=[dict(id='i2', text='Second fix')], actor='reviewer-2')

        # Partial respond (r2 answered, r1 still unanswered): 409 naming r1 with the open sentence.
        self.send(self.payload('respond', contribution=c1, resolutions=[
            dict(request=r2, item='i2', reason='Answered second', evidence='test')
        ]))
        self.assertEqual(w.project(self.issue)['review_state'], 'changes-requested')
        before = copy.deepcopy(self.issue['comments'])
        with self.assertRaises(w.SameCommitChangesRequested) as partial:
            self.send(self.contribute_payload(COMMIT_A))
        self.assert_refused_409(partial.exception, r1, answered=False)
        self.assertEqual(self.issue['comments'], before)

        # Full respond (all blocking items answered, awaiting-review before any re-review):
        # 409 naming r2 with the answered sentence ("wait for the reviewer to review it again...").
        self.send(self.payload('respond', contribution=c1, resolutions=[
            dict(request=r1, item='i1', reason='Answered first', evidence='test')
        ]))
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-review')
        before_full = copy.deepcopy(self.issue['comments'])
        with self.assertRaises(w.SameCommitChangesRequested) as full:
            self.send(self.contribute_payload(COMMIT_A))
        self.assert_refused_409(full.exception, r2, answered=True)
        self.assertEqual(self.issue['comments'], before_full)

    def test_06_same_commit_on_different_task_accepted(self):
        c1 = self.send(self.contribute_payload(COMMIT_A, task='task-1'))['comment_id']
        self.request_changes(c1, task='task-1')
        other_delivery = self.send(self.contribute_payload(COMMIT_A, task='task-2'))
        other_state = w.project(self.other)
        self.assertEqual(other_state['contribution']['comment_id'], other_delivery['comment_id'])
        self.assertEqual(other_state['contribution']['commit'], COMMIT_A)
        self.assertEqual(other_state['review_state'], 'awaiting-review')

    def test_07_reassigned_task_different_actor_same_commit_refused(self):
        c1 = self.send(self.contribute_payload(COMMIT_A), actor='worker')['comment_id']
        req_id = self.request_changes(c1)
        self.issue['assignee'] = 'worker-two'
        before = copy.deepcopy(self.issue['comments'])
        with self.assertRaises(w.SameCommitChangesRequested) as caught:
            self.send(self.contribute_payload(COMMIT_A), actor='worker-two')
        self.assert_refused_409(caught.exception, req_id)
        self.assertEqual(self.issue['comments'], before)

    def test_08_idempotent_retry_of_original_contribute_returns_original_result(self):
        original_payload = self.contribute_payload(COMMIT_A)
        first = self.send(original_payload)
        self.request_changes(first['comment_id'])
        before = copy.deepcopy(self.issue['comments'])
        replayed = self.send(copy.deepcopy(original_payload))
        self.assertTrue(replayed['reconciled'])
        self.assertEqual(replayed['comment_id'], first['comment_id'])
        self.assertEqual(self.issue['comments'], before)

    def test_09_legacy_changes_requested_rows_still_trigger_refusal(self):
        c1 = self.send(self.contribute_payload(COMMIT_A), review_writes=False)['comment_id']
        legacy_req = dict(
            schema_version=1, operation='request-changes', operation_id='legacy-req-1',
            task='task-1', previous=c1, contribution=c1,
            items=[dict(id='legacy-item', text='Written before severity existed')]
        )
        self.issue['comments'].append(dict(
            id='legacy-r1', text=w.PREFIX + canonical_bytes(legacy_req).decode(),
            author='reviewer', created_at=STAMP
        ))
        before = copy.deepcopy(self.issue['comments'])
        with self.assertRaises(w.SameCommitChangesRequested) as caught:
            self.send(self.contribute_payload(COMMIT_A), review_writes=False)
        self.assert_refused_409(caught.exception, 'legacy-r1')
        self.assertEqual(self.issue['comments'], before)

    def test_10_every_contribute_route_returns_same_409_refusal(self):
        c1 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        req_id = self.request_changes(c1)
        body = self.contribute_payload(COMMIT_A)

        # (a) work.execute(..., action='review', ...)
        with tempfile.TemporaryDirectory() as tmp:
            def fake_run(argv):
                if argv[:2] == ['show', 'task-1']:
                    return json.dumps([self.issue])
                if argv[:1] == ['export']:
                    return '\n'.join(json.dumps(r) for r in self.rows) + '\n'
                raise AssertionError('unexpected native write: %r' % (argv,))
            with self.assertRaises(w.SameCommitChangesRequested) as work_exc:
                work.execute(
                    Path(tmp), 'worker', 'review', ['task-1', '@attachment:0'],
                    {'0': {'flag': '--file', 'text': json.dumps(body)}}, fake_run, review_writes=True
                )
            self.assert_refused_409(work_exc.exception, req_id)

        # (b) Real endpoint.main() CLI/SSH JSON wire (POSIX-only because endpoint imports fcntl).
        if os.name == 'posix':
            import subprocess
            import endpoint
            with tempfile.TemporaryDirectory() as root_str:
                root = Path(root_str)
                proj = root / 'projects' / 'pp'
                (proj / '.beads').mkdir(parents=True)
                (proj / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
                (root / 'deployment.private.json').write_text(
                    json.dumps({'password': 'test-password', 'operators': [], 'review_workflow_writes': True}),
                    encoding='utf-8',
                )

                def fake_native_run(argv, env=None, **kwargs):
                    if 'show' in argv:
                        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([self.issue]) + '\n', stderr='')
                    if 'export' in argv:
                        return subprocess.CompletedProcess(
                            argv, 0, stdout='\n'.join(json.dumps(r) for r in self.rows) + '\n', stderr=''
                        )
                    raise AssertionError('unexpected bd write: %r' % (argv,))

                req_wire = {
                    'action': 'review',
                    'project': 'pp',
                    'actor': 'worker',
                    'args': ['task-1', '@attachment:0'],
                    'attachments': {'0': {'flag': '--file', 'text': json.dumps(body)}},
                }
                stdout_buf = io.StringIO()
                with mock.patch.object(endpoint.native, 'run', side_effect=fake_native_run), \
                     mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(req_wire))), \
                     mock.patch.object(sys, 'argv', ['endpoint.py', '--root', str(root)]), \
                     redirect_stdout(stdout_buf):
                    endpoint.main()
                wire_reply = json.loads(stdout_buf.getvalue())
                self.assertEqual(wire_reply['returncode'], 2, wire_reply)
                self.assertEqual(wire_reply['review_status'], 409, wire_reply)
                self.assertEqual(wire_reply['review_id'], req_id, wire_reply)
                self.assertFalse(wire_reply['review_answered'], wire_reply)
                self.assertIn(SENTENCE, wire_reply['stderr'])
                self.assertIn(req_id, wire_reply['stderr'])

                # Once answered via respond, the CLI/SSH wire returns the answered sentence and flag.
                self.send(self.payload('respond', contribution=c1, resolutions=[
                    dict(request=req_id, item='fix-1', reason='Answered', evidence='commit:' + COMMIT_A)
                ]))
                answered_body = self.contribute_payload(COMMIT_A)
                req_wire_answered = dict(req_wire, attachments={'0': {'flag': '--file', 'text': json.dumps(answered_body)}})
                stdout_answered = io.StringIO()
                with mock.patch.object(endpoint.native, 'run', side_effect=fake_native_run), \
                     mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(req_wire_answered))), \
                     mock.patch.object(sys, 'argv', ['endpoint.py', '--root', str(root)]), \
                     redirect_stdout(stdout_answered):
                    endpoint.main()
                wire_answered = json.loads(stdout_answered.getvalue())
                self.assertEqual(wire_answered['returncode'], 2, wire_answered)
                self.assertEqual(wire_answered['review_status'], 409, wire_answered)
                self.assertEqual(wire_answered['review_id'], req_id, wire_answered)
                self.assertTrue(wire_answered['review_answered'], wire_answered)
                self.assertIn(ANSWERED_SENTENCE, wire_answered['stderr'])
                self.assertIn(req_id, wire_answered['stderr'])

    def test_11_note_only_request_changes_does_not_trigger_refusal(self):
        c1 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        self.request_changes(c1, items=[dict(id='n1', text='Optional nit', severity='note')])
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-review')
        c2 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        self.assertEqual(w.project(self.issue)['contribution']['comment_id'], c2)

        # Also when the requester downgrades their sole blocking item to a note via resolve-item.
        r_block = self.request_changes(c2, items=[dict(id='b1', text='Was blocking')], actor='reviewer')
        self.send(
            self.payload('resolve-item', contribution=c2, request=r_block, item='b1',
                         disposition='note', reason='Downgraded to non-blocking note'),
            actor='reviewer'
        )
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-review')
        c3 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        self.assertEqual(w.project(self.issue)['contribution']['comment_id'], c3)

    def test_12_operator_voided_request_changes_does_not_trigger_refusal(self):
        # A conflicting forked request-changes voided by an operator (applied_review_voids)
        # is excluded from `ordered` and must NOT refuse a same-commit contribute.
        c1 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        forked_req = dict(
            schema_version=1, operation='request-changes', operation_id='fork-req-1',
            task='task-1', previous=c1, contribution=c1,
            items=[dict(id='fix-fork', text='Conflicting forked request')]
        )
        forked_req_text = w.PREFIX + canonical_bytes(forked_req).decode()
        self.issue['comments'].append(dict(id='r-fork', text=forked_req_text, author='reviewer', created_at=STAMP))
        sibling_note = dict(
            schema_version=1, operation='request-changes', operation_id='fork-note-1',
            task='task-1', previous=c1, contribution=c1,
            items=[dict(id='note-1', text='Surviving branch', severity='note')]
        )
        self.issue['comments'].append(dict(
            id='r-surviving', text=w.PREFIX + canonical_bytes(sibling_note).decode(),
            author='reviewer-2', created_at=STAMP
        ))
        void_payload = dict(
            schema_version=1, operation='void-record', operation_id='void-req-1',
            task='task-1', target='r-fork', target_kind='contribution-review',
            target_sha256=recovery.digest(forked_req_text), original=forked_req_text,
            reason='Reconcile forked request-changes record', disposition='void', operator='ops'
        )
        self.issue['comments'].append(dict(
            id='void-1', text=recovery.PREFIX + canonical_bytes(void_payload).decode(),
            author='ops', created_at=STAMP
        ))
        state = w.project(self.issue, operators='ops')
        self.assertEqual(len(state['recoveries']), 1)
        self.assertTrue(state['recoveries'][0]['applied'])
        self.assertEqual(state['recoveries'][0]['target'], 'r-fork')
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertEqual(state['latest_comment_id'], 'r-surviving')
        c2 = self.send(self.contribute_payload(COMMIT_A, operators='ops'), operators='ops')['comment_id']
        self.assertEqual(w.project(self.issue, operators='ops')['contribution']['comment_id'], c2)

        # Conversely, a refused void (trying to void the sole clean tail request-changes) stays unapplied
        # and still triggers the 409 refusal.
        clean_req = self.request_changes(c2, task='task-1', operators='ops')
        clean_comment = next(c for c in self.issue['comments'] if c['id'] == clean_req)
        refused_void = dict(
            schema_version=1, operation='void-record', operation_id='void-refused-1',
            task='task-1', target=clean_req, target_kind='contribution-review',
            target_sha256=recovery.digest(clean_comment['text']), original=clean_comment['text'],
            reason='Protected clean chain tail cannot be voided', disposition='void', operator='ops'
        )
        self.issue['comments'].append(dict(
            id='void-refused', text=recovery.PREFIX + canonical_bytes(refused_void).decode(),
            author='ops', created_at=STAMP
        ))
        with self.assertRaises(w.SameCommitChangesRequested) as caught:
            self.send(self.contribute_payload(COMMIT_A, operators='ops'), operators='ops')
        self.assert_refused_409(caught.exception, clean_req)

    def test_latest_delivery_targeted_across_multiple_rounds(self):
        # Direction 1: c1(A) -> approve -> c2(B) -> request-changes -> c3(A) MUST SUCCEED
        # (latest delivery is B, so reverting to A is a new commit relative to latest delivery B; kills Mutant b).
        c1 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        self.send(self.payload('approve', contribution=c1, summary='Approved A'), actor='reviewer')
        c2 = self.send(self.contribute_payload(COMMIT_B))['comment_id']
        self.request_changes(c2)
        c3 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        self.assertEqual(w.project(self.issue)['contribution']['comment_id'], c3)
        self.assertEqual(w.project(self.issue)['contribution']['commit'], COMMIT_A)

        # Direction 2: on task-2, c1(A) -> request-changes -> c2(B) -> request-changes -> c3(B) MUST BE REFUSED 409
        # (latest delivery is B, not A; kills Mutant b comparing against first delivery c1(A)).
        t2_c1 = self.send(self.contribute_payload(COMMIT_A, task='task-2'))['comment_id']
        self.request_changes(t2_c1, task='task-2')
        t2_c2 = self.send(self.contribute_payload(COMMIT_B, task='task-2'))['comment_id']
        t2_r2 = self.request_changes(t2_c2, task='task-2')
        with self.assertRaises(w.SameCommitChangesRequested) as caught:
            self.send(self.contribute_payload(COMMIT_B, task='task-2'))
        self.assert_refused_409(caught.exception, t2_r2, task='task-2')

    def test_13_voided_approval_keeps_changes_requested_refusal_c07(self):
        # c07: an approval removed by an operator void-record does not settle the review;
        # a same-commit contribute is still refused 409 with the answered sentence.
        c1 = self.send(self.contribute_payload(COMMIT_A, operators='ops'), operators='ops')['comment_id']
        r1 = self.request_changes(c1, operators='ops')
        resp1 = self.send(self.payload('respond', operators='ops', contribution=c1, resolutions=[
            dict(request=r1, item='fix-1', reason='Explained with evidence', evidence='commit:' + COMMIT_A)
        ]), operators='ops')['comment_id']

        forked_approve = dict(
            schema_version=1, operation='approve', operation_id='fork-app-1',
            task='task-1', previous=resp1, contribution=c1, summary='Forked premature approval'
        )
        forked_app_text = w.PREFIX + canonical_bytes(forked_approve).decode()
        self.issue['comments'].append(dict(id='app-fork', text=forked_app_text, author='reviewer', created_at=STAMP))
        surviving_note = dict(
            schema_version=1, operation='request-changes', operation_id='surviving-note-c07',
            task='task-1', previous=resp1, contribution=c1,
            items=[dict(id='n-c07', text='Surviving non-blocking note', severity='note')]
        )
        self.issue['comments'].append(dict(
            id='note-c07', text=w.PREFIX + canonical_bytes(surviving_note).decode(),
            author='reviewer-2', created_at=STAMP
        ))
        void_app = dict(
            schema_version=1, operation='void-record', operation_id='void-app-c07',
            task='task-1', target='app-fork', target_kind='contribution-review',
            target_sha256=recovery.digest(forked_app_text), original=forked_app_text,
            reason='Void forked premature approval', disposition='void', operator='ops'
        )
        self.issue['comments'].append(dict(
            id='void-c07', text=recovery.PREFIX + canonical_bytes(void_app).decode(),
            author='ops', created_at=STAMP
        ))

        state = w.project(self.issue, operators='ops')
        self.assertTrue(state['recoveries'][0]['applied'])
        self.assertEqual(state['recoveries'][0]['target'], 'app-fork')
        self.assertEqual(state['review_state'], 'awaiting-review')
        before = copy.deepcopy(self.issue['comments'])
        with self.assertRaises(w.SameCommitChangesRequested) as caught:
            self.send(self.contribute_payload(COMMIT_A, operators='ops'), operators='ops')
        self.assert_refused_409(caught.exception, r1, answered=True)
        self.assertEqual(self.issue['comments'], before)

    def test_14_stale_approval_invalidated_by_later_void_still_refuses_c08_o1(self):
        # c08 / n3-O1 (review_workflow.py:2664): an approval in `ordered` that was recorded BEFORE
        # a later applied contribution-review void (`void_position > approval_position`) is stale
        # (`review_state == 'awaiting-review'`, not `awaiting-integration`), so it must NOT clear
        # `blocking` and a same-commit contribute must still be refused 409 with the answered sentence.
        c1 = self.send(self.contribute_payload(COMMIT_A, operators='ops'), operators='ops')['comment_id']
        r1 = self.request_changes(c1, operators='ops')
        resp1 = self.send(self.payload('respond', operators='ops', contribution=c1, resolutions=[
            dict(request=r1, item='fix-1', reason='Answered before fork', evidence='commit:' + COMMIT_A)
        ]), operators='ops')['comment_id']

        forked_req = dict(
            schema_version=1, operation='request-changes', operation_id='fork-req-c08',
            task='task-1', previous=resp1, contribution=c1,
            items=[dict(id='dup', text='Conflicting forked request')]
        )
        forked_req_text = w.PREFIX + canonical_bytes(forked_req).decode()
        self.issue['comments'].append(dict(id='r-fork-c08', text=forked_req_text, author='reviewer-2', created_at=STAMP))

        surviving_approve = dict(
            schema_version=1, operation='approve', operation_id='app-before-void-c08',
            task='task-1', previous=resp1, contribution=c1, summary='Approved before fork repair'
        )
        self.issue['comments'].append(dict(
            id='app-stale', text=w.PREFIX + canonical_bytes(surviving_approve).decode(),
            author='reviewer', created_at=STAMP
        ))

        # Applied AFTER `app-stale` (`void_position > approval_position`): `app-stale` stays in `ordered`
        # as the chain head, but `projection` marks it stale and `_approval_invalidated_by_void` is True.
        void_fork = dict(
            schema_version=1, operation='void-record', operation_id='void-fork-c08',
            task='task-1', target='r-fork-c08', target_kind='contribution-review',
            target_sha256=recovery.digest(forked_req_text), original=forked_req_text,
            reason='Repair forked request-changes branch', disposition='void', operator='ops'
        )
        self.issue['comments'].append(dict(
            id='void-after-app', text=recovery.PREFIX + canonical_bytes(void_fork).decode(),
            author='ops', created_at=STAMP
        ))

        state = w.project(self.issue, operators='ops')
        self.assertEqual(state['latest_comment_id'], 'app-stale')
        self.assertEqual(state['review_state'], 'awaiting-review')
        before = copy.deepcopy(self.issue['comments'])
        with self.assertRaises(w.SameCommitChangesRequested) as caught:
            self.send(self.contribute_payload(COMMIT_A, operators='ops'), operators='ops')
        self.assert_refused_409(caught.exception, r1, answered=True)
        self.assertEqual(self.issue['comments'], before)

        # Once a fresh approval is recorded AFTER the void, the review is settled and same-commit succeeds.
        self.send(
            self.payload('approve', operators='ops', contribution=c1, summary='Fresh approval after void'),
            actor='reviewer', operators='ops'
        )
        self.assertEqual(w.project(self.issue, operators='ops')['review_state'], 'awaiting-integration')
        c2 = self.send(self.contribute_payload(COMMIT_A, operators='ops'), operators='ops')['comment_id']
        self.assertEqual(w.project(self.issue, operators='ops')['contribution']['comment_id'], c2)

    def test_15_requester_resolve_item_without_downgrade_to_note_clears_refusal_o2(self):
        # n3-O2 (review_workflow.py:2662): requester resolves their blocking item without
        # downgrading it to a note (`disposition='resolved'` and default omitted disposition).
        c1 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        r1 = self.request_changes(c1, items=[dict(id='b1', text='Blocking item to resolve')], actor='reviewer')
        self.send(
            self.payload('resolve-item', contribution=c1, request=r1, item='b1',
                         disposition='resolved', reason='Verified already handled'),
            actor='reviewer'
        )
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertEqual(state['pending_requests'], [])
        self.assertEqual(state['note_requests'], [])
        c2 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        self.assertEqual(w.project(self.issue)['contribution']['comment_id'], c2)

        # Default disposition (omitted `disposition` key -> 'resolved', also not downgraded to a note).
        r2 = self.request_changes(c2, items=[dict(id='b2', text='Second blocking item')], actor='reviewer')
        self.send(
            self.payload('resolve-item', contribution=c2, request=r2, item='b2',
                         reason='Withdrawn by requester using default disposition'),
            actor='reviewer'
        )
        state2 = w.project(self.issue)
        self.assertEqual(state2['review_state'], 'awaiting-review')
        self.assertEqual(state2['pending_requests'], [])
        self.assertEqual(state2['note_requests'], [])
        c3 = self.send(self.contribute_payload(COMMIT_A))['comment_id']
        self.assertEqual(w.project(self.issue)['contribution']['comment_id'], c3)


class SharedHttpSameCommitRefusalTests:
    """Shared HTTP scenarios run against both InProcessBackend and EndpointBackend."""

    def base(self, task):
        return '/v1/projects/%s/tasks/%s' % (self.project, task)

    def read_review(self, task):
        return self.request('GET', self.base(task) + '/brief', token=self.owner).data['review']

    def assert_http_refused_409(self, response, expected_review_id, answered=False):
        expected_sentence = ANSWERED_SENTENCE if answered else SENTENCE
        self.assertEqual(409, response.status, response.data)
        err = response.data['error']
        self.assertEqual('conflict', err['code'], response.data)
        self.assertIn(expected_sentence, err['message'])
        self.assertIn(expected_review_id, err['message'])
        self.assertEqual('%s (review %s)' % (expected_sentence, expected_review_id), err['message'])
        self.assertEqual({'review_id': expected_review_id}, err.get('detail'))

    def test_01_same_commit_open_changes_requested_refused_409_nothing_recorded_queue_unchanged(self):
        task, c1 = self.deliver('task-01', COMMIT_A)
        req_id = self.ask_changes(self.reviewer, task, c1)
        before_brief = self.read_review(task)
        before_queue = self.request('GET', '/v1/projects/%s/queue' % self.project, token=self.owner).data['items']
        before_to_review = self.request('GET', '/v1/me/work', token=self.owner).data['to_review']

        refused = self.post_contribute(task, COMMIT_A)
        self.assert_http_refused_409(refused, req_id)

        self.assertEqual(before_brief, self.read_review(task))
        self.assertEqual(before_queue, self.request('GET', '/v1/projects/%s/queue' % self.project, token=self.owner).data['items'])
        self.assertEqual(before_to_review, self.request('GET', '/v1/me/work', token=self.owner).data['to_review'])

    def test_02_same_commit_with_different_base_commit_branch_or_summary_still_refused(self):
        task, c1 = self.deliver('task-02', COMMIT_A)
        req_id = self.ask_changes(self.reviewer, task, c1)
        for label, extra in (
            ('base_commit', {'base_commit': BASE_2}),
            ('remote_branch', {'delivery': {'kind': 'remote', 'remote': 'ssh://git.example/repo', 'branch': 'new-b'},
                               'bundle_sha256': None}),
            ('summary', {'summary': 'Changed summary text only'}),
        ):
            with self.subTest(variant=label):
                refused = self.post_contribute(task, COMMIT_A, **extra)
                self.assert_http_refused_409(refused, req_id)

    def test_03_new_commit_after_changes_requested_accepted_as_today(self):
        task, c1 = self.deliver('task-03', COMMIT_A)
        self.ask_changes(self.reviewer, task, c1)
        accepted = self.post_contribute(task, COMMIT_B)
        self.assertEqual(201, accepted.status, accepted.data)
        self.assertEqual(COMMIT_B, self.read_review(task)['contribution']['commit'])

    def test_04_same_commit_after_approval_and_after_rejection_keeps_todays_behaviour(self):
        # Approval direct & after request-changes + respond -> same commit accepted (201), awaiting-review.
        task, c1 = self.deliver('task-04', COMMIT_A)
        self.assertEqual(201, self.post_approve(self.owner, task, c1).status)
        second = self.post_contribute(task, COMMIT_A)
        self.assertEqual(201, second.status, second.data)
        c2 = self.read_review(task)['contribution']['id']
        self.assertEqual('awaiting-review', self.read_review(task)['state'])

        req_id = self.ask_changes(self.reviewer, task, c2)
        self.assertEqual(201, self.post_respond(task, c2, req_id, 'fix-1').status)
        self.assertEqual(201, self.post_approve(self.owner, task, c2).status)
        third = self.post_contribute(task, COMMIT_A)
        self.assertEqual(201, third.status, third.data)
        self.assertEqual('awaiting-review', self.read_review(task)['state'])

        # Rejection / withdrawal behaviour on each HTTP backend.
        self.check_withdrawal_behaviour()

    def test_05_same_commit_after_respond_before_rereview_refused_409(self):
        task, c1 = self.deliver('task-05', COMMIT_A)
        r1 = self.ask_changes(self.reviewer, task, c1, items=[{'id': 'i1', 'text': 'First fix'}])
        r2 = self.ask_changes(self.owner, task, c1, items=[{'id': 'i2', 'text': 'Second fix'}])

        # Partial respond (r2 answered, r1 still open): 409 naming r1 with the open sentence.
        partial_resp = self.post_respond(task, c1, r2, 'i2')
        self.assertEqual(201, partial_resp.status, partial_resp.data)
        self.assertEqual('changes-requested', self.read_review(task)['state'])
        refused_open = self.post_contribute(task, COMMIT_A)
        self.assert_http_refused_409(refused_open, r1, answered=False)

        # Full respond (all blocking items answered, awaiting re-review): 409 naming r2 with answered sentence.
        answered = self.post_respond(task, c1, r1, 'i1')
        self.assertEqual(201, answered.status, answered.data)
        self.assertEqual('awaiting-review', self.read_review(task)['state'])

        refused = self.post_contribute(task, COMMIT_A)
        self.assert_http_refused_409(refused, r2, answered=True)
        self.assertEqual(c1, self.read_review(task)['contribution']['id'])

    def test_06_same_commit_on_different_task_accepted(self):
        task_a, c1 = self.deliver('task-06-a', COMMIT_A)
        self.ask_changes(self.reviewer, task_a, c1)
        second_agent_token, _ = self.make_second_worker()
        task_b, _ = self.deliver('task-06-b', COMMIT_A, token=second_agent_token)
        self.assertEqual(COMMIT_A, self.read_review(task_b)['contribution']['commit'])

    def test_07_reassigned_task_different_actor_same_commit_refused(self):
        task, c1 = self.deliver('task-07', COMMIT_A)
        req_id = self.ask_changes(self.reviewer, task, c1)
        second_agent_token, second_actor = self.make_second_worker()
        self.reassign_task(task, second_actor)
        refused = self.post_contribute(task, COMMIT_A, token=second_agent_token)
        self.assert_http_refused_409(refused, req_id)

    def test_08_idempotent_retry_of_original_contribute_returns_original_result(self):
        task = self.make_claimed_task('task-08')
        idem_key = 'idem-orig-' + secrets.token_hex(6)
        op_id = 'op-orig-' + secrets.token_hex(6)
        body = self.build_contribute_body(task, COMMIT_A, operation_id=op_id)
        first = self.request('POST', self.base(task) + '/reviews', body, token=self.agent, key=idem_key)
        self.assertEqual(201, first.status, first.data)
        c1 = self.read_review(task)['contribution']['id']
        self.ask_changes(self.reviewer, task, c1)

        replayed = self.request('POST', self.base(task) + '/reviews', body, token=self.agent, key=idem_key)
        self.assertEqual(201, replayed.status, replayed.data)
        self.assertEqual(
            first.data.get('comment_id') or first.data['contribution']['id'],
            replayed.data.get('comment_id') or replayed.data['contribution']['id'],
        )

    def test_09_legacy_changes_requested_rows_still_trigger_refusal(self):
        task, c1 = self.deliver('task-09', COMMIT_A)
        legacy_id = self.plant_legacy_request_changes(task, c1)
        refused = self.post_contribute(task, COMMIT_A)
        self.assert_http_refused_409(refused, legacy_id)

    def test_10_http_contribute_route_returns_same_409_refusal(self):
        task, c1 = self.deliver('task-10', COMMIT_A)
        req_id = self.ask_changes(self.reviewer, task, c1)
        refused = self.post_contribute(task, COMMIT_A)
        self.assert_http_refused_409(refused, req_id)

    def test_11_note_only_request_changes_does_not_trigger_refusal(self):
        task, c1 = self.deliver('task-11', COMMIT_A)
        note_id = self.ask_changes(self.reviewer, task, c1,
                                   items=[{'id': 'nit-1', 'text': 'Optional style suggestion', 'severity': 'note'}])
        self.assertTrue(note_id)
        self.assertEqual('awaiting-review', self.read_review(task)['state'])
        accepted = self.post_contribute(task, COMMIT_A)
        self.assertEqual(201, accepted.status, accepted.data)

    def test_latest_delivery_targeted_across_multiple_rounds(self):
        task, c1 = self.deliver('task-rounds-1', COMMIT_A)
        self.assertEqual(201, self.post_approve(self.owner, task, c1).status)
        self.assertEqual(201, self.post_contribute(task, COMMIT_B).status)
        c2 = self.read_review(task)['contribution']['id']
        self.ask_changes(self.reviewer, task, c2)
        reverted = self.post_contribute(task, COMMIT_A)
        self.assertEqual(201, reverted.status, reverted.data)

        task2_token, _ = self.make_second_worker()
        task2, t2_c1 = self.deliver('task-rounds-2', COMMIT_A, token=task2_token)
        self.ask_changes(self.reviewer, task2, t2_c1)
        self.assertEqual(201, self.post_contribute(task2, COMMIT_B, token=task2_token).status)
        t2_c2 = self.read_review(task2)['contribution']['id']
        t2_r2 = self.ask_changes(self.reviewer, task2, t2_c2)
        refused = self.post_contribute(task2, COMMIT_B, token=task2_token)
        self.assert_http_refused_409(refused, t2_r2)

    def test_16_operation_id_retry_reconciles_on_both_backends_with_and_without_open_request(self):
        # s2-inprocess-retry: an operation-id retry of the ORIGINAL contribute (with a new
        # Idempotency-Key or with no Idempotency-Key) reconciles (201, reconciled: True) on
        # BOTH HTTP backends, both when no request is open (ending the old duplicate) and
        # while a request-changes is open; same operation_id with different payload gets 422.
        task = self.make_claimed_task('task-16-op-retry')
        op_id = 'op-s2-' + secrets.token_hex(6)
        body = self.build_contribute_body(task, COMMIT_A, operation_id=op_id)
        first = self.request('POST', self.base(task) + '/reviews', body,
                             token=self.agent, key='idem-s2-1-' + secrets.token_hex(4))
        self.assertEqual(201, first.status, first.data)
        orig_id = first.data.get('comment_id') or first.data['contribution']['id']

        # (a) Without any open request-changes: new Idempotency-Key and no Idempotency-Key reconcile.
        retry_no_req_key = self.request('POST', self.base(task) + '/reviews', copy.deepcopy(body),
                                        token=self.agent, key='idem-s2-2-' + secrets.token_hex(4))
        self.assertEqual(201, retry_no_req_key.status, retry_no_req_key.data)
        self.assertTrue(retry_no_req_key.data.get('reconciled'), retry_no_req_key.data)
        self.assertEqual(orig_id, retry_no_req_key.data.get('comment_id') or retry_no_req_key.data['contribution']['id'])
        self.assertEqual(1, self.contribution_record_count(task))

        retry_no_req_nokey = self.request('POST', self.base(task) + '/reviews', copy.deepcopy(body),
                                          token=self.agent)
        self.assertEqual(201, retry_no_req_nokey.status, retry_no_req_nokey.data)
        self.assertTrue(retry_no_req_nokey.data.get('reconciled'), retry_no_req_nokey.data)
        self.assertEqual(orig_id, retry_no_req_nokey.data.get('comment_id') or retry_no_req_nokey.data['contribution']['id'])
        self.assertEqual(1, self.contribution_record_count(task))

        # (b) While a request-changes IS open: new Idempotency-Key and no Idempotency-Key still reconcile (201, not 409).
        self.ask_changes(self.reviewer, task, orig_id)
        retry_open_key = self.request('POST', self.base(task) + '/reviews', copy.deepcopy(body),
                                      token=self.agent, key='idem-s2-3-' + secrets.token_hex(4))
        self.assertEqual(201, retry_open_key.status, retry_open_key.data)
        self.assertTrue(retry_open_key.data.get('reconciled'), retry_open_key.data)
        self.assertEqual(orig_id, retry_open_key.data.get('comment_id') or retry_open_key.data['contribution']['id'])
        self.assertEqual(1, retry_open_key.data.get('open_items_total'))
        self.assertEqual(1, self.contribution_record_count(task))

        retry_open_nokey = self.request('POST', self.base(task) + '/reviews', copy.deepcopy(body),
                                        token=self.agent)
        self.assertEqual(201, retry_open_nokey.status, retry_open_nokey.data)
        self.assertTrue(retry_open_nokey.data.get('reconciled'), retry_open_nokey.data)
        self.assertEqual(orig_id, retry_open_nokey.data.get('comment_id') or retry_open_nokey.data['contribution']['id'])
        self.assertEqual(1, self.contribution_record_count(task))

        # (c) Reusing the same operation_id with a different payload is refused with 422.
        altered = dict(body, summary='Different summary under the same operation_id')
        mismatch = self.request('POST', self.base(task) + '/reviews', altered, token=self.agent)
        self.assertEqual(422, mismatch.status, mismatch.data)
        self.assertIn('Operation ID already used with different payload or actor',
                      json.dumps(mismatch.data))


class InProcessHttpSameCommitRefusalTests(SharedHttpSameCommitRefusalTests, test_http_agents.AgentHarness):
    """HTTP coverage on InProcessBackend."""

    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.project = self.create_project(admin, 'Alpha')
        tokens = {}
        for name, role in (('olive', 'owner'), ('carl', 'contributor'), ('rita', 'contributor'), ('dana', 'contributor')):
            uid = self.create_account(admin, name, name + '-password-1')
            self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, uid), {'role': role}, token=admin)
            tokens[name] = self.login(name, name + '-password-1')[0]
        self.owner, self.contributor, self.reviewer, self.dana = (
            tokens['olive'], tokens['carl'], tokens['rita'], tokens['dana']
        )
        made = self.request('POST', '/v1/agents', {
            'name': 'Kestrel', 'working_directory': '/home/carl/k', 'projects': [self.project]
        }, token=self.contributor)
        self.agent = made.data['credential']['secret']

    def make_claimed_task(self, title, token=None):
        task = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': title}, token=self.owner).data['id']
        self.assertEqual(200, self.request('POST', self.base(task) + '/claim', token=token or self.agent).status)
        return task

    def build_contribute_body(self, task, commit, operation_id=None, **extra):
        body = {
            'operation': 'contribute',
            'commit': commit,
            'base_commit': BASE_1,
            'bundle_sha256': BUNDLE_1,
            'summary': 'Delivered revision',
        }
        if operation_id is not None:
            body['operation_id'] = operation_id
        for key, value in extra.items():
            if value is None:
                body.pop(key, None)
            else:
                body[key] = value
        return body

    def post_contribute(self, task, commit, token=None, **extra):
        body = self.build_contribute_body(task, commit, **extra)
        return self.request('POST', self.base(task) + '/reviews', body, token=token or self.agent)

    def deliver(self, title, commit, token=None):
        task = self.make_claimed_task(title, token=token)
        made = self.post_contribute(task, commit, token=token)
        self.assertEqual(201, made.status, made.data)
        return task, made.data['contribution']['id']

    def ask_changes(self, token, task, contribution, items=None):
        if items is None:
            items = [{'id': 'fix-1', 'text': 'Fix the edge case'}]
        res = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'request-changes', 'contribution': contribution, 'items': items
        }, token=token)
        self.assertEqual(201, res.status, res.data)
        return res.data['review']['id']

    def post_respond(self, task, contribution, req_id, item_id):
        return self.request('POST', self.base(task) + '/reviews', {
            'operation': 'respond',
            'contribution': contribution,
            'resolutions': [{'request': req_id, 'item': item_id, 'reason': 'Addressed', 'evidence': 'commit'}],
        }, token=self.agent)

    def post_approve(self, token, task, contribution):
        return self.request('POST', self.base(task) + '/reviews', {
            'operation': 'approve', 'contribution': contribution, 'summary': 'Accepted'
        }, token=token)

    def make_second_worker(self):
        made = self.request('POST', '/v1/agents', {
            'name': 'Falcon', 'working_directory': '/home/dana/f', 'projects': [self.project]
        }, token=self.dana)
        self.assertEqual(201, made.status, made.data)
        return made.data['credential']['secret'], made.data['agent']['id']

    def reassign_task(self, task, new_actor):
        self.backend._task(self.project, task)['assignee'] = new_actor

    def plant_legacy_request_changes(self, task, contribution):
        # Legacy InProcessBackend row: summary-only, no `items` and no `needs_respond` flag.
        legacy_id = 'rev_legacy01'
        self.backend.state['contributions'][task].append({
            'id': legacy_id,
            'task_id': task,
            'kind': 'request-changes',
            'contribution_id': contribution,
            'summary': 'Legacy summary-only request',
            'items': [],
            'actor': 'rita',
            'created_at': STAMP,
        })
        self.backend._task(self.project, task)['review_state'] = 'changes-requested'
        return legacy_id

    def check_withdrawal_behaviour(self):
        # Today's InProcessBackend behaviour: withdraw is unsupported (422).
        task, c1 = self.deliver('task-04-withdraw', COMMIT_A)
        res = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'withdraw', 'contribution': c1, 'disposition': 'withdrawn', 'reason': 'Drop'
        }, token=self.owner)
        self.assertEqual(422, res.status, res.data)

    def contribution_record_count(self, task):
        return sum(1 for r in self.backend.state['contributions'].get(task, [])
                   if r.get('kind') == 'contribution')


class EndpointHttpSameCommitRefusalTests(SharedHttpSameCommitRefusalTests, fixes.EndpointCase):
    """HTTP coverage on EndpointBackend + strict_canonical_endpoint.py."""

    def setUp(self):
        super().setUp()
        self.canonical_root.mkdir(parents=True, exist_ok=True)
        (self.canonical_root / 'deployment.private.json').write_text(
            json.dumps({'operators': ['operator'], 'review_workflow_writes': True}), encoding='utf-8'
        )
        admin = self.admin_token()
        self.project = self.create_project(admin, 'Alpha')
        tokens = {}
        for name, role in (('olive', 'owner'), ('carl', 'contributor'), ('rita', 'contributor'), ('dana', 'contributor')):
            uid = self.create_account(admin, name, name + '-password-1')
            added = self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, uid), {'role': role}, token=admin)
            self.assertIn(added.status, (200, 201), added.data)
            tokens[name] = self.login(name, name + '-password-1')[0]
        self.owner, self.contributor, self.reviewer, self.dana = (
            tokens['olive'], tokens['carl'], tokens['rita'], tokens['dana']
        )
        made = self.request('POST', '/v1/agents', {
            'name': 'Kestrel', 'working_directory': '/home/carl/k', 'projects': [self.project]
        }, token=self.contributor)
        self.assertEqual(201, made.status, made.data)
        self.agent = made.data['credential']['secret']

    def _mutate_canonical(self, fn):
        path = self.canonical_root / 'canonical.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        fn(state)
        path.write_text(json.dumps(state), encoding='utf-8')

    def make_claimed_task(self, title, token=None):
        task = self.create_task(self.owner, self.project, title).data['id']
        self.assertEqual(200, self.request('POST', self.base(task) + '/claim', {}, token=token or self.agent).status)
        return task

    def build_contribute_body(self, task, commit, operation_id=None, **extra):
        review = self.read_review(task)
        current = (review.get('contribution') or {}).get('id')
        body = dict(
            fixes.CONTRIBUTION,
            commit=commit,
            base_commit=BASE_1,
            operation='contribute',
            schema_version=1,
            operation_id=operation_id or ('op-' + secrets.token_hex(6)),
            previous=review.get('latest_id'),
            supersedes=current,
        )
        for key, value in extra.items():
            if value is None:
                body.pop(key, None)
            else:
                body[key] = value
        return body

    def post_contribute(self, task, commit, token=None, **extra):
        body = self.build_contribute_body(task, commit, **extra)
        return self.request('POST', self.base(task) + '/reviews', body, token=token or self.agent)

    def deliver(self, title, commit, token=None):
        task = self.make_claimed_task(title, token=token)
        made = self.post_contribute(task, commit, token=token)
        self.assertEqual(201, made.status, made.data)
        return task, made.data['comment_id']

    def ask_changes(self, token, task, contribution, items=None):
        if items is None:
            items = [{'id': 'fix-1', 'text': 'Fix the edge case'}]
        latest = self.read_review(task)['latest_id']
        res = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'request-changes',
            'schema_version': 1,
            'operation_id': 'op-' + secrets.token_hex(6),
            'previous': latest,
            'contribution': contribution,
            'items': items,
        }, token=token)
        self.assertEqual(201, res.status, res.data)
        return res.data['comment_id']

    def post_respond(self, task, contribution, req_id, item_id):
        latest = self.read_review(task)['latest_id']
        return self.request('POST', self.base(task) + '/reviews', {
            'operation': 'respond',
            'schema_version': 1,
            'operation_id': 'op-' + secrets.token_hex(6),
            'previous': latest,
            'contribution': contribution,
            'resolutions': [{'request': req_id, 'item': item_id, 'reason': 'Addressed', 'evidence': 'commit'}],
        }, token=self.agent)

    def post_approve(self, token, task, contribution):
        latest = self.read_review(task)['latest_id']
        return self.request('POST', self.base(task) + '/reviews', {
            'operation': 'approve',
            'schema_version': 1,
            'operation_id': 'op-' + secrets.token_hex(6),
            'previous': latest,
            'contribution': contribution,
            'summary': 'Accepted',
        }, token=token)

    def make_second_worker(self):
        made = self.request('POST', '/v1/agents', {
            'name': 'Falcon', 'working_directory': '/home/dana/f', 'projects': [self.project]
        }, token=self.dana)
        self.assertEqual(201, made.status, made.data)
        secret = made.data['credential']['secret']
        actor = self.request('GET', '/v1/agents/me', token=secret).data['agent']['actor']
        return secret, actor

    def reassign_task(self, task, new_actor):
        def update(state):
            next(r for r in state['rows'] if r['id'] == task)['assignee'] = new_actor
        self._mutate_canonical(update)

    def plant_legacy_request_changes(self, task, contribution):
        legacy_id = 'legacy-http-r1'
        payload = {
            'schema_version': 1,
            'operation': 'request-changes',
            'operation_id': 'legacy-http-op-1',
            'task': task,
            'previous': contribution,
            'contribution': contribution,
            'items': [{'id': 'legacy-item', 'text': 'Pre-severity request'}],
        }
        def update(state):
            row = next(r for r in state['rows'] if r['id'] == task)
            row['comments'].append({
                'id': legacy_id,
                'author': 'rita',
                'created_at': STAMP,
                'text': w.PREFIX + canonical_bytes(payload).decode(),
            })
        self._mutate_canonical(update)
        return legacy_id

    def check_withdrawal_behaviour(self):
        task, c1 = self.deliver('task-04-withdraw', COMMIT_A)
        self.ask_changes(self.reviewer, task, c1)
        latest = self.read_review(task)['latest_id']
        withdrawn = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'withdraw',
            'schema_version': 1,
            'operation_id': 'op-' + secrets.token_hex(6),
            'previous': latest,
            'contribution': c1,
            'disposition': 'withdrawn',
            'reason': 'Withdrawing broken bundle',
        }, token=self.agent)
        self.assertEqual(201, withdrawn.status, withdrawn.data)
        self.assertEqual('withdrawn', self.read_review(task)['state'])
        accepted = self.post_contribute(task, COMMIT_A)
        self.assertEqual(201, accepted.status, accepted.data)
        self.assertEqual('changes-requested', self.read_review(task)['state'])

    def test_12_operator_voided_request_changes_does_not_trigger_refusal(self):
        task, c1 = self.deliver('task-12-void', COMMIT_A)

        def apply_fork_and_void(state):
            row = next(r for r in state['rows'] if r['id'] == task)
            forked_req = {
                'schema_version': 1,
                'operation': 'request-changes',
                'operation_id': 'fork-http-req-1',
                'task': task,
                'previous': c1,
                'contribution': c1,
                'items': [{'id': 'fix-fork', 'text': 'Forked blocking request'}],
            }
            forked_text = w.PREFIX + canonical_bytes(forked_req).decode()
            row['comments'].append({
                'id': 'r-http-fork',
                'author': 'rita',
                'created_at': STAMP,
                'text': forked_text,
            })
            surviving_note = {
                'schema_version': 1,
                'operation': 'request-changes',
                'operation_id': 'fork-http-note-1',
                'task': task,
                'previous': c1,
                'contribution': c1,
                'items': [{'id': 'nit-1', 'text': 'Surviving note', 'severity': 'note'}],
            }
            row['comments'].append({
                'id': 'r-http-surviving',
                'author': 'olive',
                'created_at': STAMP,
                'text': w.PREFIX + canonical_bytes(surviving_note).decode(),
            })
            void_payload = {
                'schema_version': 1,
                'operation': 'void-record',
                'operation_id': 'void-http-1',
                'task': task,
                'target': 'r-http-fork',
                'target_kind': 'contribution-review',
                'target_sha256': recovery.digest(forked_text),
                'original': forked_text,
                'reason': 'Reconcile forked request-changes record',
                'disposition': 'void',
                'operator': 'operator',
            }
            row['comments'].append({
                'id': 'void-c1',
                'author': 'operator',
                'created_at': STAMP,
                'text': recovery.PREFIX + canonical_bytes(void_payload).decode(),
            })
        self._mutate_canonical(apply_fork_and_void)
        self.assertEqual('awaiting-review', self.read_review(task)['state'])
        accepted = self.post_contribute(task, COMMIT_A)
        self.assertEqual(201, accepted.status, accepted.data)

    def contribution_record_count(self, task):
        state = json.loads((self.canonical_root / 'canonical.json').read_text(encoding='utf-8'))
        row = next(r for r in state['rows'] if r['id'] == task)
        count = 0
        for c in row.get('comments') or []:
            text = c.get('text') or ''
            if text.startswith(w.PREFIX) and '"operation":"contribute"' in text:
                count += 1
        return count

    def test_13_http_voided_and_stale_approvals_refuse_with_answered_sentence_c07_c08(self):
        # c07 over Endpoint HTTP: voided approval after respond still refuses 409 with answered sentence.
        task_c07, c1 = self.deliver('task-13-c07', COMMIT_A)
        r1 = self.ask_changes(self.reviewer, task_c07, c1)
        resp = self.post_respond(task_c07, c1, r1, 'fix-1')
        self.assertEqual(201, resp.status, resp.data)
        resp_id = resp.data['comment_id']

        def apply_voided_approval(state):
            row = next(r for r in state['rows'] if r['id'] == task_c07)
            forked_app = {
                'schema_version': 1, 'operation': 'approve', 'operation_id': 'http-c07-app',
                'task': task_c07, 'previous': resp_id, 'contribution': c1, 'summary': 'Forked approval',
            }
            app_text = w.PREFIX + canonical_bytes(forked_app).decode()
            row['comments'].append({'id': 'http-app-fork', 'author': 'olive', 'created_at': STAMP, 'text': app_text})
            surviving_note = {
                'schema_version': 1, 'operation': 'request-changes', 'operation_id': 'http-c07-note',
                'task': task_c07, 'previous': resp_id, 'contribution': c1,
                'items': [{'id': 'nit', 'text': 'Surviving note', 'severity': 'note'}],
            }
            row['comments'].append({
                'id': 'http-note-c07', 'author': 'rita', 'created_at': STAMP,
                'text': w.PREFIX + canonical_bytes(surviving_note).decode(),
            })
            void_p = {
                'schema_version': 1, 'operation': 'void-record', 'operation_id': 'http-void-c07',
                'task': task_c07, 'target': 'http-app-fork', 'target_kind': 'contribution-review',
                'target_sha256': recovery.digest(app_text), 'original': app_text,
                'reason': 'Void forked approval', 'disposition': 'void', 'operator': 'operator',
            }
            row['comments'].append({
                'id': 'http-void-app-1', 'author': 'operator', 'created_at': STAMP,
                'text': recovery.PREFIX + canonical_bytes(void_p).decode(),
            })
        self._mutate_canonical(apply_voided_approval)
        self.assertEqual('awaiting-review', self.read_review(task_c07)['state'])
        self.assert_http_refused_409(self.post_contribute(task_c07, COMMIT_A), r1, answered=True)

        # c08 / O1 over Endpoint HTTP: approval in chain made stale by a later applied void still refuses 409.
        task_c08, c8 = self.deliver('task-13-c08', COMMIT_A)
        r8 = self.ask_changes(self.reviewer, task_c08, c8)
        resp8 = self.post_respond(task_c08, c8, r8, 'fix-1')
        self.assertEqual(201, resp8.status, resp8.data)
        resp8_id = resp8.data['comment_id']

        def apply_stale_approval(state):
            row = next(r for r in state['rows'] if r['id'] == task_c08)
            forked_req = {
                'schema_version': 1, 'operation': 'request-changes', 'operation_id': 'http-c08-fork',
                'task': task_c08, 'previous': resp8_id, 'contribution': c8,
                'items': [{'id': 'dup', 'text': 'Forked request'}],
            }
            fork_text = w.PREFIX + canonical_bytes(forked_req).decode()
            row['comments'].append({'id': 'http-r-fork-c08', 'author': 'rita', 'created_at': STAMP, 'text': fork_text})
            surviving_app = {
                'schema_version': 1, 'operation': 'approve', 'operation_id': 'http-c08-app',
                'task': task_c08, 'previous': resp8_id, 'contribution': c8, 'summary': 'Before void',
            }
            row['comments'].append({
                'id': 'http-app-stale', 'author': 'olive', 'created_at': STAMP,
                'text': w.PREFIX + canonical_bytes(surviving_app).decode(),
            })
            void_p = {
                'schema_version': 1, 'operation': 'void-record', 'operation_id': 'http-void-c08',
                'task': task_c08, 'target': 'http-r-fork-c08', 'target_kind': 'contribution-review',
                'target_sha256': recovery.digest(fork_text), 'original': fork_text,
                'reason': 'Void forked request after approval', 'disposition': 'void', 'operator': 'operator',
            }
            row['comments'].append({
                'id': 'http-void-after-app', 'author': 'operator', 'created_at': STAMP,
                'text': recovery.PREFIX + canonical_bytes(void_p).decode(),
            })
        self._mutate_canonical(apply_stale_approval)
        self.assertEqual('awaiting-review', self.read_review(task_c08)['state'])
        self.assert_http_refused_409(self.post_contribute(task_c08, COMMIT_A), r8, answered=True)

    def test_15_http_requester_resolve_item_without_downgrade_clears_refusal_o2(self):
        task, c1 = self.deliver('task-15-o2', COMMIT_A)
        r1 = self.ask_changes(self.reviewer, task, c1, items=[{'id': 'b1', 'text': 'Blocking item'}])
        latest = self.read_review(task)['latest_id']
        resolved = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'resolve-item',
            'schema_version': 1,
            'operation_id': 'op-' + secrets.token_hex(6),
            'previous': latest,
            'contribution': c1,
            'request': r1,
            'item': 'b1',
            'disposition': 'resolved',
            'reason': 'Cleared without downgrading to note',
        }, token=self.reviewer)
        self.assertEqual(201, resolved.status, resolved.data)
        accepted = self.post_contribute(task, COMMIT_A)
        self.assertEqual(201, accepted.status, accepted.data)


if __name__ == '__main__':
    unittest.main()
