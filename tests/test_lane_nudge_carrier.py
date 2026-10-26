"""The lane nudge carrier (kittrial-5bb.415): slice 2 of docs/design/LANE_NUDGE_DESIGN.md.

An AUTHORIZATION change, so every rule is a test through the real routes of the in-process web service.
A carrier is an agent credential of an account whose scopes are exactly ``nudges``: it lists, claims and
answers the nudge requests of its OWN account's lanes and sends a heartbeat, and can do nothing else.
Each test names its principal; carl is the account whose lane is nudged, dave another account, olive an
owner, cora a coordinator.
"""
import json
import re
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))

import http_authority
import http_service
import nudge_messages
import nudges
import test_lane_nudges as base

NOTE = base.NOTE


class CarrierCase(base.NudgeCase):
    def carrier(self, name='carl', label='Carrier', scopes=('nudges',), projects=None):
        made = self.request('POST', '/v1/agents', {'name': label, 'working_directory': '/home/x/' + label,
                                                   'projects': projects or [self.project], 'scopes': list(scopes)},
                            token=self.tokens[name])
        self.assertEqual(201, made.status, made.data)
        return made.data['credential']['secret'], made.data['agent']['id']

    def ask(self, lane=None, token=None, message='resume', **more):
        self.later(1000)
        answer = self.nudge(token or self.tokens['olive'], lane, message=message, note=NOTE, **more)
        self.assertEqual(201, answer.status, answer.data)
        return answer.data['nudge']['id']

    def mine(self, token):
        answer = self.request('GET', '/v1/me/nudges', token=token)
        self.assertEqual(200, answer.status, answer.data)
        return answer.data['items']

    def claim(self, token, nudge_id, key=None):
        return self.request('POST', '/v1/me/nudges/%s/claim' % nudge_id, {}, token=token, key=key)

    def answer(self, token, nudge_id, outcome='delivered', **more):
        return self.request('POST', '/v1/me/nudges/%s/outcome' % nudge_id, dict({'outcome': outcome}, **more), token=token)

    def beat(self, token, lanes, kind='supervisor'):
        return self.request('POST', '/v1/me/carrier', {'kind': kind, 'lanes': lanes}, token=token)

    def state(self, nudge_id):
        return next(r for r in self.records() if r['id'] == nudge_id)


