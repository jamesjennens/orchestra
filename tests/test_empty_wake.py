"""Empty worker wakes are recorded and shown (kittrial-5bb.397).

An empty wake is a fingerprint change with no row that is the caller's move. The one
rule here is shared by the CLI endpoint read (``work.execute``'s ``wait`` action) and
the agent HTTP read (``GET /v1/agents/me/wait``). Every test uses a scratch project
directory: nothing reads a tracker, nothing writes one, and nothing sleeps.
"""
import json
import concurrent.futures
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import lanes
import test_http_agents
import wake_log
import waiting
import work

A = 'session-aaaaaaaa-1111-4111-8111-111111111111'
B = 'session-bbbbbbbb-2222-4222-8222-222222222222'


def a_row(task='t1', whose_move='you', review_state='changes-requested', **extra):
    row = {'task': task, 'id': task, 'review_state': review_state, 'whose_move': whose_move,
           'next_actor': 'assignee' if whose_move == 'you' else 'coordinator',
           'owner': A, 'assignee': A, 'status': 'open', 'contribution_id': 'c1',
           'pending_review_items': 0, 'redelivered': False,
           'wait_newer_activity_marker': 'm1'}
    row.update(extra)
    return row


def append_one(arguments):
    root, index = arguments
    import wake_log as module
    return module.append(root, A, [{'schema': module.SCHEMA, 'at': '2026-10-24T12:00:00Z',
                                    'actor': A, 'fingerprint': 'wait1:' + ('%064x' % index),
                                    'task': 't%d' % index, 'trigger': 'edit', 'empty': True}])


