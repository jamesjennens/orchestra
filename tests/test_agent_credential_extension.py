"""Real HTTP extension and target-account checks over synthetic disposable state."""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import http_auth
from test_agent_credential_lifetime import AgentLifetimeHttp, DAY
from test_http_agents import AgentHarness


class ExtensionHttp(AgentLifetimeHttp):
    # Inherit foundation tests deliberately: separate count is recorded in evidence.
    def setup_target(self):
        admin = self.admin_token()
        uid = self.create_account(admin, 'credential-owner', 'synthetic-password-1')
        token = self.login('credential-owner', 'synthetic-password-1')[0]
        pid = self.create_project(admin, 'Extension project')
        self.service.state['memberships'][pid][uid] = 'contributor'
        self.store.save()
        aid, secret, made = self.agent_secret(token, projects=[pid], scopes=['read'])
        return admin, uid, token, pid, aid, secret, made['credential']['id']

    def path(self, aid, cid, pid=None):
        stem = '/v1/agents' if pid is None else '/v1/projects/%s/agents' % pid
        return '%s/%s/credentials/%s/extend' % (stem, aid, cid)

    def extend(self, token, aid, cid, pid=None, key=None, body=None):
        return self.request('POST', self.path(aid, cid, pid), {} if body is None else body,
                            token=token, key=key)

    def test_owner_extends_same_key_secretlessly_and_exact_retry_does_not_slide(self):
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        credential = self.service.state['credentials'][cid]
        before = copy.deepcopy(credential)
        before_agents = copy.deepcopy(self.service.state['agents'])
        self.tick[0] += DAY
        # Sign in again rather than reviving an expired session by changing its TTL.
        token = self.login('credential-owner', 'synthetic-password-1')[0]
        first = self.extend(token, aid, cid, key='same-key-extension')
        self.assertEqual(200, first.status, first.data)
        self.assertEqual(before['expires_at'] + DAY, credential['expires_at'])
        allowed_changes = {'expires_at', 'agent_raw_expires_at'}
        self.assertEqual({k:v for k,v in before.items() if k not in allowed_changes},
                         {k:v for k,v in credential.items() if k not in allowed_changes})
        self.assertEqual(before_agents, self.service.state['agents'])
        self.assertEqual(1, len(self.service.state['credentials']))
        after = copy.deepcopy(credential)
        self.tick[0] += 60
        retry = self.extend(token, aid, cid, key='same-key-extension')
        self.assertEqual(200, retry.status, retry.data)
        self.assertEqual(first.data, retry.data)
        self.assertEqual(after, credential)
        self.assertNotIn(secret, first.body.decode() + retry.body.decode() + json.dumps(self.service.state['audit']))
        self.assertEqual({'id', 'expires_at', 'working', 'revoked', 'expires_soon'},
                         set(first.data['credential']))
        self.assertFalse(first.data['secret_available'])
        self.assertEqual(1, sum(e['action']=='agents.credentials.extended' for e in self.service.state['audit']))
        self.assertEqual(200, self.request('GET', '/v1/sessions/current', token=secret).status)

    def test_shorter_setting_never_shortens_either_deadline(self):
        admin, _, token, pid, aid, secret, cid = self.setup_target()
        c = self.service.state['credentials'][cid]
        deadlines = (c['expires_at'], c['agent_raw_expires_at'])
        self.service.agent_credential_ttl = DAY
        self.assertEqual(200, self.extend(token, aid, cid).status)
        self.assertEqual(deadlines, (c['expires_at'], c['agent_raw_expires_at']))

    def test_project_owner_and_superuser_can_extend_but_cannot_receive_secret(self):
        admin, _, token, pid, aid, secret, cid = self.setup_target()
        for actor, project in ((admin,pid),(admin,None),(token,None)):
            with self.subTest(project=project):
                answer = self.extend(actor, aid, cid, project)
                self.assertEqual(200, answer.status, answer.data)
                self.assertNotIn(secret, answer.body.decode())
                self.assertFalse(answer.data['secret_available'])
        rows = self.request('GET', '/v1/projects/%s/agents'%pid, token=admin).data['items']
        self.assertEqual({'id','expires_at','working','revoked','expires_soon'}, set(rows[0]['credentials'][0]))
        for field in ('working_directory','scopes','token_hash','label','notes','projects'):
            self.assertNotIn(field, rows[0])

    def test_coordinator_contributor_viewer_and_foreign_account_have_no_extension_authority(self):
        admin, _, _, pid, aid, _, cid = self.setup_target()
        uid = self.create_account(admin, 'other-human', 'synthetic-password-2')
        token = self.login('other-human', 'synthetic-password-2')[0]
        before = copy.deepcopy(self.service.state['credentials'][cid])
        for role in ('coordinator','contributor','viewer',None):
            if role:self.service.state['memberships'][pid][uid]=role
            else:self.service.state['memberships'][pid].pop(uid,None)
            with self.subTest(role=role):
                answer=self.extend(token,aid,cid,pid)
                self.assertIn(answer.status,(403,404),answer.data)
                self.assertEqual(404,self.extend(token,aid,cid).status)
                self.assertEqual(before,self.service.state['credentials'][cid])

    def test_agent_and_worker_credentials_never_extend(self):
        admin, _, token, pid, aid, secret, cid = self.setup_target()
        response=self.request('POST','/v1/projects/%s/worker-credentials'%pid,
                              {'scopes':['read'],'actor':'synthetic-worker'},token=admin)
        self.assertEqual(201,response.status,response.data)
        worker=response.data['credential']['secret']
        for bearer in (secret,worker):
            for project in (None,pid):
                with self.subTest(project=project,kind='agent' if bearer==secret else 'worker'):
                    answer=self.extend(bearer,aid,cid,project)
                    self.assertEqual(403,answer.status,answer.data)

    def test_target_binding_and_unknown_fields_are_refused_without_effect(self):
        admin, _, token, pid, aid, _, cid=self.setup_target()
        other, _, made=self.agent_secret(token,name='Another synthetic agent',scopes=['read'])
        before=copy.deepcopy(self.service.state['credentials'])
        for path,body,status in ((self.path(other,cid),{},404),
                                 (self.path(aid,'cred_missing'),{},404),
                                 (self.path(aid,cid),{'scopes':['tasks']},422)):
            answer=self.request('POST',path,body,token=token)
            self.assertEqual(status,answer.status,answer.data)
            self.assertEqual(before,self.service.state['credentials'])
        self.assertEqual(200,self.extend(token,aid,cid,key='payload-conflict').status)
        answer=self.extend(token,aid,cid,key='payload-conflict',body={'expires_at':'tomorrow'})
        self.assertEqual(409,answer.status,answer.data)

    def test_expired_revoked_disabled_and_disabled_owner_never_revive_even_on_replay(self):
        for state in ('expired','revoked','disabled-agent','disabled-owner'):
            # Separate agent/key per subcase and fresh owner session after clock changes.
            admin=self.admin_token()
            aid,_,made=self.agent_secret(admin,name='Refuse '+state,scopes=['read'])
            cid=made['credential']['id'];c=self.service.state['credentials'][cid]
            key='refusal-'+state
            self.assertEqual(200,self.extend(admin,aid,cid,key=key).status)
            if state=='expired':c['expires_at']=self.tick[0]
            elif state=='revoked':c['revoked']=True
            elif state=='disabled-agent':self.service.state['agents'][aid]['enabled']=False
            else:
                # Use a distinct account: do not disable the authenticating admin.
                uid=self.create_account(admin,'disabled-target','synthetic-password-3')
                self.service.state['agents'][aid]['owner']=uid;c['user_id']=uid
                self.service.state['users'][uid]['disabled']=True
            before=copy.deepcopy(c)
            with self.subTest(state=state):
                self.assertEqual(409,self.extend(admin,aid,cid,key=key).status)
                self.assertEqual(409,self.extend(admin,aid,cid,key=key+'-fresh').status)
                self.assertEqual(before,c)

    def test_completed_project_retry_rechecks_role_grant_and_target_owner_membership(self):
        admin, uid, _, pid, aid, _, cid=self.setup_target()
        owner_uid=self.create_account(admin,'project-owner','synthetic-password-4')
        owner=self.login('project-owner','synthetic-password-4')[0]
        self.service.state['memberships'][pid][owner_uid]='owner'
        self.assertEqual(200,self.extend(owner,aid,cid,pid,key='live-replay').status)
        before=copy.deepcopy(self.service.state['credentials'][cid])
        self.service.state['memberships'][pid][owner_uid]='coordinator'
        self.assertEqual(403,self.extend(owner,aid,cid,pid,key='live-replay').status)
        self.service.state['memberships'][pid][owner_uid]='owner'
        self.service.state['agents'][aid]['projects']=[]
        self.assertEqual(404,self.extend(owner,aid,cid,pid,key='live-replay').status)
        self.service.state['agents'][aid]['projects']=[pid]
        self.service.state['memberships'][pid].pop(uid)
        self.assertEqual(404,self.extend(owner,aid,cid,pid,key='live-replay').status)
        self.assertEqual(before,self.service.state['credentials'][cid])

    def test_save_failure_rolls_back_extension_and_releases_reservation(self):
        _, _, token, _, aid, _, cid=self.setup_target()
        self.tick[0]+=60
        c=self.service.state['credentials'][cid];before=copy.deepcopy(c)
        # Login/authentication may save last-use; target the extension save only.
        original=self.store.save
        def failed():
            if any(e['action']=='agents.credentials.extended' for e in self.service.state['audit']):
                raise OSError('synthetic extension save failure')
            return original()
        with mock.patch.object(self.store,'save',side_effect=failed):
            reply=self.extend(token,aid,cid,key='save-fault')
        self.assertEqual(500,reply.status,reply.data)
        self.assertEqual(before,self.service.state['credentials'][cid])
        self.assertFalse(any(e['action']=='agents.credentials.extended' for e in self.service.state['audit']))
        self.assertEqual(200,self.extend(token,aid,cid,key='save-fault').status)

    def test_receipt_failure_keeps_durable_extension_and_refuses_unknown_retry(self):
        _, _, token, _, aid, _, cid=self.setup_target()
        self.tick[0]+=60
        previous=self.service.state['credentials'][cid]['expires_at']
        with mock.patch.object(self.service,'idempotency_commit',side_effect=OSError('synthetic receipt failure')):
            reply=self.extend(token,aid,cid,key='receipt-fault')
        self.assertEqual(500,reply.status,reply.data)
        stored=http_auth.Store(self.store.path).state['credentials'][cid]
        self.assertGreater(stored['expires_at'],previous)
        self.assertEqual(stored,self.service.state['credentials'][cid])
        self.assertEqual(409,self.extend(token,aid,cid,key='receipt-fault').status)

    def test_seven_day_warning_on_owner_project_and_whoami(self):
        admin, _, token, pid, aid, secret, cid=self.setup_target()
        c=self.service.state['credentials'][cid]
        for days,soon in ((8,False),(7,True),(1,True)):
            c['expires_at']=self.tick[0]+days*DAY;c['agent_raw_expires_at']=c['expires_at']
            with self.subTest(days=days):
                who=self.request('GET','/v1/sessions/current',token=secret)
                self.assertEqual(200,who.status,who.data)
                self.assertEqual(soon,who.data['credential_expiry']['expires_soon'])
                own=self.request('GET','/v1/agents/'+aid,token=token).data
                self.assertEqual(soon,own['credentials'][0]['expires_soon'])
                project=self.request('GET','/v1/projects/%s/agents/%s'%(pid,aid),token=admin).data['agent']
                self.assertEqual(soon,project['credentials'][0]['expires_soon'])

    def test_legacy_default_key_extension_removes_only_obsolete_raw_cap(self):
        admin=self.admin_token();self.service.agent_credential_ttl=30*DAY
        aid,secret,made=self.agent_secret(admin,scopes=['read']);cid=made['credential']['id']
        c=self.service.state['credentials'][cid]
        c.pop('agent_raw_expires_at');c.pop('agent_issued_raw')
        before=copy.deepcopy(c);self.service.agent_credential_ttl=90*DAY
        reply=self.extend(admin,aid,cid)
        self.assertEqual(200,reply.status,reply.data)
        self.assertNotIn('issued_raw',c)
        self.assertEqual(before['issued_raw'],c['agent_issued_raw'])
        for field in ('id','token_hash','user_id','actor','scopes','created_at'):
            self.assertEqual(before[field],c[field])
        self.tick[0]+=31*DAY
        self.assertEqual(200,self.request('GET','/v1/sessions/current',token=secret).status)


