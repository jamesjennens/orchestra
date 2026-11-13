"""Invites for an existing account (kittrial-5bb.581), on the invite links of kittrial-5bb.578.

The same harness as tests/test_invites.py: the real service on a real store behind the real HTTP
server, a trusted proxy on loopback and a clock the test moves. "erin" is an account that already
exists; cora owns North and South, carl is a contributor in both, and root-admin is a superuser.
"""
import http.client
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_auth
import http_service
import invites
from http_auth import token_hash
from test_invites import ADMIN, ADMIN_PASSWORD, GOOD_PASSWORD, WHERE, InviteCase


class ExistingCase(InviteCase):
    def setUp(self):
        super().setUp()
        self.erin = self.person('erin', {})
        self.erin_id = self.service.state['usernames']['erin']

    def uid(self, username):
        return self.service.state['usernames'][username.lower()]

    def existing(self, token=None, expect=201, **fields):
        body = {'person': 'existing', 'username': 'erin', 'projects': ['north', 'south']}
        body.update(fields)
        status, answer = self.call('POST', '/v1/invites', body, token or self.cora)
        self.assertEqual(status, expect, answer)
        return answer

    def accept(self, link_token, session, key=None, source=WHERE):
        return self.call('POST', '/v1/invites/accept', {'token': link_token}, session, source=source, key=key)

    def role(self, pid, username='erin'):
        return (self.service.state['memberships'].get(pid) or {}).get(self.uid(username))

    def set_role(self, pid, username, role):
        status, body = self.call('PUT', '/v1/projects/%s/members/%s' % (pid, self.uid(username)), {'role': role}, self.admin)
        self.assertEqual(status, 200, body)

    def full_snapshot(self):
        state = self.service.state
        return tuple(json.dumps(state.get(name), sort_keys=True)
                     for name in ('users', 'usernames', 'memberships', 'agents', 'credentials', 'credential_tokens'))

    def cookie_session(self, username, password):
        status, body = self.call('POST', '/v1/sessions', {'username': username, 'password': password}, source='192.0.2.1')
        self.assertEqual(status, 201, body)
        session = body['session']
        return session['token'], session['csrf_token']

    def raw(self, method, path, body, headers):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=120)
        headers = dict(headers, **{'X-Forwarded-For': WHERE})
        if body is not None:
            headers['Content-Type'] = 'application/json'
        try:
            connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None), dict(response.getheaders())
        finally:
            connection.close()

    def agent_secret(self, owner_token, project, name='Helper'):
        status, body = self.call('POST', '/v1/agents', {'name': name, 'projects': [project]}, owner_token)
        self.assertEqual(status, 201, body)
        return body['credential']['secret']


