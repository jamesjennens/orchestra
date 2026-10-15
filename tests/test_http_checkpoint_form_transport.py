"""Checkpoint form template/mapping contracts. Native qualification is separate."""
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from http_service import ApiHandler, EndpointBackend
from http_client import Client
from test_http_agents import AgentHarness


class TransportTests(unittest.TestCase):
    def test_complete_template_retains_each_source_and_binding(self):
        items=[dict(id='item-'+str(i),kind='dependency',text='remaining',source='native:'+str(i)) for i in range(35)]
        brief=dict(checkpoint=dict(id='cp-1',intent='existing intent',acceptance='existing acceptance',source_commit='a'*40,branch='feature'),activity_cursor={'native':'exact'})
        t=ApiHandler._checkpoint_template(brief,'/task',items)
        self.assertEqual(items,t['body']['open_items'])
        self.assertEqual(35,t['carried_open_items'])
        for field in ('intent','acceptance','source_commit','branch'):
            self.assertEqual(brief['checkpoint'][field],t['body'][field])
        self.assertEqual('cp-1',t['body']['previous'])
        self.assertEqual(brief['activity_cursor'],t['body']['activity_cursor'])
        self.assertEqual([],t['body']['resolved'])

    def test_incomplete_items_are_explicitly_unavailable(self):
        t=ApiHandler._checkpoint_template({'checkpoint':{'id':'cp-1'}},'/task',None)
        self.assertIsNone(t['carried_open_items'])
        self.assertIn('could not all be read',t['note'])

    def test_lane_declaration_survives_native_attachment(self):
        backend=object.__new__(EndpointBackend)
        lane=dict(schema_version=1,task='demo-1',state='waiting-on-review',waiting_on='reviewer')
        body=dict(task_id='demo-1',schema_version=1,previous='cp-1',activity_cursor={'native':'exact'},open_items=[],resolved=[],lane_state=lane)
        action,pid,args,attachments=backend._command('checkpoints.add',None,'demo',body,'op')
        self.assertEqual(('checkpoint','demo'),(action,pid))
        carried=json.loads(attachments['0']['text'])
        self.assertEqual(lane,carried['lane_state'])
        self.assertEqual('demo-1',carried['task'])
        self.assertEqual(body['activity_cursor'],carried['activity_cursor'])


class CopiedClientCommentTests(AgentHarness):
    def test_copied_client_posts_literal_comment_over_real_http(self):
        client=Client('http://127.0.0.1:%d'%self.port)
        client.login('root-admin','correct-horse-battery-staple')
        project=client.create_project('client comment fixture')
        task=client.create_task(project['id'],'client fixture')
        first=client.add_comment(project['id'],task['id'],'--help\r\n@attachment:0',key='client-comment-0001')
        again=client.add_comment(project['id'],task['id'],'--help\r\n@attachment:0',key='client-comment-0001')
        self.assertEqual(first,again)
        self.assertEqual('--help\r\n@attachment:0',first['comment']['text'])
        self.assertEqual(1,len(self.backend.state['tasks'][task['id']]['comments']))

if __name__=='__main__':unittest.main()
