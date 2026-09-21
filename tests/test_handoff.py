"""Owner transfers preserve recovery and explicit authority."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin
import handoff
import work


def payload():
    return dict(schema_version=1, operation_id='move-1', task='trial-task',
                from_actor='alice', to_actor='bob', reason='Review follow-up',
                approval='Owner explicitly transfers contribution responsibility')


class Native:
    def __init__(self):
        self.owner='alice'; self.comments=[]; self.updates=0; self.lose_response=False
    def __call__(self,args):
        if args[0]=='show':
            return json.dumps([dict(id='trial-task',issue_type='task',assignee=self.owner)])
        if args[:2]==['comments','add']:
            self.comments.append(dict(id=str(len(self.comments)+1),text=args[3],author='alice'))
            return json.dumps(self.comments[-1])
        if args[0]=='comments':return json.dumps(self.comments)
        if args[0]=='update':
            self.owner=args[3];self.updates+=1
            if self.lose_response:self.lose_response=False;raise RuntimeError('lost response')
            return '{}'
        raise AssertionError(args)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name);self.native=Native();self.p=payload()
    def execute(self,actor='alice',operator=False):
        return handoff.execute(self.path,actor,self.p,self.native,operator=operator)
    def test_lost_native_update_response_reconciles_without_duplicate_mutation(self):
        self.native.lose_response=True
        with self.assertRaises(RuntimeError):self.execute()
        self.assertEqual(self.native.owner,'bob')
        self.execute()
        self.assertEqual(self.native.updates,1)
        self.assertEqual(len(self.native.comments),2)
        self.assertTrue(self.execute()['reconciled'])
    def test_completed_replay_does_not_revert_subsequent_owner(self):
        self.execute();self.native.owner='carol'
        self.assertEqual(self.execute()['current_owner'],'carol')
        self.assertEqual(self.native.updates,1)
    def test_stale_owner_and_unjournaled_destination_fail(self):
        for owner in ('carol','bob',None):
            self.native.owner=owner
            with self.subTest(owner=owner),self.assertRaises(ValueError):self.execute()
        self.assertEqual(self.native.updates,0)
    def test_owner_transport_cannot_enable_operator_override(self):
        attachment={'0':dict(flag='--file',text=json.dumps(self.p))}
        with self.assertRaisesRegex(ValueError,'Only the current owner'):
            work.execute(self.path,'coordinator','handoff',['trial-task','@attachment:0'],attachment,self.native)
        self.assertEqual(self.native.updates,0)
        self.assertEqual(self.execute('coordinator',operator=True)['current_owner'],'bob')
    def test_operation_id_cannot_change_authority_or_payload(self):
        self.execute()
        with self.assertRaises(ValueError):self.execute(operator=True)
        self.p['reason']='Different authorization'
        with self.assertRaises(ValueError):self.execute()
    def test_malformed_receipts_fail_closed(self):
        self.execute()
        file=next((self.path/'.handoffs').glob('*.json'))
        original=json.loads(file.read_text())
        invalid=[None,[],{},dict(original,status='garbage'),dict(original,identity={}),
                 {k:v for k,v in original.items() if k!='identity'}]
        for value in invalid:
            file.write_text(json.dumps(value))
            self.native.owner='alice'
            with self.subTest(value=value),self.assertRaises(ValueError):self.execute()
        self.assertEqual(self.native.updates,1)
    def test_backup_restore_preserves_completed_receipt_and_retry_identity(self):
        self.execute()
        source=next((self.path/'.handoffs').glob('*.json'))
        files={'.handoffs/'+source.name:json.loads(source.read_text())}
        admin.validate_coordination_files(files)
        dest=self.path/'restored';dest.mkdir()
        with patch.object(admin,'project_dir',return_value=dest),patch.object(admin,'coordination_backup',return_value=files):
            admin.restore_coordination(self.path,'source','dest')
        self.assertTrue(handoff.execute(dest,'alice',self.p,self.native)['reconciled'])
        self.assertEqual(self.native.updates,1)
    def test_backup_validation_rejects_malformed_or_misnamed_handoff(self):
        self.execute()
        file=next((self.path/'.handoffs').glob('*.json'))
        record=json.loads(file.read_text())
        with self.assertRaises(ValueError):
            admin.validate_coordination_files({'.handoffs/'+file.name:dict(record,status='unknown')})
        with self.assertRaises(ValueError):
            admin.validate_coordination_files({'.handoffs/'+'0'*64+'.json':record})

    def test_request_disposition_accepts_only_current_owner_and_reconciles_handoff(self):
        request_payload={'schema_version':1,'operation':'request',
                         'request_id':'00000000-0000-0000-0000-000000000001',
                         'task':'trial-task','from_actor':'alice','to_actor':'bob',
                         'reason':'Please take over'}
        handoff.request(self.path,'bob',request_payload)
        disposition={'schema_version':1,'operation':'disposition','operation_id':'disp-1',
                     'request_id':request_payload['request_id'],'task':'trial-task',
                     'disposition':'accept','reason':'Owner approved','supersedes':None}
        with self.assertRaisesRegex(ValueError,'current owner'):
            handoff.disposition(self.path,'bob',disposition,self.native)
        result=handoff.disposition(self.path,'alice',disposition,self.native)
        self.assertEqual(result['status'],'accepted');self.assertEqual(self.native.owner,'bob')
        self.assertTrue(handoff.disposition(self.path,'alice',disposition,self.native)['reconciled'])
        record=json.loads(next((self.path/'.handoff-requests').glob('*.json')).read_text())
        self.assertEqual(record['status'],'accepted')
        self.assertEqual(record['disposition']['kind'],'accept')

    def test_disposition_binds_task_and_rejects_requester_self_accept(self):
        request_payload={'schema_version':1,'operation':'request',
                         'request_id':'00000000-0000-0000-0000-000000000002',
                         'task':'trial-task','from_actor':'alice','to_actor':'bob',
                         'reason':'Please take over'}
        handoff.request(self.path,'bob',request_payload)
        self.native.owner='bob'
        disposition={'schema_version':1,'operation':'disposition','operation_id':'disp-2',
                     'request_id':request_payload['request_id'],'task':'trial-task',
                     'disposition':'accept','reason':'Self approval','supersedes':None}
        with self.assertRaisesRegex(ValueError,'own handoff'):
            handoff.disposition(self.path,'bob',disposition,self.native)
        disposition['task']='other-task'
        with self.assertRaisesRegex(ValueError,'does not match stored request'):
            handoff.disposition(self.path,'alice',disposition,self.native)

    def test_accept_retry_reconciles_completed_transfer_before_owner_check(self):
        request_payload={'schema_version':1,'operation':'request',
                         'request_id':'00000000-0000-0000-0000-000000000004',
                         'task':'trial-task','from_actor':'alice','to_actor':'bob',
                         'reason':'Please take over'}
        handoff.request(self.path,'bob',request_payload)
        transfer=dict(schema_version=1,operation_id='handoff-'+request_payload['request_id'],
                      task='trial-task',from_actor='alice',to_actor='bob',
                      reason='Please take over',approval='Owner approved')
        self.execute = lambda actor='alice', operator=False: handoff.execute(
            self.path,actor,transfer,self.native,operator=operator)
        self.native.lose_response=True
        with self.assertRaises(RuntimeError):self.execute()
        disposition={'schema_version':1,'operation':'disposition','operation_id':'disp-4',
                     'request_id':request_payload['request_id'],'task':'trial-task',
                     'disposition':'accept','reason':'Retry after response loss','supersedes':None}
        result=handoff.disposition(self.path,'alice',disposition,self.native)
        self.assertTrue(result['reconciled'])
        self.assertEqual(json.loads(next((self.path/'.handoff-requests').glob('*.json')).read_text())['status'],'accepted')

    def test_direct_transfer_settles_only_matching_request(self):
        request_payload={'schema_version':1,'operation':'request',
                         'request_id':'00000000-0000-0000-0000-000000000005',
                         'task':'trial-task','from_actor':'alice','to_actor':'bob',
                         'reason':'Please take over'}
        other_payload=dict(request_payload,request_id='00000000-0000-0000-0000-000000000006')
        handoff.request(self.path,'bob',request_payload)
        handoff.request(self.path,'bob',other_payload)
        transfer=dict(schema_version=1,operation_id='handoff-'+request_payload['request_id'],
                      task='trial-task',from_actor='alice',to_actor='bob',
                      reason='Please take over',approval='Owner approved')
        handoff.execute(self.path,'alice',transfer,self.native)
        records=[json.loads(p.read_text()) for p in (self.path/'.handoff-requests').glob('*.json')]
        self.assertEqual(sorted(r['status'] for r in records),['accepted','pending'])

    def test_accepted_request_history_is_required_for_backup(self):
        request_payload={'schema_version':1,'operation':'request',
                         'request_id':'00000000-0000-0000-0000-000000000003',
                         'task':'trial-task','from_actor':'alice','to_actor':'bob',
                         'reason':'Please take over'}
        handoff.request(self.path,'bob',request_payload)
        file=next((self.path/'.handoff-requests').glob('*.json'))
        record=json.loads(file.read_text())
        record['status']='accepted'
        with self.assertRaises(ValueError):
            admin.validate_coordination_files({'.handoff-requests/'+file.name:record})


if __name__=='__main__':unittest.main()
