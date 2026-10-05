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
if result['checkpoint'] is not None:
 assert not result['checkpoint']['newer_activity'],result
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
        self.assertFalse(b.brief(self.export_rows(),'pp',task)['checkpoint']['newer_activity'])

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
        self.assertFalse(read['checkpoint']['newer_activity'])

    def test_tip_then_previous_kit_then_tip(self):
        task=self.create();self.tip_write(task);self.old_write(task);self.tip_write(task)

    def test_previous_kit_then_tip_then_previous_kit(self):
        task=self.create();self.old_write(task,first=True);self.tip_write(task);self.old_write(task)

    def test_reassigned_owner_boundary_and_unassigned_author_on_native_records(self):
        task=self.create()
        def native(*args,actor='op'):
            # bd's global --actor is the audit actor; comment authors are set
            # separately, as the endpoint's native runner does in production.
            if args[:2]==('comments','add'):
                args=(*args,'--author',actor)
            result=self.bd(*args,actor=actor)
            self.assertEqual(result.returncode,0,result.stderr)
            return result.stdout
        def enabled_write(actor):
            data=self.export_rows();read=b.brief(data,'pp',task)
            payload=dict(schema_version=1,task=task,previous=(read['checkpoint'] or {}).get('comment_id'),
                         activity_cursor=read['activity_cursor'],source_commit='',branch='',intent='owner boundary',
                         acceptance='retain later instructions',summary='checkpoint',next_action='continue',
                         open_items=[],resolved=[])
            b.save_checkpoint(data,'pp',task,payload,actor,
                              lambda args:native(*args,actor=actor),provenance_writes=True)
        native('comments','add',task,'Owner progress before own checkpoint.')
        enabled_write('op')
        later=json.loads(native('comments','add',task,'Owner instruction after own checkpoint.','--json'))['id']
        enabled_write('operator')  # Observed assignee op, but not op's own checkpoint.
        native('update',task,'--assignee','bob','--status','in_progress','--json')
        enabled_write('bob')
        page=b.direction_page(self.export_rows(),'pp',task)
        stamps=[(c['id'],c['author'],c['created_at'])
                for c in b.task_row(self.export_rows(),task)['comments']]
        self.assertEqual([x['id'] for x in page['items']],[task+'-c'+str(later)],stamps)
        self.assertEqual(b.brief(self.export_rows(),'pp',task)['directions']['total'],1)
        native('update',task,'--assignee=','--json')
        native('comments','add',task,'Newest checkpoint author own progress.',actor='bob')
        reviewer=json.loads(native('comments','add',task,'Reviewer direction.','--json',actor='reviewer'))['id']
        page=b.direction_page(self.export_rows(),'pp',task)
        self.assertEqual({x['id'] for x in page['items']},
                         {task+'-c'+str(later),task+'-c'+str(reviewer)})
