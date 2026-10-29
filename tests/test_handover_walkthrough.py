"""kittrial-5bb.442: the hand-over walkthrough (design COORDINATOR_ONBOARDING_DESIGN §3, §5.1).

GET /v1/projects/{pid}/handover answers steps O1-O5 and C1 for one member, each checked from what the
service already holds, with a plain sentence and the existing route that fixes it; it writes nothing. Who
reads what follows §3.3: a member their own; an owner any member's, seeing only agents that have the
project; a coordinator any member's without their agents; an agent its own; anyone else one refusal that
says nothing about the person. The step rules are pinned on ``handover_steps`` directly, the reading rules
through the real routes (in-process backend).
"""
import json
import shutil
import sys
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import handover_steps as hs
import http_service
import test_coordinator_grant

T0, T1 = '2026-01-01T00:00:00Z', '2026-01-01T00:05:00Z'


def grant(effective=True, why=None, at=T0):
    return {'granted_by': 'u_owner', 'granted_by_display_name': 'olive', 'granted_at': at,
            'effective': effective, 'no_effect_because': why}


def agent(ident='agt_1', name='Heron', has=True, last_seen=None, coordinator=None, self_check=None):
    return {'id': ident, 'name': name, 'has_project': has, 'enabled': True, 'last_seen_at': last_seen,
            'coordinator': coordinator, 'self_check': self_check}


def report(at=T1, matches=True, listed=True, role='coordinator'):
    """An agent's report of its own first check, as ``Service.handover_facts`` gives it (kittrial-5bb.444)."""
    return {'project': 'alpha', 'sha256_matches': matches, 'project_listed': listed, 'role': role, 'reported_at': at}


def facts(role='coordinator', agents=(), effective_agent=None, viewer_agent=None, note=None):
    return {'person': 'u_cora', 'name': 'cora', 'member': True, 'role': role, 'agents': list(agents),
            'effective_agent': effective_agent, 'viewer_agent': viewer_agent, 'note': note}


NOTE = {'version': 1, 'written_by_name': 'olive', 'written_at': T0, 'acknowledged': True}


def by_id(steps):
    return {s['id']: s for s in steps}


ON = {'items': [{'name': n, 'state': 'on', 'unlocks': 'u', 'command': 'admin.py %s on --actor OPERATOR' % n}
                for n in ('review_workflow_writes', 'web_lifecycle_mark_writes')]}


