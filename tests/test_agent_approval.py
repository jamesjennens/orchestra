"""kittrial-5bb.438: a coordinator agent may approve the work of ANOTHER agent of the same person.

James, 2026-10-28: "The coordinator is different from the worker." By default (``agent_approval: by-agent``)
an agent holding an effective coordinator grant approves a delivery by another personal agent of its person;
an owner may switch a project to ``by-person``, the strict rule of kittrial-5bb.209. Whatever the setting, an
agent never approves its own work, its person's own work or work under its person's worker credentials; an
agent without an effective grant cannot approve at all; people are judged as before. An approval that only
the setting let through says so in its summary and in the audit.

Through the real routes of the web service (the in-process backend); the canonical binding is in
tests/test_coordinator_grant_real.py.
"""
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import review_workflow
import test_coordinator_grant

API = http_service.ApiHandler
STRICT = API.OWN_PARTY_AGENT[0] + API.OWN_PARTY_AGENT[3]
OWN_WORK = API.SAME_AGENT[0] + API.BY_AGENT_NEXT
OWN_TASK = API.SAME_AGENT[1] + API.BY_AGENT_NEXT
PERSONS_WORK = API.OWN_PARTY_AGENT[0] + API.BY_AGENT_NEXT
PERSONS_TASK = API.OWN_PARTY_AGENT[2] + API.BY_AGENT_NEXT


class SamePerson(test_coordinator_grant.Grants):
    """cora is a coordinator; her agent Heron holds the coordinator grant; Egret is her second agent."""

    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.grant().status)
        self.other, self.other_id = self.agent_with_id('cora', 'Egret')

    def set_mode(self, mode, token=None):
        return self.request('PUT', '/v1/projects/%s/agent-approval' % self.project, {'agent_approval': mode},
                            token=token or self.tokens['olive'])

    def refused(self, answer, sentence):
        self.assertEqual((403, sentence), (answer.status, answer.data.get('error', {}).get('message')), answer.data)

    def approval_summary(self, task):
        records = [r for r in self.backend.state['contributions'][task] if r['kind'] == 'approve']
        return records[-1]['summary']

    def events(self, action):
        with self.service.store.lock:
            return [event for event in self.service.state['audit'] if event['action'] == action]


