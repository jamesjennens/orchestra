"""Current review feedback stays discoverable independently of completion facts."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import briefing
import render
import review_workflow as rw
import work
from requirements import canonical_bytes
from lifecycle import DIMENSIONS
from test_briefing import rows,checkpoint,append_checkpoint,save_cp,comment,TASK,PROJECT
from test_lifecycle import NativeStore


def contribute():
    return dict(schema_version=1,operation='contribute',operation_id='send-1',task=TASK,
                previous=None,repository='ssh://git/example',commit='a'*40,base_commit='b'*40,
                delivery=dict(kind='bundle',path='server:/deliveries/revised.bundle',sha256='c'*64),
                summary='Contribution delivered',supersedes=None)


def add(data,p,cid,author='reviewer'):
    data[0]['comments'].append(dict(id=cid,author=author,created_at='2026-09-16T00:00:00Z',
                                  text=rw.PREFIX+canonical_bytes(p).decode()))


def reviewed_rows():
    data=rows();data[0]['labels']=['review-ready']
    append_checkpoint(data,'cp',checkpoint(data,summary='Ready for review',next_action='Wait for reviewer'))
    add(data,contribute(),'delivery','alice/session')
    add(data,dict(schema_version=1,operation='request-changes',operation_id='review-1',task=TASK,
        previous='delivery',contribution='delivery',items=[dict(id='fix',text='Fix the boundary case')]),'feedback')
    return data


class WorkQueueTests(unittest.TestCase):
    def test_approval_updates_next_action_even_when_checkpoint_is_older(self):
        data=rows()
        append_checkpoint(data,'cp',checkpoint(data,next_action='Wait for reviewer'))
        add(data,contribute(),'delivery','alice/session')
        add(data,dict(schema_version=1,operation='approve',operation_id='approve-1',task=TASK,previous='delivery',contribution='delivery',summary='Accepted'), 'approval')
        result=briefing.brief(data,PROJECT,TASK)
        self.assertIn('Authorized integrator',result['next_action'])
        self.assertEqual(result['lifecycle']['integrated']['value'],'unknown')
    def test_feedback_overrides_old_checkpoint_next_action_and_label(self):
        data=reviewed_rows();result=briefing.brief(data,PROJECT,TASK)
        self.assertEqual(result['review']['review_state'],'changes-requested')
        self.assertEqual(result['review']['pending_requests'][0]['text'],'Fix the boundary case')
        self.assertIn('outstanding review',result['next_action'])
        self.assertTrue(result['checkpoint']['newer_activity'])
        item=work.queue(data,'alice/session',['--mine'])['items'][0]
        self.assertEqual(item['review_state'],'changes-requested')
        self.assertEqual(item['pending_review_items'],1)
        self.assertEqual(set(item['lifecycle']),set(DIMENSIONS))
        self.assertTrue(all(value=='unknown' for value in item['lifecycle'].values()))
    def test_closed_task_with_review_feedback_is_still_discoverable(self):
        data=reviewed_rows();data[0]['status']='closed'
        result=work.queue(data,'alice/session',['--mine','--state','changes-requested'])
        self.assertEqual(result['total'],1)
        self.assertEqual(work.queue(data,'different-session',['--mine'])['total'],0)
    def test_queue_prioritizes_revision_requests_and_pages(self):
        data=reviewed_rows()
        other=copy.deepcopy(data[0]);other.update(id='aaa-task',comments=[],labels=[])
        data.append(other)
        first=work.queue(data,'alice/session',['--mine','--limit','1'])
        second=work.queue(data,'alice/session',['--mine','--limit','1','--offset',str(first['next_offset'])])
        self.assertEqual(first['items'][0]['task'],TASK)
        self.assertEqual(second['items'][0]['task'],'aaa-task')
        self.assertIsNone(second['next_offset'])
    def test_no_checkpoint_is_neutral_and_preserves_description_and_acceptance(self):
        result=briefing.brief(rows(),PROJECT,TASK)
        self.assertIn('No checkpoint yet',result['current_position'])
        self.assertNotIn('legacy',json.dumps(result).lower())
        self.assertEqual(result['intent']['text'],'Intent')
        self.assertEqual(result['acceptance']['text'],'Acceptance')
        self.assertIsNone(result['unresolved']['total'])
    def test_invalid_review_history_remains_discoverable_as_error(self):
        data=rows();data[0]['comments'].append(dict(id='bad',text=rw.PREFIX+'{}'))
        result=work.queue(data,'alice/session',['--mine'])['items'][0]
        self.assertEqual(result['review_state'],'error')
        self.assertIn('Malformed',result['error'])
    def test_render_surfaces_current_review_without_claiming_deployment(self):
        with tempfile.TemporaryDirectory() as temp:
            render.render(reviewed_rows(),temp)
            current=(Path(temp)/'CURRENT.md').read_text(encoding='utf-8')
            self.assertIn('changes-requested',current)
            self.assertIn('Pending feedback',current)
            self.assertNotIn('deployed: passed',current)
    def test_old_integration_evidence_does_not_hide_new_approved_contribution(self):
        store=NativeStore().seed('integrated')
        store.rows[0].update(status='closed',comments=[],assignee='alice/session')
        add(store.rows,contribute(),'delivery','alice/session')
        add(store.rows,dict(schema_version=1,operation='approve',operation_id='approve-1',task=TASK,
            previous='delivery',contribution='delivery',summary='Reviewed new revision'),'approval')
        result=work.queue(store.rows,'alice/session',['--mine'])
        self.assertEqual(result['total'],1)
        item=result['items'][0]
        self.assertEqual(item['review_state'],'awaiting-integration')
        self.assertFalse(item['lifecycle_matches_contribution'])
        self.assertEqual(item['lifecycle']['integrated'],'passed')
        self.assertEqual(item['lifecycle_scope']['source_commit'],'source-a')


class NewerActivityQueueTests(unittest.TestCase):
    """kittrial-5bb.1: resume/work must flag directions newer than the checkpoint."""

    def directed(self):
        data=rows()
        save_cp(data,'cp1',next_action='Nothing pending')
        for n in range(3):
            data[0]['comments'].append(comment(f'dir{n}',f'Direction {n}','2026-09-16T00:00:00Z',
                                               author='coordinator/session'))
        data[0]['comments'].append(comment('own','Own note','2026-09-16T01:00:00Z'))
        return data

    def test_queue_flags_newer_activity_by_others_for_the_owner(self):
        item=work.queue(self.directed(),'alice/session',['--mine'])['items'][0]
        self.assertEqual(item['newer_activity_by_others'],3)
        self.assertEqual(item['newer_activity_own'],1)

    def test_queue_flag_clears_once_checkpoint_incorporates_the_activity(self):
        data=self.directed()
        save_cp(data,'cp2',next_action='Process the three directions')
        item=work.queue(data,'alice/session',['--mine'])['items'][0]
        self.assertEqual(item['newer_activity_by_others'],0)
        self.assertEqual(item['newer_activity_own'],0)

    def test_queue_without_checkpoint_or_with_malformed_history_is_neutral(self):
        item=work.queue(rows(),'alice/session',['--mine'])['items'][0]
        self.assertIsNone(item['newer_activity_by_others'])
        broken=rows()
        p=checkpoint(broken)
        append_checkpoint(broken,'cp1',p)
        append_checkpoint(broken,'cp2',dict(p,summary='Competing branch'))
        item=work.queue(broken,'alice/session',['--mine'])['items'][0]
        self.assertIsNone(item['newer_activity_by_others'])


if __name__=='__main__':unittest.main()
