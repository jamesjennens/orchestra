"""Agent-credential follow-ups of the .214 review, revision 2 (kittrial-5bb.294).

Answering review comment ``01a14350``. Item 1 (the caller's own expiry) and item 2 (the
who-can-act 401, not an oracle) were confirmed right and are unchanged. This module
carries the two blockers and the small documentation fix:

* item 3 (M1/M3): the .214 mutants live in ``http_auth.Service.agent_extension_target``,
  not on the routes revision 1 pinned. M1 lowers the project route's
  ``CAP_PROJECT_ADMIN`` to read, M3 removes the refusal of a credential caller. Both are
  pinned here by a request through the extension route, each with a positive control.
  Revision 1's service-layer tests are kept in the revision-1 module; they pin other
  routes and are not wrong.
* item 3 (cap from the last legitimate write): issuance and every extension stamp
  ``agent_written_raw``; effective expiry is
  ``min(stored expiry, agent_written_raw + 366 days)``; a record without the field falls
  back to its issuance stamp. A key first issued 360 days ago that is then extended keeps
  its full extension.
* item 4 (docs accuracy) is a documentation change in ``docs/HTTP_DEPLOYMENT.md``.

All state is synthetic and disposable; a real loopback HTTP service is started.
"""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]

import http_authority
from http_auth import now_iso
import test_http_agents as agent_harness

DAY = 86400
CAP = http_authority.CREDENTIAL_MAX_AGE_SECONDS          # 366 * DAY
YEARS = 3650 * DAY


def _without_last_used(credentials):
    """The stored rows minus the authentication last-use stamp a refused call may touch."""
    return {cid: {key: value for key, value in row.items() if key != 'last_used'}
            for cid, row in credentials.items()}


class ExtensionRouteBase(agent_harness.AgentHarness):
    """The .214 extension fixtures, without re-running the inherited .214 suites."""

    def setUp(self):
        super().setUp()
        self.tick = [self.store.now()]
        self.start = self.tick[0]
        self.store.clock = lambda: self.tick[0]

    def setup_target(self):
        admin = self.admin_token()
        uid = self.create_account(admin, 'r2-credential-owner', 'synthetic-password-1')
        token = self.login('r2-credential-owner', 'synthetic-password-1')[0]
        pid = self.create_project(admin, 'R2 extension project')
        self.service.state['memberships'][pid][uid] = 'contributor'
        self.store.save()
        aid, secret, made = self.agent_secret(token, projects=[pid], scopes=['read'])
        return admin, uid, token, pid, aid, secret, made['credential']['id']

    def path(self, aid, cid, pid=None):
        stem = '/v1/agents' if pid is None else '/v1/projects/%s/agents' % pid
        return '%s/%s/credentials/%s/extend' % (stem, aid, cid)

    def extend(self, token, aid, cid, pid=None, key=None, body=None):
        return self.request('POST', self.path(aid, cid, pid), {} if body is None else body,
                            token=token, key=key)

    def stamp(self, cid, *, issued, expiry, written=None):
        """Hand-edit one stored agent credential's stamps; ``written=None`` drops the field."""
        credential = self.service.state['credentials'][cid]
        credential['agent_issued_raw'] = issued
        if written is None:
            credential.pop('agent_written_raw', None)
        else:
            credential['agent_written_raw'] = written
        credential['expires_at'] = expiry
        credential['agent_raw_expires_at'] = expiry
        self.store.save()
        return credential