class TargetAccountHttp(AgentHarness):
    def fixture(self):
        admin=self.admin_token()
        uid=self.create_account(admin,'target-human','synthetic-password-5')
        token=self.login('target-human','synthetic-password-5')[0]
        return admin,uid,token

    def test_superuser_creates_for_actual_account_with_creator_disclosure_and_party(self):
        admin,uid,token=self.fixture()
        pid=self.create_project(admin,'Target grant')
        self.service.state['memberships'][pid][uid]='contributor'
        answer=self.create_agent(admin,owner=uid,projects=[pid],scopes=['read'])
        self.assertEqual(201,answer.status,answer.data)
        aid=answer.data['agent']['id'];cid=answer.data['credential']['id']
        self.assertEqual(uid,answer.data['agent']['owner'])
        self.assertEqual(self.admin_user['id'],answer.data['agent']['created_by'])
        self.assertTrue(answer.data['agent']['created_by_display_name'])
        self.assertEqual(uid,self.service.state['credentials'][cid]['user_id'])
        principal=self.service.authenticate(answer.data['credential']['secret'])
        self.assertEqual(uid,principal.user_id)
        self.assertFalse(principal.superuser)
        self.assertEqual(aid,principal.actor)
        own=self.request('GET','/v1/agents/'+aid,token=token)
        self.assertEqual(200,own.status,own.data)
        events=[e for e in self.service.state['audit'] if e['action']=='agents.created']
        self.assertEqual(uid,events[-1]['owner_id'])
        self.assertEqual(self.admin_user['id'],events[-1]['created_by'])
        self.assertNotIn(answer.data['credential']['secret'],json.dumps(events))

    def test_non_superuser_foreign_target_is_refused_before_owner_or_project_existence(self):
        admin,uid,token=self.fixture()
        for owner in (self.admin_user['id'],'user_missing'):
            answer=self.create_agent(token,owner=owner,projects=['missing'],unknown=True)
            self.assertEqual(403,answer.status,answer.data)
        self.assertFalse(self.service.state['agents'])

    def test_missing_disabled_or_malformed_target_refused_without_agent(self):
        admin,uid,_=self.fixture()
        self.service.state['users'][uid]['disabled']=True
        for owner,status in (('user_missing',404),(uid,409),(None,422),(True,422),({},422)):
            with self.subTest(owner=owner):
                answer=self.create_agent(admin,owner=owner)
                self.assertEqual(status,answer.status,answer.data)
        self.assertFalse(self.service.state['agents'])

    def test_target_owns_name_limit_and_membership_checks(self):
        admin,uid,token=self.fixture()
        self.assertEqual(201,self.create_agent(admin,name='Same name').status)
        self.assertEqual(201,self.create_agent(admin,owner=uid,name='Same name').status)
        self.assertEqual(409,self.create_agent(admin,owner=uid,name='Same name').status)
        pid=self.create_project(admin,'Not a target membership')
        self.assertEqual(404,self.create_agent(admin,owner=uid,name='Cannot grant',projects=[pid]).status)
        agent=next(a for a in self.service.state['agents'].values() if a['owner']==uid)
        for i in range(http_auth.AGENT_MAX_PER_OWNER-1):
            clone=dict(agent,id='agent_synthetic_%d'%i,name='Synthetic limit %d'%i)
            self.service.state['agents'][clone['id']]=clone
        self.assertEqual(409,self.create_agent(admin,owner=uid,name='Over target limit').status)
        self.assertEqual(201,self.create_agent(admin,name='Admin still has room').status)

    def test_creation_retry_never_returns_secret_and_rechecks_target(self):
        admin,uid,_=self.fixture()
        body={'name':'Created for account','owner':uid,'scopes':['read']}
        first=self.request('POST','/v1/agents',body,token=admin,key='target-create')
        self.assertEqual(201,first.status,first.data)
        retry=self.request('POST','/v1/agents',body,token=admin,key='target-create')
        self.assertEqual(200,retry.status,retry.data)
        self.assertNotIn(first.data['credential']['secret'],retry.body.decode())
        self.service.state['users'][uid]['disabled']=True
        self.assertEqual(409,self.request('POST','/v1/agents',body,token=admin,key='target-create').status)
        self.assertEqual(1,len(self.service.state['agents']))