class ScopeTests(CarrierCase):
    def test_the_scope_is_never_combined_with_another(self):
        for scopes in (['nudges', 'tasks'], ['read', 'nudges'], ['nudges', 'read', 'feedback']):
            with self.subTest(scopes=scopes):
                self.assertEqual(422, self.request('POST', '/v1/agents', {'name': 'Mix', 'working_directory': '/home/x/m',
                                                                          'projects': [self.project], 'scopes': scopes},
                                                   token=self.tokens['carl']).status)
        _secret, agent_id = self.carrier()
        renewed = self.request('POST', '/v1/agents/%s/credentials' % agent_id, {'scopes': ['nudges', 'tasks']},
                               token=self.tokens['carl'])
        self.assertEqual(422, renewed.status, renewed.data)
        worker = self.request('POST', '/v1/projects/%s/worker-credentials' % self.project,
                              {'label': 'w', 'scopes': ['nudges']}, token=self.tokens['olive'])
        self.assertEqual(422, worker.status, worker.data)

    def test_a_credential_that_is_mixed_anyway_holds_nothing(self):
        secret, agent_id = self.carrier()
        for credential in self.service.state['credentials'].values():
            if credential.get('agent_id') == agent_id:
                credential['scopes'] = ['nudges', 'tasks', 'read']
        self.assertEqual(403, self.request('GET', '/v1/me/nudges', token=secret).status)
        self.assertEqual(403, self.request('GET', '/v1/projects/%s/tasks' % self.project, token=secret).status)
        mixed = [c for c in self.service.state['credentials'].values() if c.get('agent_id') == agent_id][0]
        issuer = self.service.state['users'][mixed['user_id']]
        self.assertEqual(frozenset(), http_authority.credential_capabilities(self.service.state, mixed, issuer, self.project))

    def test_decide_takes_exactly_the_nudges_scope_of_an_enabled_agent(self):
        _secret, agent_id = self.carrier()
        credential_id, credential = next((key, value) for key, value in self.service.state['credentials'].items()
                                         if value.get('agent_id') == agent_id)
        ask = {'via': 'credential', 'user_id': credential['user_id'], 'credential_id': credential_id,
               'project': None, 'capability': 'nudge.carry'}

        def decided():
            try:
                return http_authority.decide(self.service.state, ask)['role']
            except http_authority.AuthorityDenied as denied:
                return denied.status
        self.assertEqual('carrier', decided())
        for scopes in (['nudges', 'tasks'], ['tasks'], ['nudges', 'nudges'], ['read', 'nudges'], []):
            with self.subTest(scopes=scopes):
                credential['scopes'] = scopes
                self.assertEqual(403, decided())
        credential['scopes'] = ['nudges']
        self.service.state['agents'][agent_id]['enabled'] = False
        self.assertEqual(401, decided())
        self.service.state['agents'][agent_id]['enabled'] = True
        credential['agent_id'] = None
        self.assertEqual(401, decided())

    def test_the_scope_grants_one_capability_that_no_role_holds_and_no_baseline_read(self):
        self.assertEqual(frozenset({'nudge.carry'}), http_authority.SCOPE_CAPABILITIES['nudges'])
        for role, held in http_authority.ROLE_CAPABILITIES.items():
            self.assertNotIn('nudge.carry', held, role)
        secret, _agent_id = self.carrier()
        for path in ('/v1/projects/%s' % self.project, '/v1/projects/%s/tasks' % self.project,
                     '/v1/projects/%s/lanes' % self.project, '/v1/projects/%s/nudges' % self.project):
            with self.subTest(path=path):
                self.assertIn(self.request('GET', path, token=secret).status, (403, 404))

    def test_a_session_and_an_ordinary_credential_are_not_a_carrier(self):
        other, _ = self.agent_with_id('carl', 'Plain')
        for who, token in (('a session', self.tokens['carl']), ('an agent with the default scopes', other),
                           ('the coordinator agent', self.agent)):
            with self.subTest(who=who):
                self.assertEqual(403, self.request('GET', '/v1/me/nudges', token=token).status)
                self.assertEqual(403, self.beat(token, []).status)

    def test_every_other_route_refuses_a_carrier_credential(self):
        secret, _agent_id = self.carrier()
        swept, open_routes = 0, []
        for verb, pattern, name, anonymous, _csrf in http_service.ROUTES:
            if anonymous or name in http_service.CARRIER_ROUTES:
                continue
            path = fill(pattern)
            with self.subTest(route=name):
                self.assertIsNotNone(path, 'no sample path matches %s' % pattern.pattern)
                answer = self.request(verb, path, {} if verb != 'GET' else None, token=secret)
                swept += 1
                if answer.status not in (403, 404):
                    open_routes.append((name, answer.status))
                self.assertEqual(403, answer.status, '%s answered %s' % (name, answer.status))
        self.assertGreater(swept, 100)
        self.assertEqual([], open_routes)
        self.assertEqual({'me_nudges', 'me_carrier', 'me_nudge_claim', 'me_nudge_outcome'}, http_service.CARRIER_ROUTES)


SAMPLES = ('a1', '1', 'abc123', 'a.b', 'a.b-c', 'p-' + '0' * 12, 'nudge-' + '0' * 24, '0' * 8, 'usr_' + '0' * 16, 'cred_' + '0' * 16,
           'a' * 40, 'a.b.c', 'f' * 64)


def fill(pattern):
    """A path the route pattern matches, each named group given the first sample its own expression accepts."""
    source, text, position = pattern.pattern.strip('^$'), '', 0
    while position < len(source):
        if not source.startswith('(?P<', position):
            text += source[position]
            position += 1
            continue
        depth, end = 0, position
        while True:
            if source[end] == '\\':
                end += 2
                continue
            depth += 1 if source[end] == '(' else -1 if source[end] == ')' else 0
            end += 1
            if depth == 0:
                break
        expression = source[position:end].split('>', 1)[1][:-1]
        sample = next((item for item in SAMPLES if re.fullmatch(expression, item)), None)
        if sample is None:
            return None
        text += sample
        position = end
    return text if pattern.match(text) else None


