"""Nobody approves, or recommends, work of their own party (kittrial-5bb.199).

Slice 1a of docs/WEB_COORDINATOR_DESIGN.md. One party is an account, every agent that account
made, and every worker credential that account issued. The rule sits behind an installation
setting that is OFF by default: with it off, every answer here is what it was.

Through the real routes of the web service (the in-process backend).
"""
import secrets
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import test_http_agents
from http_auth import Service, Store

OWN_PARTY = ('This contribution was delivered by your own account (you, one of your agents, or a worker credential '
             'you issued). Another owner of this project, or a superuser who did not deliver it, must approve it.')


class Party(test_http_agents.AgentHarness):
    """Three accounts in one project: olive and oscar are owners, carl is a contributor; the admin is a superuser."""

    SETTING = True

    def setUp(self):
        super().setUp()
        self.service.approval_by_another_party = self.SETTING
        self.admin = self.admin_token()
        self.project = self.create_project(self.admin, 'Alpha')
        self.ids, self.tokens = {}, {}
        for name, role in (('olive', 'owner'), ('oscar', 'owner'), ('carl', 'contributor')):
            self.ids[name] = self.create_account(self.admin, name, name + '-password-1')
            self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, self.ids[name]),
                                               {'role': role}, token=self.admin).status)
            self.tokens[name] = self.login(name, name + '-password-1')[0]

    def base(self, task):
        return '/v1/projects/%s/tasks/%s' % (self.project, task)

    def agent_of(self, name, label):
        made = self.request('POST', '/v1/agents', {'name': label, 'working_directory': '/home/x/' + label,
                                                   'projects': [self.project]}, token=self.tokens[name])
        self.assertEqual(201, made.status, made.data)
        return made.data['credential']['secret']

    def worker_of(self, name, actor=None, token=None, **more):
        body = dict({'label': 'worker'}, **more)
        if actor:
            body['actor'] = actor
        made = self.request('POST', '/v1/projects/%s/worker-credentials' % self.project, body,
                            token=token or self.tokens[name])
        self.assertEqual(201, made.status, made.data)
        return made.data['credential']['secret'], made.data['credential']['id']

    def deliver(self, token, title=None, claim=None):
        """A task claimed and delivered with ``token``; returns (task, contribution id)."""
        task = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': title or secrets.token_hex(4)},
                            token=self.admin).data['id']
        self.assertEqual(200, self.request('POST', self.base(task) + '/claim', {}, token=claim or token).status)
        made = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'contribute', 'commit': test_http_agents.COMMIT, 'base_commit': test_http_agents.BASE,
            'bundle_sha256': test_http_agents.BUNDLE, 'summary': 'delivered'}, token=token)
        self.assertEqual(201, made.status, made.data)
        return task, made.data['contribution']['id']

    def approve(self, token, task, contribution):
        return self.request('POST', self.base(task) + '/reviews',
                            {'operation': 'approve', 'contribution': contribution, 'summary': 'accepted'}, token=token)

    def recommend(self, token, task, contribution):
        return self.request('POST', self.base(task) + '/reviews', {
            'operation': 'recommend', 'schema_version': 1, 'operation_id': 'rec-' + secrets.token_hex(6),
            'contribution': contribution, 'commit': test_http_agents.COMMIT, 'verdict': 'approve',
            'summary': 'Read the diff and ran the tests.', 'items': []}, token=token)

    def refused(self, answer):
        self.assertEqual(403, answer.status, answer.data)
        self.assertEqual(answer.data['error']['message'], OWN_PARTY)

    def state_of(self, task):
        return self.request('GET', self.base(task) + '/brief', token=self.admin).data['review']['state']


