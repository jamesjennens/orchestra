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
* a newer lifecycle scope recorded for other work does not make a genuinely
  integrated prior un-followable (the shared projection is per-scope);
* no scoped evidence at all fails closed (there is no ``fact is None`` fallback);
* the accepted additive follow-on on an integrated prior;
* the HTTP review binding forwards the optional ``follows`` relation intact, so a
  follow-on over HTTP never silently becomes a first contribution or a supersede;
* the bounded ``brief`` prior-contribution slice with a complete ``review TASK``;
* what remains visible for a prior contribution after the follow-on records its
  own lifecycle scope (record/commit/relation, not re-scoped lifecycle facts).
"""
import json
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
                         actor='worker'):
        """Append scoped lifecycle evidence marking source_commit integrated.

        ``actor`` is the recording actor. The lifecycle action only requires
        ``payload.actor == request actor``, so the contributor can record it alone.
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
                                        value='passed'), actor, run)

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
            self.assertEqual(set(entry), {'comment_id', 'commit', 'relation', 'timestamp'})
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


if __name__ == '__main__':
    unittest.main()
