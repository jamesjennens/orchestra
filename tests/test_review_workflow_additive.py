"""Additive review-workflow operations (kittrial-5bb.94, revised).

Covers the four delivered scope items, the tolerant-reader rule, and the five
changes-requested review items:

1. withdraw/supersede by the author or a coordinator, and a closed task no longer
   offered as awaiting-review;
2. item severity (blocking|note) and requester self-resolution of their own item;
3. an optional bounded summary on request-changes that `review TASK` returns;
4. first-class request-review, visible in the named reviewer's queue and closed by
   their approve, request-changes or decline, and closed by task closure;
5. `withdraw` carrying unresolved items into the next contribution, refusing once
   the contribution reads integrated, refusing a second withdraw, and the plain-text
   rule on summary/reason fields.

Revision 2 of the review adds the staged-rollout switch: the readers here
understand every new operation and field, but WRITING one is refused unless the
per-installation setting `review_workflow_writes` is on (default off).
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin
import lifecycle
import work
import review_workflow as w
from requirements import content_hash

COMMIT_1 = 'a' * 40
MERGE_1 = 'e' * 40


def row(comments=None, assignee='worker', status='in_progress'):
    return dict(id='task-1', title='A task', issue_type='task', status=status,
                assignee=assignee, description='Intent', acceptance_criteria='Acceptance',
                labels=[], comments=list(comments or []))


def chain_row(task, reviewer='alice', author='worker', request=True):
    """One native row whose chain holds a contribution, optionally a review request."""
    contribution = dict(schema_version=1, operation='contribute', operation_id='c-' + task,
                        task=task, previous=None, supersedes=None,
                        repository='ssh://git.example/project', commit=COMMIT_1,
                        base_commit='b' * 40,
                        delivery=dict(kind='bundle', path='reviewer:/d.bundle', sha256='c' * 64),
                        summary='Implementation and test evidence')
    comments = [dict(id='1', text=w.PREFIX + json.dumps(contribution), author=author,
                     created_at='2026-09-16T00:00:00Z')]
    if request:
        payload = dict(schema_version=1, operation='request-review', operation_id='r-' + task,
                       task=task, previous='1', contribution='1', reviewer=reviewer)
        comments.append(dict(id='2', text=w.PREFIX + json.dumps(payload), author=author,
                             created_at='2026-09-16T00:00:00Z'))
    return dict(id=task, title='A task', issue_type='task', status='in_progress', assignee=author,
                description='', acceptance_criteria='', labels=[], comments=comments)


class Base(unittest.TestCase):
    def setUp(self):
        self.issue = row()
        self.rows = [self.issue]
        self.actor = 'worker'
        self.count = 0

    def payload(self, op, **extra):
        self.count += 1
        return dict(schema_version=1, operation=op, operation_id='op-' + str(self.count),
                    task='task-1', previous=w.project(self.issue)['latest_comment_id'], **extra)

    def contribution(self, **extra):
        prior = w.project(self.issue)['contribution']
        p = dict(repository='ssh://git.example/project', commit=COMMIT_1, base_commit='b' * 40,
                 delivery=dict(kind='bundle', path='reviewer:/deliveries/revision-1.bundle',
                               sha256='c' * 64),
                 summary='Implementation and test evidence',
                 supersedes=prior['comment_id'] if prior else None)
        p.update(extra)
        return self.payload('contribute', **p)

    def run_native(self, args):
        if args == ['export', '--all']:
            return ''.join(json.dumps(r) + '\n' for r in self.rows)
        self.assertEqual(args[:3], ['comments', 'add', 'task-1'])
        cid = str(len(self.issue['comments']) + 1)
        self.issue['comments'].append(dict(id=cid, text=args[3], author=self.actor,
                                           created_at='2026-09-16T00:00:00Z'))
        return json.dumps({'id': cid})

    def send(self, p, actor='worker', operators=None, review_writes=True):
        self.actor = actor
        return w.execute(self.rows, 'task-1', actor, p, self.run_native, operators=operators,
                         review_writes=review_writes)

    def review_count(self):
        return len([c for c in self.issue['comments'] if c['text'].startswith(w.PREFIX)])

    def record_lifecycle(self, source_commit, integration_commit, scope_op='scope-1',
                         actor='integrator'):
        """Append scoped lifecycle evidence marking source_commit integrated."""
        def run(args):
            if args == ['export', '--all']:
                return ''.join(json.dumps(r) + '\n' for r in self.rows)
            self.assertEqual(args[0], 'set-state')
            dim, value = args[2].split('=', 1)
            self.issue['labels'] = [x for x in self.issue['labels']
                                    if not x.startswith(dim + ':')] + [dim + ':' + value]
            event_id = 'task-1.' + str(len(self.rows))
            reason = args[args.index('--reason') + 1]
            self.rows.append(dict(_type='issue', id=event_id, issue_type='event',
                                  title='State change: ' + dim + ' \u2192 ' + value,
                                  description='Set ' + dim + ' to ' + value + '\n\nReason: ' + reason,
                                  status='closed', created_by=actor,
                                  created_at='2026-09-16T00:00:00Z',
                                  dependencies=[dict(issue_id=event_id, depends_on_id='task-1',
                                                     type='parent-child')]))
            return json.dumps(dict(changed=True, dimension=dim, event_id=event_id, new_value=value))

        scope = {'source_commit': source_commit, 'integration_commit': integration_commit,
                 'release_id': '', 'environment': ''}
        base = dict(schema_version=1, task='task-1', scope=scope,
                    evidence=['commit:' + source_commit], provenance='performed', actor=actor)
        lifecycle.apply_native(dict(base, operation_id=scope_op, dimension='lifecycle-scope',
                                    value=content_hash(scope)), actor, run)
        lifecycle.apply_native(dict(base, operation_id=scope_op + '-int', dimension='integrated',
                                    value='passed'), actor, run)

    def shared(self):
        """`review TASK`'s projection: the shared review-state overlay with scopes."""
        from review_state import project as reviewed, scopes_for
        return reviewed(self.issue, scopes_for(self.rows, 'task-1'))


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
        # A legacy request has no summary; the additive key is present and null.
        self.assertIsNone(state['pending_requests'][0]['summary'])
        # Every additive key is present and empty on an old-shaped chain.
        self.assertEqual(state['note_requests'], [])
        self.assertEqual(state['pending_review_requests'], [])
        self.assertEqual(state['declined_review_requests'], [])
        self.assertIsNone(state['withdrawal'])

    def test_validate_accepts_legacy_item_and_rejects_bad_severity(self):
        base = dict(schema_version=1, operation='request-changes', operation_id='op-validate',
                    task='task-1', previous=None, contribution='1',
                    items=[dict(id='fix', text='Fix')])
        w.validate(base, 'task-1')
        bad = dict(base, items=[dict(id='fix', text='Fix', severity='blocker')])
        with self.assertRaisesRegex(ValueError, 'severity'):
            w.validate(bad, 'task-1')

    def test_validate_rejects_a_reviewer_that_is_not_an_identity(self):
        base = dict(schema_version=1, operation='request-review', operation_id='op-rv',
                    task='task-1', previous='1', contribution='1', reviewer='bad name')
        with self.assertRaisesRegex(ValueError, 'reviewer'):
            w.validate(base, 'task-1')
        for good in ('alice@host', 'team/alice', 'session-ef51-2ea'):
            w.validate(dict(base, reviewer=good), 'task-1')