class DefaultTests(SamePerson):
    def test_by_default_a_coordinator_agent_approves_another_agent_of_its_person(self):
        task, contribution = self.deliver(self.other)
        approved = self.approve(self.agent, task, contribution)
        self.assertEqual(201, approved.status, approved.data)
        self.assertEqual(self.state_of(task), 'approved')
        self.assertEqual(self.approval_summary(task), 'accepted ' + API.SAME_PERSON_APPROVAL)
        event = [e for e in self.events('reviews.add') if e.get('same_person_coordinator')][-1]
        self.assertEqual((event['author'], event['agent_approval'], event['agent_approval_set_by']),
                         (self.other_id, 'by-agent', 'default'))

    def test_its_next_actions_offer_another_agents_work_for_approval(self):
        sibling, _ = self.deliver(self.other)
        own, _ = self.deliver(self.agent)

        def offered():
            data = self.request('GET', '/v1/agents/me/next', token=self.agent).data
            return {a['task']: a for a in data['next_actions'] if a['kind'] == 'to-review'}
        listed = offered()
        self.assertIn(sibling, listed)
        self.assertTrue(listed[sibling]['reason'].startswith('A contribution by another agent of your person awaits review.'))
        self.assertNotIn(own, listed)
        self.assertEqual(200, self.set_mode('by-person').status)
        self.assertNotIn(sibling, offered())

    def test_an_approval_not_needing_the_setting_is_unchanged(self):
        task, contribution = self.deliver(self.tokens['carl'])
        self.assertEqual(201, self.approve(self.agent, task, contribution).status)
        self.assertEqual(self.approval_summary(task), 'accepted')
        self.assertFalse(any(e.get('same_person_coordinator') for e in self.events('reviews.add')))

    def test_its_own_delivery_is_always_refused(self):
        task, contribution = self.deliver(self.agent)
        self.refused(self.approve(self.agent, task, contribution), OWN_WORK)
        self.assertEqual(self.state_of(task), 'awaiting-review')

    def test_its_persons_own_delivery_is_refused(self):
        task, contribution = self.deliver(self.tokens['cora'])
        self.refused(self.approve(self.agent, task, contribution), PERSONS_WORK)

    def test_the_assignee_side_follows_the_same_rule(self):
        for assignee, sentence in (('agent', OWN_TASK), ('cora', PERSONS_TASK), ('other', None)):
            with self.subTest(assignee=assignee):
                task, contribution = self.deliver(self.tokens['carl'])
                self.backend._task(self.project, task)['assignee'] = {
                    'agent': self.agent_id, 'cora': self.ids['cora'], 'other': self.other_id}[assignee]
                answer = self.approve(self.agent, task, contribution)
                if sentence is None:
                    self.assertEqual(201, answer.status, answer.data)
                else:
                    self.refused(answer, sentence)

    def test_a_worker_credential_of_its_person_is_refused(self):
        self.assertEqual(200, self.member('cora', 'owner', token=self.admin).status)
        worker, _ = self.worker_of('cora', 'lane-c')
        task, contribution = self.deliver(worker)
        self.refused(self.approve(self.agent, task, contribution), PERSONS_WORK)

    def test_without_an_effective_grant_it_cannot_approve(self):
        task, contribution = self.deliver(self.other)
        # The grant made ineffective: cora is no longer a coordinator, so the grant adds nothing.
        self.assertEqual(200, self.member('cora', 'contributor').status)
        self.assertEqual(403, self.approve(self.agent, task, contribution).status)
        self.assertEqual(200, self.member('cora', 'coordinator').status)
        # The grant removed.
        self.assertEqual(200, self.grant(method='DELETE').status)
        self.assertEqual(403, self.approve(self.agent, task, contribution).status)
        # An agent never granted.
        self.assertEqual(403, self.approve(self.other, *self.deliver(self.agent)).status)
        self.assertEqual(self.state_of(task), 'awaiting-review')

    def test_people_are_judged_as_before(self):
        self.service.approval_by_another_party = True
        task, contribution = self.deliver(self.other)
        answer = self.approve(self.tokens['cora'], task, contribution)
        self.assertEqual(403, answer.status, answer.data)
        self.assertEqual(answer.data['error']['message'], API.OWN_PARTY + API.OWN_PARTY_NEXT)
        # And her agent, on the same delivery, still may (the setting judges agents, not people).
        self.assertEqual(201, self.approve(self.agent, task, contribution).status)

    def test_recommendations_keep_the_person_rule(self):
        task, contribution = self.deliver(self.other)
        answer = self.recommend(self.agent, task, contribution)
        self.assertEqual(403, answer.status, answer.data)

    def test_a_summary_without_room_for_the_sentence_says_how_much_to_cut(self):
        task, contribution = self.deliver(self.other)
        room = API.SUMMARY_MAX - len(API.SAME_PERSON_APPROVAL) - 1
        too_long = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'approve', 'contribution': contribution, 'summary': 'x' * (room + 3)}, token=self.agent)
        self.assertEqual(422, too_long.status, too_long.data)
        self.assertIn('shorten the summary by 3 characters', too_long.data['error']['message'])
        self.assertEqual(self.state_of(task), 'awaiting-review')
        fits = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'approve', 'contribution': contribution, 'summary': 'x' * room}, token=self.agent)
        self.assertEqual(201, fits.status, fits.data)
        self.assertEqual(len(self.approval_summary(task)), API.SUMMARY_MAX)

    def test_a_caller_cannot_write_the_basis_sentence_itself_and_similar_words_are_allowed(self):
        forged_summaries = (
            API.SAME_PERSON_APPROVAL,
            API.SAME_PERSON_APPROVAL + ' Looks good.',
            'Prefix. ' + API.SAME_PERSON_APPROVAL + ' Suffix.',
            'Looks good. ' + API.SAME_PERSON_APPROVAL,
        )
        # 1. Forged by an ordinary person or independent coordinator agent on independent work:
        ind_task, ind_cid = self.deliver(self.tokens['carl'])
        for who in (self.tokens['olive'], self.agent):
            for text in forged_summaries:
                with self.subTest(who=who, summary=text):
                    refused = self.request('POST', self.base(ind_task) + '/reviews', {
                        'operation': 'approve', 'contribution': ind_cid, 'summary': text}, token=who)
                    self.assertEqual(422, refused.status, refused.data)
                    self.assertIn(API.SAME_PERSON_APPROVAL, refused.data['error']['message'])
                    self.assertEqual(self.state_of(ind_task), 'awaiting-review')
        # 2. Forged by a same-person coordinator agent on a sibling agent's delivery:
        sib_task, sib_cid = self.deliver(self.other)
        for text in forged_summaries:
            with self.subTest(same_person=True, summary=text):
                refused = self.request('POST', self.base(sib_task) + '/reviews', {
                    'operation': 'approve', 'contribution': sib_cid, 'summary': text}, token=self.agent)
                self.assertEqual(422, refused.status, refused.data)
                self.assertIn(API.SAME_PERSON_APPROVAL, refused.data['error']['message'])
                self.assertEqual(self.state_of(sib_task), 'awaiting-review')
        self.assertFalse(any(e.get('same_person_coordinator') for e in self.events('reviews.add')))
        # 3. Legitimate summaries with similar words are accepted, and the server-written sentence still works:
        similar = 'Approved by a coordinator agent of the same person after checking agent_approval: by-agent.'
        ok_ind = self.request('POST', self.base(ind_task) + '/reviews', {
            'operation': 'approve', 'contribution': ind_cid, 'summary': similar}, token=self.tokens['olive'])
        self.assertEqual(201, ok_ind.status, ok_ind.data)
        self.assertEqual(self.approval_summary(ind_task), similar)
        ok_sib = self.request('POST', self.base(sib_task) + '/reviews', {
            'operation': 'approve', 'contribution': sib_cid, 'summary': similar}, token=self.agent)
        self.assertEqual(201, ok_sib.status, ok_sib.data)
        self.assertEqual(self.approval_summary(sib_task), similar + ' ' + API.SAME_PERSON_APPROVAL)
        event = [e for e in self.events('reviews.add') if e.get('same_person_coordinator')][-1]
        self.assertEqual(event['author'], self.other_id)

    def test_next_actions_keep_a_siblings_work_out_for_an_agent_without_a_grant(self):
        by_heron, _ = self.deliver(self.agent)
        by_egret, _ = self.deliver(self.other)
        by_carl, _ = self.deliver(self.tokens['carl'])

        def offered_to(token):
            data = self.request('GET', '/v1/agents/me/next', token=token).data
            return {a['task']: a for a in data['next_actions'] if a['kind'] == 'to-review'}

        # Egret has no coordinator grant: Heron's sibling delivery is kept out, while Carl's is offered.
        egret_actions = offered_to(self.other)
        self.assertNotIn(by_heron, egret_actions)
        self.assertNotIn(by_egret, egret_actions)
        self.assertIn(by_carl, egret_actions)
        # Once Heron's coordinator grant is removed, Egret's sibling delivery is kept out for Heron too.
        self.assertEqual(200, self.grant(method='DELETE').status)
        heron_actions = offered_to(self.agent)
        self.assertNotIn(by_egret, heron_actions)
        self.assertNotIn(by_heron, heron_actions)
        self.assertIn(by_carl, heron_actions)

    def test_summary_max_comes_from_review_workflow_text_limits(self):
        self.assertIs(http_service.TEXT_LIMITS, review_workflow.TEXT_LIMITS)
        self.assertEqual(API.SUMMARY_MAX, review_workflow.TEXT_LIMITS['summary'])


