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
import requirement_terminal as terminal
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

    def pending_transition(self,item):
        view=self.request('GET',self.base+'/requirements/'+item['id'],token=self.owner_token)
        self.assertEqual(view.status,200,view.data)
        return view.data['pending_transition']

    def recover_transition(self,item,saved,action='finish',token=None,key='finish-transition'):
        body=dict(original_operation_id=saved['operation_id'],expected_receipt_sha256=saved['expected_receipt_sha256'])
        if action=='abandon':body['reason']='No state was written.'
        return self.request('POST',self.base+'/requirements/'+item['id']+'/transition/'+action,body,
                            token=token or self.owner_token,key=key)

    def after_writer(self,item):
        pending=self.pending_transition(item)
        saved=terminal.read_receipt(terminal.receipt_path(self.path,pending['operation_id']))
        clock=mock.patch('requirement_terminal.time.time',return_value=saved['write_window']['abandon_after']+1)
        clock.start();self.addCleanup(clock.stop)

    def test_second_owner_after_departure_finishes_from_reloaded_receipt_and_audits_once(self):
        first=self.accepted(); self.backend.native.fail_comment_prefix=owner.REASON_PREFIX
        failed=self.transition(first); self.assertEqual(failed.status,503,failed.data)
        saved=self.pending_transition(first); self.assertTrue(saved['can_finish']); self.assertFalse(saved['can_abandon'])
        self.backend.native.fail_comment_prefix=None
        admin=self.admin_token(); second=self.create_account(admin,'recovery-owner','recovery-owner-password')
        token=self.login('recovery-owner','recovery-owner-password')[0]
        self.assertEqual(self.request('PUT',self.base+'/members/'+second,{'role':'owner'},token=admin).status,200)
        self.assertEqual(self.request('PUT',self.base+'/members/'+self.account,{'role':'contributor'},token=admin).status,200)
        denied=self.recover_transition(first,saved,token=self.owner_token);self.assertEqual(denied.status,403,denied.data)
        finished=self.recover_transition(first,saved,token=token);self.assertEqual(finished.status,200,finished.data)
        self.assertEqual(finished.data['state'],saved['state']);self.assertEqual(finished.data['reason'],saved['reason'])
        self.assertEqual(finished.data['completion']['actor'],second)
        self.assertTrue(finished.data['completion']['at'])
        writes=len(self.backend.native.writes())
        for key in ('finish-transition','fresh-owner-finish'):
            replay=self.recover_transition(first,saved,token=token,key=key);self.assertEqual(replay.status,200,replay.data)
            self.assertEqual(replay.data['state'],saved['state'])
        self.assertEqual(len(self.backend.native.writes()),writes)
        events=[e for e in self.service.state['audit'] if e['action']=='requirements.finish' and e['outcome']=='committed']
        self.assertEqual(len(events),1,events)
        self.assertEqual(events[0]['requirement_id'],first['id'])
        self.assertEqual(events[0]['original_operation_id'],saved['operation_id'])
        self.assertEqual(events[0]['recovery_operation_id'],finished.data['completion']['operation_id'])
        row=self.backend.native.row(first['id'])
        self.assertEqual([c['author'] for c in row['comments'] if c['text'].startswith((owner.STATE_PREFIX,owner.REASON_PREFIX))],
                         [self.account,self.account])
        shown=self.request('GET',self.base+'/requirements/'+first['id'],token=token)
        self.assertEqual(shown.data['state_author_name'],'alex')
        self.assertEqual(shown.data['completion_author_name'],'recovery-owner')
        self.service.state['users'].pop(self.account);self.service.store.save()
        shown=self.request('GET',self.base+'/requirements/'+first['id'],token=token)
        self.assertEqual(shown.status,200,shown.data)
        self.assertEqual(shown.data['state_author_name'],'Former project owner')
        self.assertNotEqual(shown.data['state_author_name'],self.account)

    def test_abandon_before_state_and_refusal_after_state_keep_new_intents_usable(self):
        item=self.accepted();self.backend.native.fail_comment_prefix=owner.STATE_PREFIX
        failed=self.transition(item);self.assertEqual(failed.status,503,failed.data)
        pending=self.pending_transition(item);self.assertFalse(pending['can_abandon'])
        denied=self.recover_transition(item,pending,'abandon',key='early-abandon')
        self.assertEqual(denied.status,422,denied.data);self.assertIn('retry abandonment after',str(denied.data))
        self.after_writer(item)
        saved=self.pending_transition(item);self.assertTrue(saved['can_abandon'])
        released=self.recover_transition(item,saved,'abandon');self.assertEqual(released.status,200,released.data)
        self.assertEqual(released.data['disposition'],'released')
        events=[e for e in self.service.state['audit'] if e['action']=='requirements.abandon' and e['outcome']=='committed']
        self.assertEqual(len(events),1,events)
        self.assertEqual(events[0]['requirement_id'],item['id'])
        self.assertEqual(events[0]['original_operation_id'],saved['operation_id'])
        self.assertEqual(events[0]['recovery_operation_id'],released.data['recovery_operation_id'])
        self.assertTrue(events[0]['recovery_operation_id'])
        self.backend.native.fail_comment_prefix=None
        new=self.transition(item,key='after-abandon');self.assertEqual(new.status,200,new.data)
        other=self.accepted('second');self.backend.native.fail_comment_prefix=owner.REASON_PREFIX
        self.assertEqual(self.transition(other,key='partial-second').status,503)
        saved=self.pending_transition(other);self.assertFalse(saved['can_abandon'])
        refused=self.recover_transition(other,saved,'abandon');self.assertEqual(refused.status,422,refused.data)
        self.assertIn('already written',str(refused.data));self.backend.native.fail_comment_prefix=None
        self.assertEqual(self.recover_transition(other,saved).status,200)

    def test_terminal_audit_exact_replays_are_one_and_duplicate_fields_are_refused(self):
        item=self.accepted(); fields=self.terminal_body(item)
        text=json.dumps(fields)[:-1]+',"reason":"different reason"}'
        before=len(self.backend.native.writes())
        duplicate=self.transition(item,body=text,key='duplicate-reason');self.assertEqual(duplicate.status,422,duplicate.data)
        self.assertEqual(len(self.backend.native.writes()),before)
        for _ in range(3):self.assertEqual(self.transition(item,key='one-terminal-audit').status,200)
        events=[e for e in self.service.state['audit'] if e['action']=='requirements.withdraw' and e['outcome']=='committed']
        self.assertEqual(len(events),1,events)

    def test_finish_body_cannot_replace_saved_evidence(self):
        item=self.accepted();self.backend.native.fail_comment_prefix=owner.STATE_PREFIX
        self.assertEqual(self.transition(item).status,503)
        saved=self.pending_transition(item); before=len(self.backend.native.writes())
        body=dict(original_operation_id=saved['operation_id'],expected_receipt_sha256=saved['expected_receipt_sha256'],reason='Forged')
        result=self.request('POST',self.base+'/requirements/'+item['id']+'/transition/finish',body,token=self.owner_token,key='forged-finish')
        self.assertEqual(result.status,422,result.data);self.assertEqual(len(self.backend.native.writes()),before)

    def test_recovery_route_and_adapter_refuse_nonowner_superuser_governed_and_stale_hash(self):
        from http_authority import AuthorityConfig, NativeRunner, authority_request, run_guarded, CAP_PROJECT_ADMIN
        item=self.accepted();self.backend.native.fail_comment_prefix=owner.STATE_PREFIX
        self.assertEqual(self.transition(item).status,503)
        self.backend.native.fail_comment_prefix=None
        self.after_writer(item)
        pending=self.pending_transition(item);before=len(self.backend.native.writes())
        admin=self.admin_token();outsider=self.create_account(admin,'outsider','outsider-password-1')
        outsider_token=self.login('outsider','outsider-password-1')[0]
        self.assertEqual(self.request('PUT',self.base+'/members/'+outsider,{'role':'contributor'},token=admin).status,200)
        self.service.state['memberships'][self.project].pop(self.service.authenticate(admin).user_id,None)
        self.service.store.save()
        def adapter(token,action,body,key):
            principal=self.service.authenticate(token)
            request=dict(project=self.project,actor=principal.user_id,action='owner-requirements',
                args=[action,item['id']],attachments={'payload':{'flag':'--file','text':json.dumps(body)}},
                operation_id=key,authority=authority_request(principal,self.project,CAP_PROJECT_ADMIN,now=self.service._expiry_now()),
                route='requirements.'+action)
            def guarded(root,built,journal,effect,**options):return run_guarded(built,journal,effect,**options)
            return http.web_action(Path(self.canonical_root),self.path,self.project,request,
                AuthorityConfig(str(self.service.store.path)),NativeRunner(self.backend.native),guarded)
        for action in ('finish','abandon'):
            fields=dict(original_operation_id=pending['operation_id'],expected_receipt_sha256=pending['expected_receipt_sha256'])
            if action=='abandon':fields['reason']='Synthetic absence decision'
            for index,token in enumerate((outsider_token,admin)):
                self.assertEqual(self.recover_transition(item,pending,action,token=token,key='route-denied-'+action+str(index)).status,403)
                with self.assertRaisesRegex(ValueError,'signed-in project owner'):
                    adapter(token,action,fields,'adapter-denied-'+action+str(index))
            stale=dict(fields,expected_receipt_sha256='0'*64)
            denied=self.request('POST',self.base+'/requirements/'+item['id']+'/transition/'+action,stale,
                                token=self.owner_token,key='stale-'+action)
            self.assertEqual(denied.status,422,denied.data)
            with self.assertRaisesRegex(ValueError,'changed'):
                adapter(self.owner_token,action,stale,'adapter-stale-'+action)
        mode=self.request('GET',self.base+'/requirements/governance',token=self.owner_token).data
        self.assertEqual(self.request('PUT',self.base+'/requirements/governance',dict(self.expected(mode),mode='governed'),
                                     token=self.owner_token,key='recovery-governed').status,200)
        for action in ('finish','abandon'):
            self.assertEqual(self.recover_transition(item,pending,action,key='governed-'+action).status,422)
            fields=dict(original_operation_id=pending['operation_id'],expected_receipt_sha256=pending['expected_receipt_sha256'])
            if action=='abandon':fields['reason']='Synthetic absence decision'
            with self.assertRaisesRegex(ValueError,'governed requirements'):
                adapter(self.owner_token,action,fields,'adapter-governed-'+action)
        self.assertEqual(len(self.backend.native.writes()),before)
        shown=self.pending_transition(item);self.assertFalse(shown['can_finish']);self.assertFalse(shown['can_abandon'])
        self.assertIn('Enable simple editing',shown['message'])

    def test_operator_completion_reason_is_durable_and_visible_in_service_audit(self):
        item=self.accepted();self.backend.native.fail_comment_prefix=owner.REASON_PREFIX
        self.assertEqual(self.transition(item).status,503)
        self.backend.native.fail_comment_prefix=None;pending=self.pending_transition(item)
        completed=records.reconcile(self.path,pending['operation_id'],'configured-operator',
            'Finished after checking the exact native state.','complete',self.backend.native)
        self.assertEqual(completed['completion']['actor'],'configured-operator')
        self.assertEqual(completed['completion']['reason'],'Finished after checking the exact native state.')
        self.assertEqual(completed['completion']['via'],'operator')
        self.assertFalse(any(event['action']=='requirements.operator.finish' for event in self.service.state['audit']))
        for _ in range(2):
            audit=self.request('GET',self.base+'/audit',token=self.owner_token)
            self.assertEqual(audit.status,200,audit.data)
            events=[event for event in audit.data['items'] if event['action']=='requirements.operator.finish']
            self.assertEqual(len(events),1,events)
            self.assertEqual(events[0]['requirement_id'],item['id'])
            self.assertEqual(events[0]['original_operation_id'],pending['operation_id'])
            self.assertEqual(events[0]['reason'],completed['completion']['reason'])

    def test_operator_abandon_alias_keeps_reason_and_recovery_audit_identity(self):
        item=self.accepted();self.backend.native.fail_comment_prefix=owner.STATE_PREFIX
        self.assertEqual(self.transition(item).status,503)
        self.backend.native.fail_comment_prefix=None;self.after_writer(item)
        pending=self.pending_transition(item)
        released=records.reconcile(self.path,pending['operation_id'],'configured-operator',
            'No native state after the writer deadline.','failed',self.backend.native)
        self.assertEqual(released['disposition'],'released')
        saved=terminal.read_receipt(terminal.receipt_path(self.path,pending['operation_id']))
        self.assertEqual(saved['terminal_disposition']['reason'],'No native state after the writer deadline.')
        self.assertEqual(saved['terminal_disposition']['operation_id'],released['recovery_operation_id'])
        for _ in range(2):
            audit=self.request('GET',self.base+'/audit',token=self.owner_token)
            self.assertEqual(audit.status,200,audit.data)
            events=[event for event in audit.data['items'] if event['action']=='requirements.operator.abandon']
            self.assertEqual(len(events),1,events)
            self.assertEqual(events[0]['requirement_id'],item['id'])
            self.assertEqual(events[0]['original_operation_id'],pending['operation_id'])
            self.assertEqual(events[0]['recovery_operation_id'],released['recovery_operation_id'])
            self.assertEqual(events[0]['reason'],saved['terminal_disposition']['reason'])

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
        self.assertNotIn(first['id'],brd.data['nonterminal_ids']); self.assertIn(second['id'],brd.data['nonterminal_ids'])
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

    def test_completed_replay_rechecks_owner_removed_after_mode_preflight(self):
        item=self.accepted(); done=self.transition(item,key='late-cached-replay')
        self.assertEqual(done.status,200,done.data); before=len(self.backend.native.writes())
        read=self.backend.requirements_read
        def remove_after_read(project,args):
            result=read(project,args)
            if args==['governance']:
                self.service.state['users'][self.account]['superuser']=True
                self.service.state['memberships'][self.project][self.account]='contributor'
                self.service.store.save()
            return result
        self.backend.requirements_read=remove_after_read
        replay=self.transition(item,key='late-cached-replay')
        self.assertEqual(replay.status,422,replay.data)
        self.assertEqual(len(self.backend.native.writes()),before)

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

    def test_browser_reloaded_second_owner_finishes_and_abandons_saved_transitions(self):
        from test_http_web import run_node_module
        node=shutil.which('node')
        if not node:self.skipTest('Node.js is required for transition recovery page checks')
        finish=self.accepted('page-finish');abandon=self.accepted('page-abandon');edit=self.accepted('page-edit')
        replacement=self.accepted('page-replacement')
        self.backend.native.fail_comment_prefix=owner.REASON_PREFIX
        self.assertEqual(self.transition(finish,'supersede',key='page-finish',body=self.terminal_body(finish,
            successor={k:replacement[k] for k in ('id','revision','sha256')})).status,503)
        self.backend.native.fail_comment_prefix=owner.STATE_PREFIX
        self.assertEqual(self.transition(abandon,key='page-abandon').status,503)
        self.after_writer(abandon)
        self.backend.native.fail_comment_prefix=None
        pending=self.request('PATCH',self.base+'/requirements/'+edit['id'],dict(self.expected(edit),description='Pending edit'),
            token=self.owner_token,key='page-pending-edit');self.assertEqual(pending.status,200,pending.data)
        admin=self.admin_token();second=self.create_account(admin,'page-owner','page-owner-password')
        token=self.login('page-owner','page-owner-password')[0]
        self.assertEqual(self.request('PUT',self.base+'/members/'+second,{'role':'owner'},token=admin).status,200)
        self.assertEqual(self.request('PUT',self.base+'/members/'+self.account,{'role':'contributor'},token=admin).status,200)
        root=Path(__file__).resolve().parents[1]
        script=(root/'tests/web_requirement_transition_recovery.mjs').read_text().replace(
            "'./web_dom_shim.mjs'",repr((root/'tests/web_dom_shim.mjs').as_uri()))
        output=run_node_module(self,node,script,'terminal-recovery-browser',(root/'web/js/api.js').as_uri(),
            (root/'web/js/views/owner_requirements.js').as_uri(),'http://127.0.0.1:'+str(self.port),self.project,
            token,finish['id'],abandon['id'],edit['id'])
        self.assertEqual(output.returncode,0,output.stderr);self.assertTrue(all(json.loads(output.stdout).values()))