class StagedRolloutTests(Base):
    """Item 3: readers always understand; writing a new shape is opt-in, default off."""

    def test_the_switch_is_off_by_default_and_reads_the_deployment_configuration(self):
        # A deterministic scratch root inside the checkout: the sandboxed hosts that
        # run this suite do not all allow an OS temp directory, and the deployment
        # file must be a real file for the reader to be tested at all.
        root = Path(__file__).resolve().parents[1] / '.review-switch-probe'
        marker = root / 'deployment.private.json'
        try:
            root.mkdir(exist_ok=True)
            self.assertFalse(admin.review_workflow_writes(root))
            marker.write_text(json.dumps({'review_workflow_writes': True}), encoding='utf-8')
            self.assertTrue(admin.review_workflow_writes(root))
            marker.write_text(json.dumps({'review_workflow_writes': False}), encoding='utf-8')
            self.assertFalse(admin.review_workflow_writes(root))
            # A malformed value is read as OFF with a warning instead of raising, so
            # `work` and `review` keep answering for everyone (item 2).
            warnings = []
            marker.write_text(json.dumps({'review_workflow_writes': 'yes'}), encoding='utf-8')
            self.assertFalse(admin.review_workflow_writes(root, warnings=warnings))
            self.assertEqual(len(warnings), 1)
            self.assertIn('review_workflow_writes', warnings[0])
            self.assertIn('true or false', warnings[0])
        finally:
            if marker.is_file():
                marker.unlink()
            if root.is_dir():
                root.rmdir()

    def test_every_new_shape_is_refused_with_the_switch_off(self):
        contribution = self.send(self.contribution())['comment_id']
        request = self.send(self.payload('request-changes', contribution=contribution,
                                         items=[dict(id='fix', text='Fix')]), 'reviewer')['comment_id']
        cases = [
            self.payload('withdraw', contribution=contribution, reason='Re-scoped'),
            self.payload('request-review', contribution=contribution, reviewer='alice'),
            self.payload('resolve-item', contribution=contribution, request=request,
                         item='fix', reason='No longer applies'),
            self.payload('decline-review', contribution=contribution, request=request,
                         reason='Not my area'),
            self.payload('request-changes', contribution=contribution, summary='Because',
                         items=[dict(id='other', text='Fix')]),
            self.payload('request-changes', contribution=contribution,
                         items=[dict(id='other', text='Fix', severity='note')]),
        ]
        for payload in cases:
            with self.subTest(operation=payload['operation']), \
                    self.assertRaisesRegex(ValueError, 'review_workflow_writes off'):
                self.send(payload, 'reviewer' if payload['operation'] != 'withdraw' else 'worker',
                          review_writes=False)
        # Nothing was written: the chain is still contribute + request-changes.
        self.assertEqual(self.review_count(), 2)

    def test_a_legacy_request_changes_item_is_still_written_with_the_switch_off(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-changes', contribution=contribution,
                               items=[dict(id='fix', text='Fix')]), 'reviewer', review_writes=False)
        self.assertEqual(w.project(self.issue)['review_state'], 'changes-requested')

    def test_readers_understand_the_new_shapes_written_while_the_switch_was_on(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-changes', contribution=contribution, summary='Because',
                               items=[dict(id='fix', text='Fix', severity='note')]), 'reviewer')
        self.send(self.payload('request-review', contribution=contribution, reviewer='alice@host'))
        state = w.project(self.issue)
        self.assertEqual(state['note_requests'][0]['summary'], 'Because')
        self.assertEqual(state['pending_review_requests'][0]['reviewer'], 'alice@host')
        # The same records read with the switch off: reads never consult it.
        self.assertEqual(w.project(self.issue)['note_requests'], state['note_requests'])

    def test_an_exact_retry_still_reconciles_after_the_switch_is_turned_off(self):
        contribution = self.send(self.contribution())['comment_id']
        payload = self.payload('withdraw', contribution=contribution, reason='Re-scoped')
        first = self.send(payload)
        self.assertEqual(first['review_state'], 'withdrawn')
        again = self.send(payload, review_writes=False)
        self.assertTrue(again['reconciled'])
        self.assertEqual(again['comment_id'], first['comment_id'])


