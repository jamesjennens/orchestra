"""Follow-on contributions to already-integrated work (kittrial-5bb.25).

An additive second change to a task whose contribution is already integrated uses
the optional ``follows`` relation instead of being forced to declare that it
``supersedes`` (retracts) the integrated revision. These tests pin:

* the integration precondition for ``follows`` (refused with zero native writes
  for an awaiting-review prior, an approved-but-unintegrated prior, and a prior
  whose integration commit does not match the follow-on ``base_commit``);
* the accepted additive follow-on on an integrated prior;
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
        self.issue['comments'].append(dict(id=cid, text=args[3], author='worker',
                                           created_at='2026-09-16T00:00:00Z'))
        return json.dumps({'id': cid})

    def send(self, p, actor='worker'):
        return w.execute(self.rows, 'task-1', actor, p, self.run_native)

    def record_lifecycle(self, source_commit, integration_commit, scope_op='scope-1', integrated=True):
        """Append scoped lifecycle evidence marking source_commit integrated."""
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
                                  description=description, status='closed', created_by='worker',
                                  created_at='2026-09-16T00:00:00Z',
                                  dependencies=[dict(issue_id=event_id, depends_on_id='task-1',
                                                     type='parent-child')]))
            return json.dumps(dict(changed=True, dimension=dim, event_id=event_id, new_value=value))

        scope = {'source_commit': source_commit, 'integration_commit': integration_commit,
                 'release_id': '', 'environment': ''}
        base = dict(schema_version=1, task='task-1', scope=scope, evidence=['commit:' + source_commit],
                    provenance='performed', actor='worker')
        lifecycle.apply_native(dict(base, operation_id=scope_op, dimension='lifecycle-scope',
                                    value=content_hash(scope)), 'worker', run)
        if integrated:
            lifecycle.apply_native(dict(base, operation_id=scope_op + '-int', dimension='integrated',
                                        value='passed'), 'worker', run)

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
        with self.assertRaisesRegex(ValueError, 'not integrated'):
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

    def test_minimum_rule_when_scoped_evidence_is_unavailable(self):
        payload = dict(operation='contribute', follows='1', base_commit='b' * 40)
        pending = dict(contribution={'commit': COMMIT_1}, review_state='awaiting-integration',
                       pending_requests=[])
        w.require_integrated_follow_on(payload, pending, None)
        for state in (dict(pending, review_state='awaiting-review'),
                      dict(pending, review_state='changes-requested'),
                      dict(pending, review_state='awaiting-integration',
                           pending_requests=[{'item': 'fix'}])):
            with self.assertRaisesRegex(ValueError, 'without scoped integration evidence'):
                w.require_integrated_follow_on(payload, state, None)

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
