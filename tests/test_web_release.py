"""Release payload adapter and the release-grant capability matrix (kittrial-5bb.378)."""
import json
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import http_authority as authority
import lifecycle
import web_release
from requirements import canonical_bytes

SCOPE = dict(source_commit='a' * 40, integration_commit='b' * 40,
             release_id='', environment='')
RELEASE_SCOPE = dict(source_commit='', integration_commit='c' * 40,
                     release_id='synthetic-release', environment='synthetic-env')
TASK = 'trial-release-task'


def deploy_body(**changes):
    body = dict(scope=dict(RELEASE_SCOPE),
                evidence=['build:synthetic'], provenance='performed', live_verified=False,
                targets=[dict(task=TASK, source_commit='a' * 40, integration_commit='b' * 40)])
    body.update(changes)
    return body


def payload(dimension, value, scope, operation='op-release'):
    return dict(schema_version=1, operation_id=operation, task=TASK, dimension=dimension,
                value=value, scope=dict(scope), evidence=['synthetic evidence'],
                provenance='performed', actor='ops')


def native_row(payload_doc, order=1):
    """The export shape a native set-state event has, with a trusted canonical payload."""
    text = canonical_bytes(payload_doc).decode()
    event = lifecycle._native_event_row(TASK + '.' + str(order), TASK, payload_doc['dimension'],
                                       payload_doc['value'], lifecycle.PREFIX + text, 'ops')
    event['created_at'] = '2026-10-01T00:00:%02dZ' % order
    return event


def task_row(**fields):
    row = {'id': TASK, 'issue_type': 'task', 'title': 'Release target', 'description': '',
           'status': 'open', 'created_by': 'ops', 'created_at': '2026-10-01T00:00:00Z',
           'labels': [], 'dependencies': []}
    row.update(fields)
    return row


class PayloadTests(unittest.TestCase):
    def test_a_deploy_builds_the_unchanged_native_payload(self):
        operation, native = web_release.validate(dict(deploy_body(), operation='deploy'),
                                                 'ops', 'release-op-1')
        self.assertEqual(operation, 'deploy')
        self.assertEqual(native['dimension'], lifecycle.RELEASE)
        self.assertEqual(native['value'], 'passed')
        self.assertEqual(native['actor'], 'ops')
        self.assertEqual(native['operation_id'], 'release-op-1')
        lifecycle.validate_release_payload(native)

    def test_a_query_builds_the_read_only_native_payload(self):
        operation, native = web_release.validate(dict(scope=dict(RELEASE_SCOPE), operation='query'),
                                                 'ops')
        self.assertEqual(operation, 'query')
        self.assertEqual(native['dimension'], lifecycle.RELEASE_QUERY)
        self.assertEqual(native['targets'], [])
        lifecycle.validate_query_payload(native)

    def test_server_supplied_fields_are_refused(self):
        for field in ('actor', 'operation_id', 'schema_version', 'dimension', 'value'):
            with self.subTest(field=field):
                with self.assertRaises(web_release.ReleaseError) as refused:
                    web_release.fields(dict(deploy_body(), operation='deploy', **{field: 'x'}))
                self.assertEqual(refused.exception.status, 422)
                self.assertIn(field, str(refused.exception))

    def test_a_deploy_needs_its_operation_identity(self):
        with self.assertRaises(web_release.ReleaseError) as refused:
            web_release.validate(dict(deploy_body(), operation='deploy'), 'ops', None)
        self.assertEqual(refused.exception.status, 422)

    def test_more_than_two_hundred_targets_is_refused_before_any_write(self):
        targets = [dict(task='t%d' % i, source_commit='a' * 40, integration_commit='b' * 40)
                   for i in range(201)]
        with self.assertRaises(web_release.ReleaseError) as refused:
            web_release.fields(dict(deploy_body(targets=targets), operation='deploy'))
        self.assertEqual(refused.exception.status, 422)
        self.assertIn('200', str(refused.exception))

    def test_a_target_names_full_commits(self):
        body = deploy_body(targets=[dict(task=TASK, source_commit='A' * 40,
                                         integration_commit='b' * 40)])
        with self.assertRaises(web_release.ReleaseError) as refused:
            web_release.fields(dict(body, operation='deploy'))
        self.assertEqual(refused.exception.status, 422)


