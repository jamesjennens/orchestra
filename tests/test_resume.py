import copy
import io
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import admin
import sessions
import worker


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.export=Mock(return_value='')
        self.record=sessions.execute(self.path,'example',['register','--name','recurring','--request-id',str(uuid.uuid4())],self.export)['session']
        self.export.reset_mock()

    def resume(self,rid=None,actor=None):
        return sessions.execute(self.path,'example',['resume','--request-id',rid or str(uuid.uuid4())],self.export,actor=actor or self.record['actor'])

    def test_resume_retries_preserve_event_without_new_actor_or_native_operations(self):
        rid=str(uuid.uuid4())
        first=self.resume(rid);second=self.resume(rid)
        self.assertFalse(first['reconciled']);self.assertTrue(second['reconciled'])
        self.assertEqual(first['resume'],second['resume'])
        self.assertEqual(first['session'],self.record)
        data=json.loads((self.path/'.sessions.json').read_text())
        self.assertEqual(list(data['records'].values()),[self.record])
        self.assertEqual(len(data['resumes']),1)
        self.export.assert_not_called()

    def test_each_new_request_records_a_resume_for_the_same_identity(self):
        first=self.resume();second=self.resume()
        self.assertEqual(first['session'],second['session'])
        self.assertNotEqual(first['resume']['request_id'],second['resume']['request_id'])

    def test_cross_actor_retry_refused(self):
        other=sessions.execute(self.path,'example',['register','--name','other','--request-id',str(uuid.uuid4())],self.export)['session']
        rid=str(uuid.uuid4());self.resume(rid)
        with self.assertRaisesRegex(ValueError,'different actor'):self.resume(rid,other['actor'])

    def test_unregistered_and_missing_actors_cannot_resume(self):
        for actor in (None,'legacy-name','session-'+str(uuid.uuid4())):
            with self.assertRaisesRegex(ValueError,'registered actor'):
                sessions.execute(self.path,'example',['resume','--request-id',str(uuid.uuid4())],self.export,actor=actor)
        self.assertNotIn('resumes',json.loads((self.path/'.sessions.json').read_text()))

    def test_invalid_request_does_not_write(self):
        before=(self.path/'.sessions.json').read_bytes()
        for args in (['resume'],['resume','--request-id','not-a-uuid']):
            with self.assertRaises(ValueError):sessions.execute(self.path,'example',args,self.export,actor=self.record['actor'])
        self.assertEqual(before,(self.path/'.sessions.json').read_bytes())

    def test_old_and_resumed_registries_pass_backup_validation_and_restore(self):
        old=json.loads((self.path/'.sessions.json').read_text())
        admin.validate_coordination_files({'.sessions.json':old})
        event=self.resume()['resume']
        data=json.loads((self.path/'.sessions.json').read_text())
        admin.validate_coordination_files({'.sessions.json':data})
        dest=self.path/'projects'/'restored';dest.mkdir(parents=True)
        with patch.object(admin,'coordination_backup',return_value={'.sessions.json':data}):
            admin.restore_coordination(self.path,'source','restored')
        got=sessions.execute(dest,'restored',['resume','--request-id',event['request_id']],self.export,actor=self.record['actor'])
        self.assertTrue(got['reconciled']);self.assertEqual(got['resume'],event)

    def test_corrupt_resume_events_fail_backup_validation(self):
        event=self.resume()['resume'];data=json.loads((self.path/'.sessions.json').read_text())
        changes=[{'actor':'legacy'}, {'timestamp':'2026-01-01T00:00:00'}, {'timestamp':None}, {'request_id':str(uuid.uuid4())}, {'extra':True}]
        for change in changes:
            broken=copy.deepcopy(data);broken['resumes'][event['request_id']].update(change)
            with self.assertRaises(ValueError):admin.validate_coordination_files({'.sessions.json':broken})
        broken=copy.deepcopy(data);broken['resumes']=[]
        with self.assertRaises(ValueError):sessions.validate(broken)

    def test_worker_resume_prints_generated_retry_id_before_request_then_onboards(self):
        event={'actor':self.record['actor'],'request_id':str(uuid.uuid4()),'timestamp':'2026-09-16T00:00:00+00:00'}
        stderr=io.StringIO()
        def request(cfg,project,actor,args,action):
            self.assertEqual(actor,self.record['actor'])
            if action=='session':
                self.assertEqual(args,['resume','--request-id',event['request_id']])
                self.assertIn(event['request_id'],stderr.getvalue())
                return {'returncode':0,'stdout':json.dumps({'resume':event}),'stderr':''}
            if action=='onboard':return {'returncode':0,'stdout':'Instructions','stderr':''}
            self.assertEqual(action,'work');self.assertEqual(args,['--mine'])
            return {'returncode':0,'stdout':'{"tasks": []}','stderr':''}
        argv=['worker.py','--root','/srv/runtime','--project','example','--actor',self.record['actor'],'resume']
        with patch.object(sys,'argv',argv),patch.object(worker.uuid,'uuid4',return_value=uuid.UUID(event['request_id'])),patch.object(worker,'request',side_effect=request) as call,patch('sys.stderr',stderr),patch('sys.stdout',new_callable=io.StringIO) as stdout:
            self.assertEqual(worker.main(),0)
            self.assertIn('Resumed session:',stdout.getvalue());self.assertIn('Instructions',stdout.getvalue())
            self.assertIn('Owned work (revision requests first)',stdout.getvalue())
            self.assertIn('{"tasks": []}',stdout.getvalue())
        self.assertEqual(call.call_count,3)

    def test_worker_queue_failure_preserves_resume_and_reports_recovery(self):
        event={'actor':self.record['actor'],'request_id':str(uuid.uuid4()),'timestamp':'2026-09-16T00:00:00+00:00'}
        replies=[{'returncode':0,'stdout':json.dumps({'resume':event}),'stderr':''},
                 {'returncode':0,'stdout':'Instructions','stderr':''},
                 {'returncode':7,'stdout':'','stderr':'queue unavailable\n'}]
        argv=['worker.py','--root','/srv/runtime','--project','example','--actor',self.record['actor'],'resume','--request-id',event['request_id']]
        with patch.object(sys,'argv',argv),patch.object(worker,'request',side_effect=replies) as call,patch('sys.stderr',new_callable=io.StringIO) as stderr,patch('sys.stdout',new_callable=io.StringIO) as stdout:
            self.assertEqual(worker.main(),7)
            self.assertIn(event['request_id'],stdout.getvalue())
            self.assertIn('queue unavailable',stderr.getvalue())
            self.assertIn('resume was recorded',stderr.getvalue())
            self.assertIn('work --mine',stderr.getvalue())
        self.assertEqual(call.call_count,3)

    def test_worker_onboarding_failure_does_not_request_work(self):
        event={'actor':self.record['actor'],'request_id':str(uuid.uuid4()),'timestamp':'2026-09-16T00:00:00+00:00'}
        replies=[{'returncode':0,'stdout':json.dumps({'resume':event}),'stderr':''},
                 {'returncode':2,'stdout':'','stderr':'onboarding failed'}]
        argv=['worker.py','--root','/srv/runtime','--project','example','--actor',self.record['actor'],'resume','--request-id',event['request_id']]
        with patch.object(sys,'argv',argv),patch.object(worker,'request',side_effect=replies) as call,patch('sys.stderr',new_callable=io.StringIO),patch('sys.stdout',new_callable=io.StringIO) as stdout:
            self.assertEqual(worker.main(),2)
            self.assertIn(event['request_id'],stdout.getvalue())
        self.assertEqual(call.call_count,2)

    def test_worker_queue_transport_error_explains_resume_is_durable(self):
        event={'actor':self.record['actor'],'request_id':str(uuid.uuid4()),'timestamp':'2026-09-16T00:00:00+00:00'}
        replies=[{'returncode':0,'stdout':json.dumps({'resume':event}),'stderr':''},
                 {'returncode':0,'stdout':'Instructions','stderr':''},RuntimeError('transport unavailable')]
        argv=['worker.py','--root','/srv/runtime','--project','example','--actor',self.record['actor'],'resume','--request-id',event['request_id']]
        with patch.object(sys,'argv',argv),patch.object(worker,'request',side_effect=replies),patch('sys.stderr',new_callable=io.StringIO),patch('sys.stdout',new_callable=io.StringIO) as stdout:
            with self.assertRaisesRegex(RuntimeError,'resume was recorded.*transport unavailable'):worker.main()
            self.assertIn(event['request_id'],stdout.getvalue())

    def test_worker_preserves_explicit_retry_id_and_stops_on_resume_failure(self):
        rid=str(uuid.uuid4());argv=['worker.py','--root','/srv/runtime','--project','example','--actor',self.record['actor'],'resume','--request-id',rid]
        with patch.object(sys,'argv',argv),patch.object(worker,'request',return_value={'returncode':1,'stdout':'','stderr':'failure'}) as call,patch('sys.stderr',new_callable=io.StringIO),patch.object(worker.uuid,'uuid4') as allocate:
            self.assertEqual(worker.main(),1)
        allocate.assert_not_called();self.assertEqual(call.call_count,1)
        self.assertEqual(call.call_args.args[3],['resume','--request-id',rid])

    def test_worker_resume_without_actor_does_not_call_server(self):
        with patch.object(sys,'argv',['worker.py','--root','/srv/runtime','--project','example','resume']),patch.object(worker,'request') as call,patch('sys.stderr',new_callable=io.StringIO):
            with self.assertRaises(SystemExit):worker.main()
        call.assert_not_called()


if __name__=='__main__':unittest.main()