class CreateTests(ExistingCase):
    def test_an_owner_invites_an_existing_account_and_the_record_names_it_by_id(self):
        answer = self.existing(agent={'name': 'Erin bot', 'project': 'south', 'role': 'reviewer'})
        record = self.record(answer['token'])
        self.assertEqual((record['person'], record['user_id'], record['username'], record['role']),
                         ('existing', self.erin_id, 'erin', 'contributor'))
        self.assertEqual(record['agent']['role'], 'reviewer')
        self.assertEqual(answer['invite']['person'], 'existing')
        self.assertNotIn(answer['token'], json.dumps(self.service.state))

    def test_an_unknown_or_disabled_username_gets_the_lookup_answer(self):
        self.person('dora', {})
        status, body = self.call('POST', '/v1/accounts/%s/disable' % self.uid('dora'), {}, self.admin)
        self.assertEqual(status, 200, body)
        for name in ('nobody-here', 'dora'):
            with self.subTest(name=name):
                answer = self.existing(expect=404, username=name)
                self.assertEqual(answer['error']['message'], invites.NO_ACCOUNT)
        self.assertFalse(self.service.state.get('invites'))

    def test_inviting_yourself_is_refused_toward_new_agent(self):
        answer = self.existing(expect=422, username='cora', agent={'name': 'Mine', 'project': 'north'})
        self.assertEqual(answer['error']['message'], invites.SELF)

    def test_an_existing_account_takes_no_display_name(self):
        answer = self.existing(expect=422, display_name='Someone Else')
        self.assertEqual(answer['error']['message'], invites.EXISTING_NO_DISPLAY_NAME)

    def test_a_new_person_takes_no_role(self):
        answer = self.invite(expect=422, role='coordinator')
        self.assertEqual(answer['error']['message'], invites.ROLE_NEW_PERSON)

    def test_an_unknown_person_kind_is_refused(self):
        self.assertEqual(self.existing(expect=422, person='robot')['error']['message'], invites.PERSON_FIELD)

    def test_the_owner_role_only_from_a_superuser(self):
        self.assertEqual(self.existing(expect=403, role='owner')['error']['message'], invites.ROLE_OWNER)
        self.existing(token=self.admin, role='owner')
        self.assertEqual(self.existing(expect=422, role='admin')['error']['code'], 'invalid_payload')

    def test_a_coordinator_grant_or_scopes_are_still_refused_for_an_existing_account(self):
        for agent in ({'name': 'Boss', 'project': 'north', 'role': 'coordinator'},
                      {'name': 'Boss', 'project': 'north', 'grants': ['coordinator']},
                      {'name': 'Boss', 'project': 'north', 'scopes': ['tasks']}):
            with self.subTest(agent=agent):
                self.assertEqual(self.existing(expect=403, agent=agent)['error']['message'], invites.WORKER_ONLY)

    def test_an_agent_name_erin_already_uses_is_not_judged_at_create_only_at_accept(self):
        # kittrial-5bb.581 r2 (review S2): the inviter learns nothing about erin's agents. A clashing
        # name is made like any other; only erin, accepting, hears the clash.
        # erin is not a member anywhere yet, so her own agent is granted no project.
        status, body = self.call('POST', '/v1/agents', {'name': 'Erin Bot'}, self.erin)
        self.assertEqual(status, 201, body)
        theirs = body['agent']['id']
        answer = self.existing(agent={'name': 'erin-bot', 'project': 'north'})
        self.assertNotIn(theirs, json.dumps(answer))
        status, body = self.accept(answer['token'], self.erin)
        self.assertEqual((status, body['error']['message']), (409, invites.AGENT_NAME_TAKEN_NOW))

    def test_the_agent_limit_is_not_said_at_create_only_at_accept(self):
        with mock.patch.object(invites, 'AGENT_MAX_PER_OWNER', 0):
            answer = self.existing(agent={'name': 'Erin bot', 'project': 'north'})
            status, body = self.accept(answer['token'], self.erin)
        self.assertEqual((status, body['error']['message']), (409, invites.AGENT_LIMIT_NOW % 0))

    def test_nothing_to_do_is_refused(self):
        self.set_role('north', 'erin', 'coordinator')
        self.set_role('south', 'erin', 'contributor')
        answer = self.existing(expect=409)
        self.assertEqual(answer['error']['message'], invites.NOTHING_TO_DO % 'erin')
        # A higher role named, or an agent asked, is something to do.
        self.existing(role='coordinator', projects=['south'])

    def test_one_pending_invite_per_username(self):
        self.existing()
        self.assertEqual(self.existing(expect=409, username='ERIN')['error']['code'], 'conflict')

    def test_a_non_owner_and_a_credential_cannot_make_one(self):
        self.assertEqual(self.existing(token=self.carl, expect=403)['error']['code'], 'forbidden')
        secret = self.agent_secret(self.cora, 'north')
        status, _ = self.call('POST', '/v1/invites', {'person': 'existing', 'username': 'erin', 'projects': ['north']}, secret)
        self.assertEqual(status, 403)
        self.assertFalse(self.service.state.get('invites'))

    def test_the_username_check_is_counted_as_a_lookup(self):
        with mock.patch.object(self.service, 'lookup_max', 1):
            self.existing(projects=['north'])
            self.assertEqual(self.existing(expect=429, projects=['south'], username='carl')['error']['code'], 'rate_limited')