class ExtensionRouteAuthority(ExtensionRouteBase):
    """Item 3: M1 and M3, pinned on the CREDENTIAL EXTENSION route itself.

    The route's own ``_mutate`` capability is only the coarse authenticated project READ
    the caller already needs; the precise authority is decided once, in
    ``agent_extension_target``: CAP_AGENTS for the agent route, CAP_PROJECT_ADMIN for the
    project route, and any credential caller refused first there. So neither guard is
    shadowed and each test below fails when its mutant is applied.
    """

    def test_m1_the_project_route_refuses_a_project_contributor(self):
        """M1 survives unless CAP_PROJECT_ADMIN, not read, gates the project route.

        With M1 applied (``agent_extension_target``'s project capability lowered to
        ``CAP_READ``), the agent's own account - a live contributor on this project -
        passes and extends the key (200), so the refusal assertions below fail.
        """
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        before = copy.deepcopy(self.service.state['credentials'][cid])
        # uid owns the agent yet is only a contributor on this project: it may extend
        # through the agent route, never through the project route.
        refused = self.extend(token, aid, cid, pid)
        self.assertEqual(403, refused.status, refused.data)
        self.assertEqual('Project role does not permit this operation',
                         refused.data['error']['message'])
        # A coordinator is not the project's owner either.
        other = self.create_account(admin, 'r2-coordinator', 'synthetic-password-2')
        coordinator = self.login('r2-coordinator', 'synthetic-password-2')[0]
        self.service.state['memberships'][pid][other] = 'coordinator'
        self.store.save()
        with_coordinator = self.extend(coordinator, aid, cid, pid)
        self.assertEqual(403, with_coordinator.status, with_coordinator.data)
        self.assertEqual(before, self.service.state['credentials'][cid])
        # Positive controls: the project's owner (here the superuser) is allowed, and the
        # agent's own account keeps its own route.
        self.tick[0] += 60
        self.assertEqual(200, self.extend(token, aid, cid).status)
        allowed = self.extend(admin, aid, cid, pid)
        self.assertEqual(200, allowed.status, allowed.data)
        self.assertEqual(self.start + 60 + 30 * DAY,
                         self.service.state['credentials'][cid]['expires_at'])

    def test_m3_the_project_route_refuses_the_agents_own_credential(self):
        """M3 survives unless a credential caller is refused by the service's own guard.

        With M3 applied (the ``principal.via == 'credential'`` refusal removed) a live,
        project-granted, read-scoped credential passes the route's coarse READ gate and is
        then refused by the generic capability sentence instead of the extension guard's,
        so the message assertions below fail. An agent extending ITSELF is exactly the
        caller M3 stops.
        """
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        worker = self.request('POST', '/v1/projects/%s/worker-credentials' % pid,
                              {'scopes': ['read'], 'actor': 'r2-extension-worker'}, token=admin)
        self.assertEqual(201, worker.status, worker.data)
        worker_secret = worker.data['credential']['secret']
        before = copy.deepcopy(self.service.state['credentials'])
        for label, bearer in (('agent', secret), ('worker', worker_secret)):
            with self.subTest(caller=label):
                refused = self.extend(bearer, aid, cid, pid)
                self.assertEqual(403, refused.status, refused.data)
                self.assertEqual('Session authority required to extend an agent credential',
                                 refused.data['error']['message'])
        # The refusal has no effect beyond the authentication last-use stamp.
        self.assertEqual(_without_last_used(before),
                         _without_last_used(self.service.state['credentials']))
        # Positive controls: the legitimate owner session extends the key, and the agent's
        # own credential keeps the route it is allowed.
        self.assertEqual(200, self.extend(token, aid, cid).status)
        self.assertEqual(200, self.request('GET', '/v1/agents/me/next', token=secret).status)

    def test_the_agent_route_still_refuses_every_credential_caller(self):
        """The agent route's coarse gate is CAP_AGENTS; the refusal is still 403."""
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        refused = self.extend(secret, aid, cid)
        self.assertEqual(403, refused.status, refused.data)
        self.assertEqual(200, self.extend(token, aid, cid).status)


