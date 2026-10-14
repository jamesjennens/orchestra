"""Actual native approval/source/assignee/slot checks for every strict fact value."""
import json
import os
from pathlib import Path
import sys
import unittest

KIT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(KIT),str(KIT/'tests')]
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers,COMMIT
import test_web_merge_slot as slot_fixture
import lifecycle
from requirements import content_hash


@unittest.skipIf(fixture.endpoint is None,'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None,'no pinned native bd (set ORCHESTRA_BD_BIN)')
class NativeStrictFactTests(RulesHelpers,unittest.TestCase):
    web=slot_fixture.NativeWebSlotTests.web

    def test_every_strict_value_and_current_native_approval_source_assignee_slot(self):
        self.deployment(operators=['ops'],web_lifecycle_mark_writes=True)
        harness,token=self.web();task=self.task('Native strict facts',assignee='other-author')
        contributed=self.contribute(task,'other-author');self.assertEqual(contributed['returncode'],0,contributed)
        contribution=json.loads(contributed['stdout'])['comment_id']
        scope=dict(source_commit=COMMIT,integration_commit='e'*40,release_id='synthetic-strict-release',environment='strict-native')
        seeded=self.lifecycle('other-author','lifecycle-scope',task,scope=scope);self.assertEqual(seeded['returncode'],0,seeded)
        path='/v1/projects/pp/tasks/'+task+'/lifecycle'

        def fact(value='passed'):
            body=dict(scope=dict(scope),value=value,evidence=['synthetic strict fact'],provenance='performed')
            if value=='pending':body['trigger']='Waiting for synthetic evidence'
            return body

        def post(dimension,body,key):
            return harness.request('POST',path+'/facts/'+dimension,body,token=token,key='native-strict-'+key)

        def refusal(dimension,body,key,status=409):
            before=self.rows();reply=post(dimension,body,key)
            self.assertEqual(reply.status,status,reply.data);after=self.rows();self.assertEqual(after,before)
            output=os.environ.get('ORCHESTRA_LIFECYCLE_TEST_EXPORTS')
            if output:
                folder=Path(output);folder.mkdir(parents=True,exist_ok=True)
                for label,rows in [('before',before),('after',after)]:
                    (folder/(key+'-'+label+'.jsonl')).write_text(''.join(json.dumps(row)+'\n' for row in rows))
                (folder/(key+'-refusal.json')).write_text(json.dumps(dict(status=reply.status,message=reply.data['error']['message']),indent=2))
            return reply

        for dimension in ('reviewed','integrated'):
            for value in lifecycle.VALUES:
                refusal(dimension,fact(value),'no-approval-'+dimension+'-'+value)
        approved=self.approve(task,'ops',contribution);self.assertEqual(approved['returncode'],0,approved)
        approval=json.loads(approved['stdout'])['comment_id']
        refusal('reviewed',fact('failed'),'approved-no-slot')
        other=self.task('Wrong held task')
        def acquire(which,key):
            reply=harness.request('POST','/v1/projects/pp/merge-slot/acquire',dict(task_id=which,target='main'),
                                  token=token,key='native-strict-slot-'+key)
            self.assertEqual(reply.status,200,reply.data)
        def release(key):
            reply=harness.request('POST','/v1/projects/pp/merge-slot/release',{},token=token,key='native-strict-release-'+key)
            self.assertEqual(reply.status,200,reply.data)
        acquire(other,'other');refusal('integrated',fact(),'wrong-held-task');release('other');acquire(task,'correct')
        def change_source(commit,key):
            expected=content_hash(scope);scope['source_commit']=commit
            reply=harness.request('POST',path+'/scope',dict(scope=dict(scope),expected_scope_token=expected,
                evidence=['synthetic scope move'],provenance='performed'),token=token,key='native-strict-scope-'+key)
            self.assertEqual(reply.status,201,reply.data)
        change_source('f'*40,'different-full-commit')
        refusal('reviewed',fact(),'approved-other-full-source')
        change_source(COMMIT[:12],'prefix-source')
        refusal('integrated',fact('unknown'),'prefix-is-not-approved-full-source')
        change_source(COMMIT,'approved-full-source')
        for dimension in ('reviewed','integrated'):
            for value in lifecycle.VALUES:
                reply=post(dimension,fact(value),'positive-'+dimension+'-'+value)
                self.assertEqual(reply.status,201,reply.data)
        changed=self.r.bd('update',task,'--assignee','ops','--json');self.assertEqual(changed.returncode,0,changed.stderr)
        refusal('integrated',fact('not-applicable'),'current-assignee-is-approver')
        changed=self.r.bd('update',task,'--assignee','other-author','--json');self.assertEqual(changed.returncode,0,changed.stderr)
        registry=self.project/'.sessions.json';prior=registry.read_bytes() if registry.exists() else None
        registry.write_text('{')
        try:refusal('reviewed',fact(),'damaged-current-registry',503)
        finally:
            if prior is None:registry.unlink()
            else:registry.write_bytes(prior)
        # A newer contribution makes the old approval obsolete; approval of the
        # new source and matching scope provide the positive control afterwards.
        newer=self.contribute(task,'other-author',commit='f'*40,previous=approval,supersedes=contribution)
        self.assertEqual(newer['returncode'],0,newer);current=json.loads(newer['stdout'])['comment_id']
        change_source('f'*40,'new-current-source')
        refusal('reviewed',fact(),'obsolete-approval')
        approved=self.approve(task,'ops',current);self.assertEqual(approved['returncode'],0,approved)
        head=json.loads(approved['stdout'])['comment_id']
        self.assertEqual(post('reviewed',fact(),'new-current-approved').status,201)
        request=self.post_review(task,'ops',dict(schema_version=1,operation='request-changes',operation_id='strict-request',
            task=task,previous=head,contribution=current,items=[dict(id='fix',text='Synthetic remaining review request')]))
        self.assertEqual(request['returncode'],0,request)
        refusal('integrated',fact('pending'),'unresolved-review-request')
        self.deployment(operators=['ops'],web_lifecycle_mark_writes=True,review_workflow_writes=True)
        withdrawn=self.post_review(task,'other-author',dict(schema_version=1,operation='withdraw',operation_id='strict-withdraw',
            task=task,previous=json.loads(request['stdout'])['comment_id'],contribution=current,reason='Synthetic withdrawn contribution'))
        self.assertEqual(withdrawn['returncode'],0,withdrawn)
        refusal('reviewed',fact('unknown'),'withdrawn-current-contribution')


if __name__=='__main__':unittest.main()