class SettingOnTests(Party):

    def test_an_owner_does_not_approve_their_own_contribution(self):
        task, contribution = self.deliver(self.tokens['olive'])
        self.refused(self.approve(self.tokens['olive'], task, contribution))
        self.assertEqual(self.state_of(task), 'awaiting-review')                    # nothing was written
        # Another owner does, and so does a superuser who did not deliver it.
        self.assertEqual(201, self.approve(self.tokens['oscar'], task, contribution).status)
        other, second = self.deliver(self.tokens['olive'])
        self.assertEqual(201, self.approve(self.admin, other, second).status)

    def test_an_owner_does_not_approve_their_own_agents_work(self):
        agent = self.agent_of('olive', 'Kestrel')
        task, contribution = self.deliver(agent)
        self.refused(self.approve(self.tokens['olive'], task, contribution))
        self.assertEqual(201, self.approve(self.tokens['oscar'], task, contribution).status)

    def test_work_under_a_worker_credential_belongs_to_the_account_that_issued_it(self):
        """The hole: such work was nobody's, so its issuer approved it and the issuer's agent recommended it."""
        for actor in ('lane-a', None):                      # a named credential, and one that writes as its issuer
            with self.subTest(actor=actor):
                worker, _ = self.worker_of('olive', actor)
                task, contribution = self.deliver(worker)
                self.refused(self.approve(self.tokens['olive'], task, contribution))
                own_agent = self.agent_of('olive', 'Merlin-' + str(actor))
                answer = self.recommend(own_agent, task, contribution)
                self.assertEqual(403, answer.status, answer.data)
                self.assertIn('must be independent of the author', answer.data['error']['message'])
                # Another account's agent recommends it and another owner approves it.
                self.assertEqual(201, self.recommend(self.agent_of('carl', 'Osprey-' + str(actor)), task, contribution).status)
                self.assertEqual(201, self.approve(self.tokens['oscar'], task, contribution).status)

    def test_a_name_inside_the_credentials_namespace_is_the_issuers_too(self):
        worker, _ = self.worker_of('olive', 'lane-a')
        task = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'sub'}, token=self.admin).data['id']
        self.assertEqual(200, self.request('POST', self.base(task) + '/claim', {'actor': 'lane-a/night'}, token=worker).status)
        made = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'contribute', 'commit': test_http_agents.COMMIT, 'base_commit': test_http_agents.BASE,
            'bundle_sha256': test_http_agents.BUNDLE, 'summary': 'delivered', 'actor': 'lane-a/night'}, token=worker)
        self.assertEqual(201, made.status, made.data)
        self.refused(self.approve(self.tokens['olive'], task, made.data['contribution']['id']))
        self.assertEqual(self.service.actor_parties('lane-a/night', self.project), {self.ids['olive']})
        self.assertEqual(self.service.actor_parties('lane-abc', self.project), {'lane-abc'})      # not inside the namespace

    def test_a_superuser_is_bound_for_their_own_partys_work_and_for_nobody_elses(self):
        task, contribution = self.deliver(self.admin)
        self.refused(self.approve(self.admin, task, contribution))
        admin_agent = self.request('POST', '/v1/agents', {'name': 'Root', 'working_directory': '/home/x/root',
                                                          'projects': [self.project]}, token=self.admin).data['credential']['secret']
        other, second = self.deliver(admin_agent)
        self.refused(self.approve(self.admin, other, second))
        self.assertEqual(201, self.approve(self.tokens['olive'], task, contribution).status)
        # Somebody else's work: a superuser approves it as before.
        third, by_carl = self.deliver(self.tokens['carl'])
        self.assertEqual(201, self.approve(self.admin, third, by_carl).status)

    def test_two_agents_of_one_account_and_agents_of_two_accounts(self):
        first, second = self.agent_of('carl', 'Kestrel'), self.agent_of('carl', 'Merlin')
        task, contribution = self.deliver(first)
        answer = self.recommend(second, task, contribution)                       # one party: refused, as before
        self.assertEqual(403, answer.status, answer.data)
        other = self.agent_of('olive', 'Osprey')                                  # another party: allowed
        self.assertEqual(201, self.recommend(other, task, contribution).status)
        # The account behind the recommending agent may then not be the one that delivered: olive approves carl's work.
        self.assertEqual(201, self.approve(self.tokens['olive'], task, contribution).status)

    def test_a_worker_credential_another_account_issued_is_another_party(self):
        worker, _ = self.worker_of('oscar', 'lane-b')
        task, contribution = self.deliver(worker)
        self.assertEqual(201, self.approve(self.tokens['olive'], task, contribution).status)
        self.assertEqual(self.service.actor_parties('lane-b', self.project), {self.ids['oscar']})

    def test_the_assignee_counts_as_well_as_the_author(self):
        """Carl delivered; the task was then given to olive's agent (a reassignment, which the host route can
        make). The author is another party and the assignee is hers: she does not approve it."""
        task, contribution = self.deliver(self.tokens['carl'])
        agent = self.agent_of('olive', 'Kestrel')
        agent_id = self.request('GET', '/v1/agents/me', token=agent).data['agent']['id']
        self.backend._task(self.project, task)['assignee'] = agent_id
        brief = self.request('GET', self.base(task) + '/brief', token=self.admin).data
        self.assertEqual((brief['task']['assignee'], brief['review']['contribution']['author']), (agent_id, self.ids['carl']))
        self.refused(self.approve(self.tokens['olive'], task, contribution))
        self.assertEqual(201, self.approve(self.tokens['oscar'], task, contribution).status)

    def test_a_label_under_an_agents_name_is_the_agents_account(self):
        """An agent may write under AGENT/label. That is a different name and the same party."""
        agent = self.agent_of('carl', 'Kestrel')
        agent_id = self.request('GET', '/v1/agents/me', token=agent).data['agent']['id']
        label = agent_id + '/night'
        self.assertEqual(self.service.actor_parties(label, self.project), {self.ids['carl']})
        self.assertEqual(self.service.actor_parties(agent_id + 'x', self.project), {agent_id + 'x'})     # not inside the name
        task = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'labelled'}, token=self.admin).data['id']
        self.assertEqual(200, self.request('POST', self.base(task) + '/claim', {'actor': label}, token=agent).status)
        made = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'contribute', 'commit': test_http_agents.COMMIT, 'base_commit': test_http_agents.BASE,
            'bundle_sha256': test_http_agents.BUNDLE, 'summary': 'delivered', 'actor': label}, token=agent)
        self.assertEqual(201, made.status, made.data)
        other = self.agent_of('carl', 'Merlin')
        self.assertEqual(403, self.recommend(other, task, made.data['contribution']['id']).status)
        self.assertEqual(201, self.recommend(self.agent_of('olive', 'Osprey'), task, made.data['contribution']['id']).status)

    def test_a_name_two_issuers_have_held_is_decided_by_the_moment_or_counts_for_both(self):
        """kittrial-5bb.188 lets a superuser waive a name's reuse: the same name under two issuers, one after the other."""
        with self.service.store.lock:
            credentials = self.service.state['credentials']
            credentials['cred_old'] = {'id': 'cred_old', 'user_id': self.ids['olive'], 'project_id': self.project,
                                       'agent_id': None, 'label': 'old', 'scopes': ['tasks'], 'actor': 'lane-x',
                                       'token_hash': 'x', 'created_at': '2026-01-01T00:00:00Z', 'issued_raw': 0,
                                       'last_used': None, 'expires_at': 1, 'revoked': True,
                                       'revoked_at': '2026-02-01T00:00:00Z'}
            credentials['cred_new'] = dict(credentials['cred_old'], id='cred_new', user_id=self.ids['oscar'], token_hash='y',
                                           created_at='2026-03-01T00:00:00Z', revoked=False, expires_at=2 * 10 ** 9)
            credentials['cred_new'].pop('revoked_at')
        parties = self.service.actor_parties
        both = {self.ids['olive'], self.ids['oscar']}
        self.assertEqual(parties('lane-x', self.project, '2026-01-15T00:00:00Z'), {self.ids['olive']})
        self.assertEqual(parties('lane-x', self.project, '2026-03-15T00:00:00Z'), {self.ids['oscar']})
        self.assertEqual(parties('lane-x', self.project), both)                   # no moment: both, which only refuses more
        self.assertEqual(parties('lane-x', self.project, '2026-02-15T00:00:00Z'), both)      # between the two lives: both
        self.assertEqual(parties('lane-x', 'another-project'), {'lane-x'})        # a name is held in a project

    def test_the_queue_and_the_brief_count_by_the_same_parties(self):
        """A recommendation from the issuer's own agent on work under the issuer's worker credential is not shown as one."""
        worker, _ = self.worker_of('olive', 'lane-a')
        task, contribution = self.deliver(worker)
        own_agent = self.agent_of('olive', 'Merlin')
        agent_id = self.request('GET', '/v1/agents/me', token=own_agent).data['agent']['id']
        # As a recommendation written before the rule was on would stand in the records.
        self.service.approval_by_another_party = False
        self.assertEqual(201, self.recommend(own_agent, task, contribution).status)
        brief = self.request('GET', self.base(task) + '/brief', token=self.tokens['oscar']).data['review']
        self.assertEqual([entry['author'] for entry in brief['recommendations']], [agent_id])
        self.service.approval_by_another_party = True
        brief = self.request('GET', self.base(task) + '/brief', token=self.tokens['oscar']).data['review']
        self.assertEqual((brief['recommendations'], brief['recommendation']), ([], None))
        row = [item for item in self.request('GET', '/v1/projects/%s/queue' % self.project, token=self.tokens['oscar']).data['items']
               if item['id'] == task][0]
        self.assertEqual(row.get('recommended_by') or [], [])

    def test_a_recommendation_under_a_worker_credential_is_its_issuers(self):
        """The other side of the comparison: the name that RECOMMENDED is traced too. olive's agent delivered;
        a worker credential olive issued recommended it (allowed while the rule was off). It does not count."""
        task, contribution = self.deliver(self.agent_of('olive', 'Kestrel'))
        worker, _ = self.worker_of('olive', 'lane-a')
        # Written under the credential's name as over the host route, where only names are compared: the web
        # route refuses it already, by the account behind the request.
        records = self.backend.state['contributions'][task]
        self.backend.state.setdefault('recommendations', {}).setdefault(task, []).append({
            'id': 'rec_planted', 'task_id': task, 'kind': 'recommendation', 'contribution_id': contribution,
            'commit': test_http_agents.COMMIT, 'verdict': 'approve', 'summary': 'planted', 'items': [], 'actor': 'lane-a',
            'created_at': '2099-01-01T00:00:00Z', 'after': len(records)})
        self.service.approval_by_another_party = False
        brief = self.request('GET', self.base(task) + '/brief', token=self.tokens['oscar']).data['review']
        self.assertEqual([entry['author'] for entry in brief['recommendations']], ['lane-a'])        # off: it counts, as today
        self.service.approval_by_another_party = True
        brief = self.request('GET', self.base(task) + '/brief', token=self.tokens['oscar']).data['review']
        self.assertEqual((brief['recommendations'], brief['recommendation']), ([], None))
        row = [item for item in self.request('GET', '/v1/projects/%s/queue' % self.project, token=self.tokens['oscar']).data['items']
               if item['id'] == task][0]
        self.assertEqual(row.get('recommended_by') or [], [])

    def test_a_name_is_traced_in_the_project_of_the_work(self):
        """`lane-a` is olive's in Alpha and oscar's in Beta. Work under it in Alpha is olive's alone: oscar's agent
        recommends it and its recommendation is shown, and oscar approves it."""
        beta = self.create_project(self.admin, 'Beta')
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s' % (beta, self.ids['oscar']), {'role': 'owner'},
                                           token=self.admin).status)
        made = self.request('POST', '/v1/projects/%s/worker-credentials' % beta, {'label': 'worker', 'actor': 'lane-a'},
                            token=self.tokens['oscar'])
        self.assertEqual(201, made.status, made.data)
        worker, _ = self.worker_of('olive', 'lane-a')
        task, contribution = self.deliver(worker)
        self.assertEqual(self.service.actor_parties('lane-a', self.project), {self.ids['olive']})
        self.assertEqual(self.service.actor_parties('lane-a', beta), {self.ids['oscar']})
        agent = self.agent_of('oscar', 'Osprey')
        agent_id = self.request('GET', '/v1/agents/me', token=agent).data['agent']['id']
        self.assertEqual(201, self.recommend(agent, task, contribution).status)
        brief = self.request('GET', self.base(task) + '/brief', token=self.tokens['carl']).data['review']
        self.assertEqual([entry['author'] for entry in brief['recommendations']], [agent_id])
        row = [item for item in self.request('GET', '/v1/projects/%s/queue' % self.project, token=self.tokens['carl']).data['items']
               if item['id'] == task][0]
        self.assertEqual(row.get('recommended_by'), [agent_id])
        self.assertEqual(201, self.approve(self.tokens['oscar'], task, contribution).status)


