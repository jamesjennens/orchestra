"""Current review feedback stays discoverable independently of completion facts."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
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
    def test_only_another_exact_actor_wakes_a_checkpoint(self):
        data=rows();data[0]['assignee']='http/agent/a'
        append_checkpoint(data,'cp',checkpoint(data))
        data[0]['comments'].append(dict(id='own',author='http/agent/a',created_at='2026-10-04T10:00:00Z',text='Own progress.'))
        row=work.queue(data,'http/agent/a',[])['items'][0]
        self.assertFalse(row['newer_activity'])
        data[0]['comments'].append(dict(id='other',author='http/agent/b',created_at='2026-10-04T10:00:00Z',text='Other worker.'))
        self.assertTrue(work.queue(data,'http/agent/a',[])['items'][0]['newer_activity'])

    def test_resolved_checkpoint_has_zero_open_items(self):
        data=rows();item=dict(id='question',kind='blocker',text='Need a key.',source='build')
        append_checkpoint(data,'cp-one',checkpoint(data,open_items=[item]))
        append_checkpoint(data,'cp-two',checkpoint(data,open_items=[],
            resolved=[dict(id='question',reason='Key supplied.',evidence='owner comment')]))
        row=work.queue(data,'alice/session',[])['items'][0]
        self.assertEqual(row['open_items'],0)
        self.assertEqual(row['checkpoint_at'],data[0]['comments'][-1]['created_at'])

    def test_pending_request_ids_are_capped_but_item_count_is_exact(self):
        data=rows()
        # The legacy writer itself caps unresolved items at20. Pin this separate
        # queue boundary defensively for a larger projection, without claiming
        # that21 legacy requests form a valid persisted history.
        projected=dict(review_state='changes-requested',contribution={},
                       pending_requests=[dict(request='request-'+str(n)) for n in range(21)])
        with patch.object(work,'workflow',return_value=projected):
            row=work.queue(data,'alice/session',[])['items'][0]
        self.assertEqual(row['pending_review_items'],21)
        self.assertEqual(len(row['pending_change_requests']),20)
        self.assertEqual(len(set(row['pending_change_requests'])),20)

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
    def test_render_shows_a_closed_withdrawn_contribution_that_still_has_an_item(self):
        """kittrial-5bb.110 item 9: the rendered view carries the withdrawn state."""
        data=reviewed_rows()
        add(data,dict(schema_version=1,operation='withdraw',operation_id='withdraw-1',task=TASK,
                      previous='feedback',contribution='delivery',reason='Re-scoped'),
            'withdraw','alice/session')
        data[0]['status']='closed'
        with tempfile.TemporaryDirectory() as temp:
            render.render(data,temp)
            current=(Path(temp)/'CURRENT.md').read_text(encoding='utf-8')
            # The closed withdrawn task with a blocking item still open is listed with
            # its true state, exactly as `work` reports it.
            self.assertIn('withdrawn',current)
            self.assertIn('| 1 |',current)                      # pending_review_items
            page=(Path(temp)/'jobs'/(TASK+'.md')).read_text(encoding='utf-8')
            self.assertIn('withdraw-1',page)
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

    def test_queue_has_explicit_journal_input_and_surfaces_malformed_records(self):
        with tempfile.TemporaryDirectory() as temp:
            journal=Path(temp);(journal/'bad.json').write_text('{bad',encoding='utf-8')
            result=work.queue(rows(),'alice/session',['--mine'],journal)
        self.assertEqual(result['items'][0]['review_state'],'error')
        self.assertIn('Malformed handoff journal',result['items'][0]['error'])
        self.assertFalse(hasattr(work.queue,'_request_dir'))

    def test_filtered_and_empty_queue_reads_expose_journal_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            journal=Path(temp);(journal/'bad.json').write_text('{bad',encoding='utf-8')
            filtered=work.queue(rows(),'different-session',['--mine'],journal)
            empty=work.queue([],'alice/session',['--mine'],journal)
        self.assertEqual(filtered['total'],0)
        self.assertEqual(empty['total'],0)
        self.assertEqual(len(filtered['journal_errors']),1)
        self.assertEqual(len(empty['journal_errors']),1)

    def test_queue_bounds_nested_requests_and_exposes_continuation(self):
        with tempfile.TemporaryDirectory() as temp:
            journal=Path(temp)
            for number in range(3):
                request={'schema_version':1,'operation':'request',
                         'request_id':'00000000-0000-0000-0000-0000000000%02d' % (10+number),
                         'task':TASK,'from_actor':'alice/session','to_actor':'bob/session',
                         'reason':'Take over','requester':'bob/session','status':'pending',
                         'created_at':'2026-09-16T00:00:00+00:00'}
                from requirements import content_hash
                request['digest']=content_hash({key:request[key] for key in
                                                ('schema_version','operation','request_id','task','from_actor','to_actor','reason')})
                (journal/(content_hash({'request_id':request['request_id']})+'.json')).write_text(
                    json.dumps(request),encoding='utf-8')
            first=work.queue(rows(),'alice/session',['--mine','--handoff-limit','1'],journal)
            item=first['items'][0]
            self.assertEqual(item['pending_handoff_total'],3)
            self.assertEqual(len(item['pending_handoff_requests']),1)
            self.assertEqual(item['pending_handoff_next_offset'],1)


class WorkQueueRevertScopeTests(unittest.TestCase):
    """kittrial-5bb.52 P3: the queue's reverts/scopes inputs are PER TASK.

    They used to apply to every row, and each row re-scanned every task's native
    comments (``reverts_for`` was O(rows) per task, computed twice per row).
    """

    def test_reverts_and_scopes_must_be_per_task_mappings(self):
        data=reviewed_rows()
        with self.assertRaisesRegex(ValueError,'reverts must be a mapping'):
            work.queue(data,'alice/session',['--mine'],reverts=[])
        with self.assertRaisesRegex(ValueError,'scopes must be a mapping'):
            work.queue(data,'alice/session',['--mine'],scopes=[])

    def test_an_explicit_reverts_mapping_is_used_for_its_own_task_only(self):
        data=reviewed_rows()
        other=copy.deepcopy(data[0]);other.update(id='aaa-task',comments=[],labels=[])
        data.append(other)
        from review_state import reverts_for
        scopes={TASK:[]}
        # The supplied maps are consulted per task id; a task absent from the map
        # gets an empty list rather than another task's records.
        page=work.queue(data,'alice/session',['--mine'],reverts={TASK:[]},scopes=scopes)
        self.assertEqual(sorted(item['task'] for item in page['items']),['aaa-task',TASK])
        for item in page['items']:
            self.assertEqual(item['integration_disagreements'],[])
        self.assertEqual(reverts_for(data,TASK),[])

    def test_reverts_for_every_task_are_resolved_once_per_page(self):
        """One reader call for the whole page, not one per row and task."""
        data=reviewed_rows()
        other=copy.deepcopy(data[0]);other.update(id='aaa-task',comments=[],labels=[])
        data.append(other)
        import review_state
        calls=[]
        real=review_state.reverts_by_task
        def counting(rows,operators=None,journal=None):
            calls.append(list(rows))
            return real(rows,operators,journal)
        with patch.object(review_state,'reverts_by_task',side_effect=counting):
            page=work.queue(data,'alice/session',['--mine'])
        self.assertEqual(len(calls),1)
        self.assertEqual(len(calls[0]),len(data))
        self.assertEqual(sorted(item['task'] for item in page['items']),['aaa-task',TASK])


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
