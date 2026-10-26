"""Reference and capability records over the web (kittrial-5bb.213).

Covers:
1. Member operations (`CAP_RECORDS`):
   - Reference and capability lookup (`GET .../{key}`), list (`GET ...` including `waiting=true`),
     and search (`GET .../find?q=...`).
   - Propose (`POST ...`) and revise (`PATCH .../{key}` and `POST .../{key}/revisions`) for
     contributors, coordinators, owners, and agent credentials with `records` scope (or a
     per-project `coordinator` grant); viewer, ungranted `tasks`-only agents, and non-member
     superusers refused with 403.
   - Capability alias proposal (`POST .../capabilities/{key}/aliases`) and account attribution
     (`account:<user_id>`).
2. Coordinator operations (`CAP_COORDINATE`):
   - Reference `accept` and `retire`; capability `accept`, `retire`, `verify` (`identity: 'attested'`),
     and `aliases/reject`.
   - Server-bound `acceptance.approvers` (forged approvers refused with 422) and `acceptance.owners`.
   - Allowed for project coordinator, project owner, and personal agent holding the per-project
     coordinator grant; refused (403) for contributors, viewers, ungranted agents, and worker credentials.
   - Role/grant revocation stops the next coordinator action immediately (403) while keeping previously
     recorded acceptances, retirements, and verifications live (`acceptance_inert: False`,
     `verification_state: 'attested'`).
3. Superuser without project membership:
   - `decide()` is unchanged (`CAP_COORDINATE` / `CAP_RECORDS`), and both `http_service.py` and
     `endpoint.py` refuse a superuser who is not a member of the project with 403 on all record
     write routes (even before payload validation) until they add themselves to the project.
4. Self-acceptance, multi-revision draft chains, and `approval_by_another_party`:
   - With `approval_by_another_party = False` (default): a human coordinator or owner CAN accept or
     verify a draft they proposed themselves; both `proposer` and `acceptor`/`verifier` are recorded.
   - With `approval_by_another_party = True` (or installation `independent_approval = True`): a
     coordinator or owner CANNOT accept or verify a draft where any author in the unaccepted draft
     revision chain belongs to their own party (403); an independent coordinator CAN accept and verify it.
   - A personal agent holding a coordinator grant CANNOT accept or verify a draft where any author
     in the unaccepted draft revision chain belongs to its own party even when
     `approval_by_another_party` is False.
5. Two-step revise-then-accept of an already-accepted key:
   - Revising an accepted reference or capability leaves the accepted revision in force (`state: 'accepted'`,
     `record.revision == 2`, `proposed.revision == 3`, `waiting_acceptance: True`) while the new draft waits
     in the waiting queue (`GET ...?waiting=true`).
   - Accepting the revised draft writes revision 4 (`acceptance_state: 'accepted'`) and new F3 acceptance
     evidence bound to revision 4.
6. Explicit `pointers_validation` and web attestation (`attested`) reporting:
   - Reference and capability reads, acceptances, retirements, and verifications report `pointers_validation`
     explicitly (`checkout_validated: False` on web acceptance and web attestation because the web service
     has no repository checkout; `verification_state: 'attested'`, `trusted: False`, `attested: True`).
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]

import http_authority
import http_client
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


def _valid_cap_results(passed=True):
    pointers = [
        'http_service.py::ApiHandler.references_list',
        'endpoint.py::main',
        'tests/test_web_records.py::StrictWebRecordsTests',
        'docs/HTTP_DEPLOYMENT.md#records',
    ]
    return [
        {'pointer': p, 'resolved': bool(passed), 'reason': None if passed else 'missing-pointer'}
        for p in pointers
    ]


class _RecordsScenarioMixin:
    """Shared scenario assertions run against both the strict canonical endpoint and real bd."""

    # -- kittrial-5bb.418: a design document over HTTP (TRACEABILITY_DESIGN §5.1; Q2's conditions) -------

    def _design_body(self, text, key='design.web-design'):
        import hashlib
        body = _valid_ref_propose(key=key, title='Web design', statement='The selected web design.')
        body['authority'] = {'type': 'repo-document', 'path': 'docs/design/WEB.md', 'commit': 'b' * 40,
                             'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
        body['document'] = text
        return body

    def _raw(self, harness, path, headers, body=b'', close_write=False, wait=10):
        """One hand-written request: answers None when the server closed without answering."""
        import socket
        lines = ['POST %s HTTP/1.1' % path, 'Host: 127.0.0.1'] + ['%s: %s' % item for item in headers.items()]
        connection = socket.create_connection(('127.0.0.1', harness.port), timeout=wait)
        try:
            connection.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('ascii') + body)
            if close_write:
                connection.shutdown(socket.SHUT_WR)
            received = b''
            while True:
                try:
                    piece = connection.recv(65536)
                except (ConnectionResetError, socket.timeout):
                    break
                if not piece:
                    break
                received += piece
                if b'\r\n\r\n' in received:
                    head, _, rest = received.partition(b'\r\n\r\n')
                    length = [int(line.split(b':', 1)[1]) for line in head.split(b'\r\n')
                              if line.lower().startswith(b'content-length:')]
                    if length and len(rest) >= length[0]:
                        break
        finally:
            connection.close()
        if not received:
            return None
        return int(received.split(b' ', 2)[1])

    def run_design_document_suite(self):
        harness = self.harness
        people = self._setup_actors(harness)
        path = f'/v1/projects/{self.pid}/references'
        text = '<!-- design-v1\r\nscope: web-design\r\nstatus: candidate\r\n-->\r\n# Web design\r\n\r\n## Plan phase 1\r\n'

        def auth(name):
            return {'Authorization': 'Bearer ' + people[name]['token']}

        big = 2 * 1024 * 1024
        # Q2: the larger body is never read for an anonymous or under-privileged caller. The server answers
        # from the headers alone (the body is never sent here), so it read nothing.
        self.assertEqual(self._raw(harness, path, {'Content-Type': 'application/json',
                                                   'Content-Length': str(1024 * 1024)}), 401)
        self.assertEqual(self._raw(harness, path, dict(auth('viewer'), **{
            'Content-Type': 'application/json', 'Content-Length': str(1024 * 1024)})), 403)
        # A contributor past the role check: over DESIGN_BODY_BYTES is refused before a byte is read.
        self.assertEqual(self._raw(harness, path, dict(auth('contrib'), **{
            'Content-Type': 'application/json', 'Content-Length': str(big)})), 413)
        # A revise of a key that is not a design entry may not send more than the normal limit.
        self.assertEqual(self._raw(harness, path + '/kit.guide/revisions', dict(auth('contrib'), **{
            'Content-Type': 'application/json', 'Content-Length': str(300 * 1024)})), 413)
        # Content-Length is required, digits only; chunked transfer is refused.
        self.assertEqual(self._raw(harness, path, dict(auth('contrib'), **{'Content-Type': 'application/json'})), 411)
        self.assertEqual(self._raw(harness, path, dict(auth('contrib'), **{
            'Content-Type': 'application/json', 'Content-Length': '+12'}), b'{}' * 6), 422)
        self.assertEqual(self._raw(harness, path, dict(auth('contrib'), **{
            'Content-Type': 'application/json', 'Content-Length': '12abc'}), b'{}' * 6), 422)
        self.assertEqual(self._raw(harness, path, dict(auth('contrib'), **{
            'Content-Type': 'application/json', 'Transfer-Encoding': 'chunked'}), b'0\r\n\r\n'), 422)
        # A body over the normal limit whose key is not a design entry is refused once read.
        plain = _valid_ref_propose(key='kit.padded', statement='s')
        plain['title'] = 'x' * 10
        raw = json.dumps(dict(plain, decisions=[]) | {'tags': ['ops']}).encode('utf-8')
        padded = raw[:-1] + b' ' * (300 * 1024) + b'}'
        self.assertEqual(self._raw(harness, path, dict(auth('contrib'), **{
            'Content-Type': 'application/json', 'Content-Length': str(len(padded))}), padded), 413)
        # A Content-Length shorter than the body: the cut body is not JSON.
        good = json.dumps(self._design_body(text)).encode('utf-8')
        self.assertEqual(self._raw(harness, path, dict(auth('contrib'), **{
            'Content-Type': 'application/json', 'Content-Length': str(len(good) - 10)}), good), 422)
        # A Content-Length longer than the body: the client went away; nothing is answered or written.
        self.assertIsNone(self._raw(harness, path, dict(auth('contrib'), **{
            'Content-Type': 'application/json', 'Content-Length': str(len(good) + 100)}), good, close_write=True))
        self.assertEqual(harness.request('GET', path + '/design.web-design', token=people['coord1']['token']).status,
                         404)
        # The design document itself, over 256 KiB of JSON and well under 1.5 MiB, by a contributor.
        long_text = text + ('word ' * 70000) + '\r\n'
        proposed = harness.request('POST', path, self._design_body(long_text), token=people['contrib']['token'],
                                   key='design-web-1')
        self.assertEqual(proposed.status, 201, proposed.data)
        self.assertEqual(proposed.data['document']['bytes'], len(long_text.encode('utf-8')))
        # The idempotency receipt binds the body read by the route: the same key with another document is refused.
        again = harness.request('POST', path, self._design_body(long_text + 'more\r\n'),
                                token=people['contrib']['token'], key='design-web-1')
        self.assertGreaterEqual(again.status, 400, again.data)
        draft = harness.request('GET', path + '/design.web-design', token=people['coord1']['token'])
        self.assertEqual(draft.status, 200, draft.data)
        accepted = harness.request('POST', path + '/design.web-design/accept',
                                   {'expected_revision': 1, 'record_sha256': proposed.data['sha256'],
                                    'evidence': 'Read the stored design document.'},
                                   token=people['coord2']['token'], key='design-web-accept')
        self.assertEqual(accepted.status, 200, accepted.data)
        # Part 2: the design page. A member reads it; every caller who may not and every scope that is not
        # there get one and the same 404 (hidden equals absent).
        page = harness.request('GET', f'/v1/projects/{self.pid}/designs/web-design', token=people['viewer']['token'])
        self.assertEqual(page.status, 200, page.data)
        self.assertEqual(page.data['document'], long_text)
        self.assertEqual((page.data['index_state'], page.data['verification']['state']), ('matches', 'unverified'))
        harness.create_account(self.owner_token, 'rec-outsider', 'rec-password-1')
        outsider, _, _ = harness.login('rec-outsider', 'rec-password-1')
        absent = [harness.request('GET', f'/v1/projects/{self.pid}/designs/{scope}', token=token)
                  for scope, token in (('web-design', outsider), ('no-such-design', outsider),
                                       ('no-such-design', people['viewer']['token']),
                                       ('kit', people['viewer']['token']))]
        self.assertEqual({(r.status, r.data['error']['code'], r.data['error']['message']) for r in absent},
                         {(404, 'not_found', 'Design not found')})
        # Without a session every scope gets the authentication answer, the same whether it exists or not.
        anonymous = [harness.request('GET', f'/v1/projects/{self.pid}/designs/{scope}')
                     for scope in ('web-design', 'no-such-design')]
        self.assertEqual({(r.status, r.data['error']['code']) for r in anonymous}, {(401, 'unauthenticated')})
        # Q4: a member who may propose files a report; it opens design-drift marked untrusted.
        authority = self._design_body(long_text)['authority']
        report = {'doc_sha256': authority['sha256'], 'path': authority['path'], 'commit': authority['commit'],
                  'observed_sha256': '0' * 64, 'result': 'fail', 'reason': 'mismatch'}
        for name in ('viewer', 'nonmember_su'):
            refused = harness.request('POST', path + '/design.web-design/verifications', report,
                                      token=people[name]['token'], key='design-report-' + name)
            self.assertEqual(refused.status, 403, (name, refused.data))
        filed = harness.request('POST', path + '/design.web-design/verifications', report,
                                token=people['contrib']['token'], key='design-report-contrib')
        self.assertEqual(filed.status, 201, filed.data)
        self.assertEqual(filed.data['identity'], 'unverified')
        seen = harness.request('GET', path + '/design.web-design', token=people['viewer']['token'])
        verification = seen.data['verification']
        self.assertEqual((verification['state'], verification['trusted'], len(verification['reported_by'])),
                         ('design-drift', False, 1))
        # A viewer may not propose one even with a small body (the role, as for every reference write).
        small = harness.request('POST', path, self._design_body(text, key='design.other-web'),
                                token=people['viewer']['token'], key='design-viewer')
        self.assertEqual(small.status, 403, small.data)

    def run_trace_suite(self):
        """kittrial-5bb.425: GET /v1/projects/{pid}/trace through the endpoint's `trace` action."""
        from urllib.parse import urlencode
        harness = self.harness
        people = self._setup_actors(harness)
        pid = self.pid
        refs = f'/v1/projects/{pid}/references'
        text = '<!-- design-v1\nscope: web-trace\nstatus: candidate\n-->\n# Web trace\n\n## Storage\nSee req:GHOST here.\n'
        proposed = harness.request('POST', refs, self._design_body(text, key='design.web-trace'),
                                   token=people['contrib']['token'], key='trace-design-1')
        self.assertEqual(proposed.status, 201, proposed.data)
        accepted = harness.request('POST', refs + '/design.web-trace/accept',
                                   {'expected_revision': 1, 'record_sha256': proposed.data['sha256'],
                                    'evidence': 'Read the stored design document.'},
                                   token=people['coord2']['token'], key='trace-design-accept')
        self.assertEqual(accepted.status, 200, accepted.data)
        for number in range(3):
            made = harness.request('POST', f'/v1/projects/{pid}/tasks',
                                   {'title': 'Trace task %d' % number,
                                    'description': 'Uses req:MISSING%d and design:web-trace#nowhere' % number},
                                   token=people['coord1']['token'])
            self.assertEqual(made.status, 201, made.data)

        def get(token, project=pid, **query):
            return harness.request('GET', f'/v1/projects/{project}/trace' + ('?' + urlencode(query) if query else ''),
                                   token=token)

        # Every member reads the same rows and the same counts: no role sees more or less.
        answers = {name: get(people[name]['token'], view='tokens') for name in ('viewer', 'contrib', 'coord1',
                                                                                'coord1_agent')}
        for name, answer in answers.items():
            self.assertEqual(answer.status, 200, (name, answer.data))
        bodies = {json.dumps(answer.data, sort_keys=True) for answer in answers.values()}
        self.assertEqual(len(bodies), 1)
        tokens = answers['viewer'].data
        self.assertEqual(sorted(row['pointer'] for row in tokens['rows']),
                         ['design:web-trace#nowhere'] * 3 + ['req:GHOST', 'req:MISSING0', 'req:MISSING1',
                                                             'req:MISSING2'])
        self.assertEqual(tokens['total'], 7)
        self.assertIsNone(tokens['next_cursor'])
        # One 404 for a non-member, a superuser outside the project, an agent scoped to another project and an
        # unknown project: hidden equals absent.
        harness.create_account(self.owner_token, 'trace-outsider', 'rec-password-1')
        outsider, _, _ = harness.login('trace-outsider', 'rec-password-1')
        other = harness.request('POST', '/v1/projects', {'project_id': 'qq', 'name': 'Other'}, token=self.owner_token)
        self.assertEqual(other.status, 201, other.data)
        elsewhere = harness.request('POST', '/v1/agents', {'name': 'Elsewhere', 'working_directory': '/tmp/elsewhere',
                                                           'projects': ['qq'], 'scopes': ['tasks']},
                                    token=self.owner_token)
        self.assertEqual(elsewhere.status, 201, elsewhere.data)
        absent = [get(outsider), get(elsewhere.data['credential']['secret']),
                  get(people['viewer']['token'], project='no-such-project'), get(outsider, view='tokens')]
        self.assertEqual({(r.status, r.data['error']['code'], r.data['error']['message']) for r in absent},
                         {(404, 'not_found', 'Project not found')})
        self.assertEqual(get(None).status, 401)
        # The existing read rule decides, never trace: a caller reads the trace exactly when it reads the tasks.
        for token in (people['nonmember_su']['token'], outsider, people['viewer']['token']):
            tasks = harness.request('GET', f'/v1/projects/{pid}/tasks', token=token)
            self.assertEqual(get(token).status, 200 if tasks.status == 200 else 404, tasks.status)
        # Pages: the cursor is the caller's, for this query, at this generation.
        viewer = people['viewer']['token']
        first = get(viewer, view='tokens', limit='3')
        self.assertEqual((first.status, len(first.data['rows'])), (200, 3), first.data)
        second = get(viewer, view='tokens', limit='3', cursor=first.data['next_cursor'])
        self.assertEqual(second.status, 200, second.data)
        self.assertEqual([row['pointer'] for row in first.data['rows'] + second.data['rows']],
                         [row['pointer'] for row in tokens['rows']][:6])
        for token, query in ((people['contrib']['token'], {'view': 'tokens', 'limit': '3'}),
                             (viewer, {'view': 'tasks', 'limit': '3'})):
            stolen = get(token, cursor=first.data['next_cursor'], **query)
            self.assertEqual(stolen.status, 409, stolen.data)
        made = harness.request('POST', f'/v1/projects/{pid}/tasks', {'title': 'A later task',
                                                                     'description': 'Cites req:LATER now'},
                               token=people['coord1']['token'])
        self.assertEqual(made.status, 201, made.data)
        # No answer is kept across requests or callers: the next read of anyone sees the write.
        for name in ('viewer', 'coord1'):
            self.assertEqual(get(people[name]['token'], view='tokens').data['total'], 8, name)
        stale = get(viewer, view='tokens', limit='3', cursor=first.data['next_cursor'])
        self.assertEqual(stale.status, 409, stale.data)
        self.assertIn('The project changed since this page was read', stale.data['error']['message'])
        # Bounds and the query's shape.
        for query in ({'view': 'everything'}, {'view': 'tokens', 'limit': '51'}, {'view': 'tokens', 'sort': 'x'},
                      {'view': 'round', 'scope': 'Not A Scope'}):
            refused = get(viewer, **query)
            self.assertEqual(refused.status, 422, (query, refused.data))
        summary = get(viewer)
        self.assertEqual(summary.status, 200, summary.data)
        self.assertEqual([line['view'] for line in summary.data['rows']],
                         ['requirements', 'tokens', 'tasks', 'decisions', 'designs', 'round'])
        self.assertTrue(summary.data['designs'])

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

        # Create a personal agent owned by coord1 (with tasks/proposals/reviews only; gets CAP_RECORDS via coordinator grant)
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

        # Create a contributor agent with explicit 'records' scope
        rec_agent_res = harness.request(
            'POST', '/v1/agents',
            {'name': 'Contrib Records Agent', 'working_directory': '/tmp/contrib-records-agent',
             'projects': [self.pid], 'scopes': ['records']},
            token=people['contrib']['token'])
        self.assertEqual(rec_agent_res.status, 201, rec_agent_res.data)
        people['contrib_records_agent'] = {
            'id': rec_agent_res.data['agent']['id'],
            'actor': rec_agent_res.data['agent']['actor'],
            'token': rec_agent_res.data['credential']['secret'],
        }
        return people

    def _delegate(self, harness, *, action='grant', decision=None, title=None, grantee='coordinator-grant',
                  scope=('capabilities', 'references'), token=None, key=None, expect=201):
        """A person owner's delegation of record acceptance (kittrial-5bb.407); returns its decision id."""
        body = {'action': action, 'decision': decision, 'title': title if decision is None else None,
                'right': 'record-acceptance', 'grantee': grantee,
                'scope': [] if action == 'revoke' else sorted(scope),
                'escalate': '' if action == 'revoke' else 'requirement changes and anything costing money',
                'reason': 'The owner delegates record acceptance to the coordinator agents.'}
        import hashlib
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode('utf-8')).hexdigest()[:24]
        result = harness.request('POST', f'/v1/projects/{self.pid}/delegations', body,
                                 token=token or self.owner_token,
                                 key=key or 'delegation-' + digest)
        self.assertEqual(result.status, expect, result.data)
        return result.data.get('decision') if expect == 201 else result

    def run_lifecycle_and_two_step_suite(self):
        harness = self.harness
        people = self._setup_actors(harness)
        contrib = people['contrib']['token']
        viewer = people['viewer']['token']
        coord1 = people['coord1']['token']
        coord2 = people['coord2']['token']
        nonmember_su = people['nonmember_su']['token']
        ungranted_tasks_agent = people['coord1_agent']
        records_scoped_agent = people['contrib_records_agent']

        # 1. Viewer, ungranted tasks-only agent, and non-member superuser cannot propose (403)
        ref_body = _valid_ref_propose('kit.guide')
        r_viewer = harness.request('POST', f'/v1/projects/{self.pid}/references', ref_body,
                                   token=viewer, key='viewer-ref-propose-1')
        self.assertEqual(r_viewer.status, 403, r_viewer.data)

        # Ungranted agent with scopes=['tasks', 'proposals', 'reviews'] (no 'records' scope) is refused (403) (P7 / M1)
        r_tasks_ag = harness.request('POST', f'/v1/projects/{self.pid}/references', ref_body,
                                     token=ungranted_tasks_agent['token'], key='tasks-ag-ref-propose-fail')
        self.assertEqual(r_tasks_ag.status, 403, r_tasks_ag.data)

        # Direct endpoint invoke with ungranted coordinator-owned agent without 'records' scope is also refused (403)
        with self.assertRaises(http_service.HttpError) as m3_ctx:
            harness.backend.invoke(
                'references.propose',
                harness.service.authenticate(ungranted_tasks_agent['token']),
                self.pid,
                {
                    'command': 'propose',
                    'body': dict(_valid_ref_propose('direct.m3'), schema_version=1, operation_id='op-direct-m3'),
                },
                'direct-m3-key',
                capability=http_service.CAP_RECORDS)
        self.assertEqual(m3_ctx.exception.status, 403)

        # Non-member superuser is refused with 403 at the HTTP membership layer even on unknown_field body before _proposal_body (M4)
        r_su_malformed = harness.request('POST', f'/v1/projects/{self.pid}/references', {'unknown_field': 1},
                                         token=nonmember_su, key='su-nonmember-ref-malformed')
        self.assertEqual(r_su_malformed.status, 403, r_su_malformed.data)
        self.assertIn('superuser without project membership', r_su_malformed.data['error']['message'])

        r_su = harness.request('POST', f'/v1/projects/{self.pid}/references', ref_body,
                               token=nonmember_su, key='su-nonmember-ref-propose-1')
        self.assertEqual(r_su.status, 403, r_su.data)
        self.assertIn('superuser without project membership', r_su.data['error']['message'])

        # 2. Contributor (and contributor's 'records'-scoped agent) proposes a reference record (revision 1, draft-only)
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

        # Unauthenticated accept is refused (401); contributor cannot accept (403); non-member superuser cannot accept via HTTP (403) or direct backend invoke (M2)
        r_unauth_acc = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/kit.guide/accept',
            {'expected_revision': 1, 'record_sha256': rev1_sha, 'evidence': 'Unauth.'},
            token=None, key='unauth-ref-accept-1')
        self.assertEqual(r_unauth_acc.status, 401, r_unauth_acc.data)

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

        with self.assertRaises(http_service.HttpError) as m2_ctx:
            harness.backend.invoke(
                'references.accept',
                harness.service.authenticate(nonmember_su),
                self.pid,
                {
                    'command': 'accept',
                    'body': {
                        'key': 'kit.guide',
                        'revision': 1,
                        'record_sha256': rev1_sha,
                        'acceptance_state': 'accepted',
                        'schema_version': 1,
                        'operation_id': 'op-direct-m2',
                        'acceptance': {
                            'decision_id': 'op-direct-m2',
                            'owners': ['account:' + people['coord1']['uid']],
                            'approvers': ['account:' + people['nonmember_su']['uid']],
                            'policy': 'any-owner',
                            'evidence': 'Direct nonmember superuser accept attempt.',
                        },
                    },
                },
                'direct-m2-key',
                capability=http_service.CAP_COORDINATE)
        self.assertEqual(m2_ctx.exception.status, 403)

        # Direct effect-time verified_web_authority check in endpoint.py when block_mismatch_denial is bypassed (M3)
        if fixture.endpoint is not None and Path(harness.backend.endpoint).name == 'endpoint.py':
            from unittest import mock
            coord1_principal = harness.service.authenticate(coord1)
            m3_req = {
                'action': 'ref',
                'project': self.pid,
                'actor': coord1_principal.user_id,
                'args': ['accept', '@attachment:0'],
                'attachments': {'0': {'flag': '--file', 'text': json.dumps({
                    'key': 'kit.guide',
                    'revision': 1,
                    'record_sha256': rev1_sha,
                    'acceptance_state': 'accepted',
                    'schema_version': 1,
                    'operation_id': 'op-direct-m3-ep',
                    'acceptance': {
                        'decision_id': 'op-direct-m3-ep',
                        'owners': ['account:' + people['coord1']['uid']],
                        'approvers': ['account:' + people['coord1']['uid']],
                        'policy': 'any-owner',
                        'evidence': 'Mismatched capability descriptor.',
                    },
                })}},
                'operation_id': 'op-direct-m3-ep',
                'authority': http_authority.authority_request(
                    coord1_principal, self.pid, http_authority.CAP_RECORDS,
                    now=harness.service._expiry_now()),
            }
            auth_cfg = http_authority.AuthorityConfig(
                harness.backend.authority_store, harness.backend.authority_lock,
                harness.backend.actor_namespace)
            with mock.patch.object(fixture.endpoint, 'block_mismatch_denial', return_value=None):
                m3_ans = fixture.endpoint.execute(
                    Path(harness.backend.root), m3_req,
                    authority_config=auth_cfg, require_authority=True)
            self.assertEqual(m3_ans.get('returncode'), 2, m3_ans)
            self.assertEqual(m3_ans.get('records_status'), 403, m3_ans)
            self.assertIn('Verified web authority required', m3_ans.get('stderr', ''))

        # Accept without evidence/acceptance is refused with 422 (P7)
        r_no_ev = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/kit.guide/accept',
            {'expected_revision': 1, 'record_sha256': rev1_sha},
            token=coord1, key='coord1-no-evidence-fail')
        self.assertEqual(r_no_ev.status, 422, r_no_ev.data)

        # Forged acceptance.approvers (claiming another coordinator approved) is refused with 422 (P7b)
        r_forged_app = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/kit.guide/accept',
            {
                'expected_revision': 1,
                'record_sha256': rev1_sha,
                'acceptance': {
                    'owners': ['account:' + people['coord1']['uid']],
                    'approvers': ['account:' + people['coord2']['uid']],
                    'policy': 'standard',
                    'decision_id': None,
                    'evidence': 'Forged approver.',
                },
            },
            token=coord1, key='coord1-forged-approver-fail')
        self.assertEqual(r_forged_app.status, 422, r_forged_app.data)

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

        # Step 1: Member's 'records'-scoped agent revises the accepted reference via PATCH -> creates draft revision 3
        # while revision 2 stays the accepted record in force!
        rev_body = _valid_ref_propose('kit.guide', statement='Updated operational guide with web records.')
        rev_body.update(expected_revision=2, expected_sha256=rev2_sha)
        r_rev = harness.request(
            'PATCH', f'/v1/projects/{self.pid}/references/kit.guide',
            rev_body,
            token=records_scoped_agent['token'], key='contrib-agent-ref-revise-1')
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

        # Verify without explicit commit, results, or passed, or with an all-zero commit, is refused with 422 (P12, small-follow-ups)
        c_ver_no_results = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/verify',
            {'revision': 2, 'record_sha256': cap_rev2_sha, 'commit': 'a' * 40, 'passed': True},
            token=coord1, key='coord1-cap-verify-missing-results')
        self.assertEqual(c_ver_no_results.status, 422, c_ver_no_results.data)

        c_ver_no_commit = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/verify',
            {'revision': 2, 'record_sha256': cap_rev2_sha, 'passed': True, 'results': _valid_cap_results()},
            token=coord1, key='coord1-cap-verify-missing-commit')
        self.assertEqual(c_ver_no_commit.status, 422, c_ver_no_commit.data)

        c_ver_zero_commit = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/verify',
            {'revision': 2, 'record_sha256': cap_rev2_sha, 'commit': '0' * 40,
             'passed': True, 'results': _valid_cap_results()},
            token=coord1, key='coord1-cap-verify-zero-commit')
        self.assertEqual(c_ver_zero_commit.status, 422, c_ver_zero_commit.data)
        self.assertIn('non-zero commit SHA', c_ver_zero_commit.data['error']['message'])

        # Worker credentials endpoint: ['tasks', 'reviews'] and ['tasks'] get 403 on propose and accept;
        # ['records'] gets 201 on propose and 403 on accept (small-follow-ups)
        wc_tr = harness.request(
            'POST', f'/v1/projects/{self.pid}/worker-credentials',
            {'actor': 'wc-tasks-reviews', 'scopes': ['tasks', 'reviews']},
            token=self.owner_token, key='wc-issue-tasks-reviews')
        self.assertEqual(wc_tr.status, 201, wc_tr.data)
        wc_tr_tok = wc_tr.data['credential']['secret']
        wc_tr_prop = harness.request(
            'POST', f'/v1/projects/{self.pid}/references',
            _valid_ref_propose('wc.tr-ref', title='WC TR Ref'),
            token=wc_tr_tok, key='wc-tr-propose-fail')
        self.assertEqual(wc_tr_prop.status, 403, wc_tr_prop.data)
        wc_tr_acc = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/accept',
            {'expected_revision': 3, 'record_sha256': cap_rev3_sha, 'evidence': 'WC TR accept fail.'},
            token=wc_tr_tok, key='wc-tr-accept-fail')
        self.assertEqual(wc_tr_acc.status, 403, wc_tr_acc.data)

        wc_t = harness.request(
            'POST', f'/v1/projects/{self.pid}/worker-credentials',
            {'actor': 'wc-tasks-only', 'scopes': ['tasks']},
            token=self.owner_token, key='wc-issue-tasks-only')
        self.assertEqual(wc_t.status, 201, wc_t.data)
        wc_t_tok = wc_t.data['credential']['secret']
        wc_t_prop = harness.request(
            'POST', f'/v1/projects/{self.pid}/references',
            _valid_ref_propose('wc.t-ref', title='WC T Ref'),
            token=wc_t_tok, key='wc-t-propose-fail')
        self.assertEqual(wc_t_prop.status, 403, wc_t_prop.data)
        wc_t_acc = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/accept',
            {'expected_revision': 3, 'record_sha256': cap_rev3_sha, 'evidence': 'WC T accept fail.'},
            token=wc_t_tok, key='wc-t-accept-fail')
        self.assertEqual(wc_t_acc.status, 403, wc_t_acc.data)

        wc_rec = harness.request(
            'POST', f'/v1/projects/{self.pid}/worker-credentials',
            {'actor': 'wc-records-only', 'scopes': ['records']},
            token=self.owner_token, key='wc-issue-records-only')
        self.assertEqual(wc_rec.status, 201, wc_rec.data)
        wc_rec_tok = wc_rec.data['credential']['secret']
        wc_rec_prop = harness.request(
            'POST', f'/v1/projects/{self.pid}/references',
            _valid_ref_propose('wc.records-ref', title='WC Records Ref'),
            token=wc_rec_tok, key='wc-rec-propose-ok')
        self.assertEqual(wc_rec_prop.status, 201, wc_rec_prop.data)
        self.assertEqual(wc_rec_prop.data['proposer'], 'wc-records-only')
        wc_rec_acc = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/accept',
            {'expected_revision': 3, 'record_sha256': cap_rev3_sha, 'evidence': 'WC records accept fail.'},
            token=wc_rec_tok, key='wc-rec-accept-fail')
        self.assertEqual(wc_rec_acc.status, 403, wc_rec_acc.data)

        # Coordinator attests verification of active accepted revision (revision 2) while draft revision 3 is pending
        c_ver = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/verify',
            {'revision': 2, 'record_sha256': cap_rev2_sha, 'commit': 'a' * 40,
             'passed': True, 'results': _valid_cap_results()},
            token=coord1, key='coord1-cap-verify-1')
        self.assertEqual(c_ver.status, 200, c_ver.data)
        self.assertFalse(c_ver.data['trusted'])
        self.assertTrue(c_ver.data['attested'])
        self.assertEqual(c_ver.data['identity'], 'attested')
        self.assertEqual(c_ver.data['verification_state'], 'attested')
        self.assertEqual(c_ver.data['proposer'], people['contrib']['uid'])
        self.assertEqual(c_ver.data['verifier'], people['coord1']['uid'])
        self.assertFalse(c_ver.data['pointers_validation']['checkout_validated'])

        # Coordinator accepts draft revision 3 -> writes revision 4 (accepted)
        c_acc2 = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/accept',
            {'expected_revision': 3, 'record_sha256': cap_rev3_sha, 'evidence': 'Accepted revised summary.'},
            token=coord1, key='coord1-cap-accept-2')
        self.assertEqual(c_acc2.status, 200, c_acc2.data)
        self.assertEqual(c_acc2.data['revision'], 4)
        self.assertEqual(c_acc2.data['state'], 'accepted')
        cap_rev4_sha = c_acc2.data['sha256']

        # Attest verification of revision 4 so the accepted revision itself is attested
        c_ver2 = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/verify',
            {'revision': 4, 'record_sha256': cap_rev4_sha, 'commit': 'b' * 40,
             'passed': True, 'results': _valid_cap_results()},
            token=coord1, key='coord1-cap-verify-2')
        self.assertEqual(c_ver2.status, 200, c_ver2.data)
        self.assertEqual(c_ver2.data['verification_state'], 'attested')
        self.assertFalse(c_ver2.data['trusted'])
        self.assertTrue(c_ver2.data['attested'])
        self.assertFalse(c_ver2.data['pointers_validation']['checkout_validated'])
        self.assertEqual(c_ver2.data['pointers_validation']['checked'], 'web-coordinator-attested')

        # Demote coord1 to contributor: their replay and next coordinator actions are refused (403),
        # but the acceptances and verifications they already wrote stay live! (M8, M9)
        demote = harness.request('PUT', f'/v1/projects/{self.pid}/members/{people["coord1"]["uid"]}',
                                 {'role': 'contributor'}, token=self.owner_token)
        self.assertEqual(demote.status, 200, demote.data)

        demoted_replay = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/accept',
            {'expected_revision': 3, 'record_sha256': cap_rev3_sha, 'evidence': 'Accepted revised summary.'},
            token=coord1, key='coord1-cap-accept-2')
        self.assertEqual(demoted_replay.status, 403, demoted_replay.data)

        demoted_verify = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/verify',
            {'revision': 4, 'record_sha256': cap_rev4_sha, 'commit': 'b' * 40,
             'passed': True, 'results': _valid_cap_results()},
            token=coord1, key='coord1-demoted-verify-fail')
        self.assertEqual(demoted_verify.status, 403, demoted_verify.data)

        demoted_rej_alias = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/records.web-sync/aliases/reject',
            {'alias': 'web.records.legacy', 'reason': 'Demoted reject.'},
            token=coord1, key='coord1-demoted-reject-alias-fail')
        self.assertEqual(demoted_rej_alias.status, 403, demoted_rej_alias.data)

        c_check_live = harness.request('GET', f'/v1/projects/{self.pid}/capabilities/records.web-sync', token=viewer)
        self.assertEqual(c_check_live.status, 200, c_check_live.data)
        self.assertEqual(c_check_live.data['state'], 'accepted')
        self.assertFalse(c_check_live.data['acceptance_inert'])
        self.assertEqual(c_check_live.data['verification_state'], 'attested')
        self.assertEqual(c_check_live.data['verification']['state'], 'attested')
        self.assertTrue(c_check_live.data['verification']['verified_at']['attested'])
        self.assertFalse(c_check_live.data['pointers_validation']['checkout_validated'])

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

        # 6. Verify http_client.py round-trip and capability list filters (P13, P13b)
        self._assert_http_client_methods_round_trip()

    def _assert_http_client_methods_round_trip(self):
        client = Client(f'http://127.0.0.1:{self.harness.port}')
        client.token = self.owner_token
        ref = client.propose_reference(self.pid, _valid_ref_propose('client.ref', title='Client Ref'), key='client-ref-1')
        self.assertEqual(ref['key'], 'client.ref')
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

        cap_body = _valid_cap_propose('client.cap', name='Client Cap')
        cap_body['aliases'] = ['client.cap.alias']
        cap = client.propose_capability(self.pid, cap_body, key='client-cap-1')
        self.assertEqual(cap['key'], 'client.cap')
        acc_cap = client.accept_capability(
            self.pid, 'client.cap',
            {'expected_revision': 1, 'record_sha256': cap['sha256'], 'evidence': 'Client accepted cap.'},
            key='client-cap-acc-1')
        self.assertEqual(acc_cap['state'], 'accepted')
        ver_cap = client.verify_capability(
            self.pid, 'client.cap',
            {'revision': 2, 'record_sha256': acc_cap['sha256'], 'commit': 'e' * 40,
             'passed': True, 'results': _valid_cap_results()},
            key='client-cap-ver-1')
        self.assertEqual(ver_cap['verification_state'], 'attested')

        # Create non-matching capabilities to verify list_capabilities filters EXCLUDE non-matching items
        nonmatch_tag_body = _valid_cap_propose('client.other-tag', name='Client Other Tag')
        nonmatch_tag_body['aliases'] = ['client.other.tag']
        nonmatch_tag_body['tags'] = ['cli-only']
        nm_tag = client.propose_capability(self.pid, nonmatch_tag_body, key='client-nm-tag-1')
        client.accept_capability(
            self.pid, 'client.other-tag',
            {'expected_revision': 1, 'record_sha256': nm_tag['sha256'], 'evidence': 'Accepted other-tag cap.'},
            key='client-nm-tag-acc-1')

        nonmatch_owner_body = _valid_cap_propose('client.other-owner', name='Client Other Owner')
        nonmatch_owner_body['aliases'] = ['client.other.owner']
        nonmatch_owner_body['owner'] = 'person:somebody-else'
        nm_owner = client.propose_capability(self.pid, nonmatch_owner_body, key='client-nm-owner-1')
        client.accept_capability(
            self.pid, 'client.other-owner',
            {'expected_revision': 1, 'record_sha256': nm_owner['sha256'], 'evidence': 'Accepted other-owner cap.'},
            key='client-nm-owner-acc-1')

        # Exercise list_capabilities with tag, owner, state, pointers, waiting (P13, P13b, small-follow-ups)
        listed = client.list_capabilities(
            self.pid, tag='web', owner='person:maintainer', state='accepted', pointers=True, waiting=False)
        keys = [item['key'] for item in listed['items']]
        self.assertIn('client.cap', keys)
        self.assertNotIn('client.other-tag', keys)
        self.assertNotIn('client.other-owner', keys)
        self.assertNotIn('records.web-sync', keys)
        self.assertNotIn('records.web-sync-v2', keys)
        cap_item = next(item for item in listed['items'] if item['key'] == 'client.cap')
        self.assertIn('code', cap_item)

        listed_waiting = client.list_capabilities(self.pid, waiting=True)
        waiting_keys = [item['key'] for item in listed_waiting['items']]
        self.assertIn('records.web-sync-v2', waiting_keys)
        self.assertNotIn('client.cap', waiting_keys)
        self.assertNotIn('client.other-tag', waiting_keys)
        self.assertNotIn('client.other-owner', waiting_keys)
        self.assertNotIn('records.web-sync', waiting_keys)

        parser = http_client._build_parser()
        parsed = parser.parse_args([
            'capability', self.pid, 'list', '--tag', 'web', '--owner', 'person:maintainer',
            '--state', 'accepted', '--pointers', '--waiting',
        ])
        self.assertEqual(parsed.tag, ['web'])
        self.assertEqual(parsed.owner, 'person:maintainer')
        self.assertEqual(parsed.state, 'accepted')
        self.assertTrue(parsed.pointers)
        self.assertTrue(parsed.waiting)

    def run_delegation_suite(self, plant=None):
        """Delegated decision rights (kittrial-5bb.407), one case per adversarial condition.

        ``plant(decision, text, author)`` writes a raw native comment past every endpoint guard,
        as an older kit or a lane with direct native access could; None skips those cases.
        """
        harness = self.harness
        people = self._setup_actors(harness)
        coord1, coord2 = people['coord1'], people['coord2']
        agent1, agent2, agent3 = people['coord1_agent'], people['coord1_agent2'], people['coord2_agent']
        pid = self.pid
        for ag in (agent1, agent2, agent3):
            g = harness.request('PUT', f'/v1/projects/{pid}/agents/{ag["id"]}/coordinator', {}, token=self.owner_token)
            self.assertEqual(g.status, 200, g.data)

        def propose(key, token, label):
            r = harness.request('POST', f'/v1/projects/{pid}/references',
                                _valid_ref_propose(key, title='Delegation ' + label), token=token, key='prop-' + key)
            self.assertEqual(r.status, 201, r.data)
            return r.data

        def propose_cap(key, token):
            body = _valid_cap_propose(key, name='Delegation ' + key)
            body['aliases'] = [key + '.alias']
            r = harness.request('POST', f'/v1/projects/{pid}/capabilities', body, token=token, key='prop-' + key)
            self.assertEqual(r.status, 201, r.data)
            return r.data

        def accept(key, draft, token, label, decision=None, family='references'):
            body = {'expected_revision': draft['revision'], 'record_sha256': draft['sha256'],
                    'evidence': 'Accepted in the delegation suite (%s).' % label}
            if decision is not None:
                body['decision_id'] = decision
            return harness.request('POST', f'/v1/projects/{pid}/{family}/{key}/accept', body, token=token,
                                   key='acc-%s-%s' % (key, label))

        # 1. Only a person owner's own web session records a delegation.
        for label, token in (('agent', agent1['token']), ('person coordinator', coord1['token']),
                             ('contributor', people['contrib']['token']), ('viewer', people['viewer']['token']),
                             ('non-member superuser', people['nonmember_su']['token'])):
            refused = self._delegate(harness, title='Refused ' + label, token=token,
                                     key='deleg-refused-' + label.replace(' ', '-'), expect=403)
            self.assertEqual(refused.status, 403, (label, refused.data))
            if label in ('agent', 'person coordinator', 'non-member superuser'):
                # Refused by the web route itself, before the endpoint is asked (the endpoint's
                # own refusals end "Nothing was written."); the endpoint refuses again below.
                self.assertNotIn('Nothing was written', refused.data['error']['message'], label)
        listed = harness.request('GET', f'/v1/projects/{pid}/delegations', token=people['viewer']['token'])
        self.assertEqual(listed.status, 200, listed.data)
        self.assertEqual(listed.data['items'], [])
        self.assertEqual({a['agent']: a['covered'] for a in listed.data['granted_agents']},
                         {agent1['id']: False, agent2['id']: False, agent3['id']: False})

        # 2. Without a delegation every agent acceptance is refused, its own party's or not.
        sibling_draft = propose('deleg.sibling', agent2['token'], 'sibling')
        r = accept('deleg.sibling', sibling_draft, agent1['token'], 'no-decision')
        self.assertEqual(r.status, 403, r.data)
        # The web service refuses it itself, before the endpoint is asked.
        self.assertIn("name the person owner's delegation decision", r.data['error']['message'])
        other_draft = propose('deleg.other', coord1['token'], 'other party')
        r = accept('deleg.other', other_draft, agent3['token'], 'other-no-decision')
        self.assertEqual(r.status, 403, r.data)

        # 3. The owner's delegation, on a new decision: the own-party refusal is lifted for it.
        decision = self._delegate(harness, title='Coordinator agents accept records')
        listed = harness.request('GET', f'/v1/projects/{pid}/delegations', token=people['viewer']['token'])
        self.assertTrue(all(a['covered'] for a in listed.data['granted_agents']), listed.data)
        self.assertTrue(listed.data['items'][0]['in_force'])
        self.assertEqual(listed.data['items'][0]['delegation']['via'], 'session')
        r = accept('deleg.sibling', sibling_draft, agent1['token'], 'sibling-ok', decision=decision)
        self.assertEqual(r.status, 200, r.data)
        self.assertEqual((r.data['proposer'], r.data['acceptor']), (agent2['actor'], agent1['actor']))
        self.assertEqual(r.data['acceptance']['decision_id'], decision)

        # 3b. Review r1 (b1): the delegation lifts ONLY the agent-only own-party refusal.
        #     An agent never accepts its own draft, delegation or not.
        own_draft = propose('deleg.own', agent1['token'], 'own')
        r = accept('deleg.own', own_draft, agent1['token'], 'own-draft', decision=decision)
        self.assertEqual(r.status, 403, r.data)
        self.assertIn('wrote itself', r.data['error']['message'])
        own_view = harness.request('GET', f'/v1/projects/{pid}/references/deleg.own', token=agent1['token'])
        self.assertTrue(own_view.data['self_accept_blocked'], own_view.data)
        self.assertTrue(own_view.data['needs_delegation'], own_view.data)
        sibling_view = harness.request('GET', f'/v1/projects/{pid}/references/deleg.own', token=agent3['token'])
        self.assertFalse(sibling_view.data['self_accept_blocked'], sibling_view.data)
        #     Its owner's draft is the own-party case the delegation lifts (independence off).
        owners_draft = propose('deleg.owners', coord1['token'], 'owners draft')
        r = accept('deleg.owners', owners_draft, agent1['token'], 'owners-draft', decision=decision)
        self.assertEqual(r.status, 200, r.data)
        #     With the project's independence setting on, the agent faces the person rule: its
        #     owner's draft is refused despite the delegation, another party's agent may accept.
        with harness.service.store.lock:
            harness.service.approval_by_another_party = True
            harness.service.state.setdefault('settings_seen', {})['approval_by_another_party'] = True
            harness.service.store.save()
        try:
            independent_draft = propose('deleg.independent', coord1['token'], 'independent')
            r = accept('deleg.independent', independent_draft, agent1['token'], 'independence-own-party',
                       decision=decision)
            self.assertEqual(r.status, 403, r.data)
            self.assertIn('approval by another party', r.data['error']['message'])
            r = accept('deleg.independent', independent_draft, agent3['token'], 'independence-other-party',
                       decision=decision)
            self.assertEqual(r.status, 200, r.data)
        finally:
            with harness.service.store.lock:
                harness.service.approval_by_another_party = False
                harness.service.state.setdefault('settings_seen', {})['approval_by_another_party'] = False
                harness.service.store.save()

        # 4. A decision id that is not a current delegation, each with its reason.
        task = harness.request('POST', f'/v1/projects/{pid}/tasks', {'title': 'An ordinary task'}, token=coord1['token'])
        self.assertEqual(task.status, 201, task.data)
        for label, named, reason in (('task', task.data['id'], 'not a decision issue'),
                                     ('missing', pid + '-nosuch', 'does not exist'),
                                     ('another-project', 'qq-1', 'does not exist'),
                                     ('operation-id', 'op-not-a-decision', 'does not exist')):
            r = accept('deleg.other', other_draft, agent3['token'], 'bad-' + label, decision=named)
            self.assertEqual(r.status, 403, (label, r.data))
            self.assertIn(reason, r.data['error']['message'].lower(), label)

        # 5. A delegation granted to one agent admits no other.
        single = self._delegate(harness, title='Only agent3', grantee=agent3['id'], scope=('references',))
        r = accept('deleg.other', other_draft, agent1['token'], 'wrong-agent', decision=single)
        self.assertEqual(r.status, 403, r.data)
        self.assertIn('granted to', r.data['error']['message'])

        # 6. Narrowed: references only now; a capability acceptance is refused, a reference one not.
        self._delegate(harness, action='narrow', decision=decision, scope=('references',), key='narrow-1')
        cap = propose_cap('deleg.cap', agent2['token'])
        r = accept('deleg.cap', cap, agent1['token'], 'cap-narrowed', decision=decision, family='capabilities')
        self.assertEqual(r.status, 403, r.data)
        self.assertIn('covers references, not capabilities', r.data['error']['message'])
        r = accept('deleg.other', other_draft, agent3['token'], 'ref-after-narrow', decision=decision)
        self.assertEqual(r.status, 200, r.data)

        # 7. Revoked: the next acceptance is refused.
        self._delegate(harness, action='revoke', decision=decision, key='revoke-1')
        r3 = propose('deleg.after-revoke', agent2['token'], 'after revoke')
        r = accept('deleg.after-revoke', r3, agent1['token'], 'revoked', decision=decision)
        self.assertEqual(r.status, 403, r.data)
        self.assertIn('revoked', r.data['error']['message'])

        # 8. The grant removed: the delegation names coordinator-grant holders, so agent1 is out.
        current = self._delegate(harness, title='Again, for the grant case')
        dropped = harness.request('DELETE', f'/v1/projects/{pid}/agents/{agent1["id"]}/coordinator',
                                  token=self.owner_token)
        self.assertEqual(dropped.status, 200, dropped.data)
        r = accept('deleg.after-revoke', r3, agent1['token'], 'grant-removed', decision=current)
        self.assertEqual(r.status, 403, r.data)

        # 9. A person coordinator is unaffected and needs no delegation; contributors and viewers
        #    stay refused.
        r4 = propose('deleg.person', agent2['token'], 'person')
        for label, token in (('contributor', people['contrib']['token']), ('viewer', people['viewer']['token'])):
            r = accept('deleg.person', r4, token, label, decision=current)
            self.assertEqual(r.status, 403, (label, r.data))
        r = accept('deleg.person', r4, coord2['token'], 'person-coordinator')
        self.assertEqual(r.status, 200, r.data)

        # 9b. The endpoint's own gates, reached past the web service's (a direct backend call):
        #     an agent's web-verify that names no delegation, and a superuser who is not an
        #     owner recording a delegation, are both refused there too.
        cap_ok = accept('deleg.cap', cap, coord2['token'], 'person-cap', family='capabilities')
        self.assertEqual(cap_ok.status, 200, cap_ok.data)
        with self.assertRaises(http_service.HttpError) as direct_verify:
            harness.backend.invoke(
                'capabilities.verify', harness.service.authenticate(agent3['token']), pid,
                {'command': 'web-verify',
                 'body': {'key': 'deleg.cap', 'revision': cap_ok.data['revision'],
                          'record_sha256': cap_ok.data['sha256'], 'commit': 'e' * 40, 'passed': True,
                          'results': _valid_cap_results(), 'schema_version': 1,
                          'operation_id': 'op-direct-verify-no-decision'}},
                'direct-verify-no-decision', capability=http_service.CAP_COORDINATE)
        self.assertEqual(direct_verify.exception.status, 403)
        # Within the delegation the same agent (another party; never the draft's own author)
        # verifies the capability.
        verified = harness.request(
            'POST', f'/v1/projects/{pid}/capabilities/deleg.cap/verify',
            {'revision': cap_ok.data['revision'], 'record_sha256': cap_ok.data['sha256'], 'commit': 'e' * 40,
             'passed': True, 'results': _valid_cap_results(), 'decision_id': current},
            token=agent3['token'], key='deleg-cap-verify-ok')
        self.assertEqual(verified.status, 200, verified.data)
        self.assertEqual(verified.data['verifier'], agent3['actor'])
        with self.assertRaises(http_service.HttpError) as direct_grant:
            harness.backend.invoke(
                'delegations.grant', harness.service.authenticate(people['nonmember_su']['token']), pid,
                {'action': 'grant', 'decision': None, 'title': 'A superuser tries', 'right': 'record-acceptance',
                 'grantee': 'coordinator-grant', 'scope': ['references'], 'escalate': 'everything',
                 'reason': 'Not an owner of this project.'},
                'direct-su-grant', capability=http_service.CAP_PROJECT_ADMIN)
        self.assertEqual(direct_grant.exception.status, 403)

        # 10. The owner demoted: the delegation they recorded is no longer in force.
        r5 = propose('deleg.demoted', agent2['token'], 'demoted')
        r = accept('deleg.demoted', r5, agent3['token'], 'before-demotion', decision=current)
        self.assertEqual(r.status, 200, r.data)
        r6 = propose('deleg.demoted2', agent2['token'], 'demoted 2')
        owner_uid = harness.service.authenticate(self.owner_token).user_id
        with harness.service.store.lock:
            harness.service.state['memberships'][pid][owner_uid] = 'coordinator'
            harness.service.store.save()
        try:
            r = accept('deleg.demoted2', r6, agent3['token'], 'after-demotion', decision=current)
            self.assertEqual(r.status, 403, r.data)
            self.assertIn('no longer a person owner', r.data['error']['message'])
        finally:
            with harness.service.store.lock:
                harness.service.state['memberships'][pid][owner_uid] = 'owner'
                harness.service.store.save()

        # 11. Lookalikes grant nothing: a raw record planted past the guards, in the owner's own
        #     name and exact form, has no host journal entry.
        if plant is not None:
            import delegations

            def text_for(issue):
                fake = delegations.make_record(
                    {'schema_version': 1, 'operation_id': 'op-plant', 'decision': issue, 'title': None,
                     'action': 'grant', 'right': 'record-acceptance', 'grantee': 'coordinator-grant',
                     'scope': ['capabilities', 'references'], 'escalate': 'nothing', 'reason': 'planted'},
                    project=pid, recorded_by=owner_uid, via='session', account=owner_uid,
                    at='2026-10-25T00:00:00Z')
                return delegations.body(fake)
            planted = plant(text_for, owner_uid)
            r = accept('deleg.demoted2', r6, agent3['token'], 'planted', decision=planted)
            self.assertEqual(r.status, 403, r.data)
            self.assertIn('no delegation is recorded', r.data['error']['message'].lower())

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
            {'revision': 2, 'record_sha256': c_self_acc.data['sha256'], 'commit': 'c' * 40,
             'passed': True, 'results': _valid_cap_results()},
            token=coord1, key='self-human-cap-verify')
        self.assertEqual(c_self_ver.status, 200, c_self_ver.data)
        self.assertEqual(c_self_ver.data['proposer'], people['coord1']['uid'])
        self.assertEqual(c_self_ver.data['verifier'], people['coord1']['uid'])

        # Direct backend invoke of capabilities.verify by agent1 (same party as coord1) is refused (403) (M7)
        with self.assertRaises(http_service.HttpError) as m7_ctx:
            harness.backend.invoke(
                'capabilities.verify',
                harness.service.authenticate(agent1['token']),
                self.pid,
                {
                    'command': 'verify',
                    'body': {
                        'key': 'self.human-cap',
                        'revision': 2,
                        'record_sha256': c_self_acc.data['sha256'],
                        'commit': 'c' * 40,
                        'passed': True,
                        'results': _valid_cap_results(),
                        'schema_version': 1,
                        'operation_id': 'op-direct-m7',
                    },
                },
                'direct-m7-key',
                capability=http_service.CAP_COORDINATE)
        self.assertEqual(m7_ctx.exception.status, 403)

        # 2. Even when approval_by_another_party is OFF, a coordinator-granted AGENT may NOT
        #    accept or verify a draft proposed by itself, its own human account, or a sibling agent! (M5, M6)
        r_ag = harness.request('POST', f'/v1/projects/{self.pid}/references',
                               _valid_ref_propose('agent.party-ref', title='Agent Party Ref'),
                               token=coord1, key='coord1-for-agent-ref-propose')
        self.assertEqual(r_ag.status, 201, r_ag.data)

        # Agent1 tries to accept its owner coord1's draft via HTTP -> 403 (M5)
        r_ag_acc_fail = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/agent.party-ref/accept',
            {'expected_revision': 1, 'record_sha256': r_ag.data['sha256'],
             'evidence': 'Agent trying to accept own owner draft.'},
            token=agent1['token'], key='agent1-accept-owner-fail')
        self.assertEqual(r_ag_acc_fail.status, 403, r_ag_acc_fail.data)
        # kittrial-5bb.407: an agent accepts only within a person owner's delegation, named as
        # decision_id; without one it is refused before the own-party rule is even asked.
        self.assertIn('delegation', r_ag_acc_fail.data['error']['message'])

        # Direct backend invoke of references.accept by agent1 (bypassing http_service precheck) is also refused (403) (M6)
        with self.assertRaises(http_service.HttpError) as m6_ctx:
            harness.backend.invoke(
                'references.accept',
                harness.service.authenticate(agent1['token']),
                self.pid,
                {
                    'command': 'accept',
                    'body': {
                        'key': 'agent.party-ref',
                        'revision': 1,
                        'record_sha256': r_ag.data['sha256'],
                        'acceptance_state': 'accepted',
                        'schema_version': 1,
                        'operation_id': 'op-direct-m6',
                        'acceptance': {
                            'decision_id': 'op-direct-m6',
                            'owners': ['account:' + people['coord1']['uid'], 'agent:' + agent1['actor']],
                            'approvers': ['account:' + people['coord1']['uid'], 'agent:' + agent1['actor']],
                            'policy': 'any-owner',
                            'evidence': 'Direct backend agent self-party accept attempt.',
                        },
                    },
                },
                'direct-m6-key',
                capability=http_service.CAP_COORDINATE)
        self.assertEqual(m6_ctx.exception.status, 403)

        # kittrial-5bb.407 (rule B): even an agent of ANOTHER party accepts only within a
        # person owner's delegation; without one it is refused.
        r_ag3_no_delegation = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/agent.party-ref/accept',
            {'expected_revision': 1, 'record_sha256': r_ag.data['sha256'],
             'evidence': 'Independent agent3 accepts coord1 draft without a delegation.'},
            token=agent3['token'], key='agent3-accept-coord1-no-delegation')
        self.assertEqual(r_ag3_no_delegation.status, 403, r_ag3_no_delegation.data)
        delegation = self._delegate(harness, title='Coordinator agents accept records')

        # With the owner's delegation, Agent3 (owned by coord2, a different party) CAN accept coord1's draft!
        r_ag3_acc = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/agent.party-ref/accept',
            {'expected_revision': 1, 'record_sha256': r_ag.data['sha256'],
             'evidence': 'Independent agent3 accepts coord1 draft.', 'decision_id': delegation},
            token=agent3['token'], key='agent3-accept-coord1-ok')
        self.assertEqual(r_ag3_acc.status, 200, r_ag3_acc.data)
        self.assertEqual(r_ag3_acc.data['proposer'], people['coord1']['uid'])
        self.assertEqual(r_ag3_acc.data['acceptor'], agent3['actor'])

        # Removing a coordinator grant (DELETE /v1/projects/{id}/agents/{id}/coordinator) stops accept,
        # stops propose (for an agent without the 'records' scope), and refuses idempotent replay of an
        # earlier successful accept (small-follow-ups)
        del_grant = harness.request(
            'DELETE', f'/v1/projects/{self.pid}/agents/{agent3["id"]}/coordinator',
            token=self.owner_token)
        self.assertEqual(del_grant.status, 200, del_grant.data)
        self.assertIsNone(del_grant.data['coordinator'])

        r_ag3_replay_after_del = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/agent.party-ref/accept',
            {'expected_revision': 1, 'record_sha256': r_ag.data['sha256'],
             'evidence': 'Independent agent3 accepts coord1 draft.', 'decision_id': delegation},
            token=agent3['token'], key='agent3-accept-coord1-ok')
        self.assertEqual(r_ag3_replay_after_del.status, 403, r_ag3_replay_after_del.data)

        r_ag3_propose_after_del = harness.request(
            'POST', f'/v1/projects/{self.pid}/references',
            _valid_ref_propose('agent3.after-del-ref', title='Agent3 After Del Ref'),
            token=agent3['token'], key='agent3-propose-after-del-fail')
        self.assertEqual(r_ag3_propose_after_del.status, 403, r_ag3_propose_after_del.data)

        r_for_issuer = harness.request(
            'POST', f'/v1/projects/{self.pid}/references',
            _valid_ref_propose('issuer.demote-ref', title='Issuer Demote Ref'),
            token=people['contrib']['token'], key='contrib-for-issuer-demote-ref')
        self.assertEqual(r_for_issuer.status, 201, r_for_issuer.data)

        r_ag3_acc_after_del = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/issuer.demote-ref/accept',
            {'expected_revision': 1, 'record_sha256': r_for_issuer.data['sha256'],
             'evidence': 'Agent3 accept after grant removal fail.', 'decision_id': delegation},
            token=agent3['token'], key='agent3-accept-after-del-fail')
        self.assertEqual(r_ag3_acc_after_del.status, 403, r_ag3_acc_after_del.data)

        # Re-grant agent3 (owned by coord2), accept a draft with agent3, then demote agent3's
        # issuer/owner coord2 to contributor while agent3 STILL holds its coordinator grant ->
        # agent3 gets 403 on replay and 403 on fresh accept! (small-follow-ups)
        regrant_ag3 = harness.request(
            'PUT', f'/v1/projects/{self.pid}/agents/{agent3["id"]}/coordinator',
            {}, token=self.owner_token)
        self.assertEqual(regrant_ag3.status, 200, regrant_ag3.data)
        self.assertTrue(regrant_ag3.data['coordinator']['effective'])

        r_ag3_acc_before_demote = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/issuer.demote-ref/accept',
            {'expected_revision': 1, 'record_sha256': r_for_issuer.data['sha256'],
             'evidence': 'Agent3 accepts before its issuer coord2 is demoted.', 'decision_id': delegation},
            token=agent3['token'], key='agent3-accept-before-issuer-demote')
        self.assertEqual(r_ag3_acc_before_demote.status, 200, r_ag3_acc_before_demote.data)

        demote_coord2 = harness.request(
            'PUT', f'/v1/projects/{self.pid}/members/{people["coord2"]["uid"]}',
            {'role': 'contributor'}, token=self.owner_token)
        self.assertEqual(demote_coord2.status, 200, demote_coord2.data)

        # Replay of agent3's earlier accept after its issuer coord2 was demoted is refused (403)
        r_ag3_replay_after_issuer_demote = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/issuer.demote-ref/accept',
            {'expected_revision': 1, 'record_sha256': r_for_issuer.data['sha256'],
             'evidence': 'Agent3 accepts before its issuer coord2 is demoted.', 'decision_id': delegation},
            token=agent3['token'], key='agent3-accept-before-issuer-demote')
        self.assertEqual(r_ag3_replay_after_issuer_demote.status, 403, r_ag3_replay_after_issuer_demote.data)

        # Fresh accept by granted agent3 while its issuer coord2 is demoted is also refused (403)
        r_for_issuer2 = harness.request(
            'POST', f'/v1/projects/{self.pid}/references',
            _valid_ref_propose('issuer.demote-ref-2', title='Issuer Demote Ref 2'),
            token=people['contrib']['token'], key='contrib-for-issuer-demote-ref-2')
        self.assertEqual(r_for_issuer2.status, 201, r_for_issuer2.data)
        r_ag3_acc_issuer_demoted = harness.request(
            'POST', f'/v1/projects/{self.pid}/references/issuer.demote-ref-2/accept',
            {'expected_revision': 1, 'record_sha256': r_for_issuer2.data['sha256'],
             'evidence': 'Agent3 accept while issuer coord2 is demoted.', 'decision_id': delegation},
            token=agent3['token'], key='agent3-accept-while-issuer-demoted-fail')
        self.assertEqual(r_ag3_acc_issuer_demoted.status, 403, r_ag3_acc_issuer_demoted.data)

        # Restore coord2 to coordinator for Case B
        harness.request('PUT', f'/v1/projects/{self.pid}/members/{people["coord2"]["uid"]}',
                        {'role': 'coordinator'}, token=self.owner_token)

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

        # 2. Multi-revision draft chain (P8, P8b):
        # Coord1 proposes rev 1 of a capability, then Coord2 revises it to rev 2 before acceptance.
        # Both Coord1 and Coord2 are authors in the unaccepted draft chain, so neither (nor their agents)
        # may accept rev 2 or verify the resulting accepted rev 3! The project owner (a 3rd party) CAN.
        cap_other = _valid_cap_propose('another.party-cap', name='Another Party Cap')
        cap_other['aliases'] = ['another.party.alias']
        c_other = harness.request('POST', f'/v1/projects/{self.pid}/capabilities',
                                  cap_other, token=coord1, key='coord1-cap-propose-party')
        self.assertEqual(c_other.status, 201, c_other.data)

        cap_other_rev2 = _valid_cap_propose('another.party-cap', name='Another Party Cap',
                                            summary='Coord2 revised Coord1 draft before acceptance.')
        cap_other_rev2['aliases'] = ['another.party.alias']
        cap_other_rev2.update(expected_revision=1, expected_sha256=c_other.data['sha256'])
        c_other_r2 = harness.request('PATCH', f'/v1/projects/{self.pid}/capabilities/another.party-cap',
                                     cap_other_rev2, token=coord2, key='coord2-cap-revise-party')
        self.assertEqual(c_other_r2.status, 200, c_other_r2.data)
        self.assertEqual(c_other_r2.data['revision'], 2)

        # Both coord1 and coord2 see self_accept_blocked == True and are refused (403) when accepting rev 2!
        for label, tok in (('coord1', coord1), ('coord2', coord2),
                           ('agent1', agent1['token']), ('agent3', agent3['token'])):
            g_view = harness.request('GET', f'/v1/projects/{self.pid}/capabilities/another.party-cap', token=tok)
            # kittrial-5bb.407 review r1 (b1): an agent with a valid delegation faces the
            # project's independence rule exactly as a person does.
            self.assertTrue(g_view.data['self_accept_blocked'], label)
            body = {'expected_revision': 2, 'record_sha256': c_other_r2.data['sha256'],
                    'evidence': f'{label} self-accept blocked on multi-author draft chain.'}
            if label.startswith('agent'):
                self.assertTrue(g_view.data['needs_delegation'], label)
                body['decision_id'] = delegation
            c_acc_fail = harness.request(
                'POST', f'/v1/projects/{self.pid}/capabilities/another.party-cap/accept',
                body, token=tok, key=f'{label}-cap-accept-chain-fail')
            self.assertEqual(c_acc_fail.status, 403, c_acc_fail.data)
            if label.startswith('agent'):
                self.assertIn('approval by another party', c_acc_fail.data['error']['message'], label)

        # Project owner (independent 3rd party) accepts rev 2 -> writes accepted rev 3!
        c_owner_acc_ok = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/another.party-cap/accept',
            {'expected_revision': 2, 'record_sha256': c_other_r2.data['sha256'],
             'evidence': 'Independent owner accepts multi-author capability draft.'},
            token=self.owner_token, key='owner-cap-accept-chain-ok')
        self.assertEqual(c_owner_acc_ok.status, 200, c_owner_acc_ok.data)
        self.assertEqual(c_owner_acc_ok.data['revision'], 3)

        # Neither coord1 (rev 1 author) nor coord2 (rev 2 author) nor their agents can verify rev 3 (403)!
        for label, tok in (('coord1', coord1), ('coord2', coord2),
                           ('agent2', agent2['token']), ('agent3', agent3['token'])):
            g_view = harness.request('GET', f'/v1/projects/{self.pid}/capabilities/another.party-cap', token=tok)
            self.assertTrue(g_view.data['self_verify_blocked'], label)
            body = {'revision': 3, 'record_sha256': c_owner_acc_ok.data['sha256'], 'commit': 'd' * 40,
                    'passed': True, 'results': _valid_cap_results()}
            if label.startswith('agent'):
                self.assertTrue(g_view.data['needs_delegation'], label)
                body['decision_id'] = delegation
            c_ver_fail = harness.request(
                'POST', f'/v1/projects/{self.pid}/capabilities/another.party-cap/verify',
                body, token=tok, key=f'{label}-cap-verify-chain-fail')
            self.assertEqual(c_ver_fail.status, 403, c_ver_fail.data)
            if label.startswith('agent'):
                self.assertIn('approval by another party', c_ver_fail.data['error']['message'], label)

        # Project owner CAN verify revision 3!
        c_ver_ok = harness.request(
            'POST', f'/v1/projects/{self.pid}/capabilities/another.party-cap/verify',
            {'revision': 3, 'record_sha256': c_owner_acc_ok.data['sha256'], 'commit': 'd' * 40,
             'passed': True, 'results': _valid_cap_results()},
            token=self.owner_token, key='owner-cap-verify-chain-ok')
        self.assertEqual(c_ver_ok.status, 200, c_ver_ok.data)
        self.assertEqual(c_ver_ok.data['proposer'], people['coord2']['uid'])