class CarrierReadTests(CarrierCase):
    def test_a_carrier_lists_only_its_own_accounts_lanes_and_only_five_fields(self):
        secret, _ = self.carrier('carl')
        self.ask(self.ids['dave'])
        mine = self.ask(self.ids['carl'])
        items = self.mine(secret)
        self.assertEqual([mine], [item['id'] for item in items])
        self.assertEqual({'id', 'project', 'lane', 'message', 'message_set'}, set(items[0]))
        self.assertEqual((self.ids['carl'], 'resume', nudge_messages.MESSAGE_SET), (items[0]['lane'], items[0]['message'],
                                                                                    items[0]['message_set']))

    def test_a_carrier_never_reads_a_reason_a_note_or_the_requester(self):
        secret, _ = self.carrier('carl')
        nudge_id = self.ask(self.ids['carl'])
        shown = json.dumps([self.request('GET', '/v1/me/nudges', token=secret).data,
                            self.claim(secret, nudge_id).data])
        for forbidden in (NOTE, 'stalled-at-prompt', 'olive', self.ids['olive'], 'requested_by'):
            self.assertNotIn(forbidden, shown)

    def test_another_accounts_carrier_cannot_claim_or_answer_this_lane(self):
        thief, _ = self.carrier('dave', 'Thief')
        nudge_id = self.ask(self.ids['carl'])
        self.assertEqual([], self.mine(thief))
        self.assertEqual(404, self.claim(thief, nudge_id).status)
        self.assertEqual(404, self.answer(thief, nudge_id).status)
        self.assertEqual('pending', self.state(nudge_id)['state'])

    def test_an_ambiguous_worker_namespace_is_nobodys_lane(self):
        # Two accounts hold worker credentials for one label: the label belongs to both, so neither carrier
        # may read or answer it (the parties are not the carrier's account alone).
        carl, _ = self.carrier('carl', 'One')
        dave, _ = self.carrier('dave', 'Two')
        shared = self.ask(self.ids['carl'])
        for number, who in enumerate(('carl', 'dave')):
            self.service.state['credentials']['shared-%d' % number] = {
                'user_id': self.ids[who], 'project_id': self.project, 'actor': 'shared-w'}
        self.state(shared)['lane'] = 'shared-w'
        for token in (carl, dave):
            self.assertEqual([], self.mine(token))
            self.assertEqual(404, self.claim(token, shared).status)
            self.assertEqual(404, self.answer(token, shared).status)

    def test_a_carrier_whose_account_left_the_project_reads_and_answers_nothing(self):
        secret, _ = self.carrier('carl')
        listed = self.ask(self.ids['carl'])
        claimed = self.ask(self.ids['carl'])
        self.assertEqual(200, self.claim(secret, claimed).status)
        self.assertEqual(200, self.request('DELETE', '/v1/projects/%s/members/%s' % (self.project, self.ids['carl']),
                                           token=self.tokens['olive']).status)
        self.assertEqual([], self.mine(secret))
        for nudge_id in (listed, claimed):
            self.assertEqual(404, self.claim(secret, nudge_id).status)
            self.assertEqual(404, self.answer(secret, nudge_id).status)
        self.assertEqual('claimed', self.state(claimed)['state'])

    def test_a_carrier_whose_account_already_left_is_not_counted_as_one_at_the_end(self):
        secret, _ = self.carrier('carl')
        nudge_id = self.ask(self.ids['carl'])
        self.assertEqual(200, self.request('DELETE', '/v1/projects/%s/members/%s' % (self.project, self.ids['carl']),
                                           token=self.tokens['olive']).status)
        self.later(nudges.PENDING_TTL - 10)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.later(20)
        self.assertNotIn('carrier_seen_ts', self.state(nudge_id))
        self.assertEqual('not-deliverable', self.request('GET', '/v1/projects/%s/nudges' % self.project,
                                                         token=self.tokens['olive']).data['items'][0]['state'])

    def test_account_in_project_is_a_live_enabled_member_or_superuser(self):
        state = self.service.state
        carl = self.ids['carl']
        self.assertTrue(http_authority.account_in_project(state, carl, self.project))
        self.assertFalse(http_authority.account_in_project(state, self.ids['otto'], self.project))
        self.assertFalse(http_authority.account_in_project(state, carl, 'no-such-project'))
        self.assertFalse(http_authority.account_in_project(state, 'no-such-user', self.project))
        state['users'][carl]['disabled'] = True
        self.assertFalse(http_authority.account_in_project(state, carl, self.project))
        state['users'][carl]['disabled'] = False
        state['users'][self.ids['otto']]['superuser'] = True
        self.assertTrue(http_authority.account_in_project(state, self.ids['otto'], self.project))
        state['users'][self.ids['otto']]['disabled'] = True
        self.assertFalse(http_authority.account_in_project(state, self.ids['otto'], self.project))

    def test_a_carrier_reaches_only_projects_its_agent_is_granted(self):
        secret, agent_id = self.carrier('carl')
        nudge_id = self.ask(self.ids['carl'])
        self.service.state['agents'][agent_id]['projects'] = []
        self.assertEqual([], self.mine(secret))
        self.assertEqual(404, self.claim(secret, nudge_id).status)

    def test_a_disabled_agent_and_a_revoked_credential_are_refused(self):
        secret, agent_id = self.carrier('carl')
        self.service.state['agents'][agent_id]['enabled'] = False
        self.assertEqual(401, self.request('GET', '/v1/me/nudges', token=secret).status)


