"""Agent-credential follow-ups of the .214 review (kittrial-5bb.294).

All state is synthetic and disposable; a real loopback HTTP service is started for the
route tests. The four items:

1. ``GET /v1/agents/me`` answers the CALLING credential's own expiry in the shape
   ``GET /v1/sessions/current`` already uses, and only its own.
2. The 401 at expiry names who can act - before expiry an owner extends an agent
   credential, after it the owner's account or a superuser issues a new secret - and
   discloses nothing about any other credential. A revoked credential still reads as
   revoked.
3. Two service-layer checks, pinned behaviourally with a positive control each:
   M1, a project route's capability check (how an agent must not reach a project route
   its grant does not allow), and M3, the refusal of a credential caller on a
   session-only route.
4. A hand-edited store expiry of years is capped at 366 days from issuance ON READ, in
   BOTH kits: the web service (``http_auth``) and the canonical endpoint's decision
   (``http_authority.decide``).
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]

import http_authority
from http_auth import now_iso
import test_http_agents as agent_harness
import test_agent_credential_extension as extension

DAY = 86400
CAP = http_authority.CREDENTIAL_MAX_AGE_SECONDS          # 366 * DAY
YEARS = 3650 * DAY


class AgentMeExpiryTests(extension.ExtensionHttp):
    """Items 1, 2 and the service half of item 4, over the real HTTP service."""

    def second_credential(self, token, aid, key='followups-second-credential'):
        answer = self.request('POST', '/v1/agents/%s/credentials' % aid, {}, token=token, key=key)
        self.assertEqual(201, answer.status, answer.data)
        return answer.data['credential']['id'], answer.data['credential']['secret']

    def hand_edit(self, cid, expires, raw=None):
        credential = self.service.state['credentials'][cid]
        credential['expires_at'] = expires
        credential['agent_raw_expires_at'] = expires if raw is None else raw
        self.store.save()
        return credential

    # -- item 1 ---------------------------------------------------------------
    def test_agents_me_answers_the_calling_credentials_own_expiry(self):
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        credential = self.service.state['credentials'][cid]
        me = self.request('GET', '/v1/agents/me', token=secret)
        self.assertEqual(200, me.status, me.data)
        who = self.request('GET', '/v1/sessions/current', token=secret)
        self.assertEqual(200, who.status, who.data)
        # The SAME shape and meaning the sessions route already answers.
        self.assertEqual({'id', 'expires_at', 'working', 'revoked', 'expires_soon'},
                         set(me.data['credential_expiry']))
        self.assertEqual(who.data['credential_expiry'], me.data['credential_expiry'])
        self.assertEqual(cid, me.data['credential_expiry']['id'])
        self.assertEqual(now_iso(credential['expires_at']),
                         me.data['credential_expiry']['expires_at'])
        self.assertFalse(me.data['credential_expiry']['expires_soon'])
        self.assertIs(False, me.data['expires_soon'])

    def test_agents_me_expires_soon_follows_the_seven_day_window(self):
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        for days, soon in ((8, False), (7, True), (1, True)):
            with self.subTest(days=days):
                self.hand_edit(cid, self.tick[0] + days * DAY)
                me = self.request('GET', '/v1/agents/me', token=secret)
                self.assertEqual(200, me.status, me.data)
                self.assertEqual(soon, me.data['credential_expiry']['expires_soon'])
                self.assertEqual(soon, me.data['expires_soon'])

    def test_agents_me_shows_only_the_callers_own_credential(self):
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        other_id, other_secret = self.second_credential(token, aid)
        first = self.request('GET', '/v1/agents/me', token=secret)
        other = self.request('GET', '/v1/agents/me', token=other_secret)
        self.assertNotEqual(cid, other_id)
        # The new expiry field answers THIS caller's credential, never the other one's.
        self.assertEqual(cid, first.data['credential_expiry']['id'])
        self.assertEqual(other_id, other.data['credential_expiry']['id'])
        self.assertNotEqual(first.data['credential_expiry']['id'],
                            other.data['credential_expiry']['id'])
        self.assertNotEqual(other_id, first.data['credential_expiry']['id'])
        self.assertNotEqual(cid, other.data['credential_expiry']['id'])

    def test_a_credential_without_an_expiry_reads_null_and_false(self):
        """The same null/false answer ``/v1/sessions/current`` gives for no expiry."""
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        credential = self.service.state['credentials'][cid]
        credential.pop('expires_at')
        credential.pop('agent_raw_expires_at')
        view = self.service.agent_credential_expiry_view(credential)
        self.assertIsNone(view['expires_at'])
        self.assertFalse(view['expires_soon'])
        # A worker credential has no expiry view on the sessions route, exactly as before.
        worker = self.request('POST', '/v1/projects/%s/worker-credentials' % pid,
                              {'scopes': ['read'], 'actor': 'synthetic-followup-worker'},
                              token=admin)
        self.assertEqual(201, worker.status, worker.data)
        who = self.request('GET', '/v1/sessions/current',
                           token=worker.data['credential']['secret'])
        self.assertEqual(200, who.status, who.data)
        self.assertNotIn('credential_expiry', who.data)

    # -- item 2 ---------------------------------------------------------------
    def test_the_expiry_401_names_who_can_act_and_no_other_credential(self):
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        other_id, other_secret = self.second_credential(token, aid, key='followups-other-cred')
        self.hand_edit(cid, self.tick[0])
        answer = self.request('GET', '/v1/agents/me', token=secret)
        self.assertEqual(401, answer.status, answer.data)
        message = answer.data['error']['message']
        self.assertIn('expired', message.lower())
        self.assertIn('owner', message)
        self.assertIn('superuser', message)
        self.assertIn('extend', message)
        self.assertIn('new secret', message)
        body = answer.body.decode()
        # Nothing about any other credential: no other id or secret, and no count.
        self.assertNotIn(other_id, body)
        self.assertNotIn(other_secret, body)
        self.assertFalse(any(character.isdigit() for character in message), message)

    def test_a_revoked_credential_still_reads_as_revoked(self):
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        credential = self.hand_edit(cid, self.tick[0] + DAY)
        credential['revoked'] = True
        self.store.save()
        answer = self.request('GET', '/v1/agents/me', token=secret)
        self.assertEqual(401, answer.status, answer.data)
        message = answer.data['error']['message']
        self.assertIn('revoked', message.lower())
        self.assertIn('new secret', message)
        self.assertNotIn('expired', message.lower())

    def test_the_endpoint_decision_carries_the_same_expiry_sentence(self):
        """The canonical endpoint's authorization says the same who-can-act sentence."""
        state = endpoint_state(self.tick[0] + YEARS)
        request = endpoint_request(self.tick[0] + CAP)
        with self.assertRaises(http_authority.AuthorityDenied) as denied:
            http_authority.decide(state, request)
        self.assertEqual((401, 'unauthenticated'),
                         (denied.exception.status, denied.exception.code))
        self.assertEqual(http_authority.CREDENTIAL_EXPIRED_MESSAGE, denied.exception.message)

    # -- item 4, the web-service kit ------------------------------------------
    def test_beyond_the_cap_reads_capped_and_expires_soon_follows(self):
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        self.hand_edit(cid, self.tick[0] + YEARS)
        capped = now_iso(self.start + CAP)
        me = self.request('GET', '/v1/agents/me', token=secret)
        self.assertEqual(200, me.status, me.data)
        self.assertEqual(capped, me.data['credential_expiry']['expires_at'])
        self.assertFalse(me.data['credential_expiry']['expires_soon'])
        who = self.request('GET', '/v1/sessions/current', token=secret)
        self.assertEqual(capped, who.data['credential_expiry']['expires_at'])
        # Inside the seven-day window before the CAP, expires_soon follows the capped value.
        self.tick[0] = self.start + CAP - DAY
        me = self.request('GET', '/v1/agents/me', token=secret)
        self.assertEqual(200, me.status, me.data)
        self.assertEqual(capped, me.data['credential_expiry']['expires_at'])
        self.assertTrue(me.data['credential_expiry']['expires_soon'])
        self.assertTrue(me.data['expires_soon'])

    def test_the_366_day_boundary_is_pinned_on_read(self):
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        credential = self.hand_edit(cid, self.tick[0] + YEARS)
        self.tick[0] = self.start + CAP - 1
        self.assertTrue(self.service._credential_live(credential))
        self.assertEqual(200, self.request('GET', '/v1/agents/me', token=secret).status)
        self.tick[0] = self.start + CAP
        self.assertFalse(self.service._credential_live(credential))
        self.assertEqual(401, self.request('GET', '/v1/agents/me', token=secret).status)

    def test_an_expiry_inside_the_cap_is_untouched(self):
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        inside = self.tick[0] + 200 * DAY
        credential = self.hand_edit(cid, inside)
        self.assertTrue(self.service._credential_live(credential))
        me = self.request('GET', '/v1/agents/me', token=secret)
        self.assertEqual(200, me.status, me.data)
        self.assertEqual(now_iso(inside), me.data['credential_expiry']['expires_at'])
        self.assertFalse(me.data['credential_expiry']['expires_soon'])


