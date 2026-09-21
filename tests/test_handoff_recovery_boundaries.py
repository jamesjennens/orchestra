"""Coordinator regression checks for interrupted disposition identity and backup."""
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import admin
import handoff
from test_handoff import Native

class RecoveryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'projects'/'source';self.path.mkdir(parents=True)
        (self.root/'projects'/'destination').mkdir();(self.root/'backups').mkdir()
        self.native=Native()
        self.req=dict(schema_version=1,operation='request',request_id='00000000-0000-0000-0000-000000000001',task='trial-task',from_actor='alice',to_actor='bob',reason='take over')
        self.disp=dict(schema_version=1,operation='disposition',operation_id='disp-1',request_id=self.req['request_id'],task='trial-task',disposition='accept',reason='approved',supersedes=None)
        handoff.request(self.path,'bob',self.req)
    def accept(self,p=None,path=None,actor='alice'):
        return handoff.disposition(path or self.path,actor,p or self.disp,self.native)
    def interrupt_before_receipt(self):
        with patch.object(handoff,'execute',side_effect=RuntimeError('before receipt')):
            with self.assertRaises(RuntimeError):self.accept()
    def test_interruption_after_request_reconciliation_preserves_exact_retry(self):
        real=handoff.atomic;accepted_writes=[]
        def fail(path,value):
            if path.parent.name=='.handoff-requests' and value.get('status')=='accepted':
                accepted_writes.append(value)
                if len(accepted_writes)==2:raise RuntimeError('final reply boundary')
            return real(path,value)
        with patch.object(handoff,'atomic',side_effect=fail):
            with self.assertRaises(RuntimeError):self.accept()
        self.assertEqual(accepted_writes[0]['disposition']['operation_id'],'disp-1')
        self.assertTrue(self.accept()['reconciled']);self.assertEqual(self.native.updates,1)
    def test_changed_kind_and_supersedes_rejected_before_receipt(self):
        self.interrupt_before_receipt()
        for change in ({'disposition':'decline'},{'supersedes':'00000000-0000-0000-0000-000000000002'}):
            with self.subTest(change=change),self.assertRaises(ValueError):self.accept(dict(self.disp,**change))
        self.assertEqual(self.native.updates,0)
        self.assertEqual(self.accept()['status'],'accepted')
    def test_unauthorized_actor_does_not_create_recovery_identity(self):
        with self.assertRaises(ValueError):self.accept(actor='mallory')
        self.assertFalse((self.path/'.handoff-recoveries').exists())
    def test_backup_restore_retains_before_receipt_identity(self):
        self.interrupt_before_receipt()
        fake=types.SimpleNamespace(flock=Mock(),LOCK_EX=2)
        with patch.dict('sys.modules',{'fcntl':fake}),patch.object(admin,'run_bd',return_value='synced'):
            admin.backup_project(self.root,'source')
        bundle=json.loads((self.root/'backups'/'source.coordination.json').read_text())
        self.assertTrue(any(k.startswith('.handoff-recoveries/') for k in bundle['files']))
        admin.restore_coordination(self.root,'source','destination')
        destination=self.root/'projects'/'destination'
        with self.assertRaises(ValueError):self.accept(dict(self.disp,reason='changed'),destination)
        self.assertEqual(self.accept(path=destination)['status'],'accepted')
    def test_invalid_recovery_backup_names_and_shapes_fail_closed(self):
        self.interrupt_before_receipt()
        file=next((self.path/'.handoff-recoveries').glob('*.json'));record=json.loads(file.read_text())
        for name,value in [('.handoff-recoveries/'+'0'*64+'.json',record),('.handoff-recoveries/'+file.name,dict(record,kind='decline'))]:
            with self.subTest(name=name),self.assertRaises(ValueError):admin.validate_coordination_files({name:value})
    def test_recovery_directory_symlink_is_rejected_without_external_write(self):
        target=self.root/'external';target.mkdir()
        try:(self.path/'.handoff-recoveries').symlink_to(target,target_is_directory=True)
        except OSError:self.skipTest('Symlink privilege unavailable')
        with self.assertRaises(ValueError):self.accept()
        self.assertEqual(list(target.iterdir()),[])
    def test_dangling_recovery_file_symlink_is_rejected(self):
        folder=self.path/'.handoff-recoveries';folder.mkdir()
        file=folder/(handoff.content_hash({'request_id':self.req['request_id']})+'.json')
        target=self.root/'absent'
        try:file.symlink_to(target)
        except OSError:self.skipTest('Symlink privilege unavailable')
        with self.assertRaises(ValueError):self.accept()
        self.assertFalse(target.exists())
