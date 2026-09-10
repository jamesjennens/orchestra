import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from client import request
from render import render

class CoreTests(unittest.TestCase):
    def test_plan_text_does_not_enter_ssh_command(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'plan with spaces.md'
            text="don't change intent; $(touch /tmp/not-executed) `anything`\n\"quoted\" café"
            path.write_text(text,encoding='utf-8')
            cfg={'host':'sample','endpoint':'/srv/kit/endpoint.py','root':'/srv/state'}
            with patch('client.subprocess.run',return_value=subprocess.CompletedProcess([],0,json.dumps({'returncode':0,'stdout':'ok','stderr':''}),'')) as run:
                request(cfg,'sample','alice',['create','title','--body-file',str(path)])
                argv=run.call_args.args[0];payload=json.loads(run.call_args.kwargs['input'])
                self.assertNotIn(text,' '.join(argv))
                self.assertEqual(payload['attachments']['0']['text'],text)
                self.assertEqual(payload['args'],['create','title','@attachment:0'])

    def test_bad_host_rejected(self):
        with self.assertRaises(ValueError):request({'host':'-oProxyCommand=bad','endpoint':'/x','root':'/y'},'p','a',[])

    def test_ssh_failure_has_no_retry(self):
        with patch('client.subprocess.run',return_value=subprocess.CompletedProcess([],255,'','offline')) as run:
            with self.assertRaisesRegex(RuntimeError,'uncertain'):
                request({'host':'sample','endpoint':'/x','root':'/y'},'sample','alice',['create','example'])
            self.assertEqual(run.call_count,1)

    def test_render_backlinks_and_closed_task(self):
        rows=[{'id':'p-job','title':'Job','status':'open','issue_type':'epic'},
              {'id':'p-task','title':'Task','status':'closed','issue_type':'task','dependencies':[{'type':'parent-child','depends_on_id':'p-job'}],
               'comments':[{'id':'one','created_at':'2026-09-10T01:00:00Z','author':'a','text':'Initial assertion'},
                           {'id':'two','created_at':'2026-09-11T01:00:00Z','author':'b','text':'Supersedes: p-task-cone\nCorrected assertion'}]}]
        with tempfile.TemporaryDirectory() as d:
            render(rows,d)
            earlier=(Path(d)/'journal/2026-09-10.md').read_text()
            self.assertIn('p-task-ctwo',earlier)
            self.assertIn('Initial assertion',earlier)
            self.assertNotIn('p-task', (Path(d)/'CURRENT.md').read_text())
            self.assertIn('p-task', (Path(d)/'jobs/p-job.md').read_text())

    def test_render_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):render([{'id':'../../escape','title':'x','status':'open'}],d)

if __name__=='__main__':unittest.main()