class WithdrawTests(Base):
    def test_author_withdraw_keeps_the_record_and_carries_the_items(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-changes', contribution=contribution,
                               items=[dict(id='fix', text='Fix')]), 'reviewer')
        self.assertEqual(w.project(self.issue)['review_state'], 'changes-requested')
        receipt = self.send(self.payload('withdraw', contribution=contribution,
                                         reason='Re-scoped; a different change is needed'))
        self.assertEqual(receipt['review_state'], 'withdrawn')
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'withdrawn')
        self.assertEqual(state['withdrawal']['reason'], 'Re-scoped; a different change is needed')
        self.assertEqual(state['withdrawal']['disposition'], 'withdrawn')
        # Item 1: the reviewer's unresolved item is NOT erased by the withdraw.
        self.assertEqual([i['item'] for i in state['pending_requests']], ['fix'])
        # The whole chain stays in history: contribute + request-changes + withdraw.
        self.assertEqual(self.review_count(), 3)

    def test_withdraw_then_recontribute_keeps_the_approve_gate_shut(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-changes', contribution=contribution,
                               items=[dict(id='a', text='One'), dict(id='b', text='Two'),
                                      dict(id='c', text='Three')]), 'reviewer')
        self.send(self.payload('withdraw', contribution=contribution, reason='Restart'))
        second = self.send(self.contribution())['comment_id']
        state = w.project(self.issue)
        self.assertEqual(state['contribution']['comment_id'], second)
        self.assertIsNone(state['withdrawal'])
        self.assertEqual(state['review_state'], 'changes-requested')
        self.assertEqual(sorted(i['item'] for i in state['pending_requests']), ['a', 'b', 'c'])
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            self.send(self.payload('approve', contribution=second, summary='Approved'), 'reviewer2')

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

    def test_withdraw_is_refused_once_the_contribution_reads_integrated(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('approve', contribution=contribution, summary='Reviewed'), 'reviewer')
        self.record_lifecycle(COMMIT_1, MERGE_1)
        self.assertEqual(self.shared()['review_state'], 'integrated')
        with self.assertRaisesRegex(ValueError, 'reads integrated'):
            self.send(self.payload('withdraw', contribution=contribution, reason='Too late'))
        # The refusal wrote nothing: the chain is still contribute + approve.
        self.assertEqual(self.review_count(), 2)
        self.assertEqual(self.shared()['review_state'], 'integrated')

    def test_a_second_withdraw_is_refused_and_does_not_flip_the_disposition(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('withdraw', contribution=contribution, reason='First',
                               disposition='withdrawn'))
        with self.assertRaisesRegex(ValueError, 'second withdraw'):
            self.send(self.payload('withdraw', contribution=contribution, reason='Second',
                                   disposition='superseded'))
        self.assertEqual(self.review_count(), 2)
        # A chain that already holds a second withdraw still reads, and the FIRST
        # disposition stands.
        clone = self.payload('withdraw', contribution=contribution, reason='Second',
                             disposition='superseded')
        clone['previous'] = w.project(self.issue)['latest_comment_id']
        self.issue['comments'].append(dict(id='9', text=w.PREFIX + json.dumps(clone), author='worker',
                                           created_at='2026-09-16T00:00:00Z'))
        state = w.project(self.issue)
        self.assertEqual(state['withdrawal']['disposition'], 'withdrawn')
        self.assertTrue(any('second withdraw' in line for line in state['warnings']))

    def test_approve_after_withdraw_is_refused(self):
        """The withdrawn revision is final; removing that check must fail a test."""
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('withdraw', contribution=contribution, reason='Re-scoped'))
        with self.assertRaisesRegex(ValueError, 'withdrawn; deliver a new revision'):
            self.send(self.payload('approve', contribution=contribution, summary='Approved'),
                      'reviewer')
        with self.assertRaisesRegex(ValueError, 'withdrawn; deliver a new revision'):
            self.send(self.payload('respond', contribution=contribution,
                                   resolutions=[dict(request='1', item='x', reason='r', evidence='e')]))
        self.assertEqual(self.review_count(), 2)

    def test_withdrawn_contribution_with_passing_integration_warns(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('approve', contribution=contribution, summary='Reviewed'), 'reviewer')
        self.send(self.payload('withdraw', contribution=contribution, reason='Withdrawn early'))
        # Integration recorded AFTER the withdraw, as the review describes.
        self.record_lifecycle(COMMIT_1, MERGE_1)
        state = self.shared()
        self.assertEqual(state['review_state'], 'withdrawn')
        self.assertEqual(state['integration']['fact'], 'passed')
        self.assertEqual([d['kind'] for d in state['integration_disagreements']],
                         ['withdrawn-integration-passed'])
        self.assertTrue(any(line.startswith('Integration after withdrawal') for line in state['warnings']))


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

    def test_resolve_item_matches_the_requester_by_normalised_name(self):
        """Item 5.1: the match is the normalised attribution key (self-declared)."""
        contribution = self.send(self.contribution())['comment_id']
        request = self.send(self.payload('request-changes', contribution=contribution,
                                         items=[dict(id='fix', text='Fix')]), 'RITA')['comment_id']
        self.send(self.payload('resolve-item', contribution=contribution, request=request,
                               item='fix', reason='Handled'), 'rita/anything')
        state = w.project(self.issue)
        self.assertEqual(state['pending_requests'], [])
        self.assertEqual(state['review_state'], 'awaiting-review')


