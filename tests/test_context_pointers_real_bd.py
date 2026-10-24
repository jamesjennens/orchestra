"""Pointers in native brief, HTTP mapping and actual carry-forward writes."""
import unittest
import test_coordinator_grant_real as native


class NativePointerTests(native.RealStackTests):
    def test_native_brief_pointer_resolution_and_template_roundtrip(self):
        task=self.new('Native pointer target')
        base=self.tasks+'/'+task
        self.assertEqual(self.request('POST',base+'/claim',{},token=self.agent).status,200)
        first=self.request('GET',base+'/brief',token=self.agent).data
        item=dict(id='point',kind='question',text='Inspect native source',source='task:'+task+' commit:abc1234 room:secret#1')
        items=[item]+[dict(id='point-'+str(n),kind='question',text='More source',source='task:'+task) for n in range(11)]
        cp=dict(schema_version=1,previous=None,activity_cursor=first['activity_cursor'],source_commit='',branch='',
                intent='Pointer brief',acceptance='Loaded data only',summary='Native index',next_action='Continue',
                open_items=items,resolved=[])
        answer=self.request('POST',base+'/checkpoints',cp,token=self.agent)
        self.assertEqual(answer.status,201,answer.data)
        cli=self.backend._run('brief','pp',self.backend.actor_namespace+'/read',[task,'--json'])
        self.assertEqual([p['state'] for p in cli['unresolved']['items'][0]['pointers']],['live','unchecked','unchecked'])
        web=self.request('GET',base+'/brief',token=self.agent)
        self.assertEqual(web.status,200,web.data)
        self.assertEqual(web.data['checkpoint']['open_items'][0]['pointers'],cli['unresolved']['items'][0]['pointers'])
        self.assertEqual(web.data['carry_open_items'],items)
        self.assertEqual(len(web.data['checkpoint']['pointer_items']),2)
        self.assertTrue(all(i['pointers'][0]['state']=='live' for i in web.data['checkpoint']['pointer_items']))
        body=web.data['checkpoint_template']['body'];body['summary']='Native carry';body['next_action']='Continue native work'
        again=self.request('POST',base+'/checkpoints',body,token=self.agent)
        self.assertEqual(again.status,201,again.data)
        latest=self.request('GET',base+'/brief',token=self.agent).data
        self.assertEqual(latest['carry_open_items'],items)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite([NativePointerTests('test_native_brief_pointer_resolution_and_template_roundtrip')])


if __name__=='__main__':unittest.main()