class ClaimAndOutcomeTests(CarrierCase):
    def test_claim_then_one_outcome_and_the_board_shows_it(self):
        secret, _ = self.carrier('carl')
        nudge_id = self.ask(self.ids['carl'])
        claimed = self.claim(secret, nudge_id)
        self.assertEqual(200, claimed.status, claimed.data)
        self.assertEqual(('claimed', True), (claimed.data['state'], claimed.data['claimed']))
        self.assertEqual(200, self.answer(secret, nudge_id, 'delivered').status)
        self.assertEqual('delivered', self.state(nudge_id)['state'])
        self.assertEqual('delivered', next(row for row in self.lanes(self.tokens['olive']).data['items']
                                           if row['actor'] == self.ids['carl'])['nudge']['state'])
        self.assertEqual([], self.mine(secret))
        self.assertEqual(['committed'], [e['outcome'] for e in self.events('lanes.nudge.claim')])
        self.assertEqual(['committed'], [e['outcome'] for e in self.events('lanes.nudge.outcome')])

    def test_a_second_outcome_is_refused_and_the_first_stands(self):
        secret, _ = self.carrier('carl')
        nudge_id = self.ask(self.ids['carl'])
        self.claim(secret, nudge_id)
        self.assertEqual(200, self.answer(secret, nudge_id, 'failed', detail='not-at-prompt').status)
        for outcome in ('delivered', 'restarted'):
            self.assertEqual(409, self.answer(secret, nudge_id, outcome).status)
        record = self.state(nudge_id)
        self.assertEqual(('failed', 'not-at-prompt'), (record['state'], record['outcome_detail']))

    def test_an_outcome_after_expiry_is_refused(self):
        secret, _ = self.carrier('carl')
        nudge_id = self.ask(self.ids['carl'])
        self.claim(secret, nudge_id)
        self.later(nudges.PENDING_TTL + 1)
        self.assertEqual(409, self.answer(secret, nudge_id, 'delivered').status)
        self.assertEqual('expired', next(i for i in self.request('GET', '/v1/projects/%s/nudges' % self.project,
                                                                  token=self.tokens['olive']).data['items']
                                         if i['id'] == nudge_id)['state'])

    def test_an_outcome_needs_a_claim_and_a_claimer(self):
        first, _ = self.carrier('carl', 'First')
        second, _ = self.carrier('carl', 'Second')
        nudge_id = self.ask(self.ids['carl'])
        self.assertEqual(409, self.answer(first, nudge_id).status)
        self.assertEqual(200, self.claim(first, nudge_id).status)
        self.assertEqual(409, self.claim(second, nudge_id).status)
        self.assertEqual(403, self.answer(second, nudge_id).status)
        self.assertEqual('claimed', self.state(nudge_id)['state'])
        self.assertEqual([], self.mine(second))

    def test_a_lapsed_claim_is_pending_again_for_another_carrier(self):
        first, _ = self.carrier('carl', 'First')
        second, _ = self.carrier('carl', 'Second')
        nudge_id = self.ask(self.ids['carl'])
        self.claim(first, nudge_id)
        self.later(nudges.CLAIM_TTL + 1)
        self.assertEqual(409, self.answer(first, nudge_id).status)
        self.assertEqual([nudge_id], [item['id'] for item in self.mine(second)])
        self.assertEqual(200, self.claim(second, nudge_id).status)
        self.assertEqual(200, self.answer(second, nudge_id, 'restarted').status)

    def test_a_requester_who_lost_the_authority_has_the_request_cancelled_at_the_claim(self):
        secret, _ = self.carrier('carl')
        nudge_id = self.ask(self.ids['carl'], token=self.tokens['cora'])
        self.assertEqual(200, self.member('cora', 'contributor').status)
        claimed = self.claim(secret, nudge_id)
        self.assertEqual(200, claimed.status, claimed.data)
        self.assertEqual((False, 'cancelled', None), (claimed.data['claimed'], claimed.data['state'], claimed.data['nudge']))
        self.assertEqual(('cancelled', nudges.REQUESTER_LOST), (self.state(nudge_id)['state'], self.state(nudge_id)['outcome_detail']))
        self.assertEqual([], self.mine(secret))
        self.assertEqual(409, self.answer(secret, nudge_id).status)

    def test_an_agent_requester_whose_grant_was_taken_away_is_cancelled_too(self):
        self.granted_agent()
        secret, _ = self.carrier('carl')
        self.later(1000)
        made = self.nudge(self.agent, self.ids['carl'])
        self.assertEqual(201, made.status, made.data)
        self.assertEqual(200, self.grant(method='DELETE').status)
        self.assertEqual('cancelled', self.claim(secret, made.data['nudge']['id']).data['state'])

    def test_the_closed_outcomes_and_details_only(self):
        secret, _ = self.carrier('carl')
        nudge_id = self.ask(self.ids['carl'])
        self.claim(secret, nudge_id)
        for body in ({'outcome': 'done'}, {'outcome': 'failed'}, {'outcome': 'failed', 'detail': 'because'},
                     {'outcome': 'delivered', 'detail': 'not-at-prompt'}, {'outcome': 'delivered', 'text': 'hi'}, {}):
            with self.subTest(body=body):
                self.assertIn(self.request('POST', '/v1/me/nudges/%s/outcome' % nudge_id, body, token=secret).status, (400, 422))
        self.assertEqual('claimed', self.state(nudge_id)['state'])
        for outcome in ('unsupported', ):
            self.assertEqual(200, self.answer(secret, nudge_id, outcome).status)
        self.assertEqual('unsupported', self.state(nudge_id)['state'])

    def test_a_claim_sent_twice_with_one_key_is_one_claim(self):
        secret, _ = self.carrier('carl')
        nudge_id = self.ask(self.ids['carl'])
        first = self.claim(secret, nudge_id, key='claim-key-0001')
        second = self.claim(secret, nudge_id, key='claim-key-0001')
        self.assertEqual((200, 200), (first.status, second.status))
        self.assertEqual(1, len(self.events('lanes.nudge.claim')))


    def test_a_claim_sent_again_after_the_lease_went_to_another_carrier_is_not_replayed(self):
        first, _ = self.carrier('carl', 'First')
        second, second_id = self.carrier('carl', 'Second')
        nudge_id = self.ask(self.ids['carl'])
        self.assertTrue(self.claim(first, nudge_id, key='claim-key-0002').data['claimed'])
        self.later(nudges.CLAIM_TTL + 1)
        self.assertTrue(self.claim(second, nudge_id).data['claimed'])
        again = self.claim(first, nudge_id, key='claim-key-0002')
        self.assertEqual(409, again.status, again.data)
        self.assertEqual(second_id, self.state(nudge_id)['claimed_by'])

    def test_a_claim_sent_again_while_the_lease_is_held_is_replayed(self):
        secret, _ = self.carrier('carl')
        nudge_id = self.ask(self.ids['carl'])
        first = self.claim(secret, nudge_id, key='claim-key-0003')
        again = self.claim(secret, nudge_id, key='claim-key-0003')
        self.assertEqual((200, True), (again.status, again.data['claimed']))
        self.assertEqual(first.data, again.data)


