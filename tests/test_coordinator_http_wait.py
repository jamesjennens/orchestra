"""Grant-scoped HTTP coordinator wait, including the real endpoint/CLI fact."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import waiting
from http_auth import authority_request
from http_authority import CAP_COORDINATE
import http_authority
from http_auth import HttpError
import test_coordinator_grant as grants
import test_coordinator_grant_real as native


class CoordinatorWaitTests(grants.Grants):
    def wait(self, token=None, query=''):
        return self.request('GET', '/v1/agents/me/wait?form=coordinator' + query,
                            token=token or self.agent)

    def test_no_grant_and_worker_credentials_are_refused_before_a_read(self):
        with mock.patch.object(self.backend, 'coordinator_wait') as read:
            self.assertEqual(403, self.wait().status)
            worker, _credential = self.worker_of('olive', 'wait-worker')
            self.assertEqual(403, self.wait(worker).status)
            read.assert_not_called()

    def test_grant_revocation_and_owner_demotion_are_live(self):
        self.assertEqual(200, self.grant().status)
        self.assertEqual(200, self.wait().status)
        self.assertEqual(200, self.grant(method='DELETE').status)
        with mock.patch.object(self.backend, 'coordinator_wait') as read:
            self.assertEqual(403, self.wait().status)
            read.assert_not_called()
        self.assertEqual(200, self.grant().status)
        self.assertEqual(200, self.member('cora', 'contributor').status)
        self.assertEqual(403, self.wait().status)

    def test_one_project_reuses_projection_and_since_compares_without_scheduling(self):
        self.assertEqual(200, self.grant().status)
        task, _contribution = self.deliver(self.tokens['carl'])
        expected = self.backend.coordinator_wait(self.project, self.service.authenticate(self.agent), 15)
        with mock.patch.object(self.backend, 'read_tasks', wraps=self.backend.read_tasks) as read:
            answer = self.wait()
        self.assertEqual(200, answer.status, answer.data)
        self.assertEqual(1, read.call_count)
        self.assertEqual(expected['fingerprint'], answer.data['fingerprint'])
        self.assertEqual(expected['triggers'], answer.data['triggers'])
        self.assertIn(task, [r['task'] for r in answer.data['triggers']['awaiting-review']])
        same = self.wait(query='&since=' + answer.data['fingerprint'])
        self.assertIs(False, same.data['changed'])
        self.assertIs(True, self.wait(query='&since=wait1:' + '0' * 64).data['changed'])

    def test_only_projects_with_effective_grants_are_read_and_multiple_are_namespaced(self):
        beta = self.create_project(self.admin, 'Beta')
        self.assertEqual(200, self.member('cora', 'coordinator', project=beta, token=self.admin).status)
        secret, agent_id = self.agent_with_id('cora', 'Two-projects', projects=[self.project, beta])
        self.assertEqual(200, self.grant(agent_id).status)
        with mock.patch.object(self.backend, 'coordinator_wait', wraps=self.backend.coordinator_wait) as read:
            first = self.wait(secret)
        self.assertEqual([self.project], [call.args[0] for call in read.call_args_list])
        self.assertNotIn(beta, json.dumps(first.data))
        self.assertEqual(200, self.grant(agent_id, project=beta, token=self.admin).status)
        with mock.patch.object(self.backend, 'coordinator_wait', wraps=self.backend.coordinator_wait) as read:
            both = self.wait(secret)
        self.assertEqual(sorted([self.project, beta]), [call.args[0] for call in read.call_args_list])
        self.assertEqual(sorted([self.project, beta]), both.data['projects'])
        self.assertNotEqual(first.data['fingerprint'], both.data['fingerprint'])
        self.assertEqual(both.data['fingerprint'], self.wait(secret).data['fingerprint'])

    def test_query_validation(self):
        self.assertEqual(200, self.grant().status)
        for query in ('&since=bad', '&idle_minutes=0', '&idle_minutes=10081', '&idle_minutes=x'):
            with self.subTest(query=query):
                self.assertEqual(400, self.wait(query=query).status)
        self.assertEqual(400, self.request('GET', '/v1/agents/me/wait?form=other', token=self.agent).status)

    def test_canonical_authority_names_only_the_coordinator_wait_form(self):
        request = {'action': 'wait', 'project': self.project,
                   'args': ['--coordinator', '--idle-minutes', '15']}
        self.assertEqual(CAP_COORDINATE, http_authority.required_capability(request))
        for args in ([], ['--mine'], ['--worker']):
            self.assertIsNone(http_authority.required_capability(dict(request, args=args)))
        denied = http_authority.block_mismatch_denial(
            dict(request, authority={'project': self.project, 'capability': http_authority.CAP_READ}), object())
        self.assertEqual(403, denied['authority_status'])


def only_local_tests(cls):
    for base in cls.__mro__[1:]:
        for name in base.__dict__:
            if name.startswith('test_') and name not in cls.__dict__:
                setattr(cls, name, None)
    return cls


@only_local_tests
class NativeCoordinatorWaitTests(native.RealStackTests):
    def wait(self, since=None):
        query = '/v1/agents/me/wait?form=coordinator'
        if since is not None:
            query += '&since=' + since
        answer = self.request('GET', query, token=self.agent)
        self.assertEqual(200, answer.status, answer.data)
        return answer.data

    def same_cli_fact(self):
        with mock.patch.object(self.backend, '_endpoint', wraps=self.backend._endpoint) as read:
            answer = self.wait()
        self.assertEqual(1, read.call_count)
        self.assertEqual('wait', read.call_args.args[0])
        cli = self.backend._checked(self.backend._endpoint(
            'wait', 'pp', self.agent_actor, ['--coordinator', '--idle-minutes', '15'],
            authority=authority_request(self.service.authenticate(self.agent), 'pp', CAP_COORDINATE,
                                        now=self.service._expiry_now())), reading=True)
        self.assertEqual(cli['fingerprint'], answer['fingerprint'])
        self.assertEqual(cli['triggers'], answer['triggers'])
        return answer

    def test_native_triggers_cli_parity_own_progress_and_revocation(self):
        self.assertEqual(403, self.request('GET', '/v1/agents/me/wait?form=coordinator', token=self.agent).status)
        self.assertEqual(200, self.grant().status)
        initial = self.same_cli_fact()
        task = self.new('Worker needs coordinator')
        self.assertEqual(200, self.claim('casey', task).status)
        base = self.tasks + '/' + task
        history = self.request('GET', base + '/history', token=self.tokens['casey']).data
        cp = {'schema_version': 1, 'previous': None, 'activity_cursor': history['activity_cursor'],
              'source_commit': '', 'branch': '', 'intent': 'Implement', 'acceptance': 'Checks pass',
              'summary': 'Need feedback', 'next_action': 'Ask coordinator', 'open_items': [], 'resolved': [],
              'lane_state': {'schema_version': 1, 'task': task, 'state': 'waiting-on-coordinator',
                             'waiting_on': 'coordinator'}}
        saved = self.request('POST', base + '/checkpoints', cp, token=self.tokens['casey'])
        self.assertEqual(201, saved.status, saved.data)
        checkpoint = self.same_cli_fact()
        self.assertNotEqual(initial['fingerprint'], checkpoint['fingerprint'])
        self.assertIn(task, [row['task'] for row in checkpoint['triggers']['checkpoint-to-coordinator']])
        brief = self.request('GET', base + '/brief', token=self.tokens['casey']).data
        cp.update(previous=brief['checkpoint']['id'], activity_cursor=brief['activity_cursor'])
        cp['lane_state']['expect_by'] = '2000-01-01T00:00:00+00:00'
        self.assertEqual(201, self.request('POST', base + '/checkpoints', cp,
                                          token=self.tokens['casey']).status)
        quiet = self.same_cli_fact()
        self.assertNotEqual(checkpoint['fingerprint'], quiet['fingerprint'])
        self.assertTrue(any('expect_by_passed' in row['flags']
                            for row in quiet['triggers']['lane-quiet']))
        # Own progress on an untriggered task never enters the coordinator fact.
        own = self.new('Coordinator own progress')
        self.assertEqual(200, self.claim('cora', own).status)
        before = self.same_cli_fact()
        self.assertEqual(201, self.request('POST', self.tasks + '/' + own + '/comments',
                                         {'text': 'Own progress'}, token=self.agent).status)
        self.assertEqual(before['fingerprint'], self.same_cli_fact()['fingerprint'])
        delivered_task, contribution = self.deliver('drew')
        delivered = self.same_cli_fact()
        self.assertNotEqual(before['fingerprint'], delivered['fingerprint'])
        self.assertIn(delivered_task, [row['task'] for row in delivered['triggers']['awaiting-review']])
        self.assertEqual(201, self.approve(self.agent, delivered_task, contribution).status)
        approved = self.same_cli_fact()
        self.assertNotEqual(delivered['fingerprint'], approved['fingerprint'])
        self.assertIn(delivered_task, [row['task'] for row in approved['triggers']['awaiting-integration']])
        self.assertIs(False, self.wait(approved['fingerprint'])['changed'])
        principal = self.service.authenticate(self.agent)
        self.assertEqual(200, self.grant('DELETE').status)
        self.assertEqual(403, self.request('GET', '/v1/agents/me/wait?form=coordinator', token=self.agent).status)
        # Bypassing the route's check cannot reuse a stale authority descriptor.
        with self.assertRaises(HttpError) as denied:
            self.backend.coordinator_wait('pp', principal, 15)
        self.assertEqual(403, denied.exception.status)