class EmptyWakeRecordTests(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='empty-wake-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def baseline(self, rows=None):
        rows = rows or [a_row()]
        view = waiting.worker_view(rows)
        warnings = wake_log.observe(self.root, A, None, view, rows)
        self.assertEqual(warnings, [])
        self.assertEqual(wake_log.read(self.root, A)['empty_wakes_24h'], 0)
        return view

    def test_an_empty_wake_is_recorded_once_and_shown(self):
        first = self.baseline()
        changed = [a_row(whose_move='coordinator', review_state='integrated')]
        view = waiting.worker_view([])
        self.assertNotEqual(view['fingerprint'], first['fingerprint'])
        self.assertEqual(wake_log.observe(self.root, A, first['fingerprint'], view, changed), [])
        summary = wake_log.read(self.root, A)
        self.assertEqual(summary['empty_wakes_24h'], 1)
        self.assertEqual(summary['empty_wake_task'], 't1')
        self.assertEqual(summary['empty_wake_trigger'], 'review-state')
        self.assertTrue(summary['empty_wake_at'].endswith('Z'))
        # The same wake is never recorded twice.
        wake_log.observe(self.root, A, first['fingerprint'], view, changed)
        self.assertEqual(wake_log.read(self.root, A)['empty_wakes_24h'], 1)
        # Only this actor's own lane exists.
        self.assertEqual(wake_log.read(self.root, B)['empty_wakes_24h'], 0)
        lane_dir = self.root / wake_log.DIRECTORY
        self.assertEqual(sorted(path.name for path in lane_dir.iterdir()
                                if path.suffix == '.jsonl'),
                         [wake_log.actor_key(A) + '.jsonl'])

    def test_a_real_wake_is_not_recorded(self):
        first = self.baseline()
        row = a_row(review_state='changes-requested', pending_review_items=2)
        view = waiting.worker_view([row])
        self.assertNotEqual(view['fingerprint'], first['fingerprint'])
        wake_log.observe(self.root, A, first['fingerprint'], view, [row])
        self.assertEqual(wake_log.read(self.root, A)['empty_wakes_24h'], 0)

    def test_a_change_with_no_fingerprint_change_is_not_a_wake(self):
        first = self.baseline()
        view = waiting.worker_view([a_row()])
        wake_log.observe(self.root, A, view['fingerprint'], view, [a_row()])
        self.assertEqual(wake_log.read(self.root, A)['empty_wakes_24h'], 0)
        self.assertIsNotNone(first['fingerprint'])

    def test_the_24_hour_window_hides_old_entries(self):
        wake_log.append(self.root, A, [
            {'schema': wake_log.SCHEMA, 'at': '2026-10-20T00:00:00Z', 'actor': A,
             'fingerprint': 'wait1:' + '1' * 64, 'task': 'old', 'trigger': 'edit', 'empty': True}])
        self.assertEqual(wake_log.read(self.root, A, now='2026-10-20T12:00:00Z')['empty_wakes_24h'], 1)
        self.assertEqual(wake_log.read(self.root, A, now='2026-10-24T12:00:00Z')['empty_wakes_24h'], 0)

    def test_the_file_is_bounded_and_keeps_the_newest(self):
        for index in range(wake_log.MAX_ENTRIES + 12):
            wake_log.append(self.root, A, [
                {'schema': wake_log.SCHEMA, 'at': '2026-10-24T12:00:00Z', 'actor': A,
                 'fingerprint': 'wait1:%064x' % index, 'task': 't%d' % index,
                 'trigger': 'edit', 'empty': True}])
        lane = self.root / wake_log.DIRECTORY / (wake_log.actor_key(A) + '.jsonl')
        raw = lane.read_bytes()
        self.assertLessEqual(len(raw), wake_log.MAX_BYTES)
        entries = [json.loads(line) for line in raw.decode('utf-8').splitlines()]
        self.assertLessEqual(len(entries), wake_log.MAX_ENTRIES)
        self.assertEqual(entries[-1]['task'], 't%d' % (wake_log.MAX_ENTRIES + 11))

    def test_two_concurrent_writers_lose_nothing_and_duplicate_nothing(self):
        # Two waits by the same actor may run at once (an SSH wait and a bridge wait, or
        # a restarted client): the lane lock must make the read-modify-write one step.
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(append_one,
                                    [(str(self.root), index) for index in range(8)]))
        self.assertEqual(sorted(results), [1] * 8)
        summary = wake_log.read(self.root, A)
        self.assertEqual(summary['empty_wakes_24h'], 8)

    def test_a_symlinked_log_directory_is_refused_and_never_raises(self):
        outside = Path(tempfile.mkdtemp(prefix='empty-outside-'))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (self.root / wake_log.DIRECTORY).symlink_to(outside, target_is_directory=True)
        view = waiting.worker_view([])
        warnings = wake_log.observe(self.root, A, waiting.fingerprint([a_row()]), view,
                                    [a_row(whose_move='coordinator')])
        self.assertTrue(any('symlink' in warning for warning in warnings), warnings)
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_symlinked_lane_file_is_refused_and_never_raises(self):
        directory = self.root / wake_log.DIRECTORY
        directory.mkdir()
        outside = Path(tempfile.mkdtemp(prefix='empty-outside-'))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (directory / (wake_log.actor_key(A) + '.jsonl')).symlink_to(outside / 'x.jsonl')
        view = waiting.worker_view([])
        warnings = wake_log.observe(self.root, A, waiting.fingerprint([a_row()]), view,
                                    [a_row(whose_move='coordinator')])
        self.assertTrue(warnings)
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_broken_project_path_is_a_warning_not_an_exception(self):
        file_path = self.root / 'not-a-directory'
        file_path.write_text('x', encoding='utf-8')
        view = waiting.worker_view([])
        warnings = wake_log.observe(file_path, A, waiting.fingerprint([a_row()]), view,
                                    [a_row(whose_move='coordinator')])
        self.assertTrue(warnings)

    def test_trigger_names_the_field_class_that_changed(self):
        cases = [
            ({'wait_newer_activity_marker': 'm2'}, 'new-comment'),
            ({'review_state': 'integrated'}, 'review-state'),
            ({'owner': B, 'assignee': B}, 'assignment'),
            ({'contribution_id': 'c2'}, 'delivery'),
            ({'pending_review_items': 2}, 'review-items'),
            ({'status': 'closed'}, 'status'),
            ({'guidance_version': 'v2'}, 'guidance'),
            ({'next_actor': 'reviewer'}, 'edit'),
        ]
        before = wake_log.normalize_row(a_row())
        for change, expected in cases:
            with self.subTest(change=change):
                after = wake_log.normalize_row(a_row(**change))
                changed = wake_log.changed_wakes([before], [a_row(**change)], waiting.worker_view([]))
                self.assertEqual(changed, [{'task': 't1', 'trigger': expected,
                                            'whose_move': after['whose_move']}])

    def test_added_and_removed_rows_are_changes(self):
        changed = wake_log.changed_wakes([wake_log.normalize_row(a_row('gone'))],
                                         [a_row('new')], waiting.worker_view([]))
        self.assertEqual([row['task'] for row in changed], ['gone', 'new'])
        self.assertEqual([row['trigger'] for row in changed], ['edit', 'assignment'])


