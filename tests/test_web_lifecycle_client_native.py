"""Page/reload and a copied standard-library client against real native HTTP routes."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest

KIT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(KIT),str(KIT/'tests')]
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers
import test_web_merge_slot as slot_fixture
from test_http_web import run_node_module


@unittest.skipIf(fixture.endpoint is None,'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None,'no pinned native bd (set ORCHESTRA_BD_BIN)')
class NativeLifecycleClientTests(RulesHelpers,unittest.TestCase):
    web=slot_fixture.NativeWebSlotTests.web

    def test_page_reload_and_copied_cli_use_the_actual_native_routes(self):
        node=shutil.which('node')
        if not node:self.skipTest('Node unavailable; native lifecycle page not exercised')
        harness,token=self.web();task=self.task('Native browser lifecycle')
        base='http://127.0.0.1:'+str(harness.port)
        rendered=run_node_module(self,node,'await import(process.argv[1])',
            (KIT/'tests/web_lifecycle_screen.mjs').as_uri(),(KIT/'tests/web_dom_shim.mjs').as_uri(),
            (KIT/'web/js/api.js').as_uri(),(KIT/'web/js/views/lifecycle.js').as_uri(),
            base,'pp',token,task,'native')
        self.assertEqual(rendered.returncode,0,rendered.stderr[-4000:])
        page=json.loads(rendered.stdout.strip().splitlines()[-1])
        self.assertTrue(page['persistedRetry']);self.assertTrue(page['sameKey']);self.assertTrue(page['sameBody'])
        self.assertTrue(page['escaped']);self.assertTrue(page['integrationDisabled'])
        committed=[entry for entry in harness.service.state['audit'] if entry['action']=='lifecycle.scope'
                   and entry['outcome']=='committed' and entry.get('task')==task]
        self.assertEqual(len(committed),1)
        other=self.task('Native copied client lifecycle')
        copied=self.root/'copied_http_client.py';shutil.copyfile(KIT/'http_client.py',copied)
        spec=importlib.util.spec_from_file_location('copied_native_lifecycle_client',copied)
        client_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(client_module)
        client=client_module.Client(base);client.token=token
        scope=dict(source_commit='a'*40,integration_commit='b'*40,release_id='synthetic-native-client',environment='native-client')
        first=client.set_lifecycle_scope('pp',other,scope,None,['copied native client scope'],key='native-client-scope')
        self.assertEqual(first['dimension'],'lifecycle-scope')
        harness.backend.fail_next('lifecycle.fact')
        with self.assertRaises(client_module.UncertainOutcome) as lost:
            client.record_lifecycle_fact('pp',other,'tested','passed',scope,['copied native client'],key='native-client-tested')
        before=self.rows()
        retried=client.record_lifecycle_fact('pp',other,'tested','passed',scope,['copied native client'],key=lost.exception.key)
        self.assertEqual(retried['value'],'passed');self.assertEqual(self.rows(),before)
        body=self.root/'native-cli-body.json';body.write_text(json.dumps(dict(scope=scope,value='pending',
            evidence=['synthetic waiting'],provenance='reported',trigger='Awaiting native evidence')))
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=client_module.main(['--base-url',base,'--token='+token,'lifecycle','pp',other,'fact',
                '--dimension','implemented','--file',str(body),'--idempotency-key','native-client-cli'])
        self.assertEqual(code,0);self.assertEqual(json.loads(output.getvalue())['dimension'],'implemented')
        self.assertEqual(client.lifecycle('pp',other)['facts']['implemented']['trigger'],'Awaiting native evidence')
        artifact=os.environ.get('ORCHESTRA_LIFECYCLE_TEST_EXPORTS')
        if artifact:
            folder=Path(artifact);folder.mkdir(parents=True,exist_ok=True)
            (folder/'native-client-after.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in self.rows()))
            (folder/'native-page-result.json').write_text(json.dumps(page,indent=2))
            (folder/'native-cli-result.json').write_text(output.getvalue())


if __name__=='__main__':unittest.main()
