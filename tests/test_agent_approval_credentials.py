"""kittrial-5bb.519: under by-agent a coordinator agent approves work under a worker credential of its person.

The shapes a same-person worker can take, through the real routes of the web service, crossed with the
project's setting (by-agent, by-person, damaged) and the installation's ``approval_by_another_party``. The
BEFORE expectations are literal: the answers of c01d067, the commit this was built on, recorded by running the
same shapes through the same route. Only the planned by-agent cells change; every by-person and damaged cell is
what it was, byte for byte.

(1) a named worker credential, (2) a name another account has held, (3) a credential issued with no name (it
writes under the account id), (4) ``ACCOUNT/label``, (5) a credential issued by an agent (not producible: the
route refuses it; an agent writing ``agent/label`` is shape 5b), (6) the coordinator agent itself (a copied
agent credential), (7) the task ASSIGNED to such a name and delivered by somebody else, (8) a second agent made
the way My agents makes one. The real endpoint and bd carry (1), (3) and (8) at the end.
"""
import json
import secrets
import shutil
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_authority
import http_service
import test_agent_approval
import test_coordinator_grant_real
import test_http_agents
import test_http_web

API = http_service.ApiHandler

# The answers of c01d067, literally (tmp/ matrix_c01d067.json): the BEFORE of kittrial-5bb.519.

B0 = ("This contribution was delivered by this agent's own party: its account, one of that account's "
    'agents, or a name that a worker credential of that account holds or has held. On this project a '
    'coordinator agent may approve the work of another agent of the same person (agent approvals: by '
    "agent), but never its own work, its person's own work, or work under a worker credential of that "
    'person: somebody of another party who may approve here must approve it (another coordinator or owner'
    ' of this project, or a superuser).')

B1 = ("This contribution was delivered by this agent's own party: its account, one of that account's "
    'agents, or a name that a worker credential of that account holds or has held. An agent never '
    "approves work of its own party, whatever this server's setting for people says: somebody of another "
    'party who may approve here must approve it (another coordinator or owner of this project, or a '
    'superuser).')

B2 = ('This contribution was delivered under lane-s, a name that worker credentials of more than one '
    "account have held. One of those accounts is this agent's, and nothing on the record says which "
    'credential wrote it, so it counts as the work of each of them. On this project a coordinator agent '
    'may approve the work of another agent of the same person (agent approvals: by agent), but never its '
    "own work, its person's own work, or work under a worker credential of that person: somebody of "
    'another party who may approve here must approve it (another coordinator or owner of this project, or'
    ' a superuser).')

B3 = ('This contribution was delivered under lane-s, a name that worker credentials of more than one '
    "account have held. One of those accounts is this agent's, and nothing on the record says which "
    'credential wrote it, so it counts as the work of each of them. An agent never approves work of its '
    "own party, whatever this server's setting for people says: somebody of another party who may approve"
    ' here must approve it (another coordinator or owner of this project, or a superuser).')

B5 = ('This contribution was delivered by this agent itself. On this project a coordinator agent may '
    'approve the work of another agent of the same person (agent approvals: by agent), but never its own '
    "work, its person's own work, or work under a worker credential of that person: somebody of another "
    'party who may approve here must approve it (another coordinator or owner of this project, or a '
    'superuser).')

B6 = ("This task is assigned to this agent's own party (its account, one of that account's agents, or a "
    'name of a worker credential of that account). Another account delivered the contribution, but nobody'
    ' approves work on a task their own party holds. On this project a coordinator agent may approve the '
    'work of another agent of the same person (agent approvals: by agent), but never its own work, its '
    "person's own work, or work under a worker credential of that person: somebody of another party who "
    'may approve here must approve it (another coordinator or owner of this project, or a superuser).')

B7 = ("This task is assigned to this agent's own party (its account, one of that account's agents, or a "
    'name of a worker credential of that account). Another account delivered the contribution, but nobody'
    ' approves work on a task their own party holds. An agent never approves work of its own party, '
    "whatever this server's setting for people says: somebody of another party who may approve here must "
    'approve it (another coordinator or owner of this project, or a superuser).')

B8 = ('This task is assigned to this agent itself. On this project a coordinator agent may approve the work'
    ' of another agent of the same person (agent approvals: by agent), but never its own work, its '
    "person's own work, or work under a worker credential of that person: somebody of another party who "
    'may approve here must approve it (another coordinator or owner of this project, or a superuser).')

# The words of the new rules, literally (the plan's sentences).
WORKER_CREDENTIAL_BASIS = ('Approved by a coordinator agent of the same person as the worker credential that '
                           'delivered it (project setting agent_approval: by-agent).')
AGENT_BASIS = ('Approved by a coordinator agent of the same person as the agent that delivered it '
               '(project setting agent_approval: by-agent).')
OLD_BY_AGENT_NEXT = (" On this project a coordinator agent may approve the work of another agent of the same person "
                     "(agent approvals: by agent), but never its own work, its person's own work, or work under a "
                     "worker credential of that person: somebody of another party who may approve here must approve "
                     "it (another coordinator or owner of this project, or a superuser).")