class StepRuleTests(unittest.TestCase):
    def read(self, f, tier='owner', switches=ON, approval=None, agent_id=None):
        return by_id(hs.steps(f, tier=tier, agent_id=agent_id, switches=switches, approval=approval))

    def test_the_steps_come_in_order_and_use_only_the_five_states(self):
        steps = hs.steps(facts(agents=[agent(coordinator=grant(), last_seen=T1, self_check=report())], note=NOTE),
                         tier='owner', switches=ON)
        # O6 and C3 (the hand-over note) arrived with kittrial-5bb.443, C2 (the agent's own check) with .444.
        self.assertEqual([s['id'] for s in steps], ['role', 'agent', 'grant', 'switches', 'agent-approval',
                                                     'handover-note', 'setup-after-grant', 'self-check',
                                                     'acknowledge-note'])
        self.assertTrue(all(s['state'] in hs.STATES for s in steps))
        self.assertEqual({s['state'] for s in steps}, {'done'})

    def test_o1_the_role(self):
        self.assertEqual(self.read(facts(role='owner'))['role']['state'], 'done')
        owner = self.read(facts(role='contributor'))['role']
        self.assertEqual((owner['state'], owner['detail']),
                         ('todo', 'cora is not yet a coordinator here. Set their role to Coordinator.'))
        self.assertEqual(owner['fix']['page'], 'members')
        mine = self.read(facts(role='contributor'), tier='self')['role']
        self.assertIn('ask an owner of this project to set your role to Coordinator', mine['detail'])
        self.assertIsNone(mine['fix'])

    def test_o2_waits_for_the_role_so_no_agent_is_made_first(self):
        self.assertEqual(self.read(facts(role='viewer', agents=[agent()]))['agent']['state'], 'waiting')

    def test_o2_the_owner_is_never_told_of_an_agent_without_the_project(self):
        f = facts(agents=[agent(name='Early', has=False)])
        steps = hs.steps(f, tier='owner', switches=ON)
        self.assertEqual(by_id(steps)['agent']['detail'],
                         'No agent of cora has this project yet: they open My agents and tick it, or create an agent now.')
        self.assertNotIn('Early', json.dumps(steps))
        self.assertIsNone(hs.pick(f, None, 'owner'))
        self.assertIsNone(hs.pick(f, 'agt_1', 'owner'))

    def test_o2_the_person_is_told_of_the_agent_made_before_the_membership(self):
        step = self.read(facts(agents=[agent(name='Early', has=False)]), tier='self')['agent']
        self.assertEqual((step['state'], step['detail']),
                         ('todo', 'Early was made before you could see this project: open it under My agents and tick '
                                  'this project.'))
        self.assertEqual(step['fix']['page'], 'my-agents')
        none = self.read(facts(agents=[]), tier='self')['agent']
        self.assertIn('You have no agent yet', none['detail'])

    def test_o3_the_grant_and_each_reason_it_has_no_effect(self):
        self.assertEqual(self.read(facts(agents=[agent()]))['grant']['state'], 'todo')
        self.assertEqual(self.read(facts(agents=[agent(coordinator=grant())]))['grant']['state'], 'done')
        for why, words in (('agent-disabled', 'Heron is switched off'), ('account-disabled', "cora's account is disabled"),
                           ('account-role', 'cora is no longer a coordinator here'),
                           ('issuer-role', 'The owner who granted it is no longer an owner here')):
            with self.subTest(why=why):
                step = self.read(facts(agents=[agent(coordinator=grant(False, why))]))['grant']
                self.assertEqual(step['state'], 'failed')
                self.assertTrue(step['detail'].startswith(words), step['detail'])
        self.assertEqual(self.read(facts(role='viewer'))['grant']['state'], 'waiting')

    def test_o3_an_agent_is_not_told_why_its_grant_has_no_effect(self):
        f = facts(agents=[agent(coordinator=grant(False, 'issuer-role'))], viewer_agent='agt_1')
        step = self.read(f, tier='agent')['grant']
        self.assertEqual(step['state'], 'failed')
        self.assertNotIn('owner who granted', step['detail'])

    def test_a_coordinator_sees_no_agent_and_only_an_effective_grant(self):
        f = facts(agents=[agent(name='Hidden', coordinator=grant(), last_seen=T1)])
        steps = hs.steps(f, tier='coordinator', switches=ON)
        got = by_id(steps)
        self.assertEqual((got['agent']['state'], got['grant']['state'], got['setup-after-grant']['state']),
                         ('hidden', 'hidden', 'hidden'))
        self.assertNotIn('Hidden', json.dumps(steps))
        f['effective_agent'] = 'Hidden'
        self.assertEqual(self.read(f, tier='coordinator')['grant']['state'], 'done')

    def test_o4_the_switches(self):
        f = facts()
        self.assertEqual(self.read(f, switches={'refused': True})['switches']['state'], 'hidden')
        self.assertIn('cannot read', self.read(f, switches={'unavailable': True})['switches']['detail'])
        self.assertEqual(self.read(f)['switches']['state'], 'done')
        off = {'items': [dict(ON['items'][0], state='off'), ON['items'][1]]}
        step = self.read(f, switches=off)['switches']
        self.assertEqual((step['state'], step['who'], step['fix']['commands']),
                         ('todo', 'operator', ['admin.py review_workflow_writes on --actor OPERATOR']))
        broken = {'items': [dict(ON['items'][1], state='unreadable')]}
        self.assertEqual(self.read(f, switches=broken)['switches']['state'], 'failed')

    def test_o5_the_agent_approval_mode(self):
        default = self.read(facts(), approval={'mode': 'by-agent', 'set_by': None})['agent-approval']
        self.assertEqual(default['state'], 'done')
        self.assertIn('the default', default['detail'])
        self.assertIn('Nobody has changed it', default['detail'])
        strict = self.read(facts(), approval={'mode': 'by-person', 'set_by': 'u_o', 'set_by_display_name': 'olive',
                                              'set_at': T1})['agent-approval']
        self.assertIn('Strict', strict['detail'])
        self.assertIn('Set by olive', strict['detail'])

    def test_c1_set_up_after_the_grant(self):
        self.assertEqual(self.read(facts(agents=[agent()]))['setup-after-grant']['state'], 'waiting')
        before = self.read(facts(agents=[agent(coordinator=grant(at=T1), last_seen=T0)]))['setup-after-grant']
        self.assertEqual(before['state'], 'todo')
        self.assertIn('Open the set-up for Heron again now', before['detail'])
        self.assertEqual(self.read(facts(agents=[agent(coordinator=grant(at=T0), last_seen=T1)]))
                         ['setup-after-grant']['state'], 'done')
        self.assertEqual(self.read(facts(agents=[agent(coordinator=grant(at=T0), last_seen=None)]))
                         ['setup-after-grant']['state'], 'todo')

    def test_c1_a_grant_and_a_request_in_the_same_second_is_not_after(self):
        # The stamps are whole seconds: a request in the grant's own second cannot show it came after the grant
        # (review of kittrial-5bb.442, M8).
        same = self.read(facts(agents=[agent(coordinator=grant(at=T0), last_seen=T0)]))['setup-after-grant']
        self.assertEqual(same['state'], 'todo')

    def test_an_agent_asked_for_that_the_reader_cannot_see_gets_one_generic_sentence(self):
        f = facts(agents=[agent(ident='agt_1', name='Early', has=False), agent(ident='agt_2', name='Heron')])
        for asked in ('agt_1', 'agt_unknown'):
            with self.subTest(asked=asked):
                step = self.read(f, agent_id=asked)['agent']
                self.assertEqual((step['state'], step['detail']),
                                 ('hidden', 'The agent asked for is not one you can see in this project.'))
        self.assertEqual(self.read(f, agent_id='agt_2')['agent']['state'], 'done')

    def test_an_unknown_viewer_is_refused(self):
        with self.assertRaises(ValueError):
            hs.steps(facts(), tier='anyone')