class TargetResolutionTests(unittest.TestCase):
    def setUp(self):
        from requirements import content_hash as _content_hash
        token = _content_hash(SCOPE)
        self.scope_event = payload('lifecycle-scope', token, SCOPE)
        self.integrated = payload('integrated', 'passed', SCOPE)
        self.rows = [task_row(labels=['lifecycle-scope:' + token, 'integrated:passed']),
                     native_row(self.scope_event, 1), native_row(self.integrated, 2)]

    def test_a_target_integrated_at_its_commits_is_resolved(self):
        resolved, skipped = web_release.resolve_targets(
            self.rows, [dict(task=TASK, source_commit='a' * 40, integration_commit='b' * 40)])
        self.assertEqual([target['task'] for target in resolved], [TASK])
        self.assertEqual(skipped, [])

    def test_a_target_without_that_integration_is_skipped_with_its_reason(self):
        for target in (dict(task=TASK, source_commit='d' * 40, integration_commit='b' * 40),
                       dict(task=TASK, source_commit='a' * 40, integration_commit='e' * 40)):
            with self.subTest(target=target):
                resolved, skipped = web_release.resolve_targets(self.rows, [target])
                self.assertEqual(resolved, [])
                self.assertEqual(skipped, [{'task': TASK,
                                            'reason': web_release.SKIPPED_NOT_INTEGRATED}])

    def test_an_unknown_or_malformed_task_is_skipped_with_its_own_reason(self):
        target = dict(task='trial-missing', source_commit='a' * 40, integration_commit='b' * 40)
        _, skipped = web_release.resolve_targets(self.rows, [target])
        self.assertEqual(skipped[0]['reason'], web_release.SKIPPED_UNKNOWN)
        broken = task_row(id='trial-broken', malformed=True, error='bad row')
        _, skipped = web_release.resolve_targets(
            self.rows + [broken],
            [dict(task='trial-broken', source_commit='a' * 40, integration_commit='b' * 40)])
        self.assertEqual(skipped, [{'task': 'trial-broken',
                                    'reason': web_release.SKIPPED_MALFORMED}])

    def test_one_stale_target_does_not_drop_the_good_one(self):
        stale = dict(task=TASK, source_commit='d' * 40, integration_commit='e' * 40)
        good = dict(task=TASK, source_commit='a' * 40, integration_commit='b' * 40)
        resolved, skipped = web_release.resolve_targets(self.rows, [stale, good])
        self.assertEqual([target['source_commit'] for target in resolved], ['a' * 40])
        self.assertEqual(len(skipped), 1)


class ReleaseCapabilityTests(unittest.TestCase):
    def state(self, role='owner', grant=True, superuser=False, disabled=False):
        state = {'users': {'agent-owner': {'id': 'agent-owner', 'superuser': superuser,
                                          'disabled': disabled}},
                 'memberships': {'pp': {'agent-owner': role}},
                 'projects': {'pp': {}},
                 'agents': {'agent-1': {'id': 'agent-1', 'owner': 'agent-owner', 'enabled': True,
                                        'projects': ['pp']}},
                 'credentials': {}, 'release_grants': {}}
        if grant:
            state['release_grants'] = {'pp': {'agent-1': {'granted_by': 'agent-owner',
                                                          'granted_at': '2026-10-22T00:00:00Z'}}}
        return state

    def caps(self, state, agent='agent-1'):
        credential = {'user_id': 'agent-owner', 'agent_id': agent, 'scopes': []}
        return authority.credential_capabilities(state, credential,
                                                 state['users']['agent-owner'], 'pp')

    def test_only_the_owner_role_holds_release_by_default(self):
        self.assertIn(authority.CAP_RELEASE, authority.ROLE_CAPABILITIES['owner'])
        self.assertNotIn(authority.CAP_RELEASE, authority.ROLE_CAPABILITIES['coordinator'])
        self.assertIn(authority.CAP_RELEASE, authority.CREDENTIAL_FORBIDDEN_CAPABILITIES)

    def test_a_granted_agent_whose_account_owns_the_project_holds_release(self):
        self.assertIn(authority.CAP_RELEASE, self.caps(self.state(role='owner', grant=True)))
        # A superuser's agent with the grant also holds it.
        self.assertIn(authority.CAP_RELEASE,
                      self.caps(self.state(role=None, grant=True, superuser=True)))

    def test_the_grant_alone_or_a_coordinator_account_does_not(self):
        self.assertNotIn(authority.CAP_RELEASE, self.caps(self.state(role='coordinator')))
        self.assertNotIn(authority.CAP_RELEASE, self.caps(self.state(role='owner', grant=False)))

    def test_a_worker_credential_never_holds_release(self):
        state = self.state(role='owner', grant=True)
        credential = {'user_id': 'agent-owner', 'agent_id': None, 'scopes': ['tasks', 'reviews']}
        self.assertNotIn(authority.CAP_RELEASE,
                         authority.credential_capabilities(state, credential,
                                                           state['users']['agent-owner'], 'pp'))

    def test_a_damaged_grant_fails_closed(self):
        for grant in ({'granted_at': 'x'}, {'granted_by': 5}, 'yes', []):
            with self.subTest(grant=grant):
                state = self.state(role='owner', grant=False)
                state['release_grants'] = {'pp': {'agent-1': grant}}
                self.assertIsNone(authority.release_grant(state, 'pp', 'agent-1'))
                self.assertNotIn(authority.CAP_RELEASE, self.caps(state))

    def test_the_endpoint_block_table_names_both_release_capabilities(self):
        query = {'action': 'web-release', 'args': [json.dumps({'operation': 'query'})]}
        deploy = {'action': 'web-release', 'args': [json.dumps({'operation': 'deploy'})]}
        self.assertEqual(authority.required_capability(query), authority.CAP_COORDINATE)
        self.assertEqual(authority.required_capability(deploy), authority.CAP_RELEASE)


if __name__ == '__main__':
    unittest.main()