class RequestChangesSummaryTests(Base):
    def test_summary_is_returned_in_review_and_is_bounded(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-changes', contribution=contribution,
                               summary='The reasoning the reviewer wants linked',
                               items=[dict(id='fix', text='Fix')]), 'reviewer')
        item = w.project(self.issue)['pending_requests'][0]
        self.assertEqual(item['text'], 'Fix')
        self.assertEqual(item['summary'], 'The reasoning the reviewer wants linked')
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

    def test_any_approval_closes_every_open_review_request(self):
        """Item 4: a request does not stay open after another reviewer approves."""
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-review', contribution=contribution, reviewer='alice'))
        self.send(self.payload('request-review', contribution=contribution, reviewer='bob'))
        self.send(self.payload('approve', contribution=contribution, summary='Reviewed'), 'carol')
        self.assertEqual(w.project(self.issue)['pending_review_requests'], [])

    def test_the_named_reviewer_declines_their_own_request(self):
        contribution = self.send(self.contribution())['comment_id']
        request = self.send(self.payload('request-review', contribution=contribution,
                                         reviewer='alice'))['comment_id']
        with self.assertRaisesRegex(ValueError, 'named reviewer'):
            self.send(self.payload('decline-review', contribution=contribution, request=request,
                                   reason='Not mine'), 'bob')
        self.send(self.payload('decline-review', contribution=contribution, request=request,
                               reason='Out of my area'), 'alice')
        state = w.project(self.issue)
        self.assertEqual(state['pending_review_requests'], [])
        self.assertEqual([d['request'] for d in state['declined_review_requests']], [request])
        self.assertEqual(work.queue([self.issue], 'alice', ['--mine'])['total'], 0)

    def test_a_closed_task_has_no_open_review_requests(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-review', contribution=contribution, reviewer='alice'))
        self.issue['status'] = 'closed'
        state = self.shared()
        self.assertEqual(state['pending_review_requests'], [])
        with self.assertRaisesRegex(ValueError, 'Reopen the closed task'):
            self.send(self.payload('request-review', contribution=contribution, reviewer='bob'))

    def test_open_review_requests_per_requester_are_capped(self):
        rows = [chain_row('task-1%02d' % index) for index in range(10)]
        target = chain_row('task-1', request=False)
        rows.append(target)
        self.issue = target
        self.rows = rows
        # The requester already holds the cap; one more is refused.
        with self.assertRaisesRegex(ValueError, 'open review requests'):
            self.send(self.payload('request-review', contribution='1', reviewer='alice'))
        # With one of the ten closed (declined), the same request is accepted.
        declined = dict(schema_version=1, operation='decline-review', operation_id='d-0',
                        task='task-100', previous='2', contribution='1', request='2', reason='Out')
        rows[0]['comments'].append(dict(id='3', text=w.PREFIX + json.dumps(declined), author='alice',
                                        created_at='2026-09-16T00:00:00Z'))
        self.send(self.payload('request-review', contribution='1', reviewer='alice'))
        self.assertEqual(len(w.project(target)['pending_review_requests']), 1)

    def test_the_queue_row_says_it_is_a_review_request(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-review', contribution=contribution, reviewer='alice',
                               summary='Boundary handling'))
        named = work.queue([self.issue], 'alice', ['--mine'])['items'][0]
        self.assertTrue(named['review_request'])
        self.assertEqual([r['reviewer'] for r in named['review_requests']], ['alice'])
        self.assertEqual(named['review_requests'][0]['summary'], 'Boundary handling')
        owner = work.queue([self.issue], 'worker', ['--mine'])['items'][0]
        self.assertFalse(owner['review_request'])
        self.assertEqual(owner['review_requests'], [])

    def test_reviewer_names_may_contain_at_and_slash(self):
        contribution = self.send(self.contribution())['comment_id']
        for reviewer in ('alice@host', 'team/alice'):
            with self.subTest(reviewer=reviewer):
                self.issue = row()
                self.rows = [self.issue]
                self.count = 0
                contribution = self.send(self.contribution())['comment_id']
                self.send(self.payload('request-review', contribution=contribution, reviewer=reviewer))
                self.assertEqual([r['reviewer'] for r in w.project(self.issue)['pending_review_requests']],
                                 [reviewer])


