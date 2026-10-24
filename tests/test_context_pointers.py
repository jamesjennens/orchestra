"""Slice 1: grammar, bounded loaded-data resolution and lossless checkpoints."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import briefing as b
from requirements import canonical_bytes
from test_briefing import rows,checkpoint,append_checkpoint,comment,contribution,add_review,TASK,PROJECT
from test_reference_records import ReferenceCase,OPERATOR,acceptance


class GrammarTests(unittest.TestCase):
    def test_all_kinds_and_original_tokens_in_order(self):
        kinds='task comment ref cap item file room commit doc archive'.split()
        text=' prose\n'+'\t'.join(k+':A_0./#:-' for k in kinds)
        parsed=b.parse_context_pointers(text)
        self.assertEqual([p['kind'] for p in parsed],kinds)
        self.assertEqual([p['pointer'] for p in parsed],[k+':A_0./#:-' for k in kinds])

    def test_near_misses_are_prose_and_limits_are_token_limits(self):
        for text in ('task:', 'Task:abc', 'other:abc', '(task:abc)', 'task:a,b',
                     'task:abc?', 'task:é', 'task:'+'a'*201, 'see task: the review'):
            with self.subTest(text=text):self.assertEqual(b.parse_context_pointers(text),[])
        self.assertEqual(len(b.parse_context_pointers('task:'+'a'*200)),1)
        self.assertEqual(len(b.parse_context_pointers(('task:a '*1000))),1000)


class ResolutionTests(unittest.TestCase):
    def resolve(self,token,rows_=None,**kwargs):
        return b.ContextPointers(rows_ if rows_ is not None else rows(),PROJECT,**kwargs).resolve(b.parse_context_pointers(token)[0])

    def test_loaded_task_and_comment_escape_then_clip_with_data_trust(self):
        data=rows();data[0]['title']='\x1b\x85\u202e\u2028<script>'+('z'*140)
        task=self.resolve('task:'+TASK,data)
        self.assertEqual(task['label'],b.clip(b.plain_text(data[0]['title']),120))
        self.assertGreater(task['label']['omitted_chars'],0)
        data[0]['comments'][0]['text']='\x1b\u2029evil'
        answer=self.resolve('comment:first',data)
        self.assertEqual(answer['label'],b.clip(b.plain_text(TASK+' / alice/session: \x1b\u2029evil'),120))
        self.assertEqual(answer['trust'],'data');self.assertEqual(task['trust'],'data')

    def test_hidden_existing_and_absent_are_identical_except_requested_token(self):
        data=rows();data[0]['title']='PRIVATE'
        for token in ('task:'+TASK,'comment:first'):
            hidden=self.resolve(token,data,can_read=lambda row:False)
            absent=self.resolve(token,[])
            self.assertEqual(hidden,absent)
            self.assertEqual(hidden['state'],'unavailable')
            self.assertNotIn('label',hidden);self.assertNotIn('follow',hidden)

    def test_every_unloaded_kind_is_uniformly_unchecked(self):
        for kind in ('item','file','room','commit','doc','archive'):
            for value in ('exists','absent','PRIVATE/abc#L1-L4'):
                answer=self.resolve(kind+':'+value)
                self.assertEqual(answer,dict(pointer=kind+':'+value,kind=kind,state='unchecked',trust='data'))

    def test_unloaded_resolution_performs_no_file_or_process_io(self):
        resolver=b.ContextPointers(rows(),PROJECT)
        with patch('builtins.open',side_effect=AssertionError('file I/O')), \
             patch.object(Path,'open',side_effect=AssertionError('path I/O')), \
             patch('subprocess.run',side_effect=AssertionError('process I/O')):
            for kind in ('item','file','room','commit','doc','archive'):
                for target in ('exists','missing'):
                    pointer=b.parse_context_pointers(kind+':'+target)[0]
                    self.assertEqual(resolver.resolve(pointer)['state'],'unchecked')

    def test_explicit_foreign_project_rows_are_not_indexed(self):
        data=rows();data[0]['project_id']='other'
        self.assertEqual(self.resolve('task:'+TASK,data),self.resolve('task:'+TASK,[]))
        self.assertEqual(self.resolve('comment:first',data),self.resolve('comment:first',[]))

    def test_deleted_comment_is_unavailable_without_changing_checkpoint_validity(self):
        data=rows();p=checkpoint(data,open_items=[dict(id='x',kind='question',text='Question',source='comment:deleted')])
        original=copy.deepcopy(p);append_checkpoint(data,'cp',p)
        result=b.brief(data,PROJECT,TASK)
        self.assertEqual(result['unresolved']['items'][0]['pointers'][0]['state'],'unavailable')
        self.assertEqual(b.checkpoint_problems(original,TASK),[])
        self.assertEqual(p,original)

    def test_superseded_contribution_is_not_read_as_current_evidence(self):
        data=rows();first=contribution('a'*40);add_review(data,first,'old')
        second=dict(contribution('d'*40),operation_id='second',previous='old',supersedes='old')
        add_review(data,second,'new')
        self.assertEqual(self.resolve('comment:old',data)['state'],'superseded')
        self.assertEqual(self.resolve('comment:new',data)['state'],'live')

    def test_record_catalog_uses_loaded_projection_and_retirement(self):
        import reference_records,capability_records
        for kind,module in (('ref',reference_records),('cap',capability_records)):
            for state in ('accepted','superseded'):
                view=dict(key='known',state=state,record=dict(name='\x1b\u202eTitle',acceptance_state=state))
                with patch.object(module,'catalog',return_value=([view],[])) as catalog:
                    answer=self.resolve(kind+':known')
                catalog.assert_called_once()
                self.assertEqual(answer['state'],'superseded' if state=='superseded' else 'live')
                self.assertEqual(answer['label']['text'],'\\u001b\\u202eTitle')
                self.assertEqual(answer['trust'],'data')

    def test_10_per_item_50_total_and_display_copies_do_not_change_source(self):
        original=[dict(id=str(n),source=' '.join('task:'+TASK for _ in range(12))) for n in range(6)]
        saved=copy.deepcopy(original);resolver=b.ContextPointers(rows(),PROJECT)
        answer=resolver.items(original,'source')
        self.assertEqual([i['pointers_omitted'] for i in answer],[2]*5+[12])
        self.assertEqual(sum(p['state']=='live' for i in answer for p in i['pointers']),50)
        self.assertEqual(sum(p['state']=='unresolved' for i in answer for p in i['pointers']),22)
        self.assertEqual(original,saved)
        self.assertTrue(all(p['trust']=='data' for i in answer for p in i['pointers']))

    def test_open_and_resolved_evidence_share_budget_and_text_surface(self):
        data=rows()
        append_checkpoint(data,'first-cp',checkpoint(data,open_items=[dict(id='old',kind='question',text='Old',source='Prose')]))
        p=checkpoint(data,open_items=[dict(id='x',kind='question',text='Q',source='task:'+TASK)],
                               resolved=[dict(id='old',reason='Done',evidence='commit:abc1234')])
        append_checkpoint(data,'cp',p)
        answer=b.brief(data,PROJECT,TASK)
        self.assertEqual(answer['unresolved']['items'][0]['pointers'][0]['state'],'live')
        self.assertEqual(answer['resolved']['items'][0]['pointers'][0]['state'],'unchecked')
        rendered=b.format_brief(answer)
        self.assertIn('commit:abc1234 [unchecked; trust=data]',rendered)
        self.assertIn('Resolved old:',rendered)

    def test_web_composition_does_not_multiply_budgets_across_existing_pages(self):
        resolver=b.ContextPointers(rows(),PROJECT)
        raw=[dict(id=str(n),source=' '.join('task:'+TASK for _ in range(10))) for n in range(6)]
        first=resolver.items(raw[:2],'source')
        whole=b.ContextPointers(rows(),PROJECT).items(raw,'source')
        resolved=b.ContextPointers(rows(),PROJECT).items([dict(id='r',evidence='task:'+TASK)],'evidence')
        saved=copy.deepcopy((first,whole,resolved))
        groups=b.bound_context_pointer_display([first,whole[2:],resolved])
        pointers=[p for group in groups for item in group for p in item['pointers']]
        self.assertEqual(sum(p['state']=='live' for p in pointers),50)
        self.assertEqual(groups[-1][0]['pointers'][0]['state'],'unresolved')
        self.assertEqual((first,whole,resolved),saved)

    def test_execute_still_uses_one_export_and_no_per_task_comment_read(self):
        data=rows();append_checkpoint(data,'cp',checkpoint(data,open_items=[dict(id='x',kind='question',text='Q',source='task:'+TASK)]))
        calls=[]
        def run(args):
            calls.append(args)
            if args[0]=='export':return '\n'.join(json.dumps(row) for row in data)
            raise AssertionError('unexpected read '+repr(args))
        with tempfile.TemporaryDirectory() as directory:
            answer=json.loads(b.execute(Path(directory),Path(directory),PROJECT,'alice/session','brief',[TASK,'--json'],{},run))
        self.assertEqual(answer['unresolved']['items'][0]['pointers'][0]['state'],'live')
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][0],'export')

    def test_older_release_validates_carries_by_id_and_publishes(self):
        previous=os.environ.get('ORCHESTRA_POINTER_PREVIOUS_KIT')
        if not previous:self.skipTest('ORCHESTRA_POINTER_PREVIOUS_KIT not supplied; old writer compatibility not exercised')
        script='''import json,sys,briefing as b
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_briefing import rows,checkpoint,append_checkpoint,TASK,PROJECT
data=rows(); source='task:trial-task ref:host.rule archive:'+'a'*64+'#L1-L4'
p=checkpoint(data,open_items=[dict(id='x',kind='question',text='Q',source=source)])
assert b.checkpoint_problems(p,TASK)==[]
append_checkpoint(data,'cp',p)
nextp=checkpoint(data,open_items=p['open_items'])
assert b.transition(p,nextp) is None
writes=[]
b.save_checkpoint(data,PROJECT,TASK,nextp,'alice/session',lambda args:(writes.append(args) or json.dumps(dict(id='next'))))
assert writes and source in writes[0][3]
assert json.loads(writes[0][3].split('\\n',1)[1])['open_items'][0]['source']==source
print('old checkpoint writer OK')
'''
        done=subprocess.run([sys.executable,'-c',script],cwd=previous,text=True,capture_output=True)
        self.assertEqual(done.returncode,0,done.stdout+done.stderr)
        self.assertIn('old checkpoint writer OK',done.stdout)


class NativeShapedCatalogTests(ReferenceCase):
    def test_actual_draft_and_accepted_reference_rows_resolve_without_native_calls(self):
        self.propose()
        for accepted in (False,True):
            if accepted:self.accept(1,self.sha(1))
            before=len(self.native.calls)
            resolver=b.ContextPointers(self.native.rows,PROJECT,operators=[OPERATOR])
            result=resolver.resolve(b.parse_context_pointers('ref:calendar.trading')[0])
            self.assertEqual(result['state'],'live',result)
            self.assertEqual(result['label']['text'],'Trading calendar authority')
            self.assertEqual(len(self.native.calls),before)
            hidden=b.ContextPointers(self.native.rows,PROJECT,operators=[OPERATOR],can_read=lambda row:False)
            absent=b.ContextPointers([],PROJECT,operators=[OPERATOR])
            pointer=b.parse_context_pointers('ref:calendar.trading')[0]
            self.assertEqual(hidden.resolve(pointer),absent.resolve(pointer))

    def test_retired_reference_uses_its_real_projection(self):
        import reference_records as rr
        self.propose()
        self.propose(key='calendar.next',operation_id='next-ref')
        self.native.actor=OPERATOR
        rr.apply_native(dict(schema_version=1,operation_id='retire-ref',operation='retire',
                             key='calendar.trading',revision=1,record_sha256=self.sha(1),
                             successor='calendar.next',acceptance_state='superseded',acceptance=acceptance()),
                        OPERATOR,self.native,self.project,operator=True,operators=[OPERATOR],web_coordinator=True)
        result=b.ContextPointers(self.native.rows,PROJECT,operators=[OPERATOR]).resolve(
            b.parse_context_pointers('ref:calendar.trading')[0])
        self.assertEqual(result['state'],'superseded',result)


if __name__=='__main__':unittest.main()