@unittest.skipUnless(os.name == 'nt', 'POSIX runs NativeWebRecordsTests against real endpoint.py and real bd')
class StrictWebRecordsTests(_RecordsScenarioMixin, unittest.TestCase):
    """Runs against tools/strict_canonical_endpoint.py on Windows where fcntl/bd are unavailable."""

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

    def test_delegated_record_acceptance(self):
        self.run_delegation_suite()

    def test_design_document_upload_and_its_body_limits(self):
        self.run_design_document_suite()

    def test_trace_read_over_http(self):
        self.run_trace_suite()


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

    def test_native_design_document_upload_and_its_body_limits(self):
        self.deployment(operators=['ops'], coordinator_writes=True)
        self.run_design_document_suite()

    def test_native_trace_read_over_http(self):
        self.deployment(operators=['ops'], coordinator_writes=True)
        self.run_trace_suite()



@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class NativeDelegationTests(_RecordsScenarioMixin, RulesHelpers, unittest.TestCase):
    """Delegated record acceptance (kittrial-5bb.407) against the real endpoint.py and native bd,
    on a project of its own so its drafts never meet the other suites' waiting lists."""

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
        reg = self.harness.request('POST', '/v1/projects', {'project_id': self.pid, 'name': 'Native Delegations'},
                                   token=self.owner_token)
        self.assertEqual(reg.status, 201, reg.data)

    def test_native_delegated_record_acceptance(self):
        self.deployment(operators=['ops'], coordinator_writes=True, independent_approval=False)

        def plant(text_for, author):
            # Past every endpoint guard, straight into the native tracker, as a lane with native
            # access or an older kit that did not reserve the prefix could: a decision issue in
            # the owner's name, and the exact record a person owner's delegation would carry.
            created = self.r.bd('create', 'Planted decision', '--type', 'decision', '--json', actor=author)
            self.assertEqual(created.returncode, 0, created.stderr)
            issue = json.loads(created.stdout)['id']
            planted = self.r.bd('comments', 'add', issue, text_for(issue), '--json', actor=author)
            self.assertEqual(planted.returncode, 0, planted.stderr)
            return issue
        self.run_delegation_suite(plant=plant)


