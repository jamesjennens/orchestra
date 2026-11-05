"""kittrial-5bb.518: an agent is told, on the reads it makes, whether it may approve a delivery and why.

``approval_permission`` is informational and grants nothing: it is worked out by the functions the approve route
calls, so the two cannot disagree. Who may approve is UNCHANGED by this task: the matrix below stores the answers
of c01d067 (the approve POST of a granted coordinator agent, five cases x three settings x the installation's
setting on and off) as literal expectations, and checks in every cell that ``may_approve`` equals the POST.

Through the real routes of the web service (the in-process backend); one real-bd path is in
tests/test_coordinator_grant_real.py.
"""
import sys
import unittest
import unittest.mock
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import approval_matrix
import http_service
import work

API = http_service.ApiHandler

#: The approve POST of the granted coordinator agent on c01d067 (recorded by running tests/approval_matrix.py on a
#: detached c01d067 worktree), as ``case|setting|installation setting`` -> (status, message of a refusal).
BASE_STRICT = ("This contribution was delivered by this agent's own party: its account, one of that account's agents, "
               "or a name that a worker credential of that account holds or has held. An agent never approves work of "
               "its own party, whatever this server's setting for people says: somebody of another party who may "
               "approve here must approve it (another coordinator or owner of this project, or a superuser).")
BASE_BY_AGENT_NEXT = (" On this project a coordinator agent may approve the work of another agent of the same person "
                      "(agent approvals: by agent), but never its own work, its person's own work, or work under a "
                      "worker credential of that person: somebody of another party who may approve here must approve "
                      "it (another coordinator or owner of this project, or a superuser).")
BASE_PERSONS = ("This contribution was delivered by this agent's own party: its account, one of that account's agents, "
                "or a name that a worker credential of that account holds or has held." + BASE_BY_AGENT_NEXT)
BASE_ITSELF = "This contribution was delivered by this agent itself." + BASE_BY_AGENT_NEXT
BASE_RESULTS = {
    'another-agent|by-agent|off': (201, None),
    'another-agent|by-agent|on': (201, None),
    'another-agent|by-person|off': (403, BASE_STRICT),
    'another-agent|by-person|on': (403, BASE_STRICT),
    'another-agent|damaged|off': (403, BASE_STRICT),
    'another-agent|damaged|on': (403, BASE_STRICT),
    'independent|by-agent|off': (201, None),
    'independent|by-agent|on': (201, None),
    'independent|by-person|off': (201, None),
    'independent|by-person|on': (201, None),
    'independent|damaged|off': (201, None),
    'independent|damaged|on': (201, None),
    'own-person|by-agent|off': (403, BASE_PERSONS),
    'own-person|by-agent|on': (403, BASE_PERSONS),
    'own-person|by-person|off': (403, BASE_STRICT),
    'own-person|by-person|on': (403, BASE_STRICT),
    'own-person|damaged|off': (403, BASE_STRICT),
    'own-person|damaged|on': (403, BASE_STRICT),
    'own-worker-credential|by-agent|off': (403, BASE_PERSONS),
    'own-worker-credential|by-agent|on': (403, BASE_PERSONS),
    'own-worker-credential|by-person|off': (403, BASE_STRICT),
    'own-worker-credential|by-person|on': (403, BASE_STRICT),
    'own-worker-credential|damaged|off': (403, BASE_STRICT),
    'own-worker-credential|damaged|on': (403, BASE_STRICT),
    'own-work|by-agent|off': (403, BASE_ITSELF),
    'own-work|by-agent|on': (403, BASE_ITSELF),
    'own-work|by-person|off': (403, BASE_STRICT),
    'own-work|by-person|on': (403, BASE_STRICT),
    'own-work|damaged|off': (403, BASE_STRICT),
    'own-work|damaged|on': (403, BASE_STRICT),
}


