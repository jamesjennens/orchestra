"""kittrial-5bb.560: integration follows the project's agent-approval setting, on the real stack.

The approve route lets a coordinator agent approve ANOTHER agent of its person, or a worker credential
only that person held, while the project's setting is ``by-agent`` (kittrial-5bb.438, .519). Integration
used to refuse every such approval by party, so it was recorded and could never be integrated. Now the
integration-check row and every reviewed/integrated writer ask the same decision
(``http_authority.by_agent_approval``), from the present state, when the party sets meet.

The real web service, the real endpoint.py and a real bd (the stack of tests/test_claim_held.py, through
tests/test_coordinator_grant_real.py): alex owns ``pp``; cora is an owner too (so she can issue worker
credentials) and her agent Heron holds the coordinator grant; Egret is cora's second agent. Every case
delivers, approves through the real route where it allows it, reads GET .../integration-check, and tries
the integrated writer (POST .../lifecycle/facts/integrated) with the task's merge slot held, so the party
check is the only thing that can refuse. Where the route refuses, the approval is written the way the
route writes it, through the host endpoint under the agent's name, to show what the row and the writer
make of it.

Skipped where the endpoint cannot run (it imports fcntl: Windows) or there is no real bd. The decision
itself is tested everywhere by tests/test_by_agent_decision_560.py and tests/test_web_lifecycle_by_agent_560.py.
"""
import secrets
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import test_claim_held as held
import test_coordinator_grant_real as grant_real
import test_http_review_fixes as fixes

API = http_service.ApiHandler
rb = grant_real.rb
OTHER_PARTY = 'The approval must be from another party than the author and current assignee'
SCOPE = dict(source_commit=fixes.COMMIT, integration_commit='e' * 40, release_id='release-560',
             environment='integration-560')
STRICT_ROUTE = API.OWN_PARTY_AGENT[0] + API.OWN_PARTY_AGENT[3]
OWN_WORK = API.SAME_AGENT[0] + API.SAME_AGENT_NEXT + API.BY_AGENT_NEXT
SUB_WORK = API.SAME_AGENT[0] + API.SUB_WORKER_NEXT + API.BY_AGENT_NEXT
ACCOUNT_ID = API.ACCOUNT_ID_AUTHOR + API.BY_AGENT_NEXT
HELD_ASSIGNEE = API.OWN_PARTY_AGENT[2] + API.BY_AGENT_NEXT


def only_its_own_tests(cls):
    """Borrows the real stack and runs none of the borrowed files' tests again."""
    for base in grant_real.RealStackTests.__mro__:
        for name in dir(base):
            if name.startswith('test_') and name not in cls.__dict__:
                setattr(cls, name, None)
    return cls


