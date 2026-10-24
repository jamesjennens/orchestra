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
        with mock.patch.object(audit,'HISTORY_VERSION_CAP',1):
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
        with mock.patch.multiple(audit,HISTORY_BYTES_CAP=16,HISTORY_TOTAL_BYTES_CAP=32):
            report=fixture.admin.lifecycle_audit_command(self.root,'pp',limit=100)
        self.assertLessEqual(report['historical_read_coverage']['read_calls'],2)
        self.assertEqual(report['historical_read_coverage']['stopped_reason'],'whole-audit-history-byte-cap')
        with mock.patch.object(audit,'HISTORY_SECONDS',0):
            report=fixture.admin.lifecycle_audit_command(self.root,'pp',limit=100)
        self.assertEqual(report['historical_read_coverage']['read_calls'],0)
        self.assertEqual(report['historical_read_coverage']['stopped_reason'],'whole-audit-history-time-budget')
        self.assertEqual(before,canonical_bytes(self.export_rows()))

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
        base='306529ec2bcdd6b8c6e10c5b1c269f282a5df155'
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


if __name__=='__main__':unittest.main()
