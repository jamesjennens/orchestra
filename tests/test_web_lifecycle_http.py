"""Real HTTP boundary with the disposable backend's explicit host limits."""
import copy
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from test_http_agents import AgentHarness


class LifecycleHttpTests(AgentHarness):
    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.project = self.create_project(self.admin, 'Lifecycle')
        made = self.request('POST', '/v1/projects/' + self.project + '/tasks',
                            dict(title='Lifecycle HTTP'), token=self.admin)
        self.assertEqual(made.status, 201, made.data)
        self.task = made.data['id']
        self.path = '/v1/projects/' + self.project + '/tasks/' + self.task + '/lifecycle'
        self.scope = dict(source_commit='a' * 40, integration_commit='b' * 40,
                          release_id='synthetic-release', environment='synthetic')

    def scope_body(self, expected=None):
        return dict(scope=copy.deepcopy(self.scope), expected_scope_token=expected,
                    evidence=['synthetic scope evidence'], provenance='performed')

    def fact_body(self, value='passed'):
        return dict(scope=copy.deepcopy(self.scope), value=value,
                    evidence=['synthetic HTTP evidence'], provenance='performed')

    def post(self, suffix, body, key, token=None):
        return self.request('POST', self.path + suffix, body,
                            key='lifecycle-test-' + key if key is not None else None,
                            token=token or self.admin)

    def state(self, token=None):
        result = self.request('GET', self.path, token=token or self.admin)
        self.assertEqual(result.status, 200, result.data)
        return result.data

    def set_scope(self):
        result = self.post('/scope', self.scope_body(), 'scope')
        self.assertEqual(result.status, 201, result.data)
        return result

    def test_scope_facts_current_token_cas_and_scoped_history(self):
        before = self.state()
        self.assertIsNone(before['scope_token'])
        self.assertEqual(set(before['facts']), {'implemented', 'tested', 'reviewed', 'integrated', 'deployed', 'live-verified'})
        self.set_scope()
        result = self.post('/facts/tested', self.fact_body(), 'tested')
        self.assertEqual(result.status, 201, result.data)
        self.assertEqual(self.state()['facts']['tested']['evidence'], ['synthetic HTTP evidence'])
        old_token = self.state()['scope_token']
        self.scope['environment'] = 'next'
        stale = self.post('/scope', self.scope_body(), 'stale')
        self.assertEqual(stale.status, 409, stale.data)
        moved = self.post('/scope', self.scope_body(old_token), 'move')
        self.assertEqual(moved.status, 201, moved.data)
        self.assertEqual(self.state()['facts']['tested']['value'], 'unknown')
        next_token = self.state()['scope_token']
        self.scope['environment'] = 'synthetic'
        back = self.post('/scope', self.scope_body(next_token), 'back')
        self.assertEqual(back.status, 201, back.data)
        self.assertEqual(self.state()['facts']['tested']['value'], 'passed')

    def test_lost_scope_response_retry_has_one_effect_and_one_committed_audit(self):
        self.backend.fail_next('lifecycle.scope')
        lost = self.post('/scope', self.scope_body(), 'lost-scope')
        self.assertEqual(lost.status, 503, lost.data)
        version = self.service.state['canonical']['tasks'][self.task]['version']
        retry = self.post('/scope', self.scope_body(), 'lost-scope')
        self.assertEqual(retry.status, 201, retry.data)
        again = self.post('/scope', self.scope_body(), 'lost-scope')
        self.assertEqual(again.status, 201, again.data)
        self.assertEqual(retry.data, again.data)
        self.assertEqual(self.service.state['canonical']['tasks'][self.task]['version'], version)
        committed = [a for a in self.service.state['audit'] if a['action'] == 'lifecycle.scope' and a['outcome'] == 'committed']
        self.assertEqual(len(committed), 1)
        self.assertEqual(committed[0]['operation_id'], retry.data['operation_id'])
        self.assertEqual(committed[0]['event_id'], retry.data['event_id'])

    def test_full_route_target_separates_same_key_and_changed_body_is_refused(self):
        self.set_scope()
        first = self.post('/facts/implemented', self.fact_body(), 'shared')
        second = self.post('/facts/tested', self.fact_body(), 'shared')
        self.assertEqual((first.status, second.status), (201, 201))
        self.assertNotEqual(first.data['operation_id'], second.data['operation_id'])
        changed = self.post('/facts/tested', self.fact_body('failed'), 'shared')
        self.assertEqual(changed.status, 409, changed.data)
        self.assertEqual(self.state()['facts']['tested']['value'], 'passed')

    def test_closed_fields_trigger_and_idempotency_requirements_leave_state_unchanged(self):
        before = copy.deepcopy(self.service.state['canonical']['tasks'][self.task])
        for field in ('actor', 'operation', 'task', 'dimension', 'authority', 'route', 'capability', 'verified', 'defect_task'):
            with self.subTest(field=field):
                bad = self.post('/scope', dict(self.scope_body(), **{field: True}), 'extra-' + field)
                self.assertEqual(bad.status, 422, bad.data)
        missing_key = self.post('/scope', self.scope_body(), None)
        self.assertEqual(missing_key.status, 422, missing_key.data)
        self.assertEqual(self.service.state['canonical']['tasks'][self.task], before)
        self.set_scope()
        trigger = self.post('/facts/tested', dict(self.fact_body(), trigger='because'), 'bad-trigger')
        self.assertEqual(trigger.status, 422, trigger.data)
        pending = self.post('/facts/tested', dict(self.fact_body('pending'), trigger='Waiting for test evidence'), 'pending')
        self.assertEqual(pending.status, 201, pending.data)
        self.assertEqual(self.state()['facts']['tested']['trigger'], 'Waiting for test evidence')

    def test_disposable_backend_cannot_issue_host_integrations_or_marks(self):
        self.set_scope()
        before = copy.deepcopy(self.service.state['canonical']['tasks'][self.task])
        for dimension in ('reviewed', 'integrated'):
            for value in ('unknown', 'pending', 'passed', 'failed', 'not-applicable'):
                with self.subTest(dimension=dimension, value=value):
                    refused = self.post('/facts/' + dimension, self.fact_body(value), dimension + '-' + value)
                    self.assertEqual(refused.status, 503, refused.data)
                    self.assertIn('host backend', refused.data['error']['message'])
        self.assertEqual(self.service.state['canonical']['tasks'][self.task], before)
        read = self.state()
        self.assertFalse(read['mark_writer_enabled'])
        self.assertFalse(read['affordances']['integrated']['passed']['allowed'])

    def test_current_coordinate_role_and_project_isolation_apply_before_write(self):
        member = self.create_account(self.admin, 'member', 'member-password-1')
        token = self.login('member', 'member-password-1')[0]
        membership = '/v1/projects/' + self.project + '/members/' + member
        for role in ('contributor', 'viewer'):
            self.assertEqual(self.request('PUT', membership, dict(role=role), token=self.admin).status, 200)
            self.assertFalse(self.state(token)['scope_write']['allowed'])
            denied = self.post('/scope', self.scope_body(), 'denied-' + role, token)
            self.assertEqual(denied.status, 403, denied.data)
        self.assertEqual(self.request('PUT', membership, dict(role='coordinator'), token=self.admin).status, 200)
        permitted = self.post('/scope', self.scope_body(), 'coordinate', token)
        self.assertEqual(permitted.status, 201, permitted.data)
        self.assertEqual(self.request('PUT', membership, dict(role='viewer'), token=self.admin).status, 200)
        revoked_retry = self.post('/scope', self.scope_body(), 'coordinate', token)
        self.assertEqual(revoked_retry.status, 403, revoked_retry.data)
        other = self.create_project(self.admin, 'Other project')
        foreign = self.request('POST', '/v1/projects/' + other + '/tasks/' + self.task + '/lifecycle/scope',
                               self.scope_body(), token=self.admin, key='lifecycle-foreign')
        self.assertEqual(foreign.status, 404, foreign.data)


if __name__ == '__main__':
    unittest.main()
