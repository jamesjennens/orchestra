"""Optional owner flow through real HTTP, EndpointBackend, endpoint and pinned bd/Dolt.

The borrowed fixture starts and stops its own disposable loopback SQL server.
No native adapter is mocked. ORCHESTRA_BD_BIN selects the pinned test binaries.
"""
import json
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT/'tests'))
import test_bd_label_aliases as fixture
import record_json


class NativeOwnerTests(fixture.RealBdLabelAliasTests):
    def rows(self):
        exported = self.bd('export', '--all')
        self.assertEqual(exported.returncode, 0, exported.stderr)
        return record_json.loads_rows(exported.stdout)

    def test_owner_without_operator_grant_accepts_and_preserves_native_history(self):
        import http_service
        import requirement_governance as governance
        import requirement_records as records
        import requirement_owner_records as owner
        import test_http_review_fixes as fixes
        root = self.root
        for key, value in fixture.admin.PROJECT_SETTINGS:
            configured = self.bd('config', 'set', key, value)
            self.assertEqual(configured.returncode, 0, configured.stderr)
        slot = self.bd('show', 'pp-merge-slot', '--json')
        if slot.returncode:
            made = self.bd('create', 'Merge Slot', '--id', 'pp-merge-slot', '--labels', 'gt:slot', '--json')
            self.assertEqual(made.returncode, 0, made.stderr)
        made = self.bd('create', 'Owner job', '--type', 'epic', '--json')
        self.assertEqual(made.returncode, 0, made.stderr)
        job = json.loads(made.stdout)['id']

        class HttpHarness(fixes.Harness):
            def make_backend(self):
                return http_service.EndpointBackend(sys.executable, str(KIT/'endpoint.py'),
                                                    str(root), service=self.service)
        harness = HttpHarness(methodName='runTest'); harness.setUp()
        try:
            admin = harness.admin_token()
            account = harness.create_account(admin, 'owner', 'owner-password-1')
            token = harness.login('owner', 'owner-password-1')[0]
            registered = harness.request('POST', '/v1/projects', {'project_id': 'pp', 'name': 'Owner flow'}, token=admin)
            self.assertEqual(registered.status, 201, registered.data)
            self.assertEqual(harness.request('PUT', '/v1/projects/pp/members/'+account,
                                           {'role': 'owner'}, token=admin).status, 200)
            self.assertNotIn(account, fixture.admin.operators(self.root))
            base = '/v1/projects/pp'
            mode = harness.request('GET', base+'/requirements/governance', token=token)
            self.assertEqual(mode.status, 200, mode.data)
            # The fixture is an existing tracker, not new project provisioning.
            self.assertEqual(mode.data['mode'], 'governed')
            simple = harness.request('PUT', base+'/requirements/governance', {
                'mode': 'simple', 'expected_revision': mode.data['revision'],
                'expected_sha256': mode.data['sha256']}, token=token, key='native-simple-mode')
            self.assertEqual(simple.status, 200, simple.data)
            created = harness.request('POST', base+'/requirements', {
                'kind': 'requirement', 'parent': job, 'title': '<script>Owner text</script>',
                'description': 'An edit box and Accept.'}, token=token, key='native-owner-create')
            self.assertEqual(created.status, 201, created.data)
            first = created.data; route = base+'/requirements/'+first['id']
            expected = lambda item: {'expected_revision': item['revision'], 'expected_sha256': item['sha256']}
            accepted = harness.request('POST', route+'/accept', expected(first), token=token, key='native-owner-accept')
            self.assertEqual(accepted.status, 200, accepted.data)
            self.assertEqual(accepted.data['revision'], 2)
            original = next(r for r in self.rows() if r['id'] == first['id'])
            evidence = owner.existing_acceptances(original)[2]
            self.assertEqual(evidence['account_id'], account)
            decision = next(r for r in self.rows() if r['id'] == evidence['decision']['decision_id'])
            self.assertEqual(decision['issue_type'], 'decision')
            self.assertEqual(decision['created_by'], account)
            self.assertTrue(records.resolved_acceptance(original, records.existing_revisions(original)[2]))
            comments = original['comments']
            positions = lambda prefix: [i for i,c in enumerate(comments) if c['text'].startswith(prefix)]
            self.assertLess(positions(owner.ACCEPTANCE_PREFIX)[0], positions(records.REVISION_PREFIX)[-1])
            replay = harness.request('POST', route+'/accept', expected(first), token=token, key='native-owner-accept')
            self.assertEqual(replay.data, accepted.data)
            self.assertEqual(next(r for r in self.rows() if r['id']==first['id']), original)
            # Both machine kinds remain unavailable to ordinary native contributors.
            for prefix in (owner.ACCEPTANCE_PREFIX, owner.STATE_PREFIX):
                with self.assertRaises(ValueError):
                    fixture.endpoint.execute(root, {'project':'pp','actor':'contributor','action':'bd',
                        'args':['comments','add',first['id'],prefix+'{}','--json']})
            changed = harness.request('PATCH', route, dict(expected(accepted.data), description='New draft.'),
                                      token=token, key='native-owner-edit')
            self.assertEqual(changed.status, 200, changed.data)
            self.assertEqual(changed.data['acceptance_state'], 'draft')
            detail = harness.request('GET', route, token=token)
            self.assertEqual(detail.status, 200, detail.data)
            self.assertEqual([r['acceptance_state'] for r in detail.data['history']], ['draft','accepted','draft'])
            for revision in detail.data['history']:
                from requirements import content_hash
                self.assertEqual(content_hash(revision), revision['sha256'])
            governed = harness.request('PUT', base+'/requirements/governance', dict(expected(simple.data), mode='governed'),
                                       token=token, key='native-governed-mode')
            self.assertEqual(governed.status, 200, governed.data)
            before = self.rows()
            denied = harness.request('POST', route+'/accept', expected(changed.data), token=token, key='native-governed-accept')
            self.assertEqual(denied.status, 422, denied.data)
            self.assertEqual(self.rows(), before)
            self.assertTrue(records.resolved_acceptance(next(r for r in before if r['id']==first['id']),
                                                       detail.data['history'][1]))
            governance.validate_evidence(self.project, 'pp', evidence)
        finally:
            harness._stop_server(); harness.doCleanups()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([NativeOwnerTests('test_owner_without_operator_grant_accepts_and_preserves_native_history')])
