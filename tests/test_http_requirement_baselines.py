"""Real loopback HTTP and canonical journal; native row access is a test seam."""
import copy
import json
from pathlib import Path
from unittest import mock

import requirement_baselines as store
import requirement_governance as governance
import requirement_http as requirements
from requirements import content_hash
from test_http_review_fixes import EndpointCase
import test_http_requirements_owner as owner_fixture
from test_requirement_baseline_records import CompleteNative


class BaselineHttpTests(EndpointCase):
    make_backend = owner_fixture.OwnerHttpTests.make_backend
    create_requirement = owner_fixture.OwnerHttpTests.create_requirement
    expected = staticmethod(owner_fixture.OwnerHttpTests.expected)

    def setUp(self):
        super().setUp()
        self.addCleanup(self._stop_server)
        self.backend.native = CompleteNative()
        self.owner_token, self.project = self.setup_project()
        self.account = self.service.authenticate(self.owner_token).user_id
        self.path = self.canonical_root / 'projects' / self.project
        governance.initialize(self.path, self.project, self.account, 'creation-alpha')
        self.base = '/v1/projects/' + self.project
        self.collection = self.base + '/requirements/baselines'

    def cut(self, name='Review-1', key='cut-one', token=None):
        return self.request('POST', self.collection, {'name': name},
                            token=token or self.owner_token, key='baseline-' + key)

    def frozen(self, name='review-1'):
        return {filename: (self.path / store.DIRECTORY / name / filename).read_bytes()
                for filename in store.FILES}

    def test_empty_collection_no_baseline_and_current_markdown_are_explicit(self):
        listing = self.request('GET', self.collection, token=self.owner_token)
        self.assertEqual(listing.status, 200, listing.data)
        self.assertEqual(listing.data['items'], [])
        result = self.request('GET', self.base + '/brd/changes', token=self.owner_token)
        self.assertFalse(result.data['available'])
        self.assertIsNone(result.data['baseline'])
        document = self.request('GET', self.base + '/brd/export', token=self.owner_token)
        self.assertEqual(document.status, 200, document.data)
        self.assertIn('No requirements yet.', document.data['markdown'])
        self.assertFalse((self.path / store.DIRECTORY).exists())

    def test_pending_and_accepted_remain_separate_and_frozen_export_ignores_later_edits(self):
        first = self.create_requirement()
        result = self.cut(); self.assertEqual(result.status, 201, result.data)
        before = self.frozen()
        exported = self.request('GET', self.base + '/brd/export?baseline=REVIEW-1', token=self.owner_token)
        self.assertEqual(exported.status, 200, exported.data)
        self.assertIn('Pending - not accepted', exported.data['markdown'])
        self.assertEqual(exported.data['markdown'].encode(), before['BRD.md'])
        self.assertEqual(exported.data['sha256'], __import__('hashlib').sha256(before['BRD.md']).hexdigest())
        accepted = self.request('POST', self.base + '/requirements/' + first['id'] + '/accept',
            self.expected(first), token=self.owner_token, key='accept-first')
        self.assertEqual(accepted.status, 200, accepted.data)
        changes = self.request('GET', self.base + '/brd/changes?baseline=review-1', token=self.owner_token)
        self.assertEqual(changes.status, 200, changes.data)
        self.assertEqual(changes.data['groups']['accepted'], [first['id']])
        self.assertEqual(changes.data['groups']['pending'], [first['id']])
        self.assertEqual(self.frozen(), before)
        self.assertEqual(self.request('GET', self.collection + '/review-1', token=self.owner_token).status, 200)
        self.assertFalse(any(call['action'] != 'owner-requirements' for call in self.backend.sent if call['args'] == ['baseline']))

    def test_exact_new_session_replay_preserves_all_bytes_and_live_owner_is_rechecked(self):
        self.create_requirement(); first = self.cut(); self.assertEqual(first.status, 201, first.data)
        before = self.frozen(); count = len(self.backend.native.calls)
        token = self.login('alex', 'alex-password-1')[0]
        replay = self.cut(token=token)
        self.assertEqual(replay.status, 201, replay.data)
        self.assertEqual(replay.data, first.data)
        self.assertEqual(len(self.backend.native.calls), count)
        self.assertEqual(self.frozen(), before)
        def demote():
            self.service.state['memberships'][self.project][self.account] = 'contributor'
            self.service.store.save()
        self.backend.before_owner = demote
        refused = self.cut(token=token)
        self.assertIn(refused.status, (403, 422), refused.data)
        self.assertEqual(self.frozen(), before)

    def test_governed_mode_refuses_new_cut_and_cached_cut(self):
        first = self.cut(); self.assertEqual(first.status, 201, first.data)
        before = self.frozen()
        current = governance.current(self.path, self.project)
        governance.set_mode(self.path, self.project, 'governed', self.account, 'governed-mode',
                            current['revision'], current['sha256'])
        for name, key in (('Review-1', 'cut-one'), ('new', 'new-cut')):
            refused = self.cut(name, key)
            self.assertEqual(refused.status, 422, refused.data)
            self.assertIn('governed', str(refused.data))
        self.assertEqual(self.frozen(), before)
        self.assertEqual(self.request('GET', self.base + '/brd/export?baseline=review-1', token=self.owner_token).status, 200)

    def test_current_human_members_read_but_only_owner_can_cut_and_credentials_cannot_read(self):
        self.assertEqual(self.cut().status, 201)
        admin = self.admin_token()
        for role in ('viewer', 'contributor'):
            account = self.create_account(admin, role, role + '-password-1')
            token = self.login(role, role + '-password-1')[0]
            self.assertEqual(self.request('PUT', self.base + '/members/' + account, {'role': role}, token=admin).status, 200)
            for route in (self.collection, self.collection + '/review-1', self.base + '/brd/export?baseline=review-1', self.base + '/brd/changes'):
                read = self.request('GET', route, token=token)
                self.assertEqual(read.status, 200, read.data)
            self.assertEqual(self.cut(role, 'deny-' + role, token).status, 403)
        credential = self.issue_credential(self.owner_token, self.project, label='worker', scopes=['read', 'tasks'])
        agent = self.request('POST', '/v1/agents', {'name': 'Baseline helper',
            'working_directory': '/tmp/synthetic-worker', 'projects': [self.project]}, token=self.owner_token)
        self.assertEqual(agent.status, 201, agent.data)
        for token in (credential['secret'], agent.data['credential']['secret']):
            self.assertEqual(self.cut('denied', 'deny-credential', token).status, 403)
            for route in (self.collection, self.base + '/brd/export', self.base + '/brd/changes'):
                refused = self.request('GET', route, token=token)
                self.assertEqual(refused.status, 403, refused.data)
        self.assertEqual(store.list_baselines(self.path, self.project)['total'], 1)

    def test_cookie_requires_csrf_and_hostile_body_never_writes(self):
        token, csrf, _ = self.login('alex', 'alex-password-1')
        refused = self.request('POST', self.collection, {'name': 'cookie'}, cookie='orchestra_session=' + token, key='cookie-cut')
        self.assertEqual(refused.status, 403, refused.data)
        accepted = self.request('POST', self.collection, {'name': 'cookie'}, cookie='orchestra_session=' + token, csrf=csrf, key='cookie-cut')
        self.assertEqual(accepted.status, 201, accepted.data)
        for fields in ({'name': '../escape'}, {'name': 'CON'}, {'name': 'new', 'account_id': self.account}, {'name': 'new', 'path': '/private/path'}):
            refused = self.request('POST', self.collection, fields, token=self.owner_token, key='hostile-body')
            self.assertEqual(refused.status, 422, refused.data)
        self.assertEqual(store.list_baselines(self.path, self.project)['total'], 1)

    def test_interrupted_stage_recovers_saved_capture_in_a_new_session_without_new_export(self):
        first = self.create_requirement(); real = store._write_once
        def interrupt(path, raw):
            if path.name == 'BRD.md':
                raise OSError('private-server-path-sentinel /private/server/file')
            return real(path, raw)
        with mock.patch.object(store, '_write_once', interrupt):
            failed = self.cut()
        self.assertEqual(failed.status, 503, failed.data)
        self.assertNotIn('private-server-path-sentinel', json.dumps(failed.data))
        self.assertIsNone(store.current(self.path, self.project))
        stage = next((self.path / store.DIRECTORY).glob('.pending-*'))
        captured = (stage / 'snapshot.json').read_bytes()
        changed = self.request('PATCH', self.base + '/requirements/' + first['id'],
            dict(self.expected(first), description='Wording after capture'), token=self.owner_token, key='later-edit')
        self.assertEqual(changed.status, 200, changed.data)
        exports = sum(call[0] == 'export' for call in self.backend.native.calls)
        token = self.login('alex', 'alex-password-1')[0]
        recovered = self.cut(token=token)
        self.assertEqual(recovered.status, 201, recovered.data)
        self.assertEqual(self.frozen()['snapshot.json'], captured)
        self.assertNotIn(b'Wording after capture', self.frozen()['BRD.md'])
        self.assertEqual(sum(call[0] == 'export' for call in self.backend.native.calls), exports)
        self.assertEqual(self.cut(token=token).data, recovered.data)

    def test_unknown_without_complete_saved_capture_is_never_recaptured(self):
        self.create_requirement()
        with mock.patch.object(store, '_write_once', side_effect=OSError('injected first-file failure')):
            failed = self.cut()
        self.assertEqual(failed.status, 503, failed.data)
        calls = len(self.backend.native.calls)
        retry = self.cut()
        self.assertIn(retry.status, (422, 503), retry.data)
        self.assertEqual(len(self.backend.native.calls), calls)
        self.assertIsNone(store.current(self.path, self.project))
        self.assertFalse((self.path / store.DIRECTORY / 'review-1').exists())

    def test_case_and_operation_conflicts_never_replace_a_cut_or_rewind_current(self):
        first = self.cut(); self.assertEqual(first.status, 201, first.data)
        before = self.frozen()
        self.assertEqual(self.cut('second', 'cut-two').status, 201)
        conflict = self.cut('different', 'cut-one')
        self.assertEqual(conflict.status, 409, conflict.data)
        conflict = self.cut('REVIEW-1', 'new-cut')
        self.assertIn(conflict.status, (409, 422), conflict.data)
        replay = self.cut(); self.assertEqual(replay.status, 201, replay.data)
        self.assertEqual(store.current(self.path, self.project)['name'], 'second')
        self.assertEqual(self.frozen(), before)

    def test_missing_or_damaged_selected_baseline_never_substitutes_current(self):
        self.assertEqual(self.cut().status, 201)
        self.assertEqual(self.cut('healthy', 'cut-healthy').status, 201)
        path = self.path / store.DIRECTORY / 'review-1' / 'BRD.md'
        path.write_text('damaged', encoding='utf-8')
        for suffix in ('/brd/export?baseline=review-1', '/brd/changes?baseline=missing'):
            failed = self.request('GET', self.base + suffix, token=self.owner_token)
            self.assertNotEqual(failed.status, 200, failed.data)
            self.assertNotIn(str(self.path), json.dumps(failed.data))
        healthy = self.request('GET', self.base + '/brd/export?baseline=healthy', token=self.owner_token)
        self.assertEqual(healthy.status, 200, healthy.data)
        listing = self.request('GET', self.collection, token=self.owner_token)
        self.assertFalse(next(row for row in listing.data['items'] if row['name'] == 'review-1')['readable'])
        self.assertTrue(next(row for row in listing.data['items'] if row['name'] == 'healthy')['readable'])

    def test_restore_comparison_bridges_a_copy_after_binding_validation(self):
        self.create_requirement(); self.assertEqual(self.cut().status, 201)
        original = self.frozen()
        files = store.backup_files(self.path, self.project)
        restored = store.restored_files(files, self.project, 'destination')
        target = self.path.parent / 'destination'; target.mkdir()
        for name, value in restored.items():
            path = target / name; path.parent.mkdir(parents=True, exist_ok=True)
            if name.endswith('/BRD.md'):
                path.write_bytes(value['text'].encode())
            else:
                path.write_bytes(__import__('requirement_baseline_records').json_bytes(value))
        source_view = store.get(target, 'destination', 'review-1')['snapshot']['view']
        current = copy.deepcopy(source_view); current['project'] = 'destination'
        import requirement_baseline_records as schema
        with mock.patch.object(schema, 'capture', return_value={'view': current}):
            answer = requirements.read(target, 'destination', ['changes', 'review-1'], self.backend.native)
        self.assertEqual(answer['total'], 0)
        self.assertEqual(answer['source_project'], self.project)
        self.assertEqual(source_view['project'], self.project)
        self.assertEqual(self.frozen(), original)
        (target / store.DIRECTORY / 'source.json').write_text('{}')
        with mock.patch.object(schema, 'capture') as capture, self.assertRaises(ValueError):
            requirements.read(target, 'destination', ['changes', 'review-1'], self.backend.native)
        capture.assert_not_called()

    def test_actual_node_page_and_saved_intents_against_loopback_http(self):
        import shutil
        from test_http_web import run_node_module
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for baseline page behavior checks')
        first = self.create_requirement()
        admin = self.admin_token(); account = self.create_account(admin, 'viewer', 'viewer-password-1')
        viewer = self.login('viewer', 'viewer-password-1')[0]
        self.assertEqual(self.request('PUT', self.base + '/members/' + account, {'role': 'viewer'}, token=admin).status, 200)
        root = Path(__file__).resolve().parents[1]
        output = run_node_module(self, node, 'await import(process.argv[1])',
            (root / 'tests/web_requirement_baselines.mjs').as_uri(),
            (root / 'web/js/api.js').as_uri(), (root / 'web/js/views/requirement_baselines.js').as_uri(),
            (root / 'web/js/views/owner_requirements.js').as_uri(), 'http://127.0.0.1:' + str(self.port),
            self.project, self.owner_token, viewer, first['id'])
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertTrue(json.loads(output.stdout)['lostResponseReloadRetry'])
