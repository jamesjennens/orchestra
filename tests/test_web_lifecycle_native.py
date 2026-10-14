"""Lifecycle HTTP effects through the actual endpoint and pinned server-mode bd."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers, COMMIT
import test_web_merge_slot as slot_fixture
import lifecycle
import web_lifecycle_marks as marks
from requirements import content_hash


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no pinned native bd (set ORCHESTRA_BD_BIN)')
class NativeLifecycleTests(RulesHelpers, unittest.TestCase):
    web = slot_fixture.NativeWebSlotTests.web

    def setUp(self):
        super().setUp()
        state = json.loads(self.r.bd('merge-slot', 'check', '--json').stdout)
        if not state.get('available'):
            released = self.r.bd('merge-slot', 'release', '--holder', state['holder'], '--json')
            self.assertEqual(released.returncode, 0, released.stderr)

    def capture(self, label):
        rows = self.rows()
        output = os.environ.get('ORCHESTRA_LIFECYCLE_TEST_EXPORTS')
        if output:
            path = Path(output)
            path.mkdir(parents=True, exist_ok=True)
            (path / (label + '.jsonl')).write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return rows

    def test_native_coordinate_roles_grants_and_pre_effect_revocation(self):
        from unittest.mock import patch
        harness, root_token = self.web()
        people = {}
        for role in ('owner', 'coordinator', 'contributor', 'viewer'):
            uid = harness.create_account(root_token, 'lifecycle-' + role, 'lifecycle-password-1')
            result = harness.request('PUT', '/v1/projects/pp/members/' + uid,
                                     {'role': role}, token=root_token)
            self.assertEqual(result.status, 200, result.data)
            people[role] = (uid, harness.login('lifecycle-' + role, 'lifecycle-password-1')[0])
        task = self.task('Native lifecycle authority matrix')
        path = '/v1/projects/pp/tasks/' + task + '/lifecycle'
        scope = dict(source_commit='a' * 40, integration_commit='b' * 40,
                     release_id='synthetic-auth-release', environment='synthetic-native-authority')
        body = dict(scope=scope, expected_scope_token=None,
                    evidence=['synthetic authority scope'], provenance='performed')
        fact = dict(scope=scope, value='passed', evidence=['synthetic authority fact'], provenance='performed')

        def post(token, suffix, payload, key):
            return harness.request('POST', path + suffix, payload, token=token,
                                   key='native-authority-' + key)

        def refused(token, suffix, payload, key, status=403):
            before = self.capture('authority-before-' + key)
            result = post(token, suffix, payload, key)
            self.assertEqual(result.status, status, result.data)
            self.assertEqual(self.capture('authority-after-' + key), before)
            return result

        worker = harness.request('POST', '/v1/projects/pp/worker-credentials',
                                 dict(scopes=['tasks', 'reviews'], label='lifecycle synthetic worker'), token=root_token)
        self.assertEqual(worker.status, 201, worker.data)
        for flags in (False, True):
            self.deployment(operators=['ops'], lifecycle_authors=flags,
                            coordinator_writes=flags, independent_approval=flags)
            for role in ('contributor', 'viewer'):
                refused(people[role][1], '/scope', body, role + '-' + str(flags))
            refused(worker.data['credential']['secret'], '/scope', body, 'worker-' + str(flags))
        made = post(root_token, '/scope', body, 'superuser-scope')
        self.assertEqual(made.status, 201, made.data)
        for role in ('owner', 'coordinator'):
            written = post(people[role][1], '/facts/tested', fact, role + '-fact')
            self.assertEqual(written.status, 201, written.data)
        coordinator_id, coordinator = people['coordinator']
        agent = harness.request('POST', '/v1/agents',
                                dict(name='Lifecycle authority agent', working_directory='/tmp/synthetic-lifecycle-agent',
                                     projects=['pp']), token=coordinator)
        self.assertEqual(agent.status, 201, agent.data)
        aid = agent.data['agent']['id'];credential = agent.data['credential']
        secret = credential['secret']
        refused(secret, '/facts/tested', fact, 'ungranted-agent')
        grant_path = '/v1/projects/pp/agents/' + aid + '/coordinator'
        self.assertEqual(harness.request('PUT', grant_path, {}, token=root_token).status, 200)
        passed = post(secret, '/facts/tested', fact, 'granted-agent')
        self.assertEqual(passed.status, 201, passed.data)
        self.assertEqual(passed.data['actor'], agent.data['agent']['actor'])
        self.assertEqual(harness.request('DELETE', grant_path, token=root_token).status, 200)
        refused(secret, '/facts/tested', fact, 'granted-agent')
        self.assertEqual(harness.request('PUT', grant_path, {}, token=root_token).status, 200)
        changed = harness.request('PUT', '/v1/projects/pp/members/' + coordinator_id,
                                  {'role': 'contributor'}, token=root_token)
        self.assertEqual(changed.status, 200, changed.data)
        refused(secret, '/facts/tested', fact, 'demoted-agent-owner')
        changed = harness.request('PUT', '/v1/projects/pp/members/' + coordinator_id,
                                  {'role': 'coordinator'}, token=root_token)
        self.assertEqual(changed.status, 200, changed.data)
        # Commit revocation after the route's permission check but before the real
        # endpoint subprocess starts: it must re-read the configured auth store.
        original = harness.backend._ask
        injected = [False]
        def revoked_before_endpoint(*args, **kwargs):
            action = args[0] if args else kwargs.get('action')
            if action == 'web-lifecycle' and not injected[0]:
                injected[0] = True
                with harness.store.lock:
                    harness.service.state['credentials'][credential['id']]['revoked'] = True
                    harness.store.save()
            return original(*args, **kwargs)
        with patch.object(harness.backend, '_ask', side_effect=revoked_before_endpoint):
            refused(secret, '/facts/tested', fact, 'revoked-at-endpoint', status=401)
        self.assertTrue(injected[0])
        # A different live credential provides the positive control, then expiry
        # and agent disablement independently stop even an identical cached key.
        fresh = harness.request('POST', '/v1/agents/' + aid + '/credentials', {}, token=coordinator)
        self.assertEqual(fresh.status, 201, fresh.data)
        fresh_secret = fresh.data['credential']['secret']
        written = post(fresh_secret, '/facts/implemented', fact, 'fresh-agent')
        self.assertEqual(written.status, 201, written.data)
        with harness.store.lock:
            harness.service.state['credentials'][fresh.data['credential']['id']]['expires_at'] = 0
            harness.store.save()
        refused(fresh_secret, '/facts/implemented', fact, 'fresh-agent', status=401)
        last = harness.request('POST', '/v1/agents/' + aid + '/credentials', {}, token=coordinator)
        self.assertEqual(last.status, 201, last.data)
        self.assertEqual(post(last.data['credential']['secret'], '/facts/tested', fact, 'last-agent').status, 201)
        disabled = harness.request('POST', '/v1/agents/' + aid + '/disable', token=coordinator)
        self.assertEqual(disabled.status, 200, disabled.data)
        refused(last.data['credential']['secret'], '/facts/tested', fact, 'last-agent', status=401)

    def test_native_scope_six_facts_refusals_retry_and_bound_backup(self):
        self.deployment(operators=['ops'])
        harness, owner = self.web()
        uid = harness.create_account(owner, 'lifecycle-coordinator', 'lifecycle-password-1')
        membership = harness.request('PUT', '/v1/projects/pp/members/' + uid,
                                     {'role': 'coordinator'}, token=owner)
        self.assertEqual(membership.status, 200, membership.data)
        token = harness.login('lifecycle-coordinator', 'lifecycle-password-1')[0]
        task = self.task('Native HTTP lifecycle', assignee='other-author')
        made = self.contribute(task, 'other-author')
        self.assertEqual(made['returncode'], 0, made)
        contribution = json.loads(made['stdout'])['comment_id']
        approved = self.approve(task, 'ops', contribution)
        self.assertEqual(approved['returncode'], 0, approved)
        path = '/v1/projects/pp/tasks/' + task + '/lifecycle'
        scope = dict(source_commit=COMMIT, integration_commit='e' * 40,
                     release_id='synthetic-native-release', environment='native-synthetic')
        scope_body = dict(scope=scope, expected_scope_token=None,
                          evidence=['synthetic native scope'], provenance='performed')
        fact = dict(scope=scope, value='passed', evidence=['synthetic native fact'], provenance='performed')

        def post(suffix, body, key, bearer=token):
            return harness.request('POST', path + suffix, body, token=bearer,
                                   key='native-lifecycle-' + key)

        def refused(suffix, body, key, status, bearer=token):
            before = self.capture('before-' + key)
            result = post(suffix, body, key, bearer)
            self.assertEqual(result.status, status, result.data)
            self.assertEqual(self.capture('after-' + key), before)
            return result

        read = harness.request('GET', path, token=token)
        self.assertEqual(read.status, 200, read.data)
        self.assertIsNone(read.data['scope_token'])
        self.assertEqual(read.data['current_actor'], uid)
        harness.backend.fail_next('lifecycle.scope')
        lost = post('/scope', scope_body, 'scope-lost')
        self.assertEqual(lost.status, 503, lost.data)
        rows = self.capture('scope-lost-committed')
        retry = post('/scope', scope_body, 'scope-lost')
        self.assertEqual(retry.status, 201, retry.data)
        self.assertEqual(self.capture('scope-retry'), rows)
        refused('/scope', dict(scope_body, scope=dict(scope, environment='stale')), 'stale', 409)
        refused('/facts/tested', dict(fact, scope=dict(scope, source_commit='f' * 40)), 'stale-fact', 409)
        refused('/facts/tested', dict(fact, actor='ops'), 'actor-field', 422)
        refused('/facts/reviewed', fact, 'without-slot', 409)
        acquired = harness.request('POST', '/v1/projects/pp/merge-slot/acquire',
                                   dict(task_id=task, target='main'), token=token,
                                   key='native-lifecycle-slot')
        self.assertEqual(acquired.status, 200, acquired.data)
        off = refused('/facts/integrated', fact, 'writer-off', 503)
        self.assertIn('writer is OFF', off.data['error']['message'])
        self.assertFalse((self.project / marks.DIRECTORY).exists())
        self.deployment(operators=['ops'], web_lifecycle_mark_writes=True)
        for dimension in lifecycle.DIMENSIONS:
            reply = post('/facts/' + dimension, fact, dimension)
            self.assertEqual(reply.status, 201, reply.data)
        rows = self.capture('six-facts')
        read = harness.request('GET', path, token=token)
        self.assertEqual(read.status, 200, read.data)
        self.assertTrue(all(read.data['facts'][dimension]['value'] == 'passed'
                            for dimension in lifecycle.DIMENSIONS))
        journal = marks.read_journal(self.project)
        self.assertEqual(journal['invalid'], [])
        self.assertEqual(len(journal['entries']), 1)
        entry = next(iter(journal['entries'].values()))
        event = next(row for row in rows if row['id'] == entry['record']['event_id'])
        self.assertTrue(marks.matches(entry, 'pp', event))
        self.assertEqual(entry['record']['created_by'], uid)
        native = lifecycle.native_event(event)['payload']
        self.assertEqual(set(native), lifecycle.PAYLOAD_FIELDS)
        self.assertEqual(native['actor'], uid)
        bound = fixture.admin.backup_lifecycle_void_family(self.root, 'pp')
        self.assertIn(marks.MANIFEST, bound)
        foreign = self.root / 'projects' / 'foreign'
        foreign.mkdir()
        marks.restore_files(foreign, bound, rows)
        self.assertFalse(marks.recorder_eligible(event, 'foreign',
                                               journal=marks.read_journal(foreign), operators=[], owners={}))
        self.deployment(operators=[], web_lifecycle_mark_writes=False)
        self.assertTrue(marks.recorder_eligible(event, 'pp',
                                              journal=marks.read_journal(self.project), operators=[], owners={}))
        # A cached success cannot outlive current coordinate permission.
        demoted = harness.request('PUT', '/v1/projects/pp/members/' + uid,
                                  {'role': 'contributor'}, token=owner)
        self.assertEqual(demoted.status, 200, demoted.data)
        refused('/facts/tested', fact, 'tested', 403)
        audits = [entry for entry in harness.service.state['audit']
                  if entry['action'] == 'lifecycle.scope' and entry['outcome'] == 'committed']
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]['event_id'], retry.data['event_id'])


if __name__ == '__main__':
    unittest.main()