class Matrix(approval_matrix.MatrixFixture):

    def brief(self, task, token=None):
        return self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (self.project, task),
                            token=token or self.agent).data['review']

    def queue_row(self, task, token=None):
        items = self.request('GET', '/v1/projects/%s/queue' % self.project, token=token or self.agent).data['items']
        return next(item for item in items if item['id'] == task)

    def test_who_may_approve_is_what_it_was_at_c01d067_and_may_approve_equals_the_post(self):
        seen = 0
        for case, mode, installation, task, contribution in self.cells():
            key = '%s|%s|%s' % (case, mode, 'on' if installation else 'off')
            with self.subTest(cell=key):
                permission = self.brief(task)['approval_permission']
                row = self.queue_row(task)['approval_permission']
                posted = self.approve(self.agent, task, contribution)
                status, message = BASE_RESULTS[key]
                # Who may approve: the same answer as c01d067's.
                self.assertEqual(status, posted.status, posted.data)
                if status == 403:
                    sentence = posted.data['error']['message']
                    if case == 'own-worker-credential':
                        # The same refusal, now naming its case (kittrial-5bb.518 condition 6).
                        self.assertEqual(API.OWN_WORKER_CREDENTIAL % 'lane-c', sentence)
                    else:
                        self.assertEqual(message, sentence)
                # may_approve is the POST, in the brief and in the queue row; and the case is the cell's.
                self.assertEqual(status == 201, permission['may_approve'])
                self.assertEqual(permission, row)
                self.assertEqual(case, permission['case'])
                self.assertEqual(mode, permission['setting']['mode'] if mode != 'damaged' else 'damaged')
                if mode == 'damaged':
                    self.assertEqual(('by-person', True), (permission['setting']['mode'], permission['setting']['damaged']))
                if status == 403 and case != 'another-agent':
                    self.assertEqual(sentence, permission['reason'])      # the refusal's own words
                seen += 1
        self.assertEqual(30, seen)
        self.assertEqual(30, len(BASE_RESULTS))


