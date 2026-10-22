"""Release query and release deploy through the real HTTP service and endpoint.

POSIX + real bd, like ``test_web_lifecycle_native.py``: the endpoint imports fcntl and the
routes write native lifecycle facts. The authority cases that need no host writer live in
``test_web_release_http.py``.
"""
import json
import os
import sys
import unittest
import uuid
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers, SCOPE, COMMIT
import test_web_merge_slot as slot_fixture
import lifecycle


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no pinned native bd (set ORCHESTRA_BD_BIN)')
class NativeReleaseWebTests(RulesHelpers, unittest.TestCase):
    web = slot_fixture.NativeWebSlotTests.web

    # -- helpers ---------------------------------------------------------------
    def integrated(self, title='Native release'):
        """A task with the recorded scope and a passing integration at that scope."""
        task = self.task(title + ' ' + uuid.uuid4().hex[:8], assignee='w1')
        self.assertEqual(self.lifecycle('w1', 'lifecycle-scope', task)['returncode'], 0)
        made = self.lifecycle('ops', 'integrated', task)
        self.assertEqual(made['returncode'], 0, made)
        return task

    def release_scope(self, release='native-release', environment='native-env'):
        return dict(source_commit='', integration_commit='c' * 40,
                    release_id=release, environment=environment)

    def target(self, task, scope=None):
        scope = scope or SCOPE
        return dict(task=task, source_commit=scope['source_commit'],
                    integration_commit=scope['integration_commit'])

    def body(self, tasks, scope=None, **changes):
        body = dict(scope=scope or self.release_scope(),
                    evidence=['synthetic release evidence'], provenance='performed',
                    live_verified=False, targets=[self.target(task) for task in tasks])
        body.update(changes)
        return body

    def deploy(self, harness, token, body, key='release-native-key'):
        return harness.request('POST', '/v1/projects/pp/releases', body, token=token, key=key)

    def query(self, harness, token, environment='native-env'):
        scope = dict(source_commit='', integration_commit='c' * 40,
                     release_id='', environment=environment)
        return harness.request('POST', '/v1/projects/pp/releases/query',
                               {'scope': scope}, token=token, key='release-query-' + environment)

    def lifecycle_state(self, task):
        return next(state for state in lifecycle.project_facts(self.rows()) if state['id'] == task)

    def lifecycle_facts(self, task):
        return self.lifecycle_state(task)['facts']

    def liveness(self, task):
        return self.lifecycle_state(task)['live']

    # -- the write -------------------------------------------------------------
    def test_one_group_deploys_and_a_stale_target_is_skipped_with_its_reason(self):
        self.deployment(operators=['ops'])
        harness, token = self.web()
        good = self.integrated('release good')
        stale = self.task('release stale ' + uuid.uuid4().hex[:8], assignee='w1')
        before = self.rows()
        reply = self.deploy(harness, token, self.body([good, stale]))
        self.assertEqual(reply.status, 201, reply.data)
        self.assertEqual(reply.data['groups_completed'], 1)
        self.assertEqual(reply.data['groups_total'], 1)
        self.assertEqual(reply.data['resolved'], [good])
        self.assertEqual(reply.data['skipped'], [{'task': stale,
                                                  'reason': 'no passing integration at the named source and integration commit'}])
        self.assertEqual([row['task'] for row in reply.data['targets']], [good])
        # The good target carries the release scope and the deployment fact; the stale
        # one is exactly as it was.
        state = self.lifecycle_state(good)
        self.assertEqual(state['scope']['release_id'], 'native-release')
        self.assertEqual(state['scope']['environment'], 'native-env')
        self.assertEqual(state['facts']['deployed']['value'], 'passed')
        self.assertEqual(self.liveness(good), 'live')
        after = self.rows()
        stale_before = [row for row in before if row.get('id', '').startswith(stale)]
        stale_after = [row for row in after if row.get('id', '').startswith(stale)]
        self.assertEqual(stale_after, stale_before)

    def test_an_exact_retry_replays_and_a_changed_body_conflicts(self):
        self.deployment(operators=['ops'])
        harness, token = self.web()
        task = self.integrated('release retry')
        body = self.body([task])
        first = self.deploy(harness, token, body, key='release-retry-1')
        self.assertEqual(first.status, 201, first.data)
        rows = self.rows()
        replay = self.deploy(harness, token, body, key='release-retry-1')
        self.assertEqual(replay.status, 201, replay.data)
        self.assertEqual(replay.data, first.data)
        self.assertEqual(self.rows(), rows)
        changed = self.deploy(harness, token, dict(body, evidence=['other evidence']),
                              key='release-retry-1')
        self.assertEqual(changed.status, 409, changed.data)
        self.assertEqual(self.rows(), rows)

    def test_live_verified_and_supersede_fields_pass_through(self):
        self.deployment(operators=['ops'])
        harness, token = self.web()
        first_task = self.integrated('release verified')
        first = self.deploy(harness, token, self.body([first_task], live_verified=True),
                            key='release-verified-1')
        self.assertEqual(first.status, 201, first.data)
        self.assertEqual(self.lifecycle_facts(first_task)['live-verified']['value'], 'passed')
        second_task = self.integrated('release supersede')
        second = self.deploy(harness, token,
                             dict(scope=self.release_scope('native-release-2'),
                                  evidence=['synthetic second release'], provenance='performed',
                                  live_verified=False, targets=[self.target(second_task)],
                                  supersede=[first_task]),
                             key='release-supersede-1')
        self.assertEqual(second.status, 201, second.data)
        self.assertEqual([row['task'] for row in second.data['superseded']], [first_task])
        self.assertEqual(self.liveness(first_task), 'superseded')
        self.assertEqual(self.liveness(second_task), 'live')

    def test_a_verification_target_binds_to_the_live_scope(self):
        self.deployment(operators=['ops'])
        harness, token = self.web()
        task = self.integrated('release verify only')
        first = self.deploy(harness, token, self.body([task]), key='release-verify-only-1')
        self.assertEqual(first.status, 201, first.data)
        live = self.lifecycle_state(task)['scope']
        verified = self.deploy(harness, token,
                               dict(scope=self.release_scope(), evidence=['synthetic verification'],
                                    provenance='performed', live_verified=True,
                                    targets=[dict(self.target(task), verify_scope=dict(live))]),
                               key='release-verify-only-2')
        self.assertEqual(verified.status, 201, verified.data)
        self.assertTrue(verified.data['targets'][0]['verify_only'])
        self.assertEqual(self.lifecycle_facts(task)['live-verified']['value'], 'passed')
        self.assertEqual(self.lifecycle_state(task)['scope'], live)

    def test_a_held_integration_is_refused_with_the_hosts_sentence(self):
        self.deployment(operators=['ops'], review_workflow_writes=True)
        harness, token = self.web()
        task = self.task('release held ' + uuid.uuid4().hex[:8], assignee='other-author')
        made = self.contribute(task, 'other-author')
        self.assertEqual(made['returncode'], 0, made)
        contribution = json.loads(made['stdout'])['comment_id']
        approved = self.approve(task, 'ops', contribution)
        self.assertEqual(approved['returncode'], 0, approved)
        approval = json.loads(approved['stdout'])['comment_id']
        self.assertEqual(self.lifecycle('other-author', 'lifecycle-scope', task)['returncode'], 0)
        self.assertEqual(self.lifecycle('ops', 'integrated', task)['returncode'], 0)
        held = dict(schema_version=1, operation='hold', operation_id='release-hold-1',
                    task=task, previous=approval, contribution=contribution,
                    reason='Synthetic release hold')
        result = self.post_review(task, 'ops', held)
        self.assertEqual(result['returncode'], 0, result)
        before = self.rows()
        refused = self.deploy(harness, token, self.body([task]), key='release-held-1')
        self.assertEqual(refused.status, 422, refused.data)
        message = refused.data['error']['message']
        self.assertIn('held', message.lower())
        self.assertIn(task, message)
        self.assertEqual(self.rows(), before)

    def test_lifecycle_authors_refuses_a_non_listed_recorder_as_the_host_does(self):
        self.deployment(operators=['ops'], lifecycle_authors=True)
        harness, token = self.web()
        task = self.integrated('release authors')
        before = self.rows()
        refused = self.deploy(harness, token, self.body([task]), key='release-authors-1')
        self.assertEqual(refused.status, 422, refused.data)
        message = refused.data['error']['message']
        self.assertIn('lifecycle_authors', message)
        self.assertIn('release-deploy', message)
        self.assertEqual(self.rows(), before)

    def test_the_query_writes_nothing_and_reports_the_live_release(self):
        self.deployment(operators=['ops'])
        harness, token = self.web()
        task = self.integrated('release query')
        self.assertEqual(self.deploy(harness, token, self.body([task]), key='release-query-1').status,
                         201)
        before = self.rows()
        reply = self.query(harness, token)
        self.assertEqual(reply.status, 200, reply.data)
        self.assertTrue(reply.data['query'])
        self.assertEqual(reply.data['environment'], 'native-env')
        self.assertEqual(reply.data['reverted'], [])
        self.assertIn({'task': task, 'release_id': 'native-release',
                       'integration_commit': 'b' * 40, 'liveness': 'live'},
                      reply.data['live_releases'])
        self.assertEqual(self.rows(), before)

    def test_the_release_grant_is_checked_live_by_the_endpoint(self):
        self.deployment(operators=['ops'])
        harness, token = self.web()
        task = self.integrated('release grant live')
        owner_id = harness.create_account(token, 'release-live-owner', 'release-live-password-1')
        self.assertEqual(harness.request('PUT', '/v1/projects/pp/members/' + owner_id,
                                         {'role': 'owner'}, token=token).status, 200)
        owner = harness.login('release-live-owner', 'release-live-password-1')[0]
        agent = harness.request('POST', '/v1/agents',
                                dict(name='Release live agent', working_directory='/tmp/release-live',
                                     projects=['pp']), token=owner)
        self.assertEqual(agent.status, 201, agent.data)
        agent_id = agent.data['agent']['id']
        secret = agent.data['credential']['secret']
        # An ungranted agent credential is refused although its account owns the project.
        refused = self.deploy(harness, secret, self.body([task]), key='release-live-ungranted')
        self.assertEqual(refused.status, 403, refused.data)
        grant = harness.request('PUT', '/v1/projects/pp/agents/' + agent_id + '/release', {}, token=owner)
        self.assertEqual(grant.status, 200, grant.data)
        self.assertTrue(grant.data['release']['effective'])
        made = self.deploy(harness, secret, self.body([task]), key='release-live-agent')
        self.assertEqual(made.status, 201, made.data)
        self.assertEqual(made.data['targets'][0]['task'], task)
        # The endpoint reads the live store: demoting the account ends the grant's effect.
        self.assertEqual(harness.request('PUT', '/v1/projects/pp/members/' + owner_id,
                                         {'role': 'coordinator'}, token=token).status, 200)
        refused = self.deploy(harness, secret, self.body([task], scope=self.release_scope('demoted')),
                              key='release-live-demoted')
        self.assertEqual(refused.status, 403, refused.data)
        self.assertEqual(harness.request('PUT', '/v1/projects/pp/members/' + owner_id,
                                         {'role': 'owner'}, token=token).status, 200)
        restored = self.deploy(harness, secret, self.body([task], scope=self.release_scope('restored')),
                               key='release-live-restored')
        self.assertEqual(restored.status, 201, restored.data)
        # The very next request after a revoke is refused.
        self.assertEqual(harness.request('DELETE', '/v1/projects/pp/agents/' + agent_id + '/release',
                                         token=owner).status, 200)
        refused = self.deploy(harness, secret, self.body([task], scope=self.release_scope('other')),
                              key='release-live-revoked')
        self.assertEqual(refused.status, 403, refused.data)

    def test_a_group_over_two_hundred_targets_is_refused_with_nothing_written(self):
        self.deployment(operators=['ops'])
        harness, token = self.web()
        targets = [dict(task='release-fake-%d' % index, source_commit='a' * 40,
                        integration_commit='b' * 40) for index in range(201)]
        before = self.rows()
        refused = self.deploy(harness, token, self.body([], targets=targets), key='release-201')
        self.assertEqual(refused.status, 422, refused.data)
        self.assertEqual(self.rows(), before)


if __name__ == '__main__':
    unittest.main()