# The coordinator's wording of 2026-11-05 04:15Z: every by-agent 403 ends with it.
BY_AGENT_NEXT = (" On this project a coordinator agent may approve the work of another agent of the same person, or "
                 "of a worker credential of that person issued with a name no other account has held (agent "
                 "approvals: by agent), but never its own work (its own sub-workers count as its own work) or its "
                 "person's own work: somebody of another party who may approve here must approve it (another "
                 "coordinator or owner of this project, or a superuser).")
N2 = ("This contribution was delivered under lane-s, a name that worker credentials of more than one account have "
      "held. One of those accounts is this agent's, and nothing on the record says which credential wrote it, so "
      "it counts as the work of each of them. Issue a credential with a name no other account has held, or set "
      "the worker up from My agents." + BY_AGENT_NEXT)
N3 = ("This contribution was delivered under your person's own account id, which their own session also writes, "
      "so it cannot be told from your person's work. Issue the worker's credential with a name, or set the "
      "worker up from My agents." + BY_AGENT_NEXT)
N6_AUTHOR = ("This contribution was delivered by this agent itself. If another worker uses a copy of this agent's "
             "credential, set it up as a new agent from My agents." + BY_AGENT_NEXT)
N9_AUTHOR = ("This contribution was delivered by this agent itself. Work by a sub-worker of this agent counts as this "
             "agent's own. Run the worker as a separate agent from My agents, in its own VS Code window, and this "
             "agent may approve it." + BY_AGENT_NEXT)
A6 = B6.replace(OLD_BY_AGENT_NEXT, BY_AGENT_NEXT)          # (7b), (7c): still refused, only the ending is new
N6_ASSIGNEE = ("This task is assigned to this agent itself. If another worker uses a copy of this agent's "
               "credential, set it up as a new agent from My agents." + BY_AGENT_NEXT)

#: shape -> (BEFORE under by-agent, AFTER under by-agent, BEFORE under by-person and damaged). A string is a 403
#: sentence; ('201', basis) is an approval. The BEFORE columns are the c01d067 results.
CELLS = {
    '1-named-worker-cred':            (B0, ('201', 'worker-credential'), B1),
    '2-shared-name':                  (B2, N2, B3),
    '3-no-name-account-id':           (B0, N3, B1),
    '4-account-slash-label':          (B0, ('201', 'worker-credential'), B1),
    '5b-agent-slash-label':           (('201', 'agent'), ('201', 'agent'), B1),
    '6-copied-agent-credential':      (B5, N6_AUTHOR, B1),
    '9-coordinator-sub-label':        (B5, N9_AUTHOR, B1),
    '10-sibling-sub-label':           (('201', 'agent'), ('201', 'agent'), B1),
    '7a-assigned-lane-c':             (B6, ('201', 'worker-credential'), B7),
    '7b-assigned-shared-name':        (B6, A6, B7),
    '7c-assigned-bare-account-id':    (B6, A6, B7),
    '7d-assigned-account-slash-label': (B6, ('201', 'worker-credential'), B7),
    '7e-assigned-sibling-agent':      (('201', 'agent'), ('201', 'agent'), B7),
    '7f-assigned-coordinator-itself': (B8, N6_ASSIGNEE, B7),
    '8-two-agents-setup-flow':        (('201', 'agent'), ('201', 'agent'), B1),
}
#: The cells the plan changes, and nothing else.
CHANGED = {'9-coordinator-sub-label', '1-named-worker-cred', '2-shared-name', '3-no-name-account-id', '4-account-slash-label',
           '6-copied-agent-credential', '7a-assigned-lane-c', '7d-assigned-account-slash-label',
           '7f-assigned-coordinator-itself'}


class PinBase(test_agent_approval.SamePerson):
    """cora (a coordinator; an owner where she issues credentials) has Heron (granted) and Egret; olive and oscar own."""

    def claim_as(self, token, actor, task=None):
        task = task or self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': secrets.token_hex(4)},
                                    token=self.admin).data['id']
        return self.request('POST', self.base(task) + '/claim', {'actor': actor}, token=token)

    def rawnames(self, task):
        recs = [r for r in self.backend.state['contributions'].get(task, []) if r['kind'] == 'contribution']
        return recs[-1]['actor'], self.backend._task(self.project, task).get('assignee')

    def set_project_setting(self, how):
        with self.service.store.lock:
            if how == 'damaged':
                self.service.state['agent_approval'] = {self.project: {'mode': 'bogus'}}
            else:
                self.service.state['agent_approval'] = {self.project: {
                    'mode': how, 'set_by': self.ids['olive'], 'set_at': '2026-01-01T00:00:00Z'}}


