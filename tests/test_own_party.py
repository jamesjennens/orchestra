"""Nobody approves, or recommends, work of their own party (kittrial-5bb.199).

Slice 1a of docs/WEB_COORDINATOR_DESIGN.md. One party is an account, every agent that account
made, and every worker credential that account issued. The rule sits behind an installation
setting that is OFF by default: with it off, every answer here is what it was.

Through the real routes of the web service (the in-process backend).
"""
import json
import os
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

API = http_service.ApiHandler
OWN_PARTY = API.OWN_PARTY + API.OWN_PARTY_NEXT


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

    def refused(self, answer, sentence=OWN_PARTY):
        self.assertEqual(403, answer.status, answer.data)
        self.assertEqual(answer.data['error']['message'], sentence)

    def issue(self, name, actor, project=None):
        return self.request('POST', '/v1/projects/%s/worker-credentials' % (project or self.project),
                            {'label': 'worker', 'actor': actor}, token=self.tokens[name])

    def revoke(self, name, credential_id):
        return self.request('POST', '/v1/projects/%s/worker-credentials/%s/revoke' % (self.project, credential_id), {},
                            token=self.tokens[name])

    def deliver_as(self, token, actor):
        """A task claimed and delivered with ``token`` under the name ``actor``."""
        task = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': secrets.token_hex(4)},
                            token=self.admin).data['id']
        self.assertEqual(200, self.request('POST', self.base(task) + '/claim', {'actor': actor}, token=token).status)
        made = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'contribute', 'commit': test_http_agents.COMMIT, 'base_commit': test_http_agents.BASE,
            'bundle_sha256': test_http_agents.BUNDLE, 'summary': 'delivered', 'actor': actor}, token=token)
        self.assertEqual(201, made.status, made.data)
        return task, made.data['contribution']['id']

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
                self.assertEqual(answer.data['error']['message'], API.NOT_INDEPENDENT_PARTY)
                self.assertIn('the account that issued the worker credential it was delivered under', API.NOT_INDEPENDENT_PARTY)
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
        self.refused(self.approve(self.tokens['olive'], task, contribution), API.OWN_PARTY_ASSIGNEE + API.OWN_PARTY_NEXT)
        self.assertIn('Another account delivered the contribution', API.OWN_PARTY_ASSIGNEE)
        self.assertEqual(201, self.approve(self.tokens['oscar'], task, contribution).status)

    def test_the_author_counts_when_the_task_is_another_partys(self):
        """The other way round: olive delivered and the task was then given to carl. The assignee is
        another party and the author is hers."""
        task, contribution = self.deliver(self.tokens['olive'])
        self.backend._task(self.project, task)['assignee'] = self.ids['carl']
        brief = self.request('GET', self.base(task) + '/brief', token=self.admin).data
        self.assertEqual((brief['task']['assignee'], brief['review']['contribution']['author']), (self.ids['carl'], self.ids['olive']))
        self.refused(self.approve(self.tokens['olive'], task, contribution))
        self.assertEqual(self.state_of(task), 'awaiting-review')
        self.assertEqual(201, self.approve(self.tokens['oscar'], task, contribution).status)

    def test_a_label_under_an_account_id_is_that_account(self):
        """The review's first finding: a worker credential issued with NO name writes under its issuer's
        account id and under any label below it. ACCOUNT/x was no account and under no credential's
        name, so it was nobody's party and its issuer approved it every time."""
        accounts = self.request('GET', '/v1/accounts', token=self.admin).data['items']
        self.ids['admin'] = [account['id'] for account in accounts if account.get('superuser')][0]
        for name, token, other in (('olive', self.tokens['olive'], self.tokens['oscar']), ('admin', self.admin, self.tokens['olive'])):
            with self.subTest(issuer=name):
                made = self.request('POST', '/v1/projects/%s/worker-credentials' % self.project, {'label': 'unnamed'}, token=token)
                self.assertEqual((201, None), (made.status, made.data['credential']['actor']), made.data)
                account = self.ids[name]
                label = account + '/x'
                self.assertEqual(self.service.actor_parties(label, self.project), {account})
                self.assertEqual(self.service.actor_parties(label, None), {account})          # an account is one everywhere
                task, contribution = self.deliver_as(made.data['credential']['secret'], label)
                brief = self.request('GET', self.base(task) + '/brief', token=self.admin).data
                self.assertEqual(brief['review']['contribution']['author'], label)
                if name == 'olive':
                    self.assertEqual(403, self.recommend(self.agent_of('olive', 'Merlin'), task, contribution).status)
                self.refused(self.approve(token, task, contribution))
                self.assertEqual(self.state_of(task), 'awaiting-review')
                self.assertEqual(201, self.approve(other, task, contribution).status)

    def test_each_name_of_the_approver_is_compared(self):
        """The name a request acts under and the account behind it are both the approver. For a signed-in
        person they are one name; an agent's are two (slice 1b), so the rule is shown on the rule itself."""
        handler = API.__new__(API)
        handler.service = self.service
        olive, oscar, carl = self.ids['olive'], self.ids['oscar'], self.ids['carl']
        said = handler._own_party_refusal
        self.assertIsNone(said(['somebody-else', oscar], carl, carl, self.project))
        self.assertEqual(said(['somebody-else', olive], carl, olive, self.project), OWN_PARTY)        # the account behind it
        self.assertEqual(said([olive, 'somebody-else'], carl, olive, self.project), OWN_PARTY)        # the name it acts under
        self.assertEqual(said(['somebody-else', olive], olive, carl, self.project), API.OWN_PARTY_ASSIGNEE + API.OWN_PARTY_NEXT)
        self.assertEqual(said([olive, None], None, olive, self.project), OWN_PARTY)                   # no assignee, no second name
        self.assertIsNone(said([olive, olive], None, None, self.project))                             # nobody to compare with

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

    def test_a_name_has_one_issuer_at_a_time(self):
        """The review's second finding, first half: while oscar's credential was unused, olive was issued the
        same name. A name another account's WORKING credential holds is refused, used or not, in both
        directions of holding; the same account may issue it again (that is how it replaces a credential)."""
        held = self.issue('oscar', 'squat')
        self.assertEqual(201, held.status, held.data)
        for name in ('squat', 'squat/night'):
            with self.subTest(asked=name):
                refused = self.issue('olive', name)
                self.assertEqual(409, refused.status, refused.data)
                self.assertEqual(refused.data['error']['message'],
                                 'The name %s is held by a working credential of this project that another account issued '
                                 '(squat, issued by oscar). A name has one issuer at a time: have that credential revoked '
                                 'first, or choose another name' % name)
                self.assertEqual(refused.data['error']['detail'], {'held_by_credential': held.data['credential']['id']})
        self.assertEqual(201, self.issue('oscar', 'deep/lane/one').status)
        self.assertEqual(409, self.issue('olive', 'deep').status)                 # it would hold oscar's name
        self.assertEqual(409, self.issue('olive', 'deep/lane').status)
        self.assertEqual(201, self.issue('olive', 'deep-other').status)           # only looks like it
        self.assertEqual(201, self.issue('olive', 'deeper/lane').status)
        self.assertEqual(201, self.issue('oscar', 'squat').status)                # the same account, again
        self.assertEqual(self.service.actor_parties('squat', self.project), {self.ids['oscar']})
        # In another project the name is free: a name is held in a project.
        beta = self.create_project(self.admin, 'Beta')
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s' % (beta, self.ids['olive']), {'role': 'owner'},
                                           token=self.admin).status)
        self.assertEqual(201, self.issue('olive', 'squat', beta).status)

    def test_every_issuer_a_name_has_had_is_of_the_works_party(self):
        """Second half: nothing on a record says which credential wrote it, and the moment cannot say it either
        (the tracker's stamp is later than this service's clock). So no moment decides: where two accounts
        have held a name, one after the other, work under it is of both, whenever it was written."""
        first = self.issue('oscar', 'lane-x')
        self.assertEqual(204, self.revoke('oscar', first.data['credential']['id']).status)
        second = self.issue('olive', 'lane-x')                                    # free again once revoked
        self.assertEqual(201, second.status, second.data)
        both = {self.ids['olive'], self.ids['oscar']}
        self.assertEqual(self.service.actor_parties('lane-x', self.project), both)
        self.assertEqual(self.service.actor_parties('lane-x/night', self.project), both)
        self.assertEqual(self.service.actor_parties('lane-x', 'another-project'), {'lane-x'})
        self.assertEqual(self.service.actor_parties('lane-x', None), {'lane-x'})
        task, contribution = self.deliver(second.data['credential']['secret'])
        shared = API.OWN_PARTY_SHARED % 'lane-x' + API.OWN_PARTY_NEXT
        self.assertIn('nothing on the record says which credential wrote it', shared)
        # olive's credential wrote it; oscar's never wrote anything. Both are refused, and told why.
        self.refused(self.approve(self.tokens['olive'], task, contribution), shared)
        self.refused(self.approve(self.tokens['oscar'], task, contribution), shared)
        self.assertEqual(self.state_of(task), 'awaiting-review')
        # Revoked too, the name stays theirs: a credential that no longer works still says whose the work was.
        self.assertEqual(204, self.revoke('olive', second.data['credential']['id']).status)
        self.refused(self.approve(self.tokens['olive'], task, contribution), shared)
        self.assertEqual(201, self.approve(self.admin, task, contribution).status)

    def test_a_writer_nobody_knows_is_its_own_party(self):
        """A name that is no account, no agent and under no credential record (a lane of the host route):
        its own party, so whoever may approve here approves it. Accepted as the rule, and said."""
        task = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'host'}, token=self.admin).data['id']
        self.assertEqual(self.service.actor_parties('host-lane', self.project), {'host-lane'})
        self.assertEqual(self.service.actor_parties('usr_0000000000000000/x', self.project), {'usr_0000000000000000/x'})

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

    def planted(self, task, contribution, name):
        """A recommendation standing under ``name``, as written over the host route."""
        records = self.backend.state['contributions'][task]
        self.backend.state.setdefault('recommendations', {}).setdefault(task, []).append({
            'id': 'rec_' + secrets.token_hex(4), 'task_id': task, 'kind': 'recommendation', 'contribution_id': contribution,
            'commit': test_http_agents.COMMIT, 'verdict': 'approve', 'summary': 'planted', 'items': [], 'actor': name,
            'created_at': '2099-01-01T00:00:00Z', 'after': len(records)})

    def test_every_read_traces_a_name_in_the_project_of_the_work(self):
        """`lane-c` is cora's here and nobody's anywhere else. Three places asked without the project and so
        took the name for nobody's: whether my party has recommended already, an agent's next actions, and
        what My work offers a person's agents to recommend. cora issued the credential as an owner and is a
        contributor now, so she recommends and does not approve."""
        self.ids['cora'] = self.create_account(self.admin, 'cora', 'cora-password-1')
        self.tokens['cora'] = self.login('cora', 'cora-password-1')[0]
        members = '/v1/projects/%s/members/%s' % (self.project, self.ids['cora'])
        self.assertEqual(200, self.request('PUT', members, {'role': 'owner'}, token=self.admin).status)
        worker, _ = self.worker_of('cora', 'lane-c')
        under, delivered = self.deliver(worker)                                   # cora's party's work
        self.assertEqual(200, self.request('PUT', members, {'role': 'contributor'}, token=self.admin).status)
        other, contribution = self.deliver(self.tokens['carl'])                   # another party's
        agent = self.agent_of('cora', 'Merlin')

        def offered():
            return {item['task'] for item in self.request('GET', '/v1/agents/me/next', token=agent).data['next_actions']
                    if item.get('kind') in ('to-review', 'review-recommended')}

        def prompt():
            return self.request('GET', '/v1/me/work', token=self.tokens['cora']).data['agent_prompts'][0]['text']
        self.assertEqual(offered() & {under, other}, {other})                     # not its own party's work
        self.assertIn(other, prompt())
        self.assertNotIn(under, prompt())
        self.assertEqual(403, self.recommend(agent, under, delivered).status)
        # A recommendation by cora's worker credential stands on carl's work: cora's party has recommended.
        self.planted(other, contribution, 'lane-c')
        again = self.recommend(agent, other, contribution)
        self.assertEqual(409, again.status, again.data)
        self.assertEqual(again.data['error']['detail'], {'recommended_by': 'lane-c'})
        self.assertNotIn(other, offered())
        self.assertNotIn(other, prompt())


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
        refused = self.recommend(second, task, contribution)
        self.assertEqual((403, API.NOT_INDEPENDENT), (refused.status, refused.data['error']['message']))   # the sentence it was
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
        self.assertIn("      rulesNote(data.rules)].filter(Boolean));\n", page)
        self.assertIn("if (!rules || typeof rules.approval_by_another_party !== 'boolean') return null;", page)
        self.assertIn('On this server nobody approves or recommends work of their own account', page)
        self.assertIn('On this server an owner may approve work of their own account', page)