class SettingOffTests(Party):
    """An installation that configures nothing: every answer is today's."""

    SETTING = False

    def test_the_default_is_off(self):
        self.assertIs(Service(Store(self.tmp_path / 'other.json')).approval_by_another_party, False)
        self.assertIs(Service(Store(self.tmp_path / 'third.json'), approval_by_another_party=True).approval_by_another_party, True)
        self.assertIs(Service(Store(self.tmp_path / 'fourth.json'), approval_by_another_party='yes').approval_by_another_party, False)

    def test_an_owner_approves_their_own_work_their_agents_and_their_worker_credentials_as_today(self):
        task, contribution = self.deliver(self.tokens['olive'])
        self.assertEqual(201, self.approve(self.tokens['olive'], task, contribution).status)
        task, contribution = self.deliver(self.agent_of('olive', 'Kestrel'))
        self.assertEqual(201, self.approve(self.tokens['olive'], task, contribution).status)
        worker, _ = self.worker_of('olive', 'lane-a')
        task, contribution = self.deliver(worker)
        # Today's hole, kept exactly while the setting is off: the issuer's agent recommends, the issuer approves.
        self.assertEqual(201, self.recommend(self.agent_of('olive', 'Merlin'), task, contribution).status)
        self.assertEqual(201, self.approve(self.tokens['olive'], task, contribution).status)
        task, contribution = self.deliver(self.admin)
        self.assertEqual(201, self.approve(self.admin, task, contribution).status)

    def test_the_person_rule_for_a_recommendation_is_as_it_was(self):
        first, second = self.agent_of('carl', 'Kestrel'), self.agent_of('carl', 'Merlin')
        task, contribution = self.deliver(first)
        self.assertEqual(403, self.recommend(second, task, contribution).status)
        self.assertEqual(403, self.recommend(self.tokens['carl'], task, contribution).status)
        self.assertEqual(201, self.recommend(self.agent_of('olive', 'Osprey'), task, contribution).status)

    def test_a_party_is_the_person_and_nothing_is_read_for_an_approval(self):
        self.worker_of('olive', 'lane-a')
        self.assertEqual(self.service.actor_parties('lane-a', self.project), {'lane-a'})
        self.assertEqual(self.service.actor_parties(self.ids['olive'], self.project), {self.ids['olive']})
        task, contribution = self.deliver(self.tokens['olive'])
        briefs = []
        real = self.backend.task_brief
        self.backend.task_brief = lambda *args: briefs.append(args) or real(*args)
        self.assertEqual(201, self.approve(self.tokens['oscar'], task, contribution).status)
        self.assertEqual(briefs, [])                                              # the rule's read is made only when it is on


