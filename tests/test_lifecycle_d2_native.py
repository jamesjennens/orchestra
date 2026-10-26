"""D2 host-only history reads on an owned native fixture; no production data."""
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import timedelta
import unittest
from unittest import mock

import test_bd_label_aliases as fixture
import lifecycle
import lifecycle_audit as audit
import lifecycle_voids as voids
import native
import review_workflow as reviews
from requirements import canonical_bytes,content_hash


@unittest.skipIf(fixture.endpoint is None,'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None,'no pinned native bd (set ORCHESTRA_BD_BIN)')
class NativeAuditTests(fixture.RealBdLabelAliasTests):
    test_create_label_spellings_refuse_every_reserved_value=None
    test_update_label_spellings_refuse_every_reserved_value=None
    test_ordinary_labels_still_reach_real_bd=None
    test_a_native_call_that_never_answers_fails_with_its_command=None

    def make_facts(self):
        made=self.bd('create','Synthetic D2 audit target','--assignee','worker','--json')
        self.assertEqual(made.returncode,0,made.stderr);task=json.loads(made.stdout)['id']
        scope=dict(source_commit='a'*40,integration_commit='b'*40,release_id='',environment='test')
        last=None
        for operation,dimension,value in [('scope','lifecycle-scope',content_hash(scope)),('first','tested','passed'),('repeat','tested','passed')]:
            payload=dict(schema_version=1,operation_id=task+'-'+operation,task=task,dimension=dimension,
                         value=value,scope=scope,actor='worker',evidence=['Synthetic D2 native proof'],provenance='performed')
            result=fixture.endpoint.execute(self.root,dict(project='pp',actor='worker',action='lifecycle',args=[json.dumps(payload)]))
            self.assertEqual(result['returncode'],0,result);last=json.loads(result['stdout'])['event_id']
        return task,last

    def inventory(self):
        return {str(p.relative_to(self.project)):p.read_bytes() for p in self.project.rglob('*')
                if p.is_file() and any(part.startswith(('.lifecycle-','.web-lifecycle-')) for part in p.relative_to(self.project).parts)}

    # Persistence latency must not replace the semantic result under test with
    # a competing history timeout. The deadline case below still sets zero.
    @mock.patch.object(audit,'HISTORY_SECONDS',600.0)
    def test_native_pairs_creation_commit_and_read_only_adapter(self):
        task,event=self.make_facts();before=canonical_bytes(self.export_rows());families=self.inventory();calls=[]
        real=native.run_bounded
        def observed(command,env,max_bytes,timeout):
            calls.append(list(command));return real(command,env,max_bytes,timeout)
        with mock.patch.object(native,'run_bounded',observed):
            report=fixture.admin.lifecycle_audit_command(self.root,'pp',limit=100)
        self.assertTrue(calls);self.assertTrue(all('--readonly' in c and 'history' in c and '--limit' in c for c in calls))
        self.assertLessEqual(len(calls),audit.HISTORY_CALL_CAP)
        pair=next((p for p in report['kit_intermediates'] if p['following_event_id']==event),None)
        self.assertIsNotNone(pair,json.dumps(dict(unpaired=report['unpaired_intermediates'],events=[dict(id=r['id'],created_at=r.get('created_at')) for r in self.export_rows() if r.get('issue_type')=='event' and r['id'].startswith(task+'.')]),sort_keys=True))
        item=next(i for i in report['items'] if i['event_id']==event)
        self.assertEqual(item['historical_assignment']['state'],'known',item)
        self.assertEqual(item['historical_assignment']['assignee'],'worker')
        self.assertIsNone(item['historical_assignment']['assigned_by'])
        self.assertEqual(before,canonical_bytes(self.export_rows()));self.assertEqual(families,self.inventory())

    def test_native_version_and_whole_call_caps_answer_unknown(self):
        task,event=self.make_facts();before=canonical_bytes(self.export_rows())
        # Coordinator fix 2026-10-25: a generous time budget, so on a loaded host the whole-history time
        # budget cannot stop the audit before the version cap this test is about (it did once in 1024j).
        with mock.patch.object(audit,'HISTORY_VERSION_CAP',1),mock.patch.object(audit,'HISTORY_SECONDS',600.0):
            report=fixture.admin.lifecycle_audit_command(self.root,'pp',limit=100)
        item=next(i for i in report['items'] if i['event_id']==event)
        self.assertEqual(item['historical_assignment']['state'],'unknown')
        self.assertIn('cap',item['historical_assignment']['reason'])
        with mock.patch.object(audit,'HISTORY_CALL_CAP',0),mock.patch.object(native,'run_bounded',side_effect=AssertionError('no history call permitted')):
            report=fixture.admin.lifecycle_audit_command(self.root,'pp',limit=100)
        self.assertEqual(report['historical_read_coverage']['read_calls'],0)
        self.assertEqual(report['historical_read_coverage']['stopped_reason'],'whole-audit-history-call-cap')
        self.assertEqual(before,canonical_bytes(self.export_rows()))

    def test_native_byte_budget_and_deadline_stay_read_only(self):
        task,event=self.make_facts();before=canonical_bytes(self.export_rows())
        with mock.patch.multiple(audit,HISTORY_BYTES_CAP=16,HISTORY_TOTAL_BYTES_CAP=32,HISTORY_SECONDS=600.0):
            report=fixture.admin.lifecycle_audit_command(self.root,'pp',limit=100)
        self.assertLessEqual(report['historical_read_coverage']['read_calls'],2)
        self.assertEqual(report['historical_read_coverage']['stopped_reason'],'whole-audit-history-byte-cap')
        with mock.patch.object(audit,'HISTORY_SECONDS',0):
            report=fixture.admin.lifecycle_audit_command(self.root,'pp',limit=100)
        self.assertEqual(report['historical_read_coverage']['read_calls'],0)
        self.assertEqual(report['historical_read_coverage']['stopped_reason'],'whole-audit-history-time-budget')
        self.assertEqual(before,canonical_bytes(self.export_rows()))

    @mock.patch.object(audit,'HISTORY_SECONDS',600.0)
    def test_native_unknown_history_cases_never_infer_an_assignment(self):
        task,event=self.make_facts();before=canonical_bytes(self.export_rows())
        selected=[r for r in self.export_rows() if r['id']==task or any(d.get('type')=='parent-child' and d.get('depends_on_id')==task for d in r.get('dependencies') or [])]
        child=self.bd('history',event,'--json','--limit','0');self.assertEqual(child.returncode,0,child.stderr)
        creation=json.loads(child.stdout)[-1]['CommitHash'];real=native.run_bounded
        cases={
            'absent':(lambda v:[],'native-history-absent'),
            'damaged':(lambda v:[{}],'native-history-damaged'),
            'tie':(lambda v:[dict(v[0],CommitDate=v[1]['CommitDate']),*v[1:]],'native-history-order-tie'),
            'head':(lambda v:[dict(v[0],Issue=dict(v[0]['Issue'],assignee='unrecorded-holder')),*v[1:]],'native-history-current-head-mismatch'),
            'gap':(lambda v:[entry for entry in v if entry['CommitHash']!=creation],'native-assignment-commit-gap'),
            'commit-mismatch':(lambda v:[dict(e,CommitDate=(reviews._recorded_at(e['CommitDate'])+timedelta(microseconds=1)).isoformat()) if e['CommitHash']==creation else e for e in v],'native-history-commit-mismatch'),
            'wrong-task':(lambda v:[dict(v[0],Issue=dict(v[0]['Issue'],id='foreign')),*v[1:]],'native-history-damaged'),
        }
        for case,(mutate,reason) in cases.items():
            def injected(command,env,max_bytes,timeout):
                done,over=real(command,env,max_bytes,timeout)
                if command[command.index('history')+1]==task and done.returncode==0 and not over:
                    done.stdout=json.dumps(mutate(json.loads(done.stdout)))
                return done,over
            with self.subTest(case=case),mock.patch.object(lifecycle,'read_event_rows',return_value=selected),mock.patch.object(native,'run_bounded',injected):
                report=fixture.admin.lifecycle_audit_command(self.root,'pp',limit=100)
                h=next(i['historical_assignment'] for i in report['items'] if i['event_id']==event)
                self.assertEqual((h['state'],h['reason']),('unknown',reason));self.assertIsNone(h['assigned_by'])
        with mock.patch.object(audit,'HISTORY_TOTAL_VERSION_CAP',1):
            report=fixture.admin.lifecycle_audit_command(self.root,'pp',limit=100)
        self.assertEqual(report['historical_read_coverage']['returned_versions'],1)
        self.assertEqual(report['historical_read_coverage']['read_calls'],1)
        with mock.patch.object(audit,'HISTORY_CALL_SECONDS',0.0001),mock.patch.object(lifecycle,'read_event_rows',return_value=selected):
            report=fixture.admin.lifecycle_audit_command(self.root,'pp',limit=100)
        self.assertTrue(all(i['historical_assignment']['state']=='unknown' for i in report['items']))
        self.assertEqual(before,canonical_bytes(self.export_rows()))

    def test_actual_base_reader_backup_restore_and_all_off_parity(self):
        # New read diagnostics create no family or format. Exercise the ACTUAL
        # qualification-base reader and backup/restore, not an imitation validator.
        from test_lifecycle_voids import resolve_kit
        base='6be804d4f029cfb3ec2340e2cacaedea7026e0d6'
        old=resolve_kit(base)
        if old is None:self.skipTest('actual D2 qualification-base kit is not available')
        task,event=self.make_facts();rows=self.export_rows();before=canonical_bytes(rows)
        cfg_path=self.root/'deployment.private.json';cfg=json.loads(cfg_path.read_text())
        cfg['operators']=['op'];cfg.update({k:False for k in ('lifecycle_authors','independent_approval','coordinator_writes')});cfg_path.write_text(json.dumps(cfg))
        self.assertTrue(all(value is False for value in fixture.admin.coordinator_rules(self.root).values()))
        with tempfile.TemporaryDirectory(prefix='d2-old-reader-') as scratch:
            snapshot=Path(scratch)/'rows.json';snapshot.write_text(json.dumps(rows))
            code='import json,sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import lifecycle;rows=json.loads(Path(sys.argv[2]).read_text());print(json.dumps(lifecycle.project_facts(rows),sort_keys=True))'
            done=subprocess.run([sys.executable,'-c',code,str(old),str(snapshot)],capture_output=True,text=True,timeout=60)
            self.assertEqual(done.returncode,0,done.stderr);expected=json.loads(done.stdout)
            report=fixture.admin.lifecycle_audit_command(self.root,'pp',limit=100)
            self.assertEqual(lifecycle.project_facts(self.export_rows()),expected)
            self.assertEqual(canonical_bytes(self.export_rows()),before)
            backups=self.root/'backups';backups.mkdir(exist_ok=True)
            init=self.bd('backup','init',str(backups/'pp'));self.assertEqual(init.returncode,0,init.stderr)
            def run(kit,*args):
                return subprocess.run([sys.executable,str(Path(kit)/'admin.py'),'--root',str(self.root),*args],
                    env=fixture.admin.environment(self.root),capture_output=True,text=True,timeout=180)
            current=Path(lifecycle.__file__).resolve().parent
            for kit in (old,current):
                done=run(kit,'backup','pp');self.assertEqual(done.returncode,0,done.stdout+done.stderr)
            done=run(old,'restore-new','pp','oldcopy');self.assertEqual(done.returncode,0,done.stdout+done.stderr)
            restored=[json.loads(line) for line in fixture.admin.run_bd(self.root,'oldcopy',['export','--all']).splitlines() if line.strip()]
            restored_facts=lifecycle.project_facts(restored)
            original_ids={item['id'] for item in expected}
            self.assertEqual([item for item in restored_facts if item['id'] in original_ids],expected)
            # Supported restore provisions its own merge slot at this base.
            extras=[item for item in restored_facts if item['id'] not in original_ids]
            self.assertEqual({item['id'] for item in extras},{'pp-merge-slot'})
            self.assertTrue(all(not item['has_lifecycle'] for item in extras))
            from coordination import is_merge_slot
            self.assertTrue(all(is_merge_slot(row) for row in restored if row['id'] in {item['id'] for item in extras}))
            self.assertEqual(canonical_bytes(self.export_rows()),before)


from test_coordinator_rules_endpoint import RulesHelpers
import test_web_merge_slot as slot_fixture
import task_assignment


@unittest.skipIf(fixture.endpoint is None,'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None,'no pinned native bd (set ORCHESTRA_BD_BIN)')
class NativeAssignmentAuditTests(RulesHelpers,unittest.TestCase):
    web=slot_fixture.NativeWebSlotTests.web
    inventory=NativeAuditTests.inventory

    @mock.patch.object(audit,'HISTORY_SECONDS',600.0)
    def test_actual_web_notice_then_raw_assignment_stays_attributed_only_when_proved(self):
        harness,owner=self.web();previous=harness.admin_user['id']
        target=harness.create_account(owner,'d2-assignee','native-assignment-password-1')
        membership=harness.request('PUT','/v1/projects/pp/members/'+target,dict(role='contributor'),token=owner)
        self.assertEqual(membership.status,200,membership.data)
        task=self.task('Synthetic D2 web assignment',assignee=previous)
        assigned=harness.request('POST','/v1/projects/pp/tasks/'+task+'/assign',
            dict(expected_assignee=previous,assignee=target,reason='Synthetic D2 assignment proof',
                 allow_pending_review=False,allow_held_slot=False),token=owner,key='d2-actual-web-assignment')
        self.assertEqual(assigned.status,200,assigned.data)
        issue=next(row for row in self.rows() if row['id']==task)
        comments=[c for c in issue.get('comments') or [] if c.get('text','').startswith(task_assignment.PREFIX)]
        self.assertEqual(len(comments),1)
        notice=task_assignment.parse_comment(comments[0],project='pp',task=task)
        self.assertIsNotNone(notice);self.assertEqual((notice['from'],notice['to'],notice['assigned_by']),(previous,target,previous))
        native_scope=dict(source_commit='a'*40,integration_commit='b'*40,release_id='',environment='test')
        result=self.lifecycle('synthetic-worker','lifecycle-scope',task,scope=native_scope,operation_id='d2-web-scope')
        self.assertEqual(result['returncode'],0,result)
        result=self.lifecycle('synthetic-worker','tested',task,scope=native_scope,operation_id='d2-web-tested')
        self.assertEqual(result['returncode'],0,result);event=json.loads(result['stdout'])['event_id']
        before=canonical_bytes(self.rows());families=self.inventory()
        report=fixture.admin.lifecycle_audit_command(self.root,'pp',limit=100)
        historical=next(item['historical_assignment'] for item in report['items'] if item['event_id']==event)
        self.assertEqual((historical['state'],historical['assignee']),('known',target))
        # Native rounding may leave notice/transition times ambiguous. Such an
        # attribution stays unknown; a service committer is never the assigner.
        if historical['assigned_by'] is not None:
            self.assertEqual(historical['assigned_by'],notice['assigned_by'])
            self.assertEqual(historical['notice_ids'],[str(comments[0]['id'])])
        else:self.assertIsNotNone(historical['who_assigned_reason'])
        self.assertEqual(before,canonical_bytes(self.rows()));self.assertEqual(families,self.inventory())
        changed=self.r.bd('update',task,'--assignee','raw-holder','--json')
        self.assertEqual(changed.returncode,0,changed.stderr)
        result=self.lifecycle('raw-holder','tested',task,scope=native_scope,operation_id='d2-raw-tested')
        self.assertEqual(result['returncode'],0,result);raw_event=json.loads(result['stdout'])['event_id']
        before=canonical_bytes(self.rows());report=fixture.admin.lifecycle_audit_command(self.root,'pp',limit=100)
        after={item['event_id']:item for item in report['items']}
        self.assertEqual(after[raw_event]['historical_assignment']['assignee'],'raw-holder')
        self.assertEqual(after[raw_event]['historical_assignment']['state'],'known')
        self.assertIsNone(after[raw_event]['historical_assignment']['assigned_by'])
        self.assertEqual(after[event]['historical_assignment']['assignee'],target)
        self.assertEqual(after[event]['current_assignee'],'raw-holder')
        self.assertEqual(before,canonical_bytes(self.rows()));self.assertEqual(families,self.inventory())


@unittest.skipIf(fixture.endpoint is None,'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None,'no pinned native bd (set ORCHESTRA_BD_BIN)')
class NativeReleaseAuditTests(RulesHelpers,unittest.TestCase):
    inventory=NativeAuditTests.inventory
    web=slot_fixture.NativeWebSlotTests.web

    def authority(self,operation):
        done=subprocess.run([sys.executable,str(Path(lifecycle.__file__).with_name('admin.py')),
            '--root',str(self.root),'operators',operation,self.operator,'--confirm-revoke'],
            env=fixture.admin.environment(self.root),capture_output=True,text=True,timeout=30)
        self.assertEqual(done.returncode,0,done.stdout+done.stderr)

    def prepare(self,case):
        import test_lifecycle_d2_audit as units
        import test_lifecycle_release as release
        import recovery
        self.operator=release.OPERATOR;self.deployment(operators=[self.operator])
        task=self.task('Synthetic native R2 '+case,assignee=release.ACTOR)
        scope=dict(source_commit='a'*40,integration_commit='b'*40,release_id='',environment='test')
        for actor,dimension in ((release.ACTOR,'lifecycle-scope'),(self.operator,'integrated')):
            answer=self.lifecycle(actor,dimension,task,scope=scope);self.assertEqual(answer['returncode'],0,answer)
        if case in ('attest','fork'):
            template=units.ReleaseDiagnosticsTests().failed_attestation_fork('3' if case=='attest' else '4',task).rows[0]['comments']
        else:
            template=units.ReleaseRegressions().held_with_void(task).rows[0]['comments']
            if case=='release':
                template=template[:3]
                payload=dict(schema_version=1,operation='release-hold',operation_id='native-release',task=task,
                    previous='3',contribution='1',hold='3',reason='Synthetic native release')
                target=dict(id='4',author=self.operator,text=reviews.PREFIX+canonical_bytes(payload).decode())
                contribution=json.loads(template[0]['text'][len(reviews.PREFIX):])
                fork=dict(contribution,operation_id='native-release-fork',previous='3',supersedes='1',commit='9'*40)
                template += [target,dict(id='repair',author=release.ACTOR,text=reviews.PREFIX+canonical_bytes(fork).decode())]
                payload=dict(schema_version=1,operation=recovery.OPERATION,operation_id='native-release-void',task=task,
                    target='4',target_kind='contribution-review',target_sha256=recovery.digest(target['text']),original=target['text'],
                    reason='Synthetic native repair',disposition='void',operator=self.operator)
                template.append(dict(id='void-comment',author=self.operator,text=recovery.PREFIX+canonical_bytes(payload).decode()))
        ids={};texts={};void_id=None
        for comment in template:
            prefix=recovery.PREFIX if comment['text'].startswith(recovery.PREFIX) else reviews.PREFIX
            body=json.loads(comment['text'][len(prefix):])
            for key in ('previous','supersedes','contribution','hold','target'):
                if body.get(key) in ids:body[key]=ids[body[key]]
            author=comment['author']
            if prefix==recovery.PREFIX:
                body['original']=texts[body['target']];body['target_sha256']=recovery.digest(body['original'])
                if case=='forged':body['operator']='never-operator';author='never-operator'
            text=prefix+canonical_bytes(body).decode()
            made=self.r.bd('comments','add',task,text,'--json',actor=author);self.assertEqual(made.returncode,0,made.stderr)
            identity=str(json.loads(made.stdout)['id']);ids[comment['id']]=identity;texts[identity]=text
            if prefix==recovery.PREFIX:void_id=identity
        baseline=fixture.admin.authority_change_baseline(dict(operators=[self.operator],verifiers=[]),None,
            'Synthetic retained baseline',at='2026-10-01T00:00:00Z')
        audit_path=self.root/fixture.admin.AUTHORITY_CHANGES_AUDIT
        if audit_path.exists() or audit_path.is_symlink():audit_path.unlink()
        audit_path.write_text(json.dumps(dict(schema_version=1,entries=[],baseline=baseline)))
        # Native comments can round upward; the existing authority writer uses
        # whole seconds. This bounded fixture delay clears both rounding edges.
        import time
        time.sleep(2.1)
        self.authority('remove')
        return task,void_id,scope

    def query(self):
        import test_lifecycle_release as release
        payload=dict(release.release_payload([]),dimension=lifecycle.RELEASE_QUERY,actor='synthetic-reader')
        answer=self.endpoint('lifecycle','synthetic-reader',[json.dumps(payload)])
        self.assertEqual(answer['returncode'],0,answer)
        return json.loads(answer['stdout'])

    def test_real_web_release_query_supplies_retained_operator_history(self):
        harness,token=self.web()
        task,void_id,scope=self.prepare('attest')
        before=canonical_bytes(self.rows());families=self.inventory()
        path=self.root/fixture.admin.AUTHORITY_CHANGES_AUDIT;trail=path.read_bytes()
        # The real HTTP EndpointBackend invokes action=release/operation=query,
        # not the separate action=lifecycle helper exercised by query() above.
        reply=harness.request('POST','/v1/projects/pp/releases/query',dict(scope=dict(
            source_commit='',integration_commit='5'*40,release_id='',environment='production')),
            token=token,key='synthetic-d2-host-release-query')
        self.assertEqual(reply.status,200,reply.data)
        self.assertTrue(reply.data['query']);self.assertEqual(reply.data['environment'],'production')
        notes=[n for n in reply.data.get('review_void_diagnostics',[]) if n['task']==task]
        self.assertEqual(len(notes),1,reply.data);note=notes[0]
        self.assertEqual(note['void_id'],void_id)
        self.assertEqual(note['target_operation'],'attest')
        self.assertEqual(note['historical_operator_eligibility'],'eligible')
        self.assertEqual(note['eligibility_basis'],'retained-authority-trail')
        self.assertEqual(note['reason'],'currently-ineligible-review-void')
        self.assertEqual((note['current_result'],note['counterfactual_result']),('unreadable','unheld'))
        self.assertEqual(before,canonical_bytes(self.rows()));self.assertEqual(families,self.inventory())
        self.assertEqual(path.read_bytes(),trail)
        with self.assertRaisesRegex(ValueError,'cannot be read'):
            self.lifecycle(self.operator,'integrated',task,scope=scope)
        self.assertEqual(before,canonical_bytes(self.rows()));self.assertEqual(families,self.inventory())
        self.assertEqual(path.read_bytes(),trail)

    def test_host_query_proves_prior_eligibility_for_all_decisive_targets_and_rejects_forgery(self):
        for case,counter in [('hold','unheld'),('release','held'),('attest','unheld'),('fork','held'),('forged',None)]:
            with self.subTest(case=case):
                task,void_id,scope=self.prepare(case);before=canonical_bytes(self.rows());families=self.inventory()
                audit_path=self.root/fixture.admin.AUTHORITY_CHANGES_AUDIT;trail=audit_path.read_bytes()
                report=self.query();notes=[n for n in report.get('review_void_diagnostics',[]) if n['task']==task]
                if counter is None:self.assertEqual(notes,[])
                else:
                    self.assertEqual(len(notes),1,report);note=notes[0]
                    self.assertEqual((note['void_id'],note['historical_operator_eligibility']),(void_id,'eligible'))
                    self.assertEqual(note['reason'],'currently-ineligible-review-void')
                    self.assertEqual((note['current_result'],note['counterfactual_result']),('unreadable',counter))
                with self.assertRaisesRegex(ValueError,'cannot be read'):
                    self.lifecycle(self.operator,'integrated',task,scope=scope)
                self.assertEqual(before,canonical_bytes(self.rows()));self.assertEqual(families,self.inventory());self.assertEqual(trail,audit_path.read_bytes())
                self.authority('add')
                self.assertFalse([n for n in self.query().get('review_void_diagnostics',[]) if n['task']==task])

    def test_host_query_missing_damaged_and_symlinked_audit_stays_unknown_and_writes_nothing(self):
        task,void_id,scope=self.prepare('hold');path=self.root/fixture.admin.AUTHORITY_CHANGES_AUDIT
        valid=path.read_bytes();before=canonical_bytes(self.rows());families=self.inventory()
        for case in ('missing','damaged','symlink'):
            with self.subTest(case=case):
                if path.exists() or path.is_symlink():path.unlink()
                target=self.root/'synthetic-audit-target.json'
                if case=='damaged':path.write_text('{damaged')
                elif case=='symlink':target.write_bytes(valid);path.symlink_to(target)
                report=self.query();notes=[n for n in report.get('review_void_diagnostics',[]) if n['task']==task]
                self.assertEqual(len(notes),1,report)
                self.assertEqual(notes[0]['void_id'],void_id)
                self.assertEqual(notes[0]['historical_operator_eligibility'],'unknown')
                self.assertEqual(notes[0]['reason'],'review-void-eligibility-unknown')
                self.assertEqual(before,canonical_bytes(self.rows()));self.assertEqual(families,self.inventory())
                if case=='missing':self.assertFalse(path.exists())
                elif case=='damaged':self.assertEqual(path.read_text(),'{damaged')
                else:self.assertTrue(path.is_symlink());self.assertEqual(target.read_bytes(),valid)


if __name__=='__main__':unittest.main()
