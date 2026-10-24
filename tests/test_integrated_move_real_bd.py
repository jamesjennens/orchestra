"""kittrial-5bb.336 with a real pinned bd: an integrated-but-open task is not the
assignee's move, does not wake the worker's wait, and does not count against the
web claim limit.

The chain is written through the canonical endpoint on a real tracker (contribute,
approve, a scoped integrated fact), so the projection the row reaches is the real
one; nothing is planted.
"""
import json
import sys
import uuid
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import test_bd_label_aliases as fixture


@unittest.skipIf(fixture.endpoint is None, 'POSIX endpoint imports unavailable')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class IntegratedMoveRealBdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = type('IntegratedMoveRuntime', (fixture.RealBdLabelAliasTests,), {})
        cls.runtime.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.runtime.tearDownClass()

    def setUp(self):
        self.fixture = self.runtime(methodName='runTest')
        self.root = self.fixture.root

    def answer(self, action, args, actor='op'):
        reply = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': actor,
                                                     'action': action, 'args': args})
        self.assertEqual(reply['returncode'], 0, reply)
        return json.loads(reply['stdout'])

    def integrated_task(self, worker):
        """A task held by ``worker`` whose approved contribution carries scoped
        integration evidence: the row reads review_state integrated, status open."""
        made = self.fixture.bd('create', 'Integrated but open', '--assignee', worker, '--json')
        self.assertEqual(made.returncode, 0, made.stderr)
        task = json.loads(made.stdout)['id']
        contribute = json.dumps({'schema_version': 1, 'operation': 'contribute',
            'operation_id': 'im-contribute-'+uuid.uuid4().hex[:8], 'previous': None, 'task': task,
            'repository': 'ssh://git.example/project', 'commit': 'a' * 40, 'base_commit': 'b' * 40,
            'delivery': {'kind': 'bundle', 'path': 'reviewer:/rev.bundle', 'sha256': 'c' * 64},
            'summary': 'Delivered', 'supersedes': None})
        reply = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': worker,
            'action': 'review', 'args': [task, '@attachment:0'],
            'attachments': {'0': {'flag': '--file', 'text': contribute}}})
        self.assertEqual(reply['returncode'], 0, reply)
        contribution = json.loads(reply['stdout'])['comment_id']
        approve = json.dumps({'schema_version': 1, 'operation': 'approve',
            'operation_id': 'im-approve-'+uuid.uuid4().hex[:8], 'previous': contribution, 'task': task,
            'contribution': contribution, 'summary': 'Approved'})
        reply = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': 'op',
            'action': 'review', 'args': [task, '@attachment:0'],
            'attachments': {'0': {'flag': '--file', 'text': approve}}})
        self.assertEqual(reply['returncode'], 0, reply)
        # Create the project's merge slot (the fixture runtime has none), then hold
        # it for the integrated fact, exactly as the route requires.
        reply = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': 'op',
            'action': 'coordinate', 'args': [json.dumps({'operation': 'merge-create'})]})
        self.assertEqual(reply['returncode'], 0, reply)
        acquire = json.dumps({'operation': 'merge-acquire', 'task': task, 'target': 'main'})
        reply = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': 'op',
            'action': 'coordinate', 'args': [acquire]})
        self.assertEqual(reply['returncode'], 0, reply)
        from lifecycle import content_hash
        scope = {'source_commit': 'a' * 40, 'integration_commit': 'e' * 40,
                 'release_id': 'im-release-'+uuid.uuid4().hex[:8], 'environment': 'synthetic'}
        for dimension, value in (('lifecycle-scope', content_hash(scope)), ('integrated', 'passed')):
            payload = json.dumps({'schema_version': 1, 'operation_id': 'im-'+dimension+'-'+uuid.uuid4().hex[:8],
                'task': task, 'dimension': dimension, 'value': value, 'scope': scope,
                'actor': 'op', 'evidence': ['real-bd test'], 'provenance': 'performed'})
            reply = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': 'op',
                'action': 'lifecycle', 'args': [payload]})
            self.assertEqual(reply['returncode'], 0, reply)
        # The slot is the project's ONE: release it so a later test can hold it.
        release = json.dumps({'operation': 'merge-release'})
        reply = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': 'op',
            'action': 'coordinate', 'args': [release]})
        self.assertEqual(reply['returncode'], 0, reply)
        return task

    def test_the_move_the_wait_and_the_role_on_a_real_tracker(self):
        worker = 'worker/im'
        task = self.integrated_task(worker)
        page = self.answer('work', ['--mine', '--json'], worker)
        item = next(row for row in page['items'] if row['task'] == task)
        self.assertEqual(item['review_state'], 'integrated')
        self.assertEqual((item['whose_move'], item['next_actor']), ('coordinator', 'coordinator'))
        self.assertIn('the coordinator closes the task or records what remains', item['next'])
        self.assertEqual(page['your_move']['count'], 0)
        # The worker's WAIT is not woken by the integrated row: the worker form's
        # items list only rows whose move is the caller's.
        view = self.answer('wait', [], worker)
        self.assertEqual([row for row in view['items'] if row['task'] == task], [])
        # The coordinator's scan lists it as its own move to close.
        coordinator = self.answer('work', ['--state', 'integrated', '--json'], 'op')
        row = next(entry for entry in coordinator['items'] if entry['task'] == task)
        # 'op' is not the assignee, so the caller-relative answer is unknown here, but
        # the ROLE the row names is the coordinator's - the coordinator's own scan of
        # --state integrated is its to-close list.
        self.assertEqual((row['whose_move'], row['next_actor']), ('unknown', 'coordinator'))

    def test_a_change_requested_task_still_wakes_the_worker_beside_it(self):
        worker = 'worker/im2'
        integrated = self.integrated_task(worker)
        made = self.fixture.bd('create', 'A real change request', '--assignee', worker, '--json')
        other = json.loads(made.stdout)['id']
        contribute = json.dumps({'schema_version': 1, 'operation': 'contribute',
            'operation_id': 'im2-contribute-'+uuid.uuid4().hex[:8], 'previous': None, 'task': other,
            'repository': 'ssh://git.example/project', 'commit': 'f' * 40, 'base_commit': 'b' * 40,
            'delivery': {'kind': 'bundle', 'path': 'reviewer:/rev2.bundle', 'sha256': 'd' * 64},
            'summary': 'Delivered', 'supersedes': None})
        reply = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': worker,
            'action': 'review', 'args': [other, '@attachment:0'],
            'attachments': {'0': {'flag': '--file', 'text': contribute}}})
        self.assertEqual(reply['returncode'], 0, reply)
        delivered = json.loads(reply['stdout'])['comment_id']
        request = json.dumps({'schema_version': 1, 'operation': 'request-changes',
            'operation_id': 'im2-request-'+uuid.uuid4().hex[:8], 'previous': delivered, 'task': other,
            'contribution': delivered,
            'items': [{'id': 'one', 'text': 'One change, asked so the row is the worker\'s move.'}]})
        reply = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': 'op',
            'action': 'review', 'args': [other, '@attachment:0'],
            'attachments': {'0': {'flag': '--file', 'text': request}}})
        self.assertEqual(reply['returncode'], 0, reply)
        view = self.answer('wait', [], worker)
        self.assertEqual([row['task'] for row in view['items']], [other])
        self.assertNotIn(integrated, [row['task'] for row in view['items']])


if __name__ == '__main__':
    unittest.main()