class ServiceLayerChecks(agent_harness.AgentHarness):
    """Item 3: M1 (project-route check) and M3 (credential caller), behaviourally."""

    def setUp(self):
        super().setUp()
        self.admin = self.admin_token()
        self.uid = self.create_account(self.admin, 'followup-member', 'followup-password-1')
        self.member = self.login('followup-member', 'followup-password-1')[0]
        self.pid = self.create_project(self.admin, 'Service layer checks')
        self.service.state['memberships'][self.pid][self.uid] = 'contributor'
        self.store.save()
        self.aid, self.secret, _ = self.agent_secret(self.member, projects=[self.pid],
                                                     scopes=['read'])
        worker = self.request('POST', '/v1/projects/%s/worker-credentials' % self.pid,
                              {'scopes': ['read'], 'actor': 'synthetic-layer-worker'},
                              token=self.admin)
        self.assertEqual(201, worker.status, worker.data)
        self.worker = worker.data['credential']['secret']

    def test_m1_a_project_route_check_is_not_lowered_to_read(self):
        """M1 survives unless a read grant is refused a project ADMIN route.

        The mutant lowers ``audit_list``'s ``self._project(ctx, CAP_PROJECT_ADMIN)`` to the
        ``CAP_READ`` default: the read-only agent credential and the read-only member then
        both get the audit listing (200) and the refusals below fail.
        """
        path = '/v1/projects/%s/audit' % self.pid
        refused = self.request('GET', path, token=self.secret)
        self.assertEqual(403, refused.status, refused.data)
        self.assertEqual('Credential scope does not permit this operation',
                         refused.data['error']['message'])
        read_only = self.request('GET', path, token=self.member)
        self.assertEqual(403, read_only.status, read_only.data)
        self.assertEqual('Project role does not permit this operation',
                         read_only.data['error']['message'])
        # Positive control: an owner (the superuser) reads the same route.
        allowed = self.request('GET', path, token=self.admin)
        self.assertEqual(200, allowed.status, allowed.data)

    def test_m3_a_session_only_route_refuses_every_credential_caller(self):
        """M3 survives unless a credential caller is stopped by the service's own check.

        The mutant removes ``me_work``'s ``principal.via == 'credential'`` refusal: both
        credential callers below then read the account's work (200) instead of the
        service's refusal, and the test fails.
        """
        path = '/v1/me/work'
        for label, bearer in (('agent', self.secret), ('worker', self.worker)):
            with self.subTest(caller=label):
                answer = self.request('GET', path, token=bearer)
                self.assertEqual(403, answer.status, answer.data)
                self.assertEqual('Session authority required; an agent reads /v1/agents/me/next',
                                 answer.data['error']['message'])
        # Positive controls: the account's own session reads it, and the agent has its route.
        self.assertEqual(200, self.request('GET', path, token=self.member).status)
        self.assertEqual(200, self.request('GET', '/v1/agents/me/next', token=self.secret).status)


