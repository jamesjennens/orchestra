"""Follow-on contributions to already-integrated work (kittrial-5bb.25).

An additive second change to a task whose contribution is already integrated uses
the optional ``follows`` relation instead of being forced to declare that it
``supersedes`` (retracts) the integrated revision. These tests pin:

* the follow-on gate: the prior revision must be **approved by a record whose
  native author is neither the prior contribution's author nor the task assignee**
  in the append-only chain (``awaiting-integration``, nothing pending) **and** the
  shared review-state projection must record a passed ``integrated`` fact for the
  prior contribution's full commit, in any scope, with ``base_commit`` equal to
  that scope's ``integration_commit``;
* a self-approval, an assignee approval, a self-recorded ``integrated=passed``
  lifecycle fact by the contributor alone and a changes-requested prior do NOT open
  the gate; all refusals write nothing;
* the assignee is judged as it was when the approval was recorded
  (``assignee_at_approval``, stamped server-side on the approve record), so a later
  handoff neither opens the gate (I5) nor closes it on a genuine reviewer who
  becomes the assignee afterwards (I6); a record with no usable snapshot -- absent,
  or an explicit null written while the task was unassigned -- falls back to the
  current assignee, so an unassign/approve/take-back cycle cannot open the gate;
* both author comparisons normalise case and the ``/``-namespace suffix, so
  ``Worker``/``worker`` and ``worker/sub``/``worker`` cannot pass as distinct
  authors (I11) and ``team/alice``/``team/bob`` fold to one author, while
  ``worker@host``, ``worker-2`` and ``worker.`` stay distinct; an exact retry of an
  approve stays idempotent across an assignee change and a caller-supplied snapshot
  is overwritten;
* a newer lifecycle scope recorded for other work does not make a genuinely
  integrated prior un-followable (the shared projection is per-scope);
* no scoped evidence at all fails closed (there is no ``fact is None`` fallback);
* the accepted additive follow-on on an integrated prior;
* the HTTP review binding forwards the optional ``follows`` relation intact, so a
  follow-on over HTTP never silently becomes a first contribution or a supersede;
* the bounded ``brief`` prior-contribution slice with a complete ``review TASK``;
* what remains visible for a prior contribution after the follow-on records its
  own lifecycle scope (record/commit/relation, not re-scoped lifecycle facts);
* the per-prior ``integration`` block the shared projection and the ``brief``
  slice now carry: an integrated prior reports ``passed`` with its scope's
  integration commit, a prior whose commit matches no scope reports ``unknown``,
  and a newer FAILED scoped fact for the same commit still leaves the older pass
  reported (the unchanged any-pass-wins rule) with ``newest_fact`` exposing the
  failure.
"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import briefing
import lifecycle
import review_workflow as w
from requirements import content_hash

COMMIT_1 = 'a' * 40
COMMIT_2 = 'd' * 40
COMMIT_3 = '9' * 40
MERGE_1 = 'e' * 40
MERGE_2 = 'f' * 40


class FollowOnChainTests(unittest.TestCase):
    def setUp(self):
        self.issue = dict(id='task-1', title='Follow-on task', assignee='worker',
                          status='in_progress', labels=[], comments=[])
        self.rows = [self.issue]
        self.count = 0
        self.actor = 'worker'

    def payload(self, op, **extra):
        self.count += 1
        return dict(schema_version=1, operation=op, operation_id='op-' + str(self.count),
                    task='task-1', previous=w.project(self.issue)['latest_comment_id'], **extra)

    def contribution(self, commit, base='b' * 40, **extra):
        p = dict(repository='ssh://git.example/project', commit=commit, base_commit=base,
                 delivery=dict(kind='bundle', path='koopa:/deliveries/' + commit[:7] + '.bundle',
                               sha256='c' * 64),
                 summary='Implementation and test evidence',
                 supersedes=(w.project(self.issue)['contribution'] or {}).get('comment_id'))
        p.update(extra)
        return self.payload('contribute', **p)

    def run_native(self, args):
        cid = str(len(self.issue['comments']) + 1)
        self.issue['comments'].append(dict(id=cid, text=args[3], author=self.actor,
                                           created_at='2026-09-16T00:00:00Z'))
        return json.dumps({'id': cid})

    def send(self, p, actor='worker'):
        # The native comment author is the acting actor, as a real endpoint records
        # attribution; the follow-on gate reads that author, not the actor label.
        self.actor = actor
        return w.execute(self.rows, 'task-1', actor, p, self.run_native)

    def record_lifecycle(self, source_commit, integration_commit, scope_op='scope-1', integrated=True,
                         actor='worker', value='passed'):
        """Append scoped lifecycle evidence marking source_commit integrated.

        ``actor`` is the recording actor. The lifecycle action only requires
        ``payload.actor == request actor``, so the contributor can record it alone.
        ``value`` is the scoped ``integrated`` value (``passed`` or ``failed``).
        """
        def run(args):
            if args == ['export', '--all']:
                return ''.join(json.dumps(r) + '\n' for r in self.rows)
            assert args[0] == 'set-state', args
            dim, value = args[2].split('=', 1)
            issue = self.issue
            old = next((x.split(':', 1)[1] for x in issue['labels'] if x.startswith(dim + ':')), None)
            issue['labels'] = [x for x in issue['labels'] if not x.startswith(dim + ':')] + [dim + ':' + value]
            event_id = 'task-1.' + str(len(self.rows))
            reason = args[args.index('--reason') + 1]
            description = (('Set ' if old is None else 'Changed ') + dim +
                           (' to ' if old is None else ' from ' + old + ' to ') + value +
                           '\n\nReason: ' + reason)
            self.rows.append(dict(_type='issue', id=event_id, issue_type='event',
                                  title='State change: ' + dim + ' \u2192 ' + value,
                                  description=description, status='closed', created_by=actor,
                                  created_at='2026-09-16T00:00:00Z',
                                  dependencies=[dict(issue_id=event_id, depends_on_id='task-1',
                                                     type='parent-child')]))
            return json.dumps(dict(changed=True, dimension=dim, event_id=event_id, new_value=value))

        scope = {'source_commit': source_commit, 'integration_commit': integration_commit,
                 'release_id': '', 'environment': ''}
        base = dict(schema_version=1, task='task-1', scope=scope, evidence=['commit:' + source_commit],
                    provenance='performed', actor=actor)
        lifecycle.apply_native(dict(base, operation_id=scope_op, dimension='lifecycle-scope',
                                    value=content_hash(scope)), actor, run)
        if integrated:
            lifecycle.apply_native(dict(base, operation_id=scope_op + '-int', dimension='integrated',
                                        value=value), actor, run)

    def raw_approve(self, contribution_id, author, operation_id='op-legacy-approve'):
        """Append an approve record by hand, as the transport wrote it before the
        ``assignee_at_approval`` snapshot existed (no snapshot field at all)."""
        payload = dict(schema_version=1, operation='approve', operation_id=operation_id,
                       task='task-1', previous=contribution_id, contribution=contribution_id,
                       summary='Reviewed')
        cid = str(len(self.issue['comments']) + 1)
        self.issue['comments'].append(dict(id=cid, text=w.PREFIX + json.dumps(payload),
                                           author=author, created_at='2026-09-16T00:00:00Z'))
        return cid

    def stored_payload(self):
        """The payload the last native comment stored."""
        return json.loads(self.issue['comments'][-1]['text'][len(w.PREFIX):])

    def shared(self):
        """`review TASK`'s projection: the shared review-state overlay with scopes."""
        from review_state import project as reviewed, scopes_for
        return reviewed(self.issue, scopes_for(self.rows, 'task-1'))


    def integration_case(self):
        """Contribution 1 approved and integrated at MERGE_1, base scope on COMMIT_1."""
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.send(self.payload('approve', contribution=first, summary='Reviewed'), 'reviewer')
        self.record_lifecycle(COMMIT_1, MERGE_1)
        return first

    def test_additive_follow_on_keeps_prior_contribution_visible(self):
        first = self.integration_case()
        second = self.send(self.contribution(COMMIT_2, base=MERGE_1,
                                             supersedes=None, follows=first))['comment_id']
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertEqual(state['contribution']['comment_id'], second)
        self.assertEqual(state['contribution']['follows'], first)
        self.assertEqual(state['contribution']['base_commit'], MERGE_1)
        self.assertEqual([c['comment_id'] for c in state['prior_contributions']], [first])
        prior = state['prior_contributions'][0]
        self.assertEqual(prior['relation'], 'follows')
        self.assertEqual(prior['commit'], COMMIT_1)
        self.assertIsNone(prior['supersedes'])

    def test_awaiting_review_prior_is_refused_without_write(self):
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'not approved'):
            self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before)

    def test_self_recorded_integration_does_not_open_the_follow_on_gate(self):
        """The contributor alone records scope + integrated=passed; the gate stays shut.

        The lifecycle action only requires ``payload.actor == request actor``, so the
        task owner can record a passed ``integrated`` fact by itself. Approval is a
        separate reviewer operation, so the unstructured self-assertion must not be
        enough to declare the prior revision integrated.
        """
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.record_lifecycle(COMMIT_1, MERGE_1, actor='worker')
        facts = next(r for r in lifecycle.project_facts(self.rows) if r['id'] == 'task-1')
        self.assertEqual(facts['facts']['integrated']['value'], 'passed')
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-review')
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'not approved'):
            self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before)

    def test_self_approval_does_not_open_the_follow_on_gate(self):
        """Item 1: the contributor approves its own revision and fabricates the
        scoped integration fact; the gate stays shut and writes nothing.

        Native comment authors are attribution, not authentication, on the SSH
        path, so the gate refuses an approval whose native author is the prior
        contribution's own author instead of pretending to verify an identity.
        """
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.send(self.payload('approve', contribution=first, summary='Self approved'), 'worker')
        self.record_lifecycle(COMMIT_1, MERGE_1, actor='worker')
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'approved by its own author'):
            self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before)

    def test_assignee_approval_does_not_open_the_follow_on_gate(self):
        """Item 1: a handoff leaves the new assignee approving the prior revision."""
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.issue['assignee'] = 'owner'   # the task is handed off after contributing
        self.send(self.payload('approve', contribution=first, summary='Approved as owner'), 'owner')
        self.record_lifecycle(COMMIT_1, MERGE_1, actor='owner')
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'approved by the task assignee'):
            self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first),
                      'owner')
        self.assertEqual(len(self.issue['comments']), before)

    def test_assignee_at_approval_time_refuses_the_later_follow_on(self):
        """I5: X approves while X is the assignee and the task then returns to W.

        The approval was an assignee self-approval when it was recorded, so W's
        follow-on must still be refused after the handoff: judging the CURRENT
        assignee (W) would wrongly open the gate.
        """
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.issue['assignee'] = 'x'            # X takes the task over
        self.send(self.payload('approve', contribution=first, summary='Approved while assignee x'), 'x')
        self.record_lifecycle(COMMIT_1, MERGE_1)
        self.issue['assignee'] = 'worker'       # handed back to the contributor
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'approved by the task assignee'):
            self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before)

    def test_reviewer_who_later_becomes_assignee_can_follow_on(self):
        """I6: the reviewer was not the assignee when approving, so their follow-on
        on the genuinely integrated prior must be accepted after they take the task."""
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.send(self.payload('approve', contribution=first, summary='Reviewed before the handoff'),
                  'reviewer')
        self.record_lifecycle(COMMIT_1, MERGE_1)
        self.issue['assignee'] = 'reviewer'     # the reviewer is handed the task later
        second = self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first),
                           'reviewer')['comment_id']
        state = w.project(self.issue)
        self.assertEqual(state['contribution']['comment_id'], second)
        self.assertEqual(state['contribution']['follows'], first)
        self.assertEqual(state['review_state'], 'awaiting-review')

    def test_case_variant_label_does_not_open_the_self_approval_gate(self):
        """I11: ``Worker`` approving its own ``worker`` contribution is a self-approval."""
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.send(self.payload('approve', contribution=first, summary='Self approved as Worker'),
                  'Worker')
        self.record_lifecycle(COMMIT_1, MERGE_1)
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'approved by its own author'):
            self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before)

    def test_namespace_variant_label_does_not_open_the_self_approval_gate(self):
        """I11: ``worker/sub`` approving its own ``worker`` contribution is a self-approval."""
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.send(self.payload('approve', contribution=first, summary='Self approved as worker/sub'),
                  'worker/sub')
        self.record_lifecycle(COMMIT_1, MERGE_1)
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'approved by its own author'):
            self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before)

    def test_case_variant_assignee_label_is_normalised(self):
        """I11: an approval by ``worker`` while the assignee label is ``Worker`` is an
        assignee approval (a distinct contribution author makes the message unambiguous)."""
        self.issue['assignee'] = 'author-a'
        first = self.send(self.contribution(COMMIT_1), 'author-a')['comment_id']
        self.issue['assignee'] = 'Worker'       # handoff to a case variant of worker
        self.send(self.payload('approve', contribution=first, summary='Approved by Worker'), 'worker')
        self.record_lifecycle(COMMIT_1, MERGE_1, actor='worker')
        self.issue['assignee'] = 'worker'
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'approved by the task assignee'):
            self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before)

    def test_namespace_variant_assignee_label_is_normalised(self):
        """I11: an approval by ``worker/sub`` while the assignee is ``worker`` is an
        assignee approval."""
        self.issue['assignee'] = 'author-a'
        first = self.send(self.contribution(COMMIT_1), 'author-a')['comment_id']
        self.issue['assignee'] = 'worker'
        self.send(self.payload('approve', contribution=first, summary='Approved by worker/sub'),
                  'worker/sub')
        self.record_lifecycle(COMMIT_1, MERGE_1, actor='worker')
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'approved by the task assignee'):
            self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before)

    def test_legacy_approve_without_snapshot_falls_back_to_current_assignee(self):
        """An approve record written before the snapshot existed must keep failing
        closed against the current assignee."""
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.issue['assignee'] = 'owner'
        self.raw_approve(first, 'owner')        # legacy record: no assignee snapshot
        self.assertNotIn(w.ASSIGNEE_SNAPSHOT, self.stored_payload())
        self.record_lifecycle(COMMIT_1, MERGE_1, actor='owner')
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'approved by the task assignee'):
            self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first),
                      'owner')
        self.assertEqual(len(self.issue['comments']), before)

    def test_approve_stamps_the_assignee_server_side(self):
        """The snapshot is the server's current assignee, not the caller's value, and
        an exact retry stays idempotent after the assignee changes."""
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        forged = self.payload('approve', contribution=first, summary='Reviewed')
        forged[w.ASSIGNEE_SNAPSHOT] = 'forged-actor'
        receipt = self.send(forged, 'reviewer')
        self.assertEqual('worker', self.stored_payload()[w.ASSIGNEE_SNAPSHOT])
        self.issue['assignee'] = 'replacement'
        retried = self.send(forged, 'reviewer')
        self.assertTrue(retried['reconciled'])
        self.assertEqual(retried['comment_id'], receipt['comment_id'])
        self.assertEqual(len(self.issue['comments']), 2)   # contribution + one approval

    def test_explicit_null_snapshot_falls_back_to_the_current_assignee(self):
        """N3: an approval written while the task was unassigned carries an explicit
        null snapshot, and the gate must treat it like a missing snapshot and fall
        back to the current assignee, so an unassign/approve/take-back cycle cannot
        open the gate."""
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.issue['assignee'] = None
        self.send(self.payload('approve', contribution=first, summary='Approved while unassigned'),
                  'reviewer')
        self.assertIsNone(self.stored_payload()[w.ASSIGNEE_SNAPSHOT])
        self.record_lifecycle(COMMIT_1, MERGE_1, actor='worker')
        self.issue['assignee'] = 'reviewer'     # the approver takes the task back
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'approved by the task assignee'):
            self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first),
                      'reviewer')
        self.assertEqual(len(self.issue['comments']), before)

    def test_namespace_prefix_shares_one_author_key(self):
        """N6: ``team/alice`` and ``team/bob`` fold to one author key, so an approval
        of ``team/alice``'s revision by ``team/bob`` is a self-approval. The extra
        refusal is deliberate and the fold applies on HTTP too."""
        self.issue['assignee'] = 'team/alice'
        first = self.send(self.contribution(COMMIT_1), 'team/alice')['comment_id']
        self.send(self.payload('approve', contribution=first, summary='Approved by team/bob'),
                  'team/bob')
        self.record_lifecycle(COMMIT_1, MERGE_1, actor='team/bob')
        self.issue['assignee'] = 'team/alice'
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'approved by its own author'):
            self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first),
                      'team/alice')
        self.assertEqual(len(self.issue['comments']), before)

    def test_author_key_normalises_only_case_and_namespace(self):
        """Pin the documented normalisation: case and a ``/``-namespace fold, while
        ``@``, a numeric suffix and a dot remain distinct attributions."""
        self.assertEqual(w.author_key('Worker'), w.author_key('worker'))
        self.assertEqual(w.author_key('worker/sub/deeper'), w.author_key('worker'))
        self.assertEqual(w.author_key('team/alice'), w.author_key('team/bob'))
        for distinct in ('worker@host', 'worker-2', 'worker.'):
            self.assertNotEqual(w.author_key(distinct), w.author_key('worker'))

    def test_changes_requested_prior_is_refused_even_when_self_integrated(self):
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.send(self.payload('request-changes', contribution=first,
                               items=[dict(id='fix-1', text='Correct the thing')]), 'reviewer')
        self.record_lifecycle(COMMIT_1, MERGE_1, actor='worker')
        self.assertEqual(w.project(self.issue)['review_state'], 'changes-requested')
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'not approved'):
            self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before)

    def test_approved_but_unintegrated_prior_is_refused_without_write(self):
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.send(self.payload('approve', contribution=first, summary='Reviewed'), 'reviewer')
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'not integrated'):
            self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before)

    def test_integrated_prior_with_mismatched_base_is_refused_without_write(self):
        first = self.integration_case()
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'must equal the prior integration commit'):
            self.send(self.contribution(COMMIT_2, base=COMMIT_3, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before)
        # The exact integration commit is accepted.
        self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before + 1)

    def test_newer_scope_for_other_work_does_not_block_a_valid_follow_on(self):
        """Item 2: the gate reads per-scope evidence, not the single current scope."""
        first = self.integration_case()
        # A newer scope for other work becomes the task's current lifecycle scope.
        self.record_lifecycle(COMMIT_3, MERGE_2, scope_op='scope-2', actor='integrator')
        current = next(r for r in lifecycle.project_facts(self.rows) if r['id'] == 'task-1')
        self.assertEqual(current['scope']['source_commit'], COMMIT_3)
        second = self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None,
                                             follows=first))['comment_id']
        state = w.project(self.issue)
        self.assertEqual(state['contribution']['comment_id'], second)
        self.assertEqual(state['contribution']['follows'], first)

    def test_shared_projection_without_scoped_evidence_fails_closed(self):
        """No ``fact is None`` fallback: an empty shared projection refuses the follow-on."""
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.send(self.payload('approve', contribution=first, summary='Reviewed'), 'reviewer')
        ordered = w.records(self.issue)
        state = w.projection(ordered)
        self.assertEqual(state['review_state'], 'awaiting-integration')
        payload = dict(operation='contribute', follows=first, base_commit=MERGE_1)
        # The task row exists but records no trusted scope, so the shared projection
        # reports fact='unknown' and the gate fails closed.
        with self.assertRaisesRegex(ValueError, 'not integrated'):
            w.require_integrated_follow_on(payload, state, ordered, self.rows, 'task-1', 'worker')
        for altered in (dict(state, review_state='awaiting-review'),
                        dict(state, review_state='changes-requested'),
                        dict(state, review_state='awaiting-integration',
                             pending_requests=[{'item': 'fix'}])):
            with self.assertRaisesRegex(ValueError, 'not approved'):
                w.require_integrated_follow_on(payload, altered, ordered, self.rows,
                                               'task-1', 'worker')

    def test_http_binding_forwards_follows_and_keeps_legacy_bodies_exact(self):
        """Item 2: the HTTP review binding must not silently drop ``follows``.

        kittrial-5bb.19 forwarded only the required canonical field list, so an
        HTTP follow-on either failed closed or became a first contribution or a
        supersede. The forwarded body must carry the optional relation, and a
        legacy payload without it must keep the exact legacy field set.
        """
        import http_service
        backend = http_service.EndpointBackend.__new__(http_service.EndpointBackend)
        first = '1'
        base = {'task_id': 'task-1', 'operation': 'contribute', 'schema_version': 1,
                'previous': first, 'operation_id': 'op-http', 'repository': 'ssh://git.example/p',
                'commit': COMMIT_2, 'base_commit': MERGE_1, 'supersedes': None,
                'delivery': dict(kind='bundle', path='koopa:/y.bundle', sha256='c' * 64),
                'summary': 'follow-on over http'}
        _, _, args, attachments = backend._command('reviews.add', None, 'proj',
                                                   dict(base, follows=first), 'f' * 64)
        self.assertEqual(['task-1', '@attachment:0'], args)
        body = json.loads(attachments['0']['text'])
        self.assertEqual(first, body['follows'])
        w.validate(body, 'task-1')
        # A legacy payload without the optional field keeps the exact legacy set.
        _, _, _, plain_attachments = backend._command('reviews.add', None, 'proj',
                                                      dict(base), 'f' * 64)
        plain = json.loads(plain_attachments['0']['text'])
        self.assertNotIn('follows', plain)
        w.validate(plain, 'task-1')

    def test_http_forwarded_body_stays_a_follow_on(self):
        """The forwarded HTTP body records a ``follows`` relation, not a first/supersede."""
        import http_service
        first = self.integration_case()
        payload = {'task_id': 'task-1', 'operation': 'contribute', 'schema_version': 1,
                   'previous': w.project(self.issue)['latest_comment_id'],
                   'operation_id': 'op-http-follow', 'repository': 'ssh://git.example/p',
                   'commit': COMMIT_2, 'base_commit': MERGE_1, 'supersedes': None,
                   'follows': first,
                   'delivery': dict(kind='bundle', path='koopa:/y.bundle', sha256='c' * 64),
                   'summary': 'follow-on over http'}
        backend = http_service.EndpointBackend.__new__(http_service.EndpointBackend)
        _, _, _, attachments = backend._command('reviews.add', None, 'proj', payload, 'f' * 64)
        body = json.loads(attachments['0']['text'])
        self.assertEqual(first, body['follows'])
        second = self.send(body)['comment_id']
        state = w.project(self.issue)
        self.assertEqual(state['contribution']['comment_id'], second)
        self.assertEqual(state['contribution']['follows'], first)
        self.assertIsNone(state['contribution']['supersedes'])
        self.assertEqual([c['relation'] for c in state['prior_contributions']], ['follows'])

    def test_legacy_supersede_chain_still_tags_the_replaced_revision(self):
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        second = self.send(self.contribution(COMMIT_2))['comment_id']
        state = w.project(self.issue)
        self.assertEqual(state['contribution']['comment_id'], second)
        self.assertEqual(state['contribution']['supersedes'], first)
        self.assertEqual([c['comment_id'] for c in state['prior_contributions']], [first])
        self.assertEqual(state['prior_contributions'][0]['relation'], 'supersedes')

    def test_wrong_follow_target_and_both_relations_are_refused_without_write(self):
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'follow the current revision'):
            self.send(self.contribution(COMMIT_2, supersedes=None, follows='other-id'))
        with self.assertRaisesRegex(ValueError, 'not both'):
            self.send(self.contribution(COMMIT_2, follows=first))
        self.assertEqual(len(self.issue['comments']), before)

    def test_first_contribution_cannot_follow(self):
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'follow the current revision'):
            self.send(self.contribution(COMMIT_1, supersedes=None, follows='anything'))
        self.assertEqual(len(self.issue['comments']), before)

    def test_brief_bounds_long_prior_chain_and_review_stays_complete(self):
        summary = 'S' * 500
        for i in range(30):
            p = self.contribution('%040x' % (i + 1))
            p['summary'] = summary
            self.send(p)
        complete = w.project(self.issue)
        self.assertEqual(len(complete['prior_contributions']), 29)
        self.assertTrue(all('summary' in c for c in complete['prior_contributions']))

        result = briefing.brief(self.rows, 'proj', 'task-1')
        review = result['review']
        self.assertEqual(review['prior_contributions_total'], 29)
        self.assertEqual(len(review['prior_contributions']), briefing.PRIOR_BRIEF_LIMIT)
        self.assertEqual(review['prior_contributions_more'], 'review task-1')
        for entry in review['prior_contributions']:
            self.assertEqual(set(entry),
                             {'comment_id', 'commit', 'relation', 'timestamp', 'integration'})
            self.assertIn(entry['integration']['fact'],
                          ('unknown', 'passed', 'failed', 'reverted'))
        # The recent slice preserves order and matches the complete projection.
        self.assertEqual([c['comment_id'] for c in review['prior_contributions']],
                         [c['comment_id'] for c in complete['prior_contributions'][-briefing.PRIOR_BRIEF_LIMIT:]])
        # The embedded brief stays small instead of scaling with the chain (base is ~2.7 KB).
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), 6000)
        self.assertLess(len(briefing.format_brief(result).encode()), 6000)

    def test_prior_scoped_fact_leaves_scope_but_stays_in_history(self):
        first = self.integration_case()
        self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first))

        before = briefing.brief(self.rows, 'proj', 'task-1')
        self.assertEqual(before['lifecycle']['integrated']['value'], 'passed')
        self.assertEqual(before['lifecycle_scope']['source_commit']['text'], COMMIT_1)
        self.assertEqual([c['comment_id'] for c in before['review']['prior_contributions']], [first])

        # The follow-on records its own scope: exactly the reviewer's scenario.
        self.record_lifecycle(COMMIT_2, MERGE_2, scope_op='scope-2')
        after = briefing.brief(self.rows, 'proj', 'task-1')
        self.assertEqual(after['lifecycle_scope']['source_commit']['text'], COMMIT_2)
        self.assertEqual(after['lifecycle']['integrated']['value'], 'passed')
        prior = after['review']['prior_contributions'][0]
        self.assertEqual((prior['comment_id'], prior['commit'], prior['relation']),
                         (first, COMMIT_1, 'follows'))
        self.assertFalse({'lifecycle', 'integrated', 'review_state', 'facts', 'scope'} & set(prior))

        # The prior's scoped integration evidence is intact in lifecycle history
        # even though it is no longer the task's current scope.
        self.assertTrue(any(r.get('issue_type') == 'event' and COMMIT_1 in r.get('description', '')
                            and 'integrated' in r.get('description', '') for r in self.rows))
        current = next(r for r in lifecycle.project_facts(self.rows) if r['id'] == 'task-1')
        self.assertEqual(current['scope']['source_commit'], COMMIT_2)
        self.assertEqual(current['facts']['integrated']['value'], 'passed')

        from work import queue
        item = queue(self.rows, 'worker', ['--mine'])['items'][0]
        self.assertEqual(item['lifecycle_scope']['source_commit'], COMMIT_2)
        self.assertEqual(item['lifecycle']['integrated'], 'passed')
        self.assertNotIn('prior_contributions', item)

    def test_prior_contribution_reports_its_integration_answer(self):
        """The follow-on read says whether the replaced revision is integrated.

        The independent review of kittrial-5bb.24 rev2 found that after a follow-on
        the read correctly showed awaiting-review for the new commit, but the
        ``prior_contributions`` entry carried no integration block, so nothing said
        the earlier change was integrated. The block is computed from the prior's
        own FULL commit over the same scopes, and existing entry keys are unchanged.
        """
        first = self.integration_case()
        second = self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None,
                                             follows=first))['comment_id']
        state = self.shared()
        self.assertEqual(state['review_state'], 'awaiting-review')
        self.assertEqual(state['contribution']['comment_id'], second)
        prior = state['prior_contributions'][0]
        # The pre-existing keys keep their values and no key is renamed ...
        self.assertEqual((prior['comment_id'], prior['commit'], prior['relation']),
                         (first, COMMIT_1, 'follows'))
        # ... and the additive block answers the prior's integration question.
        self.assertEqual(prior['integration']['fact'], 'passed')
        self.assertEqual(prior['integration']['source_commit'], COMMIT_1)
        self.assertEqual(prior['integration']['integration_commit'], MERGE_1)
        self.assertTrue(prior['integration']['matches_contribution'])
        self.assertEqual(prior['integration']['newest_fact'], 'passed')
        self.assertFalse({'lifecycle', 'integrated', 'review_state', 'facts', 'scope'} & set(prior))
        # The CURRENT contribution has no scope of its own yet, so its own block
        # and the prior's block are genuinely separate answers.
        self.assertFalse(state['integration']['matches_contribution'])
        self.assertEqual(state['integration']['fact'], 'unknown')
        # The compact brief slice carries the same block for the prior entry.
        review = briefing.brief(self.rows, 'proj', 'task-1')['review']
        slice_prior = review['prior_contributions'][0]
        self.assertEqual(slice_prior['comment_id'], first)
        self.assertEqual(slice_prior['integration']['fact'], 'passed')
        self.assertEqual(slice_prior['integration']['integration_commit'], MERGE_1)

    def test_prior_without_a_matching_scope_reports_unknown(self):
        """A scope for other work does not mark an unrelated prior integrated."""
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.record_lifecycle(COMMIT_3, MERGE_2)
        self.send(self.contribution(COMMIT_2))
        prior = self.shared()['prior_contributions'][0]
        self.assertEqual((prior['comment_id'], prior['relation']), (first, 'supersedes'))
        self.assertEqual(prior['integration']['fact'], 'unknown')
        self.assertEqual(prior['integration']['newest_fact'], 'unknown')
        self.assertFalse(prior['integration']['matches_contribution'])
        self.assertIsNone(prior['integration']['scope'])
        self.assertIsNone(prior['integration']['source_commit'])
        self.assertFalse({'lifecycle', 'integrated', 'review_state', 'facts', 'scope'} & set(prior))

    def test_prior_any_pass_wins_is_visible_with_the_newer_failure(self):
        """A newer FAILED scoped fact still reports the older pass for a prior.

        Same rule as for the current contribution: the added facts make the
        existing any-pass-wins answer visible for a prior entry instead of
        introducing a new rule. ``newest_fact`` exposes the newer failure, and the
        follow-on gate still accepts the older passing scope's integration commit
        as the base (a newest-wins reading would have demanded MERGE_2).
        """
        first = self.integration_case()
        self.record_lifecycle(COMMIT_1, MERGE_2, scope_op='scope-fail', actor='integrator',
                              value='failed')
        second = self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None,
                                             follows=first))['comment_id']
        state = self.shared()
        self.assertEqual(state['contribution']['comment_id'], second)
        prior = state['prior_contributions'][0]
        self.assertEqual(prior['commit'], COMMIT_1)
        self.assertEqual(prior['integration']['fact'], 'passed')
        self.assertEqual(prior['integration']['scope']['integration_commit'], MERGE_1)
        self.assertEqual(prior['integration']['integration_commit'], MERGE_1)
        self.assertEqual(prior['integration']['newest_fact'], 'failed')
        self.assertNotEqual(prior['integration']['newest_scope_token'],
                            prior['integration']['scope_token'])
        # A newest-wins reading is not what the projection now does or did before.
        self.assertTrue(prior['integration']['matches_contribution'])