class ViewTests(ExistingCase):
    def test_the_anonymous_view_says_whom_it_is_for_and_nothing_about_memberships(self):
        self.set_role('north', 'erin', 'viewer')
        token = self.existing(agent={'name': 'Erin bot', 'project': 'south'})['token']
        status, shown = self.view(token)
        self.assertEqual(status, 200, shown)
        self.assertEqual((shown['person'], shown['username'], shown['role'], shown['password_min_length']),
                         ('existing', 'erin', 'contributor', None))
        self.assertEqual(set(shown), {'person', 'status', 'display_name', 'username', 'projects', 'role', 'agent',
                                      'invited_by_name', 'created_at', 'expires_at', 'generated_at',
                                      'password_min_length'})
        self.assertIsNone(shown['display_name'])
        text = json.dumps(shown)
        for leak in ('viewer', 'member', self.erin_id, 'before', 'after'):
            self.assertNotIn(leak, text)

    def test_the_anonymous_view_does_not_carry_the_accounts_own_display_name(self):
        # kittrial-5bb.581 r2 (review S1): an account whose display name differs from its username.
        status, body = self.call('POST', '/v1/accounts', {'username': 'alma', 'display_name': 'Alma Hidden-Surname'},
                                 self.admin)
        self.assertEqual(status, 201, body)
        token = self.existing(username='alma', role='coordinator',
                              agent={'name': 'Alma bot', 'project': 'north'})['token']
        status, shown = self.view(token)
        self.assertEqual(status, 200, shown)
        self.assertEqual((shown['username'], shown['display_name']), ('alma', None))
        self.assertNotIn('Hidden', json.dumps(shown))


