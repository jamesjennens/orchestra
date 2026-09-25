"""Follow-on contributions to already-integrated work (kittrial-5bb.25).

An additive second change to a task whose contribution is already integrated uses
the optional ``follows`` relation instead of being forced to declare that it
``supersedes`` (retracts) the integrated revision. These tests pin the additive
relation, backward compatibility of existing ``supersedes`` chains, and the
end-to-end projection that shows both the integrated prior change and the new
pending one while the prior lifecycle facts stay intact.
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
MERGE_1 = 'e' * 40


class FollowOnChainTests(unittest.TestCase):
    def setUp(self):
        self.issue = dict(id='task-1', title='Follow-on task', assignee='worker',
                          status='in_progress', labels=[], comments=[])
        self.count = 0

    def payload(self, op, **extra):
        self.count += 1
        return dict(schema_version=1, operation=op, operation_id='op-' + str(self.count),
                    task='task-1', previous=w.project(self.issue)['latest_comment_id'], **extra)

    def contribution(self, commit, **extra):
        p = dict(repository='ssh://git.example/project', commit=commit, base_commit='b' * 40,
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
        return w.execute([self.issue], 'task-1', actor, p, self.run_native)

    def lifecycle_rows(self, comments, source_commit):
        """Task rows with scoped lifecycle evidence marking source_commit integrated."""
        rows = [dict(_type='issue', id='task-1', title='Follow-on task', issue_type='task',
                     status='in_progress', assignee='worker', labels=[], comments=comments,
                     created_at='2026-09-16T00:00:00Z', updated_at='2026-09-16T00:00:00Z')]

        def run(args):
            if args == ['export', '--all']:
                return ''.join(json.dumps(r) + '\n' for r in rows)
            assert args[0] == 'set-state', args
            dim, value = args[2].split('=', 1)
            issue = rows[0]
            old = next((x.split(':', 1)[1] for x in issue['labels'] if x.startswith(dim + ':')), None)
            issue['labels'] = [x for x in issue['labels'] if not x.startswith(dim + ':')] + [dim + ':' + value]
            event_id = 'task-1.' + str(len(rows))
            reason = args[args.index('--reason') + 1]
            description = (('Set ' if old is None else 'Changed ') + dim +
                           (' to ' if old is None else ' from ' + old + ' to ') + value +
                           '\n\nReason: ' + reason)
            rows.append(dict(_type='issue', id=event_id, issue_type='event',
                             title='State change: ' + dim + ' \u2192 ' + value, description=description,
                             status='closed', created_by='worker', created_at='2026-09-16T00:00:00Z',
                             dependencies=[dict(issue_id=event_id, depends_on_id='task-1',
                                                type='parent-child')]))
            return json.dumps(dict(changed=True, dimension=dim, event_id=event_id, new_value=value))

        scope = {'source_commit': source_commit, 'integration_commit': MERGE_1,
                 'release_id': '', 'environment': ''}
        base = dict(schema_version=1, task='task-1', scope=scope, evidence=['commit:' + source_commit],
                    provenance='performed', actor='worker')
        lifecycle.apply_native(dict(base, operation_id='scope-1', dimension='lifecycle-scope',
                                    value=content_hash(scope)), 'worker', run)
        lifecycle.apply_native(dict(base, operation_id='integrated-1', dimension='integrated',
                                    value='passed'), 'worker', run)
        return rows

    def test_additive_follow_on_keeps_prior_contribution_visible(self):
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.send(self.payload('approve', contribution=first, summary='Reviewed'), 'reviewer')
        second = self.send(self.contribution(COMMIT_2, base_commit=MERGE_1,
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

    def test_brief_shows_integrated_prior_and_pending_follow_on(self):
        first = self.send(self.contribution(COMMIT_1))['comment_id']
        self.send(self.payload('approve', contribution=first, summary='Reviewed'), 'reviewer')
        rows = self.lifecycle_rows(self.issue['comments'], COMMIT_1)
        self.issue = rows[0]
        second = self.send(self.contribution(COMMIT_2, base_commit=MERGE_1,
                                             supersedes=None, follows=first))['comment_id']

        result = briefing.brief(rows, 'proj', 'task-1')
        review = result['review']
        self.assertEqual(review['review_state'], 'awaiting-review')
        self.assertEqual(review['contribution']['comment_id'], second)
        self.assertEqual([c['comment_id'] for c in review['prior_contributions']], [first])
        self.assertEqual(review['prior_contributions'][0]['relation'], 'follows')
        # The prior revision's scoped integration fact is untouched and still visible.
        self.assertEqual(result['lifecycle']['integrated']['value'], 'passed')
        self.assertEqual(result['lifecycle_scope']['source_commit']['text'], COMMIT_1)


if __name__ == '__main__':
    unittest.main()