class PlainTextTests(Base):
    """Item 5.5: summaries and reasons carry the plain-text rule guidance uses."""

    def test_control_bidi_and_tag_characters_are_refused_on_write(self):
        contribution = self.send(self.contribution())['comment_id']
        with self.assertRaisesRegex(ValueError, 'no control characters'):
            self.send(self.payload('withdraw', contribution=contribution, reason='bad \x1b[31mred'))
        with self.assertRaisesRegex(ValueError, 'bidi, zero-width'):
            self.send(self.payload('request-changes', contribution=contribution,
                                   summary='left \u202eright', items=[dict(id='fix', text='Fix')]),
                      'reviewer')
        with self.assertRaisesRegex(ValueError, 'bidi, zero-width'):
            self.send(self.payload('request-changes', contribution=contribution,
                                   items=[dict(id='fix', text='tag \U000E0041')]), 'reviewer')
        with self.assertRaisesRegex(ValueError, 'zero-width'):
            self.send(self.payload('approve', contribution=contribution,
                                   summary='a\u200bb'), 'reviewer')
        # A joiner BETWEEN letters is legitimate text and is accepted.
        self.send(self.payload('request-changes', contribution=contribution,
                               summary='\u0645\u200c\u0646', items=[dict(id='fix', text='Fix')]),
                  'reviewer')
        # Only the contribution and the one accepted request were written.
        self.assertEqual(self.review_count(), 2)

    def test_reads_stay_tolerant_of_records_written_before_the_rule(self):
        """The rule is a write check; validate() must still accept old bytes."""
        payload = dict(schema_version=1, operation='request-changes', operation_id='op-old',
                       task='task-1', previous='1', contribution='1', summary='bad \x1b[31mred',
                       items=[dict(id='fix', text='Fix')])
        w.validate(payload, 'task-1')
        self.issue['comments'].append(dict(id='1', text=w.PREFIX + json.dumps(
            dict(schema_version=1, operation='contribute', operation_id='c1', task='task-1',
                 previous=None, supersedes=None, repository='ssh://git.example/project',
                 commit=COMMIT_1, base_commit='b' * 40,
                 delivery=dict(kind='bundle', path='p', sha256='c' * 64), summary='s')),
            author='worker', created_at='2026-09-16T00:00:00Z'))
        self.issue['comments'].append(dict(id='2', text=w.PREFIX + json.dumps(payload),
                                           author='reviewer', created_at='2026-09-16T00:00:00Z'))
        state = w.project(self.issue)
        self.assertEqual(state['pending_requests'][0]['summary'], 'bad \x1b[31mred')


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