def endpoint_state(expires_at, issued=1000000.0, scopes=None, revoked=False):
    """One project, one owner account and one raw-stamped credential, for ``decide``."""
    return {
        'schema_version': http_authority.SCHEMA_VERSION,
        'projects': {'pp': {'id': 'pp', 'name': 'pp'}},
        'users': {'u1': {'id': 'u1', 'display_name': 'U', 'superuser': False, 'disabled': False}},
        'memberships': {'pp': {'u1': 'owner'}},
        'sessions': {},
        'agents': {},
        'credentials': {
            'cred_followup': {
                'id': 'cred_followup', 'user_id': 'u1', 'project_id': 'pp', 'actor': 'followup',
                'scopes': list(scopes or ['read']), 'revoked': revoked,
                'expires_at': expires_at, 'agent_issued_raw': issued,
                'agent_raw_expires_at': expires_at,
            },
        },
    }


def endpoint_request(now, capability=None):
    return {'via': 'credential', 'user_id': 'u1', 'credential_id': 'cred_followup',
            'project': 'pp', 'capability': capability or http_authority.CAP_READ, 'now': now}


class EndpointReadCapTests(unittest.TestCase):
    """Item 4, the canonical endpoint's kit: ``http_authority.decide`` honours the cap."""

    def test_beyond_the_cap_the_endpoint_refuses(self):
        issued = 1000000.0
        state = endpoint_state(issued + YEARS, issued=issued)
        # Inside the cap the hand-edited store is still honoured...
        self.assertEqual({'role': 'credential'},
                         http_authority.decide(state, endpoint_request(issued + 200 * DAY)))
        # ...and at the 366-day boundary the read cap refuses, whatever the store says.
        self.assertEqual({'role': 'credential'},
                         http_authority.decide(state, endpoint_request(issued + CAP - 1)))
        with self.assertRaises(http_authority.AuthorityDenied) as denied:
            http_authority.decide(state, endpoint_request(issued + CAP))
        self.assertEqual(401, denied.exception.status)
        self.assertEqual(http_authority.CREDENTIAL_EXPIRED_MESSAGE, denied.exception.message)

    def test_an_expiry_inside_the_cap_is_untouched_by_the_endpoint(self):
        issued = 1000000.0
        inside = issued + 200 * DAY
        state = endpoint_state(inside, issued=issued)
        self.assertEqual({'role': 'credential'},
                         http_authority.decide(state, endpoint_request(inside - 1)))
        with self.assertRaises(http_authority.AuthorityDenied):
            http_authority.decide(state, endpoint_request(inside))

    def test_a_revoked_credential_reads_as_revoked_at_the_endpoint(self):
        issued = 1000000.0
        state = endpoint_state(issued + DAY, issued=issued, revoked=True)
        with self.assertRaises(http_authority.AuthorityDenied) as denied:
            http_authority.decide(state, endpoint_request(issued))
        self.assertEqual(http_authority.CREDENTIAL_REVOKED_MESSAGE, denied.exception.message)


if __name__ == '__main__':
    unittest.main()