# ===========================================================================
# kittrial-5bb.52: the integration disagreement warning and the explicit
# audited revert record. The owner decision on kittrial-5bb.32 (comment
# 01a0eea4) keeps any-pass-wins, so a conflict between `newest_fact` and `fact`
# is warned about instead of silently changing the answer, and removing an
# integrated commit is a separate operator-audited record.
# ===========================================================================

from review_state import disagreement_warnings, integration_disagreements  # noqa: E402
from requirements import canonical_bytes  # noqa: E402
from review_workflow import REVERT_PREFIX  # noqa: E402

OPERATOR = 'coordinator-1'


def revert_payload(operation_id='revert-1', contribution='1', integration_commit=MERGE_1,
                   revert_commit='b' * 40, reason='Re-merge dropped the change',
                   evidence=None, task='task-1'):
    payload = dict(schema_version=1, operation='revert-record', operation_id=operation_id,
                   task=task, contribution=contribution,
                   integration_commit=integration_commit, revert_commit=revert_commit,
                   reason=reason)
    if evidence is not None:
        payload['evidence'] = evidence
    return payload


class IntegrationRevertTests(FollowOnChainTests):
    """The (a) warning and (b) revert record for the shared integration projection."""

    def tearDown(self):
        os.environ.pop('ORCHESTRA_OPERATORS', None)

    # -- fixtures ----------------------------------------------------------
    def append_revert(self, payload, author=OPERATOR):
        """Append a revert comment exactly as the operator CLI would."""
        cid = str(len(self.issue['comments']) + 1)
        body = REVERT_PREFIX + canonical_bytes(payload).decode()
        self.issue['comments'].append(dict(id=cid, text=body, author=author,
                                           created_at='2026-09-16T00:00:00Z'))
        return cid

    def operator_revert(self, payload=None, actor=OPERATOR):
        """Run the operator write path with the deployment allowlist configured."""
        os.environ['ORCHESTRA_OPERATORS'] = OPERATOR
        payload = payload if payload is not None else revert_payload(task=self.issue['id'])
        if payload.get('task') != self.issue['id']:
            payload = dict(payload, task=self.issue['id'])

        def run(args):
            cid = str(len(self.issue['comments']) + 1)
            self.issue['comments'].append(dict(id=cid, text=args[3], author=actor,
                                               created_at='2026-09-16T00:00:00Z'))
            return json.dumps({'id': cid})

        return w.apply_revert(self.rows, self.issue['id'], actor, payload, run,
                              operator=True, operators=[OPERATOR])

    def reviewed(self, operators=None):
        """The shared projection with the operator allowlist the host supplies."""
        from review_state import project as reviewed
        return reviewed(self.issue, self.scopes(),
                        [OPERATOR] if operators is None else operators)

    def scopes(self):
        from review_state import scopes_for
        return scopes_for(self.rows, self.issue['id'])

    def warning_lines(self, state):
        return [w for w in state['warnings'] if w.startswith('Integration fact disagreement')]

    # -- (a) the disagreement warning --------------------------------------
    def test_current_contribution_warns_when_the_newest_scope_disagrees(self):
        self.integration_case()
        # A newer matching scope records a failure; any-pass-wins keeps the pass.
        self.record_lifecycle(COMMIT_1, MERGE_2, scope_op='scope-2', value='failed')
        state = self.reviewed()
        block = state['integration']
        self.assertEqual((block['fact'], block['newest_fact']), ('passed', 'failed'))
        self.assertEqual(block['scope']['integration_commit'], MERGE_1)
        self.assertEqual(block['newest_scope']['integration_commit'], MERGE_2)
        # The structured signal names both facts and both scopes.
        self.assertEqual(len(state['integration_disagreements']), 1)
        entry = state['integration_disagreements'][0]
        self.assertEqual(entry['kind'], 'newest-scope-disagrees')
        self.assertEqual((entry['fact'], entry['newest_fact']), ('passed', 'failed'))
        self.assertEqual(entry['scope']['integration_commit'], MERGE_1)
        self.assertEqual(entry['newest_scope']['integration_commit'], MERGE_2)
        self.assertEqual(entry['contribution'], self.issue['comments'][0]['id'])
        # The human-readable warning travels on the read and renders in the brief.
        lines = self.warning_lines(state)
        self.assertEqual(len(lines), 1)
        self.assertIn(MERGE_1, lines[0])
        self.assertIn(MERGE_2, lines[0])
        result = briefing.brief(self.rows, 'proj', 'task-1', operators=[OPERATOR])
        self.assertTrue(any(w.startswith('Integration fact disagreement')
                            for w in result['warnings']))
        rendered = briefing.format_brief(result)
        self.assertIn('Integration fact disagreement', rendered)
        self.assertIn(MERGE_1, rendered)
        self.assertIn(MERGE_2, rendered)

    def test_no_warning_when_the_newest_scope_agrees(self):
        self.integration_case()
        state = self.reviewed()
        self.assertEqual(state['integration']['fact'], 'passed')
        self.assertEqual(state['integration']['newest_fact'], 'passed')
        self.assertEqual(state['integration_disagreements'], [])
        self.assertEqual(self.warning_lines(state), [])

    def test_prior_contribution_disagreement_is_warned_about(self):
        first = self.integration_case()
        self.record_lifecycle(COMMIT_1, MERGE_2, scope_op='scope-2', value='failed')
        self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first))
        state = self.reviewed()
        prior = state['prior_contributions'][0]
        self.assertEqual((prior['integration']['fact'], prior['integration']['newest_fact']),
                         ('passed', 'failed'))
        entries = [d for d in state['integration_disagreements'] if d['contribution'] == first]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['relation'], 'follows')
        self.assertEqual(entries[0]['scope']['integration_commit'], MERGE_1)
        self.assertEqual(entries[0]['newest_scope']['integration_commit'], MERGE_2)
        self.assertTrue(any(first in line for line in self.warning_lines(state)))
        result = briefing.brief(self.rows, 'proj', 'task-1', operators=[OPERATOR])
        self.assertTrue(any(first in w for w in result['warnings']
                            if w.startswith('Integration fact disagreement')))

    def test_work_row_and_page_carry_the_disagreement(self):
        from work import queue
        self.integration_case()
        self.record_lifecycle(COMMIT_1, MERGE_2, scope_op='scope-2', value='failed')
        page = queue(self.rows, 'worker', ['--mine'], operators=[OPERATOR])
        item = page['items'][0]
        self.assertEqual(item['integration']['fact'], 'passed')
        self.assertEqual(item['integration']['newest_fact'], 'failed')
        self.assertEqual(len(item['integration_disagreements']), 1)
        self.assertTrue(item['integration_warnings'][0].startswith('Integration fact disagreement'))
        self.assertTrue(page['warnings'])
        self.assertIn(MERGE_1, page['warnings'][0])
        self.assertIn(MERGE_2, page['warnings'][0])

    def test_work_row_has_no_warning_when_the_facts_agree(self):
        from work import queue
        self.integration_case()
        item = queue(self.rows, 'worker', ['--mine'], operators=[OPERATOR])['items'][0]
        self.assertEqual(item['integration_disagreements'], [])
        self.assertEqual(item['integration_warnings'], [])

    def test_disagreement_helpers_are_pure(self):
        self.integration_case()
        self.record_lifecycle(COMMIT_1, MERGE_2, scope_op='scope-2', value='failed')
        state = self.reviewed()
        self.assertEqual(integration_disagreements(state), state['integration_disagreements'])
        self.assertEqual(disagreement_warnings(state['integration_disagreements']),
                         self.warning_lines(state))
        # A raw workflow projection (no integration block) yields nothing.
        self.assertEqual(integration_disagreements(w.project(self.issue)), [])

    # -- (b) the revert record ---------------------------------------------
    def test_transport_refuses_a_revert_payload(self):
        self.integration_case()
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'admin.py revert-record'):
            self.send(revert_payload())
        self.assertEqual(len(self.issue['comments']), before)

    def test_apply_revert_refuses_without_the_operator_flag(self):
        self.integration_case()
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'not authorized over the contributor review transport'):
            w.apply_revert(self.rows, self.issue['id'], OPERATOR,
                           revert_payload(task=self.issue['id']), self.run_native,
                           operators=[OPERATOR])
        self.assertEqual(len(self.issue['comments']), before)

    def test_apply_revert_refuses_an_unconfigured_allowlist(self):
        self.integration_case()
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'No operator allowlist'):
            w.apply_revert(self.rows, self.issue['id'], OPERATOR,
                           revert_payload(task=self.issue['id']), self.run_native,
                           operator=True, operators=[])
        self.assertEqual(len(self.issue['comments']), before)

    def test_apply_revert_refuses_an_actor_outside_the_allowlist(self):
        self.integration_case()
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            w.apply_revert(self.rows, self.issue['id'], 'worker',
                           revert_payload(task=self.issue['id']), self.run_native,
                           operator=True, operators=[OPERATOR])
        self.assertEqual(len(self.issue['comments']), before)

    def test_record_whose_author_is_not_an_operator_is_ignored(self):
        self.integration_case()
        self.append_revert(revert_payload(task=self.issue['id']), author='worker')
        reverts, invalid = w.revert_records(self.issue, [OPERATOR])
        self.assertEqual(reverts, [])
        self.assertEqual(len(invalid), 1)
        state = self.reviewed()
        self.assertEqual(state['integration']['fact'], 'passed')
        self.assertFalse(state['integration']['reverted'])

    def test_revert_removes_the_named_integration_commit(self):
        first = self.integration_case()
        receipt = self.operator_revert()
        self.assertFalse(receipt['reconciled'])
        self.assertEqual(receipt['contribution'], first)
        self.assertEqual(receipt['integration_commit'], MERGE_1)
        block = self.reviewed()['integration']
        self.assertEqual(block['fact'], 'reverted')
        self.assertTrue(block['reverted'])
        self.assertTrue(block['matches_contribution'])
        self.assertEqual(block['scope']['integration_commit'], MERGE_1)
        # The raw chain still records the approval; only the integration answer moved.
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')

    def test_revert_is_idempotent_and_refuses_a_duplicate(self):
        self.integration_case()
        first = self.operator_revert()
        retried = self.operator_revert()
        self.assertTrue(retried['reconciled'])
        self.assertEqual(retried['comment_id'], first['comment_id'])
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'already reverts'):
            self.operator_revert(revert_payload(operation_id='revert-2'))
        self.assertEqual(len(self.issue['comments']), before)

    def test_revert_requires_a_currently_integrated_contribution(self):
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'requires a contribution that is currently integrated'):
            self.operator_revert(revert_payload(contribution=first))
        self.assertEqual(len(self.issue['comments']), before)

    def test_revert_must_name_the_reported_integration_commit(self):
        first = self.integration_case()
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'must name the contribution integration commit'):
            self.operator_revert(revert_payload(contribution=first, integration_commit=MERGE_2))
        self.assertEqual(len(self.issue['comments']), before)

    def test_revert_must_name_a_known_contribution(self):
        self.integration_case()
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'current or a prior contribution'):
            self.operator_revert(revert_payload(contribution='999'))
        self.assertEqual(len(self.issue['comments']), before)

    def test_later_passed_fact_with_another_integration_commit_reintegrates(self):
        self.integration_case()
        self.operator_revert()
        self.assertEqual(self.reviewed()['integration']['fact'], 'reverted')
        # The explicit re-integration evidence: a NEW passing scope/commit.
        self.record_lifecycle(COMMIT_1, MERGE_2, scope_op='scope-2')
        block = self.reviewed()['integration']
        self.assertEqual(block['fact'], 'passed')
        self.assertFalse(block['reverted'])
        self.assertEqual(block['integration_commit'], MERGE_2)
        self.assertEqual(self.shared()['review_state'], 'integrated')

    def test_reverting_a_prior_contribution_is_reported_by_the_follow_on(self):
        first = self.integration_case()
        second = self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None,
                                             follows=first))['comment_id']
        self.operator_revert(revert_payload(contribution=first))
        state = self.reviewed()
        prior = state['prior_contributions'][0]
        self.assertEqual(prior['comment_id'], first)
        self.assertEqual(prior['integration']['fact'], 'reverted')
        self.assertTrue(prior['integration']['reverted'])
        self.assertEqual(state['contribution']['comment_id'], second)

    def test_reverted_base_is_refused_with_no_write(self):
        first = self.integration_case()
        self.operator_revert(revert_payload(contribution=first))
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'not integrated'):
            self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before)

    def test_base_after_revert_is_accepted_again_once_reintegrated(self):
        first = self.integration_case()
        self.operator_revert(revert_payload(contribution=first))
        # The explicit re-integration evidence becomes the newest passing scope, so
        # the accepted base is its integration commit again.
        self.record_lifecycle(COMMIT_1, MERGE_2, scope_op='scope-2')
        before = len(self.issue['comments'])
        self.send(self.contribution(COMMIT_2, base=MERGE_2, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before + 1)

    def test_revert_of_one_of_two_passing_scopes_refuses_that_base(self):
        first = self.integration_case()
        self.record_lifecycle(COMMIT_1, MERGE_2, scope_op='scope-2')
        self.operator_revert(revert_payload(contribution=first, integration_commit=MERGE_2))
        block = self.reviewed()['integration']
        # Any-pass-wins keeps the work integrated through the OTHER, unreverted
        # scope, and the reported base moves back to that surviving pass.
        self.assertEqual(block['fact'], 'passed')
        self.assertEqual(block['integration_commit'], MERGE_1)
        self.assertEqual(block['newest_scope']['integration_commit'], MERGE_2)
        before = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'must equal the prior integration commit'):
            self.send(self.contribution(COMMIT_2, base=MERGE_2, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before)
        self.send(self.contribution(COMMIT_2, base=MERGE_1, supersedes=None, follows=first))
        self.assertEqual(len(self.issue['comments']), before + 1)

    def test_old_chain_reader_ignores_the_new_record(self):
        """Rollback compatibility: an old kit only knows the chain prefix.

        The revert comment sits in the middle of the chain; the pre-existing reader
        still walks the chain, so nothing fails. The documented limit is fail-open:
        the old kit cannot SEE the revert.
        """
        first = self.integration_case()
        self.operator_revert(revert_payload(contribution=first))
        ordered = w.records(self.issue)
        self.assertEqual([p['operation'] for p, _ in ordered], ['contribute', 'approve'])
        self.assertEqual(w.project(self.issue)['contribution']['comment_id'], first)

    def test_revert_record_does_not_change_any_existing_record_schema(self):
        self.assertEqual(w.EXTRA['approve'], {'contribution', 'summary'})
        self.assertEqual(w.EXTRA['contribute'],
                         {'repository', 'commit', 'base_commit', 'delivery', 'summary', 'supersedes'})
        self.assertNotIn('revert-record', w.EXTRA)
        self.assertEqual(w.REVERT_PREFIX, 'Kind: integration-revert-v1\n')

    def test_malformed_revert_comment_is_ignored_not_fatal(self):
        self.integration_case()
        cid = str(len(self.issue['comments']) + 1)
        self.issue['comments'].append(dict(id=cid, text=REVERT_PREFIX + '{not json',
                                           author=OPERATOR, created_at='2026-09-16T00:00:00Z'))
        self.assertEqual(self.reviewed()['integration']['fact'], 'passed')
        self.assertEqual(w.revert_records(self.issue, [OPERATOR])[1], [cid])


if __name__ == '__main__':
    unittest.main()
