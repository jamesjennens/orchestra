"""The lane nudge request (kittrial-5bb.413): slice 1 of docs/design/LANE_NUDGE_DESIGN.md.

An AUTHORIZATION change, so every rule of design sections 1.3 and 7 is a test through the real route
of the in-process web service, and each names one principal. ``POST /v1/projects/<pid>/nudges`` is a
request a coordinator of THAT project writes (a person with the role, or an agent whose coordinator
grant is effective and whose issuer still qualifies, kittrial-5bb.410); nothing is delivered (no
carrier yet), so a request that nobody carries out ends ``not-deliverable``.
"""
import json
import sys
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))

import nudges
import test_lanes_scope as scope

NOTE = 'note-sentinel-7f3a the carrier must never see this'


class NudgeCase(scope.ScopeCase):
    """olive and oscar own Alpha, cora coordinates it (agent Heron), carl and dave each hold a task."""

    def setUp(self):
        super().setUp()
        self.shift = 0
        self.plant_coordination()

    def later(self, seconds):
        """Move the service's own clock on: the limits and the expiry are read from it."""
        self.shift += seconds
        shift = self.shift
        self.service._now = lambda: time.time() + shift

    def nudge(self, token, lane=None, project=None, key=None, **more):
        body = dict({'lane': lane or self.ids['carl'], 'message': 'resume', 'reason': 'stalled-at-prompt'}, **more)
        return self.request('POST', '/v1/projects/%s/nudges' % (project or self.project), body, token=token, key=key)

    def records(self, project=None):
        return (self.service.state.get('nudges') or {}).get(project or self.project) or []

    def events(self, action='lanes.nudge.request', outcome=None):
        return [event for event in self.service.state['audit']
                if event['action'] == action and (outcome is None or event['outcome'] == outcome)]

    def granted_agent(self):
        self.assertEqual(200, self.grant().status)
        return self.agent


class WhoMayRequestTests(NudgeCase):
    def test_an_owner_a_coordinator_and_a_superuser_may_request(self):
        for who, token, lane in (('olive', self.tokens['olive'], self.ids['carl']),
                                 ('cora', self.tokens['cora'], self.ids['dave']),
                                 ('the superuser', self.admin, self.ids['carl'])):
            with self.subTest(who=who):
                self.later(1000)
                answer = self.nudge(token, lane)
                self.assertEqual(201, answer.status, answer.data)
                shown = answer.data['nudge']
                self.assertEqual((lane, 'resume', 'pending', False), (shown['lane'], shown['message'], shown['state'],
                                                                      answer.data['coalesced']))
        self.assertEqual(3, len(self.records()))

    def test_a_contributor_and_a_viewer_are_refused_and_nothing_is_read_or_written(self):
        asked = self.watch_reads()
        for name in ('carl', 'vera'):
            with self.subTest(who=name):
                self.assertEqual(403, self.nudge(self.tokens[name]).status)
        self.assertEqual([], asked)
        self.assertEqual([], self.records())

    def test_a_non_member_and_the_owner_of_another_project_are_refused_here(self):
        elsewhere = self.create_project(self.tokens['otto'], 'Elsewhere')
        asked = self.watch_reads()
        for token in (self.tokens['otto'], None):
            with self.subTest(token=bool(token)):
                self.assertIn(self.nudge(token).status, (401, 404))
        self.assertEqual([], asked)
        self.assertEqual([], self.records())
        self.assertEqual([], self.records(elsewhere))

    def test_an_owner_of_another_project_cannot_nudge_here_by_naming_the_lane_in_theirs(self):
        # otto owns Elsewhere only: the request in Alpha is the not-a-member answer, in Elsewhere the lane is not there.
        elsewhere = self.create_project(self.tokens['otto'], 'Elsewhere')
        self.assertEqual(404, self.nudge(self.tokens['otto']).status)
        self.assertEqual(404, self.nudge(self.tokens['otto'], project=elsewhere).status)
        self.assertEqual({}, self.service.state.get('nudges') or {})

    def test_a_demoted_coordinator_is_refused_at_the_next_request(self):
        self.assertEqual(201, self.nudge(self.tokens['cora']).status)
        self.assertEqual(200, self.member('cora', 'contributor').status)
        self.later(1000)
        self.assertEqual(403, self.nudge(self.tokens['cora']).status)
        self.assertEqual(1, len(self.records()))