class ShapeFourPins(PinBase):
    """Condition 1: only O's own no-name credential can write ``usr_O/label``. If one of these fails, (4) stays refused."""

    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.member('cora', 'owner', token=self.admin).status)
        self.name = self.ids['cora'] + '/night'

    def test_a_persons_web_session_cannot_write_account_slash_label(self):
        for who in ('cora', 'olive', 'carl'):
            with self.subTest(session=who):
                refused = self.claim_as(self.tokens[who], self.name)
                self.assertEqual(403, refused.status, refused.data)
                self.assertIn('does not match the authenticated account', refused.data['error']['message'])
        refused = self.claim_as(self.admin, self.name)                  # a superuser's session too
        self.assertEqual(403, refused.status, refused.data)
        # the session of the account itself writes only its exact account id
        self.assertEqual(200, self.claim_as(self.tokens['cora'], self.ids['cora']).status)

    def test_a_persons_session_cannot_deliver_under_it_either(self):
        task = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'x'}, token=self.admin).data['id']
        self.assertEqual(200, self.request('POST', self.base(task) + '/claim', {}, token=self.tokens['cora']).status)
        made = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'contribute', 'commit': test_http_agents.COMMIT, 'base_commit': test_http_agents.BASE,
            'bundle_sha256': test_http_agents.BUNDLE, 'summary': 'delivered', 'actor': self.name},
            token=self.tokens['cora'])
        self.assertEqual(403, made.status, made.data)

    def test_an_agent_token_cannot_write_it(self):
        for label, token in (('heron', self.agent), ('egret', self.other)):
            with self.subTest(agent=label):
                refused = self.claim_as(token, self.name)
                self.assertEqual(403, refused.status, refused.data)
                self.assertIn('outside the credential attribution namespace', refused.data['error']['message'])

    def test_another_accounts_no_name_credential_cannot_write_it(self):
        oscars, _ = self.worker_of('oscar', None)
        refused = self.claim_as(oscars, self.name)
        self.assertEqual(403, refused.status, refused.data)
        self.assertIn('outside the credential attribution namespace', refused.data['error']['message'])
        named, _ = self.worker_of('oscar', 'oscar-lane')                # nor with a name of its own
        self.assertEqual(403, self.claim_as(named, self.name).status)

    def test_a_named_credential_of_the_same_person_cannot_write_it(self):
        named, _ = self.worker_of('cora', 'lane-c')
        self.assertEqual(403, self.claim_as(named, self.name).status)

    def test_the_persons_own_no_name_credential_can(self):
        mine, _ = self.worker_of('cora', None)
        accepted = self.claim_as(mine, self.name)
        self.assertEqual(200, accepted.status, accepted.data)
        task, _ = self.deliver_as(mine, self.ids['cora'] + '/other')
        self.assertEqual(self.ids['cora'] + '/other', self.rawnames(task)[0])

    def test_the_host_route_refuses_an_actor_shaped_as_an_account(self):
        self.assertIsNotNone(http_authority.http_actor_id(self.name))
        self.assertIsNotNone(http_authority.http_actor_id(self.ids['cora']))
        for config in (None, object()):
            with self.subTest(config=config):
                denied = http_authority.http_actor_denial({'actor': self.name}, config)
                self.assertIsNotNone(denied)
                self.assertEqual(126, denied['returncode'])
        # And an agent-shaped head, which an agent's own credential is the only writer of.
        self.assertIsNotNone(http_authority.http_actor_denial({'actor': self.agent_id + '/night'}, None))


class ShapeSetup(PinBase):
    """The shapes of one person's worker, built once on cora's side of the project."""

    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.member('cora', 'owner', token=self.admin).status)
        self.lane_c, _ = self.worker_of('cora', 'lane-c')
        first = self.issue('oscar', 'lane-s')
        self.assertEqual(204, self.revoke('oscar', first.data['credential']['id']).status)
        second = self.issue('cora', 'lane-s')
        self.assertEqual(201, second.status, second.data)
        self.lane_s = second.data['credential']['secret']
        self.no_name, _ = self.worker_of('cora', None)
        self.fresh, self.fresh_id = self.agent_with_id('cora', 'Fresh')

    def shape_makers(self):
        carl = self.tokens['carl']

        def assigned(name):
            def made():
                task, contribution = self.deliver(carl)
                self.backend._task(self.project, task)['assignee'] = name
                return task, contribution
            return made

        cora = self.ids['cora']
        return {
            '1-named-worker-cred': lambda: self.deliver(self.lane_c),
            '2-shared-name': lambda: self.deliver(self.lane_s),
            '3-no-name-account-id': lambda: self.deliver(self.no_name),
            '4-account-slash-label': lambda: self.deliver_as(self.no_name, cora + '/night'),
            '5b-agent-slash-label': lambda: self.deliver_as(self.fresh, self.fresh_id + '/lane'),
            '6-copied-agent-credential': lambda: self.deliver(self.agent),
            '9-coordinator-sub-label': lambda: self.deliver_as(self.agent, self.agent_id + '/sub'),
            '10-sibling-sub-label': lambda: self.deliver_as(self.other, self.other_id + '/sub'),
            '7a-assigned-lane-c': assigned('lane-c'),
            '7b-assigned-shared-name': assigned('lane-s'),
            '7c-assigned-bare-account-id': assigned(cora),
            '7d-assigned-account-slash-label': assigned(cora + '/night'),
            '7e-assigned-sibling-agent': assigned(self.other_id),
            '7f-assigned-coordinator-itself': assigned(self.agent_id),
            '8-two-agents-setup-flow': lambda: self.deliver(self.other),
        }