class SaidOnTheSetUpPageTests(Party):

    def test_the_set_up_answer_says_whether_the_rule_is_on(self):
        for setting in (True, False):
            self.service.approval_by_another_party = setting
            answer = self.request('GET', '/v1/projects/%s/setup' % self.project, token=self.tokens['olive'])
            self.assertEqual(200, answer.status, answer.data)
            self.assertEqual(answer.data['rules'], {'approval_by_another_party': setting})

    def test_the_page_has_a_line_for_it(self):
        page = (KIT / 'web' / 'js' / 'views' / 'setup.js').read_text(encoding='utf-8')
        self.assertIn("      rulesNote(data.rules));\n", page)
        self.assertIn("if (!rules || typeof rules.approval_by_another_party !== 'boolean') return null;", page)
        self.assertIn('On this server nobody approves or recommends work of their own account', page)
        self.assertIn('On this server an owner may approve work of their own account', page)


class WiringTests(unittest.TestCase):

    def test_the_service_is_started_with_the_setting_only_when_the_office_configuration_says_true(self):
        import inspect
        source = inspect.getsource(http_service.main)
        self.assertIn("approval_by_another_party=args.approval_by_another_party", source)
        self.assertIn("'--approval-by-another-party', action='store_true'", source)
        office = (KIT / 'office_service.py').read_text(encoding='utf-8')
        self.assertIn("if settings.get('approval_by_another_party') is True:\n        command.append('--approval-by-another-party')", office)
        self.assertIn("raise ValueError('approval_by_another_party must be true or false')", office)


if __name__ == '__main__':
    unittest.main()
