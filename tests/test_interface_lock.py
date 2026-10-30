"""Real file-lock ordering at the two canonical participating route entry points."""
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import interface_lock
from http_authority import file_lock


class InterfaceLockTests(unittest.TestCase):
    @unittest.skipIf(os.name=='nt','endpoint is POSIX-only; file_lock itself has Windows coverage')
    def test_two_participating_routes_in_both_orders_before_project_locks(self):
        import endpoint
        routes=[{'action':'interface','args':['accept'],'project':'program'},
                {'action':'ref','args':['accept'],'project':'area'}]
        for first,second in (routes,list(reversed(routes))):
            with self.subTest(first=first['action']),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);entered=threading.Event();second_entered=threading.Event();release=threading.Event()
                order=[]
                def effect(root,request,*args):
                    name=request['action']
                    # Different projects: their individual project locks cannot serialize this race.
                    with file_lock(root/(request['project']+'.lock')):
                        order.append(name+' start')
                        if name==first['action']:
                            entered.set();self.assertTrue(release.wait(5))
                        else:second_entered.set()
                        order.append(name+' end')
                    return {'returncode':0}
                try:
                    with patch.object(endpoint,'_execute',effect),ThreadPoolExecutor(2) as pool:
                        one=pool.submit(endpoint.execute,root,first)
                        self.assertTrue(entered.wait(5))
                        two=pool.submit(endpoint.execute,root,second)
                        self.assertFalse(second_entered.wait(.15),'second route entered before first released deployment lock')
                        release.set();self.assertEqual(0,one.result(5)['returncode']);self.assertEqual(0,two.result(5)['returncode'])
                finally:release.set()
                self.assertEqual([first['action']+' start',first['action']+' end',second['action']+' start',second['action']+' end'],order)

    @unittest.skipIf(os.name=='nt','endpoint imports POSIX fcntl')
    def test_busy_acquisition_is_503_before_any_project_effect(self):
        import endpoint
        original=interface_lock.held
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);ready=threading.Event();release=threading.Event()
            def holder():
                with original(root):ready.set();release.wait(5)
            with ThreadPoolExecutor(1) as pool:
                future=pool.submit(holder);self.assertTrue(ready.wait(5))
                try:
                    with patch.object(interface_lock,'held',lambda root:original(root,timeout=.02)),patch.object(endpoint,'_execute') as effect:
                        result=endpoint.execute(root,{'action':'interface','args':['accept']})
                        self.assertEqual(503,result['records_status']);effect.assert_not_called()
                finally:release.set();future.result(5)

    @unittest.skipIf(os.name=='nt','endpoint is POSIX-only; HTTP lock uses the portable file_lock')
    def test_project_membership_routes_and_interface_accept_serialize_in_both_orders(self):
        import endpoint
        import http_service
        from types import SimpleNamespace
        # Host creation is canonical for provisioning, but its owner-membership save
        # is in this process. Its endpoint does not own the interface mutex.
        for route,canonical in [('projects.create',False),('projects.host-create',True),('projects.confirm',False)]:
            for first in ('interface','membership'):
                with self.subTest(route=route,first=first),tempfile.TemporaryDirectory() as temp:
                    root=Path(temp);entered=threading.Event();second_entered=threading.Event();release=threading.Event();order=[]
                    def effect(name):
                        with file_lock(root/(name+'.lock')):
                            order.append(name+' start')
                            if name==first:
                                entered.set();self.assertTrue(release.wait(5))
                            else:second_entered.set()
                            order.append(name+' end')
                        return {'returncode':0}
                    handler=SimpleNamespace(backend=SimpleNamespace(root=root),
                        _mutate_unlocked=lambda ctx,name,pid,fn,**kwargs:fn())
                    def membership():
                        return http_service.ApiHandler._mutate(handler,None,route,None,
                            lambda:effect('membership'),capability='fixture',canonical=canonical)
                    def interface():return endpoint.execute(root,{'action':'interface','args':['accept'],'project':'program'})
                    calls={'interface':interface,'membership':membership}
                    with patch.object(endpoint,'_execute',lambda *args,**kwargs:effect('interface')),ThreadPoolExecutor(2) as pool:
                        try:
                            one=pool.submit(calls[first]);self.assertTrue(entered.wait(5))
                            other='membership' if first=='interface' else 'interface'
                            two=pool.submit(calls[other]);self.assertFalse(second_entered.wait(.15))
                        finally:release.set()
                        self.assertEqual(0,one.result(5)['returncode']);self.assertEqual(0,two.result(5)['returncode'])
                    self.assertEqual([first+' start',first+' end',other+' start',other+' end'],order)

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
    def test_actual_capability_route_mutex_only_for_interface_changes_in_both_orders(self):
        import endpoint
        import capability_records as cr
        from contextlib import ExitStack
        from test_capability_records import entry
        import json
        for touches in (False,True):
            for first in ('interface','capability'):
                with self.subTest(touches=touches,first=first),tempfile.TemporaryDirectory() as temp:
                    root=Path(temp);area=root/'projects/area';(area/'.beads').mkdir(parents=True)
                    (area/'.beads/metadata.json').write_text('{}')
                    entered=threading.Event();second_entered=threading.Event();release=threading.Event()
                    def effect(name):
                        if name==first:
                            entered.set();self.assertTrue(release.wait(5))
                        else:second_entered.set()
                        return {'returncode':0}
                    cap=entry();cap.pop('operation',None)
                    if touches:cap['interfaces']=[]
                    request={'project':'area','actor':'fixture','action':'capability',
                        'args':['propose','@attachment:p'],'attachments':{'p':{'flag':'--file','text':json.dumps(cap)}}}
                    dispatch=endpoint._execute
                    def route(root,request,*args):
                        if request['action']=='interface':return effect('interface')
                        return dispatch(root,request,*args)
                    with ExitStack() as stack:
                        stack.enter_context(patch.object(endpoint,'_execute',route))
                        stack.enter_context(patch.object(endpoint,'guarded_write',lambda root,req,journal,fn,**kw:fn()))
                        stack.enter_context(patch.object(cr,'write',lambda *a,**kw:effect('capability')))
                        stack.enter_context(patch.object(endpoint,'configured_operators',return_value=[]))
                        stack.enter_context(patch.object(endpoint,'configured_verifiers',return_value=[]))
                        pool=stack.enter_context(ThreadPoolExecutor(2))
                        calls={'interface':lambda:endpoint.execute(root,{'action':'interface','args':['accept'],'project':'prog'}),
                               'capability':lambda:endpoint.execute(root,request)}
                        try:
                            one=pool.submit(calls[first]);self.assertTrue(entered.wait(5))
                            two=pool.submit(calls['capability' if first=='interface' else 'interface'])
                            self.assertEqual(not touches,second_entered.wait(.2))
                        finally:release.set()
                        self.assertEqual(0,one.result(5)['returncode']);self.assertEqual(0,two.result(5)['returncode'])


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