class AcceptTests(ExistingCase):
    def test_the_named_account_accepts_memberships_agent_and_secret_once(self):
        self.set_role('north', 'erin', 'viewer')
        answer = self.existing(agent={'name': 'Erin bot', 'project': 'south', 'claim_limit': 2})
        status, done = self.accept(answer['token'], self.erin, key='accept-1')
        self.assertEqual(status, 201, done)
        self.assertEqual((self.role('north'), self.role('south')), ('contributor', 'contributor'))
        self.assertEqual([(p['id'], p['role_before'], p['role']) for p in done['projects']],
                         [('north', 'viewer', 'contributor'), ('south', None, 'contributor')])
        agent = self.service.state['agents'][done['agent']['id']]
        self.assertEqual((agent['owner'], agent['created_by'], agent['projects'], agent['claim_limit'], agent['role']),
                         (self.erin_id, self.uid('cora'), ['south'], 2, 'worker'))
        secret = done['credential']['secret']
        self.assertTrue(secret and done['secret_available'])
        self.assertEqual(self.service.authenticate(secret).agent_id, agent['id'])
        record = self.record(answer['token'])
        self.assertEqual((record['used_by'], record['agent_id']), (self.erin_id, agent['id']))
        self.assertEqual(record['memberships'], {'north': {'before': 'viewer', 'after': 'contributor'},
                                                 'south': {'before': None, 'after': 'contributor'}})
        # The exact retry: 200, the same account and agent, no secret.
        status, again = self.accept(answer['token'], self.erin, key='accept-1')
        self.assertEqual(status, 200, again)
        self.assertNotIn(secret, json.dumps(again))
        self.assertFalse(again['secret_available'])
        self.assertIsNone(again['setup'])
        # Used: a new request is the unknown-token answer.
        status, body = self.accept(answer['token'], self.erin, key='accept-2')
        self.assertEqual(self.generic(status, body), self.UNUSABLE)
        # The inviter never sees the secret: not in the list, not in the stored record.
        listed = self.call('GET', '/v1/invites', token=self.cora)[1]
        self.assertNotIn(secret, json.dumps(listed))
        self.assertNotIn(secret, json.dumps(self.service.state['invites']))

    def test_a_reviewer_agent_is_made_a_reviewer(self):
        # kittrial-5bb.581 r2 (review S3, MA10): the asked role reaches the agent made at accept.
        answer = self.existing(agent={'name': 'Erin reviewer', 'project': 'north', 'role': 'reviewer'})
        status, done = self.accept(answer['token'], self.erin)
        self.assertEqual(status, 201, done)
        self.assertEqual(self.service.state['agents'][done['agent']['id']]['role'], 'reviewer')

    def test_a_higher_role_is_kept_and_an_owner_is_never_touched(self):
        self.set_role('north', 'erin', 'coordinator')
        self.set_role('south', 'erin', 'owner')
        token = self.existing(token=self.admin, role='contributor', agent={'name': 'Erin bot', 'project': 'north'})['token']
        status, done = self.accept(token, self.erin)
        self.assertEqual(status, 201, done)
        self.assertEqual((self.role('north'), self.role('south')), ('coordinator', 'owner'))
        self.assertEqual([(p['role_before'], p['role']) for p in done['projects']],
                         [('coordinator', 'coordinator'), ('owner', 'owner')])

    def test_the_same_role_is_kept_and_a_lower_one_is_raised(self):
        self.set_role('north', 'erin', 'contributor')
        self.set_role('south', 'erin', 'viewer')
        token = self.existing(role='coordinator')['token']
        status, done = self.accept(token, self.erin)
        self.assertEqual(status, 201, done)
        self.assertEqual((self.role('north'), self.role('south')), ('coordinator', 'coordinator'))
        token = self.existing(token=self.admin, role='coordinator', projects=['north', 'west'])['token']
        self.assertEqual(self.accept(token, self.erin)[0], 201)
        self.assertEqual((self.role('north'), self.role('west')), ('coordinator', 'coordinator'))

    def test_the_audit_of_each_project_names_the_existing_account(self):
        self.set_role('north', 'erin', 'viewer')
        token = self.existing()['token']
        mark = len(self.service.state['audit'])
        self.assertEqual(self.accept(token, self.erin)[0], 201)
        events = [e for e in self.service.state['audit'][mark:] if e.get('action') == 'invites.redeemed']
        self.assertEqual(sorted(e['project_id'] for e in events), ['north', 'south'])
        for event in events:
            self.assertEqual((event['existing_user'], event['invited_by']), (self.erin_id, self.uid('cora')))
            self.assertNotIn('new_user', event)
        north = next(e for e in events if e['project_id'] == 'north')
        self.assertEqual((north['role_before'], north['role_after']), ('viewer', 'contributor'))