class ClosedWithdrawnItemTests(Base):
    """kittrial-5bb.110 item 4: a closed withdrawn task with a blocking item open."""

    def test_closed_withdrawn_task_with_a_blocking_item_stays_in_work(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-changes', contribution=contribution,
                               items=[dict(id='fix', text='Fix')]), 'reviewer')
        self.send(self.payload('withdraw', contribution=contribution, reason='Re-scoped'))
        state = self.shared()
        self.assertEqual(state['review_state'], 'withdrawn')
        # The withdraw carries the blocking item (it is not erased).
        self.assertEqual([i['item'] for i in state['pending_requests']], ['fix'])
        self.issue['status'] = 'closed'
        page = work.queue([self.issue], 'worker', ['--mine'])
        self.assertEqual([i['task'] for i in page['items']], ['task-1'])
        self.assertEqual(page['items'][0]['review_state'], 'withdrawn')

    def test_closed_withdrawn_task_with_no_open_item_is_hidden(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('withdraw', contribution=contribution, reason='Re-scoped'))
        self.issue['status'] = 'closed'
        self.assertEqual(work.queue([self.issue], 'worker', ['--mine'])['total'], 0)


class ReopenAndResolveTests(Base):
    """kittrial-5bb.110 items 5 and 6: closure hides requests on read; withdraw freezes items."""

    def test_reopening_a_task_revives_its_open_review_requests(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-review', contribution=contribution, reviewer='alice'))
        self.issue['status'] = 'closed'
        self.assertEqual(self.shared()['pending_review_requests'], [])
        self.assertEqual(work.queue([self.issue], 'alice', ['--mine'])['total'], 0)
        self.issue['status'] = 'in_progress'
        self.assertEqual([r['reviewer'] for r in self.shared()['pending_review_requests']], ['alice'])
        self.assertEqual(work.queue([self.issue], 'alice', ['--mine'])['total'], 1)
        # Closure changed no record: the chain is still contribute + request-review.
        self.assertEqual(self.review_count(), 2)

    def test_the_requester_cannot_resolve_an_item_while_the_contribution_is_withdrawn(self):
        contribution = self.send(self.contribution())['comment_id']
        request = self.send(self.payload('request-changes', contribution=contribution,
                                         items=[dict(id='fix', text='Fix')]), 'reviewer')['comment_id']
        self.send(self.payload('withdraw', contribution=contribution, reason='Re-scoped'))
        with self.assertRaisesRegex(ValueError, 'withdrawn; deliver a new revision'):
            self.send(self.payload('resolve-item', contribution=contribution, request=request,
                                   item='fix', reason='Handled'), 'reviewer')
        self.assertEqual(self.review_count(), 3)
        # The next contribution revives the item; the same requester may then resolve it.
        second = self.send(self.contribution())['comment_id']
        self.send(self.payload('resolve-item', contribution=second, request=request,
                               item='fix', reason='Handled'), 'reviewer')
        self.assertEqual(w.project(self.issue)['pending_requests'], [])


class SwitchRefusalFieldTests(Base):
    """kittrial-5bb.110 item 7: the refusal names the additive FIELD, not just the operation."""

    def test_a_summary_or_severity_refusal_names_the_field(self):
        contribution = self.send(self.contribution())['comment_id']
        with self.assertRaisesRegex(ValueError, r'request-changes summary'):
            self.send(self.payload('request-changes', contribution=contribution, summary='Because',
                                   items=[dict(id='a', text='A')]), 'reviewer', review_writes=False)
        with self.assertRaisesRegex(ValueError, r'item severity'):
            self.send(self.payload('request-changes', contribution=contribution,
                                   items=[dict(id='b', text='B', severity='note')]),
                      'reviewer', review_writes=False)
        # A new operation still names the operation, not a field.
        with self.assertRaisesRegex(ValueError, r'operation withdraw'):
            self.send(self.payload('withdraw', contribution=contribution, reason='x'),
                      review_writes=False)
        # A legacy request-changes item is still written with the switch off.
        self.send(self.payload('request-changes', contribution=contribution,
                               items=[dict(id='c', text='C')]), 'reviewer', review_writes=False)
        self.assertEqual(self.review_count(), 2)


class VariationSelectorTests(Base):
    """kittrial-5bb.110 item 8: U+E0100-U+E01EF selectors are refused like tag characters."""

    def test_variation_selectors_are_refused_in_review_text(self):
        contribution = self.send(self.contribution())['comment_id']
        with self.assertRaisesRegex(ValueError, 'variation-selector'):
            self.send(self.payload('request-changes', contribution=contribution,
                                   items=[dict(id='fix', text='hide \U000E0100 here')]), 'reviewer')
        with self.assertRaisesRegex(ValueError, 'variation-selector'):
            self.send(self.payload('withdraw', contribution=contribution,
                                   reason='bad \U000E01EF'))
        # A plain string with no selector is still accepted.
        self.send(self.payload('request-changes', contribution=contribution,
                               items=[dict(id='ok', text='Plain')]), 'reviewer')
        self.assertEqual(self.review_count(), 2)


class RequestCapBoundaryTests(Base):
    """Mutation M8 (cap 10->11) and M10 (the cap counts declined requests)."""

    def _rows_with_open_requests(self, count, requester='mallory'):
        rows = [chain_row('task-1%02d' % index, author=requester) for index in range(count)]
        target = chain_row('task-1', request=False)
        rows.append(target)
        self.issue = target
        self.rows = rows
        return target

    def test_the_cap_refuses_the_eleventh_open_request(self):
        self._rows_with_open_requests(9)
        self.send(self.payload('request-review', contribution='1', reviewer='alice'),
                  'mallory', operators=('mallory',))
        self.assertEqual(w.open_review_requests_by(self.rows, 'mallory'), 10)
        with self.assertRaisesRegex(ValueError, 'already has 10 open review requests'):
            self.send(self.payload('request-review', contribution='1', reviewer='bob'),
                      'mallory', operators=('mallory',))
        self.assertEqual(w.open_review_requests_by(self.rows, 'mallory'), 10)

    def test_the_cap_is_per_requester_name(self):
        self._rows_with_open_requests(10)
        with self.assertRaisesRegex(ValueError, 'open review requests'):
            self.send(self.payload('request-review', contribution='1', reviewer='alice'),
                      'mallory', operators=('mallory', 'coord'))
        # A different requester name (here a configured coordinator) has its own budget.
        self.send(self.payload('request-review', contribution='1', reviewer='alice'),
                  'coord', operators=('mallory', 'coord'))
        self.assertEqual(w.open_review_requests_by(self.rows, 'coord'), 1)

    def test_a_declined_request_does_not_count_toward_the_cap(self):
        row = chain_row('task-1', author='mallory')       # contribution + request-review (alice)
        decline = dict(schema_version=1, operation='decline-review', operation_id='d-1',
                       task='task-1', previous='2', contribution='1', request='2', reason='Out')
        row['comments'].append(dict(id='3', text=w.PREFIX + json.dumps(decline), author='alice',
                                    created_at='2026-09-16T00:00:00Z'))
        # The request is closed; it is not an OPEN request for anyone, so neither the
        # requester nor the reviewer carries it against their cap.
        self.assertEqual(w.open_review_requests_by([row], 'mallory'), 0)
        self.assertEqual(w.open_review_requests_by([row], 'alice'), 0)


class ReviewWritesCommandTests(unittest.TestCase):
    """Mutation M15 (admin allowlist) and kittrial-5bb.110 item 3 (audit + lock)."""

    def setUp(self):
        from unittest.mock import patch
        self.root = Path(__file__).resolve().parents[1] / '.review-writes-probe'
        self.root.mkdir(exist_ok=True)
        self.marker = self.root / 'deployment.private.json'
        self.marker.write_text(json.dumps({'operators': ['coord']}), encoding='utf-8')
        # `operators(strict=True)` refuses a disagreeing shell ORCHESTRA_OPERATORS.
        self._env = patch.dict('os.environ', {'ORCHESTRA_OPERATORS': ''})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        for name in ('deployment.private.json', admin.REVIEW_WRITES_AUDIT, admin.REVIEW_WRITES_LOCK):
            path = self.root / name
            if path.exists():
                path.unlink()
        if self.root.is_dir():
            self.root.rmdir()

    def test_only_an_allowlisted_operator_may_flip_the_switch(self):
        with self.assertRaisesRegex(ValueError, 'operator allowlist'):
            admin.review_writes_command(self.root, 'mallory', 'on')
        self.assertFalse(admin.review_workflow_writes(self.root))
        self.assertIsNone(admin.review_writes_audit(self.root))

    def test_a_flip_records_who_and_when_and_the_value_it_replaced(self):
        result, warnings = admin.review_writes_command(self.root, 'coord', 'on')
        self.assertTrue(result['review_workflow_writes'])
        self.assertEqual(warnings, [])
        record = admin.review_writes_audit(self.root)
        self.assertEqual((record['set_by'], record['review_workflow_writes'], record['previous']),
                         ('coord', True, False))
        self.assertTrue(record['set_at'])
        result, _ = admin.review_writes_command(self.root, 'coord', 'off')
        self.assertFalse(result['review_workflow_writes'])
        record = admin.review_writes_audit(self.root)
        self.assertEqual((record['set_by'], record['review_workflow_writes'], record['previous']),
                         ('coord', False, True))
        self.assertIsNone(json.loads(self.marker.read_text(encoding='utf-8'))
                          .get('review_workflow_writes'))

    def test_status_reads_a_malformed_switch_as_off_with_a_warning(self):
        self.marker.write_text(json.dumps({'operators': ['coord'], 'review_workflow_writes': 'yes'}),
                               encoding='utf-8')
        result, warnings = admin.review_writes_command(self.root, 'coord', 'status')
        self.assertFalse(result['review_workflow_writes'])
        self.assertTrue(warnings)


@unittest.skipIf(sys.platform == 'win32', 'endpoint.py imports fcntl (POSIX-only)')
class EndpointReviewSwitchTests(unittest.TestCase):
    """Mutation M7: the endpoint supplies the deployment switch, never a constant True."""

    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / '.endpoint-switch-probe'
        project = self.root / 'projects' / 'example'
        (project / '.beads').mkdir(parents=True, exist_ok=True)
        (project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, writes):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'operators': ['coord'], 'review_workflow_writes': writes}), encoding='utf-8')
        import endpoint
        import work
        from unittest.mock import patch
        seen = {}

        def fake_work(path, actor, action, args, attachments, runner, operators=None,
                      verifiers=None, review_writes=None):
            seen['review_writes'] = review_writes
            return {'task': 'example-1', 'review_state': 'awaiting-review'}

        with patch.object(work, 'execute', side_effect=fake_work):
            answer = endpoint.execute(self.root, {'project': 'example', 'actor': 'worker',
                                                  'action': 'review', 'args': ['example-1']})
        return seen, answer

    def test_the_endpoint_reads_the_switch_from_the_deployment(self):
        seen, _ = self._run(False)
        self.assertFalse(seen['review_writes'])
        seen, _ = self._run(True)
        self.assertTrue(seen['review_writes'])

    def test_a_malformed_switch_is_off_and_warns_on_the_endpoint(self):
        seen, answer = self._run('yes')
        self.assertFalse(seen['review_writes'])
        self.assertIn('WARNING', answer['stderr'])
        self.assertIn('review_workflow_writes', answer['stderr'])


class BackupRoundTripTests(Base):
    """kittrial-5bb.110 item 9: a rev-2 chain survives an export/restore round trip.

    A native backup and ``restore-new`` carry issue rows and their comments verbatim,
    so the new-shaped records must read identically after the round trip and must not
    be mistaken for a malformed or unsupported history by any reader (the reserved
    prefix, the projection and the work queue).
    """

    def test_a_new_shape_chain_reads_identically_after_the_round_trip(self):
        contribution = self.send(self.contribution())['comment_id']
        self.send(self.payload('request-changes', contribution=contribution, summary='Why',
                               items=[dict(id='fix', text='Fix', severity='note')]), 'reviewer')
        self.send(self.payload('request-review', contribution=contribution, reviewer='alice'))
        before = w.project(self.issue)
        # Exactly what a native export writes and a restore parses back.
        exported = json.dumps(self.issue, ensure_ascii=False)
        restored = [json.loads(exported)]
        self.assertEqual(exported, json.dumps(restored[0], ensure_ascii=False))
        self.assertEqual(before, w.project(restored[0]))
        self.assertEqual(work.queue(restored, 'alice', ['--mine'])['total'], 1)


if __name__ == '__main__':
    unittest.main()
