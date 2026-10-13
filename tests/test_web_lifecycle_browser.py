"""Real routes for the page and a standalone copied standard-library client."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import unittest

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
from test_http_agents import AgentHarness
from test_http_web import run_node_module
KIT=Path(__file__).resolve().parents[1]


class BrowserClientTests(AgentHarness):
    def setUp(self):
        super().setUp();self.token=self.admin_token();self.pid=self.create_project(self.token,'Browser lifecycle')
        made=self.request('POST','/v1/projects/'+self.pid+'/tasks',{'title':'Lifecycle client'},token=self.token)
        self.assertEqual(made.status,201,made.data);self.tid=made.data['id']
        self.base='http://127.0.0.1:'+str(self.port)

    def test_real_page_persisted_scope_retry_and_safe_evidence_dom(self):
        node=shutil.which('node')
        if not node:self.skipTest('Node unavailable; lifecycle page not exercised')
        done=run_node_module(self,node,'await import(process.argv[1])',
            (KIT/'tests/web_lifecycle_screen.mjs').as_uri(),(KIT/'tests/web_dom_shim.mjs').as_uri(),
            (KIT/'web/js/api.js').as_uri(),(KIT/'web/js/views/lifecycle.js').as_uri(),
            self.base,self.pid,self.token,self.tid)
        self.assertEqual(done.returncode,0,done.stderr[-4000:])
        result=json.loads(done.stdout.strip().splitlines()[-1])
        self.assertTrue(result['sameKey']);self.assertTrue(result['sameBody']);self.assertTrue(result['escaped'])
        self.assertTrue(result['integrationDisabled']);self.assertEqual(result['tested'],'passed')
        committed=[a for a in self.service.state['audit'] if a['action']=='lifecycle.scope' and a['outcome']=='committed']
        self.assertEqual(len(committed),1)

    def test_copied_client_and_named_cli_reconcile_without_another_effect(self):
        copied=self.tmp_path/'copied_http_client.py';shutil.copyfile(KIT/'http_client.py',copied)
        spec=importlib.util.spec_from_file_location('copied_lifecycle_client',copied)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        client=module.Client(self.base);client.token=self.token
        scope=dict(source_commit='a'*40,integration_commit='b'*40,release_id='copy-proof',environment='synthetic')
        first=client.set_lifecycle_scope(self.pid,self.tid,scope,None,['copied client scope'],key='copy-scope-1')
        self.assertEqual(first['dimension'],'lifecycle-scope')
        self.backend.fail_next('lifecycle.fact')
        with self.assertRaises(module.UncertainOutcome) as raised:
            client.record_lifecycle_fact(self.pid,self.tid,'tested','passed',scope,['copied client tests'],key='copy-fact-1')
        version=self.service.state['canonical']['tasks'][self.tid]['version']
        repeated=client.record_lifecycle_fact(self.pid,self.tid,'tested','passed',scope,['copied client tests'],key=raised.exception.key)
        self.assertEqual(repeated['value'],'passed')
        self.assertEqual(self.service.state['canonical']['tasks'][self.tid]['version'],version)
        body=self.tmp_path/'lifecycle-body.json';body.write_text(json.dumps(dict(scope=scope,value='pending',
            evidence=['waiting'],provenance='reported',trigger='Awaiting further evidence')),encoding='utf-8')
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            result=module.main(['--base-url',self.base,'--token',self.token,'lifecycle',self.pid,self.tid,
                'fact','--dimension','implemented','--file',str(body),'--idempotency-key','copied-cli-fact-1'])
        self.assertEqual(result,0);self.assertEqual(json.loads(output.getvalue())['dimension'],'implemented')
        self.assertEqual(client.lifecycle(self.pid,self.tid)['facts']['implemented']['trigger'],'Awaiting further evidence')


if __name__=='__main__':unittest.main()