class LifetimePagesHttp(AgentHarness):
    def setUp(self):
        from test_agent_credential_lifetime import Service90
        with mock.patch('test_http_agents.Service', Service90):
            super().setUp()
        self.tick=[self.store.now()]
        self.store.clock=lambda:self.tick[0]

    def test_agents_and_project_controls_against_actual_http_and_lost_response(self):
        import shutil
        from test_http_agents import ROOT
        from test_http_web import run_node_module
        node=shutil.which('node')
        if not node:self.skipTest('Node is not installed; lifetime page controls were not run')
        admin,uid,owner,pid,aid,secret,cid=ExtensionHttp.setup_target(self)
        c=self.service.state['credentials'][cid]
        c['expires_at']=self.tick[0]+7*DAY;c['agent_raw_expires_at']=c['expires_at'];self.store.save()
        web=ROOT/'web'/'js'
        done=run_node_module(self,node,'await import(process.argv[1])',
            (ROOT/'tests/web_agent_lifetime_screen.mjs').as_uri(),
            (ROOT/'tests/web_dom_shim.mjs').as_uri(),(web/'api.js').as_uri(),
            (web/'views/agents.js').as_uri(),(web/'views/project.js').as_uri(),
            'http://127.0.0.1:%d'%self.port,pid,aid,cid,uid,owner,self.admin_user['id'],admin)
        self.assertEqual(0,done.returncode,done.stderr[-4000:])
        self.assertNotIn(secret,done.stdout+done.stderr)
        seen=json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(0,seen['ownerSelector'])
        self.assertTrue(seen['warning']);self.assertTrue(seen['expiry'])
        self.assertTrue(seen['visibleExpiryDates'])
        for date in seen['visibleExpiryDates']:self.assertRegex(date,r'\d{4}')
        self.assertEqual(0,seen['cancelSent'])
        self.assertIn('existing secret keeps working',seen['confirmText'])
        self.assertTrue(seen['retainedAfterLost']);self.assertTrue(seen['sameRetryKey'])
        self.assertEqual([{},{}],seen['extensionBodies'])
        self.assertEqual([200,200],seen['extensionStatuses'])
        self.assertEqual(0,seen['setupDialogsAfterExtension'])
        self.assertTrue(seen['targetAvailable']);self.assertEqual([uid],seen['creationTargets'])
        self.assertTrue(seen['creatorDisclosure']);self.assertTrue(seen['projectExpiry'])
        self.assertEqual([{'status':200,'body':{}}],seen['projectExtension'])
        self.assertTrue(seen['projectPrivacy'])
        self.assertEqual(2,sum(e['action']=='agents.credentials.extended' for e in self.service.state['audit']))
        self.assertEqual(2,len(self.service.state['agents']))

if __name__=='__main__':unittest.main()
