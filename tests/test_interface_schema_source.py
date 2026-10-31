"""Exact source proof uses immutable objects, not checkout contents or a claim."""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
KIT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(KIT))
import interface_schema_source as source


class SourceProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=Path(self.tmp.name);self.git('init','-q')
        (self.repo/'schemas').mkdir();self.body=b'{"type":"string"}'
        (self.repo/'schemas'/'example.json').write_bytes(self.body)
        self.git('add','.');self.git('commit','-qm','Synthetic schema')
        self.commit=self.git('rev-parse','HEAD').strip().decode()
        self.pointer={'type':'repo-path','commit':self.commit,'path':'schemas/example.json',
                      'anchor':None,'sha256':hashlib.sha256(self.body).hexdigest()}

    def git(self,*args):
        return subprocess.check_output(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',*args],cwd=self.repo,stderr=subprocess.DEVNULL)

    def test_pinned_bytes_and_commit_are_proved_despite_a_changed_checkout(self):
        (self.repo/'schemas'/'example.json').write_bytes(b'Uncommitted different schema')
        before=self.git('status','--porcelain')
        body,proof=source.prove(self.pointer,self.repo)
        self.assertEqual(self.body,body);self.assertEqual(self.commit,proof['source_commit'])
        self.assertEqual(4,len(proof['objects']));self.assertEqual(before,self.git('status','--porcelain'))

    def test_pinned_digest_and_commit_mismatch_unknown(self):
        for changed in (dict(self.pointer,sha256='0'*64),dict(self.pointer,commit='0'*40)):
            with self.subTest(changed=changed),self.assertRaises(source.Unproved):source.prove(changed,self.repo)

    def test_commit_tree_and_blob_object_hashes_are_independently_checked(self):
        original=source._git
        for kind in ('commit','tree','blob'):
            def tampered(repository,args,limit,deadline):
                code,body=original(repository,args,limit,deadline)
                if args[:2]==['cat-file',kind] and body:body=body[:-1]+bytes([body[-1]^1])
                return code,body
            with self.subTest(kind=kind),patch.object(source,'_git',side_effect=tampered):
                with self.assertRaisesRegex(source.Unproved,'object hash mismatch'):source.prove(self.pointer,self.repo)

    def test_path_fragments_urls_symlinks_and_partial_sources_refuse(self):
        for changed in (dict(self.pointer,path='../escape'),dict(self.pointer,path='/absolute'),
                        dict(self.pointer,path='schemas//example.json'),dict(self.pointer,anchor='fragment'),
                        dict(self.pointer,type='url',url='https://invalid.example/s')):
            with self.subTest(changed=changed),self.assertRaises(source.Unproved):source.prove(changed,self.repo)
        if hasattr(os,'symlink'):
            try:(self.repo/'linked.json').symlink_to('schemas/example.json')
            except OSError:pass
            else:
                self.git('add','linked.json');self.git('commit','-qm','Synthetic link')
                pointed=dict(self.pointer,commit=self.git('rev-parse','HEAD').strip().decode(),path='linked.json')
                with self.assertRaisesRegex(source.Unproved,'regular blob'):source.prove(pointed,self.repo)
        self.git('config','remote.origin.promisor','true')
        with self.assertRaisesRegex(source.Unproved,'partial/promisor'):source.prove(self.pointer,self.repo)

    def test_oversize_blob_and_source_time_budget_refuse(self):
        with patch.object(source,'OBJECT_MAX',3):
            with self.assertRaisesRegex(source.Unproved,'object budget'):source.prove(self.pointer,self.repo)
        with patch.object(source,'TIMEOUT',0):
            with self.assertRaisesRegex(source.Unproved,'timed out'):source.prove(self.pointer,self.repo)

    def test_git_environment_disables_protocols_and_ignores_inherited_git_overrides(self):
        original=subprocess.Popen;seen=[]
        def captured(command,**kwargs):
            seen.append((command,kwargs['env']));return original(command,**kwargs)
        with patch.dict(os.environ,{'GIT_DIR':'/not/the/configured/repository','GIT_CONFIG_COUNT':'99'}),patch.object(source.subprocess,'Popen',side_effect=captured):
            self.assertEqual(self.body,source.prove(self.pointer,self.repo)[0])
        self.assertTrue(seen)
        for command,env in seen:
            self.assertIn('--no-replace-objects',command);self.assertNotIn('GIT_DIR',env)
            self.assertNotIn('GIT_CONFIG_COUNT',env);self.assertEqual('',env['GIT_ALLOW_PROTOCOL'])
            self.assertEqual('1',env['GIT_NO_LAZY_FETCH'])

    def test_unavailable_git_is_an_unproved_source(self):
        with patch.object(source.subprocess,'Popen',side_effect=OSError('synthetic unavailable git')):
            with self.assertRaisesRegex(source.Unproved,'reader unavailable'):source.prove(self.pointer,self.repo)

    def test_interrupted_read_kills_and_reaps_its_child(self):
        original_popen=source.subprocess.Popen;original_join=source.threading.Thread.join
        processes=[];joins=[0]
        def started(*args,**kwargs):
            process=original_popen(*args,**kwargs);processes.append(process);return process
        def interrupted(reader,*args,**kwargs):
            joins[0]+=1
            if joins[0]==1:raise KeyboardInterrupt('synthetic interruption')
            return original_join(reader,*args,**kwargs)
        with patch.object(source.subprocess,'Popen',side_effect=started),patch.object(source.threading.Thread,'join',new=interrupted):
            with self.assertRaises(KeyboardInterrupt):source.prove(self.pointer,self.repo)
        self.assertEqual(1,len(processes));self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)

    def test_included_partial_clone_configuration_is_also_refused(self):
        included=self.repo/'partial.gitconfig'
        included.write_text('[remote "synthetic"]\n\tpromisor = true\n')
        self.git('config','include.path',str(included))
        with self.assertRaisesRegex(source.Unproved,'partial/promisor'):source.prove(self.pointer,self.repo)

    def test_interrupted_blocked_source_child_is_killed_reaped_and_closed(self):
        import threading
        class Output:
            def __init__(self):
                self.entered=threading.Event();self.release=threading.Event();self.closed=False
            def read(self,limit):
                self.entered.set();self.release.wait(5);return b''
            def close(self):self.closed=True
        class Child:
            def __init__(self):self.stdout=Output();self.killed=False;self.reaped=False
            def poll(self):return -9 if self.killed else None
            def kill(self):self.killed=True;self.stdout.release.set()
            def wait(self,**kwargs):
                self.reaped=True
                if not self.killed:
                    self.stdout.release.set()
                    raise AssertionError('Interrupted source child must be killed before reaping')
                return -9
        child=Child();self.addCleanup(child.stdout.release.set)
        original_join=threading.Thread.join;calls=[0]
        def interrupt(reader,*args,**kwargs):
            calls[0]+=1
            if calls[0]==1:
                self.assertTrue(child.stdout.entered.wait(5),'source reader never started')
                raise KeyboardInterrupt('synthetic blocked read interruption')
            return original_join(reader,*args,**kwargs)
        with patch.object(source.subprocess,'Popen',return_value=child),patch.object(threading.Thread,'join',new=interrupt):
            with self.assertRaises(KeyboardInterrupt):source.prove(self.pointer,self.repo)
        self.assertTrue(child.killed);self.assertTrue(child.reaped);self.assertTrue(child.stdout.closed)
