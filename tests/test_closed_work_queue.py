"""Closed approval is history, not an integration invitation (.210 F1)."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import briefing
import review_state
import work
from test_briefing import rows, PROJECT, TASK
from test_work_queue import add, contribute, reviewed_rows


class ClosedQueueTests(unittest.TestCase):
    def approved(self, closed=True):
        data=rows()
        add(data,contribute(),'delivery','alice/session')
        add(data,dict(schema_version=1,operation='approve',operation_id='accept',task=TASK,
                     previous='delivery',contribution='delivery',summary='Reviewed'),'approval')
        data[0]['status']='closed' if closed else 'in_progress'
        return data

    def test_closed_approval_leaves_all_work_selections_but_review_history_remains(self):
        data=self.approved();before=copy.deepcopy(data)
        for args in ([],['--mine'],['--owner','alice/session'],['--state','awaiting-integration'],
                     ['--mine','--limit','1','--offset','0']):
            with self.subTest(args=args):
                self.assertEqual(work.queue(data,'alice/session',args)['total'],0)
        read=briefing.brief(data,PROJECT,TASK)
        self.assertEqual(read['review']['review_state'],'awaiting-integration')
        self.assertEqual(read['review']['contribution']['comment_id'],'delivery')
        self.assertEqual(read['lifecycle']['integrated']['value'],'unknown')
        self.assertNotIn('Authorized integrator: integrate',read['next_action'])
        self.assertEqual(read['review']['next_actor'],'none')
        self.assertEqual(data,before)

    def test_open_approved_control_keeps_integration_invitation_and_paging(self):
        data=self.approved(False)
        row=work.queue(data,'alice/session',['--mine','--state','awaiting-integration'])['items'][0]
        self.assertEqual((row['whose_move'],row['next_actor']),('coordinator','coordinator'))
        self.assertIn('Authorized integrator: integrate',briefing.brief(data,PROJECT,TASK)['next_action'])
        closed=self.approved()[0];closed['id']='closed-task';closed['comments']=[]
        data.append(closed)
        answer=work.queue(data,'alice/session',['--limit','1'])
        self.assertEqual((answer['total'],answer['next_offset']),(1,None))

    def test_closed_feedback_and_malformed_history_still_surface(self):
        data=reviewed_rows();data[0]['status']='closed';before=copy.deepcopy(data)
        row=work.queue(data,'alice/session',['--mine'])['items'][0]
        self.assertEqual((row['review_state'],row['pending_review_items'],row['next_actor']),
                         ('changes-requested',1,'assignee'))
        self.assertEqual(data,before)
        bad=rows();bad[0]['status']='closed';bad[0]['comments'].append(dict(id='bad',text='Kind: contribution-review-v1\n{}'))
        error=work.queue(bad,'alice/session',['--mine'])['items'][0]
        self.assertEqual((error['review_state'],error['next_actor']),('error','operator'))

    def test_closed_withdrawal_keeps_only_unresolved_blocking_work(self):
        data=reviewed_rows();data[0]['status']='closed'
        payload=dict(schema_version=1,operation='withdraw',operation_id='final-withdraw',
                     task=TASK,previous='feedback',contribution='delivery',reason='Re-scoped')
        add(data,payload,'final','alice/session')
        row=work.queue(data,'alice/session',['--mine'])['items'][0]
        self.assertEqual((row['review_state'],row['pending_review_items'],row['next_actor']),
                         ('withdrawn',1,'assignee'))
        final=self.approved()
        payload.update(previous='approval',operation_id='final-no-items')
        add(final,payload,'final','alice/session')
        self.assertEqual(work.queue(final,'alice/session',['--mine'])['total'],0)
        self.assertEqual(work.workflow(final[0])['next_actor'],'unknown')
        self.assertEqual(work.move_decision('withdrawn',TASK,True,assignee='alice/session',closed=True)[2],'unknown')

    def test_closed_caller_never_gets_integration_sentence(self):
        for state in ('awaiting-integration','approved'):
            with self.subTest(state=state):
                move,text,role=work.move_decision(state,TASK,True,assignee='alice/session',closed=True)
                self.assertEqual((move,role),('unknown','none'))
                self.assertNotIn("next move is the coordinator",text)
                self.assertEqual(review_state.next_actor(state,'alice/session',closed=True),'none')


# Use the pinned native fixture in its own deployment; no team runtime or stub database.
import test_bd_label_aliases as native


class NativeClosedQueueTests(native.RealBdLabelAliasTests):
    test_create_label_spellings_refuse_every_reserved_value=None
    test_update_label_spellings_refuse_every_reserved_value=None
    test_ordinary_labels_still_reach_real_bd=None

    def test_native_closure_filters_approval_without_rewriting_comments(self):
        result=self.bd('create','Closed approval queue regression','--assignee','alice/session','--json')
        self.assertEqual(result.returncode,0,result.stderr);task=json.loads(result.stdout)['id']
        payload=contribute();payload['task']=task
        from review_workflow import PREFIX
        from requirements import canonical_bytes
        result=self.bd('comments','add',task,PREFIX+canonical_bytes(payload).decode(),'--author','alice/session')
        self.assertEqual(result.returncode,0,result.stderr)
        comments=json.loads(self.bd('comments',task,'--json').stdout);delivery=comments[-1]['id']
        approval=dict(schema_version=1,operation='approve',operation_id='native-approve-'+task,
                      task=task,previous=delivery,contribution=delivery,summary='Independent approval')
        result=self.bd('comments','add',task,PREFIX+canonical_bytes(approval).decode(),'--author','reviewer')
        self.assertEqual(result.returncode,0,result.stderr)
        before=json.loads(self.bd('comments',task,'--json').stdout)
        self.assertIn(task,[x['task'] for x in work.queue(self.export_rows(),'alice/session',['--mine'])['items']])
        result=self.bd('update',task,'--status','closed');self.assertEqual(result.returncode,0,result.stderr)
        data=self.export_rows()
        self.assertNotIn(task,[x['task'] for x in work.queue(data,'alice/session',['--mine'])['items']])
        read=briefing.brief(data,'pp',task)
        self.assertEqual(read['review']['review_state'],'awaiting-integration')
        self.assertEqual(read['review']['next_actor'],'none')
        self.assertEqual(json.loads(self.bd('comments',task,'--json').stdout),before)
