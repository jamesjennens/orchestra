"""kittrial-5bb.540, approval basis part C: the text gate, one basis for the route and the hint, true wording.

1. The text gate stays as defence in depth and is widened. On approve and on recommend it refuses (422, nothing
   written) the two server sentences as they were and as they are now, the two field labels as they were and as
   they are now (with or without "Allowed by:"), and a line starting "Approval basis:", the brief's label. Words
   that only resemble them are still allowed.
3. The queue hint names the basis the approval record would: :func:`http_authority.by_agent_approval`'s, where
   the delivering author decides when it is of the approver's party and the assignee only when the author is
   independent.
4. The wording names the KIND of party on the task (the coordinator's answer, 2026-11-06), so it is true whichever
   of the author and the assignee decided.
"""
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import review_workflow as w
import test_agent_approval_credentials as cred
import test_http_agents

API = http_service.ApiHandler

#: The texts as they were before kittrial-5bb.540. Records already written keep them, so they stay refused.
OLD_AGENT_SENTENCE = ('Approved by a coordinator agent of the same person as the agent that delivered it '
                      '(project setting agent_approval: by-agent).')
OLD_CREDENTIAL_SENTENCE = ('Approved by a coordinator agent of the same person as the worker credential that '
                           'delivered it (project setting agent_approval: by-agent).')
OLD_AGENT_LABEL = ('Allowed by: coordinator agent of the same person as the delivering agent '
                   '(project setting agent_approval: by-agent; the audit entry holds who set it and when)')
OLD_CREDENTIAL_LABEL = ('Allowed by: coordinator agent of the same person as the delivering worker credential '
                        '(project setting agent_approval: by-agent; the audit entry holds who set it and when)')
#: The texts from kittrial-5bb.540 on: the kind of party on the task, not which one delivered.
AGENT_SENTENCE = ('Approved by a coordinator agent of the same person as the agent on this task '
                  '(project setting agent_approval: by-agent).')
CREDENTIAL_SENTENCE = ('Approved by a coordinator agent of the same person as the worker credential on this task '
                       '(project setting agent_approval: by-agent).')
AGENT_LABEL = ('Allowed by: coordinator agent of the same person as the agent on this task '
               '(project setting agent_approval: by-agent; the audit entry holds who set it and when)')
CREDENTIAL_LABEL = ('Allowed by: coordinator agent of the same person as the worker credential on this task '
                    '(project setting agent_approval: by-agent; the audit entry holds who set it and when)')


def bare(label):
    return label[len('Allowed by: '):]


#: Every text the gate refuses, by name; each one is its own subtest on approve and on recommend.
REFUSED = {
    'old agent sentence': OLD_AGENT_SENTENCE,
    'old credential sentence': OLD_CREDENTIAL_SENTENCE,
    'new agent sentence': AGENT_SENTENCE,
    'new credential sentence': CREDENTIAL_SENTENCE,
    'old agent label': OLD_AGENT_LABEL,
    'old agent label without Allowed by': bare(OLD_AGENT_LABEL),
    'old credential label': OLD_CREDENTIAL_LABEL,
    'old credential label without Allowed by': bare(OLD_CREDENTIAL_LABEL),
    'new agent label': AGENT_LABEL,
    'new agent label without Allowed by': bare(AGENT_LABEL),
    'new credential label': CREDENTIAL_LABEL,
    'new credential label without Allowed by': bare(CREDENTIAL_LABEL),
    'brief label line': 'Looks good.\nApproval basis: same-person-agent',
    'brief label first': 'Approval basis: whatever I like',
    'brief label spaced and cased': 'ok\n   APPROVAL  BASIS : mine',
}

#: Words that only resemble the texts: allowed.
ALLOWED = ('approved', 'the agent that delivered it', 'basis', 'The approval basis looks right to me.',
           'Approved by a coordinator agent of the same person after checking the worker credential.')


class GateTests(cred.PinBase):
    """Rule 1: every text the server or a reader writes is refused from a caller, on approve and on recommend."""

    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.member('cora', 'owner', token=self.admin).status)

    def test_each_text_is_refused_422_on_approve_and_on_recommend_and_nothing_is_written(self):
        task, cid = self.deliver(self.tokens['carl'])
        before = len(self.events('reviews.add'))
        for name, text in REFUSED.items():
            for who, token in (('agent', self.agent), ('person', self.tokens['olive'])):
                with self.subTest(text=name, on='approve', by=who):
                    refused = self.request('POST', self.base(task) + '/reviews', {
                        'operation': 'approve', 'contribution': cid, 'summary': text}, token=token)
                    self.assertEqual(422, refused.status, refused.data)
                    self.assertIn('may not contain', refused.data['error']['message'])
                    self.assertEqual('awaiting-review', self.state_of(task))
            with self.subTest(text=name, on='recommend'):
                refused = self.request('POST', self.base(task) + '/reviews', {
                    'operation': 'recommend', 'schema_version': 1, 'operation_id': 'rec-' + str(abs(hash(name))),
                    'contribution': cid, 'commit': test_http_agents.COMMIT, 'verdict': 'approve',
                    'summary': text, 'items': []}, token=self.agent)
                self.assertEqual(422, refused.status, refused.data)
                self.assertIn('may not contain', refused.data['error']['message'])
        self.assertEqual(before, len(self.events('reviews.add')))

    def test_the_refusal_names_the_text_it_found(self):
        task, cid = self.deliver(self.tokens['carl'])
        for text, named in ((bare(OLD_CREDENTIAL_LABEL), OLD_CREDENTIAL_LABEL), (AGENT_SENTENCE, AGENT_SENTENCE),
                            ('x\nApproval basis: y', 'Approval basis:')):
            with self.subTest(text=text):
                refused = self.request('POST', self.base(task) + '/reviews', {
                    'operation': 'approve', 'contribution': cid, 'summary': text}, token=self.agent)
                self.assertEqual(422, refused.status, refused.data)
                self.assertIn(repr(named), refused.data['error']['message'])

    def test_words_that_only_resemble_them_are_allowed(self):
        for text in ALLOWED:
            with self.subTest(text=text):
                self.assertIsNone(API._basis_sentence_in(API.__new__(API), text))
        task, cid = self.deliver(self.tokens['carl'])
        done = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'approve', 'contribution': cid, 'summary': ALLOWED[3]}, token=self.tokens['olive'])
        self.assertEqual(201, done.status, done.data)


