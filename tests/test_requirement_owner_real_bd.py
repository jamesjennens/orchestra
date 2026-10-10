"""Optional owner flow through real HTTP, EndpointBackend, endpoint and pinned bd/Dolt.

The borrowed fixture starts and stops its own disposable loopback SQL server.
No native adapter is mocked. ORCHESTRA_BD_BIN selects the pinned test binaries.
"""
import json
import shutil
import subprocess
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

    def install_fault_boundary(self):
        """Inject one failure around a real command; every successful effect is bd."""
        real = self.bd_path.with_name('bd-real')
        shutil.copy(self.bd_path, real)
        self.bd_path.write_text(
            '#!/usr/bin/env python3\nimport os,sys,subprocess\nfrom pathlib import Path\n'
            'p=Path(sys.argv[0]); args=sys.argv[1:]; real=p.with_name("bd-real")\n'
            'def once(name):\n'
            ' marker=p.with_name(name)\n'
            ' if not marker.exists(): return False\n'
            ' marker.unlink(); return True\n'
            'if "create" in args and "--dry-run" not in args:\n'
            ' if once("fail-one-create"): print("synthetic interruption before create",file=sys.stderr); sys.exit(1)\n'
            ' if "--type" in args and args[args.index("--type")+1]=="decision" and once("lose-one-decision-id"):\n'
            '  done=subprocess.run([str(real),*args],capture_output=True,text=True)\n'
            '  sys.stderr.write(done.stderr)\n'
            '  if done.returncode: sys.stdout.write(done.stdout); sys.exit(done.returncode)\n'
            '  print("synthetic lost decision answer",file=sys.stderr); sys.exit(1)\n'
            'if "comments" in args:\n'
            ' i=args.index("comments")\n'
            ' if args[i+1]=="add":\n'
            '  text=args[i+3]\n'
            '  name="fail-one-owner-revision" if text.startswith("Kind: requirement-revision-v1\\n") else "fail-one-owner-evidence" if text.startswith("Kind: requirement-owner-acceptance-v1\\n") else None\n'
            '  if name and once(name): print("synthetic interruption before comment",file=sys.stderr); sys.exit(1)\n'
            'os.execv(str(real),[str(real),*args])\n', encoding='utf-8')

    def fault(self, name):
        self.bd_path.with_name(name).write_text('one synthetic interruption', encoding='utf-8')

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
        # The existing fixture has no governance sidecar: capture that old
        # backup before any owner records or parent tasks exist.
        (root/'backups').mkdir(exist_ok=True)
        initialized = self.bd('backup', 'init', str(root/'backups'/'pp'))
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        for command in (['backup','pp'], ['restore-new','pp','legacy']):
            done = subprocess.run([sys.executable, str(KIT/'admin.py'), '--root', str(root), *command],
                env=fixture.admin.environment(root), capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stdout+'\n'+done.stderr)
        self.assertEqual(governance.current(root/'projects'/'legacy', 'legacy'),
                         {'revision':0, 'sha256':None, 'mode':'governed'})
        self.assertFalse((root/'projects'/'legacy'/governance.FILE).exists())
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
            registered = harness.request('POST', '/v1/projects', {'project_id':'legacy','name':'Old backup'}, token=admin)
            self.assertEqual(registered.status, 201, registered.data)
            self.assertEqual(harness.request('PUT','/v1/projects/legacy/members/'+account,
                                           {'role':'owner'}, token=admin).status, 200)
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
            # LF-only parsing retains a valid NEL in owner text and comments.
            nel = harness.request('POST', base+'/requirements', {'kind':'requirement','parent':job,
                'title':'NEL\u0085requirement','description':'Before\u0085after'}, token=token, key='native-nel-create')
            self.assertEqual(nel.status, 201, nel.data)
            nel_accept = harness.request('POST', base+'/requirements/'+nel.data['id']+'/accept',
                {'expected_revision':nel.data['revision'],'expected_sha256':nel.data['sha256']},
                token=token, key='native-nel-accept')
            self.assertEqual(nel_accept.status, 200, nel_accept.data)
            nel_detail = harness.request('GET', base+'/requirements/'+nel.data['id'], token=token)
            self.assertEqual(nel_detail.status, 200, nel_detail.data)
            self.assertEqual(nel_detail.data['accepted']['description'], 'Before\u0085after')
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
            before = self.rows()
            for suffix in ('acceptance-v1 ', 'acceptance-v1\r{}', 'acceptance-v2\n{}', 'xyz-v1'):
                with self.assertRaisesRegex(ValueError, 'Refusing raw'):
                    fixture.endpoint.execute(root, {'project':'pp','actor':'alice','action':'bd',
                        'args':['comments','add',first['id'],'Kind: requirement-owner-'+suffix,'--json']})
            self.assertEqual(self.rows(), before)
            viewer_id = harness.create_account(admin, 'viewer', 'viewer-password-1')
            viewer = harness.login('viewer', 'viewer-password-1')[0]
            self.assertEqual(harness.request('PUT', base+'/members/'+viewer_id,
                                           {'role':'viewer'}, token=admin).status, 200)
            credential = harness.issue_credential(token, 'pp', label='worker', scopes=['read','tasks'])
            agent = harness.request('POST', '/v1/agents', {'name':'Owner helper',
                'working_directory':'/tmp/synthetic-worker', 'projects':['pp']}, token=token)
            self.assertEqual(agent.status, 201, agent.data)
            admin_id = harness.service.authenticate(admin).user_id
            self.assertEqual(harness.request('PUT', base+'/members/'+admin_id,
                                           {'role':'contributor'}, token=admin).status, 200)
            before = self.rows()
            for denied_token in (viewer, admin, credential['secret'], agent.data['credential']['secret']):
                refused = harness.request('POST', route+'/accept', expected(accepted.data),
                                           token=denied_token, key='native-principal-denied')
                self.assertEqual(refused.status, 403, refused.data)
                mode_refused = harness.request('PUT', base+'/requirements/governance',
                    dict(expected(simple.data), mode='governed'), token=denied_token, key='native-mode-denied')
                self.assertEqual(mode_refused.status, 403, mode_refused.data)
            self.assertEqual(self.rows(), before)
            viewer_document = harness.request('GET', base+'/brd', token=viewer)
            self.assertEqual(viewer_document.status, 200, viewer_document.data)
            self.assertEqual(next(i for i in viewer_document.data['items'] if i['id']==first['id'])['current']['sha256'], accepted.data['sha256'])
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
            # The contributor endpoint must protect the latest human revision
            # through real native reads, including a repeated refused request.
            latest = detail.data['history'][-1]
            contributor_payload = {'schema_version': 1,
                'operation_id': 'native-contributor-owner-edit', 'operation': 'revise',
                'kind': 'requirement', 'task': first['id'], 'key': latest['key'],
                'title': 'Contributor replacement', 'description': 'Propose this instead.',
                'revision': latest['revision'] + 1, 'acceptance_state': 'draft'}
            before = self.rows()
            contributor_receipts = {p.name: p.read_bytes()
                for p in (self.project / '.requirement-requests').glob('*.json')}
            for attempt in range(2):
                with self.subTest(contributor_retry=attempt), self.assertRaisesRegex(
                        ValueError, 'belongs to the project owner.*Propose'):
                    fixture.endpoint.execute(root, {'project': 'pp', 'actor': 'alice',
                        'action': 'requirement', 'args': [json.dumps(contributor_payload)]})
            self.assertEqual(self.rows(), before)
            self.assertEqual({p.name: p.read_bytes()
                for p in (self.project / '.requirement-requests').glob('*.json')},
                contributor_receipts)
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
            # Run the shipped owner and viewer pages against this real endpoint.
            node = shutil.which('node')
            enabled = harness.request('PUT', base+'/requirements/governance',
                dict(expected(governed.data), mode='simple'), token=token, key='native-page-simple')
            self.assertEqual(enabled.status, 200, enabled.data)
            # Two distinct owner clicks on one immutable draft serialize at the
            # actual endpoint lock. Only one creates acceptance evidence.
            from concurrent.futures import ThreadPoolExecutor
            race = harness.request('POST', base+'/requirements', {
                'kind':'requirement', 'parent':job, 'title':'One accepted version',
                'description':'Concurrent clicks do not create two decisions.'},
                token=token, key='native-race-create')
            self.assertEqual(race.status, 201, race.data)
            race_route = base+'/requirements/'+race.data['id']+'/accept'
            def accept_click(number):
                return harness.request('POST', race_route, expected(race.data),
                    token=token, key='native-race-accept-'+str(number))
            with ThreadPoolExecutor(max_workers=2) as pool:
                answers = list(pool.map(accept_click, (1, 2)))
            self.assertEqual(sorted(a.status for a in answers), [200,409], [a.data for a in answers])
            raced = next(r for r in self.rows() if r['id']==race.data['id'])
            self.assertEqual(set(records.existing_revisions(raced)), {1,2})
            self.assertEqual(len(owner.existing_acceptances(raced)), 1)
            self.install_fault_boundary()
            # No row was allocated: complete real export/list absence permits
            # one audited release, and the original identity cannot be reused.
            import requirement_http
            from requirements import content_hash, load_json
            fields = dict(kind='requirement', parent=job, title='Absent failed creation',
                          description='Clear only when native absence is proved.')
            self.fault('fail-one-create')
            before_empty = self.rows()
            failed = harness.request('POST', base+'/requirements', fields,
                                     token=token, key='native-empty-create')
            self.assertEqual(failed.status, 503, failed.data)
            self.assertEqual(self.rows(), before_empty)
            listing = harness.request('GET', base+'/requirements/recoveries', token=token)
            self.assertEqual(listing.status, 200, listing.data)
            self.assertEqual(len(listing.data['items']), 1)
            empty = listing.data['items'][0]; self.assertTrue(empty['can_clear'], empty)
            clear_body = dict(original_operation_id=empty['operation_id'],
                              expected_receipt_sha256=empty['expected_receipt_sha256'],
                              reason='Complete native reads prove no row was written.')
            cleared = harness.request('POST', base+'/requirements/recoveries/clear', clear_body,
                                      token=token, key='native-empty-clear')
            self.assertEqual(cleared.status, 200, cleared.data)
            released_path = self.project/requirement_http.JOURNAL/(content_hash(
                {'operation_id': empty['operation_id']})+'.json')
            released = load_json(released_path)
            self.assertEqual(released['status'], 'released')
            self.assertEqual(released['release_history'], [cleared.data['audit']])
            self.assertEqual(self.rows(), before_empty)
            old = harness.request('POST', base+'/requirements', fields,
                                  token=token, key='native-empty-create')
            self.assertEqual(old.status, 422, old.data)
            self.assertEqual(self.rows(), before_empty)
            new = harness.request('POST', base+'/requirements', fields,
                                  token=token, key='native-empty-new-intent')
            self.assertEqual(new.status, 201, new.data)
            # A confirmed native allocation with no revision comment is never
            # empty. Refuse clearing, then finish the original request once.
            self.fault('fail-one-owner-revision')
            fields = dict(fields, title='Allocated failed creation')
            allocated = harness.request('POST', base+'/requirements', fields,
                                        token=token, key='native-allocated-create')
            self.assertEqual(allocated.status, 503, allocated.data)
            listing = harness.request('GET', base+'/requirements/recoveries', token=token)
            self.assertEqual(listing.status, 200, listing.data)
            self.assertEqual(len(listing.data['items']), 1)
            item = listing.data['items'][0]; self.assertFalse(item['can_clear'])
            self.assertIn('Retry the original creation', item['message'])
            self.assertIn('host operator', item['message'])
            before_allocated = self.rows()
            refused = harness.request('POST', base+'/requirements/recoveries/clear',
                dict(original_operation_id=item['operation_id'],
                     expected_receipt_sha256=item['expected_receipt_sha256'], reason='Cannot clear an allocated row.'),
                token=token, key='native-allocated-clear')
            self.assertEqual(refused.status, 422, refused.data)
            self.assertEqual(self.rows(), before_allocated)
            retried = harness.request('POST', base+'/requirements', fields,
                                     token=token, key='native-allocated-create')
            self.assertEqual(retried.status, 201, retried.data)
            self.assertEqual(len(self.rows()), len(before_allocated))
            # Confirmed decision identity survives an evidence interruption.
            # Retitle/remove its discovery label and plant a decoy with that
            # label through real native commands: recovery still uses its ID.
            bound = harness.request('POST', base+'/requirements', dict(fields, title='Bound decision'),
                                    token=token, key='native-bound-create')
            self.assertEqual(bound.status, 201, bound.data)
            bound_route = base+'/requirements/'+bound.data['id']+'/accept'
            self.fault('fail-one-owner-evidence')
            failed = harness.request('POST', bound_route, expected(bound.data),
                                     token=token, key='native-bound-accept')
            self.assertEqual(failed.status, 503, failed.data)
            paths = [p for p in (self.project/requirement_http.JOURNAL).glob('*.json')
                     if load_json(p).get('id')==bound.data['id'] and load_json(p).get('status')=='pending']
            self.assertEqual(len(paths), 1)
            binding = load_json(paths[0])['owner_decision']
            bound_id = binding['decision']['decision_id']
            decision = next(r for r in self.rows() if r['id']==bound_id)
            label = next(l for l in decision['labels'] if l.startswith('owner-requirement-decision:'))
            edited = self.bd('update', bound_id, '--title', 'Contributor retitle',
                             '--description', 'Editable text has no binding authority.', '--remove-label', label)
            self.assertEqual(edited.returncode, 0, edited.stderr)
            decoy = self.bd('create', 'Decoy decision', '--type', 'decision', '--labels', label, '--json')
            self.assertEqual(decoy.returncode, 0, decoy.stderr)
            before_bound = self.rows()
            token = harness.login('owner', 'owner-password-1')[0]
            recovered_bound = harness.request('POST', bound_route, expected(bound.data),
                                             token=token, key='native-bound-accept')
            self.assertEqual(recovered_bound.status, 200, recovered_bound.data)
            row = next(r for r in self.rows() if r['id']==bound.data['id'])
            recorded = owner.existing_acceptances(row)[2]
            self.assertEqual(recorded['decision']['decision_id'], bound_id)
            self.assertEqual(recorded['at'], binding['at'])
            self.assertEqual(len(self.rows()), len(before_bound))
            # The actual create can commit while its answer is lost. Without a
            # durable ID binding the retry stays unknown and creates nothing.
            lost = harness.request('POST', base+'/requirements', dict(fields, title='Lost decision ID'),
                                   token=token, key='native-lost-create')
            self.assertEqual(lost.status, 201, lost.data)
            lost_route = base+'/requirements/'+lost.data['id']+'/accept'
            self.fault('lose-one-decision-id')
            failed = harness.request('POST', lost_route, expected(lost.data),
                                     token=token, key='native-lost-accept')
            self.assertEqual(failed.status, 503, failed.data)
            before_lost = self.rows()
            lost_retry = harness.request('POST', lost_route, expected(lost.data),
                                         token=token, key='native-lost-accept')
            self.assertEqual(lost_retry.status, 422, lost_retry.data)
            self.assertIn('no recorded decision binding', str(lost_retry.data))
            self.assertEqual(self.rows(), before_lost)
            self.assertFalse(owner.existing_acceptances(next(r for r in before_lost if r['id']==lost.data['id'])))
            partial = harness.request('POST', base+'/requirements', {
                'kind':'requirement', 'parent':job, 'title':'Recoverable owner content',
                'description':'The evidence survives an interrupted revision.'},
                token=token, key='native-partial-create')
            self.assertEqual(partial.status, 201, partial.data)
            # Fault injection at the native process boundary: all successful
            # commands still use the actual pinned binary. Fail one revision
            # command after the real decision and real evidence were written.
            self.fault('fail-one-owner-revision')
            partial_route = base+'/requirements/'+partial.data['id']+'/accept'
            interrupted = harness.request('POST', partial_route, expected(partial.data),
                                          token=token, key='native-partial-accept')
            self.assertEqual(interrupted.status, 503, interrupted.data)
            pending = next(r for r in self.rows() if r['id']==partial.data['id'])
            self.assertEqual(set(records.existing_revisions(pending)), {1})
            self.assertEqual(set(owner.existing_acceptances(pending)), {2})
            pending_paths = [p for p in (self.project/requirement_http.JOURNAL).glob('*.json')
                             if load_json(p).get('id')==partial.data['id'] and load_json(p).get('status')=='pending']
            self.assertEqual(len(pending_paths), 1)
            self.assertEqual(load_json(pending_paths[0])['owner_decision']['decision'],
                             owner.existing_acceptances(pending)[2]['decision'])
            fresh = harness.login('owner', 'owner-password-1')[0]
            self.assertNotEqual(fresh, token); token = fresh
            recovered = harness.request('POST', partial_route, expected(partial.data),
                                        token=token, key='native-partial-accept')
            self.assertEqual(recovered.status, 200, recovered.data)
            finished = next(r for r in self.rows() if r['id']==partial.data['id'])
            self.assertEqual(set(records.existing_revisions(finished)), {1,2})
            self.assertEqual(sum(c['text'].startswith(owner.ACCEPTANCE_PREFIX) for c in finished['comments']), 1)
            replay = harness.request('POST', partial_route, expected(partial.data),
                                     token=token, key='native-partial-accept')
            self.assertEqual(replay.data, recovered.data)
            self.assertEqual(next(r for r in self.rows() if r['id']==partial.data['id']), finished)
            with self.subTest(probe='owner and viewer browser pages'):
                if not node:
                    self.skipTest('Node.js is unavailable for the browser probe')
                from test_http_web import run_node_module
                self.fault('fail-one-create')
                page_failed = harness.request('POST', base+'/requirements',
                    dict(fields, title='Page clears absent creation'), token=token, key='native-page-empty-create')
                self.assertEqual(page_failed.status, 503, page_failed.data)
                page = run_node_module(self, node, 'await import(process.argv[1])',
                    (KIT/'tests'/'web_owner_requirements.mjs').as_uri(),
                    (KIT/'web'/'js'/'api.js').as_uri(), (KIT/'web'/'js'/'views'/'owner_requirements.js').as_uri(),
                    'http://127.0.0.1:'+str(harness.port), 'pp', token, viewer)
                self.assertEqual(page.returncode, 0, page.stderr)
                self.assertTrue(all(json.loads(page.stdout).values()), page.stdout)
                legacy = harness.request('GET','/v1/projects/legacy/requirements', token=token)
                self.assertEqual(legacy.status, 200, legacy.data)
                self.assertEqual(legacy.data['jobs'], [])
                enabled_legacy = harness.request('PUT','/v1/projects/legacy/requirements/governance',
                    dict(expected(legacy.data['governance']), mode='simple'), token=token, key='legacy-enable')
                self.assertEqual(enabled_legacy.status, 200, enabled_legacy.data)
                self.assertEqual(harness.request('PUT','/v1/projects/legacy/members/'+viewer_id,
                                                {'role':'viewer'}, token=admin).status, 200)
                page = run_node_module(self, node, 'await import(process.argv[1])',
                    (KIT/'tests'/'web_owner_requirements.mjs').as_uri(),
                    (KIT/'web'/'js'/'api.js').as_uri(), (KIT/'web'/'js'/'views'/'owner_requirements.js').as_uri(),
                    'http://127.0.0.1:'+str(harness.port), 'legacy', token, viewer, 'optional')
                self.assertEqual(page.returncode, 0, page.stderr)
                self.assertTrue(all(json.loads(page.stdout).values()), page.stdout)
            # Real native backup plus the shipped restore-new order: restore
            # governance, inner receipts and outer journal before the merge slot.
            # The browser probe ends by testing governed-mode refusal. Capture
            # a simple-mode backup explicitly for destination owner acceptance.
            current_mode = harness.request('GET', base+'/requirements/governance', token=token)
            self.assertEqual(current_mode.status, 200, current_mode.data)
            restored_simple = harness.request('PUT', base+'/requirements/governance',
                dict(expected(current_mode.data), mode='simple'), token=token, key='restore-simple')
            self.assertEqual(restored_simple.status, 200, restored_simple.data)
            # Synthetic bad host clock in a valid immutable history. Timestamp
            # order changes no authority; the real route warns and still writes.
            future = load_json(self.project/governance.FILE)
            future['revisions'].append(governance.entry(len(future['revisions'])+1,
                future['revisions'][-1]['sha256'], 'simple', account, 'session',
                'native-future-clock-history', at='2999-01-01T00:00:00Z'))
            governance.validate(future)
            from coordination import atomic
            atomic(self.project/governance.FILE, future)
            future_bytes = (self.project/governance.FILE).read_bytes()
            warned = harness.request('GET', base+'/requirements/governance', token=token)
            self.assertEqual(warned.status, 200, warned.data)
            self.assertEqual(warned.data['revision'], len(future['revisions']))
            self.assertEqual(warned.data['mode'], 'simple')
            self.assertEqual(len(warned.data['warnings']), 1)
            clock_create = harness.request('POST', base+'/requirements', dict(fields, title='Behind host clock'),
                                           token=token, key='native-future-create')
            self.assertEqual(clock_create.status, 201, clock_create.data)
            clock_accept = harness.request('POST', base+'/requirements/'+clock_create.data['id']+'/accept',
                expected(clock_create.data), token=token, key='native-future-accept')
            self.assertEqual(clock_accept.status, 200, clock_accept.data)
            self.assertEqual((self.project/governance.FILE).read_bytes(), future_bytes)
            (root/'backups').mkdir(exist_ok=True)
            before_restore = self.rows()
            governance_bytes = (self.project/governance.FILE).read_bytes()
            import requirement_http
            inner = {p.name:p.read_bytes() for p in (self.project/requirement_http.JOURNAL).glob('*.json')}
            for command in (['backup','pp'], ['restore-new','pp','cloned']):
                done = subprocess.run([sys.executable, str(KIT/'admin.py'), '--root', str(root), *command],
                    env=fixture.admin.environment(root), capture_output=True, text=True, timeout=120)
                self.assertEqual(done.returncode, 0, done.stdout+'\n'+done.stderr)
            cloned = root/'projects'/'cloned'
            self.assertEqual((cloned/governance.FILE).read_bytes(), governance_bytes)
            self.assertEqual({p.name:p.read_bytes() for p in (cloned/requirement_http.JOURNAL).glob('*.json')}, inner)
            restored = fixture.endpoint.execute(root, {'project':'cloned','actor':'reader',
                'action':'requirements','args':['get',partial.data['id']]})
            self.assertEqual(restored['returncode'], 0, restored)
            restored_record = json.loads(restored['stdout'])
            self.assertEqual(restored_record['current']['sha256'], recovered.data['sha256'])
            self.assertEqual(restored_record['acceptance']['project'], 'pp')
            from http_authority import OperationJournal, journal_path
            original_journal = OperationJournal(journal_path(self.project))
            copied_journal = OperationJournal(journal_path(cloned))
            operation = owner.existing_acceptances(finished)[2]['operation_id']
            original_receipt = original_journal.lookup(operation)
            self.assertEqual(original_receipt['state'], 'committed')
            self.assertEqual(copied_journal.lookup(operation), original_receipt)
            registered = harness.request('POST','/v1/projects',{'project_id':'cloned','name':'Restored owner flow'}, token=admin)
            self.assertEqual(registered.status, 201, registered.data)
            self.assertEqual(harness.request('PUT','/v1/projects/cloned/members/'+account,
                                           {'role':'owner'}, token=admin).status, 200)
            restored_warning = harness.request('GET', '/v1/projects/cloned/requirements/governance', token=token)
            self.assertEqual(restored_warning.status, 200, restored_warning.data)
            self.assertEqual(len(restored_warning.data['warnings']), 1)
            self.assertEqual(load_json(cloned/requirement_http.JOURNAL/released_path.name), released)
            new = harness.request('POST','/v1/projects/cloned/requirements', {'kind':'requirement','parent':job,
                'title':'Destination owner content','description':'Uses the restored source mode.'}, token=token, key='clone-create')
            self.assertEqual(new.status, 201, new.data)
            accepted_new = harness.request('POST','/v1/projects/cloned/requirements/'+new.data['id']+'/accept',
                expected(new.data), token=token, key='clone-accept')
            self.assertEqual(accepted_new.status, 200, accepted_new.data)
            for suffix in ('/requirements', '/brd', '/requirements/'+new.data['id']):
                view = harness.request('GET','/v1/projects/cloned'+suffix, token=token)
                self.assertEqual(view.status, 200, view.data)
            # Source native evidence remains byte-for-byte unchanged by restore.
            self.assertEqual(self.rows(), before_restore)
            for damage in ('missing', 'unreadable'):
                with self.subTest(recorded_decision=damage):
                    damaged = harness.request('POST', base+'/requirements',
                        dict(fields, title='Decision '+damage), token=token, key='native-'+damage+'-create')
                    self.assertEqual(damaged.status, 201, damaged.data)
                    damaged_route = base+'/requirements/'+damaged.data['id']+'/accept'
                    self.fault('fail-one-owner-evidence')
                    failed = harness.request('POST', damaged_route, expected(damaged.data),
                        token=token, key='native-'+damage+'-accept')
                    self.assertEqual(failed.status, 503, failed.data)
                    paths = [p for p in (self.project/requirement_http.JOURNAL).glob('*.json')
                             if load_json(p).get('id')==damaged.data['id'] and load_json(p).get('status')=='pending']
                    self.assertEqual(len(paths), 1)
                    damaged_id = load_json(paths[0])['owner_decision']['decision']['decision_id']
                    if damage == 'missing':
                        changed_decision = self.bd('delete', damaged_id, '--force')
                    else:
                        metadata = '{"deep":'+'['*750+'0'+']'*750+'}'
                        changed_decision = self.bd('update', damaged_id, '--metadata', metadata)
                    self.assertEqual(changed_decision.returncode, 0, changed_decision.stderr)
                    before_damage = self.rows(); receipt_bytes = paths[0].read_bytes()
                    retry = harness.request('POST', damaged_route, expected(damaged.data),
                        token=token, key='native-'+damage+'-accept')
                    self.assertEqual(retry.status, 422, retry.data)
                    self.assertIn('Recorded requirement decision is missing or unreadable', str(retry.data))
                    self.assertEqual(self.rows(), before_damage)
                    self.assertEqual(paths[0].read_bytes(), receipt_bytes)
                    self.assertFalse(owner.existing_acceptances(next(r for r in before_damage if r['id']==damaged.data['id'])))
            # The offline publication adapter reads the real export. Ordinary
            # metadata beyond comment depth64 remains intact; unrelated native
            # rows beyond the row bound are isolated, selected ones are refused.
            import export_requirements as export_adapter
            ordinary = self.bd('create', 'Ordinary historical metadata', '--json')
            self.assertEqual(ordinary.returncode, 0, ordinary.stderr)
            ordinary_id = json.loads(ordinary.stdout)['id']
            metadata = '{"deep":'+'['*80+'0'+']'*80+'}'
            changed_metadata = self.bd('update', ordinary_id, '--metadata', metadata)
            self.assertEqual(changed_metadata.returncode, 0, changed_metadata.stderr)
            export_path = root/'native-export.jsonl'
            export_path.write_text(self.bd('export','--all').stdout, encoding='utf-8')
            exported = export_adapter.read_export(export_path)
            self.assertFalse(next(r for r in exported if r['id']==ordinary_id).get('malformed'))
            selected = records.existing_revisions(next(r for r in exported if r['id']==clock_create.data['id']))[2]
            selection = dict(schema_version=1,baseline='native-0.1',state='draft',canonical_project='pp',
                job=job,authority='Synthetic owner fixture',hash_convention='Canonical JSON excluding sha256',
                narrative=[],requirements=[export_adapter.reference(selected)],context_ids=[],blocking_ids=[])
            publication = export_adapter.adapt(exported, selection, governance=governance.snapshot(self.project,'pp'))
            self.assertEqual(publication['manifest']['requirements'], [selected])
            # A raw, historical row beyond the JSON guard is visible by native
            # filtered membership; it cannot turn into a missing requirement.
            bad = self.bd('create', 'Unreadable historical requirement', '--labels','requirement', '--json')
            self.assertEqual(bad.returncode, 0, bad.stderr); bad_id = json.loads(bad.stdout)['id']
            past = '{"deep":' + '{"a":'*749 + '1' + '}'*749 + '}'
            nested = self.bd('update', bad_id, '--metadata', past)
            self.assertEqual(nested.returncode, 0, nested.stderr)
            export_path.write_text(self.bd('export','--all').stdout, encoding='utf-8')
            exported = export_adapter.read_export(export_path)
            self.assertTrue(next(r for r in exported if r['id']==bad_id)['malformed'])
            isolated = export_adapter.adapt(exported, selection, governance=governance.snapshot(self.project,'pp'))
            self.assertEqual(isolated['manifest'], publication['manifest'])
            selected_bad = dict(selection, requirements=[dict(id=bad_id,revision=1,sha256='0'*64)])
            with self.assertRaisesRegex(ValueError,'selected requirement row cannot be read'):
                export_adapter.adapt(exported, selected_bad, governance=governance.snapshot(self.project,'pp'))
            for suffix in ('/requirements', '/brd'):
                read = harness.request('GET', base+suffix, token=token)
                self.assertEqual(read.status, 200, read.data)
                self.assertTrue(next(i for i in read.data['items'] if i['id']==bad_id)['unreadable'])
                self.assertTrue(next(i for i in read.data['items'] if i['id']==first['id'])['accepted'])
            refused = harness.request('GET', base+'/requirements/'+bad_id, token=token)
            self.assertEqual(refused.status, 422, refused.data)
            self.assertIn(bad_id, str(refused.data))
        finally:
            harness._stop_server(); harness.doCleanups()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([NativeOwnerTests('test_owner_without_operator_grant_accepts_and_preserves_native_history')])