class AgentTests(NudgeCase):
    def test_an_agent_without_a_grant_is_refused_and_with_one_may_request(self):
        self.assertEqual(403, self.nudge(self.agent).status)
        self.assertEqual([], self.records())
        self.granted_agent()
        answer = self.nudge(self.agent)
        self.assertEqual(201, answer.status, answer.data)
        self.assertEqual('Heron (agent)', answer.data['nudge']['requested_by_display'])
        self.assertEqual({'kind': 'agent', 'user_id': self.ids['cora'], 'agent_id': self.agent_id},
                         self.records()[0]['requested_by'])

    def test_an_agent_whose_issuer_was_demoted_is_refused(self):
        self.granted_agent()
        self.assertEqual(200, self.member('olive', 'contributor', token=self.admin).status)
        self.assertEqual(403, self.nudge(self.agent).status)
        self.assertEqual([], self.records())
        # The issuer is an owner again: the same grant counts again, with no new record.
        self.assertEqual(200, self.member('olive', 'owner', token=self.admin).status)
        self.assertEqual(201, self.nudge(self.agent).status)

    def test_a_grant_in_another_project_is_no_right_here(self):
        beta = self.create_project(self.admin, 'Beta')
        self.assertEqual(200, self.member('cora', 'coordinator', token=self.admin, project=beta).status)
        both, both_id = self.agent_with_id('cora', 'Stork', projects=[self.project, beta])
        self.assertEqual(200, self.grant(both_id, project=beta, token=self.admin).status)
        self.assertEqual(403, self.nudge(both).status)

    def test_an_agent_never_nudges_its_own_lane_or_its_accounts(self):
        self.granted_agent()
        egret, _egret_id = self.agent_with_id('cora', 'Egret')
        before = self.actors(self.lanes(self.admin))
        own, sibling = {}, {}
        for token, found in ((self.agent, own), (egret, sibling)):
            task = self.request('POST', '/v1/projects/%s/tasks' % self.project, {'title': 'agent work'},
                                token=self.admin).data['id']
            self.assertEqual(200, self.request('POST', self.base(task) + '/claim', {}, token=token).status)
            found['actor'] = (self.actors(self.lanes(self.admin)) - before).pop()
            before = self.actors(self.lanes(self.admin))
        self.cora_task = self.claimed('cora')
        for label, lane in (('its own', own['actor']), ('a sibling agent of its account', sibling['actor']),
                            ("its account's", self.ids['cora'])):
            with self.subTest(lane=label):
                answer = self.nudge(self.agent, lane)
                self.assertEqual(403, answer.status, answer.data)
                self.assertEqual(nudges.OWN_PARTY, answer.data['error']['message'])
        self.assertEqual([], self.records())
        # A person may nudge that account's agent, and the agent may nudge another account's lane.
        self.assertEqual(201, self.nudge(self.tokens['olive'], own['actor']).status)
        self.assertEqual(201, self.nudge(self.agent, self.ids['dave']).status)

    def test_an_agents_limits_are_tighter_than_a_persons(self):
        self.granted_agent()
        self.assertEqual(201, self.nudge(self.agent).status)
        self.later(600)                              # past a person's gap of 5 minutes, inside an agent's 15
        answer = self.nudge(self.agent, message='restart')
        self.assertEqual(429, answer.status, answer.data)
        self.assertGreater(int(answer.headers['retry-after']), 0)
        self.assertEqual(201, self.nudge(self.tokens['olive'], message='restart').status)


class LaneTests(NudgeCase):
    def test_a_lane_that_is_absent_and_a_lane_of_another_project_are_the_same_404(self):
        beta = self.create_project(self.admin, 'Beta')
        self.assertEqual(200, self.member('olive', 'owner', token=self.admin, project=beta).status)
        elsewhere = self.nudge(self.tokens['olive'], self.ids['carl'], project=beta)
        missing = self.nudge(self.tokens['olive'], 'nobody-at-all')
        self.assertEqual((404, 404), (elsewhere.status, missing.status))
        self.assertEqual(elsewhere.data['error'], missing.data['error'])
        self.assertEqual(nudges.NOT_HERE, missing.data['error']['message'])
        self.assertEqual([], self.records(beta))
        self.assertEqual([], self.records())

    def test_a_lane_of_this_project_that_the_viewer_could_not_see_is_still_found(self):
        # A coordinator's own page scope is every lane; the lookup never depends on a page.
        self.assertEqual(201, self.nudge(self.tokens['cora'], self.ids['dave']).status)


