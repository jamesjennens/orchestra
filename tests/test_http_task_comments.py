"""Task comment HTTP contracts; disposable backend, not native acceptance proof."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_http_agents import AgentHarness
from http_authority import CAP_TASKS, required_capability
from http_service import EndpointBackend


class CommentTests(AgentHarness):
    def fixture(self):
        token = self.admin_token()
        pid = self.create_project(token, 'comment fixture')
        made = self.request('POST', '/v1/projects/%s/tasks' % pid,
                            {'title': 'Comment fixture'}, token=token)
        self.assertEqual(201, made.status, made.data)
        return token, pid, made.data['id']

    def post(self, token, pid, tid, body, key='comment-key-0001'):
        return self.request('POST', '/v1/projects/%s/tasks/%s/comments' % (pid, tid),
                            body, token=token, key=key)

    def test_literal_bytes_actor_identity_and_exact_replay(self):
        token, pid, tid = self.fixture()
        for index, text in enumerate(['--help', '@attachment:0', '\ufeffordinary\r\n- item', '<img src=x onerror=alert(1)>']):
            key = 'literal-comment-%04d' % index
            first = self.post(token, pid, tid, {'text': text}, key)
            again = self.post(token, pid, tid, {'text': text}, key)
            self.assertEqual((201, 201), (first.status, again.status), first.data)
            self.assertEqual(text, first.data['comment']['text'])
            self.assertEqual(self.admin_user['id'], first.data['comment']['author'])
            self.assertTrue(first.data['comment']['id'])
            self.assertTrue(first.data['comment']['created_at'])
            self.assertEqual(first.data, again.data)
        self.assertEqual(4, len(self.backend.state['tasks'][tid]['comments']))
        audit = self.service.state['audit']
        self.assertFalse(any('onerror' in str(e) for e in audit))

    def test_closed_shape_refuses_impersonation_before_effect(self):
        token, pid, tid = self.fixture()
        for field in ('author', 'actor', 'project', 'task_id', 'path', 'capability'):
            result = self.post(token, pid, tid, dict(text='hello', **{field:'forged'}))
            self.assertEqual(422, result.status, result.data)
        self.assertFalse(self.backend.state['tasks'][tid].get('comments'))
        self.assertEqual(201, self.post(token, pid, tid, {'text':'corrected'}).status)

    def test_reserved_envelopes_and_variants_refused(self):
        token, pid, tid = self.fixture()
        kinds = ('task-assignment', 'contribution-review', 'task-checkpoint', 'lifecycle',
                 'lifecycle-event-correction', 'requirement-revision', 'requirement-acceptance',
                 'requirement-owner-acceptance', 'requirement-owner-state', 'requirement-owner-state-reason',
                 'task-handoff', 'task-handoff-complete', 'plan-registration', 'record-void',
                 'integration-revert', 'review-recommendation', 'reference-entry', 'reference-acceptance',
                 'requirement-proposal', 'proposal-disposition', 'contribution-settings',
                 'capability-entry', 'capability-acceptance', 'capability-verification', 'capability-alias',
                 'open-item', 'item-resolution', 'owner-answer', 'coordinator-decision', 'lane-state')
        for prefix in [kind + '-v' + version for kind in kinds for version in ('1', '9', '999999999')]:
            for leading, newline in [('', '\n'), ('\ufeff', '\r\n')]:
                result = self.post(token, pid, tid, {'text':leading+'Kind: '+prefix+newline+'{}'})
                self.assertEqual(422, result.status, result.data)
        self.assertFalse(self.backend.state['tasks'][tid].get('comments'))

    def test_bounds_and_unknown_outcome_retry(self):
        token, pid, tid = self.fixture()
        for text in ('', ' ', 'x\x00y', '\ud800', 'é' * 32769):
            self.assertEqual(422, self.post(token, pid, tid, {'text':text}).status)
        self.backend.fail_next('tasks.comment')
        body={'text':'saved before response was lost'}
        first=self.post(token,pid,tid,body)
        self.assertEqual(503,first.status,first.data)
        retry=self.post(token,pid,tid,body)
        self.assertEqual(201,retry.status,retry.data)
        self.assertEqual(1,len(self.backend.state['tasks'][tid]['comments']))

    def test_membership_revoked_replay_refuses_and_foreign_never_writes(self):
        token,pid,tid=self.fixture()
        uid=self.create_account(token,'contributor','password-contributor-1')
        self.assertEqual(200,self.request('PUT','/v1/projects/%s/members/%s'%(pid,uid),
          {'role':'contributor'},token=token).status)
        member=self.login('contributor','password-contributor-1')[0]
        body={'text':'member comment'}
        self.assertEqual(201,self.post(member,pid,tid,body).status)
        self.request('DELETE','/v1/projects/%s/members/%s'%(pid,uid),token=token)
        self.assertIn(self.post(member,pid,tid,body).status,(403,404))
        self.assertEqual(401,self.post(None,pid,tid,body).status)
        self.assertEqual(1,len(self.backend.state['tasks'][tid]['comments']))


class NativeMappingTests(unittest.TestCase):
    def test_literal_attachment_and_own_authority_table_entry(self):
        backend=object.__new__(EndpointBackend)
        text='--help\n@attachment:0'
        action,project,args,attachments=backend._command('tasks.comment',None,'demo',
             {'task_id':'demo-1','text':text},'operation')
        self.assertEqual(('bd','demo'),(action,project))
        self.assertEqual(['comments','add','demo-1','@attachment:0','--json'],args)
        self.assertEqual({'0':{'flag':'--file','text':text}},attachments)
        self.assertEqual(CAP_TASKS,required_capability({'action':action,'args':args}))

if __name__ == '__main__': unittest.main()