class StrictTests(SamePerson):
    def test_by_person_refuses_another_agent_of_its_person_as_before(self):
        self.assertEqual(200, self.set_mode('by-person').status)
        task, contribution = self.deliver(self.other)
        self.refused(self.approve(self.agent, task, contribution), STRICT)
        self.assertEqual(200, self.set_mode('by-agent').status)
        self.assertEqual(201, self.approve(self.agent, task, contribution).status)
        event = [e for e in self.events('reviews.add') if e.get('same_person_coordinator')][-1]
        self.assertEqual(event['agent_approval_set_by'], self.ids['olive'])

    def test_a_damaged_setting_reads_strict(self):
        with self.service.store.lock:
            self.service.state['agent_approval'] = {self.project: 'yes'}
        task, contribution = self.deliver(self.other)
        self.refused(self.approve(self.agent, task, contribution), STRICT)
        shown = self.request('GET', '/v1/projects/%s' % self.project, token=self.tokens['olive']).data['agent_approval']
        self.assertEqual((shown['mode'], shown.get('damaged')), ('by-person', True))


class SettingTests(SamePerson):
    def test_only_an_owner_sets_it_and_it_is_audited_and_shown(self):
        project = self.request('GET', '/v1/projects/%s' % self.project, token=self.tokens['olive']).data
        self.assertEqual((project['agent_approval']['mode'], project['agent_approval']['set_by']), ('by-agent', None))
        self.assertIs(project['can_set_agent_approval'], True)
        for name in ('cora', 'carl'):
            with self.subTest(member=name):
                self.assertEqual(403, self.set_mode('by-person', token=self.tokens[name]).status)
                self.assertIs(self.request('GET', '/v1/projects/%s' % self.project,
                                           token=self.tokens[name]).data['can_set_agent_approval'], False)
        self.assertEqual(403, self.set_mode('by-person', token=self.agent).status)     # never a credential
        outsider = self.create_account(self.admin, 'otis', 'otis-password-1')
        self.assertTrue(outsider)
        self.assertEqual(404, self.set_mode('by-person', token=self.login('otis', 'otis-password-1')[0]).status)
        written = self.set_mode('by-person')
        self.assertEqual(200, written.status, written.data)
        self.assertEqual((written.data['mode'], written.data['set_by']), ('by-person', self.ids['olive']))
        self.assertTrue(written.data['set_at'])
        shown = self.request('GET', '/v1/projects/%s' % self.project, token=self.tokens['carl']).data['agent_approval']
        self.assertEqual((shown['mode'], shown['set_by_display_name']), ('by-person', 'olive'))
        audited = self.events('projects.agent_approval')
        self.assertEqual(audited[-1]['agent_approval'], 'by-person')

    def test_an_unknown_value_or_field_is_refused(self):
        for body in ({'agent_approval': 'by-team'}, {'agent_approval': True}, {'agent_approval': 'by-agent', 'x': 1}, {}):
            with self.subTest(body=body):
                answer = self.request('PUT', '/v1/projects/%s/agent-approval' % self.project, body,
                                      token=self.tokens['olive'])
                self.assertEqual(422, answer.status, answer.data)