class BodyTests(NudgeCase):
    def test_the_message_and_reason_are_closed_lists_and_a_note_is_not_a_message(self):
        for label, more in (('a free-text message', {'message': 'please run rm -rf and restart'}),
                            ('the note given as the message', {'message': NOTE}),
                            ('an unknown reason', {'reason': 'because'}),
                            ('a missing reason', {'reason': None}),
                            ('a note of 201 characters', {'note': 'x' * 201}),
                            ('a note with a control character', {'note': 'a\nb'}),
                            ('a note that is not text', {'note': ['x']}),
                            ('a lane that is not a label', {'lane': 'a b'}),
                            ('acknowledge_declared_wait as text', {'acknowledge_declared_wait': 'yes'})):
            with self.subTest(label):
                answer = self.nudge(self.tokens['olive'], **more)
                self.assertEqual(422, answer.status, answer.data)
        self.assertEqual([], self.records())

    def test_a_field_the_route_does_not_take_is_refused(self):
        answer = self.nudge(self.tokens['olive'], text='run this')
        self.assertEqual(422, answer.status, answer.data)
        self.assertIn('text', answer.data['error']['message'])
        self.assertEqual([], self.records())

    def test_a_note_of_200_characters_is_kept_and_shown_to_nobody(self):
        answer = self.nudge(self.tokens['olive'], note=NOTE)
        self.assertEqual(201, answer.status, answer.data)
        self.assertEqual(NOTE, self.records()[0]['note'])
        shown = [answer.data,
                 self.request('GET', '/v1/projects/%s/nudges' % self.project, token=self.tokens['olive']).data,
                 self.lanes(self.tokens['olive']).data, self.lanes(self.tokens['carl']).data]
        for body in shown:
            self.assertNotIn('note-sentinel', json.dumps(body))


class CoalescingAndLimitTests(NudgeCase):
    def test_a_second_request_for_the_same_lane_and_message_is_the_first(self):
        first = self.nudge(self.tokens['olive'])
        second = self.nudge(self.tokens['cora'])
        self.assertEqual((201, 200), (first.status, second.status))
        self.assertEqual(first.data['nudge']['id'], second.data['nudge']['id'])
        self.assertIs(True, second.data['coalesced'])
        self.assertEqual(1, len(self.records()))
        self.assertEqual(1, len(self.events(outcome='committed')))
        self.assertEqual([], self.events(outcome='rejected'))

    def test_a_person_may_nudge_a_lane_once_in_five_minutes(self):
        self.assertEqual(201, self.nudge(self.tokens['olive']).status)
        refused = self.nudge(self.tokens['olive'], message='restart')
        self.assertEqual(429, refused.status, refused.data)
        self.assertEqual('rate_limited', refused.data['error']['code'])
        retry = int(refused.headers['retry-after'])
        self.assertTrue(1 <= retry <= 301, retry)
        self.assertEqual(1, len(self.records()))                     # a refusal writes no record
        self.assertEqual(1, len(self.events(outcome='committed')))
        self.assertEqual(1, len(self.events(outcome='rejected')))   # ... and is audited as refused
        self.assertEqual(201, self.nudge(self.tokens['olive'], self.ids['dave']).status)   # another lane: its own limit
        self.later(retry)
        self.assertEqual(201, self.nudge(self.tokens['olive'], message='restart').status)

    def test_the_bounds_of_section_1_3_are_exactly_the_proposed_numbers(self):
        now = 10 ** 9
        record = lambda lane, at, agent=None: {'lane': lane, 'requested_ts': at,
                                               'requested_by': {'kind': 'agent' if agent else 'person', 'agent_id': agent}}
        # six in the last hour on one lane (spaced 6 minutes apart): the seventh waits for the oldest to leave
        six = [record('a', now - 3500 + 360 * step) for step in range(6)]
        self.assertGreater(nudges.retry_after(six, 'a', 'person', None, now), 0)
        self.assertEqual(0, nudges.retry_after(six[1:], 'a', 'person', None, now))
        # thirty in the last hour in one project, on thirty lanes
        thirty = [record('lane-%d' % step, now - 3000 + 90 * step) for step in range(30)]
        self.assertGreater(nudges.retry_after(thirty, 'fresh', 'person', None, now), 0)
        self.assertEqual(0, nudges.retry_after(thirty[1:], 'fresh', 'person', None, now))
        # an agent: three per lane per hour (spaced 20 minutes), ten per agent per hour
        three = [record('a', now - 3500 + 1200 * step, 'ag') for step in range(3)]
        self.assertGreater(nudges.retry_after(three, 'a', 'agent', 'ag', now), 0)
        self.assertEqual(0, nudges.retry_after(three[1:], 'a', 'agent', 'ag', now))
        ten = [record('lane-%d' % step, now - 3000 + 250 * step, 'ag') for step in range(10)]
        self.assertGreater(nudges.retry_after(ten, 'fresh', 'agent', 'ag', now), 0)
        self.assertEqual(0, nudges.retry_after(ten, 'fresh', 'agent', 'other', now))

    def test_the_record_list_is_bounded_and_keeps_every_open_request(self):
        iso = lambda stamp: 'at-%s' % stamp
        now = 10 ** 9
        records = [{'id': 'old-%d' % step, 'state': 'not-deliverable', 'requested_ts': now - 40 * 86400}
                   for step in range(5)]
        records += [{'id': 'fin-%d' % step, 'state': 'delivered', 'requested_ts': now - 3600 - step}
                    for step in range(nudges.KEEP_RECORDS + 20)]
        records.append({'id': 'open', 'state': 'pending', 'requested_ts': now - 60})
        self.assertTrue(nudges.settle(records, now, iso))
        self.assertEqual(nudges.KEEP_RECORDS, len(records))
        self.assertIn('open', [record['id'] for record in records])
        self.assertFalse(any(record['id'].startswith('old-') for record in records))