class HandoverHttpTests(test_coordinator_grant.Grants):
    """olive and oscar own Alpha, carl contributes, cora coordinates and has the agent Heron."""

    def read(self, token, person=None, agent=None):
        query = '&'.join(p for p in ('person=%s' % person if person else '', 'agent=%s' % agent if agent else '') if p)
        return self.request('GET', '/v1/projects/%s/handover%s' % (self.project, '?' + query if query else ''),
                            token=token)

    def steps(self, token, person=None):
        answer = self.read(token, person)
        self.assertEqual(200, answer.status, answer.data)
        return by_id(answer.data['steps']), answer.data

    def snapshot(self):
        with self.service.store.lock:
            state = self.service.state
            return json.dumps({k: state.get(k) for k in ('memberships', 'agents', 'coordinator_grants',
                                                         'agent_approval')}, sort_keys=True, default=str)

    def test_an_owner_reads_a_members_walkthrough_and_nothing_is_written(self):
        before = self.snapshot()
        got, data = self.steps(self.tokens['olive'], self.ids['cora'])
        self.assertEqual((data['viewer'], data['person']['name'], data['agent']['name']), ('owner', 'cora', 'Heron'))
        self.assertEqual((got['role']['state'], got['agent']['state'], got['grant']['state']), ('done', 'done', 'todo'))
        self.assertEqual(before, self.snapshot())

    def test_a_member_reads_their_own(self):
        got, data = self.steps(self.tokens['carl'])
        self.assertEqual(data['viewer'], 'self')
        self.assertIn('ask an owner of this project', got['role']['detail'])

    def test_anyone_else_gets_one_refusal_that_says_nothing_about_the_person(self):
        refused = self.read(self.tokens['carl'], self.ids['cora'])
        nobody = self.read(self.tokens['carl'], 'u_nobody_here')
        self.assertEqual((refused.status, nobody.status), (403, 403))
        self.assertEqual(refused.data['error']['message'], http_service.ApiHandler.HANDOVER_REFUSED)
        self.assertEqual(refused.data['error'], nobody.data['error'])
        self.assertNotIn('Heron', json.dumps(refused.data))

    def test_a_coordinator_reads_the_person_without_their_agents(self):
        self.assertEqual(200, self.member('carl', 'coordinator').status)
        got, data = self.steps(self.tokens['carl'], self.ids['cora'])
        self.assertEqual(data['viewer'], 'coordinator')
        self.assertIsNone(data['agent'])
        self.assertEqual((got['agent']['state'], got['grant']['state'], got['setup-after-grant']['state']),
                         ('hidden', 'hidden', 'hidden'))
        self.assertNotIn('Heron', json.dumps(data))
        self.assertEqual(200, self.grant().status)
        got, _ = self.steps(self.tokens['carl'], self.ids['cora'])
        self.assertEqual(got['grant']['state'], 'done')     # an effective grant is what any reader can see

    def test_an_outsider_gets_404_and_nobody_signed_in_401(self):
        self.create_account(self.admin, 'otis', 'otis-password-1')
        self.assertEqual(404, self.read(self.login('otis', 'otis-password-1')[0]).status)
        self.assertEqual(401, self.read(None).status)

    def test_an_outsider_gets_exactly_the_answer_for_a_project_that_does_not_exist(self):
        # Review of kittrial-5bb.442, M5b: the route's own project check, not a later one, answers an outsider.
        self.create_account(self.admin, 'olga', 'olga-password-1')
        outsider = self.login('olga', 'olga-password-1')[0]
        real = self.read(outsider)
        missing = self.request('GET', '/v1/projects/proj_missing0000/handover', token=outsider)
        usual = self.request('GET', '/v1/projects/%s' % self.project, token=outsider)
        self.assertEqual((real.status, missing.status, usual.status), (404, 404, 404))
        # Both equal the project read's own 404, so neither comes from the walkthrough's later member check.
        self.assertEqual(real.data['error'], usual.data['error'])
        self.assertEqual(missing.data['error'], usual.data['error'])

    def test_only_an_owner_or_a_coordinator_is_offered_the_member_picker(self):
        self.assertEqual(200, self.member('carl', 'coordinator').status)
        for token, offered in ((self.tokens['olive'], True), (self.tokens['carl'], True)):
            self.assertIs(self.steps(token)[1]['can_choose_person'], offered)
        self.assertEqual(200, self.member('carl', 'contributor').status)
        self.assertIs(self.steps(self.tokens['carl'])[1]['can_choose_person'], False)
        self.assertEqual(200, self.grant().status)
        self.assertIs(self.steps(self.agent)[1]['can_choose_person'], False)

    def test_an_owner_asking_for_an_agent_it_cannot_see_gets_the_generic_sentence(self):
        _, early_id = self.agent_with_id('cora', 'Kite')         # the fixture gives it the project ...
        self.assertEqual(200, self.request('PATCH', '/v1/agents/%s' % early_id, {'projects': []},
                                           token=self.tokens['cora']).status)   # ... and cora takes it away
        answer = self.read(self.tokens['olive'], self.ids['cora'], agent=early_id)
        step = {s['id']: s for s in answer.data['steps']}['agent']
        self.assertEqual(step['detail'], 'The agent asked for is not one you can see in this project.')
        self.assertNotIn('Kite', json.dumps(answer.data))

    def test_an_owner_asking_for_a_non_member_gets_404(self):
        stranger = self.create_account(self.admin, 'nina', 'nina-password-1')
        self.assertEqual(404, self.read(self.tokens['olive'], stranger).status)

    def test_an_agent_reads_its_own_steps_and_no_one_elses(self):
        self.assertEqual(200, self.grant().status)
        got, data = self.steps(self.agent)
        self.assertEqual((data['viewer'], data['agent']['name'], got['grant']['state']), ('agent', 'Heron', 'done'))
        self.assertEqual(403, self.read(self.agent, self.ids['olive']).status)

    def test_the_silent_case_from_both_sides(self):
        """An agent made before its person was a member: named to the person, never to the owner."""
        dana = self.create_account(self.admin, 'dana', 'dana-password-1')
        self.ids['dana'] = dana
        self.tokens['dana'] = self.login('dana', 'dana-password-1')[0]
        made = self.request('POST', '/v1/agents', {'name': 'Early', 'working_directory': '/home/x/early', 'projects': []},
                            token=self.tokens['dana'])
        self.assertEqual(201, made.status, made.data)
        self.assertEqual(200, self.member('dana', 'coordinator').status)
        owner_steps, owner_data = self.steps(self.tokens['olive'], dana)
        self.assertEqual(owner_steps['agent']['state'], 'todo')
        self.assertNotIn('Early', json.dumps(owner_data))
        mine, _ = self.steps(self.tokens['dana'])
        self.assertTrue(mine['agent']['detail'].startswith('Early was made before you could see this project'))

    def test_a_grant_without_effect_says_why(self):
        self.assertEqual(200, self.grant().status)
        self.assertEqual(200, self.member('cora', 'contributor').status)
        got, _ = self.steps(self.tokens['olive'], self.ids['cora'])
        self.assertEqual(got['role']['state'], 'todo')
        self.assertEqual(200, self.member('cora', 'coordinator').status)
        self.assertEqual(200, self.request('POST', '/v1/agents/%s/disable' % self.agent_id, {},
                                           token=self.tokens['cora']).status)
        got, _ = self.steps(self.tokens['olive'], self.ids['cora'])
        self.assertEqual(got['grant']['state'], 'failed')
        self.assertTrue(got['grant']['detail'].startswith('Heron is switched off'))

    def test_set_up_after_the_grant_follows_the_agents_own_requests(self):
        start = time.time()
        self.service._now = lambda: start
        self.assertEqual(200, self.grant().status)
        got, _ = self.steps(self.tokens['olive'], self.ids['cora'])
        self.assertEqual(got['setup-after-grant']['state'], 'todo')
        self.service._now = lambda: start + 60
        self.assertEqual(200, self.request('GET', '/v1/agents/me', token=self.agent).status)
        got, _ = self.steps(self.tokens['olive'], self.ids['cora'])
        self.assertEqual(got['setup-after-grant']['state'], 'done')

    def test_the_switches_come_from_the_backend_and_only_for_coordinators(self):
        got, _ = self.steps(self.tokens['olive'], self.ids['cora'])
        self.assertIn('cannot read', got['switches']['detail'])            # the in-process backend has none
        off = {'items': [{'name': 'review_workflow_writes', 'state': 'off', 'unlocks': 'u',
                          'command': 'admin.py review-writes on --actor OPERATOR'}]}
        self.backend.switches = lambda pid: off
        got, _ = self.steps(self.tokens['olive'], self.ids['cora'])
        self.assertEqual((got['switches']['state'], got['switches']['fix']['commands']),
                         ('todo', ['admin.py review-writes on --actor OPERATOR']))
        mine, _ = self.steps(self.tokens['carl'])
        self.assertEqual(mine['switches']['state'], 'hidden')

        def refused(pid):
            raise http_service.HttpError(422, 'invalid_payload', 'Unknown action web-switches')
        self.backend.switches = refused                          # an older endpoint: only this step says so
        got, _ = self.steps(self.tokens['olive'], self.ids['cora'])
        self.assertIn('cannot read', got['switches']['detail'])
        self.assertEqual(got['role']['state'], 'done')