class TaskPageTests(unittest.TestCase):
    """The review form of web/js/views/task.js run under Node: a refusal that says who must approve
    instead stays on the page. As a toast it was gone after seven seconds."""

    def test_the_refusal_stays_on_the_page(self):
        import shutil
        from test_http_web import run_node_module
        node = shutil.which('node')
        if not node:
            print('NOTE: TaskPageTests.test_the_refusal_stays_on_the_page was SKIPPED: node is not installed, so the '
                  'review form of web/js/views/task.js was not run on this platform.', file=sys.stderr)
            self.skipTest('node is not installed; the review form is not run here')
        done = run_node_module(self, node, 'await import(process.argv[1])',
                               (KIT / 'tests' / 'web_task_review_screen.mjs').as_uri(),
                               (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(),
                               (KIT / 'web' / 'js' / 'views' / 'task.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        refused = seen['refused']
        self.assertEqual((refused['sent'], refused['renders']), (['approve'], 0))             # the page is not redrawn
        self.assertEqual(refused['banner']['hidden'], False)
        self.assertEqual(refused['banner']['refused'], '403')
        self.assertTrue(refused['banner']['text'].startswith('This contribution was delivered by your own party'))
        self.assertIn('must approve it.', refused['banner']['text'])
        # A stale page keeps its own sentence, and an approval that is taken redraws the page.
        self.assertEqual((seen['stale']['banner']['hidden'], seen['stale']['banner']['refused'], seen['stale']['renders']),
                         (False, None, 0))
        self.assertIn('A newer revision arrived', seen['stale']['banner']['text'])
        self.assertEqual((seen['approved']['banner']['hidden'], seen['approved']['renders']), (True, 1))


class WiringTests(Party):
    """How the setting reaches the service, loaded and run rather than read."""

    @unittest.skipUnless(os.name == 'posix', 'office_service.py imports fcntl; POSIX only')
    def test_the_office_configuration_takes_true_or_false_and_nothing_else(self):
        import office_service
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'office.json'

            def loaded(value):
                path.write_text(json.dumps(dict({'schema_version': 1}, **({} if value is None else
                                                                          {'approval_by_another_party': value}))),
                                encoding='utf-8')
                return office_service.service_config(path)
            for bad in ('true', 1, 0, 'on', [], None.__class__.__name__):
                with self.subTest(value=bad):
                    with self.assertRaisesRegex(ValueError, 'approval_by_another_party must be true or false'):
                        loaded(bad)
            commands = {}
            for value in (True, False, None):
                settings = loaded(value)
                commands[value] = office_service.web_command(settings, Path(folder), 8443, 'python3', Path(folder) / 'office_service.py')
            self.assertIn('--approval-by-another-party', commands[True])
            self.assertNotIn('--approval-by-another-party', commands[False])
            self.assertNotIn('--approval-by-another-party', commands[None])

    def test_the_flag_is_taken_whole_or_not_at_all(self):
        import contextlib
        import io
        for prefix in ('--approval-by-another', '--approval', '--approval-by-another-part'):
            with self.subTest(flag=prefix):
                said = io.StringIO()
                with contextlib.redirect_stderr(said), self.assertRaises(SystemExit) as stopped:
                    http_service.main(['--state', str(Path(self.service.store.path).with_name('none.json')), prefix])
                self.assertEqual(stopped.exception.code, 2)
                self.assertIn('unrecognized arguments: ' + prefix, said.getvalue())
        import inspect
        source = inspect.getsource(http_service.main)
        self.assertIn("approval_by_another_party=args.approval_by_another_party", source)
        said = "    for line in settings_lines(service):\n        print(line, file=sys.stderr, flush=True)\n"
        self.assertIn(said, source)
        # After the port is had: a start that fails for a taken port says its one line and nothing else.
        self.assertLess(source.index('return EXIT_PORT_TAKEN'), source.index(said))

    def test_a_start_says_the_setting_and_a_change_is_in_the_audit(self):
        def entries():
            with self.service.store.lock:
                return [(event['action'], event['outcome'], event['reason'], event['user_id'])
                        for event in self.service.state['audit'] if event['action'].startswith('settings.')]
        self.service.approval_by_another_party = False
        self.assertEqual(http_service.settings_lines(self.service),
                         ['approval by another party (--approval-by-another-party): off'])
        self.assertEqual(entries(), [])                                           # never recorded counts as off
        self.service.approval_by_another_party = True
        self.assertEqual(http_service.settings_lines(self.service),
                         ['approval by another party (--approval-by-another-party): on '
                          '(it was off at the last start; recorded in the audit)'])
        self.assertEqual(entries(), [('settings.approval_by_another_party', 'committed', 'off -> on at service start', None)])
        self.assertEqual(http_service.settings_lines(self.service), ['approval by another party (--approval-by-another-party): on'])
        self.assertEqual(len(entries()), 1)                                       # the same again: no second entry
        # It is in the state file, so the next process knows what the last start had.
        again = Service(Store(self.service.store.path), approval_by_another_party=False)
        self.assertEqual(again.note_settings(), (True, False))
        self.assertEqual([event['reason'] for event in again.state['audit'] if event['action'].startswith('settings.')],
                         ['off -> on at service start', 'on -> off at service start'])

if __name__ == '__main__':
    unittest.main()