class DeclaredWaitTests(NudgeCase):
    def declare(self, whose_move='coordinator'):
        planted = self.backend.lanes_read

        def read(*args, **kwargs):
            answer = planted(*args, **kwargs)
            for row in answer['items']:
                for entry in row.get('held') or []:
                    entry.update(declared=True, state='waiting-on-review', waiting_on='reviewer',
                                 state_since='2026-10-25T08:00:00Z', whose_move=whose_move)
            return answer
        self.backend.lanes_read = read

    def test_a_lane_that_declared_a_wait_needs_the_acknowledgement(self):
        self.declare()
        refused = self.nudge(self.tokens['olive'])
        self.assertEqual(409, refused.status, refused.data)
        self.assertEqual(nudges.DECLARED_WAIT % ('reviewer', '2026-10-25T08:00:00Z'), refused.data['error']['message'])
        self.assertEqual([], self.records())
        answer = self.nudge(self.tokens['olive'], acknowledge_declared_wait=True)
        self.assertEqual(201, answer.status, answer.data)
        self.assertIs(True, answer.data['nudge']['declared_wait'])
        self.assertEqual({'task': self.carl_task, 'state': 'waiting-on-review', 'waiting_on': 'reviewer',
                          'since': '2026-10-25T08:00:00Z', 'whose_move': 'coordinator'},
                         self.records()[0]['declared_wait'])

    def test_a_stale_declaration_is_also_a_declared_wait(self):
        self.declare(whose_move='you')
        self.assertEqual(409, self.nudge(self.tokens['olive']).status)

    def test_a_wait_that_is_only_guessed_from_prose_needs_no_acknowledgement(self):
        row = {'held': [{'task': 't', 'declared': False, 'inferred': True, 'state': 'waiting-on-coordinator',
                         'waiting_on': 'coordinator'}]}
        self.assertIsNone(nudges.declared_wait(row))
        self.assertEqual(201, self.nudge(self.tokens['olive']).status)

    def test_a_lane_that_declared_working_is_not_waiting(self):
        row = {'held': [{'task': 't', 'declared': True, 'state': 'working'}]}
        self.assertIsNone(nudges.declared_wait(row))