class AcceptRefusalTests(ExistingCase):
    def test_anyone_but_the_named_account_gets_the_unknown_token_answer(self):
        token = self.existing(agent={'name': 'Erin bot', 'project': 'north'})['token']
        before = self.full_snapshot()
        unknown = self.generic(*self.accept('x' * 43, self.carl))
        self.assertEqual(unknown, self.UNUSABLE)
        for who in (self.carl, self.cora, self.admin):
            with self.subTest(who=who[:6]):
                self.assertEqual(self.generic(*self.accept(token, who)), unknown)
        self.assertEqual(self.full_snapshot(), before)
        self.assertIsNone(self.record(token)['used_at'])
        self.assertEqual(self.accept(token, self.erin)[0], 201)

    def test_an_agent_credential_cannot_accept_even_its_owners_invite(self):
        token = self.existing(projects=['north'])['token']
        self.set_role('south', 'erin', 'contributor')
        secret = self.agent_secret(self.erin, 'south')
        # Using the credential marks its agent seen, so compare what an accept would write.
        state = self.service.state
        before = (json.dumps(state['memberships'], sort_keys=True), sorted(state['agents']), sorted(state['credentials']))
        # The route refuses it at its capability check (a credential has no session authority) before the
        # token is read; the module refuses it on its own with PERSON_ONLY (below), whatever the route checked.
        status, body = self.accept(token, secret)
        self.assertEqual((status, body['error']['code']), (403, 'forbidden'))
        self.assertEqual((json.dumps(state['memberships'], sort_keys=True), sorted(state['agents']), sorted(state['credentials'])),
                         before)
        principal = self.service.authenticate(secret)
        with self.assertRaises(http_auth.HttpError) as refused:
            invites.accept(self.service, principal, token)
        self.assertEqual((refused.exception.status, refused.exception.message), (403, invites.PERSON_ONLY))
        self.assertIsNone(self.record(token)['used_at'])

    def test_no_session_is_401(self):
        token = self.existing()['token']
        self.assertEqual(self.call('POST', '/v1/invites/accept', {'token': token})[0], 401)

    def test_a_cookie_session_needs_the_csrf_header(self):
        token = self.existing()['token']
        session, csrf = self.cookie_session('erin', 'erin-password-1')
        self.assertTrue(csrf)
        status, body, _ = self.raw('POST', '/v1/invites/accept', {'token': token},
                                   {'Cookie': 'orchestra_session=' + session, 'Idempotency-Key': 'cookie-accept-1'})
        self.assertEqual(status, 403, body)
        self.assertIsNone(self.record(token)['used_at'])
        status, body, _ = self.raw('POST', '/v1/invites/accept', {'token': token},
                                   {'Cookie': 'orchestra_session=' + session, 'X-CSRF-Token': csrf,
                                    'Idempotency-Key': 'cookie-accept-2'})
        self.assertEqual(status, 201, body)

    def test_a_new_person_token_cannot_be_accepted_by_a_session(self):
        token = self.invite()['token']
        self.assertEqual(self.generic(*self.accept(token, self.erin)), self.UNUSABLE)
        self.assertIsNone(self.record(token)['used_at'])

    def test_the_inviter_no_longer_an_owner_makes_it_stale(self):
        token = self.existing(agent={'name': 'Erin bot', 'project': 'north'})['token']
        self.set_role('south', 'cora', 'coordinator')
        before = self.full_snapshot()
        status, body = self.accept(token, self.erin)
        self.assertEqual((status, body['error']['code'], body['error']['message']), (409, 'invite_stale', invites.STALE))
        self.assertEqual(self.full_snapshot(), before)

    def test_the_inviter_no_longer_an_owner_of_the_first_project_makes_it_stale(self):
        # kittrial-5bb.581 r2 (review S3, MA3): every project is judged again, not only the last.
        token = self.existing(agent={'name': 'Erin bot', 'project': 'south'})['token']
        self.set_role('north', 'cora', 'coordinator')
        before = self.full_snapshot()
        status, body = self.accept(token, self.erin)
        self.assertEqual((status, body['error']['code']), (409, 'invite_stale'), body)
        self.assertEqual(self.full_snapshot(), before)
        self.assertIsNone(self.record(token)['used_at'])

    def test_the_backends_usability_check_is_run_again_at_accept_for_each_project(self):
        # kittrial-5bb.581 r2 (review S3, MA1): the twin of .578's test_x3 for accept.
        token = self.existing(agent={'name': 'Erin bot', 'project': 'north'})['token']
        for refused in ('north', 'south'):
            with self.subTest(refused=refused):
                asked = []

                def unusable(handler, project_id):
                    asked.append(project_id)
                    if project_id == refused:
                        raise http_auth.HttpError(409, 'conflict', 'This record cannot be served')
                before = self.full_snapshot()
                with mock.patch.object(http_service.ApiHandler, '_require_usable', unusable):
                    status, body = self.accept(token, self.erin)
                self.assertEqual((status, body['error']['code']), (409, 'invite_stale'), body)
                self.assertIn(refused, asked)
                self.assertEqual(self.full_snapshot(), before)
                self.assertIsNone(self.record(token)['used_at'])
        self.assertEqual(self.accept(token, self.erin)[0], 201)

    def test_an_archived_project_makes_it_stale(self):
        token = self.existing()['token']
        self.assertEqual(self.call('POST', '/v1/projects/south/archive', {}, self.admin)[0], 200)
        self.assertEqual(self.accept(token, self.erin)[1]['error']['code'], 'invite_stale')

    def test_an_owner_role_whose_inviter_lost_superuser_is_stale(self):
        token = self.existing(token=self.admin, role='owner', projects=['west'])['token']
        admin_id = self.uid(ADMIN)
        self.service.state['users'][admin_id]['superuser'] = False
        self.service.state['memberships'].setdefault('west', {})[admin_id] = 'owner'
        before = self.full_snapshot()
        status, body = self.accept(token, self.erin)
        self.assertEqual((status, body['error']['code']), (409, 'invite_stale'))
        self.assertEqual(self.full_snapshot(), before)

    def test_an_agent_name_taken_since_changes_nothing(self):
        token = self.existing(agent={'name': 'Erin bot', 'project': 'north'})['token']
        self.assertEqual(self.call('POST', '/v1/agents', {'name': 'ERIN_BOT'}, self.erin)[0], 201)
        before = self.full_snapshot()
        status, body = self.accept(token, self.erin)
        self.assertEqual((status, body['error']['message']), (409, invites.AGENT_NAME_TAKEN_NOW))
        self.assertEqual(self.full_snapshot(), before)
        self.assertIsNone(self.record(token)['used_at'])

    def test_the_agent_limit_reached_since_changes_nothing(self):
        token = self.existing(agent={'name': 'Erin bot', 'project': 'north'})['token']
        before = self.full_snapshot()
        with mock.patch.object(invites, 'AGENT_MAX_PER_OWNER', 0):
            status, body = self.accept(token, self.erin)
        self.assertEqual((status, body['error']['message']), (409, invites.AGENT_LIMIT_NOW % 0))
        self.assertEqual(self.full_snapshot(), before)

    def test_expired_and_withdrawn_are_the_unknown_token_answer(self):
        expired = self.existing(expires_hours=1, projects=['north'])['token']
        self.clock.advance(3601)
        # The clock moved past the sessions' idle limit.
        self.erin = self.session('erin', 'erin-password-1')
        self.cora = self.session('cora', 'cora-password-1')
        self.assertEqual(self.generic(*self.accept(expired, self.erin)), self.UNUSABLE)
        withdrawn = self.existing(projects=['south'])
        self.assertEqual(self.call('POST', '/v1/invites/%s/revoke' % withdrawn['invite']['id'], {}, self.cora)[0], 200)
        self.assertEqual(self.generic(*self.accept(withdrawn['token'], self.erin)), self.UNUSABLE)

    def test_the_switch_off_refuses_accept(self):
        token = self.existing()['token']
        self.backend.invites_enabled = False
        status, body = self.accept(token, self.erin)
        self.assertEqual((status, body['error']['code']), (403, 'invites_disabled'))

    def test_misses_are_counted_as_sign_in_failures(self):
        calls = []
        real = self.service._record_failure

        def counting(*args, **kwargs):
            calls.append(args[0])
            return real(*args, **kwargs)
        token = self.existing()['token']
        with mock.patch.object(self.service, '_record_failure', counting):
            self.accept(token, self.carl)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith(invites.THROTTLE_PREFIX))


