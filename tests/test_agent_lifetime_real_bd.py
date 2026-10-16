"""The approved lifetime/extension on disposable native bd and the actual released HTTP reader."""
import copy
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock
KIT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(KIT),str(KIT/'tests')]
import http_auth
import http_client
import test_bd_label_aliases as rb
import test_claim_held as held
import test_coordinator_grant_real as native
DAY=86400


@unittest.skipIf(rb.endpoint is None,'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None,'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@native.only_its_own_tests
class NativeLifetimeTests(held.RealStackTests):
    def test_extension_real_project_read_replay_restore_and_actual_released_http_reader(self):
        previous=os.environ.get('ORCHESTRA_CREDENTIAL_PREVIOUS_KIT')
        if not previous:self.skipTest('No explicit actual released kit; native old-reader proof not run')
        previous=Path(previous).resolve()
        self.assertTrue((previous/'provenance.json').is_file())
        tick=[self.store.now()];self.store.clock=lambda:tick[0]
        made=self.request('POST','/v1/agents',{'name':'Native lifetime synthetic',
            'projects':['pp'],'scopes':['read']},token=self.tokens['casey'])
        self.assertEqual(201,made.status,made.data)
        aid=made.data['agent']['id'];cid=made.data['credential']['id'];secret=made.data['credential']['secret']
        c=self.service.state['credentials'][cid];before=copy.deepcopy(c)
        self.assertIn('issued_raw',c)
        task=self.new('Synthetic lifetime native read')
        path='/v1/projects/pp/tasks/'+task
        self.assertEqual(200,self.request('GET',path,token=secret).status)
        tick[0]+=DAY
        self.service.agent_credential_ttl=90*DAY
        owner=self.login('alex','alex-password-1')[0]
        client=http_client.Client('http://127.0.0.1:%d'%self.port);client.token=owner
        result=client.extend_agent_credential(aid,cid,project='pp',key='native-extension')
        self.assertFalse(result['secret_available'])
        self.assertEqual(before['expires_at']+61*DAY,c['expires_at'])
        for key in ('id','token_hash','user_id','actor','scopes','created_at'):
            self.assertEqual(before[key],c[key])
        self.assertNotIn('issued_raw',c)
        extended=copy.deepcopy(c)
        self.assertEqual(result,client.extend_agent_credential(aid,cid,project='pp',key='native-extension'))
        self.assertEqual(extended,c)
        # Actual saved JSON + consistent existing SQLite sidecar, copied to a new own
        # directory using SQLite backup. No new backup family is needed for these fields.
        self.store.save()
        restored=self.tmp/'synthetic-auth-restore';restored.mkdir()
        state=restored/self.store.path.name
        shutil.copyfile(self.store.path,state)
        source=self.store.path.with_name(self.store.path.name+http_auth.RECORD_STORE_SUFFIX)
        target=state.with_name(state.name+http_auth.RECORD_STORE_SUFFIX)
        with sqlite3.connect(source) as src,sqlite3.connect(target) as dst:src.backup(dst)
        tick[0]+=30*DAY
        self.assertEqual(200,self.request('GET',path,token=secret).status)
        current=http_auth.Service(http_auth.Store(state,clock=lambda:tick[0]),agent_credential_days=1)
        self.assertEqual(aid,current.authenticate(secret).actor)
        self.assertEqual(extended['expires_at'],current.state['credentials'][cid]['expires_at'])
        # A new process imports the *actual released* code and serves one real HTTP
        # request on loopback from the restored pair. Secrets go through stdin only.
        code='''import json,sys
sys.path.insert(0,sys.argv[1])
from http_auth import Service,Store
from http_service import create_server,InProcessBackend
v=json.load(sys.stdin)
s=Service(Store(v['state'],clock=lambda:v['now']))
h=create_server(s,InProcessBackend(s),host='127.0.0.1',port=0)
import threading
finished=threading.Event()
Original=h.RequestHandlerClass
class Once(Original):
 def finish(self):
  try:super().finish()
  finally:finished.set()
h.RequestHandlerClass=Once
thread=threading.Thread(target=h.serve_forever,daemon=True);thread.start()
print(json.dumps({'port':h.server_address[1]}),flush=True)
if not finished.wait(30):raise RuntimeError('Actual released HTTP response did not finish')
h.shutdown();h.server_close();thread.join(timeout=5)
'''
        child=subprocess.Popen([sys.executable,'-B','-c',code,str(previous)],cwd=previous,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
            env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.addCleanup(lambda:child.kill() if child.poll() is None else None)
        child.stdin.write(json.dumps(dict(state=str(state),now=tick[0])));child.stdin.close();child.stdin=None
        line=child.stdout.readline();self.assertTrue(line,'Released HTTP child did not report its port')
        address=json.loads(line)
        conn=http.client.HTTPConnection('127.0.0.1',address['port'],timeout=30)
        try:
            conn.request('GET','/v1/sessions/current',headers={'Authorization':'Bearer '+secret})
            response=conn.getresponse();status=response.status;data=json.loads(response.read())
        finally:conn.close()
        out,err=child.communicate(timeout=45)
        self.assertEqual(0,child.returncode,err)
        self.assertNotIn(secret,line+out+err)
        self.assertEqual(200,status,data)
        self.assertEqual(cid,data['credential'])
        self.assertEqual(before['user_id'],data['user']['id'])
        # Actual released reader has honored the extended expiry past its fixed30days.
        # Current reader still refuses raw expiry with a pinned monotone floor.
        current.state['credentials'][cid]['expires_at']=tick[0]+500*DAY
        current.state['credentials'][cid]['agent_raw_expires_at']=tick[0]
        with self.assertRaises(http_auth.HttpError) as expired:current.authenticate(secret)
        self.assertEqual(401,expired.exception.status)
        # Live project removal refuses the completed retry; no second extension.
        owner=self.login('alex','alex-password-1')[0];client.token=owner
        self.service.state['agents'][aid]['projects']=[]
        frozen=copy.deepcopy(self.service.state['credentials'][cid])
        with self.assertRaises(http_client.HttpApiError) as refused:
            client.extend_agent_credential(aid,cid,project='pp',key='native-extension')
        self.assertEqual(404,refused.exception.status)
        self.assertEqual(frozen,self.service.state['credentials'][cid])
        # Native export checks its real records remain readable, without printing secrets.
        export=self.bd('export','--all')
        self.assertEqual(0,export.returncode,export.stderr)
        self.assertIn(task,export.stdout)
        evidence=os.environ.get('ORCHESTRA_LIFETIME_EVIDENCE')
        if evidence:
            directory=Path(evidence);directory.mkdir(parents=True,exist_ok=True)
            (directory/'native-export.jsonl').write_text(export.stdout,encoding='utf-8')
            (directory/'observations.json').write_text(json.dumps(dict(
                task=task,agent=aid,credential=cid,base_expiry=before['expires_at'],
                extended_expiry=extended['expires_at'],
                restored_pair=[state.name,target.name],
                released_reader=str(previous),released_http_status=status,
                released_http_whoami=data,raw_expiry_status=expired.exception.status,
                completed_retry_after_removed_grant=refused.exception.status,
                identity_hash_scopes_actor_unchanged=True,
                state_after_refused_retry_unchanged=True),indent=2),encoding='utf-8')

if __name__=='__main__':unittest.main()
