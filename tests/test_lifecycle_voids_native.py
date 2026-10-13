"""Pinned native correction and bound-principal raw-write proofs in disposable data."""
import copy
import json
import unittest

import test_bd_label_aliases as fixture
import lifecycle
import lifecycle_voids as voids
from requirements import content_hash


class NativeVoidTests(fixture.RealBdLabelAliasTests):
    # Reuse the disposable real-bd fixture, not its unrelated label-regression methods.
    test_create_label_spellings_refuse_every_reserved_value = None
    test_update_label_spellings_refuse_every_reserved_value = None
    test_ordinary_labels_still_reach_real_bd = None

    def make_fact(self):
        reply = self.bd('create', 'synthetic VOID target', '--json')
        self.assertEqual(reply.returncode, 0, reply.stderr)
        task = json.loads(reply.stdout)['id']
        scope = dict(source_commit='a' * 40, integration_commit='b' * 40,
                     release_id='', environment='synthetic-VOID')
        for dimension, value in [('lifecycle-scope', content_hash(scope)), ('integrated', 'passed')]:
            payload = dict(schema_version=1, operation_id='native-' + task + '-' + dimension,
                           task=task, dimension=dimension, value=value, scope=scope,
                           actor='op', evidence=['synthetic native proof'], provenance='performed')
            result = fixture.endpoint.execute(self.root, dict(project='pp', actor='op', action='lifecycle',
                                                              args=[json.dumps(payload)]))
            self.assertEqual(result['returncode'], 0, result)
        native_rows = self.export_rows()
        target = next(row for row in native_rows if (lifecycle.native_event(row) or {}).get('task') == task
                      and (lifecycle.native_event(row) or {}).get('dimension') == 'integrated')
        return task, target

    def test_host_admin_gate_actual_native_void_exact_retry_and_linked_retraction(self):
        import admin
        task, target = self.make_fact()
        marker = self.root / 'deployment.private.json'
        before = marker.read_bytes()
        cfg = json.loads(before); cfg['operators'] = ['op']
        marker.write_text(json.dumps(cfg))
        try:
            before_rows = self.export_rows()
            with self.assertRaisesRegex(ValueError, 'is off'):
                admin.lifecycle_correction_command(self.root, 'pp', 'void', target['id'], 'op',
                                                   'Synthetic gate proof.', ['synthetic native test'])
            self.assertEqual(self.export_rows(), before_rows)
            self.assertFalse((self.project / voids.DIRECTORY).exists())
            admin.lifecycle_void_switch(self.root, 'on', 'op')
            answer = admin.lifecycle_correction_command(self.root, 'pp', 'void', target['id'], 'op',
                         'Synthetic gate proof.', ['synthetic native test'], 'admin-void-' + task)
            retry = admin.lifecycle_correction_command(self.root, 'pp', 'void', target['id'], 'op',
                         'Synthetic gate proof.', ['synthetic native test'], answer['operation_id'])
            self.assertEqual(retry['comment_id'], answer['comment_id']); self.assertTrue(retry['reconciled'])
            self.assertEqual(voids.context(self.export_rows(), voids.read_journal(self.project))['reasons'],
                             {target['id']: 'voided'})
            with self.assertRaisesRegex(ValueError, 'currently listed operator'):
                admin.lifecycle_correction_command(self.root, 'pp', 'retract', answer['operation_id'],
                         'worker', 'Synthetic refusal.', ['synthetic native test'])
            # Replacement does not undo issued evidence; the current operator
            # can retract it using the actual host CLI and pinned native store.
            cfg = json.loads(marker.read_bytes()); cfg['operators'] = ['new-op']; marker.write_text(json.dumps(cfg))
            self.assertEqual(voids.context(self.export_rows(), voids.read_journal(self.project))['reasons'],
                             {target['id']: 'voided'})
            with self.assertRaisesRegex(ValueError, 'currently listed operator'):
                admin.lifecycle_correction_command(self.root, 'pp', 'retract', answer['operation_id'], 'op',
                             'Synthetic retraction.', ['synthetic native test'], 'admin-retract-' + task)
            import subprocess, sys
            result = subprocess.run([sys.executable, admin.__file__, '--root', str(self.root),
                'lifecycle-event-retract', 'pp', answer['operation_id'], '--actor', 'new-op',
                '--reason', 'Synthetic retraction.', '--evidence', 'synthetic native test',
                '--operation-id', 'admin-retract-' + task], env=admin.environment(self.root),
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['operation'], 'retract')
            self.assertEqual(voids.context(self.export_rows(), voids.read_journal(self.project))['reasons'], {})
            self.assertEqual(next(row for row in self.export_rows() if row['id'] == target['id']), target)
        finally:
            marker.write_bytes(before)

    def test_real_native_comment_and_journal_bind_void_and_retraction_without_changing_event(self):
        task, target = self.make_fact()
        before = copy.deepcopy(target)
        payload = dict(schema_version=1, operation='void', operation_id='native-void-' + task,
                       actor='op', reason='Synthetic operator correction.', evidence=['synthetic independent review'],
                       binding=voids.event_binding(target), target_void=None)
        def native(argv):
            actor = json.loads(argv[3][len(voids.PREFIX):])['actor'] if argv[:2] == ['comments', 'add'] else 'op'
            result = self.bd(*argv, actor=actor)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout
        answer = voids.apply(self.export_rows(), 'op', payload, native, self.project,
                             operators=['op'], enabled=True)
        self.assertFalse(answer['reconciled'])
        after = self.export_rows()
        self.assertEqual(next(row for row in after if row['id'] == target['id']), before)
        captured = voids.snapshot(after, self.project)
        state = next(row for row in lifecycle.project_facts(captured) if row['id'] == task)
        self.assertEqual((state['facts']['integrated']['value'], state['facts']['integrated']['reason']),
                         ('unknown', 'voided'))
        retry = voids.apply(after, 'op', payload, native, self.project, operators=['op'], enabled=True)
        self.assertTrue(retry['reconciled'])
        # Replacement does not undo the existing record; only its linked current-operator retraction does.
        self.assertEqual(next(row for row in lifecycle.project_facts(voids.snapshot(self.export_rows(), self.project))
                              if row['id'] == task)['facts']['integrated']['value'], 'unknown')
        retract = dict(payload, operation='retract', operation_id='native-retract-' + task,
                       actor='replacement', target_void=payload['operation_id'])
        voids.apply(self.export_rows(), 'replacement', retract, native, self.project,
                    operators=['replacement'], enabled=True)
        recovered = next(row for row in lifecycle.project_facts(voids.snapshot(self.export_rows(), self.project))
                         if row['id'] == task)
        self.assertEqual(recovered['facts']['integrated']['value'], 'passed')
        self.assertNotIn('reason', recovered['facts']['integrated'])

    def test_bound_operator_raw_exemption_other_principal_and_unreadable_registry(self):
        task, target = self.make_fact()
        marker = self.root / 'deployment.private.json'
        cfg = json.loads(marker.read_text()); cfg['operators'] = ['op']; marker.write_text(json.dumps(cfg))
        registry = self.project / '.sessions.json'
        registry.write_text(json.dumps(dict(schema_version=1, records={}, owners={
            'op': 'person:owner', 'bound': 'person:owner', 'other': 'person:other'})))
        refused = ['update', target['id'], '--title', 'synthetic changed lifecycle title', '--json']
        before = self.export_rows()
        with self.assertRaisesRegex(ValueError, 'lifecycle action'):
            self.raw(refused, actor='other')
        self.assertEqual(self.export_rows(), before)
        registry.write_text('{')
        with self.assertRaisesRegex(ValueError, 'lifecycle action'):
            self.raw(refused, actor='bound')
        self.assertEqual(self.export_rows(), before)
        registry.write_text(json.dumps(dict(schema_version=1, records={}, owners={
            'op': 'person:owner', 'bound': 'person:owner', 'other': 'person:other'})))
        allowed = self.raw(refused, actor='bound')
        self.assertEqual(allowed['returncode'], 0, allowed)

    def test_raw_native_comment_cannot_forge_correction_envelope(self):
        task, target = self.make_fact()
        before = self.export_rows()
        with self.assertRaisesRegex(ValueError, 'lifecycle correction'):
            self.raw(['comments', 'add', task, voids.PREFIX + '{}', '--json'], actor='other')
        self.assertEqual(self.export_rows(), before)

    def test_damaged_registry_does_not_stop_ordinary_raw_create_update_dep_close_reopen(self):
        task,target=self.make_fact()
        marker=self.root/'deployment.private.json';cfg=json.loads(marker.read_text());cfg['operators']=['op'];marker.write_text(json.dumps(cfg))
        registry=self.project/'.sessions.json';original=registry.read_bytes() if registry.exists() else None
        self.addCleanup(lambda:registry.write_bytes(original) if original is not None else registry.unlink(missing_ok=True))
        registry.write_text('{')
        def ordinary(args):
            result=self.raw(args,actor='worker');self.assertEqual(result['returncode'],0,result)
            return json.loads(result['stdout']) if result['stdout'].strip() else None
        parent=ordinary(['create','ordinary parent','--json'])['id']
        child=ordinary(['create','ordinary child','--json'])['id']
        ordinary(['update',child,'--title','ordinary changed title','--json'])
        ordinary(['dep','add',child,parent,'--json'])
        ordinary(['close',parent,'--json'])
        ordinary(['close',child,'--json'])
        ordinary(['reopen',child,'--json'])
        before=self.export_rows()
        with self.assertRaisesRegex(ValueError,'lifecycle action'):
            self.raw(['update',target['id'],'--title','must refuse lifecycle edit','--json'],actor='worker')
        self.assertEqual(self.export_rows(),before)

    def test_real_refresh_and_view_keep_unknown_voided_fact_and_unintegrated_review(self):
        import admin,review_workflow as reviews
        task,target=self.make_fact()
        self.bd('update',task,'--assignee','worker','--json')
        con=dict(schema_version=1,operation='contribute',operation_id='view-con-'+task,task=task,
                 previous=None,supersedes=None,follows=None,repository='https://example.test/repo.git',
                 commit='a'*40,base_commit='f'*40,delivery=dict(kind='remote',remote='git@example.test:repo.git',branch='own/change'),summary='Synthetic view proof.')
        reviews.validate(con,task)
        result=self.bd('comments','add',task,reviews.PREFIX+json.dumps(con,sort_keys=True,separators=(',',':')),'--json',actor='worker')
        self.assertEqual(result.returncode,0,result.stderr);cid=json.loads(result.stdout)['id']
        approval=dict(schema_version=1,operation='approve',operation_id='view-approve-'+task,task=task,
                      previous=cid,contribution=cid,summary='Independent synthetic review.',assignee_at_approval='worker')
        reviews.validate(approval,task)
        result=self.bd('comments','add',task,reviews.PREFIX+json.dumps(approval,sort_keys=True,separators=(',',':')),'--json',actor='reviewer')
        self.assertEqual(result.returncode,0,result.stderr)
        marker=self.root/'deployment.private.json';cfg=json.loads(marker.read_text());cfg['operators']=['op'];marker.write_text(json.dumps(cfg))
        admin.lifecycle_void_switch(self.root,'on','op')
        admin.lifecycle_correction_command(self.root,'pp','void',target['id'],'op','Synthetic render proof.',['synthetic'],'view-void-'+task)
        result=fixture.endpoint.execute(self.root,dict(project='pp',actor='worker',action='refresh',args=[]))
        self.assertEqual(result['returncode'],0,result)
        result=fixture.endpoint.execute(self.root,dict(project='pp',actor='worker',action='view',args=['CURRENT.md']))
        self.assertEqual(result['returncode'],0,result);page=result['stdout']
        self.assertIn('[unknown](jobs/'+target['id']+'.md) (voided)',page)
        self.assertIn('| '+task+' | worker | open | awaiting-integration |',page)
        self.assertNotIn('| '+task+' | worker | open | integrated |',page)
        self.assertEqual(next(row for row in self.export_rows() if row['id']==target['id']),target)

    def test_void_racing_new_fact_same_scope_serializes_and_new_fact_remains_passed(self):
        import admin,threading
        from unittest import mock
        from http_authority import NativeRunner
        task,target=self.make_fact();marker=self.root/'deployment.private.json';cfg=json.loads(marker.read_text());cfg['operators']=['op'];marker.write_text(json.dumps(cfg));admin.lifecycle_void_switch(self.root,'on','op')
        new=dict(lifecycle.native_event(target)['payload'],operation_id='racing-new-'+task)
        entered=threading.Event();release=threading.Event();requested=threading.Event();native_fact=threading.Event();answers={};errors=[]
        real_publish=voids.publish;real_call=NativeRunner.__call__
        def paused(project,entry):
            entered.set()
            if not release.wait(20):raise AssertionError('synthetic publication barrier timed out')
            return real_publish(project,entry)
        def observed(runner,args):
            if threading.current_thread().name=='F0-new-fact':native_fact.set()
            return real_call(runner,args)
        def correct():
            try:answers['void']=admin.lifecycle_correction_command(self.root,'pp','void',target['id'],'op','Synthetic race.',['synthetic'],'racing-void-'+task)
            except Exception as error:errors.append(error)
        def record():
            requested.set()
            try:answers['fact']=fixture.endpoint.execute(self.root,dict(project='pp',actor='op',action='lifecycle',args=[json.dumps(new)]))
            except Exception as error:errors.append(error)
        with mock.patch.object(voids,'publish',side_effect=paused),mock.patch.object(NativeRunner,'__call__',observed):
            one=threading.Thread(target=correct,name='F0-void');two=threading.Thread(target=record,name='F0-new-fact')
            one.start()
            try:
                self.assertTrue(entered.wait(20),'void must reach the held native publication')
                two.start();self.assertTrue(requested.wait(5))
                self.assertFalse(native_fact.wait(.2),'the fact must not read/write native data while VOID holds the project lock')
            finally:
                release.set();one.join(30)
                if two.ident is not None:two.join(30)
        self.assertFalse(one.is_alive());self.assertFalse(two.is_alive());self.assertEqual(errors,[])
        self.assertEqual(answers['fact']['returncode'],0,answers)
        rows=self.export_rows();facts=next(row for row in lifecycle.project_facts(voids.snapshot(rows,self.project)) if row['id']==task)
        self.assertEqual(facts['facts']['integrated']['value'],'passed')
        self.assertNotEqual(facts['facts']['integrated']['event_id'],target['id'])
        self.assertEqual(voids.context(rows,voids.read_journal(self.project))['reasons'][target['id']],'voided')
        self.assertEqual(next(row for row in rows if row['id']==target['id']),target)

    def omitted_backup_restore(self,old_kit=None,without_coordination=False):
        import admin,subprocess,sys
        from pathlib import Path
        task,target=self.make_fact()
        marker=self.root/'deployment.private.json';cfg=json.loads(marker.read_text());cfg['operators']=['op'];marker.write_text(json.dumps(cfg))
        admin.lifecycle_void_switch(self.root,'on','op')
        admin.lifecycle_correction_command(self.root,'pp','void',target['id'],'op','Synthetic missing-family restore.',['synthetic'],'restore-void-'+task)
        before=self.export_rows();backups=self.root/'backups';backups.mkdir(exist_ok=True)
        init=self.bd('backup','init',str(backups/'pp'));self.assertEqual(init.returncode,0,init.stderr)
        kit=Path(old_kit) if old_kit else Path(admin.__file__).parent
        def command(script,*args):
            return subprocess.run([sys.executable,str(script),'--root',str(self.root),*args],
                       env=admin.environment(self.root),capture_output=True,text=True,timeout=180)
        result=command(kit/'admin.py','backup','pp');self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        sidecar=backups/'pp.coordination.json';bundle=json.loads(sidecar.read_text())
        if old_kit:
            self.assertFalse(any(k==voids.MANIFEST or k.startswith(voids.DIRECTORY+'/') for k in bundle['files']))
        else:
            self.assertIn(voids.MANIFEST,bundle['files'])
            bundle['files']={k:v for k,v in bundle['files'].items() if k!=voids.MANIFEST and not k.startswith(voids.DIRECTORY+'/')}
            sidecar.write_text(json.dumps(bundle))
        restored_name='plaincopy' if without_coordination else 'oldercopy' if old_kit else 'tamperedcopy'
        if without_coordination:
            # The flag is only admitted for unusable sidecars. First pin that
            # pre-native refusal, then exercise the actual native-only restore.
            refused=command(Path(admin.__file__),'restore-new','pp',restored_name,'--without-coordination')
            self.assertNotEqual(refused.returncode,0)
            self.assertIn('--without-coordination is only for',refused.stderr)
            self.assertFalse((self.root/'projects'/restored_name).exists())
            sidecar.write_text('{')
            fallback=admin.last_complete_sidecar_path(self.root,'pp')
            if fallback.exists():fallback.write_text('{')
        result=command(Path(admin.__file__),'restore-new','pp',restored_name,
                       *(['--without-coordination'] if without_coordination else []))
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('Native lifecycle correction comments exist',result.stderr)
        destination=self.root/'projects'/restored_name
        self.assertTrue((destination/voids.RESTORE_MARKER).is_file())
        restored=[json.loads(line) for line in admin.run_bd(self.root,restored_name,['export','--all']).splitlines() if line.strip()]
        state=next(f for f in lifecycle.project_facts(voids.snapshot(restored,destination)) if f['id']==task)
        self.assertEqual((state['facts']['integrated']['value'],state['facts']['integrated']['reason']),('unknown',voids.UNKNOWN_JOURNAL))
        audit=voids.audit(voids.snapshot(restored,destination));self.assertEqual(audit['correction_status'],'damaged')
        self.assertGreaterEqual(audit['incomplete_correction_count'],1)
        with self.assertRaisesRegex(ValueError,'unreadable/incomplete'):
            admin.backup_project(self.root,restored_name)
        self.assertEqual(next(r for r in restored if r['id']==target['id']),target)
        self.assertEqual(self.export_rows(),before)

    def test_native_restore_refuses_whole_family_and_manifest_removed_together(self):
        self.omitted_backup_restore()

    def test_native_without_coordination_cannot_override_a_missing_void_family(self):
        self.omitted_backup_restore(without_coordination=True)

    def test_incomplete_native_comment_recovery_from_its_retained_payload_resumes_backup(self):
        import admin
        from unittest import mock
        task,target=self.make_fact();cfg=json.loads((self.root/'deployment.private.json').read_text())
        cfg['operators']=['op'];(self.root/'deployment.private.json').write_text(json.dumps(cfg))
        admin.lifecycle_void_switch(self.root,'on','op')
        with mock.patch.object(voids,'publish',side_effect=OSError('synthetic publication cut')):
            with self.assertRaisesRegex(ValueError,'uncertain'):
                admin.lifecycle_correction_command(self.root,'pp','void',target['id'],'op',
                    'Synthetic words recovered from native.',['synthetic round-two proof'],'recover-'+task)
        before=self.export_rows()
        with self.assertRaisesRegex(ValueError,'VOID family is missing|native correction comments without'):
            voids.backup_files(self.project,before)
        comment=next(c for row in before if row['id']==task for c in row.get('comments',[])
                     if c['text'].startswith(voids.PREFIX))
        recovered=json.loads(comment['text'][len(voids.PREFIX):])
        answer=admin.lifecycle_correction_command(self.root,'pp','void',recovered['binding']['event_id'],
            recovered['actor'],recovered['reason'],recovered['evidence'],recovered['operation_id'])
        self.assertTrue(answer['reconciled']);self.assertEqual(answer['comment_id'],str(comment['id']))
        self.assertEqual(self.export_rows(),before)
        self.assertIn(voids.MANIFEST,voids.backup_files(self.project,before))
        self.assertEqual(voids.audit(voids.snapshot(before,self.project))['incomplete_correction_count'],0)

    def test_raw_guard_keeps_the_complete_released_valueerror_line(self):
        import admin,subprocess,sys
        before=self.export_rows()
        request=dict(project='pp',actor='worker',action='bd',
                     args=['create','synthetic refused','--labels','integrated:passed','--json'])
        result=subprocess.run([sys.executable,fixture.endpoint.__file__,'--root',str(self.root)],
                    input=json.dumps(request),env=admin.environment(self.root),capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr)
        answer=json.loads(result.stdout);self.assertEqual(answer['returncode'],2)
        self.assertEqual(answer['stderr'].splitlines()[0],
            'ValueError: Reserved lifecycle namespace: lifecycle dimension, scope, enabled and live labels '
            'require a lifecycle write. Use the dedicated lifecycle action (lifecycle.py record); no native write was attempted.')
        self.assertEqual(self.export_rows(),before)

    def test_real_export_offline_list_parity_and_wrong_journal_paths(self):
        import admin,os,subprocess,sys
        from pathlib import Path
        base=os.environ.get('ORCHESTRA_F0_REVIEW_BASE')
        if not base:self.skipTest('requires pinned pre-VOID kit for main-beside-tip offline CLI proof; never run in ordinary CI')
        task,target=self.make_fact();exported=self.project/'round-two-export.jsonl'
        def read(script,journal=None):
            exported.write_text(''.join(json.dumps(r)+'\n' for r in self.export_rows()))
            args=[sys.executable,str(script),'list','--export',str(exported)]
            if journal is not None:args+=['--journal',str(journal)]
            return subprocess.run(args,env=admin.environment(self.root),capture_output=True,text=True,timeout=60)
        main=read(Path(base)/'lifecycle.py');tip=read(Path(lifecycle.__file__))
        self.assertEqual(main.returncode,0,main.stderr);self.assertEqual(tip.returncode,0,tip.stderr)
        current=json.loads(tip.stdout)
        for row in current:
            self.assertFalse(row.pop('corrections_consulted',True));self.assertIn('not consulted',row.pop('correction_note',''))
        # Other tasks in this shared fixture already have correction comments;
        # their deliberate unknown result is not a legacy-parity claim.
        self.assertEqual(next(r for r in current if r['id']==task),
                         next(r for r in json.loads(main.stdout) if r['id']==task))
        cfg=json.loads((self.root/'deployment.private.json').read_text());cfg['operators']=['op'];(self.root/'deployment.private.json').write_text(json.dumps(cfg))
        admin.lifecycle_void_switch(self.root,'on','op')
        admin.lifecycle_correction_command(self.root,'pp','void',target['id'],'op',
              'Synthetic offline CLI.',['synthetic round-two proof'],'offline-'+task)
        empty=self.project/'no-family';empty.mkdir()
        foreign=self.project/'foreign-project';foreign.mkdir();(foreign/'.beads').mkdir()
        (foreign/'.beads'/'metadata.json').write_text(json.dumps({'database':'foreign'}))
        for path in (None,empty,foreign):
            answer=read(Path(lifecycle.__file__),path);self.assertEqual(answer.returncode,0,answer.stderr)
            row=next(r for r in json.loads(answer.stdout) if r['id']==task)
            self.assertFalse(row.get('corrections_consulted',True))
            self.assertEqual(row['facts']['integrated'].get('reason'),'lifecycle-corrections-not-consulted')
        answer=read(Path(lifecycle.__file__),self.project/'missing');self.assertNotEqual(answer.returncode,0)
        self.assertIn('existing project directory',answer.stderr)
        answer=read(Path(lifecycle.__file__),self.project);self.assertEqual(answer.returncode,0,answer.stderr)
        row=next(r for r in json.loads(answer.stdout) if r['id']==task)
        self.assertTrue(row.get('corrections_consulted',False));self.assertEqual(row['facts']['integrated'].get('reason'),'voided')

    def test_real_changed_target_keeps_unknown_and_stops_backup(self):
        import admin
        task,target=self.make_fact();cfg=json.loads((self.root/'deployment.private.json').read_text())
        cfg['operators']=['op'];(self.root/'deployment.private.json').write_text(json.dumps(cfg))
        admin.lifecycle_void_switch(self.root,'on','op')
        admin.lifecycle_correction_command(self.root,'pp','void',target['id'],'op',
              'Synthetic target change.',['synthetic round-two proof'],'changed-'+task)
        result=self.bd('update',target['id'],'--title','synthetic changed native event','--json',actor='op')
        self.assertEqual(result.returncode,0,result.stderr)
        self.addCleanup(lambda:self.assertEqual(
            self.bd('update',target['id'],'--title',target['title'],'--json',actor='op').returncode,0))
        captured=voids.snapshot(self.export_rows(),self.project)
        fact=next(f for f in lifecycle.project_facts(captured) if f['id']==task)['facts']['integrated']
        self.assertEqual(fact.get('reason'),'lifecycle-void-target-changed');self.assertEqual(fact['value'],'unknown')
        self.assertEqual(voids.audit(captured)['damaged_correction_count'],1)
        with self.assertRaisesRegex(ValueError,'native target/comment generation'):
            voids.backup_files(self.project,self.export_rows())

    def test_native_restore_refuses_backup_created_by_pre_void_kit(self):
        import os
        base=os.environ.get('ORCHESTRA_F0_REVIEW_BASE')
        if not base:self.skipTest('requires explicit pinned pre-VOID kit for native backup compatibility')
        self.omitted_backup_restore(base)


if __name__ == '__main__':
    unittest.main()
