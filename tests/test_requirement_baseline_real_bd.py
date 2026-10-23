"""B2 through real HTTP, canonical endpoint and pinned disposable bd/Dolt.

Only the explicit sidecar interruption probe replaces one file write. Normal
requests and its recovery use the shipped endpoint without native adapters.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT)); sys.path.insert(0, str(KIT / 'tests'))
import test_bd_label_aliases as fixture
import test_requirement_owner_real_bd as owner_fixture
import record_json
import requirement_baselines as baselines
import requirement_governance as governance


class NativeBaselineTests(fixture.RealBdLabelAliasTests):
    install_fault_boundary = owner_fixture.NativeOwnerTests.install_fault_boundary

    def rows(self):
        result = self.bd('export', '--all')
        self.assertEqual(result.returncode, 0, result.stderr)
        return record_json.loads_rows(result.stdout)

    def test_native_capture_authority_replay_backup_restore_and_previous_reader(self):
        import http_service
        import test_http_review_fixes as fixes
        previous = os.environ.get('ORCHESTRA_BASELINE_PREVIOUS_KIT')
        if not previous:
            self.skipTest('ORCHESTRA_BASELINE_PREVIOUS_KIT is required for both actual backup compatibility directions')
        previous = Path(previous)
        self.assertTrue((previous / 'admin.py').is_file())
        self.install_fault_boundary()  # wraps this fixture's copied bd, logs argv only
        root = self.root; proof = []
        output = os.environ.get('ORCHESTRA_BASELINE_TEST_EXPORTS')
        if output:
            output = Path(output); output.mkdir(parents=True, exist_ok=False)
        def record(name, **details):
            proof.append(dict(probe=name, **details))
            if output:
                (output / 'progress.json').write_text(json.dumps(proof, indent=2), encoding='utf-8')
        def cli(kit, *args, success=True):
            done = subprocess.run([sys.executable, str(kit / 'admin.py'), '--root', str(root), *args],
                env=fixture.admin.environment(root), capture_output=True, text=True, timeout=180)
            record('cli', kit='current' if kit == KIT else 'previous', args=list(args),
                   returncode=done.returncode, stdout=done.stdout, stderr=done.stderr)
            if success:
                self.assertEqual(done.returncode, 0, done.stdout + '\n' + done.stderr)
            return done
        for key, value in fixture.admin.PROJECT_SETTINGS:
            done = self.bd('config', 'set', key, value); self.assertEqual(done.returncode, 0, done.stderr)
        slot = self.bd('create', 'Merge Slot', '--id', 'pp-merge-slot', '--labels', 'gt:slot', '--json')
        self.assertEqual(slot.returncode, 0, slot.stderr)
        made = self.bd('create', 'Baseline parent', '--type', 'epic', '--json')
        self.assertEqual(made.returncode, 0, made.stderr); parent = json.loads(made.stdout)['id']
        (root / 'backups').mkdir(exist_ok=True)
        initialized = self.bd('backup', 'init', str(root / 'backups' / 'pp'))
        self.assertEqual(initialized.returncode, 0, initialized.stderr)

        class HttpHarness(fixes.Harness):
            # Observe the native backend's bounded 150-second answer rather
            # than the small in-memory fixture's ordinary 20-second deadline.
            http_timeout = 180

            def make_backend(self):
                return http_service.EndpointBackend(sys.executable, str(KIT / 'endpoint.py'), str(root), service=self.service)
        harness = HttpHarness(methodName='runTest'); harness.setUp()
        try:
            admin = harness.admin_token()
            account = harness.create_account(admin, 'baseline-owner', 'baseline-owner-password')
            token = harness.login('baseline-owner', 'baseline-owner-password')[0]
            base = '/v1/projects/pp'; collection = base + '/requirements/baselines'
            def register(project, title):
                registered = harness.request('POST', '/v1/projects', {'project_id': project, 'name': title}, token=admin)
                self.assertEqual(registered.status, 201, registered.data)
                member = harness.request('PUT', '/v1/projects/' + project + '/members/' + account, {'role': 'owner'}, token=admin)
                self.assertEqual(member.status, 200, member.data)
            register('pp', 'Native baseline proof')
            mode = harness.request('GET', base + '/requirements/governance', token=token)
            expected = lambda item: dict(expected_revision=item['revision'], expected_sha256=item['sha256'])
            simple = harness.request('PUT', base + '/requirements/governance', dict(expected(mode.data), mode='simple'),
                                     token=token, key='native-baseline-simple')
            self.assertEqual(simple.status, 200, simple.data)
            def create(kind, key, title):
                answer = harness.request('POST', base + '/requirements', dict(kind=kind, parent=parent,
                    title=title, description='Original <script>wording</script> with \u202e control.'), token=token, key=key)
                self.assertEqual(answer.status, 201, answer.data); return answer.data
            first = create('requirement', 'native-baseline-create', 'A native requirement')
            narrative = create('brd-section', 'native-baseline-narrative', 'Purpose and users')
            route = base + '/requirements/' + first['id']
            accepted = harness.request('POST', route + '/accept', expected(first), token=token, key='native-baseline-accept')
            self.assertEqual(accepted.status, 200, accepted.data)
            pending = harness.request('PATCH', route, dict(expected(accepted.data), description='Pending requirement wording'),
                                      token=token, key='native-baseline-pending')
            self.assertEqual(pending.status, 200, pending.data)
            for kind, labels, title in (('decision', '', 'Open native decision'), ('task', 'question', 'Open native question')):
                args = ['create', title, '--type', kind, '--description', 'Observed, never acceptance authority.', '--json']
                if labels: args += ['--labels', labels]
                done = self.bd(*args); self.assertEqual(done.returncode, 0, done.stderr)
            # No baseline-family writes: both actual CLI directions remain compatible.
            self.assertFalse((self.project / baselines.DIRECTORY).exists())
            cli(KIT, 'backup', 'pp'); cli(previous, 'restore-new', 'pp', 'preold')
            cli(previous, 'backup', 'pp'); cli(KIT, 'restore-new', 'pp', 'prenew')
            for destination in ('preold', 'prenew'):
                restored = root / 'projects' / destination
                self.assertFalse((restored / baselines.DIRECTORY).exists())
                self.assertEqual(governance.current(restored, destination)['mode'], 'simple')
            record('before-family-both-cli-directions', passed=True)
            empty = harness.request('GET', collection, token=token)
            self.assertEqual(empty.status, 200, empty.data); self.assertEqual(empty.data['total'], 0)
            cut = harness.request('POST', collection, {'name': 'Native-1'}, token=token, key='native-baseline-cut-one')
            record('first-native-cut', status=cut.status, data=cut.data)
            self.assertEqual(cut.status, 201, cut.data)
            frozen = {name: (self.project / baselines.DIRECTORY / 'native-1' / name).read_bytes() for name in baselines.FILES}
            snapshot = json.loads(frozen['snapshot.json'])
            requirement = next(item for item in snapshot['view']['items'] if item['id'] == first['id'])
            self.assertEqual(requirement['accepted']['sha256'], accepted.data['sha256'])
            self.assertEqual(requirement['pending_draft']['sha256'], pending.data['sha256'])
            self.assertEqual(len(requirement['history']), 3)
            self.assertTrue(snapshot['view']['decisions']); self.assertTrue(snapshot['view']['questions'])
            self.assertEqual(frozen['BRD.md'].count(b'**Pending - not accepted**'), 2)
            self.assertIn(b'Open native observation - not acceptance evidence.', frozen['BRD.md'])
            self.assertNotIn(b'<script>', frozen['BRD.md']); self.assertNotIn('\u202e'.encode(), frozen['BRD.md'])
            exported = harness.request('GET', base + '/brd/export?baseline=NATIVE-1', token=token)
            self.assertEqual(exported.status, 200, exported.data)
            self.assertEqual(exported.data['markdown'].encode(), frozen['BRD.md'])
            if output:
                for name, raw in frozen.items(): (output / ('native-1-' + name)).write_bytes(raw)

            # Every current human member reads; other identities gain no authority.
            principals = []
            for role in ('viewer', 'contributor', 'coordinator'):
                user = harness.create_account(admin, 'baseline-' + role, 'baseline-' + role + '-password')
                member_token = harness.login('baseline-' + role, 'baseline-' + role + '-password')[0]
                assigned = harness.request('PUT', base + '/members/' + user, {'role': role}, token=admin)
                self.assertEqual(assigned.status, 200, assigned.data); principals.append(member_token)
                for suffix in ('/requirements/baselines', '/requirements/baselines/native-1', '/brd/export?baseline=native-1', '/brd/changes?baseline=native-1'):
                    answer = harness.request('GET', base + suffix, token=member_token)
                    self.assertEqual(answer.status, 200, answer.data)
            viewer = principals[0]
            credential = harness.issue_credential(token, 'pp', label='baseline-worker', scopes=['read', 'tasks'])
            agent = harness.request('POST', '/v1/agents', {'name': 'Native baseline helper',
                'working_directory': '/tmp/synthetic-worker', 'projects': ['pp']}, token=token)
            self.assertEqual(agent.status, 201, agent.data)
            grant = harness.request('PUT', base + '/agents/' + agent.data['agent']['id'] + '/coordinator',
                                    {}, token=token)
            self.assertEqual(grant.status, 200, grant.data)
            admin_id = harness.service.authenticate(admin).user_id
            self.assertEqual(harness.request('PUT', base + '/members/' + admin_id, {'role': 'contributor'}, token=admin).status, 200)
            before = self.rows()
            for who in (*principals, admin, credential['secret'], agent.data['credential']['secret']):
                denied = harness.request('POST', collection, {'name': 'denied'}, token=who, key='native-baseline-denied')
                self.assertEqual(denied.status, 403, denied.data)
            for who in (credential['secret'], agent.data['credential']['secret']):
                denied = harness.request('GET', collection, token=who); self.assertEqual(denied.status, 403, denied.data)
            # Bypass only the HTTP route to exercise the shipped endpoint's
            # independent B2 owner check against the actual native project.
            for label, who in (('coordinator', principals[2]), ('granted-agent', agent.data['credential']['secret'])):
                principal = harness.service.authenticate(who)
                with self.assertRaises(http_service.HttpError) as refused:
                    harness.backend.owner_requirements(principal, 'pp', 'baseline',
                        {'name': 'canonical-' + label}, 'native-baseline-canonical-' + label)
                self.assertEqual(refused.exception.status, 422)
                self.assertIn('signed-in project owner', refused.exception.detail)
                self.assertFalse((self.project / baselines.DIRECTORY / ('canonical-' + label)).exists())
            record('native-canonical-coordinator-and-granted-agent-refusals', passed=True)
            self.assertEqual(self.rows(), before)
            cookie_token, csrf, _ = harness.login('baseline-owner', 'baseline-owner-password')
            denied = harness.request('POST', collection, {'name': 'cookie'}, cookie='orchestra_session=' + cookie_token,
                                     key='native-baseline-no-csrf')
            self.assertEqual(denied.status, 403, denied.data)
            confirmed = harness.request('POST', collection, {'name': 'cookie'}, cookie='orchestra_session=' + cookie_token,
                                        csrf=csrf, key='native-baseline-cookie-csrf')
            self.assertEqual(confirmed.status, 201, confirmed.data)
            replay = harness.request('POST', collection, {'name': 'Native-1'}, token=cookie_token, key='native-baseline-cut-one')
            self.assertEqual(replay.status, 201, replay.data); self.assertEqual(replay.data, cut.data)
            governed = harness.request('PUT', base + '/requirements/governance', dict(expected(simple.data), mode='governed'),
                                       token=token, key='native-baseline-governed')
            self.assertEqual(governed.status, 200, governed.data)
            denied = harness.request('POST', collection, {'name': 'Native-1'}, token=token, key='native-baseline-cut-one')
            self.assertEqual(denied.status, 422, denied.data)
            back = harness.request('PUT', base + '/requirements/governance', dict(expected(governed.data), mode='simple'),
                                   token=token, key='native-baseline-simple-again')
            self.assertEqual(back.status, 200, back.data)
            self.assertEqual(harness.request('PUT', base + '/members/' + admin_id, {'role': 'owner'}, token=admin).status, 200)
            self.assertEqual(harness.request('PUT', base + '/members/' + account, {'role': 'coordinator'}, token=admin).status, 200)
            denied = harness.request('POST', collection, {'name': 'Native-1'}, token=token, key='native-baseline-cut-one')
            self.assertEqual(denied.status, 403, denied.data)
            self.assertEqual(harness.request('PUT', base + '/members/' + account, {'role': 'owner'}, token=admin).status, 200)
            record('native-authority-matrix-csrf-mode-newsession-cached-owner', passed=True)

            # One injected sidecar error after a COMPLETE capture; normal recovery
            # uses the stock endpoint, current owner and the same account/key.
            marker = root / 'inject-baseline-write-failure'
            wrapper = root / 'baseline-fault-endpoint.py'
            wrapper.write_text('import sys,runpy\nfrom pathlib import Path\nsys.path.insert(0,' + repr(str(KIT)) + ')\n'
                'import requirement_baselines as store\nreal=store._write_once\nmarker=Path(' + repr(str(marker)) + ')\n'
                'def write(path, raw):\n if path.name=="BRD.md" and marker.exists():\n  marker.unlink()\n  raise OSError("synthetic cut after complete capture")\n return real(path,raw)\n'
                'store._write_once=write\nrunpy.run_path(' + repr(str(KIT / 'endpoint.py')) + ',run_name="__main__")\n', encoding='utf-8')
            marker.write_text('one interruption', encoding='utf-8'); normal = harness.backend.endpoint; harness.backend.endpoint = str(wrapper)
            try:
                interrupted = harness.request('POST', collection, {'name': 'interrupted'}, token=token, key='native-baseline-interrupted')
            finally:
                harness.backend.endpoint = normal
            record('injected-native-cut-interruption', status=interrupted.status, data=interrupted.data)
            self.assertEqual(interrupted.status, 503, interrupted.data)
            stages = list((self.project / baselines.DIRECTORY).glob('.pending-*')); self.assertEqual(len(stages), 1)
            captured = (stages[0] / 'snapshot.json').read_bytes()
            changed = harness.request('PATCH', route, dict(expected(pending.data), description='Wording after saved capture'),
                                      token=token, key='native-baseline-after-capture')
            self.assertEqual(changed.status, 200, changed.data)
            new_session = harness.login('baseline-owner', 'baseline-owner-password')[0]
            recovered = harness.request('POST', collection, {'name': 'interrupted'}, token=new_session, key='native-baseline-interrupted')
            self.assertEqual(recovered.status, 201, recovered.data)
            self.assertEqual((self.project / baselines.DIRECTORY / 'interrupted' / 'snapshot.json').read_bytes(), captured)
            self.assertNotIn(b'Wording after saved capture', (self.project / baselines.DIRECTORY / 'interrupted' / 'BRD.md').read_bytes())
            changes = harness.request('GET', base + '/brd/changes?baseline=native-1', token=viewer)
            self.assertEqual(changes.status, 200, changes.data); self.assertIn(first['id'], changes.data['groups']['edited'])
            self.assertIn(first['id'], changes.data['groups']['pending']); self.assertEqual(changes.data['groups']['accepted'], [])
            record('native-saved-capture-recovery-and-exact-comparison', passed=True)

            # Actual native selected rows without a canonical revision refuse;
            # removing only that label makes the unrelated task harmless.
            orphan = self.bd('create', 'Unverified selected requirement', '--type', 'task', '--labels', 'requirement', '--json')
            self.assertEqual(orphan.returncode, 0, orphan.stderr); orphan_id = json.loads(orphan.stdout)['id']
            total = baselines.list_baselines(self.project, 'pp')['total']
            bad = harness.request('POST', collection, {'name': 'unverified'}, token=token, key='native-baseline-unverified')
            self.assertEqual(bad.status, 422, bad.data)
            self.assertEqual(baselines.list_baselines(self.project, 'pp')['total'], total)
            cleared = self.bd('update', orphan_id, '--remove-label', 'requirement', '--json')
            self.assertEqual(cleared.returncode, 0, cleared.stderr)
            healthy = harness.request('POST', collection, {'name': 'unrelated'}, token=token, key='native-baseline-unrelated')
            self.assertEqual(healthy.status, 201, healthy.data)
            record('actual-native-selected-unverified-versus-unrelated', passed=True)

            # Native export and filtered membership probes remain real bd. At
            # one explicit I/O boundary replace a single exported row with an
            # unreadable shape to measure selected-versus-unrelated isolation.
            bd_source = self.bd_path.read_text(encoding='utf-8')
            before_corruption = self.rows()
            export_wrapper = ('if "export" in args and p.with_name("corrupt-one-export-id").exists():\n'
                ' target=p.with_name("corrupt-one-export-id").read_text().strip()\n'
                ' done=subprocess.run([str(real),*args],capture_output=True,text=True)\n'
                ' sys.stderr.write(done.stderr)\n'
                ' if done.returncode: sys.stdout.write(done.stdout); sys.exit(done.returncode)\n'
                ' for line in done.stdout.split("\\n"):\n'
                '  if line.strip():\n'
                '   row=json.loads(line)\n'
                '   print(json.dumps({"id":row["id"],"malformed":True}) if row["id"]==target else line)\n'
                ' sys.exit(0)\n')
            self.bd_path.write_text(bd_source.replace('os.execv(str(real),[str(real),*args])', export_wrapper + 'os.execv(str(real),[str(real),*args])'), encoding='utf-8')
            export_marker = self.bd_path.with_name('corrupt-one-export-id')
            try:
                export_marker.write_text(orphan_id, encoding='utf-8')
                isolated = harness.request('POST', collection, {'name': 'isolated'}, token=token, key='native-baseline-isolated')
                self.assertEqual(isolated.status, 201, isolated.data)
                export_marker.write_text(first['id'], encoding='utf-8')
                selected_bad = harness.request('POST', collection, {'name': 'corrupt-selected'}, token=token, key='native-baseline-selected-corrupt')
                self.assertEqual(selected_bad.status, 422, selected_bad.data)
                self.assertFalse((self.project / baselines.DIRECTORY / 'corrupt-selected').exists())
            finally:
                export_marker.unlink(missing_ok=True)
                self.bd_path.write_text(bd_source, encoding='utf-8')
            self.assertEqual(self.rows(), before_corruption)
            record('one-corrupt-native-export-row-boundary', passed=True, native_database_unchanged=True)

            node = shutil.which('node')
            if not node:
                with self.subTest(probe='native browser'):
                    self.skipTest('Node.js is required for the actual pinned-native browser proof')
            else:
                from test_http_web import run_node_module
                done = run_node_module(self, node, 'await import(process.argv[1])',
                    (KIT / 'tests/web_requirement_baseline_native.mjs').as_uri(), (KIT / 'web/js/api.js').as_uri(),
                    (KIT / 'web/js/views/requirement_baselines.js').as_uri(), 'http://127.0.0.1:' + str(harness.port),
                    'pp', token, viewer, first['id'])
                if output:
                    (output / 'native-node.stdout').write_text(done.stdout, encoding='utf-8')
                    (output / 'native-node.stderr').write_text(done.stderr, encoding='utf-8')
                self.assertEqual(done.returncode, 0, done.stderr)
                record('actual-node-through-pinned-native-http', **json.loads(done.stdout))


            # New-family backup requires a compatible reader; the old restore
            # refuses its unknown paths. Current restore preserves named bytes.
            cli(KIT, 'backup', 'pp')
            refused = cli(previous, 'restore-new', 'pp', 'oldfrozen', success=False)
            self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
            self.assertIn('Invalid coordination backup path', refused.stdout + refused.stderr)
            self.assertFalse((root / 'projects' / 'oldfrozen' / baselines.DIRECTORY).exists())
            cli(KIT, 'restore-new', 'pp', 'baselinecopy')
            copied = root / 'projects' / 'baselinecopy'
            for name, raw in frozen.items():
                self.assertEqual((copied / baselines.DIRECTORY / 'native-1' / name).read_bytes(), raw)
            register('baselinecopy', 'Restored native baselines')
            copied_base = '/v1/projects/baselinecopy'
            restored_export = harness.request('GET', copied_base + '/brd/export?baseline=native-1', token=token)
            self.assertEqual(restored_export.status, 200, restored_export.data)
            self.assertEqual(restored_export.data['markdown'].encode(), frozen['BRD.md'])
            restored_changes = harness.request('GET', copied_base + '/brd/changes?baseline=native-1', token=token)
            self.assertEqual(restored_changes.status, 200, restored_changes.data)
            self.assertEqual(restored_changes.data['source_project'], 'pp')
            destination_cut = harness.request('POST', copied_base + '/requirements/baselines', {'name': 'destination'},
                                              token=token, key='native-destination-baseline')
            self.assertEqual(destination_cut.status, 201, destination_cut.data)
            self.assertEqual(json.loads((copied / baselines.DIRECTORY / 'destination' / 'snapshot.json').read_bytes())['project'], 'baselinecopy')
            self.assertEqual((copied / baselines.DIRECTORY / 'native-1' / 'BRD.md').read_bytes(), frozen['BRD.md'])
            record('actual-backup-cli-forward-boundary-and-preserved-restore', passed=True)
            self.assertFalse((self.project / baselines.RESTORE_MARKER).exists())
            self.assertEqual({name: (self.project / baselines.DIRECTORY / 'native-1' / name).read_bytes() for name in baselines.FILES}, frozen)
            record('complete', passed=True, frozen_sha256=exported.data['sha256'])
        finally:
            if output:
                calls = self.bd_path.with_name('native-calls.jsonl')
                if calls.is_file(): shutil.copyfile(calls, output / 'native-calls.jsonl')
            harness._stop_server(); harness.doCleanups()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([NativeBaselineTests('test_native_capture_authority_replay_backup_restore_and_previous_reader')])
