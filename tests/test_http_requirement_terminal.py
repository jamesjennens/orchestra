"""Ordinary HTTP routes through the shipped owner adapter and authority journal."""
import copy
import json
import shutil
from pathlib import Path
import requirement_governance as governance
from unittest import mock

import requirement_http as http
import requirement_owner_records as owner
import requirement_records as records
from http_authority import journal_path
from test_http_review_fixes import EndpointCase
import test_http_requirements_owner as owner_fixture


class TerminalHttpTests(EndpointCase):
    make_backend = owner_fixture.OwnerHttpTests.make_backend
    create_requirement = owner_fixture.OwnerHttpTests.create_requirement
    expected = staticmethod(owner_fixture.OwnerHttpTests.expected)

    def setUp(self):
        super().setUp()
        self.addCleanup(self._stop_server)
        self.owner_token, self.project = self.setup_project()
        self.account = self.service.authenticate(self.owner_token).user_id
        self.path = self.canonical_root / 'projects' / self.project
        governance.initialize(self.path, self.project, self.account, 'creation-alpha')
        self.base = '/v1/projects/' + self.project

    def accepted(self, key='first'):
        draft=self.create_requirement('create-'+key)
        result=self.request('POST',self.base+'/requirements/'+draft['id']+'/accept',self.expected(draft),
                            token=self.owner_token,key='accept-'+key)
        self.assertEqual(result.status,200,result.data); return result.data

    def terminal_body(self,item,**extra):
        return dict(self.expected(item),expected_state_sha256=None,reason='No longer needed.',**extra)

    def transition(self,item,action='withdraw',key='terminal',token=None,body=None):
        return self.request('POST',self.base+'/requirements/'+item['id']+'/'+action,
                            self.terminal_body(item) if body is None else body,
                            token=self.owner_token if token is None else token,key=key)

    def test_routes_and_member_read_separate_status_from_acceptance_and_history(self):
        first=self.accepted(); second=self.accepted('second')
        reference={key:second[key] for key in ('id','revision','sha256')}
        done=self.transition(first,'supersede',body=self.terminal_body(first,successor=reference))
        self.assertEqual(done.status,200,done.data); self.assertEqual(done.data['requirement_state'],'superseded')
        admin=self.admin_token(); viewer_id=self.create_account(admin,'viewer','viewer-password-1')
        token=self.login('viewer','viewer-password-1')[0]
        self.assertEqual(self.request('PUT',self.base+'/members/'+viewer_id,{'role':'viewer'},token=admin).status,200)
        view=self.request('GET',self.base+'/requirements/'+first['id'],token=token)
        self.assertEqual(view.status,200,view.data); self.assertEqual(view.data['state']['superseded_by'],reference)
        self.assertEqual(view.data['accepted']['sha256'],first['sha256']); self.assertEqual(len(view.data['history']),2)
        brd=self.request('GET',self.base+'/brd',token=token)
        self.assertNotIn(first['id'],brd.data['active_ids']); self.assertIn(second['id'],brd.data['active_ids'])
        self.assertEqual(self.request('GET',self.base+'/requirements/'+second['id'],token=token).data['supersedes'][0]['id'],first['id'])

    def test_closed_fields_state_content_cas_and_draft_refuse_before_effect(self):
        item=self.accepted(); initial=copy.deepcopy(self.backend.native.rows)
        before=len(self.backend.native.writes())
        for index,changes in enumerate(({'account_id':self.account},{'at':'tomorrow'},{'governance':{}},
            {'expected_revision':1},{'expected_sha256':'0'*64},{'expected_state_sha256':'0'*64},{'reason':''},
            {'reason':'x'*1001},{'reason':'nul\0text'},{'successor':{}})):
            bad=self.transition(item,key='terminal-bad-'+str(index),body=dict(self.terminal_body(item),**changes))
            self.assertIn(bad.status,(409,422),bad.data)
        missing=self.terminal_body(item); missing.pop('expected_state_sha256')
        self.assertEqual(self.transition(item,key='terminal-missing',body=missing).status,422)
        self.assertEqual(len(self.backend.native.writes()),before)
        self.assertEqual(self.backend.native.rows,initial)
        draft=self.create_requirement('create-draft')
        self.assertEqual(self.transition(draft,key='draft-terminal').status,422)
        self.assertEqual(records.existing_revisions(self.backend.native.row(item['id']))[2]['sha256'],item['sha256'])
        self.assertEqual(self.backend.native.rows[:len(initial)],initial)
        # The deliberate draft creation is the only native change in this group.
        self.assertFalse(any(c['text'].startswith(owner.STATE_PREFIX) for r in self.backend.native.rows for c in r['comments']))

    def test_human_owner_boundary_including_superuser_credentials_agents_and_removed_owner(self):
        item=self.accepted(); admin=self.admin_token()
        self.service.state['memberships'][self.project].pop(self.service.authenticate(admin).user_id)
        self.service.store.save()
        member_id=self.create_account(admin,'member','member-password-1'); member=self.login('member','member-password-1')[0]
        self.assertEqual(self.request('PUT',self.base+'/members/'+member_id,{'role':'contributor'},token=admin).status,200)
        credential=self.issue_credential(self.owner_token,self.project,label='worker',scopes=['read','tasks'])
        agent=self.request('POST','/v1/agents',{'name':'Terminal helper','working_directory':'/tmp/synthetic-worker',
                                            'projects':[self.project]},token=self.owner_token)
        self.assertEqual(agent.status,201,agent.data); before=len(self.backend.native.writes())
        for index,token in enumerate((admin,member,credential['secret'],agent.data['credential']['secret'])):
            for action in ('withdraw','supersede'):
                body=self.terminal_body(item)
                if action=='supersede': body['successor']={key:item[key] for key in ('id','revision','sha256')}
                self.assertEqual(self.transition(item,action,key='denied-'+action+str(index),token=token,body=body).status,403)
        self.service.state['users'][self.account]['superuser']=True
        self.service.state['memberships'][self.project][self.account]='contributor'; self.service.store.save()
        self.assertEqual(self.transition(item,key='removed-owner').status,403)
        self.assertEqual(len(self.backend.native.writes()),before)

    def test_authority_is_rechecked_after_preflight_and_on_completed_replay(self):
        item=self.accepted(); before=len(self.backend.native.writes())
        def revoke():
            self.service.state['users'][self.account]['superuser']=True
            self.service.state['memberships'][self.project][self.account]='contributor'; self.service.store.save()
        self.backend.before_owner=revoke
        denied=self.transition(item,key='late-revoke')
        self.assertEqual(denied.status,422,denied.data); self.assertEqual(len(self.backend.native.writes()),before)
        self.service.state['memberships'][self.project][self.account]='owner'; self.service.store.save()
        done=self.transition(item,key='completed-replay'); self.assertEqual(done.status,200,done.data)
        revoke(); before=len(self.backend.native.writes())
        self.assertEqual(self.transition(item,key='completed-replay').status,403)
        self.assertEqual(len(self.backend.native.writes()),before)

    def test_cookie_csrf_and_governed_mode_are_checked_on_new_writes_and_replays(self):
        item=self.accepted(); token,csrf,_=self.login('alex','alex-password-1')
        route=self.base+'/requirements/'+item['id']+'/withdraw'; before=len(self.backend.native.writes())
        denied=self.request('POST',route,self.terminal_body(item),cookie='orchestra_session='+token,key='terminal-csrf')
        self.assertEqual(denied.status,403,denied.data); self.assertEqual(len(self.backend.native.writes()),before)
        done=self.request('POST',route,self.terminal_body(item),cookie='orchestra_session='+token,csrf=csrf,key='terminal-csrf')
        self.assertEqual(done.status,200,done.data)
        mode=self.request('GET',self.base+'/requirements/governance',token=token).data
        self.assertEqual(self.request('PUT',self.base+'/requirements/governance',dict(self.expected(mode),mode='governed'),
                                     token=token,key='terminal-governed').status,200)
        before=len(self.backend.native.writes())
        self.assertEqual(self.transition(item,key='terminal-csrf',token=token).status,422)
        self.assertEqual(self.transition(item,key='fresh-governed',token=token).status,422)
        self.assertEqual(len(self.backend.native.writes()),before)
        self.assertEqual(self.request('GET',self.base+'/requirements/'+item['id'],token=token).data['accepted']['sha256'],item['sha256'])

    def test_interrupted_reason_exact_recovery_and_conflicting_request_keep_one_pair(self):
        item=self.accepted(); self.backend.native.fail_comment_prefix=owner.REASON_PREFIX
        failed=self.transition(item,key='lost-reason'); self.assertEqual(failed.status,503,failed.data)
        detail=self.request('GET',self.base+'/requirements/'+item['id'],token=self.owner_token)
        self.assertEqual(detail.data['requirement_state'],'unknown')
        before=len(self.backend.native.writes()); self.backend.native.fail_comment_prefix=None
        conflict=self.transition(item,key='lost-reason',body=dict(self.terminal_body(item),reason='Different reason'))
        self.assertEqual(conflict.status,409,conflict.data); self.assertEqual(len(self.backend.native.writes()),before)
        # A new human login preserves the account's request identity.
        token=self.login('alex','alex-password-1')[0]
        recovered=self.transition(item,key='lost-reason',token=token); self.assertEqual(recovered.status,200,recovered.data)
        row=self.backend.native.row(item['id'])
        self.assertEqual(sum(c['text'].startswith(owner.STATE_PREFIX) for c in row['comments']),1)
        self.assertEqual(sum(c['text'].startswith(owner.REASON_PREFIX) for c in row['comments']),1)
        before=len(self.backend.native.writes()); journal=journal_path(self.path).read_bytes()
        replay=self.transition(item,key='lost-reason',token=token)
        self.assertEqual(replay.data,recovered.data); self.assertEqual(len(self.backend.native.writes()),before)
        self.assertEqual(journal_path(self.path).read_bytes(),journal)

    def test_browser_irreversible_confirmation_exact_target_and_history_use_real_routes(self):
        from test_http_web import run_node_module
        node=shutil.which('node')
        if not node: self.skipTest('Node.js is required for page behavior checks')
        first=self.accepted(); second=self.accepted('second')
        admin=self.admin_token(); viewer_id=self.create_account(admin,'viewer','viewer-password-1')
        token=self.login('viewer','viewer-password-1')[0]
        self.assertEqual(self.request('PUT',self.base+'/members/'+viewer_id,{'role':'viewer'},token=admin).status,200)
        root=Path(__file__).resolve().parents[1]
        script=(root/'tests/web_requirement_terminal.mjs').read_text(encoding='utf-8').replace(
            "'./web_dom_shim.mjs'", repr((root/'tests/web_dom_shim.mjs').as_uri()))
        output=run_node_module(self,node,script,'terminal-browser',
            (root/'web/js/api.js').as_uri(),(root/'web/js/views/owner_requirements.js').as_uri(),
            'http://127.0.0.1:'+str(self.port),self.project,self.owner_token,token,first['id'],second['id'])
        self.assertEqual(output.returncode,0,output.stderr)
        self.assertTrue(all(json.loads(output.stdout).values()))
