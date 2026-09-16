import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sessions
import client
import admin
import worker

class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.export=Mock(return_value='')
    def register(self,name='Worker',rid=None):
        return sessions.execute(self.path,'example',['register','--name',name,'--request-id',rid or str(uuid.uuid4())],self.export)
    def test_same_readable_name_gets_distinct_actors(self):
        a=self.register();b=self.register()
        self.assertNotEqual(a['session']['actor'],b['session']['actor'])
        self.assertEqual(a['session']['name'],b['session']['name'])
        got=sessions.execute(self.path,'example',['show',a['session']['actor']],self.export)
        self.assertEqual(got['session'],a['session'])
    def test_retry_reconciles_without_export_or_allocating_again(self):
        rid=str(uuid.uuid4());first=self.register(rid=rid)
        self.export.reset_mock();second=self.register(rid=rid)
        self.assertTrue(second['reconciled']);self.assertEqual(first['session'],second['session']);self.export.assert_not_called()
        with self.assertRaisesRegex(ValueError,'different name'):self.register(name='Someone else',rid=rid)
    def test_collision_checks_existing_native_actors_and_registry(self):
        a,b,c=[uuid.uuid4() for _ in range(3)]
        self.export.return_value=json.dumps({'assignee':'session-'+str(a),'comments':[{'author':'session-'+str(b)}]})
        with patch.object(sessions.uuid,'uuid4',side_effect=[a,b,c]):
            first=self.register(rid=str(uuid.UUID(int=42)))
        self.assertEqual(first['session']['actor'],'session-'+str(c))
        self.export.return_value=''
        d=uuid.uuid4()
        with patch.object(sessions.uuid,'uuid4',side_effect=[c,d]):
            second=self.register(rid=str(uuid.UUID(int=43)))
        self.assertEqual(second['session']['actor'],'session-'+str(d))
    def test_collision_exhaustion_and_failed_export_write_nothing(self):
        a=uuid.uuid4();self.export.return_value=json.dumps({'actor':'session-'+str(a)})
        with patch.object(sessions.uuid,'uuid4',return_value=a),self.assertRaises(ValueError):self.register(rid=str(uuid.UUID(int=44)))
        self.assertFalse((self.path/'.sessions.json').exists())
        self.export.side_effect=RuntimeError('offline')
        with self.assertRaises(RuntimeError):self.register()
        self.assertFalse((self.path/'.sessions.json').exists())
    def test_invalid_request_and_corrupt_registry_fail_closed(self):
        for name,rid in [('',str(uuid.uuid4())),('line\nbreak',str(uuid.uuid4())),('good','../x')]:
            with self.assertRaises(ValueError):self.register(name,rid)
        (self.path/'.sessions.json').write_text('{bad')
        with self.assertRaises(ValueError):self.register()
    def test_duplicate_actors_rejected_in_backup(self):
        a=self.register()['session'];rid=str(uuid.uuid4())
        data={'schema_version':1,'records':{a['request_id']:a,rid:dict(a,request_id=rid)}}
        with self.assertRaises(ValueError):admin.validate_coordination_files({'.sessions.json':data})
    def test_registry_roundtrips_through_restore(self):
        a=self.register()['session'];data=json.loads((self.path/'.sessions.json').read_text())
        dest=self.path/'projects'/'restored';dest.mkdir(parents=True)
        with patch.object(admin,'coordination_backup',return_value={'.sessions.json':data}):admin.restore_coordination(self.path,'source','restored')
        got=sessions.execute(dest,'restored',['register','--name',a['name'],'--request-id',a['request_id']],Mock(side_effect=AssertionError('must not export')))
        self.assertEqual(got['session'],a);self.assertTrue(got['reconciled'])
    def test_symlink_registry_rejected(self):
        target=self.path/'other';target.write_text('{}')
        try:(self.path/'.sessions.json').symlink_to(target)
        except OSError:self.skipTest('No symlink privilege')
        with self.assertRaises(ValueError):self.register()
    def test_actorless_registration_only_and_retry_id_printed(self):
        with patch('sys.stderr') as stderr:
            payload=json.loads(client._wire('example',None,['register','--name','Worker'],'session',None))
        self.assertEqual(payload['actor'],'');self.assertIn('--request-id',payload['args'])
        uuid.UUID(payload['args'][-1]);self.assertTrue(stderr.write.called)
        with self.assertRaises(ValueError):client._wire('example',None,['list'],'bd',None)
        with self.assertRaises(ValueError):client._wire('example',None,['show','session-unknown'],'session',None)
    def test_explicit_request_id_is_not_replaced(self):
        rid=str(uuid.uuid4());args=['register','--name','Worker','--request-id',rid]
        payload=json.loads(client._wire('example',None,args,'session',None))
        self.assertEqual(payload['args'],args)

    def test_worker_start_uses_allocated_actor_for_onboarding(self):
        record={'actor':'session-'+str(uuid.uuid4()),'name':'worker','request_id':str(uuid.uuid4()),'created_at':'2026-09-16T00:00:00+00:00'}
        replies=[{'returncode':0,'stdout':json.dumps({'session':record}),'stderr':''},{'returncode':0,'stdout':'Instructions','stderr':''}]
        with patch.object(sys,'argv',['worker.py','--root','/srv/runtime','--project','example','start','--name','worker']),patch('onboarding.execute'),patch('admin.root_path',return_value=self.path),patch('worker.request',side_effect=replies) as request,patch('sys.stdout'):
            self.assertEqual(worker.main(),0)
        self.assertIsNone(request.call_args_list[0].args[2])
        self.assertEqual(request.call_args_list[1].args[2],record['actor'])
        self.assertEqual(request.call_args_list[1].kwargs['action'],'onboard')

    def test_worker_does_not_register_when_onboarding_missing(self):
        with patch.object(sys,'argv',['worker.py','--root','/srv/runtime','--project','example','start','--name','worker']),patch('onboarding.execute',side_effect=ValueError('missing')),patch('admin.root_path',return_value=self.path),patch('worker.request') as request:
            with self.assertRaises(ValueError):worker.main()
        request.assert_not_called()
