"""Real loopback HTTP and endpoint adapter model; pinned native proof is separate."""
import json
import shutil
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requirement_governance as governance
import requirement_http as requirements
import requirement_records as records
import requirement_owner_records as owner_records
from http_authority import AuthorityConfig, NativeRunner, journal_path, run_guarded
from http_service import EndpointBackend
from test_http_review_fixes import EndpointCase, STUB
from test_requirement_owner_records import OwnerNative


class OwnerBackend(EndpointBackend):
    """Uses the shipped adapter/authority journal; only native bd is modeled."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.native = OwnerNative()
        self.before_owner = None
        self.sent = []

    def _endpoint(self, action, project, actor, args, attachments=None, operation_id=None,
                  authority=None, require_authority=False, route=None, **extra):
        if action not in ('requirements', 'owner-requirements'):
            return super()._endpoint(action, project, actor, args, attachments, operation_id,
                                     authority, require_authority, route, **extra)
        self.sent.append(dict(action=action, project=project, actor=actor, args=args,
                              authority=authority, require_authority=require_authority))
        path = Path(self.root) / 'projects' / project
        try:
            if action == 'requirements':
                return {'returncode': 0, 'stdout': json.dumps(requirements.read(
                    path, project, args, self.native)), 'stderr': ''}
            self.service.store.save()
            if self.before_owner:
                hook = self.before_owner; self.before_owner = None; hook()
            self.native.actor = actor
            runner = NativeRunner(self.native)
            request = dict(project=project, actor=actor, action=action, args=args,
                           attachments=attachments, operation_id=operation_id, authority=authority)
            if route and operation_id:
                request['route'] = route
            config = AuthorityConfig(str(self.service.store.path))
            def guarded(root, built, journal, effect, **options):
                return run_guarded(built, journal, effect, **options)
            return requirements.web_action(Path(self.root), path, project, request,
                                           config, runner, guarded)
        except ValueError as error:
            return {'returncode': 2, 'stdout': '', 'stderr': str(error)}


class OwnerHttpTests(EndpointCase):
    def make_backend(self):
        self.canonical_root = self.tmp / 'canonical'
        return OwnerBackend(sys.executable, str(STUB), str(self.canonical_root), service=self.service)

    def setUp(self):
        super().setUp()
        self.addCleanup(self._stop_server)
        self.owner_token, self.project = self.setup_project()
        self.account = self.service.authenticate(self.owner_token).user_id
        self.path = self.canonical_root / 'projects' / self.project
        governance.initialize(self.path, self.project, self.account, 'creation-alpha')
        self.base = '/v1/projects/' + self.project

    def create_requirement(self, key='new-req-1'):
        result = self.request('POST', self.base + '/requirements', {
            'kind': 'requirement', 'parent': 'job-1', 'title': '<script>title</script>',
            'description': 'Owner content \u2028 remains text.'}, token=self.owner_token, key=key)
        self.assertEqual(result.status, 201, result.data)
        return result.data

    @staticmethod
    def expected(item):
        return dict(expected_revision=item['revision'], expected_sha256=item['sha256'])

    def test_owner_create_accept_edit_and_member_read_only_document(self):
        first = self.create_requirement()
        route = self.base + '/requirements/' + first['id']
        accepted = self.request('POST', route + '/accept', self.expected(first),
                                token=self.owner_token, key='accept-req')
        self.assertEqual(accepted.status, 200, accepted.data)
        self.assertEqual(accepted.data['acceptance_state'], 'accepted')
        before = len(self.backend.native.writes())
        retry = self.request('POST', route + '/accept', self.expected(first),
                             token=self.owner_token, key='accept-req')
        self.assertEqual(retry.data, accepted.data)
        self.assertEqual(len(self.backend.native.writes()), before)
        edited = self.request('PATCH', route, dict(self.expected(accepted.data), description='New draft'),
                              token=self.owner_token, key='edit-req')
        self.assertEqual(edited.status, 200, edited.data)
        self.assertEqual(edited.data['acceptance_state'], 'draft')
        admin = self.admin_token()
        user = self.create_account(admin, 'viewer', 'viewer-password-1')
        viewer = self.login('viewer', 'viewer-password-1')[0]
        self.assertEqual(200, self.request('PUT', self.base + '/members/' + user,
                                          {'role': 'viewer'}, token=admin).status)
        detail = self.request('GET', route, token=viewer)
        self.assertEqual(detail.status, 200, detail.data)
        self.assertFalse(detail.data['can_edit'])
        self.assertEqual([r['acceptance_state'] for r in detail.data['history']], ['draft', 'accepted', 'draft'])
        brd = self.request('GET', self.base + '/brd', token=viewer)
        self.assertEqual(brd.status, 200, brd.data)
        self.assertEqual(brd.data['items'][0]['current']['description'], 'New draft')
        self.assertEqual(self.request('POST', route + '/accept', self.expected(edited.data),
                                      token=viewer, key='viewer-accept').status, 403)
        self.assertTrue(all(s['require_authority'] for s in self.backend.sent if s['action']=='owner-requirements'))

    def test_non_owner_superuser_credential_and_member_cannot_edit_or_change_mode(self):
        first = self.create_requirement()
        admin = self.admin_token()
        # Registration made the bootstrap superuser an owner too. Remove only
        # that fixture membership so this exercises a superuser who is NOT owner.
        admin_id = self.service.authenticate(admin).user_id
        self.service.state['memberships'][self.project].pop(admin_id)
        self.service.store.save()
        member_id = self.create_account(admin, 'member', 'member-password-1')
        member = self.login('member', 'member-password-1')[0]
        self.assertEqual(200, self.request('PUT', self.base + '/members/' + member_id,
                                          {'role': 'contributor'}, token=admin).status)
        credential = self.issue_credential(self.owner_token, self.project, label='worker', scopes=['read', 'tasks'])
        agent = self.request('POST', '/v1/agents', {'name': 'Requirements helper',
                            'working_directory': '/tmp/synthetic-worker', 'projects': [self.project]}, token=self.owner_token)
        self.assertEqual(agent.status, 201, agent.data)
        mode = governance.current(self.path, self.project)
        before = len(self.backend.native.writes())
        for token in (admin, member, credential['secret'], agent.data['credential']['secret']):
            with self.subTest(principal=token[:4]):
                self.assertEqual(self.request('POST', self.base + '/requirements/' + first['id'] + '/accept',
                    self.expected(first), token=token, key='denied-accept').status, 403)
                self.assertEqual(self.request('PUT', self.base + '/requirements/governance',
                    dict(self.expected(mode), mode='governed'), token=token, key='denied-mode').status, 403)
        self.assertEqual(len(self.backend.native.writes()), before)
        self.assertEqual(governance.current(self.path, self.project), mode)

    def test_governance_cas_mode_blocks_mutation_and_keeps_historical_acceptance(self):
        first = self.create_requirement()
        route = self.base + '/requirements/' + first['id']
        accepted = self.request('POST', route + '/accept', self.expected(first),
                                token=self.owner_token, key='accept-req').data
        mode = self.request('GET', self.base + '/requirements/governance', token=self.owner_token).data
        switched = self.request('PUT', self.base + '/requirements/governance',
                                dict(self.expected(mode), mode='governed'), token=self.owner_token, key='mode-001')
        self.assertEqual(switched.status, 200, switched.data)
        before = len(self.backend.native.writes())
        refused = self.request('PATCH', route, dict(self.expected(accepted), title='Changed'),
                               token=self.owner_token, key='governed-edit')
        self.assertEqual(refused.status, 422, refused.data)
        stale = self.request('PUT', self.base + '/requirements/governance',
                             dict(self.expected(mode), mode='simple'), token=self.owner_token, key='stale-mode')
        self.assertEqual(stale.status, 409, stale.data)
        self.assertEqual(len(self.backend.native.writes()), before)
        self.assertEqual(self.request('GET', route, token=self.owner_token).data['current']['acceptance_state'], 'accepted')

    def test_owner_demotion_between_http_check_and_effect_prevents_all_writes(self):
        first = self.create_requirement()
        before = len(self.backend.native.writes())
        def demote():
            self.service.state['memberships'][self.project][self.account] = 'contributor'
            self.service.store.save()
        self.backend.before_owner = demote
        result = self.request('POST', self.base + '/requirements/' + first['id'] + '/accept',
                              self.expected(first), token=self.owner_token, key='demoted-1')
        self.assertIn(result.status, (403, 422), result.data)
        self.assertEqual(len(self.backend.native.writes()), before)

    def test_concurrent_accepts_of_the_same_draft_write_one_decision_and_revision(self):
        from concurrent.futures import ThreadPoolExecutor
        first = self.create_requirement()
        route = self.base + '/requirements/' + first['id'] + '/accept'
        def accept(number):
            return self.request('POST', route, self.expected(first), token=self.owner_token,
                                key='race-accept-'+str(number))
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(accept, (1, 2)))
        self.assertEqual(sorted(r.status for r in responses), [200, 409], [r.data for r in responses])
        row = self.backend.native.row(first['id'])
        self.assertEqual(set(records.existing_revisions(row)), {1, 2})
        self.assertEqual(len(owner_records.existing_acceptances(row)), 1)
        self.assertEqual(sum(r['issue_type']=='decision' for r in self.backend.native.rows), 1)

    def test_owner_narrative_uses_the_same_acceptance_and_member_read_path(self):
        created = self.request('POST', self.base+'/requirements', {
            'kind':'brd-section', 'parent':'job-1', 'title':'Purpose',
            'description':'Who uses the project and why.'}, token=self.owner_token, key='narrative-create')
        self.assertEqual(created.status, 201, created.data)
        accepted = self.request('POST', self.base+'/requirements/'+created.data['id']+'/accept',
                               self.expected(created.data), token=self.owner_token, key='narrative-accept')
        self.assertEqual(accepted.status, 200, accepted.data)
        document = self.request('GET', self.base+'/brd', token=self.owner_token)
        item = next(i for i in document.data['items'] if i['id']==created.data['id'])
        self.assertEqual(item['kind'], 'brd-section')
        self.assertNotIn('key', item['current'])
        self.assertEqual(item['current']['acceptance_state'], 'accepted')

    def test_partial_owner_acceptance_reconciles_exact_receipt_through_guarded_recovery(self):
        first = self.create_requirement()
        route = self.base + '/requirements/' + first['id'] + '/accept'
        self.backend.native.fail_comment_prefix = records.REVISION_PREFIX
        failed = self.request('POST', route, self.expected(first), token=self.owner_token, key='partial-accept')
        self.assertEqual(failed.status, 503, failed.data)
        row = self.backend.native.row(first['id'])
        self.assertEqual(set(owner_records.existing_acceptances(row)), {2})
        self.assertEqual(set(records.existing_revisions(row)), {1})
        conflict = self.request('POST', route, dict(self.expected(first), expected_sha256='0'*64),
                                token=self.owner_token, key='partial-accept')
        self.assertEqual(conflict.status, 409, conflict.data)
        # The first recovery also loses its outcome. Its unknown identity must
        # remain held; the next reconciliation proves the same inner receipt.
        interrupted = self.request('POST', route, self.expected(first), token=self.owner_token, key='partial-accept')
        self.assertEqual(interrupted.status, 503, interrupted.data)
        self.backend.native.fail_comment_prefix = None
        completed = self.request('POST', route, self.expected(first), token=self.owner_token, key='partial-accept')
        self.assertEqual(completed.status, 200, completed.data)
        self.assertEqual(set(records.existing_revisions(row)), {1, 2})
        self.assertEqual(sum(r['issue_type']=='decision' for r in self.backend.native.rows), 1)
        self.assertEqual(sum(c['text'].startswith(owner_records.ACCEPTANCE_PREFIX) for c in row['comments']), 1)
        before = len(self.backend.native.writes())
        replay = self.request('POST', route, self.expected(first), token=self.owner_token, key='partial-accept')
        self.assertEqual(replay.data, completed.data)
        self.assertEqual(len(self.backend.native.writes()), before)

    def test_unknown_owner_request_without_matching_receipt_cannot_be_recovered(self):
        first = self.create_requirement()
        route = self.base + '/requirements/' + first['id'] + '/accept'
        self.backend.native.fail_comment_prefix = records.REVISION_PREFIX
        failed = self.request('POST', route, self.expected(first), token=self.owner_token, key='missing-receipt')
        self.assertEqual(failed.status, 503, failed.data)
        row = self.backend.native.row(first['id'])
        from requirements import load_json
        # Match the pending native receipt by its exact accepted target, not by a
        # guessed filename; only this disposable fixture's receipt is removed.
        pending = [p for p in (self.path/requirements.JOURNAL).glob('*.json')
                   if load_json(p).get('id') == first['id'] and load_json(p).get('status') == 'pending']
        self.assertEqual(len(pending), 1)
        pending[0].unlink()
        before = len(self.backend.native.writes())
        self.backend.native.fail_comment_prefix = None
        refused = self.request('POST', route, self.expected(first), token=self.owner_token, key='missing-receipt')
        self.assertEqual(refused.status, 422, refused.data)
        self.assertEqual(len(self.backend.native.writes()), before)
        self.assertEqual(set(records.existing_revisions(row)), {1})

    def test_closed_fields_and_idempotency_conflict_do_not_write(self):
        first = self.create_requirement()
        before = len(self.backend.native.writes())
        route = self.base + '/requirements/' + first['id'] + '/accept'
        for field in ('actor', 'account_id', 'governance', 'decision', 'source', 'acceptance_state'):
            refused = self.request('POST', route, dict(self.expected(first), **{field:'invented'}),
                                   token=self.owner_token, key='injected-' + field)
            self.assertEqual(refused.status, 422, refused.data)
        self.assertEqual(len(self.backend.native.writes()), before)

        self.request('POST', route, self.expected(first), token=self.owner_token, key='one-click')
        before = len(self.backend.native.writes())
        conflict = self.request('POST', route, dict(self.expected(first), expected_sha256='0'*64),
                                token=self.owner_token, key='one-click')
        self.assertEqual(conflict.status, 409, conflict.data)
        self.assertEqual(len(self.backend.native.writes()), before)

    def test_owner_and_viewer_pages_against_loopback_api(self):
        from test_http_web import run_node_module
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for page behavior checks')
        admin = self.admin_token()
        user = self.create_account(admin, 'viewer', 'viewer-password-1')
        viewer = self.login('viewer', 'viewer-password-1')[0]
        self.assertEqual(200, self.request('PUT', self.base + '/members/' + user,
                                          {'role': 'viewer'}, token=admin).status)
        root = Path(__file__).resolve().parents[1]
        done = run_node_module(self, node, 'await import(process.argv[1])',
            (root/'tests'/'web_owner_requirements.mjs').as_uri(),
            (root/'web'/'js'/'api.js').as_uri(),
            (root/'web'/'js'/'views'/'owner_requirements.js').as_uri(),
            'http://127.0.0.1:' + str(self.port), self.project, self.owner_token, viewer)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(all(json.loads(done.stdout).values()), done.stdout)
