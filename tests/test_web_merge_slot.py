"""The merge slot through real HTTP, the actual endpoint and pinned native bd."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers
import http_authority
import web_merge_slot


class BoundaryTests(unittest.TestCase):
    def test_descriptor_alone_or_other_capability_project_is_not_authority(self):
        request = {'project': 'pp', 'authority': {'project': 'pp', 'capability': 'coordinate'}}
        self.assertFalse(http_authority.verified_web_authority(request, None, 'coordinate', 'pp'))
        # A mismatched descriptor never even consults the configured store.
        config = SimpleNamespace(store=Path('absent-authority-test-file'))
        self.assertFalse(http_authority.verified_web_authority(request, config, 'reviews.approve', 'pp'))
        self.assertFalse(http_authority.verified_web_authority(request, config, 'coordinate', 'other'))

    def test_unbounded_or_extra_request_fields_are_refused(self):
        for body in ({'operation': 'release', 'holder': 'somebody'},
                     {'operation': 'acquire', 'task': 'pp-1', 'target': 'x' * 161},
                     {'operation': 'force-release', 'expected_holder': 'ops', 'reason': ' '},
                     {'operation': 'create', 'verified_web_authority': True}):
            with self.subTest(body=body), self.assertRaises(web_merge_slot.SlotError):web_merge_slot.validate(body)
        self.assertEqual(web_merge_slot.validate({'operation': 'acquire', 'task': 'pp-1', 'target': 'main'}), 'acquire')

    def test_target_and_reason_refuse_control_characters_other_than_nul(self):
        # kittrial-5bb.268 item 7: the plain-text rule of the service's other reason fields.
        for bad in ('main\x1b[2J', 'main\x07', 'main\x7f', 'main\x85', 'main\u202e', 'main\u200b'):
            with self.subTest(text=bad):
                for body in ({'operation': 'acquire', 'task': 'pp-1', 'target': bad},
                             {'operation': 'force-release', 'expected_holder': 'ops', 'reason': bad}):
                    with self.assertRaises(web_merge_slot.SlotError) as raised:
                        web_merge_slot.validate(body)
                    self.assertEqual(raised.exception.status, 422)
                    self.assertIn('plain text', str(raised.exception))
        # Tab, newline and carriage return stay allowed, as the rule allows them; so do letters
        # outside ASCII and a joiner between letters.
        for good in ('release/2026\tmain', 'Stuck holder.\r\nSee the log.', 'versión-ü', '\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645'):
            with self.subTest(text=good):
                self.assertEqual(web_merge_slot.validate({'operation': 'acquire', 'task': 'pp-1', 'target': good}), 'acquire')
                self.assertEqual(web_merge_slot.validate(
                    {'operation': 'force-release', 'expected_holder': 'ops', 'reason': good}), 'force-release')
        # The expected holder is compared with bd's holder, not checked for plain text.
        self.assertEqual(web_merge_slot.validate(
            {'operation': 'force-release', 'expected_holder': 'ops\u200b', 'reason': 'Stuck'}), 'force-release')

    def test_slot_operations_share_the_endpoint_capability_table(self):
        cases = {
            'coordinate': {'merge-check': http_authority.CAP_READ,
                           'merge-acquire': http_authority.CAP_COORDINATE,
                           'merge-release': http_authority.CAP_COORDINATE,
                           'merge-create': http_authority.CAP_PROJECT_ADMIN},
            'web-merge-slot': {'check': http_authority.CAP_READ,
                               'acquire': http_authority.CAP_COORDINATE,
                               'release': http_authority.CAP_COORDINATE,
                               'force-release': http_authority.CAP_PROJECT_ADMIN,
                               'create': http_authority.CAP_PROJECT_ADMIN}}
        for action, operations in cases.items():
            for operation, capability in operations.items():
                with self.subTest(action=action, operation=operation):
                    request = {'project': 'pp', 'action': action, 'args': [json.dumps({'operation': operation})]}
                    self.assertEqual(http_authority.required_capability(request), capability)
                    request['authority'] = {'project': 'pp', 'capability': capability}
                    config = SimpleNamespace(store=Path('never-read-slot-authority-file'))
                    self.assertIsNone(http_authority.block_mismatch_denial(request, config))


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EarlySlotBindingTests(unittest.TestCase):
    def test_wrong_capability_or_project_is_refused_before_project_authority_or_tracker_reads(self):
        from unittest.mock import patch
        endpoint = fixture.endpoint
        config = SimpleNamespace(store=Path('never-read-slot-authority-file'))
        operations = {'coordinate': ('merge-check', 'merge-acquire', 'merge-release', 'merge-create'),
                      'web-merge-slot': ('check', 'acquire', 'release', 'force-release', 'create')}
        for action, names in operations.items():
            for operation in names:
                request = {'project': 'pp', 'actor': 'usr_' + '0' * 16, 'action': action,
                           'args': [json.dumps({'operation': operation})], 'operation_id': 'early-slot-binding'}
                needed = http_authority.required_capability(request)
                wrong = http_authority.CAP_TASKS if needed != http_authority.CAP_TASKS else http_authority.CAP_READ
                for block in ({'project': 'pp', 'capability': wrong},
                              {'project': 'foreign', 'capability': needed}):
                    with self.subTest(action=action, operation=operation, block=block), \
                         patch.object(endpoint, 'project_dir', side_effect=AssertionError('A mismatch reached the project reader')) as project, \
                         patch.object(endpoint.native, 'run', side_effect=AssertionError('A mismatch reached the tracker')) as native, \
                         patch.object(http_authority, 'read_state', side_effect=AssertionError('A mismatch reached the authority store')) as authority:
                        refused = endpoint.execute(Path('absent-slot-root'), dict(request, authority=block),
                                                   authority_config=config, require_authority=True)
                        self.assertEqual((refused['returncode'], refused['authority_status']), (126, 403))
                        project.assert_not_called();native.assert_not_called();authority.assert_not_called()


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class NativeWebSlotTests(RulesHelpers, unittest.TestCase):
    def setUp(self):
        super().setUp()
        def restore_slot():
            # The fixture shares its disposable SQL server within this class.
            # Preserve a primary assertion failure without contaminating the
            # next test's holder/context or native row shape.
            self.coordinate('ops', 'merge-create')
            state = json.loads(self.coordinate('ops', 'merge-check')['stdout'])
            if not state.get('available'):
                released = self.r.bd('merge-slot', 'release', '--holder', state['holder'], '--json')
                self.assertEqual(released.returncode, 0, released.stderr)
        self.addCleanup(restore_slot)

    def web(self):
        import http_service
        import test_http_review_fixes as fixes
        root = self.root
        class Harness(fixes.Harness):
            def make_backend(harness):
                return http_service.EndpointBackend(sys.executable, str(KIT / 'endpoint.py'), str(root), service=harness.service)
        harness = Harness(methodName='runTest');harness.setUp();self.addCleanup(harness._stop_server)
        admin = harness.admin_token()
        registered = harness.request('POST', '/v1/projects', {'project_id': 'pp', 'name': 'Slots'}, token=admin)
        self.assertEqual(registered.status, 201, registered.data)
        return harness, admin

    def request(self, harness, token, operation, body=None, key=None):
        return harness.request('GET' if operation == 'read' else 'POST',
                               '/v1/projects/pp/merge-slot' + ('' if operation == 'read' else '/' + operation),
                               body, token=token, key=key)

    def test_web_holder_host_conflict_force_retry_and_one_complete_audit(self):
        self.deployment(operators=['ops'], coordinator_writes=True)
        harness, token = self.web();task = self.task()
        read = self.request(harness, token, 'read');self.assertEqual(read.status, 200, read.data)
        actor = read.data['current_actor']
        taken = self.request(harness, token, 'acquire', {'task_id': task, 'target': 'main'}, key='take-web-slot-1')
        self.assertEqual(taken.status, 200, taken.data);self.assertEqual(taken.data['holder_after'], actor)
        host = self.coordinate('ops', 'merge-acquire', task, 'main')
        self.assertEqual(json.loads(host['stdout']), {'acquired': False, 'holder': actor})
        released = self.request(harness, token, 'release', {}, key='release-web-slot-1')
        self.assertEqual(released.status, 200, released.data)
        self.assertTrue(json.loads(self.coordinate('ops', 'merge-acquire', task, 'main')['stdout'])['acquired'])
        read = self.request(harness, token, 'read');self.assertEqual(read.data['holder'], 'ops')
        harness.backend.faults['merge-slot.force-release'] = 1
        body = {'expected_holder': 'ops', 'reason': 'Holder is unavailable'}
        lost = self.request(harness, token, 'force-release', body, key='force-web-slot-1')
        self.assertEqual(lost.status, 503, lost.data)
        self.assertTrue(json.loads(self.coordinate('ops', 'merge-acquire', task, 'main')['stdout'])['acquired'])
        retry = self.request(harness, token, 'force-release', body, key='force-web-slot-1')
        self.assertEqual(retry.status, 200, retry.data)
        self.assertEqual(self.request(harness, token, 'read').data['holder'], 'ops')
        events = [e for e in harness.service.state['audit'] if e['action'] == 'merge-slot.force-release' and e['outcome'] == 'committed']
        self.assertEqual(len(events), 1);self.assertEqual(events[0]['holder_before'], 'ops');self.assertIsNone(events[0]['holder_after'])
        self.assertEqual(events[0]['task_id'], task);self.assertEqual(events[0]['actor'], actor)
        self.coordinate('ops', 'merge-release')

    def test_stale_holder_and_invalid_reason_write_no_native_state(self):
        harness, token = self.web();task = self.task()
        self.coordinate('ops', 'merge-acquire', task, 'main');before = self.rows()
        for body, status in [({'expected_holder': 'old', 'reason': 'Stale page'}, 409),
                             ({'expected_holder': 'ops', 'reason': ''}, 422),
                             ({'expected_holder': 'ops', 'reason': 'x' * 1001}, 422),
                             ({'expected_holder': 'ops', 'reason': 'Okay', 'actor': 'ops'}, 422),
                             ({'expected_holder': 'ops', 'reason': 'Stop\x1b[2J'}, 422)]:
            reply = self.request(harness, token, 'force-release', body, key='invalid-slot-' + str(status) + '-' + str(len(body.get('reason',''))))
            self.assertEqual(reply.status, status, reply.data);self.assertEqual(self.rows(), before)
        self.coordinate('ops', 'merge-release');before = self.rows()
        # kittrial-5bb.268 item 7: a target with a control character takes nothing.
        reply = self.request(harness, token, 'acquire', {'task_id': task, 'target': 'main\x1b[2J'}, key='invalid-slot-target')
        self.assertEqual(reply.status, 422, reply.data);self.assertIn('plain text', reply.data['error']['message'])
        self.assertEqual(self.rows(), before)

    def test_independent_approval_is_still_required_before_acquisition(self):
        self.deployment(operators=[], coordinator_writes=True, independent_approval=True)
        harness, token = self.web();task = self.task();before = self.rows()
        original_endpoint = harness.backend._endpoint;answers = []
        def record_endpoint(*args, **kwargs):
            answer = original_endpoint(*args, **kwargs);answers.append(answer);return answer
        from unittest.mock import patch
        with patch.object(harness.backend, '_endpoint', side_effect=record_endpoint):
            reply = self.request(harness, token, 'acquire', {'task_id': task, 'target': 'main'}, key='no-approval-slot-1')
        self.assertEqual(reply.status, 409, reply.data);self.assertEqual(self.rows(), before)
        self.assertEqual(answers[-1]['refused'], 'coordinator-rule')
        self.assertEqual(reply.data['error']['message'], answers[-1]['stderr'].strip())
        self.assertIn('installation setting independent_approval is on', reply.data['error']['message'])
        self.assertIn('--rule independent_approval --actor OPERATOR', reply.data['error']['message'])

    def test_roles_grant_removal_demotion_and_owner_only_recovery(self):
        self.deployment(operators=[], coordinator_writes=True)
        harness, owner = self.web();task = self.task()
        people = {}
        for role in ('coordinator', 'contributor', 'viewer'):
            uid = harness.create_account(owner, 'slot-' + role, 'slot-password-1')
            self.assertEqual(harness.request('PUT', '/v1/projects/pp/members/' + uid, {'role': role}, token=owner).status, 200)
            people[role] = (uid, harness.login('slot-' + role, 'slot-password-1')[0])
        coordinator = people['coordinator'][1]
        agent = harness.request('POST', '/v1/agents', {'name': 'Slot agent', 'working_directory': '/tmp/slot-agent', 'projects': ['pp']}, token=coordinator)
        self.assertEqual(agent.status, 201, agent.data);agent_token = agent.data['credential']['secret'];aid = agent.data['agent']['id']
        worker = harness.request('POST', '/v1/projects/pp/worker-credentials', {'scopes': ['tasks', 'reviews'], 'label': 'slot worker'}, token=owner)
        self.assertEqual(worker.status, 201, worker.data);worker_token = worker.data['credential']['secret']
        body = {'task_id': task, 'target': 'main'}
        for label, token in [('agent', agent_token), ('worker', worker_token), ('contributor', people['contributor'][1]), ('viewer', people['viewer'][1])]:
            before = self.rows();reply = self.request(harness, token, 'acquire', body, key='denied-slot-' + label)
            self.assertEqual(reply.status, 403, reply.data);self.assertEqual(self.rows(), before)
        for label, token in [('coordinator', coordinator), ('owner', owner)]:
            take = self.request(harness, token, 'acquire', body, key='allowed-slot-' + label)
            self.assertEqual(take.status, 200, take.data)
            self.assertEqual(self.request(harness, token, 'release', {}, key='allowed-release-' + label).status, 200)
        self.assertEqual(harness.request('PUT', '/v1/projects/pp/agents/' + aid + '/coordinator', {}, token=owner).status, 200)
        take = self.request(harness, agent_token, 'acquire', body, key='granted-agent-slot-1')
        self.assertEqual(take.status, 200, take.data);self.assertEqual(take.data['holder_after'], agent.data['agent']['actor'])
        before = self.rows()
        for op, body2 in [('create', {}), ('force-release', {'expected_holder': take.data['holder_after'], 'reason': 'Own slot'})]:
            self.assertEqual(self.request(harness, agent_token, op, body2, key='agent-admin-slot-' + op).status, 403)
            self.assertEqual(self.rows(), before)
        self.assertEqual(harness.request('DELETE', '/v1/projects/pp/agents/' + aid + '/coordinator', token=owner).status, 200)
        self.assertEqual(self.request(harness, agent_token, 'release', {}, key='revoked-release-slot-1').status, 403)
        recovered = self.request(harness, owner, 'force-release', {'expected_holder': take.data['holder_after'], 'reason': 'Grant removed'}, key='owner-recovers-agent-1')
        self.assertEqual(recovered.status, 200, recovered.data)
        self.assertEqual(harness.request('PUT', '/v1/projects/pp/agents/' + aid + '/coordinator', {}, token=owner).status, 200)
        self.assertEqual(harness.request('PUT', '/v1/projects/pp/members/' + people['coordinator'][0], {'role': 'contributor'}, token=owner).status, 200)
        before = self.rows();reply = self.request(harness, agent_token, 'acquire', body, key='demoted-agent-slot-1')
        self.assertEqual(reply.status, 403, reply.data);self.assertEqual(self.rows(), before)

    def test_committed_audit_survives_a_lost_http_receipt_without_duplicate(self):
        from unittest.mock import patch
        harness, owner = self.web();task = self.task();commit = harness.service.idempotency_commit
        failed = [False]
        def interrupted(*args, **kwargs):
            if not failed[0]:failed[0] = True;raise OSError('synthetic receipt interruption')
            return commit(*args, **kwargs)
        body = {'task_id': task, 'target': 'main'}
        with patch.object(harness.service, 'idempotency_commit', side_effect=interrupted):
            lost = self.request(harness, owner, 'acquire', body, key='audit-before-receipt-1')
        self.assertEqual(lost.status, 500, lost.data)
        stored = json.loads(harness.store.path.read_text())
        self.assertEqual(len([e for e in stored['audit'] if e['action'] == 'merge-slot.acquire' and e['outcome'] == 'committed']), 1)
        retry = self.request(harness, owner, 'acquire', body, key='audit-before-receipt-1')
        self.assertEqual(retry.status, 200, retry.data)
        self.assertEqual(len([e for e in harness.service.state['audit'] if e['action'] == 'merge-slot.acquire' and e['outcome'] == 'committed']), 1)
        self.assertEqual(self.request(harness, owner, 'release', {}, key='audit-receipt-release-1').status, 200)

    def test_real_page_and_copied_named_client_take_and_release(self):
        import shutil
        from test_http_web import run_node_module
        from http_client import Client
        node = shutil.which('node')
        if not node:self.skipTest('Node is unavailable; the real page is not exercised')
        harness, token = self.web();task = self.task()
        done = run_node_module(self, node, 'await import(process.argv[1])',
                               (KIT / 'tests/web_merge_slot_screen.mjs').as_uri(),
                               (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                               (KIT / 'web/js/api.js').as_uri(),
                               (KIT / 'web/js/views/merge_slot.js').as_uri(),
                               'http://127.0.0.1:' + str(harness.port), 'pp', token, task)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:]);view = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertIn('Available', view['before']);self.assertIn('Held by usr_', view['held'])
        self.assertTrue(view['escaped']);self.assertTrue(view['releaseEnabled']);self.assertTrue(view['slot']['available'])
        client = Client('http://127.0.0.1:' + str(harness.port));client.token = token
        take = client.acquire_merge_slot('pp', task, 'main')
        self.assertFalse(take['slot']['available']);self.assertTrue(client.merge_slot('pp')['holder'].startswith('usr_'))
        self.assertTrue(client.release_merge_slot('pp')['slot']['available'])

    def test_explicit_owner_create_repairs_a_damaged_slot_without_releasing_its_holder(self):
        harness, token = self.web();task = self.task()
        self.coordinate('ops', 'merge-acquire', task, 'main')
        changed = self.r.bd('update', 'pp-merge-slot', '--status', 'closed', '--json')
        self.assertEqual(changed.returncode, 0, changed.stderr)
        before = self.rows();read = self.request(harness, token, 'read')
        self.assertEqual(read.status, 503, read.data);self.assertEqual(self.rows(), before)
        repaired = self.request(harness, token, 'create', {}, key='explicit-owner-repair-1')
        self.assertEqual(repaired.status, 200, repaired.data)
        self.assertEqual(repaired.data['holder_after'], 'ops')
        self.assertFalse(self.request(harness, token, 'read').data['available'])
        self.coordinate('ops', 'merge-release')

    def test_endpoint_rejects_a_different_capability_or_project_descriptor(self):
        from unittest.mock import patch
        harness, token = self.web();task = self.task();original = harness.backend._endpoint
        body = {'task_id': task, 'target': 'main'}
        for field, value in [('capability', http_authority.CAP_READ), ('project', 'other')]:
            before = self.rows()
            def forged(*args, **kwargs):
                kwargs['authority'] = dict(kwargs['authority'], **{field: value})
                return original(*args, **kwargs)
            with patch.object(harness.backend, '_endpoint', side_effect=forged):
                reply = self.request(harness, token, 'acquire', body, key='wrong-descriptor-' + field)
            self.assertIn(reply.status, (403, 404), reply.data);self.assertEqual(self.rows(), before)
        control = self.request(harness, token, 'acquire', body, key='right-descriptor-slot-1')
        self.assertEqual(control.status, 200, control.data)
        self.assertEqual(self.request(harness, token, 'release', {}, key='right-descriptor-release-1').status, 200)

    def test_real_page_repair_keeps_its_retry_after_repeated_lost_responses(self):
        import shutil
        from test_http_web import run_node_module
        node = shutil.which('node')
        if not node:self.skipTest('Node is unavailable; the real repair page is not exercised')
        harness, token = self.web();task = self.task()
        self.coordinate('ops', 'merge-acquire', task, 'main')
        damaged = self.r.bd('update', 'pp-merge-slot', '--status', 'closed', '--json')
        self.assertEqual(damaged.returncode, 0, damaged.stderr)
        done = run_node_module(self, node, 'await import(process.argv[1])',
                               (KIT / 'tests/web_merge_slot_repair_screen.mjs').as_uri(),
                               (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                               (KIT / 'web/js/api.js').as_uri(),
                               (KIT / 'web/js/views/merge_slot.js').as_uri(),
                               'http://127.0.0.1:' + str(harness.port), 'pp', token)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        proof = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertTrue(proof['stableKey']);self.assertTrue(proof['repeatedRetry']);self.assertTrue(proof['refreshRace'])
        self.assertEqual(proof['repairRequests'], 3)
        committed = [e for e in harness.service.state['audit'] if e['action'] == 'merge-slot.create' and e['outcome'] == 'committed']
        self.assertEqual(len(committed), 1);self.assertEqual(committed[0]['holder_after'], 'ops')
        self.coordinate('ops', 'merge-release')


if __name__ == '__main__':unittest.main()