class RedeemNeverTouchesAnExistingAccountTests(ExistingCase):
    def test_an_existing_account_invite_at_redeem_is_the_unknown_token_answer_and_sets_no_password(self):
        token = self.existing(agent={'name': 'Erin bot', 'project': 'north'})['token']
        before = self.full_snapshot()
        status, body = self.redeem(token, 'a-password-the-attacker-chose')
        self.assertEqual(self.generic(status, body), self.UNUSABLE)
        # A password that breaks the rules gets the same answer: the kind is judged before the password.
        self.assertEqual(self.generic(*self.redeem(token, 'short')), self.UNUSABLE)
        self.assertEqual(self.full_snapshot(), before)
        self.assertEqual(self.call('POST', '/v1/sessions', {'username': 'erin', 'password': 'a-password-the-attacker-chose'})[0], 401)
        self.assertIsNone(self.record(token)['used_at'])


class AllOrNothingTests(ExistingCase):
    def test_a_failure_in_the_credential_step_takes_everything_back(self):
        self.set_role('north', 'erin', 'viewer')
        token = self.existing(agent={'name': 'Erin bot', 'project': 'south'})['token']
        before = self.full_snapshot()
        audit_before = len(self.service.state['audit'])

        def broken(*args, **kwargs):
            raise RuntimeError('the credential step failed')
        with mock.patch.object(self.service, '_issue_agent_credential_locked', broken):
            status, body = self.accept(token, self.erin)
        self.assertEqual(status, 500, body)
        self.assertEqual(self.full_snapshot(), before)
        self.assertEqual(self.role('north'), 'viewer')
        self.assertFalse([e for e in self.service.state['audit'][audit_before:] if e['outcome'] == 'committed'])
        self.assertIsNone(self.record(token)['used_at'])
        self.assertEqual(self.accept(token, self.erin)[0], 201)

    def test_a_failure_building_the_answer_takes_everything_back(self):
        # kittrial-5bb.581 r2 (review N1): the answer is built before the save, so a fault there
        # leaves no used invite whose secret was never shown.
        self.set_role('north', 'erin', 'viewer')
        token = self.existing(agent={'name': 'Erin bot', 'project': 'south'})['token']
        before = self.full_snapshot()

        def broken(*args, **kwargs):
            raise RuntimeError('the set-up step failed')
        with mock.patch.object(self.service, 'agent_setup', broken):
            status, body = self.accept(token, self.erin)
        self.assertEqual(status, 500, body)
        self.assertEqual(self.full_snapshot(), before)
        self.assertIsNone(self.record(token)['used_at'])
        status, done = self.accept(token, self.erin)
        self.assertEqual(status, 201, done)
        self.assertTrue(done['credential']['secret'])

    def test_a_failed_save_leaves_neither_memory_nor_the_file_changed(self):
        self.set_role('north', 'erin', 'viewer')
        token = self.existing(agent={'name': 'Erin bot', 'project': 'south'})['token']
        before = self.full_snapshot()
        on_disk = self.state_path.read_text(encoding='utf-8')

        def refused():
            raise OSError('disk full')
        with mock.patch.object(self.store, '_write', refused):
            status, body = self.accept(token, self.erin)
        self.assertEqual(status, 500, body)
        self.assertEqual(self.full_snapshot(), before)
        self.assertEqual(self.state_path.read_text(encoding='utf-8'), on_disk)
        self.assertEqual(self.role('north'), 'viewer')
        self.assertIsNone(self.record(token)['used_at'])
        self.assertEqual(self.accept(token, self.erin)[0], 201)


class ListAndBuildTests(ExistingCase):
    def test_the_list_says_may_invite_while_the_switch_is_off(self):
        self.backend.invites_enabled = False
        listed = self.call('GET', '/v1/invites', token=self.cora)[1]
        self.assertEqual((listed['enabled'], listed['may_invite'], [p['id'] for p in listed['projects']]),
                         (False, True, ['north', 'south']))
        listed = self.call('GET', '/v1/invites', token=self.carl)[1]
        self.assertEqual((listed['may_invite'], listed['projects']), (False, []))

    def test_every_answer_carries_the_build_of_healthz(self):
        status, health, _ = self.raw('GET', '/healthz', None, {})
        self.assertEqual(status, 200)
        for path in ('/healthz', '/v1/invites'):
            with self.subTest(path=path):
                _, _, headers = self.raw('GET', path, None, {'Authorization': 'Bearer ' + self.cora})
                self.assertEqual(headers.get(http_service.BUILD_HEADER), str(health['build_id']))
        _, _, headers = self.raw('POST', '/v1/invites/view', {'token': 'nope'}, {})
        self.assertEqual(headers.get(http_service.BUILD_HEADER), str(health['build_id']))


if __name__ == '__main__':
    unittest.main()
