"""Caller-scoped pointer display and lossless HTTP checkpoint templates."""
import unittest
import json
import shutil
from pathlib import Path
from test_http_web import TeamHarness


class PointerHttpTests(TeamHarness):
    def test_project_scoped_targets_and_raw_carry_remain_distinct(self):
        self.claim(self.carl)
        foreign=self.create_task(self.otto,self.other,'PRIVATE title').data['id']
        note=self.request('POST',self.path('/tasks/'+self.task+'/comments'),{'text':'Readable comment'},token=self.carl)
        self.assertEqual(note.status,201,note.data)
        comment=note.data.get('comment',note.data)
        cid=comment['id']
        items=[dict(id='source',kind='question',text='Q',source='task:'+self.task+' comment:'+cid),
               dict(id='hidden',kind='question',text='Hidden',source='task:'+foreign),
               dict(id='absent',kind='question',text='Absent',source='task:task_absent'),
               dict(id='file',kind='question',text='File',source='file:'+foreign+'/abc room:secret#1')]
        answer=self.request('POST',self.path('/tasks/'+self.task+'/checkpoints'),
                            dict(previous=None,summary='Index',open_items=items),token=self.carl)
        self.assertEqual(answer.status,201,answer.data)
        result=self.request('GET',self.path('/tasks/'+self.task+'/brief'),token=self.vera)
        self.assertEqual(result.status,200,result.data)
        data=result.data;shown=data['checkpoint']['pointer_items']
        self.assertEqual(data['checkpoint']['open_items'],items)
        self.assertEqual([p['state'] for p in shown['source']['pointers']],['live','live'])
        hidden=dict(shown['hidden']['pointers'][0]);absent=dict(shown['absent']['pointers'][0])
        hidden.pop('pointer');absent.pop('pointer');self.assertEqual(hidden,absent)
        self.assertEqual(hidden['state'],'unavailable');self.assertNotIn('label',hidden)
        self.assertEqual([p['state'] for p in shown['file']['pointers']],['unchecked','unchecked'])
        self.assertEqual(data['carry_open_items'],items)
        self.assertNotIn('PRIVATE title',str(data))
        body=data['checkpoint_template']['body']
        self.assertEqual(body['open_items'],items)
        # The actual template publishes through the existing checkpoint route.
        body['summary']='Carried unchanged'
        body['open_items']=data['checkpoint']['open_items']
        again=self.request('POST',self.path('/tasks/'+self.task+'/checkpoints'),body,token=self.carl)
        self.assertEqual(again.status,201,again.data)


class PointerWebTests(unittest.TestCase):
    def test_labels_and_follow_are_text_nodes_not_markup_or_links(self):
        node=shutil.which('node')
        if not node:self.skipTest('node is not installed')
        import test_http_web
        root=Path(__file__).resolve().parents[1]
        code='''await import(process.argv[1]);
const view=await import(process.argv[2]);
const el=view.contextPointers({pointers:[{pointer:'task:example',state:'live',trust:'data',
 label:{text:'<img src=x onerror=evil()>\\\\u202e'},follow:'brief example'},
 {pointer:'room:hidden#1',state:'unchecked',trust:'data'}],pointers_omitted:0});
console.log(JSON.stringify({text:el.textContent,images:el.querySelectorAll('img').length,links:el.querySelectorAll('a').length}));'''
        done=test_http_web.run_node_module(self,node,code,(root/'tests/web_dom_shim.mjs').as_uri(),
                                          (root/'web/js/views/task.js').as_uri())
        self.assertEqual(done.returncode,0,done.stderr)
        result=json.loads(done.stdout.strip().splitlines()[-1])
        self.assertIn('<img src=x onerror=evil()>',result['text'])
        self.assertIn('unchecked',result['text']);self.assertIn('untrusted data',result['text'])
        self.assertEqual(result['images'],0);self.assertEqual(result['links'],0)


if __name__=='__main__':unittest.main()