class Shapes(ShapeSetup):
    """The approve route's answer for every shape, before and after."""

    def run_matrix(self, setting):
        """Every shape under ``setting`` and both values of approval_by_another_party; returns observed cells."""
        seen = {}
        makers = self.shape_makers()
        for another in (False, True):
            self.service.approval_by_another_party = another
            self.set_project_setting(setting)
            for shape, make in makers.items():
                task, contribution = make()
                answer = self.approve(self.agent, task, contribution)
                if answer.status == 201:
                    event = [e for e in self.events('reviews.add') if e.get('same_person_coordinator')][-1]
                    seen[shape, another] = (('201', event.get('basis')), self.approval_summary(task), event, task)
                else:
                    seen[shape, another] = (answer.data['error']['message'], answer.status, None, task)
                    self.assertEqual('awaiting-review', self.state_of(task), shape)
        return seen

    def check_setting(self, setting, column):
        for (shape, another), (got, extra, event, task) in self.run_matrix(setting).items():
            with self.subTest(shape=shape, approval_by_another_party=another, setting=setting):
                expected = CELLS[shape][column]
                if isinstance(expected, tuple):
                    self.assertEqual(expected, got)
                    self.assertEqual(
                        'accepted ' + (WORKER_CREDENTIAL_BASIS if expected[1] == 'worker-credential' else AGENT_BASIS),
                        extra)
                    self.assertEqual(event['agent_approval'], 'by-agent')
                else:
                    self.assertEqual((403, expected), (extra, got))

    def test_by_agent_matrix_before_and_after(self):
        self.check_setting('by-agent', 1)

    def test_by_agent_before_literal_cells_that_the_plan_does_not_change(self):
        for shape, (before, after, _) in CELLS.items():
            with self.subTest(shape=shape):
                # Every by-agent 403 has the new ending (the coordinator's 04:15Z decision); apart from it, only
                # the planned cells change.
                same = (after.replace(BY_AGENT_NEXT, OLD_BY_AGENT_NEXT) if isinstance(after, str) else after)
                if shape in CHANGED:
                    self.assertNotEqual(before, same)
                else:
                    self.assertEqual(before, same)
        # Exactly the planned cells went 403 -> 201.
        opened = {s for s, (b, a, _) in CELLS.items() if isinstance(a, tuple) and not isinstance(b, tuple)}
        self.assertEqual({'1-named-worker-cred', '4-account-slash-label', '7a-assigned-lane-c',
                          '7d-assigned-account-slash-label'}, opened)

    def test_the_by_agent_ending_is_the_coordinators_sentence_and_ends_every_by_agent_refusal(self):
        self.assertEqual(BY_AGENT_NEXT, API.BY_AGENT_NEXT)
        refused = 0
        for (shape, another), (got, extra, _, _) in self.run_matrix('by-agent').items():
            if extra == 403:
                refused += 1
                with self.subTest(shape=shape, approval_by_another_party=another):
                    self.assertTrue(got.endswith(BY_AGENT_NEXT), got)
                    self.assertNotIn(OLD_BY_AGENT_NEXT, got)
        self.assertEqual(2 * 7, refused)              # (2), (3), (6), (9), (7b), (7c), (7f), both values

    def test_by_person_matrix_is_what_it_was(self):
        self.check_setting('by-person', 2)

    def test_a_damaged_setting_matrix_is_what_it_was(self):
        self.check_setting('damaged', 2)

    def test_shape_8_two_agents_made_the_way_my_agents_makes_them_follow_the_generated_agent_md(self):
        egret, egret_id = self.other, self.other_id
        self.assertEqual(200, self.request('GET', '/v1/agents/me/next', token=egret).status)
        task = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'w'}, token=self.admin).data['id']
        self.assertEqual(200, self.request('POST', self.base(task) + '/claim', {}, token=egret).status)
        self.assertEqual(201, self.request('POST', self.base(task) + '/checkpoints',
                                           {'summary': 'working', 'previous': None}, token=egret).status)
        made = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'contribute', 'commit': test_http_agents.COMMIT, 'base_commit': test_http_agents.BASE,
            'bundle_sha256': test_http_agents.BUNDLE, 'summary': 'delivered'}, token=egret)
        self.assertEqual(201, made.status, made.data)
        self.assertEqual((egret_id, egret_id), self.rawnames(task))
        approved = self.approve(self.agent, task, made.data['contribution']['id'])
        self.assertEqual(201, approved.status, approved.data)
        self.assertEqual('accepted ' + AGENT_BASIS, self.approval_summary(task))
        event = [e for e in self.events('reviews.add') if e.get('same_person_coordinator')][-1]
        self.assertEqual((event['basis'], event['author']), ('agent', egret_id))