class CapFromLastWrite(ExtensionRouteBase):
    """Item 3 (review): the 366-day read cap measures from the latest LEGITIMATE WRITE."""

    def test_a_key_issued_360_days_ago_keeps_the_full_extension(self):
        """The reviewer's key: issued 360 days ago, 200-day lifetime, extended."""
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        self.service.agent_credential_ttl = 200 * DAY
        old = self.start - 360 * DAY
        credential = self.stamp(cid, issued=old, written=old, expiry=self.start + 30 * DAY)
        # Until the extension the last legitimate write is still the old issuance: the cap
        # agrees with revision 1 and leaves six days.
        self.assertEqual(old + CAP, self.service.credential_effective_expiry(credential))
        answer = self.extend(token, aid, cid, key='r2-old-key-extension')
        self.assertEqual(200, answer.status, answer.data)
        # The extension is a legitimate write: the cap moves with it, so the full
        # 200-day extension stands and the key is not cut to six days.
        self.assertEqual(self.start + 200 * DAY, credential['expires_at'])
        self.assertEqual(self.start, credential['agent_written_raw'])
        self.assertEqual(self.start + 200 * DAY,
                         self.service.credential_effective_expiry(credential))
        self.assertEqual(self.start + 200 * DAY,
                         self.service.credential_effective_raw_expiry(credential))
        view = self.service.agent_credential_expiry_view(credential)
        self.assertEqual(now_iso(self.start + 200 * DAY), view['expires_at'])
        self.assertTrue(view['working'])
        # It really lasts the extension, and then the stored raw deadline ends it.
        self.tick[0] = self.start + 200 * DAY - 1
        self.assertTrue(self.service._credential_live(credential))
        self.assertEqual(200, self.request('GET', '/v1/sessions/current', token=secret).status)
        self.tick[0] = self.start + 200 * DAY
        self.assertFalse(self.service._credential_live(credential))
        self.assertEqual(401, self.request('GET', '/v1/sessions/current', token=secret).status)

    def test_a_hand_edited_multi_year_expiry_is_cut_at_written_plus_366_days(self):
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        credential = self.stamp(cid, issued=self.start, written=self.start,
                                expiry=self.start + YEARS)
        self.assertEqual(self.start + CAP, self.service.credential_effective_expiry(credential))
        self.assertEqual(now_iso(self.start + CAP),
                         self.service.agent_credential_expiry_view(credential)['expires_at'])
        self.tick[0] = self.start + CAP - 1
        self.assertTrue(self.service._credential_live(credential))
        self.assertEqual(200, self.request('GET', '/v1/agents/me', token=secret).status)
        self.tick[0] = self.start + CAP
        self.assertFalse(self.service._credential_live(credential))
        self.assertEqual(401, self.request('GET', '/v1/agents/me', token=secret).status)

    def test_a_record_without_the_write_stamp_still_caps_from_issuance(self):
        """An old record, written before the field existed, still caps (review)."""
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        credential = self.stamp(cid, issued=self.start - 360 * DAY, written=None,
                                expiry=self.start + YEARS)
        self.assertNotIn('agent_written_raw', credential)
        self.assertEqual(self.start + 6 * DAY,
                         self.service.credential_effective_expiry(credential))
        self.tick[0] = self.start + 6 * DAY
        self.assertFalse(self.service._credential_live(credential))
        self.assertEqual(401, self.request('GET', '/v1/sessions/current', token=secret).status)

    def test_issuance_and_every_extension_stamp_the_write_clock(self):
        admin, uid, token, pid, aid, secret, cid = self.setup_target()
        credential = self.service.state['credentials'][cid]
        self.assertEqual(self.start, credential['agent_written_raw'])
        self.assertEqual(self.start, credential['agent_issued_raw'])
        self.tick[0] += 60
        self.assertEqual(200, self.extend(token, aid, cid).status)
        self.assertEqual(self.start + 60, credential['agent_written_raw'])
        self.assertEqual(self.start, credential['agent_issued_raw'])
        self.tick[0] += 60
        self.assertEqual(200, self.extend(token, aid, cid).status)
        self.assertEqual(self.start + 120, credential['agent_written_raw'])
        self.assertEqual(self.start, credential['agent_issued_raw'])


def endpoint_state(expires_at, *, issued, written=None, revoked=False):
    """One project, one owner account and one raw-stamped credential, for ``decide``."""
    credential = {
        'id': 'cred_followup_r2', 'user_id': 'u1', 'project_id': 'pp', 'actor': 'followup',
        'scopes': ['read'], 'revoked': revoked,
        'expires_at': expires_at, 'agent_issued_raw': issued,
        'agent_raw_expires_at': expires_at,
    }
    if written is not None:
        credential['agent_written_raw'] = written
    return {
        'schema_version': http_authority.SCHEMA_VERSION,
        'projects': {'pp': {'id': 'pp', 'name': 'pp'}},
        'users': {'u1': {'id': 'u1', 'display_name': 'U', 'superuser': False, 'disabled': False}},
        'memberships': {'pp': {'u1': 'owner'}},
        'sessions': {},
        'agents': {},
        'credentials': {'cred_followup_r2': credential},
    }


def endpoint_request(now, capability=None):
    return {'via': 'credential', 'user_id': 'u1', 'credential_id': 'cred_followup_r2',
            'project': 'pp', 'capability': capability or http_authority.CAP_READ, 'now': now}


class EndpointCapFromLastWrite(unittest.TestCase):
    """Item 3 (review), the canonical endpoint's kit: ``decide`` honours the write cap."""

    def test_the_endpoint_caps_from_the_write_stamp(self):
        issued = 1000000.0
        written = issued + 360 * DAY
        state = endpoint_state(issued + YEARS, issued=issued, written=written)
        # A write 360 days after issuance moves the cap 360 days out.
        self.assertEqual({'role': 'credential'},
                         http_authority.decide(state, endpoint_request(written + CAP - 1)))
        with self.assertRaises(http_authority.AuthorityDenied) as denied:
            http_authority.decide(state, endpoint_request(written + CAP))
        self.assertEqual(401, denied.exception.status)
        self.assertEqual(http_authority.CREDENTIAL_EXPIRED_MESSAGE, denied.exception.message)

    def test_the_endpoint_falls_back_to_issuance_without_the_write_stamp(self):
        issued = 1000000.0
        state = endpoint_state(issued + YEARS, issued=issued)
        self.assertEqual({'role': 'credential'},
                         http_authority.decide(state, endpoint_request(issued + CAP - 1)))
        with self.assertRaises(http_authority.AuthorityDenied) as denied:
            http_authority.decide(state, endpoint_request(issued + CAP))
        self.assertEqual(401, denied.exception.status)


if __name__ == '__main__':
    unittest.main()