class AgentApprovalPanelTests(unittest.TestCase):
    """Members & settings: the panel follows the server's word and renders text only."""

    def test_the_panel_follows_the_server_and_renders_text(self):
        import json
        import shutil
        import test_http_web
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        code = """await import(process.argv[1]);
const view=await import(process.argv[2]);
const input=JSON.parse(process.argv[3]);
const ctx={href:(p)=>'#'+p,api:{}};
const owner=view.agentApprovalPanel(ctx,{id:'pp',can_set_agent_approval:true,agent_approval:input.block});
const member=view.agentApprovalPanel(ctx,{id:'pp',can_set_agent_approval:false,agent_approval:input.block});
const unset=view.agentApprovalPanel(ctx,{id:'pp',can_set_agent_approval:true});
const broken=view.agentApprovalPanel(ctx,{id:'pp',can_set_agent_approval:true,agent_approval:{mode:'by-person',damaged:true}});
console.log(JSON.stringify({owner:owner.querySelectorAll('select').length,member:member.querySelectorAll('select').length,
  text:member.textContent,unset:unset.textContent,broken:broken.textContent,
  scripts:member.querySelectorAll('script').length+member.querySelectorAll('img').length}));"""
        block = {'mode': 'by-person', 'set_by': 'u_1', 'set_by_display_name': '<img src=x onerror=1>olive',
                 'set_at': '2026-01-02T03:04:05Z'}
        done = test_http_web.run_node_module(self, node, code, (KIT / 'tests/web_dom_shim.mjs').as_uri(),
                                             (KIT / 'web/js/views/project.js').as_uri(), json.dumps({'block': block}))
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual((out['owner'], out['member']), (1, 0))
        self.assertEqual(out['scripts'], 0)
        self.assertIn('<img src=x onerror=1>olive', out['text'])
        self.assertIn('Strict', out['text'])
        self.assertIn('the default; nobody has changed it', out['unset'])
        self.assertIn('another agent of the same person', out['unset'])
        self.assertIn('could not be read, so the strict rule applies', out['broken'])


if __name__ == '__main__':
    unittest.main()