class NextActionsTests(ShapeSetup):
    """``GET /v1/agents/me/next`` offers for approval exactly what the approve route lets through (kittrial-5bb.519)."""

    LISTED = {'1-named-worker-cred': 'A contribution under a worker credential of your person awaits review.',
              '4-account-slash-label': 'A contribution under a worker credential of your person awaits review.',
              '7a-assigned-lane-c': 'A contribution under a worker credential of your person awaits review.',
              '8-two-agents-setup-flow': 'A contribution by another agent of your person awaits review.'}
    NEVER = ('2-shared-name', '3-no-name-account-id', '6-copied-agent-credential', '9-coordinator-sub-label',
             '7b-assigned-shared-name', '7c-assigned-bare-account-id', '7f-assigned-coordinator-itself')

    def offered(self):
        data = self.request('GET', '/v1/agents/me/next', token=self.agent).data
        return {a['task']: a for a in data['next_actions'] if a['kind'] == 'to-review'}

    def made(self):
        makers = self.shape_makers()
        return {shape: makers[shape]()[0] for shape in tuple(self.LISTED) + self.NEVER}

    def test_shape_1_is_listed_under_by_agent_and_not_under_by_person(self):
        tasks = self.made()
        listed = self.offered()
        for shape, reason in self.LISTED.items():
            with self.subTest(shape=shape):
                self.assertIn(tasks[shape], listed)
                self.assertTrue(listed[tasks[shape]]['reason'].startswith(reason), listed[tasks[shape]]['reason'])
        self.set_project_setting('by-person')
        listed = self.offered()
        for shape in self.LISTED:
            with self.subTest(shape=shape, setting='by-person'):
                self.assertNotIn(tasks[shape], listed)

    def test_shapes_2_3_and_6_are_never_listed(self):
        tasks = self.made()
        for setting in ('by-agent', 'by-person', 'damaged'):
            self.set_project_setting(setting)
            listed = self.offered()
            for shape in self.NEVER:
                with self.subTest(shape=shape, setting=setting):
                    self.assertNotIn(tasks[shape], listed)

    def test_what_is_listed_is_what_the_approve_route_lets_through(self):
        tasks = self.made()
        listed = set(self.offered())
        makers = self.shape_makers()
        for shape in tuple(self.LISTED) + self.NEVER:
            task, contribution = makers[shape]()
            with self.subTest(shape=shape):
                self.assertEqual(tasks[shape] in listed, self.approve(self.agent, task, contribution).status == 201)


class WorkerCredentialRuleTests(PinBase):
    """The rule itself, one case at a time: what makes a name a worker credential's of the approving agent's person."""

    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.member('cora', 'owner', token=self.admin).status)

    def of_same_person(self, name, agent_id=None, project=None):
        return self.service.worker_credential_of_same_person(
            self.agent_id if agent_id is None else agent_id, name, self.project if project is None else project)

    def test_the_names_that_count_and_the_names_that_do_not(self):
        cora, oscar = self.ids['cora'], self.ids['oscar']
        self.worker_of('cora', 'lane-c')
        self.worker_of('oscar', 'oscar-lane')
        shared = self.issue('oscar', 'lane-s')
        self.assertEqual(204, self.revoke('oscar', shared.data['credential']['id']).status)
        self.assertEqual(201, self.issue('cora', 'lane-s').status)
        for name, expected in (
                ('lane-c', True), ('lane-c/sub', True), ('LANE-C', True),           # a name of her credential, any case, below it
                (cora + '/night', True), (cora + '/a/b', True),                      # her no-name credential's labels
                (cora, False),                                                       # her session writes this too
                (cora + '/', False),                                                 # no label
                ('lane-s', False), ('lane-s/x', False),                              # held by two accounts
                ('oscar-lane', False), (oscar, False), (oscar + '/night', False),    # another account's
                (self.agent_id, False), (self.agent_id + '/x', False),               # the agent itself
                (self.agent_id.upper(), False), (self.other_id, False), (self.other_id + '/lane', False),
                ('never-issued', False), ('', False), (None, False), (7, False)):
            with self.subTest(name=name):
                self.assertIs(expected, self.of_same_person(name))
        self.assertIs(False, self.of_same_person('lane-c', agent_id='agent_nobody'))
        self.assertIs(False, self.of_same_person('lane-c', agent_id=''))
        self.assertIs(False, self.of_same_person('lane-c', project='other-project'))
        self.assertIs(False, self.of_same_person('lane-c', project=''))

    def test_it_is_the_issuer_set_of_actor_parties_and_no_other(self):
        """Condition 2: one implementation of the set. Whatever actor_parties computes for a name, this agrees."""
        self.worker_of('cora', 'lane-c')
        first = self.issue('oscar', 'lane-s')
        self.assertEqual(204, self.revoke('oscar', first.data['credential']['id']).status)
        self.assertEqual(201, self.issue('cora', 'lane-s').status)
        cora = self.ids['cora']
        for name in ('lane-c', 'lane-s', 'lane-c/x', 'nobody', cora + '/night'):
            with self.subTest(name=name):
                parties = self.service.actor_parties(name, self.project, True)
                self.assertEqual(parties == {cora} and name != cora, self.of_same_person(name))

    def test_a_name_revoked_and_issued_again_by_the_same_person_is_still_one_issuer(self):
        first = self.issue('cora', 'lane-r')
        self.assertEqual(201, first.status, first.data)
        self.assertEqual(204, self.revoke('cora', first.data['credential']['id']).status)
        again = self.issue('cora', 'lane-r')
        self.assertEqual(201, again.status, again.data)
        self.assertEqual({self.ids['cora']}, self.service.actor_parties('lane-r', self.project, True))
        self.assertIs(True, self.of_same_person('lane-r'))
        task, contribution = self.deliver(again.data['credential']['secret'])
        approved = self.approve(self.agent, task, contribution)
        self.assertEqual(201, approved.status, approved.data)
        self.assertEqual('accepted ' + WORKER_CREDENTIAL_BASIS, self.approval_summary(task))

    def test_a_name_once_held_by_another_accounts_revoked_credential_stays_shared_and_refused(self):
        first = self.issue('oscar', 'lane-o')
        self.assertEqual(204, self.revoke('oscar', first.data['credential']['id']).status)
        mine = self.issue('cora', 'lane-o')
        self.assertEqual(201, mine.status, mine.data)
        self.assertEqual({self.ids['cora'], self.ids['oscar']}, self.service.actor_parties('lane-o', self.project, True))
        self.assertIs(False, self.of_same_person('lane-o'))
        task, contribution = self.deliver(mine.data['credential']['secret'])
        refused = self.approve(self.agent, task, contribution)
        self.assertEqual((403, N2.replace('lane-s', 'lane-o')), (refused.status, refused.data['error']['message']))
        self.assertEqual('awaiting-review', self.state_of(task))

    def test_the_other_order_too_cora_held_it_first_and_revoked_it(self):
        first = self.issue('cora', 'lane-p')
        self.assertEqual(204, self.revoke('cora', first.data['credential']['id']).status)
        theirs = self.issue('oscar', 'lane-p')
        if theirs.status == 201:
            self.assertIs(False, self.of_same_person('lane-p'))
            task, contribution = self.deliver(theirs.data['credential']['secret'])
            self.assertEqual(403, self.approve(self.agent, task, contribution).status)
            self.assertEqual('awaiting-review', self.state_of(task))
        else:
            self.assertGreaterEqual(theirs.status, 400)     # the service already keeps the name from a second account
            self.assertIs(True, self.of_same_person('lane-p'))

    def test_an_expired_credential_of_another_account_also_keeps_a_name_shared(self):
        theirs = self.issue('oscar', 'lane-e')
        self.assertEqual(201, theirs.status, theirs.data)
        with self.service.store.lock:
            self.service.state['credentials'][theirs.data['credential']['id']]['expires_at'] = '2000-01-01T00:00:00Z'
        mine = self.issue('cora', 'lane-e')
        if mine.status == 201:
            self.assertIs(False, self.of_same_person('lane-e'))
        else:
            self.assertGreaterEqual(mine.status, 400)