class LaneBoardTests(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='empty-lane-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def row(self, task='t1'):
        return {'id': task, 'issue_type': 'task', 'status': 'open', 'assignee': A,
                'labels': [], 'dependencies': [], 'comments': [],
                'title': 'Task ' + task, 'description': 'Intent',
                'acceptance_criteria': 'Acceptance'}

    def record(self, fingerprints):
        for fingerprint in fingerprints:
            wake_log.append(self.root, A, [
                {'schema': wake_log.SCHEMA, 'at': '2026-10-24T12:00:00Z', 'actor': A,
                 'fingerprint': 'wait1:' + fingerprint * 64, 'task': 't1',
                 'trigger': 'review-state', 'empty': True}])

    def test_the_lane_row_carries_the_count_and_newest_wake(self):
        self.record(['a'])
        page = lanes.read([self.row()], A, {'schema_version': 1, 'records': {}},
                          ['--limit', '20'], journal=self.root)
        lane = page['items'][0]
        self.assertEqual(lane['actor'], A)
        self.assertEqual(lane['empty_wakes_24h'], 1)
        self.assertEqual(lane['empty_wake_task'], 't1')
        self.assertEqual(lane['empty_wake_trigger'], 'review-state')
        self.assertTrue(lane['empty_wake_at'].endswith('Z'))
        # The same read without a project directory keeps every old field and adds the
        # zeroes; no existing field changes because of the new ones.
        old = lanes.read([self.row()], A, {'schema_version': 1, 'records': {}},
                         ['--limit', '20'])
        added = ('empty_wakes_24h', 'empty_wake_task', 'empty_wake_trigger', 'empty_wake_at')
        for name, value in old['items'][0].items():
            if name in added:
                continue
            self.assertEqual(value, lane[name], name)
        self.assertEqual(old['items'][0]['empty_wakes_24h'], 0)

    def test_the_coordinator_trigger_fires_at_three_not_two(self):
        self.record(['b', 'c'])
        page = lanes.read([self.row()], A, {'schema_version': 1, 'records': {}},
                          ['--limit', '20'], journal=self.root)
        view = waiting.coordinator_view([], page)
        self.assertEqual(view['triggers']['empty-wake'], [])
        self.assertEqual(wake_log.TRIGGER_LIMIT, 3)
        # A quiet answer keeps the fingerprint it had before the new trigger existed.
        self.assertEqual(view['fingerprint'],
                         waiting.digest({name: [] for name in waiting.COORDINATOR_TRIGGERS
                                         if name != 'empty-wake'}))
        self.record(['d'])
        page = lanes.read([self.row()], A, {'schema_version': 1, 'records': {}},
                          ['--limit', '20'], journal=self.root)
        view = waiting.coordinator_view([], page)
        self.assertEqual(len(view['triggers']['empty-wake']), 1)
        row = view['triggers']['empty-wake'][0]
        self.assertEqual(row['lane'], A)
        self.assertEqual(row['task'], 't1')
        self.assertEqual(row['empty_wakes_24h'], 3)


class WorkWaitRecordingTests(unittest.TestCase):
    """The CLI endpoint read records the empty wake from its ONE ``work --mine`` read."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='empty-work-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.project = self.root / 'projects' / 'p'
        self.project.mkdir(parents=True)

    def row(self, status='open'):
        return {'id': 't1', 'title': 'Task t1', 'issue_type': 'task', 'status': status,
                'assignee': A, 'description': 'Intent', 'acceptance_criteria': 'Acceptance',
                'labels': [], 'priority': 2, 'created_by': A,
                'created_at': '2026-09-01T00:00:00Z', 'updated_at': '2026-09-15T09:00:00Z',
                'dependencies': [], 'comments': []}

    def run_wait(self, rows, args):
        lines = '\n'.join(json.dumps(row) for row in rows)

        def runner(_argv):
            return lines
        return work.execute(self.project, A, 'wait', args, {}, runner, operators=[A])

    def test_an_empty_wake_from_the_wait_is_recorded(self):
        first = self.run_wait([self.row()], [])
        self.assertIn('t1', [item['task'] for item in first['items']])
        self.assertEqual(wake_log.read(self.project, A)['empty_wakes_24h'], 0)
        second = self.run_wait([self.row('closed')], ['--since', first['fingerprint']])
        self.assertEqual(second['items'], [])
        summary = wake_log.read(self.project, A)
        self.assertEqual(summary['empty_wakes_24h'], 1)
        self.assertEqual(summary['empty_wake_task'], 't1')

    def test_a_real_wake_stays_a_real_wake_and_is_not_recorded(self):
        first = self.run_wait([self.row()], [])
        second = self.run_wait([self.row()], [])
        self.assertEqual(first['fingerprint'], second['fingerprint'])
        self.run_wait([self.row()], ['--since', first['fingerprint']])
        self.assertEqual(wake_log.read(self.project, A)['empty_wakes_24h'], 0)

    def test_an_invalid_since_is_refused_by_the_read(self):
        with self.assertRaises(ValueError):
            self.run_wait([self.row()], ['--since', 'nonsense'])


@unittest.skipIf(test_http_agents is None, 'the http service needs a POSIX host')
class HttpWorkerWaitRecordingTests(test_http_agents.AgentHarness):
    """The agent route records the same empty wake from its one read (no extra read)."""

    def setUp(self):
        super().setUp()
        self.backend.wake_root = self.tmp_path / 'wakes'
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.alex = self.login('alex', 'alex-password-1')[0]
        self.project = self.create_project(self.alex, 'Alpha')
        self.agent_id, self.secret, _data = self.agent_secret(self.alex, projects=[self.project])

    def actor(self):
        record = self.service.state['agents'][self.agent_id]
        return self.service.agent_actor(record)

    def log_dir(self):
        return self.backend.wake_root / self.project

    def test_an_empty_wake_is_recorded_once_and_matches_the_endpoint_rule(self):
        created = self.create_task(self.alex, self.project, 'first')
        task_id = created.data['id']
        claimed = self.request('POST', '/v1/projects/%s/tasks/%s/claim'
                               % (self.project, task_id), token=self.secret)
        self.assertEqual(200, claimed.status, claimed.data)
        first = self.request('GET', '/v1/agents/me/wait', token=self.secret)
        self.assertEqual(200, first.status, first.data)
        self.assertIn(task_id, [row['task'] for row in first.data['items']])
        self.assertEqual(wake_log.read(self.log_dir(), self.actor())['empty_wakes_24h'], 0)
        current = self.request('GET', '/v1/projects/%s/tasks/%s' % (self.project, task_id),
                               token=self.secret)
        self.assertEqual(200, current.status, current.data)
        version = current.data['version']
        closed = self.request('PATCH', '/v1/projects/%s/tasks/%s' % (self.project, task_id),
                              {'status': 'closed', 'version': version},
                              token=self.secret, key='close-once')
        self.assertEqual(200, closed.status, closed.data)
        woke = self.request('GET', '/v1/agents/me/wait?since=' + first.data['fingerprint'],
                            token=self.secret)
        self.assertEqual(200, woke.status, woke.data)
        self.assertIs(True, woke.data['changed'])
        self.assertEqual(woke.data['items'], [])
        summary = wake_log.read(self.log_dir(), self.actor())
        self.assertEqual(summary['empty_wakes_24h'], 1)
        self.assertEqual(summary['empty_wake_task'], task_id)
        # The same rows and ``since`` classify identically through the shared rule.
        endpoint_dir = self.tmp_path / 'endpoint-wakes'
        previous_view = waiting.worker_view([a_row(task_id)])
        wake_log.observe(endpoint_dir, self.actor(), None, previous_view, [a_row(task_id)])
        view = waiting.worker_view([])
        wake_log.observe(endpoint_dir, self.actor(), previous_view['fingerprint'],
                         dict(view, fingerprint=woke.data['fingerprint']), [])
        endpoint = wake_log.read(endpoint_dir, self.actor())
        self.assertEqual(endpoint['empty_wake_task'], summary['empty_wake_task'])
        self.assertEqual(endpoint['empty_wake_trigger'], summary['empty_wake_trigger'])
        # A second poll of the same wake records nothing new.
        self.request('GET', '/v1/agents/me/wait?since=' + first.data['fingerprint'],
                     token=self.secret)
        self.assertEqual(wake_log.read(self.log_dir(), self.actor())['empty_wakes_24h'], 1)


if __name__ == '__main__':
    unittest.main()