class Reads(approval_matrix.MatrixFixture):
    """What each read says, with no approve tried."""

    def put_mode(self, mode):
        self.set_mode(mode)

    def brief(self, task, token=None):
        return self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (self.project, task),
                            token=token or self.agent).data['review']

    def next_reason(self, task):
        data = self.request('GET', '/v1/agents/me/next', token=self.agent).data
        return [a['reason'] for a in data['next_actions'] if a['task'] == task and a['kind'] == 'to-review']

    def agent_prompt(self):
        found = self.request('GET', '/v1/me/work', token=self.tokens['cora']).data
        return next(p for p in found['agent_prompts'] if p['agent_id'] == self.agent_id)['text']

    def test_by_agent_a_sibling_deliverys_brief_says_you_may_approve_and_no_wait(self):
        self.service.approval_by_another_party = True            # the case that told the agent to wait
        task, _ = self.deliver(self.other)
        review = self.brief(task)
        self.assertNotEqual(work.APPROVAL_WAIT, review['waiting_for_approval'])
        permission = review['approval_permission']
        self.assertEqual((True, 'another-agent'), (permission['may_approve'], permission['case']))
        self.assertEqual("You may approve this: the project's setting Approvals by coordinator agents is by-agent "
                         "(the default; nobody has set it), so a coordinator agent may approve another agent of its "
                         "own person.", permission['reason'])
        self.assertEqual({'mode': 'by-agent', 'set_by': None, 'set_by_display_name': None, 'set_at': None,
                          'damaged': False}, permission['setting'])

    def test_the_sentence_names_who_set_the_setting_and_when(self):
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/agent-approval' % self.project,
                                           {'agent_approval': 'by-agent'}, token=self.tokens['olive']).status)
        task, _ = self.deliver(self.other)
        permission = self.brief(task)['approval_permission']
        setting = permission['setting']
        self.assertEqual(self.ids['olive'], setting['set_by'])
        self.assertTrue(setting['set_at'])
        self.assertIn('(set by %s at %s)' % (setting['set_by_display_name'] or setting['set_by'], setting['set_at']),
                      permission['reason'])

    def test_by_person_the_brief_says_another_party_must_approve(self):
        self.service.approval_by_another_party = True
        self.set_mode('by-person')
        task, _ = self.deliver(self.other)
        review = self.brief(task)
        self.assertEqual(work.APPROVAL_WAIT, review['waiting_for_approval'])
        permission = review['approval_permission']
        self.assertEqual((False, 'another-agent'), (permission['may_approve'], permission['case']))
        self.assertTrue(permission['reason'].startswith(
            "Another party must approve this: the project's setting Approvals by coordinator agents is by-person ("))
        self.assertEqual([], self.next_reason(task))               # and it is not offered

    def test_a_damaged_stored_value_reads_as_by_person_with_damaged_true(self):
        self.set_mode('damaged')
        task, _ = self.deliver(self.other)
        permission = self.brief(task)['approval_permission']
        self.assertEqual((False, 'by-person', True), (permission['may_approve'], permission['setting']['mode'],
                                                       permission['setting']['damaged']))
        self.assertIn('the stored value is damaged, so it is read as by-person', permission['reason'])

    def test_its_own_delivery_is_never_approvable_under_either_setting(self):
        for mode in ('by-agent', 'by-person'):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                task, _ = self.deliver(self.agent)
                permission = self.brief(task)['approval_permission']
                self.assertEqual((False, 'own-work'), (permission['may_approve'], permission['case']))
                # c01d067's own sentences, unchanged: by-agent names the agent itself, by-person the party.
                self.assertTrue(permission['reason'].startswith(
                    API.SAME_AGENT[0] if mode == 'by-agent' else API.OWN_PARTY_AGENT[0]), permission['reason'])

    def test_the_worker_credential_case_is_named_plainly(self):
        for mode in ('by-agent', 'by-person'):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                task, contribution = self.deliver(self.worker)
                permission = self.brief(task)['approval_permission']
                expected = ('This was delivered under lane-c, a worker credential of your own person. A coordinator '
                            'agent never approves that, whatever the agent-approval setting says: another party must '
                            'approve it.')
                self.assertEqual((False, 'own-worker-credential', expected),
                                 (permission['may_approve'], permission['case'], permission['reason']))
                refused = self.approve(self.agent, task, contribution)
                self.assertEqual((403, expected), (refused.status, refused.data['error']['message']))

    def test_the_other_cases_have_their_sentences(self):
        task, _ = self.deliver(self.tokens['carl'])
        permission = self.brief(task)['approval_permission']
        self.assertEqual((True, 'independent', 'You may approve this: it was delivered by another party.'),
                         (permission['may_approve'], permission['case'], permission['reason']))
        task, _ = self.deliver(self.tokens['cora'])
        permission = self.brief(task)['approval_permission']
        self.assertEqual((False, 'own-person'), (permission['may_approve'], permission['case']))
        self.assertTrue(permission['reason'].startswith(API.OWN_PARTY_AGENT[0]))

    def test_a_person_coordinator_under_the_installations_setting_still_sees_the_wait(self):
        """Condition 3: the brief fix hides nothing from a PERSON, whatever the project's agent setting says."""
        self.service.approval_by_another_party = True
        self.assertEqual(200, self.member('cora', 'coordinator', token=self.admin).status)         # a PERSON who is a coordinator
        for mode in ('by-agent', 'by-person'):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                for deliverer, case, waits in ((self.other, 'own-person', True), (self.tokens['cora'], 'own-person', True),
                                               (self.tokens['carl'], 'independent', False)):
                    task, contribution = self.deliver(deliverer)
                    review = self.brief(task, token=self.tokens['cora'])
                    self.assertEqual(waits, review['waiting_for_approval'] == work.APPROVAL_WAIT, (case, review))
                    permission = review['approval_permission']
                    self.assertEqual((not waits, case), (permission['may_approve'], permission['case']))
                    posted = self.approve(self.tokens['cora'], task, contribution)
                    self.assertEqual(201 if not waits else 403, posted.status, posted.data)

    def test_the_queue_shows_the_permission_before_any_attempt(self):
        task, _ = self.deliver(self.other)
        listed = next(item for item in self.request('GET', '/v1/projects/%s/queue' % self.project,
                                                    token=self.agent).data['items'] if item['id'] == task)
        self.assertEqual((True, 'another-agent', 'by-agent'),
                         (listed['approval_permission']['may_approve'], listed['approval_permission']['case'],
                          listed['approval_permission']['setting']['mode']))
        self.assertEqual('awaiting-review', self.brief(task)['state'])          # nothing was tried
        self.set_mode('by-person')
        listed = next(item for item in self.request('GET', '/v1/projects/%s/queue' % self.project,
                                                    token=self.agent).data['items'] if item['id'] == task)
        self.assertFalse(listed['approval_permission']['may_approve'])

    def test_a_queue_of_200_awaiting_rows_reads_the_setting_once(self):
        self.deliver(self.other)
        rows = [{'id': 'task-%03d' % n, 'title': 'row %d' % n, 'status': 'open', 'review_state': 'awaiting-review',
                 'assignee': self.other_id, 'contribution_author': self.other_id, 'priority': 2} for n in range(200)]

        def counted(rows, path):
            with unittest.mock.patch.object(http_service.ApiHandler, '_review_queue',
                                            return_value={'items': rows, 'complete': True}):
                with unittest.mock.patch.object(self.service, 'agent_approval', wraps=self.service.agent_approval) as setting, \
                        unittest.mock.patch.object(self.service, 'capabilities_for',
                                                   wraps=self.service.capabilities_for) as caps:
                    answer = self.request('GET', path, token=self.agent)
            self.assertEqual(200, answer.status, answer.data)
            return answer.data['items'], setting.call_count, caps.call_count
        queue = '/v1/projects/%s/queue?limit=100' % self.project
        few, setting_few, caps_few = counted(rows[:2], queue)
        many, setting_many, caps_many = counted(rows, queue)
        self.assertEqual((2, 100), (len(few), len(many)))
        self.assertTrue(all(item['approval_permission']['may_approve'] for item in many))
        # One read of the setting for the request, and the same reads for 2 rows as for 200: none per row.
        self.assertEqual(1, setting_few)
        self.assertEqual((setting_few, caps_few), (setting_many, caps_many))
        # The second page of the 200 reads it once too.
        with unittest.mock.patch.object(http_service.ApiHandler, '_review_queue',
                                        return_value={'items': rows, 'complete': True}):
            first = self.request('GET', queue, token=self.agent).data
            self.assertTrue(first['next_cursor'])
            with unittest.mock.patch.object(self.service, 'agent_approval', wraps=self.service.agent_approval) as setting:
                second = self.request('GET', queue + '&cursor=' + first['next_cursor'], token=self.agent).data
        self.assertEqual((100, 1), (len(second['items']), setting.call_count))
        self.assertEqual(200, len({item['id'] for item in first['items'] + second['items']}))

    def test_my_work_with_200_awaiting_rows_reads_the_setting_as_often_as_with_2(self):
        self.deliver(self.tokens['carl'])

        def counted(count):
            rows = [{'id': 'task-%03d' % n, 'title': 'row %d' % n, 'status': 'open', 'review_state': 'awaiting-review',
                     'assignee': self.ids['carl'], 'contribution_author': self.ids['carl'], 'priority': 2}
                    for n in range(count)]
            with unittest.mock.patch.object(http_service.ApiHandler, '_review_queue',
                                            return_value={'items': rows, 'complete': True}):
                with unittest.mock.patch.object(self.service, 'agent_approval',
                                                wraps=self.service.agent_approval) as setting:
                    answer = self.request('GET', '/v1/me/work', token=self.tokens['cora'])
            self.assertEqual(200, answer.status, answer.data)
            return answer.data['to_review'], setting.call_count
        few, reads_few = counted(2)
        many, reads_many = counted(200)
        self.assertEqual((2, 100), (len(few), len(many)))              # the page is cut at 100
        self.assertTrue(all(row['approval_permission']['may_approve'] for row in many))
        self.assertEqual(reads_few, reads_many)

    def test_next_actions_name_the_setting_and_the_case(self):
        task, _ = self.deliver(self.other)
        reason = self.next_reason(task)[0]
        self.assertTrue(reason.startswith('A contribution by another agent of your person awaits review.'))
        self.assertTrue(reason.endswith('(agent_approval: by-agent; case: another-agent)'))
        self.assertIn("so a coordinator agent may approve another agent of its own person", reason)
        indep, _ = self.deliver(self.tokens['carl'])
        reason = self.next_reason(indep)[0]
        self.assertTrue(reason.endswith('(agent_approval: by-agent; case: independent)'))

    def test_my_works_prompt_for_the_agent_uses_the_agents_own_permission(self):
        task, _ = self.deliver(self.other)
        own, _ = self.deliver(self.agent)
        text = self.agent_prompt()
        line = next(l for l in text.split('\n') if task in l)
        self.assertIn('; you may approve this (another-agent)', line)
        self.assertIn('; you may NOT approve this (own-work): another party must', next(l for l in text.split('\n') if own in l))
        self.set_mode('by-person')
        line = next(l for l in self.agent_prompt().split('\n') if task in l)
        self.assertIn('; you may NOT approve this (another-agent): another party must', line)

    def test_rule_five_of_the_prompt_says_the_grant_and_the_setting_decide(self):
        self.deliver(self.other)
        text = self.agent_prompt()
        self.assertIn("Approvals and integration stay your owner's decision unless your coordinator grant and the "
                      "project's agent-approval setting allow them; the task brief's approval_permission says which, "
                      "for each delivery.", text)
        self.assertNotIn("unless they tell you otherwise", text)

    def test_my_work_rows_of_a_person_carry_the_persons_permission(self):
        self.service.approval_by_another_party = False
        task, _ = self.deliver(self.tokens['carl'])
        rows = self.request('GET', '/v1/me/work', token=self.tokens['cora']).data['to_review']
        row = next(r for r in rows if r['id'] == task)
        self.assertEqual((True, 'independent'), (row['approval_permission']['may_approve'], row['approval_permission']['case']))

    def test_me_answers_the_agent_md_version_and_note(self):
        found = self.request('GET', '/v1/agents/me', token=self.agent).data['agent_md']
        self.assertEqual({'version': 2, 'note': (
            "If your .orchestra/AGENT.md has no version line or an older one, it predates current guidance. The "
            "server's reads are authoritative over it: the brief's approval_permission decides whether you may "
            "approve. Ask your owner to re-run set-up from My agents to refresh the file.")}, found)


if __name__ == '__main__':
    unittest.main()