class RecordTests(NudgeCase):
    def test_a_request_is_audited_once_with_who_the_lane_the_time_and_the_reason(self):
        answer = self.nudge(self.tokens['cora'], note='api error at 10:42')
        self.assertEqual(201, answer.status, answer.data)
        [event] = self.events()
        self.assertEqual((self.ids['cora'], self.ids['carl'], self.project, 'committed'),
                         (event['user_id'], event['actor'], event['project_id'], event['outcome']))
        self.assertEqual('resume stalled-at-prompt api error at 10:42', event['reason'])
        self.assertTrue(event['time'])

    def test_a_refused_request_is_audited_as_refused_and_writes_nothing(self):
        self.assertEqual(403, self.nudge(self.tokens['carl']).status)
        self.assertEqual(404, self.nudge(self.tokens['olive'], 'nobody-at-all').status)
        self.assertEqual(422, self.nudge(self.tokens['olive'], message='x').status)
        self.assertEqual([], self.records())
        self.assertEqual([], self.events(outcome='committed'))

    def test_a_pending_request_not_carried_out_ends_not_deliverable_and_is_never_delivered(self):
        made = self.nudge(self.tokens['olive']).data['nudge']
        self.assertEqual('pending', made['state'])
        self.later(nudges.PENDING_TTL + 1)
        listed = self.request('GET', '/v1/projects/%s/nudges' % self.project, token=self.tokens['olive'])
        self.assertEqual(200, listed.status, listed.data)
        [shown] = listed.data['items']
        self.assertEqual((made['id'], 'not-deliverable', 'no-carrier'), (shown['id'], shown['state'], shown['outcome_detail']))
        self.assertEqual('pending', self.records()[0]['state'])            # a read writes nothing
        # The next request settles it for good, and the same lane may be asked for again.
        again = self.nudge(self.tokens['olive'])
        self.assertEqual(201, again.status, again.data)
        self.assertNotEqual(made['id'], again.data['nudge']['id'])
        self.assertEqual(['not-deliverable', 'pending'], [record['state'] for record in self.records()])

    def test_the_board_row_carries_the_newest_nudge_of_its_lane_in_this_project_only(self):
        self.assertEqual(201, self.nudge(self.tokens['olive']).status)
        rows = {row['actor']: row for row in self.lanes(self.tokens['olive']).data['items']}
        shown = rows[self.ids['carl']]['nudge']
        self.assertEqual({'id', 'lane', 'message', 'reason', 'state', 'requested_at', 'requested_by_display',
                          'outcome_at', 'outcome_detail', 'declared_wait'}, set(shown))
        self.assertEqual(('resume', 'pending'), (shown['message'], shown['state']))
        self.assertIsNone(rows[self.ids['dave']]['nudge'])
        # The lane itself sees its own row's nudge, and nobody sees another lane's through the page.
        own = self.lanes(self.tokens['carl']).data['items']
        self.assertEqual([self.ids['carl']], [row['actor'] for row in own])
        self.assertEqual('pending', own[0]['nudge']['state'])
        beta = self.create_project(self.admin, 'Beta')
        self.assertEqual(200, self.lanes(self.admin, beta).status)
        self.assertEqual([], self.records(beta))

    def test_the_list_is_for_a_coordinator_and_filters_by_lane(self):
        self.assertEqual(201, self.nudge(self.tokens['olive']).status)
        self.assertEqual(201, self.nudge(self.tokens['olive'], self.ids['dave']).status)
        path = '/v1/projects/%s/nudges' % self.project
        self.assertEqual(403, self.request('GET', path, token=self.tokens['carl']).status)
        both = self.request('GET', path, token=self.tokens['cora']).data
        self.assertEqual(2, both['total'])
        one = self.request('GET', path + '?lane=' + self.ids['dave'], token=self.tokens['cora']).data
        self.assertEqual([self.ids['dave']], [item['lane'] for item in one['items']])
        self.assertEqual(400, self.request('GET', path + '?limit=0', token=self.tokens['cora']).status)

    def test_a_retry_with_the_same_key_is_the_same_request(self):
        key = 'nudge-retry-0001'
        first = self.nudge(self.tokens['olive'], key=key)
        second = self.nudge(self.tokens['olive'], key=key)
        self.assertEqual((201, first.data['nudge']['id']), (second.status, second.data['nudge']['id']))
        self.assertEqual(1, len(self.records()))
        third = self.nudge(self.tokens['olive'], self.ids['dave'], operation_id='nudge-op-000002')
        self.assertEqual(201, third.status, third.data)
        self.assertEqual(422, self.nudge(self.tokens['olive'], key='nudge-retry-0001',
                                         operation_id='nudge-op-000009').status)

    def test_a_damaged_record_is_refused_and_does_not_break_the_board(self):
        self.service.state['nudges'] = None
        answer = self.nudge(self.tokens['olive'])
        self.assertEqual(503, answer.status, answer.data)
        board = self.lanes(self.tokens['olive'])
        self.assertEqual(200, board.status, board.data)
        self.assertTrue(all(row['nudge'] is None for row in board.data['items']))


if __name__ == '__main__':
    unittest.main()
