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
                {'action':'capability','args':['accept'],'project':'area'}]
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