class NoCarrierTests(CarrierCase):
    def lane_state(self, nudge_id):
        self.later(0)
        return next(i for i in self.request('GET', '/v1/projects/%s/nudges' % self.project,
                                            token=self.tokens['olive']).data['items'] if i['id'] == nudge_id)

    def test_with_no_carrier_seen_an_unclaimed_request_is_not_deliverable(self):
        nudge_id = self.ask(self.ids['carl'])
        self.later(nudges.PENDING_TTL + 1)
        shown = self.lane_state(nudge_id)
        self.assertEqual(('not-deliverable', 'no-carrier'), (shown['state'], shown['outcome_detail']))

    def test_a_carrier_seen_just_before_the_end_makes_it_expired_not_no_carrier(self):
        secret, _ = self.carrier('carl')
        nudge_id = self.ask(self.ids['carl'])
        self.later(nudges.PENDING_TTL - nudges.CARRIER_WINDOW // 2)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.later(nudges.PENDING_TTL)
        shown = self.lane_state(nudge_id)
        self.assertEqual(('expired', 'unclaimed'), (shown['state'], shown['outcome_detail']))

    def test_a_carrier_last_seen_long_before_is_no_carrier(self):
        secret, _ = self.carrier('carl')
        nudge_id = self.ask(self.ids['carl'])
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.later(nudges.PENDING_TTL + 1)
        self.assertEqual('not-deliverable', self.lane_state(nudge_id)['state'])

    def test_a_carrier_heard_only_long_after_the_request_ended_is_no_carrier_for_it(self):
        secret, _ = self.carrier('carl')
        nudge_id = self.ask(self.ids['carl'])
        self.later(nudges.PENDING_TTL + 1)
        self.assertEqual('not-deliverable', self.lane_state(nudge_id)['state'])
        self.later(3600)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        shown = self.lane_state(nudge_id)
        self.assertEqual(('not-deliverable', 'no-carrier'), (shown['state'], shown['outcome_detail']))

    def test_a_carrier_that_beat_all_along_counts_and_one_that_went_away_and_came_back_does_not(self):
        secret, _ = self.carrier('carl')
        steady = self.ask(self.ids['carl'])
        for _ in range(nudges.PENDING_TTL // nudges.CARRIER_POLL + 12):   # well past the request's end
            self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
            self.later(nudges.CARRIER_POLL)
        self.assertEqual(('expired', 'unclaimed'), tuple(self.lane_state(steady)[k] for k in ('state', 'outcome_detail')))
        gap = self.ask(self.ids['carl'])
        self.later(nudges.PENDING_TTL // 2)
        self.beat(secret, [self.ids['carl']])
        self.later(nudges.PENDING_TTL + 600)
        self.beat(secret, [self.ids['carl']])          # back after a gap far longer than the window
        self.later(1)
        self.assertEqual(('not-deliverable', 'no-carrier'), tuple(self.lane_state(gap)[k] for k in ('state', 'outcome_detail')))

    def beats_through_the_end(self, secret, extra=0):
        """A carrier that heartbeats every poll from now past the request's end (plus ``extra`` seconds)."""
        for _ in range((nudges.PENDING_TTL + extra) // nudges.CARRIER_POLL + 1):
            self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
            self.later(nudges.CARRIER_POLL)

    def board(self, nudge_id):
        return tuple(self.lane_state(nudge_id)[k] for k in ('state', 'outcome_detail'))

    def test_what_was_true_at_the_end_is_not_rewritten_by_a_pause_and_a_later_heartbeat(self):
        secret, _ = self.carrier('carl')
        first = self.ask(self.ids['carl'])
        self.beats_through_the_end(secret)
        self.assertEqual(('expired', 'unclaimed'), self.board(first))
        self.later(nudges.CARRIER_WINDOW + 30)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.assertEqual(('expired', 'unclaimed'), self.board(first))
        second = self.ask(self.ids['carl'])         # a new request settles (writes) the first
        self.assertEqual(('expired', 'unclaimed'), (self.state(first)['state'], self.state(first)['outcome_detail']))
        self.assertEqual('pending', self.state(second)['state'])

    def test_what_was_true_at_the_end_survives_the_account_leaving_or_being_disabled_afterwards(self):
        for how in ('leaves', 'disabled'):
            with self.subTest(how=how):
                secret, _ = self.carrier('carl', 'Carrier-' + how)
                nudge_id = self.ask(self.ids['carl'])
                self.beats_through_the_end(secret)
                if how == 'leaves':
                    self.assertEqual(200, self.request('DELETE', '/v1/projects/%s/members/%s' % (self.project, self.ids['carl']),
                                                       token=self.tokens['olive']).status)
                else:
                    self.service.state['users'][self.ids['carl']]['disabled'] = True
                self.assertEqual(('expired', 'unclaimed'), self.board(nudge_id))
                self.ask(self.ids['dave'])
                self.assertEqual('expired', self.state(nudge_id)['state'])
                self.member('carl', 'contributor')
                self.service.state['users'][self.ids['carl']]['disabled'] = False

    def test_what_was_true_at_the_end_survives_the_carriers_entry_being_cleaned_up_or_evicted(self):
        secret, _ = self.carrier('carl')
        nudge_id = self.ask(self.ids['carl'])
        self.beats_through_the_end(secret)
        self.service.state['nudge_carriers'].clear()       # the 24 h cleanup and the 64-entry bound both do this
        self.assertEqual(('expired', 'unclaimed'), self.board(nudge_id))
        self.later(2 * 86400)
        self.assertEqual(200, self.beat(secret, [self.ids['carl']]).status)
        self.assertEqual(('expired', 'unclaimed'), self.board(nudge_id))

    def test_a_carrier_for_another_lane_or_account_does_not_count(self):
        elsewhere, _ = self.carrier('dave', 'Other')
        mine, _ = self.carrier('carl', 'Mine')
        nudge_id = self.ask(self.ids['carl'])
        self.later(nudges.PENDING_TTL - 10)
        self.beat(elsewhere, [self.ids['carl']])          # dave's account does not own carl's lane
        self.beat(mine, [self.ids['dave']])               # carl's carrier does not say it reaches this lane
        self.later(20)
        self.assertEqual('not-deliverable', self.lane_state(nudge_id)['state'])

    def test_a_request_claimed_and_never_answered_is_expired(self):
        secret, _ = self.carrier('carl')
        nudge_id = self.ask(self.ids['carl'])
        self.claim(secret, nudge_id)
        self.later(nudges.PENDING_TTL + 1)
        self.assertEqual(('expired', 'unanswered'), tuple(self.lane_state(nudge_id)[k] for k in ('state', 'outcome_detail')))

    def test_the_heartbeat_is_checked_and_bounded(self):
        secret, agent_id = self.carrier('carl')
        for body in ({'kind': 'robot', 'lanes': []}, {'kind': 'bridge', 'lanes': 'x'}, {'kind': 'bridge', 'lanes': ['a b']},
                     {'kind': 'bridge', 'lanes': ['l%d' % n for n in range(nudges.CARRIER_LANES_MAX + 1)]},
                     {'kind': 'bridge', 'lanes': [], 'extra': 1}):
            with self.subTest(body=body):
                self.assertIn(self.request('POST', '/v1/me/carrier', body, token=secret).status, (400, 422))
        answer = self.beat(secret, ['b', 'a', 'a'], kind='bridge')
        self.assertEqual(200, answer.status, answer.data)
        self.assertEqual((nudges.CARRIER_POLL, nudge_messages.MESSAGE_SET), (answer.data['poll_seconds'], answer.data['message_set']))
        self.assertEqual({agent_id: ['a', 'b']}, {k: v['lanes'] for k, v in self.service.state['nudge_carriers'].items()})


class MessageTests(unittest.TestCase):
    def test_the_texts_are_one_line_of_plain_ascii_with_nothing_variable(self):
        self.assertEqual(('resume', 'restart'), tuple(nudge_messages.TEXTS))
        self.assertEqual(set(nudges.MESSAGES), set(nudge_messages.TEXTS))
        for key, text in nudge_messages.TEXTS.items():
            with self.subTest(key=key):
                self.assertTrue(re.fullmatch(r'[\x20-\x7e]+', text), text)
                self.assertNotIn('/', text.replace('`work --mine`', ''))

    def test_an_unknown_key_or_set_has_no_text(self):
        self.assertIsNone(nudge_messages.text_for('shutdown', nudge_messages.MESSAGE_SET))
        self.assertIsNone(nudge_messages.text_for('resume', 'nudge-messages-v0'))
        self.assertEqual(nudge_messages.TEXTS['resume'], nudge_messages.text_for('resume', nudge_messages.MESSAGE_SET))


if __name__ == '__main__':
    unittest.main()
