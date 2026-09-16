import copy
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_workflow as w


class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.issue = dict(id='task-1', assignee='worker', status='in_progress', comments=[])
        self.actor = 'worker'
        self.count = 0

    def payload(self, op, **extra):
        self.count += 1
        return dict(schema_version=1, operation=op, operation_id='op-'+str(self.count),
                    task='task-1', previous=w.project(self.issue)['latest_comment_id'], **extra)

    def contribution(self, **extra):
        p = dict(repository='ssh://git.example/project', commit='a'*40, base_commit='b'*40,
                 delivery=dict(kind='bundle', path='koopa:/deliveries/revision-1.bundle', sha256='c'*64),
                 summary='Implementation and test evidence',
                 supersedes=(w.project(self.issue)['contribution'] or {}).get('comment_id'))
        p.update(extra)
        return self.payload('contribute', **p)

    def run_native(self, args):
        self.assertEqual(args[:3], ['comments', 'add', 'task-1'])
        cid = str(len(self.issue['comments'])+1)
        self.issue['comments'].append(dict(id=cid, text=args[3], author=self.actor,
                                          created_at='2026-09-16T00:00:00Z'))
        return json.dumps({'id': cid})

    def send(self, p, actor='worker'):
        self.actor = actor
        return w.execute([self.issue], 'task-1', actor, p, self.run_native)

    def request(self):
        return self.send(self.payload('request-changes', contribution=w.project(self.issue)['contribution']['comment_id'],
                                      items=[dict(id='fix', text='Correct the edge case')]), 'reviewer')['comment_id']

    def test_feedback_survives_new_revision_and_old_checkpoint(self):
        first = self.send(self.contribution())['comment_id']
        request = self.request()
        self.issue['comments'].append(dict(id='checkpoint', author='worker', created_at='2026-09-16T00:00:00Z',
                                           text='Kind: task-checkpoint-v1\n{"summary":"Ready for review"}'))
        second = self.send(self.contribution(delivery=dict(kind='remote', remote='ssh://git.example/project', branch='fix/revised')))['comment_id']
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'changes-requested')
        self.assertEqual(state['pending_requests'][0]['contribution'], first)
        self.assertEqual(state['contribution']['comment_id'], second)
        self.assertEqual(state['contribution']['supersedes'], first)
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            self.send(self.payload('approve', contribution=second, summary='Looks good'), 'reviewer')
        self.send(self.payload('respond', contribution=second, resolutions=[dict(request=request, item='fix', reason='Corrected', evidence=second)]))
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-review')
        self.send(self.payload('approve', contribution=second, summary='Reviewed revised tests'), 'reviewer')
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-integration')
        self.assertNotIn('lifecycle', w.project(self.issue))

    def test_stale_revision_review_and_cas(self):
        first = self.send(self.contribution())['comment_id']
        stale = self.payload('approve', contribution=first, summary='Reviewed')
        second = self.send(self.contribution())['comment_id']
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.send(stale, 'reviewer')
        stale['previous'] = second
        with self.assertRaisesRegex(ValueError, 'current contribution'):
            self.send(stale, 'reviewer')

    def test_idempotent_retry_after_handoff_and_later_records(self):
        p = self.contribution(); receipt = self.send(p)
        self.request(); self.issue['assignee'] = 'replacement'
        self.assertEqual(self.send(p)['comment_id'], receipt['comment_id'])
        self.assertTrue(self.send(p)['reconciled'])
        with self.assertRaisesRegex(ValueError, 'different payload or actor'):
            self.send(p, 'replacement')
        changed = dict(p, summary='Changed')
        with self.assertRaisesRegex(ValueError, 'different payload or actor'):
            self.send(changed)

    def test_owner_enforcement_and_closed_review(self):
        with self.assertRaisesRegex(ValueError, 'assigned owner'):
            self.send(self.contribution(), 'other')
        self.issue['assignee'] = None
        with self.assertRaisesRegex(ValueError, 'assigned owner'):
            self.send(self.contribution())
        self.issue['assignee'] = 'worker'; self.send(self.contribution()); self.issue['status'] = 'closed'
        with self.assertRaisesRegex(ValueError, 'Reopen'):
            self.request()

    def test_wrong_supersedes_and_unknown_resolution_do_not_write(self):
        self.send(self.contribution()); n = len(self.issue['comments'])
        with self.assertRaisesRegex(ValueError, 'supersede'):
            self.send(self.contribution(supersedes=None))
        with self.assertRaisesRegex(ValueError, 'unresolved request'):
            self.send(self.payload('respond', contribution='1', resolutions=[dict(request='none', item='fix', reason='done', evidence='commit')]))
        self.assertEqual(len(self.issue['comments']), n)

    def test_malformed_fork_and_unlinked_history_fail_closed(self):
        p = self.contribution(); self.send(p)
        original = copy.deepcopy(self.issue)
        for change in ('malformed', 'fork', 'unlinked'):
            self.issue = copy.deepcopy(original)
            bad = dict(p, operation_id='other')
            if change == 'unlinked': bad['previous'] = 'missing'
            raw = 'not-json' if change == 'malformed' else json.dumps(bad)
            self.issue['comments'].append(dict(id='other', text=w.PREFIX+raw, author='worker', created_at='now'))
            with self.assertRaises(ValueError): w.project(self.issue)

    def test_bounds_duplicate_items_and_exact_commits(self):
        for extra in ({'commit':'abc'}, {'base_commit':'z'*40}, {'summary':'x'*1201},
                      {'delivery':dict(kind='bundle', path='p', sha256='z'*64)}):
            with self.assertRaises(ValueError): self.send(self.contribution(**extra))
        self.send(self.contribution())
        items = [dict(id='i'+str(i), text='Fix') for i in range(20)]
        self.send(self.payload('request-changes', contribution='1', items=items), 'reviewer')
        with self.assertRaisesRegex(ValueError, '20 unresolved'):
            self.request()
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.send(self.payload('request-changes', contribution='1', items=[items[0], items[0]]), 'reviewer')

    def test_new_revision_invalidates_approval_but_no_native_status_change(self):
        self.send(self.contribution())
        self.send(self.payload('approve', contribution='1', summary='Approved'), 'reviewer')
        self.send(self.contribution())
        self.assertEqual(w.project(self.issue)['review_state'], 'awaiting-review')
        self.assertEqual(self.issue['status'], 'in_progress')

    def test_response_requires_owner_and_each_item_is_resolved_once(self):
        self.send(self.contribution(commit='a'*64, base_commit='b'*64))
        request = self.request()
        p = self.payload('respond', contribution='1', resolutions=[dict(request=request, item='fix', reason='Fixed', evidence='commit')])
        with self.assertRaisesRegex(ValueError, 'assigned owner'):
            self.send(p, 'reviewer')
        self.send(p)
        with self.assertRaisesRegex(ValueError, 'unresolved request'):
            self.send(self.payload('respond', contribution='1', resolutions=p['resolutions']))

    def test_no_contribution_is_explicit_and_ordinary_comments_are_ignored(self):
        self.issue['comments'] = [dict(text='Ready for review and deployed')]
        state = w.project(self.issue)
        self.assertEqual(state['review_state'], 'none')
        self.assertEqual(state['pending_requests'], [])
        self.assertIsNone(state['contribution'])
        with self.assertRaisesRegex(ValueError, 'current contribution'):
            self.send(self.payload('approve', contribution='unknown', summary='Approved'), 'reviewer')


if __name__ == '__main__': unittest.main()