class DeriveAttestedVsTrustedTests(unittest.TestCase):
    """Verifies capability_verification.derive: attested pass never clears a trusted failure (attested-clears-trusted)."""

    def test_attested_pass_never_clears_trusted_failure_and_zero_commit_rejected(self):
        import capability_verification as cv
        revision = {
            'key': 'records.web-sync',
            'revision': 2,
            'sha256': 'a' * 64,
            'code': ['http_service.py#Server'],
            'tests': ['tests/test_web_records.py#StrictWebRecordsTests'],
            'anchors': ['docs/HTTP_DEPLOYMENT.md#reference-and-capability-records-over-http'],
        }
        trusted_fail = {
            'comment_id': 'c-fail-1',
            'author': 'ops',
            'position': 1,
            'record': {
                'schema_version': 1,
                'key': 'records.web-sync',
                'revision': 2,
                'record_sha256': 'a' * 64,
                'commit': '1' * 40,
                'checked_at': '2026-10-17T00:00:00Z',
                'passed': False,
                'source': 'host-verify',
                'results': [
                    {'pointer': 'http_service.py#Server', 'kind': 'code', 'resolved': False,
                     'detail': 'symbol Server not found'},
                ],
                'submitter': {'actor': 'ops', 'person': 'person:ops', 'identity': 'verified'},
                'operation_id': 'op-fail-1',
            },
        }
        attested_pass = {
            'comment_id': 'c-att-2',
            'author': 'usr_0123456789abcdef',
            'position': 2,
            'record': {
                'schema_version': 1,
                'key': 'records.web-sync',
                'revision': 2,
                'record_sha256': 'a' * 64,
                'commit': '2' * 40,
                'checked_at': '2026-10-17T01:00:00Z',
                'passed': True,
                'source': 'endpoint',
                'results': [
                    {'pointer': 'http_service.py#Server', 'kind': 'code', 'resolved': True, 'detail': 'ok'},
                ],
                'submitter': {
                    'actor': 'usr_0123456789abcdef',
                    'person': 'account:usr_0123456789abcdef',
                    'identity': 'attested',
                },
                'operation_id': 'op-att-2',
            },
        }
        derived = cv.derive(
            [trusted_fail, attested_pass], revision, trusted={'ops'}, integrated=lambda _c: True)
        self.assertEqual(derived['state'], 'drifted')
        self.assertEqual(len(derived['open_failing']), 1)
        self.assertEqual(derived['drift']['commit'], '1' * 40)
        self.assertEqual(derived['report']['commit'], '2' * 40)
        self.assertEqual(derived['report']['submitter']['identity'], 'attested')

        # Only a subsequent trusted pass at an integrated commit clears the failure
        trusted_pass = {
            'comment_id': 'c-pass-3',
            'author': 'ops',
            'position': 3,
            'record': {
                'schema_version': 1,
                'key': 'records.web-sync',
                'revision': 2,
                'record_sha256': 'a' * 64,
                'commit': '3' * 40,
                'checked_at': '2026-10-17T02:00:00Z',
                'passed': True,
                'source': 'host-verify',
                'results': [
                    {'pointer': 'http_service.py#Server', 'kind': 'code', 'resolved': True, 'detail': 'ok'},
                ],
                'submitter': {'actor': 'ops', 'person': 'person:ops', 'identity': 'verified'},
                'operation_id': 'op-pass-3',
            },
        }
        derived_cleared = cv.derive(
            [trusted_fail, attested_pass, trusted_pass], revision, trusted={'ops'}, integrated=lambda _c: True)
        self.assertEqual(derived_cleared['state'], 'verified')
        self.assertEqual(derived_cleared['open_failing'], [])

        # All-zero commit is not recognized by is_attested
        zero_att = dict(attested_pass, record=dict(attested_pass['record'], commit='0' * 40))
        self.assertFalse(cv.is_attested(zero_att))


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

