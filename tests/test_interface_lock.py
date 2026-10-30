"""Real file-lock ordering at the two canonical participating route entry points."""
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from unittest.mock import patch
import interface_lock
from http_authority import file_lock


class InterfaceLockTests(unittest.TestCase):
    def setup_routes(self,root,touches=True):
        from test_capability_records import entry
        from test_reference_records import entry as ref_entry
        import json
        for name in ('prog','area'):
            path=root/'projects'/name;(path/'.beads').mkdir(parents=True)
            (path/'.beads/metadata.json').write_text('{}')
        cap=entry();cap.pop('operation',None)
        if touches:cap['interfaces']=[]
        ref=ref_entry(key='program.area.area',statement='Area: area\nState: in',authority={'type':'decision','id':'decision-42'})
        ref.pop('operation',None)
        def request(action,project,payload):
            return {'project':project,'actor':'fixture','action':action,'args':['propose','@attachment:p'],
                'attachments':{'p':{'flag':'--file','text':json.dumps(payload)}}}
        return request('ref','prog',ref),request('capability','area',cap)

    @unittest.skipIf(os.name=='nt','endpoint is POSIX-only')
    def test_two_participating_routes_in_both_orders_before_project_locks(self):
        import endpoint,capability_records,reference_records
        from contextlib import ExitStack
        for reverse in (False,True):
            with self.subTest(reverse=reverse),tempfile.TemporaryDirectory() as temp,ExitStack() as stack:
                root=Path(temp);routes=list(self.setup_routes(root))
                if reverse:routes.reverse()
                entered=threading.Event();second=threading.Event();release=threading.Event();order=[]
                first=routes[0]['action']
                def effect(name):
                    order.append(name+' start')
                    if name==first:entered.set();self.assertTrue(release.wait(5))
                    else:second.set()
                    order.append(name+' end');return {'returncode':0}
                stack.enter_context(patch.object(endpoint,'guarded_write',lambda root,req,journal,fn,**kw:fn()))
                stack.enter_context(patch.object(endpoint,'configured_operators',return_value=[]))
                stack.enter_context(patch.object(endpoint,'configured_verifiers',return_value=[]))
                stack.enter_context(patch.object(reference_records,'apply_native',lambda *a,**kw:effect('ref')))
                stack.enter_context(patch.object(capability_records,'write',lambda *a,**kw:effect('capability')))
                pool=stack.enter_context(ThreadPoolExecutor(2))
                try:
                    one=pool.submit(endpoint.execute,root,routes[0]);self.assertTrue(entered.wait(5))
                    two=pool.submit(endpoint.execute,root,routes[1]);self.assertFalse(second.wait(.15))
                finally:release.set()
                self.assertEqual(0,one.result(5)['returncode']);self.assertEqual(0,two.result(5)['returncode'])
                self.assertEqual([first+' start',first+' end',routes[1]['action']+' start',routes[1]['action']+' end'],order)

    @unittest.skipIf(os.name=='nt','endpoint is POSIX-only')
    def test_busy_acquisition_is_503_before_any_project_effect(self):
        import endpoint,capability_records
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as temp,ExitStack() as stack:
            root=Path(temp);_,request=self.setup_routes(root)
            original=interface_lock.held;ready=threading.Event();release=threading.Event()
            def holder():
                with original(root):ready.set();release.wait(5)
            pool=stack.enter_context(ThreadPoolExecutor(1));held=pool.submit(holder);self.assertTrue(ready.wait(5))
            effect=stack.enter_context(patch.object(capability_records,'write'))
            stack.enter_context(patch.object(endpoint,'configured_operators',return_value=[]))
            stack.enter_context(patch.object(interface_lock,'held',lambda root:original(root,timeout=.02)))
            try:
                result=endpoint.execute(root,request);self.assertEqual(503,result['records_status']);effect.assert_not_called()
            finally:release.set();held.result(5)

    def test_store_only_membership_and_registration_routes_bypass_semantic_mutex(self):
        import http_service
        from types import SimpleNamespace
        for route in ('members.set','accounts.update','agents.disable','projects.create','projects.host-create','projects.confirm'):
            with self.subTest(route=route),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);entered=threading.Event()
                handler=SimpleNamespace(backend=SimpleNamespace(root=root),
                    _mutate_unlocked=lambda ctx,name,pid,fn,**kwargs:fn())
                with interface_lock.held(root),ThreadPoolExecutor(1) as pool:
                    def run():
                        return http_service.ApiHandler._mutate(handler,None,route,None,
                            lambda:entered.set(),capability='fixture')
                    job=pool.submit(run);self.assertTrue(entered.wait(1));job.result(1)

    @unittest.skipIf(os.name=='nt','endpoint is POSIX-only')
    def test_actual_capability_route_mutex_only_for_interface_changes(self):
        import endpoint,capability_records
        from contextlib import ExitStack
        for touches in (False,True):
            with self.subTest(touches=touches),tempfile.TemporaryDirectory() as temp,ExitStack() as stack:
                root=Path(temp);_,request=self.setup_routes(root,touches=touches);entered=threading.Event()
                stack.enter_context(patch.object(endpoint,'guarded_write',lambda root,req,journal,fn,**kw:fn()))
                stack.enter_context(patch.object(endpoint,'configured_operators',return_value=[]))
                stack.enter_context(patch.object(endpoint,'configured_verifiers',return_value=[]))
                stack.enter_context(patch.object(capability_records,'write',lambda *a,**kw:(entered.set() or {})))
                with ThreadPoolExecutor(1) as pool:
                    with interface_lock.held(root):
                        job=pool.submit(endpoint.execute,root,request)
                        self.assertEqual(not touches,entered.wait(.15))
                    self.assertEqual(0,job.result(5)['returncode'])

    @unittest.skipIf(os.name=='nt','endpoint is POSIX-only')
    def test_project_refusal_precedes_mutex_and_dispatch(self):
        import endpoint
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(interface_lock,'held') as mutex,patch.object(endpoint,'_execute') as dispatch:
                with self.assertRaisesRegex(ValueError,'Unknown/uninitialized project'):
                    endpoint.execute(Path(temp),{'project':'private','action':'interface','args':['accept']},
                                     key_projects=frozenset({'public'}))
                mutex.assert_not_called();dispatch.assert_not_called()
                self.assertEqual([],list(Path(temp).iterdir()))


    @unittest.skipIf(os.name=='nt','endpoint is POSIX-only')
    def test_capability_preflight_authority_refusal_is_403_before_any_native_read(self):
        import endpoint
        import http_authority
        from types import SimpleNamespace
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as temp,ExitStack() as stack:
            root=Path(temp);area=root/'projects/area';(area/'.beads').mkdir(parents=True)
            (area/'.beads/metadata.json').write_text('{}')
            config=SimpleNamespace(store=root/'state.json')
            uid='usr_'+'d'*16
            request={'project':'area','actor':uid,'action':'capability','args':['accept','@attachment:p'],
                'authority':{'user_id':uid,'via':'session'}}
            for name in ('http_actor_denial','descriptor_actor_denial','block_mismatch_denial'):
                stack.enter_context(patch.object(endpoint,name,return_value=None))
            stack.enter_context(patch.object(endpoint,'verified_web_authority',return_value=True))
            stack.enter_context(patch.object(endpoint,'configured_coordinator_rules',return_value={}))
            stack.enter_context(patch.object(endpoint,'configured_operators',return_value=[]))
            stack.enter_context(patch.object(http_authority,'read_state',return_value={'memberships':{}}))
            native=stack.enter_context(patch.object(endpoint.native,'run',side_effect=AssertionError('unauthorized native read')))
            result=endpoint.execute(root,request,authority_config=config,require_authority=True)
            self.assertEqual(403,result['records_status']);native.assert_not_called()


    @unittest.skipIf(os.name=='nt','endpoint is POSIX-only')
    def test_unrelated_reference_proposal_bypasses_semantic_mutex(self):
        import endpoint,reference_records,json
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as temp,ExitStack() as stack:
            root=Path(temp);request,_=self.setup_routes(root);entered=threading.Event()
            body=json.loads(request['attachments']['p']['text']);body['key']='ordinary.unrelated'
            request['attachments']['p']['text']=json.dumps(body)
            stack.enter_context(patch.object(endpoint,'guarded_write',lambda root,req,journal,fn,**kw:fn()))
            stack.enter_context(patch.object(endpoint,'configured_operators',return_value=[]))
            stack.enter_context(patch.object(reference_records,'apply_native',lambda *a,**kw:(entered.set() or {})))
            with ThreadPoolExecutor(1) as pool:
                with interface_lock.held(root):
                    job=pool.submit(endpoint.execute,root,request)
                    self.assertTrue(entered.wait(1),'unrelated reference waited for the interface mutex')
                self.assertEqual(0,job.result(5)['returncode'])

    @unittest.skipIf(os.name=='nt','host administration uses POSIX runtime paths')
    def test_unrelated_host_verifier_configuration_bypasses_semantic_mutex(self):
        import admin,json,io,contextlib
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'projects').mkdir()
            (root/'deployment.private.json').write_text(json.dumps({'password':'synthetic','unit':'none','operators':[]}))
            done=threading.Event()
            def change():
                try:admin.main()
                finally:done.set()
            with patch.object(sys,'argv',['admin.py','--root',str(root),'verifiers','add','operators']), \
                 contextlib.redirect_stdout(io.StringIO()),ThreadPoolExecutor(1) as pool:
                with interface_lock.held(root):
                    job=pool.submit(change)
                    self.assertTrue(done.wait(1),'unrelated verifier configuration waited for the interface mutex')
                job.result(5)
            self.assertIn('operators',json.loads((root/'deployment.private.json').read_text())['verifiers'])


    @unittest.skipIf(os.name=='nt','host administration uses POSIX runtime paths')
    def test_host_native_restore_takes_semantic_mutex_without_authority_restore_flags(self):
        import admin
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            def effect():
                with self.assertRaises(TimeoutError):
                    with file_lock(root/'.interface-mutations.lock',timeout=.02):pass
                return 0
            with patch.object(sys,'argv',['admin.py','--root',str(root),'restore-new']),patch.object(admin,'_main',side_effect=effect):
                self.assertEqual(0,admin.main())
