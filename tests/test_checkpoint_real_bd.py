"""Real bd write/read compatibility in a disposable runtime, both write orders.

Opt in with ORCHESTRA_BD_BIN and ORCHESTRA_CHECKPOINT_ROLLBACK_KIT pointing to a
fresh checkout of cd980cd. Nothing uses the binaries' original runtime/project.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import briefing as b
import test_bd_label_aliases as rb

ROLLBACK=os.environ.get('ORCHESTRA_CHECKPOINT_ROLLBACK_KIT')
OLD_RUN='''
import json,sys,subprocess
from pathlib import Path
import admin,briefing as b
root=Path(sys.argv[1]);project=Path(sys.argv[2]);task=sys.argv[3]
def run(args):
    p=subprocess.run([str(root/'bin'/'bd'),'--directory',str(project),'--sandbox','--actor','op',*args],
                     env=admin.environment(root),capture_output=True,text=True)
    if p.returncode:raise RuntimeError(p.stderr)
    return p.stdout
rows=[json.loads(x) for x in run(['export','--all']).splitlines() if x.strip()]
result=b.brief(rows,'pp',task)
assert result['checkpoint'] is not None and not any('Malformed checkpoint' in x for x in result['warnings']),result
payload=dict(schema_version=1,task=task,previous=result['checkpoint']['comment_id'],
 activity_cursor=result['activity_cursor'],source_commit='',branch='',intent='compatibility',
 acceptance='both kits read and write',summary='old writer',next_action='continue',open_items=[],resolved=[])
print(json.dumps(b.save_checkpoint(rows,'pp',task,payload,'op',run)))
'''


@unittest.skipIf(rb.endpoint is None,'real bd checkpoint test needs POSIX endpoint imports')
@unittest.skipIf(rb.BD is None or not ROLLBACK,'real bd/rollback checkout not configured (ORCHESTRA_BD_BIN and ORCHESTRA_CHECKPOINT_ROLLBACK_KIT)')
class RealBdCheckpointCompatibilityTests(unittest.TestCase):
    bd=rb.RealBdLabelAliasTests.bd
    export_rows=rb.RealBdLabelAliasTests.export_rows

    @classmethod
    def setUpClass(cls):
        rb.RealBdLabelAliasTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        rb.RealBdLabelAliasTests.tearDownClass.__func__(cls)

    def create(self):
        result=self.bd('create','Compatibility task','--assignee','op','--json')
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)['id']

    def tip_write(self,task):
        data=self.export_rows();result=b.brief(data,'pp',task)
        payload=dict(schema_version=1,task=task,previous=(result['checkpoint'] or {}).get('comment_id'),
                     activity_cursor=result['activity_cursor'],source_commit='',branch='',intent='compatibility',
                     acceptance='both kits read and write',summary='tip default writer',next_action='continue',open_items=[],resolved=[])
        def run(args):
            p=self.bd(*args);self.assertEqual(p.returncode,0,p.stderr);return p.stdout
        b.save_checkpoint(data,'pp',task,payload,'op',run)
        current,invalid=b.checkpoints(b.task_row(self.export_rows(),task))
        self.assertEqual(invalid,[])
        self.assertNotIn('provenance',current[0])

    def old_write(self,task,first=False):
        script=OLD_RUN
        if first:
            script=script.replace("assert result['checkpoint'] is not None and not any('Malformed checkpoint' in x for x in result['warnings']),result",
                                  "assert result['checkpoint'] is None,result")
            script=script.replace("previous=result['checkpoint']['comment_id']","previous=None")
        process=subprocess.run([sys.executable,'-c',script,str(self.root),str(self.project),task],
                               cwd=ROLLBACK,capture_output=True,text=True)
        self.assertEqual(process.returncode,0,process.stdout+process.stderr)
        read=b.brief(self.export_rows(),'pp',task)
        self.assertIsNotNone(read['checkpoint'])
        self.assertFalse(any('Malformed checkpoint' in x for x in read['warnings']))

    def test_tip_then_previous_kit_then_tip(self):
        task=self.create();self.tip_write(task);self.old_write(task);self.tip_write(task)

    def test_previous_kit_then_tip_then_previous_kit(self):
        task=self.create();self.old_write(task,first=True);self.tip_write(task);self.old_write(task)