class HandoverPageTests(unittest.TestCase):
    """The page links to where a step is done, shows commands as text, and fixes nothing itself."""

    def test_each_step_links_to_its_fix_and_renders_text(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        code = """await import(process.argv[1]);
const view=await import(process.argv[2]);
const input=JSON.parse(process.argv[3]);
const ctx={href:(p)=>'#'+p};
const out=input.steps.map((s)=>{const li=view.stepItem(ctx,'pp',s);return {id:s.id,
  links:[...li.querySelectorAll('a')].map((a)=>a.getAttribute('href')),codes:li.querySelectorAll('code').length,
  buttons:li.querySelectorAll('button').length,scripts:li.querySelectorAll('script').length+li.querySelectorAll('img').length,
  text:li.textContent};});
console.log(JSON.stringify(out));"""
        steps = [
            {'id': 'role', 'title': 'Coordinator role', 'state': 'todo', 'detail': '<img src=x onerror=1>cora',
             'fix': {'page': 'members'}},
            {'id': 'grant', 'title': 'Coordinator grant', 'state': 'done', 'detail': 'ok', 'fix': {'page': 'settings'}},
            {'id': 'agent', 'title': 'Agent', 'state': 'hidden', 'detail': 'x', 'fix': {'page': 'my-agents'}},
            {'id': 'switches', 'title': 'Switches', 'state': 'todo', 'detail': 'off',
             'fix': {'commands': ['admin.py review-writes on --actor OPERATOR']}},
        ]
        done = test_http_web.run_node_module(self, node, code, (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web/js/views/handover.js').as_uri(), json.dumps({'steps': steps}))
        self.assertEqual(done.returncode, 0, done.stderr)
        out = {o['id']: o for o in json.loads(done.stdout.strip().splitlines()[-1])}
        self.assertEqual(out['role']['links'], ['#/p/pp/settings'])
        self.assertEqual(out['grant']['links'], [])                 # done: nothing to open
        self.assertEqual(out['agent']['links'], [])                 # hidden: nothing to open
        self.assertEqual((out['switches']['codes'], out['switches']['buttons']), (1, 1))   # a command and its copy
        self.assertEqual(out['role']['scripts'], 0)
        self.assertIn('<img src=x onerror=1>cora', out['role']['text'])


    def test_the_member_picker_appears_only_on_the_servers_word(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        import test_http_web
        code = """await import(process.argv[1]);
const view=await import(process.argv[2]);
const ctx={href:(p)=>'#'+p,api:{members:async()=>({items:[{user_id:'u1',display_name:'olive'},{user_id:'u2',display_name:'cora'}]})}};
const base={person:{id:'u1',name:'olive'}};
const off=await view.memberPicker(ctx,'pp',Object.assign({can_choose_person:false},base),()=>{});
const on=await view.memberPicker(ctx,'pp',Object.assign({can_choose_person:true},base),()=>{});
console.log(JSON.stringify({off:off===null,on:on?on.querySelectorAll('select').length:0}));"""
        done = test_http_web.run_node_module(self, node, code, (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web/js/views/handover.js').as_uri(), '{}')
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual((out['off'], out['on']), (True, 1))


if __name__ == '__main__':
    unittest.main()