@unittest.skipIf(rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@only_its_own_tests
class AgentApprovalIntegration(grant_real.RealStackTests):

    def setUp(self):
        super().setUp()
        self.assertEqual(200, self.grant().status)
        made = self.request('PUT', '/v1/projects/pp/members/%s' % self.ids['cora'], {'role': 'owner'},
                            token=self.admin_token())
        self.assertEqual(200, made.status, made.data)
        egret = self.request('POST', '/v1/agents', {'name': 'Egret', 'working_directory': '/home/x/egret',
                                                    'projects': ['pp']}, token=self.tokens['cora'])
        self.assertEqual(201, egret.status, egret.data)
        self.tokens['egret'], self.egret_id = egret.data['credential']['secret'], egret.data['agent']['id']
        self.tokens['heron'] = self.agent
        self.suffix = secrets.token_hex(3)

    # -- the parties -----------------------------------------------------------------------------
    def worker(self, name, actor, issuer='cora'):
        # A name the shared tracker has seen in an earlier test is refused: each test takes its own.
        actor = actor and (actor if actor.endswith(self.suffix) else '%s-%s' % (actor, self.suffix))
        body = {'label': 'worker', 'actor': actor} if actor else {'label': 'worker'}
        made = self.request('POST', '/v1/projects/pp/worker-credentials', body, token=self.tokens[issuer])
        self.assertEqual(201, made.status, made.data)
        self.tokens[name] = made.data['credential']['secret']
        return made.data['credential']

    def deliver(self, who, actor=None):
        """A task claimed and delivered under ``who``'s token (and ``actor``, a label below it)."""
        self.count += 1
        task = self.new('work %d' % self.count)
        claimed = self.request('POST', '%s/%s/claim' % (self.tasks, task), {'actor': actor} if actor else {},
                               token=self.tokens[who])
        self.assertEqual(200, claimed.status, claimed.data)
        body = dict(fixes.CONTRIBUTION, operation='contribute', schema_version=1, previous=None,
                    operation_id='op-deliver-%d-%s' % (self.count, secrets.token_hex(3)))
        if actor:
            body['actor'] = actor
        made = self.request('POST', '%s/%s/reviews' % (self.tasks, task), body, token=self.tokens[who])
        self.assertEqual(201, made.status, made.data)
        return task, self.review(task)['contribution']['id']

    def set_mode(self, mode):
        changed = self.request('PUT', '/v1/projects/pp/agent-approval', {'agent_approval': mode},
                               token=self.tokens['alex'])
        self.assertEqual(200, changed.status, changed.data)

    def change_state(self, change):
        """Edit the service state the endpoint reads, as an operator's repair or the clock would."""
        with self.service.store.lock:
            change(self.service.state)
            self.service.store.save()

    # -- the route, the queue, the row and the writer ----------------------------------------------
    def route(self, task, contribution):
        approved = self.approve(self.agent, task, contribution)
        return approved.status, held.message(approved)

    def write_approval(self, task, contribution):
        """Heron's approve record written by the real route, the real endpoint and bd with the route's party
        rule taken out: what a route of another day (or before kittrial-5bb.438) wrote. No basis sentence, no
        new field; the endpoint still authorizes Heron's credential from the present state."""
        with mock.patch.object(API, '_own_party_refusal', return_value=None):
            return self.approve(self.agent, task, contribution)

    def offered(self, task):
        nxt = self.request('GET', '/v1/agents/me/next', token=self.agent)
        self.assertEqual(200, nxt.status, nxt.data)
        return any(action.get('task') == task and action.get('kind') == 'to-review'
                   for action in nxt.data.get('next_actions') or [])

    def row(self, task):
        checked = self.request('GET', '%s/%s/integration-check' % (self.tasks, task), token=self.tokens['alex'])
        self.assertEqual(200, checked.status, checked.data)
        return next(check for check in checked.data['checks'] if check['check'] == 'approval')

    def scoped(self, task):
        """A lifecycle scope on the approved commit."""
        state = self.request('GET', '%s/%s/lifecycle' % (self.tasks, task), token=self.tokens['alex'])
        self.assertEqual(200, state.status, state.data)
        if state.data['scope'] != SCOPE:
            scope = self.request('POST', '%s/%s/lifecycle/scope' % (self.tasks, task),
                                 dict(scope=dict(SCOPE), expected_scope_token=state.data['scope_token'],
                                      evidence=['scope 560'], provenance='performed'),
                                 token=self.tokens['alex'], key='scope-' + secrets.token_hex(6))
            self.assertEqual(201, scope.status, scope.data)

    def with_slot(self, task, act):
        """``act()`` with the task's merge slot held, released after."""
        taken = self.request('POST', '/v1/projects/pp/merge-slot/acquire', dict(task_id=task, target='main'),
                             token=self.tokens['alex'], key='slot-' + secrets.token_hex(6))
        self.assertEqual(200, taken.status, taken.data)
        try:
            return act()
        finally:
            released = self.request('POST', '/v1/projects/pp/merge-slot/release', {}, token=self.tokens['alex'],
                                    key='release-' + secrets.token_hex(6))
            self.assertEqual(200, released.status, released.data)

    def fact(self, task, dimension, value='failed'):
        written = self.request('POST', '%s/%s/lifecycle/facts/%s' % (self.tasks, task, dimension),
                               dict(scope=dict(SCOPE), value=value, evidence=['%s 560' % dimension],
                                    provenance='performed'), token=self.tokens['alex'],
                               key='fact-' + secrets.token_hex(6))
        return written.status, held.message(written)

    def lifecycle(self, task):
        state = self.request('GET', '%s/%s/lifecycle' % (self.tasks, task), token=self.tokens['alex'])
        self.assertEqual(200, state.status, state.data)
        return state.data

    def writer(self, task):
        """The integrated writer, with a scope on the approved commit and the task's merge slot held."""
        self.scoped(task)
        return self.with_slot(task, lambda: self.fact(task, 'integrated'))

    def integrates(self, task):
        row = self.row(task)
        self.assertEqual((True, None), (row['allowed'], row['reason']), row)
        self.assertEqual((201, ''), self.writer(task))

    def refuses(self, task, sentence=OTHER_PARTY):
        row = self.row(task)
        self.assertEqual((False, sentence), (row['allowed'], row['reason']), row)
        self.assertEqual((409, sentence), self.writer(task))

    def recorded(self, task, contribution):
        written = self.write_approval(task, contribution)
        self.assertEqual(201, written.status, written.data)
        self.assertEqual('awaiting-integration', self.review(task)['state'])

    # -- the cases -------------------------------------------------------------------------------
    def test_by_agent_a_sibling_agents_delivery_is_approved_and_integrated(self):
        task, contribution = self.deliver('egret')
        self.assertEqual((201, ''), self.route(task, contribution))
        self.integrates(task)

    def test_by_person_the_route_refuses_and_a_setting_switched_after_approving_refuses_at_integration(self):
        approved, contribution = self.deliver('egret')
        self.assertEqual((201, ''), self.route(approved, contribution))
        self.set_mode('by-person')
        task, other = self.deliver('egret')
        self.assertEqual((403, STRICT_ROUTE), self.route(task, other))
        self.refuses(approved)
        self.set_mode('by-agent')
        self.integrates(approved)

    def test_the_agents_own_delivery_and_its_sub_label_refuse(self):
        for actor, sentence in ((None, OWN_WORK), (self.agent_actor + '/w1', SUB_WORK)):
            with self.subTest(actor=actor):
                task, contribution = self.deliver('heron', actor)
                self.assertEqual((403, sentence), self.route(task, contribution))
                self.write_approval(task, contribution)
                row = self.row(task)
                self.assertFalse(row['allowed'], row)
                status, said = self.writer(task)
                self.assertEqual((409, row['reason']), (status, said))

    def test_the_persons_own_delivery_refuses_under_the_session_and_the_bare_account_id(self):
        self.worker('plain', None)
        for who in ('cora', 'plain'):
            with self.subTest(who=who):
                task, contribution = self.deliver(who)
                self.assertEqual(self.ids['cora'], self.review(task)['contribution']['author'])
                self.assertEqual((403, ACCOUNT_ID), self.route(task, contribution))
                self.recorded(task, contribution)
                self.refuses(task)

    def test_worker_credentials_a_shared_name_refuses_and_a_name_only_the_person_held_integrates(self):
        once = self.worker('alex-lane', 'lane-s', issuer='alex')
        revoked = self.request('POST', '/v1/projects/pp/worker-credentials/%s/revoke' % once['id'], {},
                               token=self.tokens['alex'])
        self.assertEqual(204, revoked.status, revoked.data)
        self.worker('shared', 'lane-s')
        task, contribution = self.deliver('shared')
        status, said = self.route(task, contribution)
        self.assertEqual(403, status)
        self.assertIn('lane-s-' + self.suffix, said)
        self.recorded(task, contribution)
        self.refuses(task)
        self.worker('lane', 'lane-c')
        task, contribution = self.deliver('lane')
        self.assertEqual((201, ''), self.route(task, contribution))
        self.assertIn(API.WORKER_CREDENTIAL_APPROVAL, self.comments(task)[-1][1])
        self.integrates(task)

    def test_a_grant_that_ended_after_approving_refuses_at_integration(self):
        revoked, contribution = self.deliver('egret')
        self.assertEqual((201, ''), self.route(revoked, contribution))
        expired, contribution = self.deliver('egret')
        self.assertEqual((201, ''), self.route(expired, contribution))
        self.assertEqual(200, self.grant('DELETE').status)
        self.refuses(revoked)
        self.assertEqual(200, self.grant().status)
        self.integrates(expired)
        # The agent's credential expires against the server's own expiry clock.
        moment = self.service._expiry_now()
        self.change_state(lambda state: [credential.update(expires_at=moment - 1)
                                         for credential in state['credentials'].values()
                                         if credential.get('agent_id') == self.agent_id])
        self.refuses(revoked)

    def test_a_damaged_setting_refuses_at_integration(self):
        task, contribution = self.deliver('egret')
        self.assertEqual((201, ''), self.route(task, contribution))
        self.change_state(lambda state: state.setdefault('agent_approval', {}).update(pp='damaged'))
        self.assertTrue(self.service.agent_approval('pp').get('damaged'))
        self.refuses(task)

    def test_the_assignee_rule_still_applies(self):
        task, contribution = self.deliver('egret')
        self.assertEqual((201, ''), self.route(task, contribution))
        moved = self.bd('update', task, '--assignee', self.ids['cora'], '--json')
        self.assertEqual(0, moved.returncode, moved.stderr)
        self.refuses(task)
        other, contribution = self.deliver('egret')
        moved = self.bd('update', other, '--assignee', self.ids['cora'], '--json')
        self.assertEqual(0, moved.returncode, moved.stderr)
        self.assertEqual((403, HELD_ASSIGNEE), self.route(other, contribution))

    def test_a_legacy_approval_integrates_when_the_decision_holds(self):
        task, contribution = self.deliver('egret')
        self.recorded(task, contribution)
        self.assertNotIn(API.SAME_PERSON_APPROVAL, self.comments(task)[-1][1])
        self.integrates(task)
        self.set_mode('by-person')
        self.refuses(task)

    def test_the_lifecycle_read_and_the_reviewed_writer_follow_the_decision(self):
        """r2 item s1: GET .../lifecycle (standing approval, affordances) and POST .../facts/reviewed ask
        the decision too, not only the check row and the integrated writer."""
        task, contribution = self.deliver('egret')
        self.assertEqual((201, ''), self.route(task, contribution))
        self.scoped(task)

        def read_and_review():
            state = self.lifecycle(task)
            return (state['standing_approval'] is not None, state['approval_reason'],
                    state['affordances']['reviewed']['allowed'], state['affordances']['reviewed']['reason'],
                    state['affordances']['integrated']['allowed'], state['affordances']['integrated']['reason'],
                    self.fact(task, 'reviewed'))
        self.assertEqual((True, None, True, None, True, None, (201, '')), self.with_slot(task, read_and_review))
        self.set_mode('by-person')
        self.assertEqual((False, OTHER_PARTY, False, OTHER_PARTY, False, OTHER_PARTY, (409, OTHER_PARTY)),
                         self.with_slot(task, read_and_review))

    def test_each_credential_shape_the_service_refuses_stops_integration(self):
        """r2 item m1: the grant counts only a credential the service would accept. Each shape is written
        into the state after the approval, and refuses at the row and the writer without an error;
        undoing it integrates again."""
        moment, raw = self.service._expiry_now(), self.service._raw_now()
        shapes = {
            'past the 366-day cap': dict(agent_written_raw=moment - 367 * 86400, agent_issued_raw=moment - 367 * 86400),
            'expires_at null': dict(expires_at=None),
            'expires_at NaN': dict(expires_at=float('nan')),
            'issued to another account': dict(user_id=self.ids['alex']),
            'raw deadline passed': dict(agent_raw_expires_at=raw - 1),
        }
        task, contribution = self.deliver('egret')
        self.assertEqual((201, ''), self.route(task, contribution))
        def mine(state):
            return {key: c for key, c in state['credentials'].items() if c.get('agent_id') == self.agent_id}
        for label, change in shapes.items():
            with self.subTest(shape=label):
                kept = {}

                def spoil(state):
                    for key, credential in mine(state).items():
                        kept[key] = dict(credential)
                        credential.update(change)

                def restore(state):
                    for key, credential in mine(state).items():
                        credential.clear()
                        credential.update(kept[key])
                self.change_state(spoil)
                self.refuses(task)
                self.change_state(restore)
                self.integrates(task)

    def test_one_source_the_route_the_queue_offer_and_the_row_agree(self):
        self.worker('lane', 'lane-c')
        self.worker('plain', None)
        expected = {'egret': True, 'lane': True, 'plain': False, 'cora': False, 'casey': True}
        seen = {}
        for mode in ('by-agent', 'by-person'):
            self.set_mode(mode)
            for who, by_agent in expected.items():
                task, contribution = self.deliver(who)
                offered = self.offered(task)
                status, said = self.route(task, contribution)
                if status != 201:
                    self.recorded(task, contribution)
                row = self.row(task)
                seen[mode, who] = (offered, status == 201, row['allowed'])
        want = {}
        for mode in ('by-agent', 'by-person'):
            for who, by_agent in expected.items():
                allowed = by_agent if mode == 'by-agent' else who == 'casey'
                want[mode, who] = (allowed, allowed, allowed)
        self.assertEqual(want, seen)


if __name__ == '__main__':
    unittest.main()
