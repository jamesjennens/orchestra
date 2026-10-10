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
            # State is persisted by the HTTP service. Writing it again here
            # would race two fixture requests on the same temporary file.
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

    def failed_creation(self, key='failed-create'):
        self.backend.native.create_outcome = 'fail-after-preflight'
        fields = dict(kind='requirement', parent='job-1', title='Failed intent', description='No row yet.')
        failed = self.request('POST', self.base + '/requirements', fields, token=self.owner_token, key=key)
        self.backend.native.create_outcome = 'ok'
        self.assertEqual(failed.status, 503, failed.data)
        listing = self.request('GET', self.base + '/requirements/recoveries', token=self.owner_token)
        self.assertEqual(listing.status, 200, listing.data)
        operation = self.backend._result_key(self.service.authenticate(self.owner_token),
                                             self.project, 'requirements.create', key, None)
        return fields, next(x for x in listing.data['items'] if x['operation_id'] == operation)

    @staticmethod
    def recovery_body(item):
        return dict(original_operation_id=item['operation_id'],
                    expected_receipt_sha256=item['expected_receipt_sha256'], reason='No requirement was written.')

    def test_clear_empty_failed_creation_retains_audit_and_requires_new_intent(self):
        import admin
        from requirements import content_hash
        fields, item = self.failed_creation()
        self.assertTrue(item['can_clear'], item)
        before = len(self.backend.native.writes())
        body = self.recovery_body(item)
        cleared = self.request('POST', self.base + '/requirements/recoveries/clear', body,
                               token=self.owner_token, key='clear-empty')
        self.assertEqual(cleared.status, 200, cleared.data)
        self.assertTrue(cleared.data['released'])
        self.assertEqual(len(self.backend.native.writes()), before)
        path = self.path / requirements.JOURNAL / (content_hash({'operation_id': item['operation_id']}) + '.json')
        receipt = json.loads(path.read_text())
        self.assertEqual(receipt['status'], 'released')
        self.assertEqual(receipt['release_history'], [cleared.data['audit']])
        admin.validate_coordination_files({requirements.JOURNAL + '/' + path.name: receipt})
        new_token = self.login('alex', 'alex-password-1')[0]
        repeated = self.request('POST', self.base + '/requirements/recoveries/clear', body,
                                token=new_token, key='clear-empty')
        self.assertEqual(repeated.data, cleared.data)
        old = self.request('POST', self.base + '/requirements', fields, token=new_token, key='failed-create')
        self.assertEqual(old.status, 422, old.data)
        self.assertEqual(len(self.backend.native.writes()), before)
        # Even after the outer journal's retention window, the inner terminal
        # receipt cannot become the generic core's reusable released reservation.
        context = requirements.OwnerContext(self.project, self.account, governance.current(self.path, self.project))
        with self.assertRaisesRegex(ValueError, 'Start a new creation'):
            requirements.apply(self.path, context, 'create', fields, item['operation_id'], self.backend.native)
        self.assertEqual(len(self.backend.native.writes()), before)
        self.assertEqual(self.request('GET', self.base + '/requirements/recoveries',
                                     token=new_token).data['items'], [])
        changed = self.request('POST', self.base + '/requirements/recoveries/clear',
            dict(body, reason='Changed reason'), token=new_token, key='clear-changed')
        self.assertEqual(changed.status, 422, changed.data)
        self.assertEqual(self.request('POST', self.base + '/requirements', fields,
                                     token=new_token, key='new-creation').status, 201)

    def test_clear_requires_the_exact_current_receipt_hash_before_any_effect(self):
        from requirements import content_hash
        _, item = self.failed_creation('hash-guard-create')
        path = self.path / requirements.JOURNAL / (content_hash({'operation_id': item['operation_id']}) + '.json')
        original = path.read_bytes()
        before = len(self.backend.native.writes())
        body = self.recovery_body(item)
        refused = self.request('POST', self.base + '/requirements/recoveries/clear',
            dict(body, expected_receipt_sha256='0' * 64), token=self.owner_token, key='wrong-clear-hash')
        self.assertEqual(refused.status, 422, refused.data)
        self.assertIn('reload', str(refused.data).lower())
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.backend.native.writes()), before)
        cleared = self.request('POST', self.base + '/requirements/recoveries/clear',
            body, token=self.owner_token, key='right-clear-hash')
        self.assertEqual(cleared.status, 200, cleared.data)
        self.assertTrue(cleared.data['released'])
        self.assertEqual(len(self.backend.native.writes()), before)

    def test_one_damaged_recovery_receipt_does_not_hide_other_creations_or_paths(self):
        from requirements import content_hash
        _, damaged = self.failed_creation('a-damaged')
        _, healthy = self.failed_creation('b-healthy')
        path = self.path / requirements.JOURNAL / (content_hash({'operation_id': damaged['operation_id']}) + '.json')
        path.write_text('{not valid JSON', encoding='utf-8')
        listing = self.request('GET', self.base + '/requirements/recoveries', token=self.owner_token)
        self.assertEqual(listing.status, 200, listing.data)
        items = {x['operation_id']: x for x in listing.data['items']}
        self.assertFalse(items[damaged['operation_id']]['can_clear'])
        self.assertTrue(items[healthy['operation_id']]['can_clear'])
        self.assertNotIn(str(self.path), json.dumps(listing.data))
        self.assertEqual((listing.data['listed'], listing.data['total'], listing.data['truncated']), (2, 2, False))
        refused = self.request('POST', self.base + '/requirements/recoveries/clear',
            self.recovery_body(damaged), token=self.owner_token, key='damaged-clear')
        self.assertEqual(refused.status, 422, refused.data)
        self.assertNotIn(str(self.path), json.dumps(refused.data))
        # Operating-system errors can carry a private filename too.
        load = requirements.load_json
        def unreadable(candidate):
            if Path(candidate) == path:
                raise OSError('private-server-path-sentinel /private/server/receipt.json')
            return load(candidate)
        with mock.patch.object(requirements, 'load_json', unreadable):
            listing = self.request('GET', self.base + '/requirements/recoveries', token=self.owner_token)
            refused = self.request('POST', self.base + '/requirements/recoveries/clear',
                self.recovery_body(damaged), token=self.owner_token, key='unreadable-clear')
        self.assertEqual(listing.status, 200, listing.data)
        self.assertNotIn('private-server-path-sentinel', json.dumps(listing.data))
        self.assertNotIn('private-server-path-sentinel', json.dumps(refused.data))

    def test_recovery_work_is_bounded_and_shares_one_complete_native_read(self):
        from http_authority import OperationJournal
        from requirements import content_hash
        _, first = self.failed_creation('bounded-first')
        original_path = self.path / requirements.JOURNAL / (content_hash({'operation_id': first['operation_id']}) + '.json')
        receipt = original_path.read_text()
        journal = OperationJournal(journal_path(self.path))
        original = journal.lookup(first['operation_id'])
        # Populate actual request-journal rows and receipts in this disposable
        # project; discovery, count and HTTP projection are production paths.
        for n in range(24):
            operation = 'bounded-pending-' + str(n)
            journal._actor = original['actor']; journal._route = original['route']
            journal.reserve(operation, original['request_hash'], original['principal'])
            journal.mark_unknown(operation)
            path = self.path / requirements.JOURNAL / (content_hash({'operation_id': operation}) + '.json')
            path.write_text(receipt)
        for repeat in range(2):
            start = len(self.backend.native.calls)
            listing = self.request('GET', self.base + '/requirements/recoveries', token=self.owner_token)
            self.assertEqual(listing.status, 200, listing.data)
            self.assertEqual((listing.data['listed'], listing.data['total'], listing.data['truncated']), (20, 25, True))
            self.assertEqual(len(listing.data['items']), 20)
            self.assertTrue(all(x['can_clear'] for x in listing.data['items']))
            calls = self.backend.native.calls[start:]
            self.assertEqual(sum(x[0] == 'export' for x in calls), 1, calls)
            self.assertEqual(sum(x[0] == 'list' for x in calls), 1, calls)

    def test_an_incomplete_native_read_is_not_reused_as_an_absence_proof(self):
        self.failed_creation('incomplete-first'); self.failed_creation('incomplete-second')
        call = type(self.backend.native).__call__
        def missing(native, args):
            if args[0] == 'list':
                native.calls.append(list(args)); return '[]'
            return call(native, args)
        start = len(self.backend.native.calls)
        with mock.patch.object(type(self.backend.native), '__call__', missing):
            listing = self.request('GET', self.base + '/requirements/recoveries', token=self.owner_token)
        self.assertEqual(listing.status, 200, listing.data)
        self.assertEqual(len(listing.data['items']), 2)
        self.assertFalse(any(x['can_clear'] for x in listing.data['items']))
        self.assertEqual(sum(x[0] == 'export' for x in self.backend.native.calls[start:]), 1)

    def test_recovery_panel_error_keeps_the_document_and_edit_controls_usable(self):
        from test_http_web import run_node_module
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for page behavior checks')
        item = self.create_requirement('panel-keeps-document')
        from http_authority import OperationJournal
        with mock.patch.object(OperationJournal, 'pending_owner_creates_snapshot',
                               side_effect=OSError('private-server-path-sentinel /private/server/journal')):
            reply = self.request('GET', self.base + '/requirements/recoveries', token=self.owner_token)
            self.assertNotIn('private-server-path-sentinel', json.dumps(reply.data))
            root = Path(__file__).resolve().parents[1]
            output = run_node_module(self, node, 'await import(process.argv[1])',
                (root/'tests/web_owner_recovery_failure.mjs').as_uri(),
                (root/'web/js/api.js').as_uri(), (root/'web/js/views/owner_requirements.js').as_uri(),
                'http://127.0.0.1:' + str(self.port), self.project, self.owner_token)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertTrue(json.loads(output.stdout)['documentStillUsable'])

    def test_allocated_even_empty_native_row_refuses_owner_clear_with_next_action(self):
        fields = dict(kind='requirement', parent='job-1', title='Allocated', description='No comment.')
        self.backend.native.fail_comment_prefix = records.REVISION_PREFIX
        failed = self.request('POST', self.base + '/requirements', fields, token=self.owner_token, key='allocated')
        self.assertEqual(failed.status, 503, failed.data)
        self.backend.native.fail_comment_prefix = None
        listing = self.request('GET', self.base + '/requirements/recoveries', token=self.owner_token)
        item = listing.data['items'][0]
        self.assertFalse(item['can_clear'])
        self.assertIn('Retry the original creation', item['message'])
        self.assertIn('host operator', item['message'])
        before = len(self.backend.native.writes())
        refused = self.request('POST', self.base + '/requirements/recoveries/clear',
                               self.recovery_body(item), token=self.owner_token, key='clear-allocated')
        self.assertEqual(refused.status, 422, refused.data)
        self.assertEqual(len(self.backend.native.writes()), before)
        retried = self.request('POST', self.base + '/requirements', fields, token=self.owner_token, key='allocated')
        self.assertEqual(retried.status, 201, retried.data)
        self.assertEqual(sum('requirement' in row['labels'] for row in self.backend.native.rows), 1)

    def test_recovery_rechecks_current_owner_and_original_account(self):
        _, item = self.failed_creation()
        admin = self.admin_token()
        other_id = self.create_account(admin, 'other', 'other-password-1')
        token = self.login('other', 'other-password-1')[0]
        self.assertEqual(self.request('PUT', self.base + '/members/' + other_id,
                                     {'role': 'owner'}, token=admin).status, 200)
        self.assertEqual(self.request('GET', self.base + '/requirements/recoveries', token=token).data['items'], [])
        body = self.recovery_body(item)
        before = len(self.backend.native.writes())
        self.assertEqual(self.request('POST', self.base + '/requirements/recoveries/clear', body,
                                     token=token, key='foreign-clear').status, 422)
        admin_id = self.service.authenticate(admin).user_id
        self.service.state['memberships'][self.project].pop(admin_id)
        self.service.store.save()
        self.assertEqual(self.request('GET', self.base + '/requirements/recoveries', token=admin).status, 403)
        self.assertEqual(self.request('POST', self.base + '/requirements/recoveries/clear', body,
                                     token=admin, key='superuser-clear').status, 403)
        def demote():
            self.service.state['memberships'][self.project][self.account] = 'contributor'
            self.service.store.save()
        self.backend.before_owner = demote
        refused = self.request('POST', self.base + '/requirements/recoveries/clear', body,
                               token=self.owner_token, key='demoted-clear')
        self.assertIn(refused.status, (403, 422), refused.data)
        self.assertEqual(len(self.backend.native.writes()), before)

    def test_interrupted_release_reuses_audit_without_releasing_a_second_intent(self):
        from http_authority import OperationJournal
        _, item = self.failed_creation()
        complete = OperationJournal.complete
        def fail_original(journal, operation_id, *args):
            if operation_id == item['operation_id']:
                raise OSError('Synthetic interruption after release audit')
            return complete(journal, operation_id, *args)
        body = self.recovery_body(item)
        with mock.patch.object(OperationJournal, 'complete', fail_original):
            failed = self.request('POST', self.base + '/requirements/recoveries/clear', body,
                                  token=self.owner_token, key='interrupted-clear')
        self.assertEqual(failed.status, 503, failed.data)
        pending = self.request('GET', self.base + '/requirements/recoveries', token=self.owner_token).data['items'][0]
        self.assertTrue(pending['can_clear'])
        self.assertEqual(pending['reason'], body['reason'])
        self.assertEqual(pending['expected_receipt_sha256'], body['expected_receipt_sha256'])
        recovered = self.request('POST', self.base + '/requirements/recoveries/clear', body,
                                 token=self.owner_token, key='interrupted-clear')
        self.assertEqual(recovered.status, 200, recovered.data)
        repeated = self.request('POST', self.base + '/requirements/recoveries/clear', body,
                                token=self.owner_token, key='interrupted-clear')
        self.assertEqual(repeated.data, recovered.data)

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

    def test_owner_preflight_reader_cannot_overlap_another_requests_state_replacement(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        import http_authority
        first = self.create_requirement()
        route = self.base + '/requirements/' + first['id'] + '/accept'
        armed = threading.Event(); armed.set()
        opened = threading.Event(); release = threading.Event()
        reader_open = threading.Event(); overlapping_write = threading.Event()
        second_save = threading.Event()
        read = http_authority.read_state
        save = self.service.store.save
        write = self.service.store._write

        def hold_reader(path):
            if not armed.is_set():
                return read(path)
            armed.clear()
            with Path(path).open('r', encoding='utf-8') as handle:
                state = json.load(handle)
                reader_open.set(); opened.set()
                try:
                    if not release.wait(10):
                        raise RuntimeError('Synthetic reader was not released')
                finally:
                    reader_open.clear()
            return state

        def observe_save(*args, **kwargs):
            if reader_open.is_set():
                second_save.set()
            return save(*args, **kwargs)

        def observe_write():
            if reader_open.is_set():
                overlapping_write.set()
            return write()

        def accept(number):
            return self.request('POST', route, self.expected(first), token=self.owner_token,
                                key='reader-accept-' + str(number))

        with mock.patch.object(http_authority, 'read_state', hold_reader), \
                mock.patch.object(self.service.store, 'save', observe_save), \
                mock.patch.object(self.service.store, '_write', observe_write):
            with ThreadPoolExecutor(max_workers=2) as pool:
                one = pool.submit(accept, 1)
                try:
                    self.assertTrue(opened.wait(10), 'First request did not open the authority file')
                    two = pool.submit(accept, 2)
                    self.assertTrue(second_save.wait(10), 'Second request did not attempt to save state')
                    # Keep the reader open while the competing save attempts
                    # its replacement. A correctly locked save must wait here.
                    overlapping_write.wait(.5)
                finally:
                    release.set()
                responses = [one.result(), two.result()]
        self.assertFalse(overlapping_write.is_set(), 'State replacement raced an open owner preflight reader')
        self.assertEqual(sorted(r.status for r in responses), [200, 409], [r.data for r in responses])
        self.assertEqual(len(owner_records.existing_acceptances(self.backend.native.row(first['id']))), 1)
        self.assertEqual(sum(r['issue_type'] == 'decision' for r in self.backend.native.rows), 1)

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

    def test_new_login_recovers_the_same_account_unknown_acceptance(self):
        first = self.create_requirement(); route = self.base+'/requirements/'+first['id']+'/accept'
        self.backend.native.fail_comment_prefix = owner_records.ACCEPTANCE_PREFIX
        failed = self.request('POST', route, self.expected(first), token=self.owner_token, key='new-login-recovery')
        self.assertEqual(failed.status, 503, failed.data)
        decision = next(row for row in self.backend.native.rows if row['issue_type'] == 'decision')
        self.backend.native.seed('duplicate-decision-label', issue_type='decision', labels=decision['labels'])
        decision.update(title='Retitled by contributor', description='Reworded', created_by='alice', labels=[])
        new_token = self.login('alex', 'alex-password-1')[0]
        self.assertNotEqual(new_token, self.owner_token)
        self.backend.native.fail_comment_prefix = None
        recovered = self.request('POST', route, self.expected(first), token=new_token, key='new-login-recovery')
        self.assertEqual(recovered.status, 200, recovered.data)
        self.assertEqual(sum(r['issue_type']=='decision' for r in self.backend.native.rows), 2)
        evidence = next(iter(owner_records.existing_acceptances(self.backend.native.row(first['id'])).values()))
        self.assertEqual(evidence['decision']['decision_id'], decision['id'])
        self.assertEqual(len(owner_records.existing_acceptances(self.backend.native.row(first['id']))), 1)
        before = len(self.backend.native.writes())
        self.assertEqual(self.request('POST', route, self.expected(first), token=new_token,
                                     key='new-login-recovery').data, recovered.data)
        self.assertEqual(len(self.backend.native.writes()), before)

    def test_future_governance_warning_preserves_owner_http_reads_and_writes(self):
        from requirements import content_hash
        path = self.path / governance.FILE
        history = json.loads(path.read_text())
        history['revisions'][0]['at'] = '2999-01-01T00:00:00Z'
        history['revisions'][0]['sha256'] = content_hash(history['revisions'][0])
        path.write_text(json.dumps(history))
        before = path.read_bytes()
        current = self.request('GET', self.base + '/requirements/governance', token=self.owner_token)
        self.assertEqual(current.status, 200, current.data)
        self.assertEqual(current.data['warnings'][0]['revision'], 1)
        first = self.create_requirement()
        accepted = self.request('POST', self.base + '/requirements/' + first['id'] + '/accept',
                                self.expected(first), token=self.owner_token, key='future-history-accept')
        self.assertEqual(accepted.status, 200, accepted.data)
        self.assertEqual(self.request('GET', self.base + '/requirements/' + first['id'],
                                     token=self.owner_token).status, 200)
        self.assertEqual(path.read_bytes(), before)

    def test_keyless_edits_are_distinct_requests_and_latest_acceptance_is_noop(self):
        first = self.create_requirement(); route = self.base+'/requirements/'+first['id']
        one = self.request('PATCH', route, dict(self.expected(first), title='First edit'), token=self.owner_token)
        self.assertEqual(one.status, 200, one.data)
        two = self.request('PATCH', route, dict(self.expected(one.data), title='Second edit'), token=self.owner_token)
        self.assertEqual(two.status, 200, two.data)
        accepted = self.request('POST', route+'/accept', self.expected(two.data), token=self.owner_token)
        self.assertEqual(accepted.status, 200, accepted.data)
        before = len(self.backend.native.writes())
        repeated = self.request('POST', route+'/accept', self.expected(accepted.data), token=self.owner_token)
        self.assertEqual(repeated.status, 200, repeated.data)
        self.assertEqual(repeated.data['sha256'], accepted.data['sha256'])
        self.assertEqual(len(self.backend.native.writes()), before)

    def test_lost_committed_owner_answer_replays_in_same_and_new_session(self):
        from http_service import UncertainOutcome
        from http_service import SERVER_TIME_HEADER
        for new_session in (False, True):
            with self.subTest(new_session=new_session):
                payload = dict(kind='requirement', parent='job-1', title='Owner intent',
                               description='Preserve this original committed answer.')
                key = 'lost-owner-answer-' + str(new_session)
                original = self.backend._endpoint
                committed = []
                def lose_answer(action, *args, **kwargs):
                    answer = original(action, *args, **kwargs)
                    if action == 'owner-requirements':
                        committed.append(answer)
                        raise UncertainOutcome('synthetic answer loss after commit')
                    return answer
                self.backend._endpoint = lose_answer
                try:
                    lost = self.request('POST', self.base+'/requirements', payload,
                                        token=self.owner_token, key=key)
                finally:
                    self.backend._endpoint = original
                self.assertEqual(lost.status, 503, lost.data)
                self.assertEqual(committed[0]['returncode'], 0, committed)
                before = len(self.backend.native.writes())
                receipt_before = journal_path(self.path).read_bytes()
                token = self.login('alex', 'alex-password-1')[0] if new_session else self.owner_token
                replay = self.request('POST', self.base+'/requirements', payload, token=token, key=key)
                self.assertEqual(replay.status, 201, replay.data)
                original_data = json.loads(committed[0]['stdout'])
                for name, value in original_data.items():
                    self.assertEqual(replay.data[name], value)
                self.assertEqual(replay.headers[SERVER_TIME_HEADER.lower()], committed[0]['server_time'])
                self.assertEqual(len(self.backend.native.writes()), before)
                self.assertEqual(journal_path(self.path).read_bytes(), receipt_before)
                self.assertEqual(self.request('POST', self.base+'/requirements', payload,
                                             token=token, key=key).data, replay.data)
                changed = self.request('POST', self.base+'/requirements',
                                       dict(payload, title='Different request'), token=token, key=key)
                self.assertEqual(changed.status, 409, changed.data)
                self.assertEqual(len(self.backend.native.writes()), before)

    def test_owner_is_checked_inside_effect_with_authority_lock_held(self):
        import contextlib
        import http_authority
        first = self.create_requirement(); before = len(self.backend.native.writes())
        original_context = requirements.owner_context
        original_lock = http_authority.file_lock
        held, checks = [], []
        @contextlib.contextmanager
        def observe_lock(*args, **kwargs):
            with original_lock(*args, **kwargs):
                held.append(True)
                try: yield
                finally: held.pop()
        def observe_context(*args, **kwargs):
            context = original_context(*args, **kwargs)
            checks.append(bool(held))
            if len(checks) == 2:
                # Keep generic project.admin authority, but revoke the narrower
                # owner membership after the replay check and before effect.
                self.service.state['users'][self.account]['superuser'] = True
                self.service.state['memberships'][self.project][self.account] = 'contributor'
                self.service.store.save()
            return context
        with mock.patch.object(http_authority, 'file_lock', observe_lock), \
                mock.patch.object(requirements, 'owner_context', observe_context):
            denied = self.request('POST', self.base+'/requirements/'+first['id']+'/accept',
                                 self.expected(first), token=self.owner_token, key='effect-check')
        self.assertEqual(denied.status, 422, denied.data)
        self.assertEqual(checks, [False, True])
        self.assertEqual(len(self.backend.native.writes()), before)

    def test_client_at_is_refused_and_same_mode_stale_hash_does_not_noop(self):
        first = self.create_requirement(); before = len(self.backend.native.writes())
        refused = self.request('POST', self.base+'/requirements/'+first['id']+'/accept',
            dict(self.expected(first), at='2030-01-01T00:00:00Z'), token=self.owner_token, key='client-at')
        self.assertEqual(refused.status, 422, refused.data)
        self.assertEqual(len(self.backend.native.writes()), before)
        state = governance.current(self.path, self.project)
        same = self.request('PUT', self.base+'/requirements/governance',
            dict(self.expected(state), mode='simple'), token=self.owner_token, key='same-mode')
        self.assertEqual(same.status, 200, same.data)
        self.assertEqual({key: same.data[key] for key in state}, state)
        stale = self.request('PUT', self.base+'/requirements/governance',
            dict(self.expected(state), expected_sha256='0'*64, mode='simple'),
            token=self.owner_token, key='stale-same-mode')
        self.assertEqual(stale.status, 409, stale.data)

    def test_cookie_needs_csrf_but_explicit_human_bearer_does_not(self):
        first = self.create_requirement(); route = self.base+'/requirements/'+first['id']
        token, csrf, _ = self.login('alex', 'alex-password-1')
        body = dict(self.expected(first), title='Cookie edit')
        refused = self.request('PATCH', route, body, cookie='orchestra_session='+token, key='cookie-missing-csrf')
        self.assertEqual(refused.status, 403, refused.data)
        edited = self.request('PATCH', route, body, cookie='orchestra_session='+token, csrf=csrf, key='cookie-csrf')
        self.assertEqual(edited.status, 200, edited.data)
        bearer = self.request('PATCH', route, dict(self.expected(edited.data), title='Bearer edit'), token=token, key='bearer-edit')
        self.assertEqual(bearer.status, 200, bearer.data)

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

    def test_lost_deep_decision_id_keeps_draft_and_unknown_without_regeneration(self):
        import record_json
        first = self.create_requirement()
        route = self.base + '/requirements/' + first['id'] + '/accept'
        native_type = type(self.backend.native)
        original = native_type.__call__

        def deep_receipt(native, args):
            answer = original(native, args)
            if (args[0] == 'create' and '--dry-run' not in args
                    and args[args.index('--type') + 1] == 'decision'):
                depth = record_json.NESTING_MAX + 1
                return answer[:-1] + ',"extra":' + '[' * depth + '0' + ']' * depth + '}'
            return answer

        with mock.patch.object(native_type, '__call__', deep_receipt):
            failed = self.request('POST', route, self.expected(first),
                                  token=self.owner_token, key='deep-decision-receipt')
        self.assertEqual(failed.status, 503, failed.data)
        row = self.backend.native.row(first['id'])
        self.assertEqual(set(records.existing_revisions(row)), {1})
        self.assertFalse(owner_records.existing_acceptances(row))
        self.assertEqual(sum(r['issue_type'] == 'decision' for r in self.backend.native.rows), 1)
        before = len(self.backend.native.writes())
        recovered = self.request('POST', route, self.expected(first),
                                 token=self.owner_token, key='deep-decision-receipt')
        self.assertEqual(recovered.status, 422, recovered.data)
        self.assertIn('no recorded decision binding', str(recovered.data))
        self.assertEqual(set(records.existing_revisions(row)), {1})
        self.assertFalse(owner_records.existing_acceptances(row))
        self.assertEqual(len(self.backend.native.writes()), before)
        self.assertEqual(sum(r['issue_type'] == 'decision' for r in self.backend.native.rows), 1)

        # Reload the still-readable draft and use a fresh key; the old uncertain
        # identity is never completed or silently bound to a guessed decision.
        fresh = self.request('GET', self.base + '/requirements/' + first['id'], token=self.owner_token)
        self.assertEqual(fresh.status, 200, fresh.data)
        self.assertIn('Reload', str(recovered.data))
        accepted = self.request('POST', route, self.expected(fresh.data['current']),
                                token=self.owner_token, key='fresh-after-reload')
        self.assertEqual(accepted.status, 200, accepted.data)
        self.assertEqual(accepted.data['acceptance_state'], 'accepted')
        self.assertEqual(sum(r['issue_type'] == 'decision' for r in self.backend.native.rows), 2)

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
        self.backend.native.seed('task-2', title='Another task', issue_type='task')
        self.failed_creation('page-failed-creation')
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