class SentenceTests(PinBase):
    """Condition 3 and the record: the new sentence is the server's, never a caller's."""

    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.member('cora', 'owner', token=self.admin).status)
        self.lane_c, _ = self.worker_of('cora', 'lane-c')

    VARIANTS = {
        'exact': lambda s: s,
        'prefix': lambda s: s + ' Looks good.',
        'middle': lambda s: 'Prefix. ' + s + ' Suffix.',
        'lowercase': lambda s: 'ok. ' + s.lower(),
        'no-period': lambda s: 'ok. ' + s[:-1],
        'whitespace': lambda s: 'ok. ' + s.replace('same person', 'same   person'),
        'nbsp': lambda s: 'ok. ' + s.replace(' ', ' '),
        'zero-width': lambda s: 'ok. ' + s.replace('Approved', 'App​roved'),
        'fullwidth-paren': lambda s: 'ok. ' + s.replace('(', '（').replace(')', '）'),
        'newline': lambda s: 'ok. ' + s.replace(' as ', '\nas '),
    }

    def test_a_caller_typing_the_worker_credential_sentence_is_refused_422_everywhere(self):
        mine, mine_cid = self.deliver(self.lane_c)
        carls, carls_cid = self.deliver(self.tokens['carl'])
        sibling, sibling_cid = self.deliver(self.other)
        for name, make in self.VARIANTS.items():
            text = make(WORKER_CREDENTIAL_BASIS)
            for label, task, cid, who in (('credential work', mine, mine_cid, self.agent),
                                          ('independent work', carls, carls_cid, self.agent),
                                          ('independent work by a person', carls, carls_cid, self.tokens['olive']),
                                          ('sibling work', sibling, sibling_cid, self.agent)):
                with self.subTest(variant=name, on=label):
                    refused = self.request('POST', self.base(task) + '/reviews', {
                        'operation': 'approve', 'contribution': cid, 'summary': text}, token=who)
                    self.assertEqual(422, refused.status, refused.data)
                    self.assertIn(WORKER_CREDENTIAL_BASIS, refused.data['error']['message'])
                    self.assertEqual('awaiting-review', self.state_of(task))
            with self.subTest(variant=name, on='recommend'):
                refused = self.request('POST', self.base(carls) + '/reviews', {
                    'operation': 'recommend', 'schema_version': 1, 'operation_id': 'rec-' + name,
                    'contribution': carls_cid, 'commit': test_http_agents.COMMIT, 'verdict': 'approve',
                    'summary': text, 'items': []}, token=self.agent)
                self.assertEqual(422, refused.status, refused.data)
                self.assertIn(WORKER_CREDENTIAL_BASIS, refused.data['error']['message'])
        self.assertFalse(any(e.get('same_person_coordinator') for e in self.events('reviews.add')))

    def test_the_old_sentence_is_still_refused_on_credential_work(self):
        task, contribution = self.deliver(self.lane_c)
        refused = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'approve', 'contribution': contribution, 'summary': AGENT_BASIS}, token=self.agent)
        self.assertEqual(422, refused.status, refused.data)
        self.assertIn(AGENT_BASIS, refused.data['error']['message'])

    def test_a_forged_sentence_on_the_agents_own_work_is_still_a_403_first(self):
        own, cid = self.deliver(self.agent)
        refused = self.request('POST', self.base(own) + '/reviews', {
            'operation': 'approve', 'contribution': cid, 'summary': WORKER_CREDENTIAL_BASIS}, token=self.agent)
        self.assertEqual(403, refused.status, refused.data)

    def test_similar_words_are_allowed_and_the_server_adds_its_sentence(self):
        task, contribution = self.deliver(self.lane_c)
        similar = 'Approved by a coordinator agent of the same person after checking the worker credential.'
        done = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'approve', 'contribution': contribution, 'summary': similar}, token=self.agent)
        self.assertEqual(201, done.status, done.data)
        self.assertEqual(similar + ' ' + WORKER_CREDENTIAL_BASIS, self.approval_summary(task))

    def test_a_summary_without_room_for_the_credential_sentence_says_how_much_to_cut(self):
        task, contribution = self.deliver(self.lane_c)
        room = API.SUMMARY_MAX - len(WORKER_CREDENTIAL_BASIS) - 1
        too_long = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'approve', 'contribution': contribution, 'summary': 'x' * (room + 3)}, token=self.agent)
        self.assertEqual(422, too_long.status, too_long.data)
        self.assertIn('shorten the summary by 3 characters', too_long.data['error']['message'])
        self.assertIn('a sentence of %d characters' % len(WORKER_CREDENTIAL_BASIS), too_long.data['error']['message'])
        self.assertEqual('awaiting-review', self.state_of(task))
        fits = self.request('POST', self.base(task) + '/reviews', {
            'operation': 'approve', 'contribution': contribution, 'summary': 'x' * room}, token=self.agent)
        self.assertEqual(201, fits.status, fits.data)
        self.assertEqual(API.SUMMARY_MAX, len(self.approval_summary(task)))

    def test_the_audit_names_the_basis_and_the_old_approvals_say_agent(self):
        by_credential, c1 = self.deliver(self.lane_c)
        by_agent, c2 = self.deliver(self.other)
        self.assertEqual(201, self.approve(self.agent, by_credential, c1).status)
        self.assertEqual(201, self.approve(self.agent, by_agent, c2).status)
        events = [e for e in self.events('reviews.add') if e.get('same_person_coordinator')]
        self.assertEqual([('lane-c', 'worker-credential'), (self.other_id, 'agent')],
                         [(e['author'], e['basis']) for e in events])
        for event in events:
            self.assertEqual(('by-agent', 'default'), (event['agent_approval'], event['agent_approval_set_by']))
        self.assertEqual('accepted ' + WORKER_CREDENTIAL_BASIS, self.approval_summary(by_credential))
        self.assertEqual('accepted ' + AGENT_BASIS, self.approval_summary(by_agent))

    def test_a_task_delivered_by_both_kinds_says_worker_credential(self):
        """Author another agent of the person and assignee a worker credential's name: the stronger statement."""
        task, contribution = self.deliver(self.other)
        self.backend._task(self.project, task)['assignee'] = 'lane-c'
        self.assertEqual(201, self.approve(self.agent, task, contribution).status)
        self.assertEqual('accepted ' + WORKER_CREDENTIAL_BASIS, self.approval_summary(task))
        event = [e for e in self.events('reviews.add') if e.get('same_person_coordinator')][-1]
        self.assertEqual('worker-credential', event['basis'])

    def test_the_people_are_judged_as_before_on_credential_work(self):
        """cora's own session is not the agent: with the installation's setting on she is still refused her own party's work."""
        self.service.approval_by_another_party = True
        task, contribution = self.deliver(self.lane_c)
        refused = self.approve(self.tokens['cora'], task, contribution)
        self.assertEqual((403, API.OWN_PARTY + API.OWN_PARTY_NEXT), (refused.status, refused.data['error']['message']))

    def test_a_person_who_is_not_an_agent_gains_nothing_and_an_ungranted_agent_cannot_approve(self):
        task, contribution = self.deliver(self.lane_c)
        self.assertEqual(403, self.approve(self.other, task, contribution).status)      # Egret holds no grant
        self.assertEqual(403, self.approve(self.lane_c, task, contribution).status)     # a credential has no approve
        self.assertEqual('awaiting-review', self.state_of(task))


