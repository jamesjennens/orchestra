"""Reference and capability records over the web (kittrial-5bb.213).

Covers:
1. Member operations (`CAP_RECORDS`):
   - Reference and capability lookup (`GET .../{key}`), list (`GET ...` including `waiting=true`),
     and search (`GET .../find?q=...`).
   - Propose (`POST ...`) and revise (`PATCH .../{key}` and `POST .../{key}/revisions`) for
     contributors, coordinators, owners, and agent credentials with `records` scope; viewer
     and ungranted callers refused with 403.
   - Capability alias proposal (`POST .../capabilities/{key}/aliases`) and account attribution
     (`account:<user_id>`).
2. Coordinator operations (`CAP_COORDINATE`):
   - Reference `accept` and `retire`; capability `accept`, `retire`, `verify` (`identity: 'verified'`),
     and `aliases/reject`.
   - Allowed for project coordinator, project owner, and personal agent holding the per-project
     coordinator grant; refused (403) for contributors, viewers, ungranted agents, and worker credentials.
   - Role/grant revocation stops the next coordinator action immediately (403) while keeping previously
     recorded acceptances, retirements, and verifications live (`acceptance_inert: False`,
     `verification_state: 'verified'`).
3. Superuser without project membership:
   - `decide()` is unchanged (`CAP_COORDINATE` / `CAP_RECORDS`), and `endpoint.py` refuses a superuser
     who is not a member of the project with 403 on all record write routes until they add themselves
     to the project.
4. Self-acceptance and `approval_by_another_party`:
   - With `approval_by_another_party = False` (default): a human coordinator or owner CAN accept or
     verify a draft they proposed themselves; both `proposer` and `acceptor`/`verifier` are recorded.
   - With `approval_by_another_party = True` (or installation `independent_approval = True`): a
     coordinator or owner CANNOT accept or verify a draft proposed by themselves, their own account,
     or one of their account's agents (403); a second independent coordinator CAN accept and verify it.
   - A personal agent holding a coordinator grant CANNOT accept or verify a draft proposed by its own
     party (itself, its owning account, or a sibling agent of the same account) even when
     `approval_by_another_party` is False.
5. Two-step revise-then-accept of an already-accepted key:
   - Revising an accepted reference or capability leaves the accepted revision in force (`state: 'accepted'`,
     `record.revision == 2`, `proposed.revision == 3`, `waiting_acceptance: True`) while the new draft waits
     in the waiting queue (`GET ...?waiting=true`).
   - Accepting the revised draft writes revision 4 (`acceptance_state: 'accepted'`) and new F3 acceptance
     evidence bound to revision 4.
6. Explicit `pointers_validation` reporting:
   - Reference and capability reads, acceptances, retirements, and verifications report `pointers_validation`
     explicitly (`checkout_validated: False` on web acceptance because the web service has no repository
     checkout; `checkout_validated: True` once a trusted verification is recorded).
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]

import http_authority
import http_service
import test_bd_label_aliases as fixture
import test_http_review_fixes as fixes
from http_client import Client
from test_coordinator_rules_endpoint import RulesHelpers

STRICT_ENDPOINT = KIT / 'tools' / 'strict_canonical_endpoint.py'


def _valid_ref_propose(key='kit.guide', title='Kit Guide', statement='Operational guide for the kit.'):
    return {
        'key': key,
        'title': title,
        'statement': statement,
        'authority': {'type': 'repo-path', 'path': 'docs/OPERATIONS.md', 'commit': 'a' * 40},
        'owner': 'person:maintainer',
        'review_by': '2026-12-31',
        'tags': ['ops'],
        'decisions': [],
    }


def _valid_cap_propose(key='records.web-sync', name='Web Record Sync', summary='Sync records over HTTP.'):
    return {
        'key': key,
        'name': name,
        'summary': summary,
        'owner': 'person:maintainer',
        'code': ['http_service.py::ApiHandler.references_list', 'endpoint.py::main'],
        'tests': ['tests/test_web_records.py::StrictWebRecordsTests'],
        'anchors': ['docs/HTTP_DEPLOYMENT.md#records'],
        'requirements': [],
        'aliases': ['web.records.sync'],
        'tags': ['web', 'records'],
    }


class _RecordsScenarioMixin:
    """Shared scenario assertions run against both the strict canonical endpoint and real bd."""

    def _setup_actors(self, harness):
        owner = self.owner_token
        people = {}
        for name, role in (('coord1', 'coordinator'), ('coord2', 'coordinator'),
                           ('contrib', 'contributor'), ('viewer', 'viewer')):
            uid = harness.create_account(owner, 'rec-' + name, 'rec-password-1')
            res = harness.request('PUT', f'/v1/projects/{self.pid}/members/{uid}', {'role': role}, token=owner)
            self.assertEqual(res.status, 200, res.data)
            token, csrf, user = harness.login('rec-' + name, 'rec-password-1')
            people[name] = {'uid': uid, 'token': token, 'csrf': csrf, 'user': user}

        # Create a superuser who is NOT a member of the project
        su_uid = harness.create_account(owner, 'rec-nonmember-su', 'rec-password-1')
        with harness.service.store.lock:
            harness.service.state['users'][su_uid]['superuser'] = True
            harness.service.store.save()
        su_token, _, _ = harness.login('rec-nonmember-su', 'rec-password-1')
        people['nonmember_su'] = {'uid': su_uid, 'token': su_token}

        # Create a personal agent owned by coord1
        agent_res = harness.request(
            'POST', '/v1/agents',
            {'name': 'Coord1 Agent', 'working_directory': '/tmp/coord1-agent',
             'projects': [self.pid], 'scopes': ['tasks', 'proposals', 'reviews']},
            token=people['coord1']['token'])
        self.assertEqual(agent_res.status, 201, agent_res.data)
        people['coord1_agent'] = {
            'id': agent_res.data['agent']['id'],
            'actor': agent_res.data['agent']['actor'],
            'token': agent_res.data['credential']['secret'],
        }

        # Create a second sibling agent owned by coord1
        agent2_res = harness.request(
            'POST', '/v1/agents',
            {'name': 'Coord1 Sibling Agent', 'working_directory': '/tmp/coord1-agent-2',
             'projects': [self.pid], 'scopes': ['tasks', 'proposals', 'reviews']},
            token=people['coord1']['token'])
        self.assertEqual(agent2_res.status, 201, agent2_res.data)
        people['coord1_agent2'] = {
            'id': agent2_res.data['agent']['id'],
            'actor': agent2_res.data['agent']['actor'],
            'token': agent2_res.data['credential']['secret'],
        }

        # Create a personal agent owned by coord2
        agent3_res = harness.request(
            'POST', '/v1/agents',
            {'name': 'Coord2 Agent', 'working_directory': '/tmp/coord2-agent',
             'projects': [self.pid], 'scopes': ['tasks', 'proposals', 'reviews']},
            token=people['coord2']['token'])
        self.assertEqual(agent3_res.status, 201, agent3_res.data)
        people['coord2_agent'] = {
            'id': agent3_res.data['agent']['id'],
            'actor': agent3_res.data['agent']['actor'],
            'token': agent3_res.data['credential']['secret'],
        }
        return people

    def run_lifecycle_and_two_step_suite(self):
        harness = self.harness
        people = self._setup_actors(harness)
        contrib = people['contrib']['token']
        viewer = people['viewer']['token']
        coord1 = people['coord1']['token']
        coord2 = people['coord2']['token']
        nonmember_su = people['nonmember_su']['token']

        # 1. Viewer and non-member superuser cannot propose a reference or capability (403)
        ref_body = _valid_ref_propose('kit.guide')
        r_viewer = harness.request('POST', f'/v1/projects/{self.pid}/references', ref_body,
                                   token=viewer, key='viewer-ref-propose-1')
        self.assertEqual(r_viewer.status, 403, r_viewer.data)

        r_su = harness.request('POST', f'/v1/projects/{self.pid}/references', ref_body,
                               token=nonmember_su, key='su-nonmember-ref-propose-1')
        self.assertEqual(r_su.status, 403, r_su.data)
        self.assertIn('superuser without project membership', r_su.data['error']['message'])

        # 2. Contributor proposes a reference record (revision 1, draft-only)
        r_prop = harness.request('POST', f'/v1/projects/{self.pid}/references', ref_body,
                                 token=contrib, key='contrib-ref-propose-1')
        self.assertEqual(r_prop.status, 201, r_prop.data)
        self.assertEqual(r_prop.data['key'], 'kit.guide')
        self.assertEqual(r_prop.data['revision'], 1)
        self.assertEqual(r_prop.data['state'], 'draft')
        self.assertEqual(r_prop.data['proposer'], people['contrib']['uid'])
        self.assertIn('pointers_validation', r_prop.data)
        self.assertFalse(r_prop.data['pointers_validation']['checkout_validated'])
        rev1_sha = r_prop.data['sha256']

        # Waiting list includes the draft reference
        r_wait = harness.request('GET', f'/v1/projects/{self.pid}/references?waiting=true', token=coord1)
        self.assertEqual(r_wait.status, 200, r_wait.data)
        self.assertEqual([item['key'] for item in r_wait.data['items']], ['kit.guide'])
        self.assertTrue(r_wait.data['items'][0]['waiting_acceptance'])
        self.assertTrue(r_wait.data['can_coordinate'])

        # Contributor cannot accept (403); non-member superuser cannot accept (403)
        r_c_acc = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/kit.guide/accept',
            {'expected_revision': 1, 'record_sha256': rev1_sha, 'evidence': 'Checked docs/OPERATIONS.md.'},
            token=contrib, key='contrib-ref-accept-1')
        self.assertEqual(r_c_acc.status, 403, r_c_acc.data)

        r_su_acc = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/kit.guide/accept',
            {'expected_revision': 1, 'record_sha256': rev1_sha, 'evidence': 'Checked docs/OPERATIONS.md.'},
            token=nonmember_su, key='su-nonmember-ref-accept-1')
        self.assertEqual(r_su_acc.status, 403, r_su_acc.data)
        self.assertIn('superuser without project membership', r_su_acc.data['error']['message'])

        # 3. Coordinator accepts revision 1 -> writes revision 2 (accepted) + F3 acceptance evidence
        r_acc = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/kit.guide/accept',
            {'expected_revision': 1, 'record_sha256': rev1_sha, 'evidence': 'Verified against docs/OPERATIONS.md on main.'},
            token=coord1, key='coord1-ref-accept-1')
        self.assertEqual(r_acc.status, 200, r_acc.data)
        self.assertEqual(r_acc.data['revision'], 2)
        self.assertEqual(r_acc.data['state'], 'accepted')
        self.assertEqual(r_acc.data['proposer'], people['contrib']['uid'])
        self.assertEqual(r_acc.data['acceptor'], people['coord1']['uid'])
        self.assertTrue(r_acc.data['acceptance'])
        self.assertEqual(r_acc.data['pointers_validation']['checked'], 'syntax-and-pinned-commit')
        self.assertFalse(r_acc.data['pointers_validation']['checkout_validated'])
        rev2_sha = r_acc.data['sha256']

        # Find returns the accepted reference (exact title match and scored statement candidate)
        r_find = harness.request('GET', f'/v1/projects/{self.pid}/references/find?q=Kit+Guide', token=viewer)
        self.assertEqual(r_find.status, 200, r_find.data)
        self.assertTrue(r_find.data['found'])
        self.assertEqual(r_find.data['records'][0]['key'], 'kit.guide')
        self.assertEqual(r_find.data['items'][0]['key'], 'kit.guide')
        r_find_cand = harness.request('GET', f'/v1/projects/{self.pid}/references/find?q=Operational+guide', token=viewer)
        self.assertEqual(r_find_cand.status, 200, r_find_cand.data)
        self.assertEqual(r_find_cand.data['candidates'][0]['key'], 'kit.guide')

        # 4. Two-step revise-then-accept on an already-accepted reference key:
        # Accepting a stale revision (revision 1 when revision 2 exists) is refused (409)
        r_stale_acc = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/kit.guide/accept',
            {'expected_revision': 1, 'record_sha256': rev1_sha, 'evidence': 'Stale accept'},
            token=coord1, key='coord1-ref-stale-accept-fail')
        self.assertEqual(r_stale_acc.status, 409, r_stale_acc.data)

        # Step 1: Member revises the accepted reference via PATCH -> creates draft revision 3
        # while revision 2 stays the accepted record in force!
        rev_body = _valid_ref_propose('kit.guide', statement='Updated operational guide with web records.')
        rev_body.update(expected_revision=2, expected_sha256=rev2_sha)
        r_rev = harness.request(
            'PATCH', f'/v1/projects/{self.pid}/references/kit.guide',
            rev_body,
            token=contrib, key='contrib-ref-revise-1')
        self.assertEqual(r_rev.status, 200, r_rev.data)
        self.assertEqual(r_rev.data['revision'], 3)
        self.assertEqual(r_rev.data['state'], 'draft')
        rev3_sha = r_rev.data['sha256']

        r_detail = harness.request('GET', f'/v1/projects/{self.pid}/references/kit.guide', token=coord1)
        self.assertEqual(r_detail.status, 200, r_detail.data)
        self.assertEqual(r_detail.data['state'], 'accepted')
        self.assertEqual(r_detail.data['record']['revision'], 2)
        self.assertEqual(r_detail.data['record']['statement']['text'], 'Operational guide for the kit.')
        self.assertEqual(r_detail.data['proposed']['revision'], 3)
        self.assertEqual(r_detail.data['proposed']['statement']['text'], 'Updated operational guide with web records.')
        self.assertTrue(r_detail.data['waiting_acceptance'])
        self.assertFalse(r_detail.data['acceptance_inert'])

        # Waiting queue now shows kit.guide again because it has an unaccepted revision 3!
        r_wait2 = harness.request('GET', f'/v1/projects/{self.pid}/references?waiting=true', token=coord1)
        self.assertEqual([item['key'] for item in r_wait2.data['items']], ['kit.guide'])

        # Step 2: Coordinator accepts draft revision 3 -> writes accepted revision 4
        r_acc2 = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/kit.guide/accept',
            {'expected_revision': 3, 'record_sha256': rev3_sha, 'evidence': 'Checked updated statement.'},
            token=coord1, key='coord1-ref-accept-2')
        self.assertEqual(r_acc2.status, 200, r_acc2.data)
        self.assertEqual(r_acc2.data['revision'], 4)
        self.assertEqual(r_acc2.data['state'], 'accepted')
        rev4_sha = r_acc2.data['sha256']

        r_detail2 = harness.request('GET', f'/v1/projects/{self.pid}/references/kit.guide', token=viewer)
        self.assertEqual(r_detail2.data['state'], 'accepted')
        self.assertEqual(r_detail2.data['record']['revision'], 4)
        self.assertEqual(r_detail2.data['record']['statement']['text'], 'Updated operational guide with web records.')
        self.assertIsNone(r_detail2.data['proposed'])
        self.assertFalse(r_detail2.data['waiting_acceptance'])

        # 5. Capability lifecycle: propose, accept, alias propose/reject, two-step revise-then-accept, verify, retire
        cap_body = _valid_cap_propose('records.web-sync')
        c_prop = harness.request('POST', f'/v1/projects/{self.pid}/capabilities', cap_body,
                                 token=contrib, key='contrib-cap-propose-1')
        self.assertEqual(c_prop.status, 201, c_prop.data)
        self.assertEqual(c_prop.data['revision'], 1)
        self.assertEqual(c_prop.data['state'], 'draft')
        self.assertEqual(c_prop.data['pointers_validation']['checked'], 'syntax-and-requirements')
        self.assertFalse(c_prop.data['pointers_validation']['checkout_validated'])
        cap_rev1_sha = c_prop.data['sha256']

        # Accept capability (revision 1 -> revision 2)
        c_acc = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/accept',
            {'expected_revision': 1, 'record_sha256': cap_rev1_sha, 'evidence': 'Checked http_service.py and endpoint.py.'},
            token=coord1, key='coord1-cap-accept-1')
        self.assertEqual(c_acc.status, 200, c_acc.data)
        self.assertEqual(c_acc.data['revision'], 2)
        self.assertEqual(c_acc.data['state'], 'accepted')
        self.assertEqual(c_acc.data['proposer'], people['contrib']['uid'])
        self.assertEqual(c_acc.data['acceptor'], people['coord1']['uid'])
        cap_rev2_sha = c_acc.data['sha256']

        # Propose alias by contributor (bound to account:<uid>) and reject by coordinator
        c_alias = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/aliases',
            {'alias': 'web.records.legacy', 'evidence': 'docs/HTTP_DEPLOYMENT.md#records'},
            token=contrib, key='contrib-cap-alias-1')
        self.assertEqual(c_alias.status, 201, c_alias.data)
        self.assertEqual(c_alias.data['proposer_party'], 'account:' + people['contrib']['uid'])

        c_alias_rej = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/aliases/reject',
            {'alias': 'web.records.legacy', 'reason': 'Not needed; use canonical key.'},
            token=coord1, key='coord1-cap-alias-reject-1')
        self.assertEqual(c_alias_rej.status, 200, c_alias_rej.data)
        self.assertEqual(c_alias_rej.data['state'], 'rejected')

        # Two-step revise-then-accept on accepted capability
        cap_rev_body = _valid_cap_propose('records.web-sync', summary='Sync reference and capability records over HTTP.')
        cap_rev_body.update(expected_revision=2, expected_sha256=cap_rev2_sha)
        c_rev = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/revisions',
            cap_rev_body,
            token=contrib, key='contrib-cap-revise-1')
        self.assertEqual(c_rev.status, 200, c_rev.data)
        self.assertEqual(c_rev.data['revision'], 3)
        self.assertEqual(c_rev.data['state'], 'draft')
        cap_rev3_sha = c_rev.data['sha256']

        c_get_mid = harness.request('GET', f'/v1/projects/{self.pid}/capabilities/records.web-sync', token=coord1)
        self.assertEqual(c_get_mid.data['state'], 'accepted')
        self.assertEqual(c_get_mid.data['record']['revision'], 2)
        self.assertEqual(c_get_mid.data['proposed']['revision'], 3)
        self.assertTrue(c_get_mid.data['waiting_acceptance'])

        # Coordinator verifies active accepted revision (revision 2) while draft revision 3 is pending
        c_ver = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/verify',
            {'revision': 2, 'record_sha256': cap_rev2_sha, 'commit': 'a' * 40, 'passed': True},
            token=coord1, key='coord1-cap-verify-1')
        self.assertEqual(c_ver.status, 200, c_ver.data)
        self.assertTrue(c_ver.data['trusted'])
        self.assertEqual(c_ver.data['verification_state'], 'verified')
        self.assertEqual(c_ver.data['proposer'], people['contrib']['uid'])
        self.assertEqual(c_ver.data['verifier'], people['coord1']['uid'])

        # Coordinator accepts draft revision 3 -> writes revision 4 (accepted)
        c_acc2 = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/accept',
            {'expected_revision': 3, 'record_sha256': cap_rev3_sha, 'evidence': 'Accepted revised summary.'},
            token=coord1, key='coord1-cap-accept-2')
        self.assertEqual(c_acc2.status, 200, c_acc2.data)
        self.assertEqual(c_acc2.data['revision'], 4)
        self.assertEqual(c_acc2.data['state'], 'accepted')
        cap_rev4_sha = c_acc2.data['sha256']

        # Verify revision 4 so the accepted revision itself is verified
        c_ver2 = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/verify',
            {'revision': 4, 'record_sha256': cap_rev4_sha, 'commit': 'b' * 40, 'passed': True},
            token=coord1, key='coord1-cap-verify-2')
        self.assertEqual(c_ver2.status, 200, c_ver2.data)
        self.assertEqual(c_ver2.data['verification_state'], 'verified')
        self.assertTrue(c_ver2.data['pointers_validation']['checkout_validated'])

        # Demote coord1 to contributor: their next accept is refused (403),
        # but the acceptances and verifications they already wrote stay live!
        demote = harness.request('PUT', f'/v1/projects/{self.pid}/members/{people["coord1"]["uid"]}',
                                 {'role': 'contributor'}, token=self.owner_token)
        self.assertEqual(demote.status, 200, demote.data)

        c_check_live = harness.request('GET', f'/v1/projects/{self.pid}/capabilities/records.web-sync', token=viewer)
        self.assertEqual(c_check_live.status, 200, c_check_live.data)
        self.assertEqual(c_check_live.data['state'], 'accepted')
        self.assertFalse(c_check_live.data['acceptance_inert'])
        self.assertEqual(c_check_live.data['verification_state'], 'verified')

        r_check_live = harness.request('GET', f'/v1/projects/{self.pid}/references/kit.guide', token=viewer)
        self.assertEqual(r_check_live.status, 200, r_check_live.data)
        self.assertEqual(r_check_live.data['state'], 'accepted')
        self.assertFalse(r_check_live.data['acceptance_inert'])

        # Restore coord1 to coordinator, create successor records, and retire kit.guide and records.web-sync using coord2
        harness.request('PUT', f'/v1/projects/{self.pid}/members/{people["coord1"]["uid"]}',
                        {'role': 'coordinator'}, token=self.owner_token)
        harness.request('POST', f'/v1/projects/{self.pid}/references',
                        _valid_ref_propose('kit.guide-v2', title='Kit Guide v2'),
                        token=coord2, key='coord2-ref-successor')
        r_ret = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/kit.guide/retire',
            {'expected_revision': 4, 'record_sha256': rev4_sha, 'successor': 'kit.guide-v2',
             'reason': 'Replaced by updated runbook.'},
            token=coord2, key='coord2-ref-retire-1')
        self.assertEqual(r_ret.status, 200, r_ret.data)
        self.assertEqual(r_ret.data['state'], 'superseded')

        cap_succ = _valid_cap_propose('records.web-sync-v2', name='Web Record Sync v2')
        cap_succ['aliases'] = ['web.records.sync.v2']
        harness.request('POST', f'/v1/projects/{self.pid}/capabilities',
                        cap_succ, token=coord2, key='coord2-cap-successor')
        c_ret = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/retire',
            {'expected_revision': 4, 'record_sha256': cap_rev4_sha, 'successor': 'records.web-sync-v2',
             'reason': 'Retired in test.'},
            token=coord2, key='coord2-cap-retire-1')
        self.assertEqual(c_ret.status, 200, c_ret.data)
        self.assertEqual(c_ret.data['state'], 'superseded')

    def run_self_acceptance_and_party_rules_suite(self):
        harness = self.harness
        people = self._setup_actors(harness)
        coord1 = people['coord1']['token']
        coord2 = people['coord2']['token']
        agent1 = people['coord1_agent']
        agent2 = people['coord1_agent2']
        agent3 = people['coord2_agent']

        # Grant coordinator authority to coord1_agent, coord1_agent2, and coord2_agent
        for ag in (agent1, agent2, agent3):
            g = harness.request('PUT', f'/v1/projects/{self.pid}/agents/{ag["id"]}/coordinator',
                                {}, token=self.owner_token)
            self.assertEqual(g.status, 200, g.data)

        # Case A: approval_by_another_party is OFF (default)
        # 1. Human coordinator coord1 proposes a reference and capability AND accepts/verifies them itself -> ALLOWED!
        r_self = harness.request('POST', f'/v1/projects/{self.pid}/references',
                                 _valid_ref_propose('self.human-ref', title='Self Human Ref'),
                                 token=coord1, key='self-human-ref-propose')
        self.assertEqual(r_self.status, 201, r_self.data)
        r_self_acc = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/self.human-ref/accept',
            {'expected_revision': 1, 'record_sha256': r_self.data['sha256'],
             'evidence': 'Self-accepted by human coordinator when rule is off.'},
            token=coord1, key='self-human-ref-accept')
        self.assertEqual(r_self_acc.status, 200, r_self_acc.data)
        self.assertEqual(r_self_acc.data['proposer'], people['coord1']['uid'])
        self.assertEqual(r_self_acc.data['acceptor'], people['coord1']['uid'])

        cap_self = _valid_cap_propose('self.human-cap', name='Self Human Cap')
        cap_self['aliases'] = ['self.human.cap']
        c_self = harness.request('POST', f'/v1/projects/{self.pid}/capabilities',
                                 cap_self, token=coord1, key='self-human-cap-propose')
        self.assertEqual(c_self.status, 201, c_self.data)
        c_self_acc = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/self.human-cap/accept',
            {'expected_revision': 1, 'record_sha256': c_self.data['sha256'],
             'evidence': 'Self-accepted capability by human coordinator.'},
            token=coord1, key='self-human-cap-accept')
        self.assertEqual(c_self_acc.status, 200, c_self_acc.data)
        self.assertEqual(c_self_acc.data['proposer'], people['coord1']['uid'])
        self.assertEqual(c_self_acc.data['acceptor'], people['coord1']['uid'])

        c_self_ver = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/self.human-cap/verify',
            {'revision': 2, 'record_sha256': c_self_acc.data['sha256'], 'commit': 'c' * 40, 'passed': True},
            token=coord1, key='self-human-cap-verify')
        self.assertEqual(c_self_ver.status, 200, c_self_ver.data)
        self.assertEqual(c_self_ver.data['proposer'], people['coord1']['uid'])
        self.assertEqual(c_self_ver.data['verifier'], people['coord1']['uid'])

        # 2. Even when approval_by_another_party is OFF, a coordinator-granted AGENT may NOT
        #    accept or verify a draft proposed by itself, its own human account, or a sibling agent!
        r_ag = harness.request('POST', f'/v1/projects/{self.pid}/references',
                               _valid_ref_propose('agent.party-ref', title='Agent Party Ref'),
                               token=coord1, key='coord1-for-agent-ref-propose')
        self.assertEqual(r_ag.status, 201, r_ag.data)

        # Agent1 tries to accept its owner coord1's draft -> 403!
        r_ag_acc_fail = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/agent.party-ref/accept',
            {'expected_revision': 1, 'record_sha256': r_ag.data['sha256'],
             'evidence': 'Agent trying to accept own owner draft.'},
            token=agent1['token'], key='agent1-accept-owner-fail')
        self.assertEqual(r_ag_acc_fail.status, 403, r_ag_acc_fail.data)
        self.assertIn('own party', r_ag_acc_fail.data['error']['message'])

        # Agent3 (owned by coord2, a different party) CAN accept coord1's draft!
        r_ag3_acc = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/agent.party-ref/accept',
            {'expected_revision': 1, 'record_sha256': r_ag.data['sha256'],
             'evidence': 'Independent agent3 accepts coord1 draft.'},
            token=agent3['token'], key='agent3-accept-coord1-ok')
        self.assertEqual(r_ag3_acc.status, 200, r_ag3_acc.data)
        self.assertEqual(r_ag3_acc.data['proposer'], people['coord1']['uid'])
        self.assertEqual(r_ag3_acc.data['acceptor'], agent3['actor'])

        # Case B: Turn ON approval_by_another_party!
        with harness.service.store.lock:
            harness.service.approval_by_another_party = True
            harness.service.state.setdefault('settings_seen', {})['approval_by_another_party'] = True
            harness.service.store.save()

        # 1. Coord1 proposes a reference and capability (via its agent1)
        r_other = harness.request('POST', f'/v1/projects/{self.pid}/references',
                                  _valid_ref_propose('another.party-ref', title='Another Party Ref'),
                                  token=agent1['token'], key='agent1-ref-propose-party')
        self.assertEqual(r_other.status, 201, r_other.data)

        # Detail view for coord1 reports self_accept_blocked = True
        r_other_get = harness.request('GET', f'/v1/projects/{self.pid}/references/another.party-ref', token=coord1)
        self.assertEqual(r_other_get.status, 200, r_other_get.data)
        self.assertTrue(r_other_get.data['self_accept_blocked'])

        # Coord1 (human owner of agent1) tries to accept agent1's draft while approval_by_another_party is ON -> 403!
        r_coord1_fail = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/another.party-ref/accept',
            {'expected_revision': 1, 'record_sha256': r_other.data['sha256'],
             'evidence': 'Coord1 trying to accept own agent draft.'},
            token=coord1, key='coord1-accept-own-agent-fail')
        self.assertEqual(r_coord1_fail.status, 403, r_coord1_fail.data)
        self.assertIn('approval_by_another_party', r_coord1_fail.data['error']['message'])

        # Coord2 (independent party) CAN accept it!
        r_coord2_ok = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/another.party-ref/accept',
            {'expected_revision': 1, 'record_sha256': r_other.data['sha256'],
             'evidence': 'Coord2 independently accepts agent1 draft.'},
            token=coord2, key='coord2-accept-agent1-ok')
        self.assertEqual(r_coord2_ok.status, 200, r_coord2_ok.data)
        self.assertEqual(r_coord2_ok.data['proposer'], agent1['actor'])
        self.assertEqual(r_coord2_ok.data['acceptor'], people['coord2']['uid'])

        # Same for capability accept and verify when approval_by_another_party is ON:
        cap_other = _valid_cap_propose('another.party-cap', name='Another Party Cap')
        cap_other['aliases'] = ['another.party.alias']
        c_other = harness.request('POST', f'/v1/projects/{self.pid}/capabilities',
                                  cap_other, token=coord1, key='coord1-cap-propose-party')
        self.assertEqual(c_other.status, 201, c_other.data)

        # Coord1 cannot accept its own capability draft when approval_by_another_party is ON -> 403
        c_coord1_acc_fail = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/another.party-cap/accept',
            {'expected_revision': 1, 'record_sha256': c_other.data['sha256'],
             'evidence': 'Coord1 self-accept blocked.'},
            token=coord1, key='coord1-cap-accept-fail')
        self.assertEqual(c_coord1_acc_fail.status, 403, c_coord1_acc_fail.data)
        self.assertIn('approval_by_another_party', c_coord1_acc_fail.data['error']['message'])

        # Coord2 accepts it -> 200
        c_coord2_acc_ok = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/another.party-cap/accept',
            {'expected_revision': 1, 'record_sha256': c_other.data['sha256'],
             'evidence': 'Coord2 accepts coord1 capability.'},
            token=coord2, key='coord2-cap-accept-ok')
        self.assertEqual(c_coord2_acc_ok.status, 200, c_coord2_acc_ok.data)

        # Coord1 (and its sibling agent2) still cannot verify revision 2 because coord1 was the proposer of that revision!
        for label, tok in (('coord1', coord1), ('agent2', agent2['token'])):
            c_ver_fail = harness.request(
                'POST', f'/v1/projects/{self.pid}/capabilities/another.party-cap/verify',
                {'revision': 2, 'record_sha256': c_coord2_acc_ok.data['sha256'], 'commit': 'd' * 40, 'passed': True},
                token=tok, key=f'{label}-cap-verify-fail')
            self.assertEqual(c_ver_fail.status, 403, c_ver_fail.data)

        # Coord2 CAN verify revision 2!
        c_ver_ok = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/another.party-cap/verify',
            {'revision': 2, 'record_sha256': c_coord2_acc_ok.data['sha256'], 'commit': 'd' * 40, 'passed': True},
            token=coord2, key='coord2-cap-verify-ok')
        self.assertEqual(c_ver_ok.status, 200, c_ver_ok.data)
        self.assertEqual(c_ver_ok.data['proposer'], people['coord1']['uid'])
        self.assertEqual(c_ver_ok.data['verifier'], people['coord2']['uid'])


class StrictWebRecordsTests(_RecordsScenarioMixin, unittest.TestCase):
    """Runs against tools/strict_canonical_endpoint.py (cross-platform, including Windows)."""

    def setUp(self):
        super().setUp()
        self.pid = 'pp'
        root_holder = {}

        class Harness(fixes.Harness):
            def make_backend(h):
                root = h.tmp / 'canonical'
                (root / self.pid / '.beads').mkdir(parents=True, exist_ok=True)
                (root / self.pid / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
                root_holder['root'] = root
                return http_service.EndpointBackend(
                    sys.executable, str(STRICT_ENDPOINT), str(root), service=h.service)

        self.harness = Harness(methodName='runTest')
        self.harness.setUp()
        self.addCleanup(self.harness._stop_server)
        self.root = root_holder['root']
        self.owner_token = self.harness.admin_token()
        reg = self.harness.request('POST', '/v1/projects', {'project_id': self.pid, 'name': 'Records'},
                                   token=self.owner_token)
        self.assertEqual(reg.status, 201, reg.data)

    def test_lifecycle_and_two_step_revise_then_accept(self):
        self.run_lifecycle_and_two_step_suite()

    def test_self_acceptance_and_approval_by_another_party(self):
        self.run_self_acceptance_and_party_rules_suite()

    def test_http_client_methods_round_trip(self):
        client = Client(f'http://127.0.0.1:{self.harness.port}')
        client.token = self.owner_token
        ref = client.propose_reference(self.pid, _valid_ref_propose('client.ref', title='Client Ref'), key='client-ref-1')
        self.assertEqual(ref['key'], 'client.ref')
        found_refs = client.find_references(self.pid, 'Operational guide')
        self.assertFalse(found_refs['found'])
        acc_ref = client.accept_reference(
            self.pid, 'client.ref',
            {'expected_revision': 1, 'record_sha256': ref['sha256'], 'evidence': 'Client checked.'},
            key='client-ref-acc-1')
        self.assertEqual(acc_ref['state'], 'accepted')
        rev_body = _valid_ref_propose('client.ref', title='Client Ref', statement='Revised via client.')
        rev_body.update(expected_revision=2, expected_sha256=acc_ref['sha256'])
        rev_ref = client.revise_reference(
            self.pid, 'client.ref', rev_body,
            key='client-ref-rev-1')
        self.assertEqual(rev_ref['revision'], 3)
        acc_ref2 = client.accept_reference(
            self.pid, 'client.ref',
            {'expected_revision': 3, 'record_sha256': rev_ref['sha256'], 'evidence': 'Client re-accepted.'},
            key='client-ref-acc-2')
        self.assertEqual(acc_ref2['revision'], 4)
        client.propose_reference(self.pid, _valid_ref_propose('client.ref-v2', title='Client Ref v2'), key='client-ref-succ')
        ret_ref = client.retire_reference(
            self.pid, 'client.ref',
            {'expected_revision': 4, 'record_sha256': acc_ref2['sha256'],
             'successor': 'client.ref-v2', 'reason': 'Client retired.'},
            key='client-ref-ret-1')
        self.assertEqual(ret_ref['state'], 'superseded')

        cap = client.propose_capability(self.pid, _valid_cap_propose('client.cap', name='Client Cap'), key='client-cap-1')
        self.assertEqual(cap['key'], 'client.cap')
        acc_cap = client.accept_capability(
            self.pid, 'client.cap',
            {'expected_revision': 1, 'record_sha256': cap['sha256'], 'evidence': 'Client accepted cap.'},
            key='client-cap-acc-1')
        self.assertEqual(acc_cap['state'], 'accepted')
        ver_cap = client.verify_capability(
            self.pid, 'client.cap',
            {'revision': 2, 'record_sha256': acc_cap['sha256'], 'commit': 'e' * 40, 'passed': True},
            key='client-cap-ver-1')
        self.assertEqual(ver_cap['verification_state'], 'verified')


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class NativeWebRecordsTests(_RecordsScenarioMixin, RulesHelpers, unittest.TestCase):
    """Runs against the real endpoint.py and pinned native bd."""

    def setUp(self):
        super().setUp()
        self.pid = 'pp'
        root = self.root

        class Harness(fixes.Harness):
            def make_backend(h):
                return http_service.EndpointBackend(
                    sys.executable, str(KIT / 'endpoint.py'), str(root), service=h.service)

        self.harness = Harness(methodName='runTest')
        self.harness.setUp()
        self.addCleanup(self.harness._stop_server)
        self.owner_token = self.harness.admin_token()
        reg = self.harness.request('POST', '/v1/projects', {'project_id': self.pid, 'name': 'Native Records'},
                                   token=self.owner_token)
        self.assertEqual(reg.status, 201, reg.data)

    def test_native_lifecycle_and_two_step_revise_then_accept(self):
        self.deployment(operators=['ops'], coordinator_writes=True)
        self.run_lifecycle_and_two_step_suite()

    def test_native_self_acceptance_and_approval_by_another_party(self):
        self.deployment(operators=['ops'], coordinator_writes=True, independent_approval=False)
        self.run_self_acceptance_and_party_rules_suite()


class WebRecordsScreenTests(unittest.TestCase):
    """Runs the Node DOM/screen test for web/js/views/records.js."""

    def test_records_view_dom_flows(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not on PATH')
        script = KIT / 'tests' / 'web_records_screen.mjs'
        completed = subprocess.run(
            [node, str(script)], cwd=str(KIT), capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)


if __name__ == '__main__':
    unittest.main()
