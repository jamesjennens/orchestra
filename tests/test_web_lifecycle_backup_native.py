"""Passed HTTP integration marks in actual native backup/copy/restore generations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

KIT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(KIT),str(KIT/'tests')]
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers,COMMIT
import test_web_merge_slot as slot_fixture
import lifecycle
import review_workflow as review
import web_lifecycle_marks as marks
from requirements import content_hash


@unittest.skipIf(fixture.endpoint is None,'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None,'no pinned native bd (set ORCHESTRA_BD_BIN)')
class NativeMarkBackupTests(RulesHelpers,unittest.TestCase):
    web=slot_fixture.NativeWebSlotTests.web

    def command(self,*args):
        result=subprocess.run([sys.executable,str(KIT/'admin.py'),'--root',str(self.root),*args],
                              env=fixture.admin.environment(self.root),capture_output=True,text=True,timeout=180)
        output=os.environ.get('ORCHESTRA_LIFECYCLE_TEST_EXPORTS')
        if output:
            path=Path(output);path.mkdir(parents=True,exist_ok=True)
            label='backup-command-'+str(len(list(path.glob('backup-command-*.json'))))
            (path/(label+'.json')).write_text(json.dumps(dict(args=list(args),returncode=result.returncode,
                                                             stdout=result.stdout,stderr=result.stderr),indent=2))
        return result

    def test_native_scheduled_pair_copy_restore_and_previous_reader_boundary(self):
        self.deployment(operators=['ops'],web_lifecycle_mark_writes=True)
        earlier_task=self.task('Earlier native integration')
        earlier_scope=dict(source_commit='2'*40,integration_commit='1'*40,
                           release_id='synthetic-earlier-release',environment='synthetic-backup')
        for dimension in ('lifecycle-scope','integrated'):
            earlier=self.lifecycle('ops',dimension,earlier_task,scope=earlier_scope)
            self.assertEqual(earlier['returncode'],0,earlier)
        harness,token=self.web();task=self.task('Native marked backup',assignee='other-author')
        contributed=self.contribute(task,'other-author');self.assertEqual(contributed['returncode'],0,contributed)
        approved=self.approve(task,'ops',json.loads(contributed['stdout'])['comment_id'])
        self.assertEqual(approved['returncode'],0,approved)
        scope=dict(source_commit=COMMIT,integration_commit='e'*40,release_id='synthetic-backup-release',environment='synthetic-backup')
        seeded=self.lifecycle('other-author','lifecycle-scope',task,scope=scope)
        self.assertEqual(seeded['returncode'],0,seeded)
        acquired=harness.request('POST','/v1/projects/pp/merge-slot/acquire',dict(task_id=task,target='main'),
                                 token=token,key='native-backup-mark-slot')
        self.assertEqual(acquired.status,200,acquired.data)
        written=harness.request('POST','/v1/projects/pp/tasks/'+task+'/lifecycle/facts/integrated',
            dict(scope=scope,value='passed',evidence=['synthetic backup integration'],provenance='performed'),
            token=token,key='native-backup-mark-integration')
        self.assertEqual(written.status,201,written.data)
        rows=self.rows();event=next(row for row in rows if row['id']==written.data['event_id'])
        bound=marks.read_journal(self.project);self.assertEqual(bound['invalid'],[])
        entry=bound['entries'][event['id']];self.assertTrue(marks.matches(entry,'pp',event))
        backups=self.root/'backups';backups.mkdir(exist_ok=True)
        initialized=self.r.bd('backup','init',str(backups/'pp'))
        self.assertEqual(initialized.returncode,0,initialized.stderr)
        # This is the exact command a generated multi-project timer executes;
        # no system timer or service is changed by the fixture.
        scheduled=self.command('backup','--all')
        self.assertEqual(scheduled.returncode,0,scheduled.stdout+scheduled.stderr)
        pair=json.loads((backups/'pp.coordination.json').read_text())
        self.assertEqual(pair['status'],'complete');self.assertIn(marks.MANIFEST,pair['files'])
        self.assertEqual(pair['files'][marks.DIRECTORY+'/'+entry['sha256']+'.json'],entry)
        fixture.admin.validate_coordination_files(pair['files'])
        outside=tempfile.TemporaryDirectory(prefix='native-mark-copy-');self.addCleanup(outside.cleanup)
        copy_path=Path(outside.name)/'snapshot'
        copied=self.command('backup-copy',str(copy_path))
        self.assertEqual(copied.returncode,0,copied.stdout+copied.stderr)
        copied_pair=json.loads((copy_path/'pp.coordination.json').read_text())
        self.assertEqual(copied_pair['files'],pair['files'])
        self.assertTrue((copy_path/'pp').is_dir())
        restored=self.command('restore-new','pp','markforeign')
        self.assertEqual(restored.returncode,0,restored.stdout+restored.stderr)
        self.assertIn('DO NOT confer marked integration eligibility',restored.stderr)
        self.assertIn('cannot qualify later contribution bases',restored.stderr)
        destination=self.root/'projects'/'markforeign'
        restored_rows=[json.loads(line) for line in fixture.admin.run_bd(self.root,'markforeign',['export','--all']).splitlines() if line.strip()]
        restored_event=next(row for row in restored_rows if row['id']==event['id'])
        self.assertEqual(restored_event,event)
        restored_marks=marks.read_journal(destination);self.assertEqual(restored_marks['invalid'],[])
        self.assertEqual(restored_marks['entries'][event['id']],entry)
        self.assertFalse(marks.recorder_eligible(restored_event,'markforeign',operators=[],owners={},journal=restored_marks))
        self.assertFalse((destination/marks.RESTORE_MARKER).exists())
        # Restore the host family over its own unchanged native generation too.
        family=self.project/marks.DIRECTORY;retained=self.project/'retained-web-marks'
        family.rename(retained)
        self.assertTrue(fixture.admin.restore_web_lifecycle_mark_family(self.root,'pp',pair['files']))
        self.assertTrue(marks.recorder_eligible(event,'pp',operators=[],owners={},journal=marks.read_journal(self.project)))
        # The current shared reader can use this historical integration with no
        # current web store/roles or listed operator. The named previous checkout
        # must reject it as an unlisted recorder, even with host marks present.
        prior=dict(scope_token=content_hash(earlier_scope))
        current=review.recorded_integrations(rows,earlier_task,prior,operators=[],owners={},journal=self.project)
        self.assertEqual(current[1],scope['integration_commit'])
        old=os.environ.get('ORCHESTRA_LIFECYCLE_PREVIOUS_KIT')
        if not old:
            self.skipTest('no explicit previous checkout for mark-reader boundary')
        previous=Path(old)
        probe=Path(outside.name)/'reader-input.json'
        probe.write_text(json.dumps(dict(rows=rows,task=earlier_task,prior=prior,project=str(self.project))))
        script="import json,sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import review_workflow as r;v=json.loads(Path(sys.argv[2]).read_text());print(json.dumps(r.recorded_integrations(v['rows'],v['task'],v['prior'],operators=[],owners={},journal=Path(v['project']))))"
        legacy=subprocess.run([sys.executable,'-B','-c',script,str(previous),str(probe)],cwd=previous,
                              capture_output=True,text=True,timeout=60)
        self.assertEqual(legacy.returncode,0,legacy.stderr)
        legacy_result=json.loads(legacy.stdout)
        self.assertIsNone(legacy_result[1]);self.assertEqual(legacy_result[0][scope['integration_commit']][0],'unlisted')
        # Keep the complete newer pair outside the older kit's backup destination.
        def previous_command(*args):
            return subprocess.run([sys.executable,'-B',str(previous/'admin.py'),'--root',str(self.root),*args],
                                  cwd=previous,env=fixture.admin.environment(self.root),
                                  capture_output=True,text=True,timeout=180)
        old_restore=previous_command('restore-new','pp','oldmarkrefused')
        self.assertNotEqual(old_restore.returncode,0,old_restore.stdout+old_restore.stderr)
        self.assertIn('Invalid coordination backup path',old_restore.stderr)
        self.assertFalse((self.root/'projects'/'oldmarkrefused').exists())
        old_backup=previous_command('backup','pp')
        self.assertEqual(old_backup.returncode,0,old_backup.stdout+old_backup.stderr)
        omitted=json.loads((backups/'pp.coordination.json').read_text())
        self.assertNotIn(marks.MANIFEST,omitted['files'])
        self.assertFalse(any(name.startswith(marks.DIRECTORY+'/') for name in omitted['files']))
        preserved=json.loads((copy_path/'pp.coordination.json').read_text())
        self.assertEqual(preserved['files'],pair['files'])
        legacy_restore=self.command('restore-new','pp','marklegacy')
        self.assertEqual(legacy_restore.returncode,0,legacy_restore.stdout+legacy_restore.stderr)
        self.assertIn('no web integration mark family',legacy_restore.stdout)
        self.assertIn('does not prove it was absent at the source',legacy_restore.stdout)
        legacy_project=self.root/'projects'/'marklegacy'
        legacy_marks=marks.read_journal(legacy_project)
        self.assertFalse(legacy_marks['present'])
        self.assertFalse(marks.recorder_eligible(event,'marklegacy',[],{},legacy_marks))
        output=os.environ.get('ORCHESTRA_LIFECYCLE_TEST_EXPORTS')
        if output:
            audit=Path(output);audit.mkdir(parents=True,exist_ok=True)
            for label,result in [('previous-mark-restore-refusal',old_restore),('previous-mark-backup-omission',old_backup),('current-legacy-mark-restore',legacy_restore)]:
                (audit/(label+'.json')).write_text(json.dumps(dict(returncode=result.returncode,stdout=result.stdout,stderr=result.stderr),indent=2))
            (audit/'previous-marks-free-sidecar.json').write_text(json.dumps(omitted,indent=2))
            (audit/'preserved-new-kit-sidecar.json').write_text(json.dumps(preserved,indent=2))
        output=os.environ.get('ORCHESTRA_LIFECYCLE_TEST_EXPORTS')
        if output:
            path=Path(output)
            (path/'backup-native-source.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
            (path/'backup-native-restored.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in restored_rows))
            (path/'backup-pair.json').write_text(json.dumps(pair,indent=2))
            (path/'previous-reader-result.json').write_text(json.dumps(dict(current=current,previous=legacy_result),indent=2))


if __name__=='__main__':unittest.main()