class WebFormTests(PinBase):
    """The web approve form shows the server's word for shapes (1) and (3), through the task page under Node."""

    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.member('cora', 'owner', token=self.admin).status)

    def test_the_form_answers_for_shapes_1_and_3(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        lane_c, _ = self.worker_of('cora', 'lane-c')
        no_name, _ = self.worker_of('cora', None)
        self.service.approval_by_another_party = True
        answers = {}
        for shape, token in (('1', lane_c), ('3', no_name)):
            task, contribution = self.deliver(token)
            refused = self.approve(self.tokens['cora'], task, contribution)       # the person at the form
            self.assertEqual((403, API.OWN_PARTY + API.OWN_PARTY_NEXT), (refused.status, refused.data['error']['message']))
            answers[shape] = {'status': refused.status, 'message': refused.data['error']['message']}
        self.service.approval_by_another_party = False
        task, contribution = self.deliver(lane_c)
        accepted = self.approve(self.tokens['cora'], task, contribution)
        self.assertEqual(201, accepted.status, accepted.data)
        answers['ok'] = {'status': accepted.status, 'message': ''}
        done = test_http_web.run_node_module(
            self, node, 'await import(process.argv[1])',
            (KIT / 'tests/web_agent_approval_credentials_form.mjs').as_uri(),
            (KIT / 'tests/web_dom_shim.mjs').as_uri(), (KIT / 'web/js/views/task.js').as_uri(), json.dumps(answers))
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout.strip().splitlines()[-1])
        for shape in ('1', '3'):
            with self.subTest(shape=shape):
                self.assertEqual({'hidden': False, 'text': answers[shape]['message'], 'refused': '403', 'renders': 0}, out[shape])
        self.assertEqual({'hidden': True, 'text': '', 'refused': None, 'renders': 1}, out['ok'])