class WordingTests(cred.PinBase):
    """Rule 4: the labels and the sentences name the kind of party on the task."""

    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.member('cora', 'owner', token=self.admin).status)
        self.lane_c, _ = self.worker_of('cora', 'lane-c')

    def test_the_labels_name_the_kind_on_the_task(self):
        self.assertEqual({'same-person-agent': AGENT_LABEL, 'same-person-worker-credential': CREDENTIAL_LABEL},
                         w.APPROVAL_BASES)

    def test_the_old_labels_are_kept_for_the_gate(self):
        self.assertEqual((OLD_AGENT_LABEL, OLD_CREDENTIAL_LABEL), w.RETIRED_APPROVAL_BASIS_LABELS)

    def test_the_sentences_name_the_kind_on_the_task(self):
        self.assertEqual((AGENT_SENTENCE, CREDENTIAL_SENTENCE), (API.SAME_PERSON_APPROVAL, API.WORKER_CREDENTIAL_APPROVAL))
        self.assertEqual((OLD_AGENT_SENTENCE, OLD_CREDENTIAL_SENTENCE), API.RETIRED_APPROVAL_SENTENCES)

    def test_a_new_approval_is_written_with_the_new_sentence(self):
        by_agent, c1 = self.deliver(self.other)
        by_credential, c2 = self.deliver(self.lane_c)
        self.assertEqual(201, self.approve(self.agent, by_agent, c1).status)
        self.assertEqual(201, self.approve(self.agent, by_credential, c2).status)
        self.assertEqual('accepted ' + AGENT_SENTENCE, self.approval_summary(by_agent))
        self.assertEqual('accepted ' + CREDENTIAL_SENTENCE, self.approval_summary(by_credential))

    def test_the_new_agent_sentence_is_true_when_the_assignee_decided(self):
        """An independent author and another agent of the person assigned: the agent on the task decided."""
        task, cid = self.deliver(self.tokens['carl'])
        self.backend._task(self.project, task)['assignee'] = self.other_id
        self.assertEqual(201, self.approve(self.agent, task, cid).status)
        self.assertEqual('accepted ' + AGENT_SENTENCE, self.approval_summary(task))
        event = [e for e in self.events('reviews.add') if e.get('same_person_coordinator')][-1]
        self.assertEqual('agent', event['basis'])


class HintTests(cred.ShapeSetup):
    """Rule 3: GET /v1/agents/me/next names the basis the approval record would, shape by shape."""

    CREDENTIAL = 'A contribution under a worker credential of your person awaits review.'
    AGENT = 'A contribution by another agent of your person awaits review.'

    def offered(self):
        data = self.request('GET', '/v1/agents/me/next', token=self.agent).data
        return {a['task']: a for a in data['next_actions'] if a['kind'] == 'to-review'}

    def check(self, task, cid, hint, basis):
        listed = self.offered()
        self.assertIn(task, listed)
        self.assertTrue(listed[task]['reason'].startswith(hint), listed[task]['reason'])
        self.assertEqual(201, self.approve(self.agent, task, cid).status)
        event = [e for e in self.events('reviews.add') if e.get('same_person_coordinator')][-1]
        self.assertEqual(basis, event['basis'])

    def test_an_agent_delivers_and_a_credential_is_assigned_the_hint_says_another_agent(self):
        task, cid = self.deliver(self.other)
        self.backend._task(self.project, task)['assignee'] = 'lane-c'
        self.check(task, cid, self.AGENT, 'agent')

    def test_a_credential_delivers_and_an_agent_is_assigned_the_hint_says_a_worker_credential(self):
        task, cid = self.deliver(self.lane_c)
        self.backend._task(self.project, task)['assignee'] = self.other_id
        self.check(task, cid, self.CREDENTIAL, 'worker-credential')

    def test_an_independent_author_and_a_credential_assignee_the_hint_says_a_worker_credential(self):
        task, cid = self.deliver(self.tokens['carl'])
        self.backend._task(self.project, task)['assignee'] = 'lane-c'
        self.check(task, cid, self.CREDENTIAL, 'worker-credential')

    def test_the_credential_helper_is_gone(self):
        self.assertFalse(hasattr(API, '_credential_work_in'))


if __name__ == '__main__':
    unittest.main()
