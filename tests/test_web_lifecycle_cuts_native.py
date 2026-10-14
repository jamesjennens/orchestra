"""Real endpoint process exits across native effect/mark/HTTP receipt boundaries."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

KIT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(KIT),str(KIT/'tests')]
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers,COMMIT
import test_web_merge_slot as slot_fixture
import lifecycle
import web_lifecycle_marks as marks
from http_authority import OperationJournal,journal_path


@unittest.skipIf(fixture.endpoint is None,'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None,'no pinned native bd (set ORCHESTRA_BD_BIN)')
class NativeMarkCutTests(RulesHelpers,unittest.TestCase):
    web=slot_fixture.NativeWebSlotTests.web

    def capture(self,label,rows):
        output=os.environ.get('ORCHESTRA_LIFECYCLE_TEST_EXPORTS')
        if output:
            path=Path(output);path.mkdir(parents=True,exist_ok=True)
            (path/(label+'.jsonl')).write_text(''.join(json.dumps(row)+'\n' for row in rows))

    def test_real_process_cuts_preserve_uncertainty_and_receipt_retry_is_one_effect(self):
        self.deployment(operators=['ops'],web_lifecycle_mark_writes=True)
        harness,token=self.web()
        for point in ('before-mark','after-mark','http-receipt'):
            task=self.task('Native cut '+point,assignee='other-author')
            contributed=self.contribute(task,'other-author');self.assertEqual(contributed['returncode'],0,contributed)
            approved=self.approve(task,'ops',json.loads(contributed['stdout'])['comment_id'])
            self.assertEqual(approved['returncode'],0,approved)
            scope=dict(source_commit=COMMIT,integration_commit='e'*40,release_id='synthetic-cut-release',environment=point)
            seeded=self.lifecycle('other-author','lifecycle-scope',task,scope=scope)
            self.assertEqual(seeded['returncode'],0,seeded)
            acquired=harness.request('POST','/v1/projects/pp/merge-slot/acquire',dict(task_id=task,target='main'),
                                     token=token,key='native-cut-slot-'+point)
            self.assertEqual(acquired.status,200,acquired.data)
            path='/v1/projects/pp/tasks/'+task+'/lifecycle/facts/integrated'
            body=dict(scope=scope,value='passed',evidence=['synthetic process cut'],provenance='performed')
            def post():return harness.request('POST',path,body,token=token,key='native-cut-'+point)
            before=self.rows();self.capture(point+'-before',before)
            count=len(marks.read_journal(self.project)['entries'])
            if point!='http-receipt':
                wrapper=self.root/('endpoint-cut-'+point+'.py')
                wrapper.write_text("import os,sys\nsys.path.insert(0,"+repr(str(KIT))+")\nimport endpoint,web_lifecycle_marks as marks\noriginal=marks.issue\ndef interrupted(*args,**kwargs):\n"
                                   +(" os._exit(73)\n" if point=='before-mark' else " original(*args,**kwargs)\n os._exit(74)\n")
                                   +"marks.issue=interrupted\nendpoint.main()\n")
                original_endpoint=harness.backend.endpoint
                harness.backend.endpoint=str(wrapper)
                try:lost=post()
                finally:harness.backend.endpoint=original_endpoint
                self.assertEqual(lost.status,503,lost.data)
            else:
                commit=harness.service.idempotency_commit
                interrupted=[False]
                def lose_receipt(*args,**kwargs):
                    if not interrupted[0]:interrupted[0]=True;raise OSError('synthetic HTTP receipt cut')
                    return commit(*args,**kwargs)
                with patch.object(harness.service,'idempotency_commit',side_effect=lose_receipt):lost=post()
                self.assertEqual(lost.status,500,lost.data);self.assertTrue(interrupted[0])
            after=self.rows();self.capture(point+'-after-effect',after)
            events=[row for row in after if ((lifecycle.native_event(row) or {}).get('payload') or {}).get('task')==task
                    and (lifecycle.native_event(row) or {}).get('dimension')=='integrated']
            self.assertEqual(len(events),1)
            event=events[0];payload=lifecycle.native_event(event)['payload']
            journal=OperationJournal(journal_path(self.project));operation=journal.lookup(payload['operation_id'])
            self.assertEqual(operation['state'],'committed' if point=='http-receipt' else 'in_progress')
            family=marks.read_journal(self.project);self.assertEqual(family['invalid'],[])
            self.assertEqual(len(family['entries']),count+(point!='before-mark'))
            self.assertEqual(marks.recorder_eligible(event,'pp',operators=[],owners={},journal=family),point!='before-mark')
            repeated=post()
            self.assertEqual(repeated.status,201 if point=='http-receipt' else 503,repeated.data)
            self.assertEqual(self.rows(),after)
            self.capture(point+'-after-identical-retry',after)
            audits=[entry for entry in harness.service.state['audit'] if entry['action']=='lifecycle.fact'
                    and entry['outcome']=='committed' and entry.get('event_id')==event['id']]
            self.assertEqual(len(audits),1 if point=='http-receipt' else 0)
            if point=='http-receipt':
                self.assertEqual(repeated.data['event_id'],event['id'])
                self.assertEqual(audits[0]['operation_id'],payload['operation_id'])
            output=os.environ.get('ORCHESTRA_LIFECYCLE_TEST_EXPORTS')
            if output:
                (Path(output)/(point+'-boundary.json')).write_text(json.dumps(dict(point=point,first_status=lost.status,
                    retry_status=repeated.status,event_id=event['id'],operation=operation,committed_audits=audits,
                    mark=family['entries'].get(event['id'])),indent=2))
            # Leave the same actor free to acquire the next independent case.
            release=harness.request('POST','/v1/projects/pp/merge-slot/release',{},token=token,key='native-cut-release-'+point)
            self.assertEqual(release.status,200,release.data)


if __name__=='__main__':unittest.main()