def only_its_own_tests(cls):
    """Borrows the real stack of test_coordinator_grant_real and runs none of that file's tests again."""
    for base in test_coordinator_grant_real.RealStackTests.__mro__:
        for name in dir(base):
            if name.startswith('test_') and name not in cls.__dict__:
                setattr(cls, name, None)
    return cls


@unittest.skipIf(test_coordinator_grant_real.rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(test_coordinator_grant_real.rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@only_its_own_tests
class RealBdTests(test_coordinator_grant_real.RealStackTests):
    """The real web service, the real endpoint.py and a real bd: shapes (1), (3) and (8) through the approve route."""

    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.request('PUT', '/v1/projects/pp/agents/%s/coordinator' % self.agent_id, {},
                                           token=self.tokens['alex']).status)
        self.assertEqual(200, self.request('PUT', '/v1/projects/pp/members/%s' % self.ids['cora'], {'role': 'owner'},
                                           token=self.admin_token()).status)

    def worker(self, name, actor):
        made = self.request('POST', '/v1/projects/pp/worker-credentials', {'label': 'worker', 'actor': actor} if actor
                            else {'label': 'worker'}, token=self.tokens['cora'])
        self.assertEqual(201, made.status, made.data)
        self.tokens[name] = made.data['credential']['secret']

    def test_shape_1_a_named_worker_credential_is_approved_and_bd_holds_the_basis(self):
        self.worker('lane', 'lane-c')
        task, contribution = self.deliver('lane')
        before = self.comments(task)
        approved = self.approve(self.agent, task, contribution)
        self.assertEqual(201, approved.status, approved.data)
        self.assertEqual('awaiting-integration', self.review(task)['state'])
        written = self.comments(task)[len(before):]
        self.assertEqual([self.agent_actor], [author for author, _ in written], written)
        self.assertIn(WORKER_CREDENTIAL_BASIS, written[0][1])
        self.assertNotIn(AGENT_BASIS, written[0][1])

    def test_shape_3_the_bare_account_id_is_refused_with_its_own_sentence_and_bd_writes_nothing(self):
        self.worker('plain', None)
        task, contribution = self.deliver('plain')
        before = self.comments(task)
        refused = self.approve(self.agent, task, contribution)
        self.assertEqual((403, N3), (refused.status, refused.data['error']['message']), refused.data)
        self.assertEqual((before, 'awaiting-review'), (self.comments(task), self.review(task)['state']))

    def test_shape_8_two_agents_of_one_person_are_approved_as_before_with_the_agent_basis(self):
        made = self.request('POST', '/v1/agents', {'name': 'Egret', 'working_directory': '/home/x/egret',
                                                   'projects': ['pp']}, token=self.tokens['cora'])
        self.assertEqual(201, made.status, made.data)
        self.tokens['egret'] = made.data['credential']['secret']
        task, contribution = self.deliver('egret')
        before = self.comments(task)
        approved = self.approve(self.agent, task, contribution)
        self.assertEqual(201, approved.status, approved.data)
        written = self.comments(task)[len(before):]
        self.assertEqual([self.agent_actor], [author for author, _ in written], written)
        self.assertIn(AGENT_BASIS, written[0][1])
        self.assertNotIn(WORKER_CREDENTIAL_BASIS, written[0][1])


if __name__ == '__main__':
    unittest.main()
