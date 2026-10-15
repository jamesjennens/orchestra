"""Actual HTTP -> EndpointBackend -> native bd/project files, in owned fixtures."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import guidance
import onboarding
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class NativeGuidanceHttpTests(RulesHelpers, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # The class owns this disposable project. Clear only our fixed document
        # names between cases, not any native task or unrelated runtime record.
        for name in ('GUIDANCE.md', '.guidance.json', '.guidance-repair.json', 'ONBOARDING.md', onboarding.OPERATOR_COPY, guidance.CLEAR_NAME):
            target = self.project / name
            if target.exists(): target.unlink()
        self.deployment(operators=['ops'])
        import http_service
        import test_http_review_fixes as fixes
        root = self.root
        class Harness(fixes.Harness):
            def make_backend(h):
                return http_service.EndpointBackend(sys.executable, str(KIT / 'endpoint.py'), str(root), service=h.service)
        self.h = Harness(methodName='runTest'); self.h.setUp()
        self.addCleanup(self.h.doCleanups); self.addCleanup(self.h._stop_server)
        self.admin = self.h.admin_token()
        reply = self.h.request('POST', '/v1/projects', {'project_id': 'pp', 'name': 'Guidance'}, token=self.admin)
        self.assertEqual(reply.status, 201, reply.data)
        self.url = '/v1/projects/pp'

    def call(self, method, suffix, body=None, token=None, key=None):
        return self.h.request(method, self.url + suffix, body, token=self.admin if token is None else token, key=key)

    def person(self, name, role=None):
        password = name + '-password-1'
        uid = self.h.create_account(self.admin, name, password)
        if role:
            reply = self.call('PUT', '/members/' + uid, {'role': role})
            self.assertEqual(reply.status, 200, reply.data)
        return uid, self.h.login(name, password)[0]

    def docs(self):
        return {p.name: p.read_bytes() for p in self.project.iterdir()
                if p.name in ('GUIDANCE.md', '.guidance.json', '.guidance-repair.json', 'ONBOARDING.md', onboarding.OPERATOR_COPY, guidance.CLEAR_NAME)}

    def set_guidance(self, text='Current guidance', token=None, key='guidance-set-1'):
        reply = self.call('PUT', '/guidance', {'text': text}, token=token, key=key)
        self.assertEqual(reply.status, 200, reply.data)
        return reply.data

    def _backup_restore_guidance(self, previous=None):
        import os
        import subprocess
        uid, token = self.person('backup-coordinator', 'coordinator')
        viewer, viewer_token = self.person('backup-viewer', 'viewer')
        made = self.set_guidance('Synthetic backed-up web guidance.', token=token, key='backup-web-set')
        ack = self.call('POST', '/guidance/ack', {'version': made['version']},
                        token=viewer_token, key='backup-web-ack')
        self.assertEqual(ack.status, 200, ack.data)
        (self.project / 'ONBOARDING.md').write_bytes(b'Synthetic retained operator entry\n')
        edited = self.call('PUT', '/onboarding', {'text': 'Synthetic restored web entry.'},
                           token=token, key='backup-web-onboarding')
        self.assertEqual(edited.status, 200, edited.data)
        self.assertEqual(edited.data['operator_text_kept_as'], onboarding.OPERATOR_COPY)
        self.assertEqual((self.project / onboarding.OPERATOR_COPY).read_bytes(),
                         b'Synthetic retained operator entry\n')
        before_docs = {name: (self.project / name).read_bytes()
                       for name in ('GUIDANCE.md', '.guidance.json', 'ONBOARDING.md')}
        source_rows = self.rows()
        records = []
        def command(kit, *words, expected=0):
            argv = [sys.executable, '-B', str(kit / 'admin.py'), '--root', str(self.root), *words]
            reply = subprocess.run(argv, env=fixture.admin.environment(self.root),
                                   capture_output=True, text=True, timeout=180)
            records.append(dict(argv=argv, returncode=reply.returncode, stdout=reply.stdout, stderr=reply.stderr))
            if expected == 0:
                self.assertEqual(reply.returncode, 0, reply.stdout + reply.stderr)
            else:
                self.assertNotEqual(reply.returncode, 0, reply.stdout + reply.stderr)
            return reply
        backups = self.root / 'backups'; backups.mkdir(exist_ok=True)
        initialized = self.r.bd('backup', 'init', str(backups / 'pp'))
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        command(KIT, 'backup', 'pp')
        sidecar = json.loads((backups / 'pp.coordination.json').read_text())
        self.assertEqual(sidecar['status'], 'complete')
        self.assertTrue((backups / 'pp').is_dir())
        fixture.admin.validate_coordination_files(sidecar['files'])
        self.assertEqual(sidecar['files']['.guidance.json']['set_by'], uid)
        self.assertEqual(sidecar['files']['.guidance.json']['set_via'], 'web')
        self.assertIn(viewer, sidecar['files']['.guidance.json']['acknowledged'])
        destination = 'guidanceprevious' if previous else 'guidancecurrent'
        reader = previous or KIT
        if previous:
            # New generations carry set_via. The actual old strict validator must
            # refuse, not silently drop attribution or claim a compatible restore.
            refused = command(reader, 'restore-new', 'pp', destination, expected=1)
            self.assertIn('set_via', refused.stdout + refused.stderr)
            self.assertFalse((self.root / 'projects' / destination).exists())
            self.assertEqual(self.rows(), source_rows)
            self.assertEqual({name: (self.project / name).read_bytes() for name in before_docs}, before_docs)
            export = os.environ.get('ORCHESTRA_GUIDANCE_TEST_EXPORTS')
            if export:
                directory = Path(export); directory.mkdir(parents=True, exist_ok=True)
                (directory / 'backup-restore-released-refusal.json').write_text(json.dumps(
                    dict(commands=records, sidecar=sidecar, source_rows=source_rows,
                         restored=False, source_documents_unchanged=True), indent=2))
            return
        command(reader, 'restore-new', 'pp', destination)
        target = self.root / 'projects' / destination
        for name, content in before_docs.items():
            self.assertEqual((target / name).read_bytes(), content, name)
        rows = [json.loads(line) for line in fixture.admin.run_bd(
            self.root, destination, ['export', '--all']).splitlines() if line.strip()]
        restored_by_id = {row['id']: row for row in rows}
        for row in source_rows:
            self.assertEqual(restored_by_id[row['id']], row)
        def endpoint_read(operation, actor):
            request = dict(project=destination, actor=actor, action='guidance', args=[operation])
            reply = subprocess.run([sys.executable, '-B', str(reader / 'endpoint.py'),
                                    '--root', str(self.root)], input=json.dumps(request),
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(reply.returncode, 0, reply.stderr)
            envelope = json.loads(reply.stdout)
            self.assertEqual(envelope['returncode'], 0, envelope)
            return json.loads(envelope['stdout'])
        get = endpoint_read('get', 'backup-host-reader')
        status = endpoint_read('status', 'ops')
        self.assertEqual(get['set_by'], uid); self.assertEqual(get['version'], made['version'])
        self.assertEqual(status['set_by'], uid)
        self.assertTrue(any(row['actor'] == viewer and row['version'] == made['version']
                            for row in status['acknowledged']))
        export = os.environ.get('ORCHESTRA_GUIDANCE_TEST_EXPORTS')
        if export:
            directory = Path(export); directory.mkdir(parents=True, exist_ok=True)
            label = 'released' if previous else 'current'
            value = dict(commands=records, sidecar=sidecar, source_rows=source_rows, restored_rows=rows,
                         restored_get=get, restored_status=status,
                         canonical_documents_byte_equal=True,
                         retained_operator_copy_in_sidecar=onboarding.OPERATOR_COPY in sidecar['files'],
                         retained_operator_copy_restored=(target / onboarding.OPERATOR_COPY).exists())
            (directory / ('backup-restore-' + label + '.json')).write_text(json.dumps(value, indent=2))

    def test_actual_native_backup_restore_keeps_web_guidance_ack_and_onboarding(self):
        self._backup_restore_guidance()

    def test_released_kit_refuses_route_metadata_restore_without_source_effect(self):
        import os
        previous = os.environ.get('ORCHESTRA_GUIDANCE_PREVIOUS_KIT')
        if not previous:
            self.skipTest('no explicit released previous kit for guidance backup/restore boundary')
        self._backup_restore_guidance(Path(previous))

    def test_ack_waiting_for_host_replacement_or_clear_rechecks_at_effect(self):
        import concurrent.futures
        import fcntl
        import threading
        original = self.h.backend._endpoint
        before_rows = self.rows()
        for change in ('replace', 'clear'):
            made = self.set_guidance('Before concurrent ' + change, key='race-set-' + change)
            sent = threading.Event()
            def observe(action, *args, **kwargs):
                if action == 'web-guidance' and json.loads(args[2][0])['operation'] == 'ack':
                    sent.set()
                return original(action, *args, **kwargs)
            with self.subTest(change=change), patch.object(self.h.backend, '_endpoint', side_effect=observe):
                pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                with (self.project / '.coordination.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    try:
                        future = pool.submit(self.call, 'POST', '/guidance/ack',
                                             {'version': made['version']}, key='race-ack-' + change)
                        self.assertTrue(sent.wait(5), 'HTTP ACK did not reach its canonical request')
                        if change == 'replace':
                            guidance.write_guidance(self.project, 'Host replaced before ACK effect', 'ops')
                        else:
                            guidance.clear(self.project, 'ops')
                        before = self.docs()
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_UN)
                    try:
                        reply = future.result(timeout=10)
                    finally:
                        pool.shutdown(wait=True)
                self.assertEqual(reply.status, 422, reply.data)
                self.assertEqual(self.docs(), before, 'Stale in-flight ACK must not touch the host generation')
        self.assertEqual(self.rows(), before_rows)

    def test_human_demotion_is_rechecked_at_effect_and_completed_replay(self):
        uid, token = self.person('demoted-coordinator', 'coordinator')
        self.set_guidance('Human coordinator guidance', token=token, key='human-original')
        original = self.h.backend._endpoint
        before = self.docs()
        for operation, key, text in [('coordinate-check', 'human-original', 'Human coordinator guidance'),
                                      ('set', 'human-new', 'Denied new guidance')]:
            self.h.service.state['memberships']['pp'][uid] = 'coordinator'
            self.h.service.store.save()
            observed = []
            def demote(action, *args, **kwargs):
                if action == 'web-guidance' and json.loads(args[2][0])['operation'] == operation:
                    observed.append(operation)
                    self.h.service.state['memberships']['pp'][uid] = 'viewer'
                    self.h.service.store.save()
                return original(action, *args, **kwargs)
            with self.subTest(operation=operation), patch.object(self.h.backend, '_endpoint', side_effect=demote):
                reply = self.call('PUT', '/guidance', {'text': text}, token=token, key=key)
            self.assertEqual(observed, [operation])
            self.assertEqual(reply.status, 403, reply.data)
            self.assertEqual(self.docs(), before)

    def test_read_configuration_faults_keep_the_safe_shared_error_category(self):
        def configuration_fault(*args, **kwargs):
            return dict(returncode=2, stdout='', fault='configuration',
                        stderr='PermissionError: synthetic/private/deployment.private.json cannot be read')
        for suffix in ('/guidance', '/guidance/status', '/onboarding/read', '/onboarding'):
            with self.subTest(route=suffix), patch.object(self.h.backend, '_ask', side_effect=configuration_fault):
                reply = self.call('GET', suffix)
            self.assertEqual(reply.status, 503, reply.data)
            self.assertEqual(reply.data['error']['code'], 'server_configuration')
            self.assertEqual(reply.data['error']['message'], self.h.backend.CONFIGURATION_UNREADABLE)
            self.assertNotIn('synthetic/private', json.dumps(reply.data))
            self.assertNotIn('PermissionError', json.dumps(reply.data))

    def test_actual_endpoint_reads_share_project_and_authority_locks(self):
        import concurrent.futures
        import fcntl
        self.set_guidance()
        (self.project / 'ONBOARDING.md').write_bytes(b'Operator project text\n')
        original = self.h.backend._endpoint
        calls = []
        def capture(*args, **kwargs):
            if args[0] == 'web-guidance':
                calls.append((args, kwargs))
            return original(*args, **kwargs)
        with patch.object(self.h.backend, '_endpoint', side_effect=capture):
            for route in ('/guidance', '/guidance/status', '/onboarding/read', '/onboarding'):
                reply = self.call('GET', route)
                self.assertEqual(reply.status, 200, reply.data)
        # The replay guard is also a read, using the exact trusted launch and
        # identity block captured above, with its named coordinate operation.
        args, kwargs = calls[1]
        check = list(args)
        check[3] = [json.dumps({'operation': 'coordinate-check'})]
        calls.append((tuple(check), kwargs))
        before = self.docs()
        before_rows = self.rows()
        for args, kwargs in calls:
            with self.subTest(operation=json.loads(args[3][0])['operation']):
                project_lock = (self.project / '.coordination.lock').open('a')
                authority_lock = Path(str(self.h.service.store.path) + '.lock').open('a')
                pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                try:
                    fcntl.flock(project_lock, fcntl.LOCK_SH)
                    fcntl.flock(authority_lock, fcntl.LOCK_SH)
                    future = pool.submit(original, *args, **kwargs)
                    try:
                        reply = future.result(timeout=5)
                    except concurrent.futures.TimeoutError:
                        reply = None
                finally:
                    fcntl.flock(authority_lock, fcntl.LOCK_UN)
                    fcntl.flock(project_lock, fcntl.LOCK_UN)
                    authority_lock.close(); project_lock.close()
                    pool.shutdown(wait=True)
                self.assertIsNotNone(reply, 'Canonical read queued behind a shared project or authority reader')
                self.assertEqual(reply['returncode'], 0, reply)
        self.assertEqual(before, self.docs())
        self.assertEqual(before_rows, self.rows())

    def test_actual_http_member_roles_exact_ack_host_reads_and_stale_version(self):
        made = self.set_guidance(); self.assertEqual(made['set_by'], self.h.admin_user['id'])
        before_rows = self.rows()
        (self.project / 'ONBOARDING.md').write_bytes(b'Operator-only project text <script>not markup</script>\n')
        for name, role in [('viewer', 'viewer'), ('contributor', 'contributor'), ('coordinator', 'coordinator'), ('owner', 'owner')]:
            uid, token = self.person(name, role)
            before = self.docs()
            read = self.call('GET', '/guidance', token=token)
            self.assertEqual(read.status, 200, read.data); self.assertFalse(read.data['acknowledged'])
            self.assertEqual(read.data['can_coordinate'], role in ('coordinator', 'owner'))
            document = self.call('GET', '/onboarding/read', token=token)
            self.assertEqual(document.status, 200, document.data)
            self.assertEqual(document.data['text'], 'Operator-only project text <script>not markup</script>\n')
            self.assertEqual(before, self.docs(), 'Reads must not acknowledge or rewrite files')
            ack = self.call('POST', '/guidance/ack', {'version': made['version']}, token=token, key='ack-' + name)
            self.assertEqual(ack.status, 200, ack.data)
            self.assertEqual(ack.data, self.call('POST', '/guidance/ack', {'version': made['version']}, token=token, key='ack-' + name).data)
            self.assertTrue(self.call('GET', '/guidance', token=token).data['acknowledged'])
            if role in ('viewer', 'contributor'):
                before = self.docs()
                for method, suffix, body in [('PUT', '/guidance', {'text': 'Forbidden'}), ('DELETE', '/guidance', {}),
                        ('GET', '/guidance/status', None), ('GET', '/onboarding', None), ('PUT', '/onboarding', {'text': 'Forbidden'})]:
                    denied = self.call(method, suffix, body, token=token, key='no-' + name + method + suffix.replace('/', '-'))
                    self.assertEqual(denied.status, 403, denied.data); self.assertEqual(before, self.docs())
            else:
                self.assertIsNone(self.call('GET', '/onboarding', token=token).data['text'])
        roster = self.call('GET', '/guidance/status?offset=0&limit=2')
        self.assertEqual(roster.status, 200, roster.data); self.assertEqual(len(roster.data['acknowledged']), 2)
        self.assertEqual(roster.data['next_offset'], 2); self.assertIn('not a complete', roster.data['retention'])
        for suffix in ('?offset=0&offset=1', '?limit=101', '?limit=', '?actor=ops'):
            self.assertEqual(self.call('GET', '/guidance/status' + suffix).status, 422)
        host = self.endpoint('guidance', 'host-worker', ['get'])
        self.assertEqual(host['returncode'], 0, host); self.assertEqual(json.loads(host['stdout'])['set_by'], self.h.admin_user['id'])
        replacement = guidance.write_guidance(self.project, 'Later host guidance', 'ops')
        self.assertEqual(self.call('GET', '/guidance').data['set_by'], 'ops')
        stale = self.call('POST', '/guidance/ack', {'version': made['version']}, key='stale-ack')
        self.assertEqual(stale.status, 422, stale.data)
        self.assertEqual(self.rows(), before_rows, 'Guidance never mutates native task rows')
        self.assertNotEqual(replacement['version'], made['version'])

    def test_granted_personal_agent_onboarding_replay_and_effect_revoke(self):
        uid, coordinator = self.person('coord-agent', 'coordinator')
        agent = self.h.request('POST', '/v1/agents', {'name': 'Guidance agent', 'working_directory': '/tmp/guidance-agent', 'projects': ['pp']}, token=coordinator)
        self.assertEqual(agent.status, 201, agent.data)
        aid = agent.data['agent']['id']; token = agent.data['credential']['secret']; actor = agent.data['agent']['actor']
        before = self.docs()
        self.assertEqual(self.call('PUT', '/guidance', {'text': 'No grant'}, token=token, key='agent-no-grant').status, 403)
        self.assertEqual(before, self.docs())
        self.assertEqual(self.call('PUT', '/agents/' + aid + '/coordinator', {}).status, 200)
        made = self.set_guidance('Agent guidance', token, 'agent-granted-set')
        self.assertEqual(made['set_by'], actor)
        (self.project / 'ONBOARDING.md').write_bytes(b'Original operator entry\n')
        self.assertEqual(self.call('DELETE', '/onboarding', {}, token=token, key='agent-clear-operator').status, 422)
        written = self.call('PUT', '/onboarding', {'text': 'Agent project entry'}, token=token, key='agent-onboarding')
        self.assertEqual(written.status, 200, written.data)
        self.assertEqual(written.data['operator_text_kept_as'], onboarding.OPERATOR_COPY)
        self.assertEqual((self.project / onboarding.OPERATOR_COPY).read_bytes(), b'Original operator entry\n')
        self.assertEqual(self.call('GET', '/onboarding', token=token).data['text'], 'Agent project entry\n')
        self.assertIn('Agent project entry', self.call('GET', '/onboarding/read', token=token).data['text'])
        self.assertEqual(self.call('DELETE', '/agents/' + aid + '/coordinator').status, 200)
        before = self.docs()
        for suffix, body, key in [('/guidance', {'text': 'Agent guidance'}, 'agent-granted-set'),
                                   ('/onboarding', {'text': 'Agent project entry'}, 'agent-onboarding')]:
            denied = self.call('PUT', suffix, body, token=token, key=key)
            self.assertEqual(denied.status, 403, denied.data); self.assertEqual(before, self.docs())
        self.assertEqual(self.call('PUT', '/agents/' + aid + '/coordinator', {}).status, 200)
        original = self.h.backend._endpoint
        def revoke_at_effect(action, *args, **kwargs):
            if action == 'web-guidance' and json.loads(args[2][0])['operation'] == 'set':
                self.h.service.state['coordinator_grants']['pp'].pop(aid)
                self.h.service.store.save()
            return original(action, *args, **kwargs)
        with patch.object(self.h.backend, '_endpoint', side_effect=revoke_at_effect):
            denied = self.call('PUT', '/guidance', {'text': 'Lost grant before effect'}, token=token, key='agent-effect-race')
        self.assertEqual(denied.status, 403, denied.data); self.assertEqual(before, self.docs())
        for operation, method, suffix, body, key in [
            ('coordinate-check', 'PUT', '/guidance', {'text': 'Agent guidance'}, 'agent-granted-set'),
            ('coordinate-check', 'PUT', '/onboarding', {'text': 'Agent project entry'}, 'agent-onboarding'),
            ('onboarding-edit', 'GET', '/onboarding', None, None)]:
            self.assertEqual(self.call('PUT', '/agents/' + aid + '/coordinator', {}).status, 200)
            observed = []
            def revoke_during_replay(action, *args, **kwargs):
                if action == 'web-guidance' and json.loads(args[2][0])['operation'] == operation:
                    observed.append(operation)
                    self.h.service.state['coordinator_grants']['pp'].pop(aid)
                    self.h.service.store.save()
                return original(action, *args, **kwargs)
            with patch.object(self.h.backend, '_endpoint', side_effect=revoke_during_replay):
                denied = self.call(method, suffix, body, token=token, key=key)
            self.assertEqual(observed, [operation], 'Must reach current canonical guard after the HTTP authority check')
            self.assertEqual(denied.status, 403, denied.data); self.assertEqual(before, self.docs())

    def test_worker_without_distinct_actor_cannot_ack_as_issuer_over_http(self):
        uid, owner = self.person('legacy-worker-owner', 'owner')
        made = self.set_guidance()
        before_rows = self.rows()
        for label, actor in [('absent', None), ('issuer', uid)]:
            issued = self.call('POST', '/worker-credentials', {'scopes': ['tasks'], 'label': label}, token=owner)
            self.assertEqual(issued.status, 201, issued.data)
            credential = issued.data['credential']; token = credential['secret']
            # Existing stores can contain the historical issuer fallback even if a
            # current issuance route rejects account-shaped actor names.
            self.h.service.state['credentials'][credential['id']]['actor'] = actor
            self.h.service.store.save()
            self.assertEqual(self.call('GET', '/guidance', token=token).status, 200)
            before = self.docs()
            for _ in range(2):
                refused = self.call('POST', '/guidance/ack', {'version': made['version']},
                                    token=token, key='issuer-ack-' + label)
                self.assertEqual(refused.status, 403, refused.data)
                self.assertEqual(self.docs(), before)
            self.assertNotIn(uid, guidance.read_meta(self.project)['acknowledged'])
        self.assertEqual(self.rows(), before_rows)
        control = self.call('POST', '/guidance/ack', {'version': made['version']},
                            token=owner, key='real-owner-ack')
        self.assertEqual(control.status, 200, control.data)
        self.assertIn(uid, guidance.read_meta(self.project)['acknowledged'])

    def test_worker_namespace_foreign_anonymous_removed_and_unbound(self):
        uid, owner = self.person('worker-owner', 'owner')
        worker = self.call('POST', '/worker-credentials', {'scopes': ['tasks'], 'label': 'guidance worker', 'actor': 'guidance-worker'}, token=owner)
        self.assertEqual(worker.status, 201, worker.data)
        token = worker.data['credential']['secret']; made = self.set_guidance()
        before = self.docs()
        self.assertEqual(self.call('GET', '/guidance', token=token).status, 200)
        self.assertEqual(before, self.docs())
        ack = self.call('POST', '/guidance/ack', {'version': made['version']}, token=token, key='worker-ack')
        self.assertEqual(ack.status, 200, ack.data)
        metadata = guidance.read_meta(self.project)
        self.assertIn(worker.data['credential']['actor'], metadata['acknowledged'])
        before = self.docs()
        for suffix, body in [('/guidance', {'text': 'No worker coordination'}), ('/onboarding', {'text': 'No worker entry'})]:
            self.assertEqual(self.call('PUT', suffix, body, token=token, key='worker-denied' + suffix.replace('/', '-')).status, 403)
            self.assertEqual(before, self.docs())
        foreign_uid, foreign = self.person('foreign')
        self.assertEqual(self.call('GET', '/onboarding/read', token=foreign).status, 404)
        self.assertEqual(self.h.request('GET', self.url + '/guidance').status, 401)
        self.assertEqual(self.call('DELETE', '/members/' + uid).status, 200)
        self.assertEqual(self.call('POST', '/guidance/ack', {'version': made['version']}, token=token, key='worker-ack').status, 403)
        self.assertEqual(before, self.docs())
        # An out-of-band text change breaks attribution: no text and no ACK.
        (self.project / 'GUIDANCE.md').write_bytes(b'Unbound text')
        read = self.call('GET', '/guidance')
        self.assertTrue(read.data['unbound']); self.assertIsNone(read.data['text']); self.assertIn('repair', read.data['warning'])
        self.assertEqual(self.call('POST', '/guidance/ack', {'version': guidance.version_of('Unbound text')}, key='unbound-ack').status, 422)

    def test_expired_and_revoked_credentials_cannot_read_or_replay_ack(self):
        import time
        uid, owner = self.person('expiry-owner', 'owner')
        made = self.set_guidance()
        for label in ('expired', 'revoked'):
            reply = self.call('POST', '/worker-credentials', {'scopes': ['tasks'], 'label': label, 'actor': 'guidance-' + label}, token=owner)
            self.assertEqual(reply.status, 201, reply.data)
            credential = reply.data['credential']; token = credential['secret']; cid = credential['id']
            first = self.call('POST', '/guidance/ack', {'version': made['version']}, token=token, key=label + '-ack')
            self.assertEqual(first.status, 200, first.data)
            if label == 'revoked':
                revoked = self.call('POST', '/worker-credentials/' + cid + '/revoke', {}, token=owner)
                self.assertEqual(revoked.status, 204, revoked.data)
            else:
                self.h.service.state['credentials'][cid]['expires_at'] = time.time() - 1
                self.h.service.store.save()
            before = self.docs()
            for method, suffix, body in [('GET', '/guidance', None), ('GET', '/onboarding/read', None),
                                         ('POST', '/guidance/ack', {'version': made['version']})]:
                refused = self.call(method, suffix, body, token=token, key=label + '-ack' if method == 'POST' else None)
                self.assertEqual(refused.status, 401, refused.data); self.assertEqual(before, self.docs())

    def test_actual_page_lost_ack_retry_preserves_drafts_and_named_client(self):
        import shutil
        from test_http_web import run_node_module
        from http_client import Client
        node = shutil.which('node')
        if not node: self.skipTest('Node unavailable; actual HTTP page not exercised')
        self.set_guidance('Read <img src=x onerror=alert(1)> literally')
        (self.project / 'ONBOARDING.md').write_bytes(b'Operator entry <script>literal</script>\n')
        done = run_node_module(self, node, 'await import(process.argv[1])',
            (KIT/'tests/web_guidance_screen.mjs').as_uri(), (KIT/'tests/web_dom_shim.mjs').as_uri(),
            (KIT/'web/js/api.js').as_uri(), (KIT/'web/js/views/guidance.js').as_uri(),
            'http://127.0.0.1:' + str(self.h.port), 'pp', self.admin)
        self.assertEqual(done.returncode, 0, done.stderr[-4000:])
        view = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(view['initialWrites'], 0); self.assertTrue(view['draftsRetained']); self.assertTrue(view['escaped'])
        self.assertIn('Operator entry <script>literal</script>', view['initial'])
        self.assertIn(' via web at ', view['initial'])
        self.assertIn('outcome is unknown', view['uncertain'])
        self.assertIn('You acknowledged this version', view['afterRetry']); self.assertTrue(view['state']['acknowledged'])
        self.assertEqual(len(view['ackCalls']), 2)
        self.assertEqual(view['ackCalls'][0]['headers']['Idempotency-Key'], view['ackCalls'][1]['headers']['Idempotency-Key'])
        self.assertEqual(view['ackCalls'][0]['body'], view['ackCalls'][1]['body'])
        self.assertIn('not a complete historical', view['roster'])
        client = Client('http://127.0.0.1:' + str(self.h.port)); client.token = self.admin
        self.assertEqual(client.member_onboarding('pp')['source'], 'operator')
        made = client.set_guidance('pp', 'Named client guidance', key='named-client-set')
        self.assertEqual(client.guidance('pp')['version'], made['version'])
        self.assertTrue(client.acknowledge_guidance('pp', made['version'], key='named-client-ack')['acknowledged'])
        self.assertEqual(client.guidance_status('pp', 0, 1)['limit'], 1)
        self.assertTrue(client.clear_guidance('pp', key='named-client-clear')['changed'])
        self.assertFalse(client.guidance('pp')['present'])


if __name__ == '__main__': unittest.main()
