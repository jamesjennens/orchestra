"""Invite links (kittrial-5bb.578).

Every test runs the real service on a real store behind the real HTTP server on 127.0.0.1, with a
trusted proxy on loopback forwarding ``X-Forwarded-For`` (the documented deployment) and a clock the
test moves. Accounts, memberships, agents and credentials are the web service's own state
(``http_auth.Store``), so the routes are the same for both backends; the endpoint backend's only
part, its reading of the switch from deployment.private.json, is tested on its own at the end.
"""
import http.client
import json
import secrets
import shutil
import subprocess
import sqlite3
import sys
import tempfile
import threading
import types
from contextlib import closing
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import admin
import http_auth
import http_service
import invites
from http_auth import Service, Store, token_hash

ADMIN = 'root-admin'
ADMIN_PASSWORD = 'correct-horse-battery-staple'
GOOD_PASSWORD = 'a-long-enough-password'
WHERE = '203.0.113.5'


class Clock:
    def __init__(self, start=1_700_000_000.0):
        self.t = start
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.t

    def advance(self, seconds):
        with self._lock:
            self.t += seconds


class InviteCase(unittest.TestCase):
    """A superuser, a coordinator ("cora") of North and South, a contributor of South ("carl"), and
    the switch on unless a test turns it off."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.clock = Clock()
        self.state_path = self.tmp / 'state.json'
        self.store = Store(self.state_path, clock=self.clock)
        Service.bootstrap_superuser(self.store, ADMIN, ADMIN_PASSWORD)
        self.service = Service(self.store)
        self.backend = http_service.InProcessBackend(self.service)
        self.backend.invites_enabled = True
        self.httpd = http_service.create_server(self.service, self.backend, host='127.0.0.1', port=0,
                                                web_root=None, trusted_proxies=('127.0.0.1/32',))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.httpd.server_address[1]
        self.admin = self.session(ADMIN, ADMIN_PASSWORD)
        for pid, name in (('north', 'North'), ('south', 'South'), ('west', 'West')):
            status, body = self.call('POST', '/v1/projects', {'project_id': pid, 'name': name}, self.admin)
            self.assertEqual(status, 201, body)
        self.cora = self.person('cora', {'north': 'coordinator', 'south': 'coordinator'})
        self.carl = self.person('carl', {'south': 'contributor', 'north': 'contributor'})

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    # -- plumbing --------------------------------------------------------------------------------
    def call(self, method, path, body=None, token=None, source=WHERE, key=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=120)
        headers = {'X-Forwarded-For': source} if source else {}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if key:
            headers['Idempotency-Key'] = key
        data = None
        if body is not None:
            data = json.dumps(body)
            headers['Content-Type'] = 'application/json'
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
        finally:
            connection.close()

    def session(self, username, password, source='192.0.2.1'):
        status, body = self.call('POST', '/v1/sessions', {'username': username, 'password': password},
                                 source=source)
        self.assertEqual(status, 201, body)
        return body['session']['token']

    def person(self, username, roles):
        status, body = self.call('POST', '/v1/accounts', {'username': username}, self.admin)
        self.assertEqual(status, 201, body)
        uid = body['id']
        status, issued = self.call('POST', '/v1/accounts/%s/reset' % uid, {}, self.admin, key='k' + secrets.token_hex(8))
        self.assertEqual(status, 201, issued)
        status, body = self.call('POST', '/v1/accounts/%s/reset/redeem' % uid,
                                 {'reset_value': issued['reset_value'], 'new_password': username + '-password-1'})
        self.assertEqual(status, 200, body)
        for pid, role in roles.items():
            status, body = self.call('PUT', '/v1/projects/%s/members/%s' % (pid, uid), {'role': role}, self.admin)
            self.assertEqual(status, 200, body)
        return self.session(username, username + '-password-1')

    def invite(self, token=None, expect=201, **fields):
        body = {'display_name': 'Nina New', 'username': 'nina', 'projects': ['north', 'south']}
        body.update(fields)
        status, answer = self.call('POST', '/v1/invites', body, token or self.cora)
        self.assertEqual(status, expect, answer)
        return answer

    def view(self, token, source=WHERE):
        return self.call('POST', '/v1/invites/view', {'token': token}, source=source)

    def redeem(self, token, password=GOOD_PASSWORD, source=WHERE):
        return self.call('POST', '/v1/invites/redeem', {'token': token, 'password': password}, source=source)

    def generic(self, status, body):
        """The answer as a prober sees it, without the request id that differs per request."""
        return status, body['error']['code'], body['error']['message']

    def snapshot(self):
        state = self.service.state
        return (json.dumps(state['users'], sort_keys=True), json.dumps(state['usernames'], sort_keys=True),
                json.dumps(state['memberships'], sort_keys=True), json.dumps(state['agents'], sort_keys=True),
                json.dumps(state['credentials'], sort_keys=True))

    def record(self, token):
        return self.service.state['invites'][token_hash(token)]

    UNUSABLE = (404, 'invite_unusable', invites.UNUSABLE)


class HappyPathTests(InviteCase):
    def test_create_redeem_makes_the_account_memberships_agent_and_credential_and_offers_the_files(self):
        made = self.invite(agent={'name': 'Nina bot', 'project': 'south', 'claim_limit': 2}, expires_hours=48)
        token = made['token']
        self.assertGreaterEqual(len(token), 43)                       # 32 random bytes, URL-safe
        self.assertRegex(token, r'^[A-Za-z0-9_-]+$')
        self.assertEqual(made['path'], '/#/invite/' + token)
        self.assertIn('key', made['note'])
        self.assertEqual(made['invite']['status'], 'pending')
        status, shown = self.view(token)
        self.assertEqual(status, 200, shown)
        self.assertEqual((shown['display_name'], shown['username'], shown['invited_by_name']), ('Nina New', 'nina', 'cora'))
        self.assertEqual([p['name'] for p in shown['projects']], ['North', 'South'])
        self.assertEqual(shown['agent'], {'name': 'Nina bot', 'project': 'south', 'claim_limit': 2, 'role': 'worker'})
        self.assertNotIn('used_from', shown)

        status, done = self.redeem(token)
        self.assertEqual(status, 201, done)
        uid = done['user']['id']
        state = self.service.state
        self.assertEqual(state['users'][uid]['username'], 'nina')
        self.assertEqual(state['users'][uid]['display_name'], 'Nina New')
        self.assertFalse(state['users'][uid]['superuser'])
        self.assertEqual({pid: state['memberships'][pid].get(uid) for pid in ('north', 'south', 'west')},
                         {'north': 'contributor', 'south': 'contributor', 'west': None})
        agent = state['agents'][done['agent']['id']]
        self.assertEqual((agent['owner'], agent['projects'], agent['role'], agent['claim_limit'], agent['created_by']),
                         (uid, ['south'], 'worker', 2, state['usernames']['cora']))
        self.assertEqual(agent['scopes'], list(http_auth.AGENT_DEFAULT_SCOPES))
        self.assertNotIn('coordinator_grants', agent)
        # The credential, issued as New agent issues one, works at once; the files are those New agent offers.
        secret = done['credential']['secret']
        self.assertTrue(done['secret_available'])
        status, me = self.call('GET', '/v1/agents/me', token=secret)
        self.assertEqual(status, 200, me)
        self.assertEqual(me['agent']['id'], agent['id'])
        setup = done['setup']
        self.assertEqual(setup['config']['agent_id'], agent['id'])
        self.assertEqual(setup['config']['projects'], ['south'])
        self.assertEqual(setup['config_path'], '.orchestra/agent.json')
        self.assertEqual(setup['secret_file']['name'], http_auth.agent_secret_file('Nina bot')['name'])
        self.assertFalse(setup['config_contains_secret'])
        self.assertNotIn(secret, json.dumps(setup))
        # The new person signs in with the password set on the page.
        self.assertTrue(self.session('nina', GOOD_PASSWORD))
        # The inviter sees it used, by whom, when and from where; the projects' audit says so.
        status, listed = self.call('GET', '/v1/invites', token=self.cora)
        self.assertEqual(status, 200, listed)
        item = listed['items'][0]
        self.assertEqual((item['status'], item['used_by'], item['used_by_name'], item['used_from'], item['agent_id']),
                         ('used', uid, 'Nina New', WHERE, agent['id']))
        self.assertTrue(item['used_at'])
        events = [e for e in state['audit'] if e['action'] == 'invites.redeemed']
        self.assertEqual(sorted(e['project_id'] for e in events), ['north', 'south'])
        self.assertTrue(all(e['invited_by'] == state['usernames']['cora'] and e['used_from'] == WHERE for e in events))
        self.assertTrue(any(e['action'] == 'invites.create' and e['outcome'] == 'committed' for e in state['audit']))

    def test_an_invite_without_an_agent_makes_the_account_and_memberships_only(self):
        token = self.invite(projects=['north'])['token']
        status, done = self.redeem(token)
        self.assertEqual(status, 201, done)
        self.assertIsNone(done['agent'])
        self.assertIsNone(done['credential'])
        self.assertFalse(any(a['owner'] == done['user']['id'] for a in self.service.state['agents'].values()))

    def test_a_superuser_may_invite_to_any_project(self):
        self.assertEqual(self.invite(token=self.admin, projects=['west'])['invite']['projects'][0]['id'], 'west')

    def test_an_exact_retry_of_create_never_shows_the_link_again(self):
        body = {'display_name': 'Nina New', 'username': 'nina', 'projects': ['north']}
        status, first = self.call('POST', '/v1/invites', body, self.cora, key='invite-key-1')
        self.assertEqual(status, 201, first)
        status, again = self.call('POST', '/v1/invites', body, self.cora, key='invite-key-1')
        self.assertEqual(status, 200, again)
        self.assertNotIn('token', again)
        self.assertFalse(again['token_available'])
        self.assertEqual(len(self.service.state['invites']), 1)


class SingleUseExpiryRevokeTests(InviteCase):
    def test_a_second_redeem_is_refused_with_the_generic_message(self):
        token = self.invite()['token']
        self.assertEqual(self.redeem(token)[0], 201)
        before = self.snapshot()
        self.assertEqual(self.generic(*self.redeem(token, 'another-long-password')), self.UNUSABLE)
        self.assertEqual(self.generic(*self.view(token)), self.UNUSABLE)
        self.assertEqual(self.snapshot(), before)

    def test_an_expired_invite_is_refused_and_creates_nothing(self):
        token = self.invite(expires_hours=1)['token']
        self.clock.advance(3600 - 1)
        self.assertEqual(self.view(token)[0], 200)
        self.clock.advance(1)
        before = self.snapshot()
        self.assertEqual(self.generic(*self.redeem(token)), self.UNUSABLE)
        self.assertEqual(self.snapshot(), before)
        cora = self.session('cora', 'cora-password-1')              # the old session's idle window has passed
        self.assertEqual(self.call('GET', '/v1/invites', token=cora)[1]['items'][0]['status'], 'expired')

    def test_the_default_is_72_hours_and_more_than_7_days_is_refused(self):
        for bad in (7 * 24 + 1, 0, True, 2.5, '72'):
            with self.subTest(hours=bad):
                self.assertEqual(self.invite(expect=422, username='other', expires_hours=bad)['error']['code'],
                                 'invalid_payload')
        week = self.invite(username='other', expires_hours=7 * 24)['token']
        token = self.invite()['token']
        self.assertEqual(self.record(token)['ttl'], 72 * 3600)
        self.clock.advance(72 * 3600 - 1)
        self.assertEqual(self.view(token)[0], 200)
        self.clock.advance(1)
        self.assertEqual(self.generic(*self.view(token)), self.UNUSABLE)
        self.assertEqual(self.view(week)[0], 200)

    def test_a_revoked_invite_is_refused_and_only_its_inviter_or_a_superuser_may_revoke(self):
        made = self.invite()
        token, iid = made['token'], made['invite']['id']
        self.assertEqual(self.call('POST', '/v1/invites/%s/revoke' % iid, {}, self.carl)[0], 404)
        status, body = self.call('POST', '/v1/invites/%s/revoke' % iid, {}, self.cora)
        self.assertEqual((status, body['status'], body['revoked_by_name']), (200, 'revoked', 'cora'), body)
        self.assertEqual(self.generic(*self.redeem(token)), self.UNUSABLE)
        self.assertEqual(self.call('POST', '/v1/invites/%s/revoke' % iid, {}, self.cora)[0], 409)
        other = self.invite(username='other')
        status, body = self.call('POST', '/v1/invites/%s/revoke' % other['invite']['id'], {}, self.admin)
        self.assertEqual((status, body['status']), (200, 'revoked'), body)

    def test_unknown_expired_used_and_revoked_tokens_get_one_identical_answer(self):
        expired = self.invite(username='e1', expires_hours=1)['token']
        used = self.invite(username='u1')['token']
        revoked = self.invite(username='r1')
        self.assertEqual(self.redeem(used)[0], 201)
        self.call('POST', '/v1/invites/%s/revoke' % revoked['invite']['id'], {}, self.cora)
        self.clock.advance(3600)
        answers, n = set(), 10
        for token in (secrets.token_urlsafe(32), expired, used, revoked['token'], '', 'x'):
            for route in (self.view, self.redeem):
                n += 1
                with self.subTest(token=token[:6], route=route.__name__):
                    answers.add(self.generic(*route(token, source='198.51.100.%d' % n)))
        self.assertEqual(answers, {self.UNUSABLE})
        # A token that is not text at all is the same answer too.
        self.assertEqual(self.generic(*self.call('POST', '/v1/invites/redeem', {'token': 7, 'password': GOOD_PASSWORD})),
                         self.UNUSABLE)


class WhoMayInviteTests(InviteCase):
    def agent_secret(self, owner_token, project):
        status, body = self.call('POST', '/v1/agents', {'name': 'Helper', 'projects': [project]}, owner_token)
        self.assertEqual(status, 201, body)
        return body['credential']['secret']

    def test_an_agent_credential_cannot_create_list_or_revoke_invites(self):
        secret = self.agent_secret(self.cora, 'north')
        before = len(self.service.state.get('invites', {}))
        for method, path, body in (('POST', '/v1/invites', {'display_name': 'X', 'username': 'xx', 'projects': ['north']}),
                                   ('GET', '/v1/invites', None),
                                   ('POST', '/v1/invites/inv_0123456789abcdef/revoke', {})):
            with self.subTest(path=path):
                self.assertEqual(self.call(method, path, body, secret)[0], 403)
        self.assertEqual(len(self.service.state.get('invites', {})), before)

    def test_the_module_refuses_a_credential_whatever_the_route_checked(self):
        """The second layer: invites.create, list_for and revoke refuse any principal that is not a session."""
        secret = self.agent_secret(self.cora, 'north')
        principal = self.service.authenticate(secret)
        self.assertEqual(principal.via, 'credential')
        body = {'display_name': 'X', 'username': 'xx', 'projects': ['north']}
        for call in (lambda: invites.create(self.service, principal, body),
                     lambda: invites.list_for(self.service, principal),
                     lambda: invites.revoke(self.service, principal, 'inv_0123456789abcdef')):
            with self.assertRaises(http_auth.HttpError) as refused:
                call()
            self.assertEqual((refused.exception.status, refused.exception.message), (403, invites.PERSON_ONLY))
        self.assertFalse(self.service.state.get('invites'))

    def test_an_agent_holding_the_coordinator_grant_still_cannot_create_one(self):
        secret = self.agent_secret(self.cora, 'north')
        agent_id = next(a['id'] for a in self.service.state['agents'].values() if a['name'] == 'Helper')
        status, body = self.call('PUT', '/v1/projects/north/agents/%s/coordinator' % agent_id, {}, self.admin)
        self.assertIn(status, (200, 201), body)
        self.assertEqual(self.call('POST', '/v1/invites', {'display_name': 'X', 'username': 'xx',
                                                           'projects': ['north']}, secret)[0], 403)

    def test_an_inviter_cannot_name_a_project_they_do_not_administer(self):
        # carl is only a contributor in south and north: 403; west he cannot see: 404.
        self.assertEqual(self.invite(token=self.carl, expect=403, projects=['south'])['error']['code'], 'forbidden')
        self.assertEqual(self.invite(expect=404, projects=['north', 'west'])['error']['code'], 'not_found')
        self.assertEqual(self.invite(expect=404, projects=['nowhere'])['error']['code'], 'not_found')
        self.assertNotIn('invites', {k for k, v in self.service.state.items() if v})

    def test_the_create_page_lists_only_the_projects_the_caller_may_invite_to(self):
        self.assertEqual([p['id'] for p in self.call('GET', '/v1/invites', token=self.cora)[1]['projects']], ['north', 'south'])
        self.assertEqual(self.call('GET', '/v1/invites', token=self.carl)[1]['projects'], [])
        self.assertEqual([p['id'] for p in self.call('GET', '/v1/invites', token=self.admin)[1]['projects']],
                         ['north', 'south', 'west'])

    def test_no_coordinator_grant_can_be_pre_approved(self):
        for agent in ({'name': 'Boss', 'project': 'north', 'role': 'coordinator'},
                      {'name': 'Boss', 'project': 'north', 'coordinator': True},
                      {'name': 'Boss', 'project': 'north', 'grants': ['coordinator']},
                      {'name': 'Boss', 'project': 'north', 'role': 'reviewer'},
                      {'name': 'Boss', 'project': 'north', 'scopes': ['tasks', 'records']}):
            with self.subTest(agent=agent):
                answer = self.invite(expect=403, agent=agent)
                self.assertEqual(answer['error']['message'], invites.WORKER_ONLY)
        self.assertFalse(self.service.state.get('invites'))

    def test_the_agent_must_be_in_one_of_the_invites_projects(self):
        answer = self.invite(expect=422, projects=['north'], agent={'name': 'Bot', 'project': 'south'})
        self.assertEqual(answer['error']['message'], invites.AGENT_PROJECT)
        self.assertFalse(self.service.state.get('invites'))

    def test_a_role_or_other_unknown_field_is_refused(self):
        self.assertEqual(self.invite(expect=422, role='owner')['error']['code'], 'invalid_payload')

    def test_the_username_is_checked_like_account_creation(self):
        self.assertEqual(self.invite(expect=422, username='-bad')['error']['code'], 'invalid_payload')
        self.assertEqual(self.invite(expect=409, username='carl')['error']['code'], 'conflict')
        self.invite(username='nina')
        self.assertEqual(self.invite(expect=409, username='NINA')['error']['code'], 'conflict')

    def test_an_inviter_who_lost_the_role_since_cannot_have_the_invite_used(self):
        token = self.invite(projects=['north'])['token']
        cora = self.service.state['usernames']['cora']
        status, body = self.call('PUT', '/v1/projects/north/members/%s' % cora, {'role': 'contributor'}, self.admin)
        self.assertEqual(status, 200, body)
        before = self.snapshot()
        status, body = self.redeem(token)
        self.assertEqual((status, body['error']['code']), (409, 'invite_stale'), body)
        self.assertEqual(self.snapshot(), before)


class NothingHalfMadeTests(InviteCase):
    def test_a_username_taken_before_redeem_is_refused_cleanly_with_no_partial_account(self):
        token = self.invite(agent={'name': 'Nina bot', 'project': 'north'})['token']
        self.call('POST', '/v1/accounts', {'username': 'Nina'}, self.admin)       # clashes case-insensitively
        before = self.snapshot()
        status, body = self.redeem(token)
        self.assertEqual((status, body['error']['code'], body['error']['message']), (409, 'username_taken', invites.TAKEN))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.record(token)['used_at'], None)

    def test_a_password_that_breaks_the_rules_creates_nothing(self):
        token = self.invite(agent={'name': 'Nina bot', 'project': 'north'})['token']
        before = self.snapshot()
        for bad in ('short', ' ' * 20, 'x' * 1025, None):
            with self.subTest(password=repr(bad)[:12]):
                status, body = self.redeem(token, bad)
                self.assertEqual(status, 422, body)
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.record(token)['used_at'], None)
        self.assertEqual(self.redeem(token)[0], 201)

    def test_a_failure_between_the_steps_leaves_no_half_made_account(self):
        token = self.invite(agent={'name': 'Nina bot', 'project': 'north'})['token']
        before = self.snapshot()
        audit_before = len(self.service.state['audit'])

        def broken(*args, **kwargs):
            raise RuntimeError('the credential step failed')
        with mock.patch.object(self.service, '_issue_agent_credential_locked', broken):
            status, body = self.redeem(token)
        self.assertEqual(status, 500, body)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse([e for e in self.service.state['audit'][audit_before:] if e['outcome'] == 'committed'])
        self.assertEqual(self.record(token)['used_at'], None)
        self.assertEqual(self.redeem(token)[0], 201)           # still usable once the fault is gone

    def test_a_failed_save_leaves_neither_memory_nor_the_file_with_a_half_made_account(self):
        token = self.invite(agent={'name': 'Nina bot', 'project': 'north'})['token']
        before = self.snapshot()
        on_disk = self.state_path.read_text(encoding='utf-8')

        def refused():
            raise OSError('disk full')
        with mock.patch.object(self.store, '_write', refused):
            status, body = self.redeem(token)
        self.assertEqual(status, 500, body)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.state_path.read_text(encoding='utf-8'), on_disk)
        self.assertNotIn('nina', json.loads(on_disk)['usernames'])
        self.assertEqual(self.redeem(token)[0], 201)


class RateLimitTests(InviteCase):
    def test_an_address_that_keeps_guessing_is_answered_429_and_another_address_is_not(self):
        token = self.invite()['token']
        guesser = '198.51.100.66'
        for _ in range(http_auth.LOGIN_MAX_ATTEMPTS):
            self.assertEqual(self.generic(*self.view(secrets.token_urlsafe(32), source=guesser)), self.UNUSABLE)
        status, body = self.redeem(token, source=guesser)
        self.assertEqual(status, 429, body)
        self.assertEqual(self.record(token)['used_at'], None)
        # Another address is not held back by the guesser's misses.
        self.assertEqual(self.view(token, source='198.51.100.67')[0], 200)
        self.assertEqual(self.redeem(token, source='198.51.100.67')[0], 201)

    def test_a_successful_view_counts_nothing(self):
        token = self.invite()['token']
        for _ in range(http_auth.LOGIN_MAX_ATTEMPTS + 3):
            self.assertEqual(self.view(token)[0], 200)
        self.assertEqual(self.redeem(token)[0], 201)


class SwitchTests(InviteCase):
    def test_switch_off_refuses_create_view_and_redeem_with_the_one_sentence(self):
        token = self.invite()['token']
        self.backend.invites_enabled = False
        before = self.snapshot()
        answers = [self.call('POST', '/v1/invites', {'display_name': 'O', 'username': 'other', 'projects': ['north']}, self.cora),
                   self.view(token), self.redeem(token)]
        for status, body in answers:
            self.assertEqual((status, body['error']['code'], body['error']['message']),
                             (403, 'invites_disabled', invites.INVITES_OFF))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.service.state['invites']), 1)
        listed = self.call('GET', '/v1/invites', token=self.cora)[1]
        self.assertEqual((listed['enabled'], listed['projects'], listed['off_sentence']), (False, [], invites.INVITES_OFF))
        # Withdrawing still works while it is off.
        iid = listed['items'][0]['id']
        self.assertEqual(self.call('POST', '/v1/invites/%s/revoke' % iid, {}, self.cora)[0], 200)

    def test_a_backend_without_the_reader_is_off(self):
        with mock.patch.object(http_service.InProcessBackend, 'get_invites_enabled', None):
            status, body = self.call('POST', '/v1/invites', {'display_name': 'O', 'username': 'other',
                                                             'projects': ['north']}, self.cora)
        self.assertEqual((status, body['error']['message']), (403, invites.INVITES_OFF))

    def test_the_service_local_backend_is_off_by_default(self):
        self.assertFalse(http_service.InProcessBackend(self.service).get_invites_enabled())


class StoredHashTests(InviteCase):
    def test_the_store_holds_the_tokens_hash_and_never_the_token(self):
        token = self.invite(agent={'name': 'Nina bot', 'project': 'north'})['token']
        self.store.save()
        text = self.state_path.read_text(encoding='utf-8')
        self.assertNotIn(token, text)
        self.assertIn(token_hash(token), text)
        records = Path(str(self.state_path) + http_auth.RECORD_STORE_SUFFIX)
        if records.exists():
            self.assertNotIn(token.encode(), records.read_bytes())
            with closing(sqlite3.connect(str(records))) as connection:
                dumped = '\n'.join(connection.iterdump())
            self.assertNotIn(token, dumped)
        self.assertNotIn(token, json.dumps(self.service.state['audit']))
        # Nor the password or the agent secret once it is redeemed.
        status, done = self.redeem(token)
        self.assertEqual(status, 201)
        self.store.save()
        text = self.state_path.read_text(encoding='utf-8')
        for secret in (token, GOOD_PASSWORD, done['credential']['secret']):
            self.assertNotIn(secret, text)


class EndpointSwitchTests(unittest.TestCase):
    """The endpoint backend reads the switch from deployment.private.json on every request."""

    def backend(self, root):
        fake = types.SimpleNamespace(root=str(root), CONFIGURATION_UNREADABLE=http_service.EndpointBackend.CONFIGURATION_UNREADABLE)
        fake._read_deployment_setting = lambda fn: http_service.EndpointBackend._read_deployment_setting(fake, fn)
        return lambda: http_service.EndpointBackend.get_invites_enabled(fake)

    def test_absent_false_true_and_not_a_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            read = self.backend(root)
            self.assertFalse(read())                                      # no file at all
            marker = root / 'deployment.private.json'
            for value, expected in ((None, False), (False, False), (True, True), ('yes', False), (1, False)):
                with self.subTest(value=value):
                    marker.write_text(json.dumps({} if value is None else {'invites_enabled': value}), encoding='utf-8')
                    self.assertIs(read(), expected)
            warnings = []
            marker.write_text(json.dumps({'invites_enabled': 'yes'}), encoding='utf-8')
            self.assertFalse(admin.invites_enabled(root, warnings=warnings))
            self.assertIn('invites_enabled', warnings[0])

    def test_an_unreadable_configuration_is_the_usual_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'deployment.private.json').write_text('{not json', encoding='utf-8')
            with self.assertRaises(http_auth.HttpError) as refused:
                self.backend(Path(tmp))()
            self.assertEqual(refused.exception.status, 503)


class WebPageTests(unittest.TestCase):
    """The page's modules parse, and the link is built from the answer (node, when it is installed)."""

    NODE = shutil.which('node')

    @unittest.skipIf(NODE is None, 'node is not installed')
    def test_the_touched_modules_parse_and_the_link_is_the_services_or_this_pages(self):
        web = KIT / 'web' / 'js'
        for name in ('views/invites.js', 'views/agents.js', 'views/project.js', 'app.js', 'api.js'):
            with self.subTest(module=name):
                run = subprocess.run([self.NODE, '--input-type=module', '--check'], input=(web / name).read_text(encoding='utf-8'),
                                     capture_output=True, text=True, encoding='utf-8', timeout=60)
                self.assertEqual(run.returncode, 0, run.stderr[:400])
        source = (web / 'views' / 'invites.js').read_text(encoding='utf-8')
        start = source.index('export function inviteLink')
        body = source[start:source.index('\n}\n', start) + 3].replace('export ', '')
        script = body + ("console.log(JSON.stringify([inviteLink({token: 'T'}, 'https://here'), "
                         "inviteLink({token: 'T', link: 'https://pub/#/invite/T'}, 'https://here')]));")
        run = subprocess.run([self.NODE, '--input-type=module', '-e', script], capture_output=True, text=True,
                             encoding='utf-8', timeout=60)
        self.assertEqual(json.loads(run.stdout), ['https://here/#/invite/T', 'https://pub/#/invite/T'], run.stderr[:400])
        self.assertIn(invites.SHOWN_ONCE.split(':')[0], source)


class RestoreTests(unittest.TestCase):
    """A restored HTTP state withdraws the backup's pending invites, as it does its unused reset values."""

    def test_a_restore_withdraws_the_pending_invites_of_the_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'backups').mkdir()
            live = root / 'http-state.json'
            Service.bootstrap_superuser(Store(live), ADMIN, ADMIN_PASSWORD)
            store = Store(live)
            store.state['invites'] = {
                'pending-hash': {'id': 'inv_0000000000000001', 'used_at': None, 'revoked_at': None},
                'used-hash': {'id': 'inv_0000000000000002', 'used_at': '2026-01-01T00:00:00Z', 'revoked_at': None},
            }
            store.save()
            del store
            admin.backup_http_state(root)
            admin.restore_http_state(root, credentials='keep', now=1_700_000_000)
            restored = json.loads(live.read_text(encoding='utf-8'))['invites']
            self.assertEqual(restored['pending-hash']['revoked_by'], '(restore)')
            self.assertEqual(restored['pending-hash']['revoked_at'], '2023-11-14T22:13:20Z')
            self.assertIsNone(restored['used-hash']['revoked_at'])


if __name__ == '__main__':
    unittest.main()
