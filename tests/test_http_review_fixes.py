"""HTTP-boundary regression tests for the P1 findings on kittrial-5bb.19.

Round 1 (rev2, commit 6fb527a) covered one requested change per class:
``CanonicalEndpointCase``, ``CredentialAuthorityCase``, ``IdempotencyTargetCase``,
``FinalOwnerCase``, ``RevocationRaceCase``/``StoreDurabilityCase`` and
``DeploymentContractCase``.

Round 2 (rev3) adds a class per finding, each with the positive flow and the negative
case:

1. ``CanonicalProtocolCase``      -> real canonical argv/field sets and history paging
2. ``CanonicalLostResultCase``    -> one effect across commit-before-response/restart
3. ``CanonicalRevocationCase``    -> revocation committed first blocks the effect
4. ``LiveMembershipCase``         -> issuer demotion/removal narrows its credentials
5. ``IdempotencyAtomicityCase``   -> atomic reservation and a stable creation namespace
6. ``AttachmentLossCase``         -> evidence is never discarded behind a 201

Round 3 (rev4) adds a class per round-4 request, each with the negative and positive
case:

7. ``RequestAuthorityCase``       -> authority store/lock are launch configuration
8. ``OperationIdentityCase``      -> an operation identity is bound to its principal
9. ``ExceptionUncertaintyCase``   -> an exception after a possible write keeps the
                                     reservation and reports 124
10. ``JournalBoundsCase``         -> capacity fails closed, envelopes are bounded,
                                     only an explicit prune removes an identity
11. ``RealEndpointAuthorityCase`` -> the same boundary through the real ``endpoint.py``
                                     SSH/launch entry point (POSIX only)

Round 4 (rev5) adds a class per round-5 request, including the negative cases:

12. ``PreEffectValidationCase``      -> a refusal raised before the effect's first
                                       native write keeps rc=2 and releases the
                                       identity; one after a write stays 124
13. ``JournalRetentionCase``         -> expired receipts are reclaimed instead of
                                       locking a busy project out; an in-window retry
                                       replays and an expired retry is refused
14. ``SshPrincipalClaimCase``        -> an SSH request cannot assert another
                                       principal for the operation identity
15. ``PreEffectValidationHttpCase``  -> the canonical refusal is a clean HTTP client
                                       error again, not UncertainOutcome (503)
16. ``JournalRetentionHttpCase``     -> a full journal of expired receipts does not
                                       lock out authenticated HTTP writes
17. ``RealEndpointPreEffectCase``    -> the same release through real ``endpoint.py``
                                       (POSIX only)
18. ``JournalOperatorCommandCase``   -> ``admin.py journal`` inspect/reclaim/prune
                                       (POSIX only)

Round 5 (rev6) reworks the journal into one bounded, tombstoned policy and bounds the
HTTP result map, with a class per round-5 request:

19. ``JournalRetentionCase``         -> committed receipts are short-lived; reclaim and
                                       capacity pressure compact to tombstones that
                                       refuse a retry (never re-run); a jumped-forward
                                       clock cannot reclaim a live identity
20. ``JournalBoundsCase``            -> sustained load past the old 2000/8 MiB limits is
                                       never refused; only live reservations fail closed;
                                       tombstones are bounded
21. ``ResultsBoundCase``             -> ``canonical.results`` is bounded by count/bytes
                                       and an evicted local result still never duplicates
"""
import argparse
import http.client
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import http_authority
import http_service
from http_auth import HttpError, Service, Store
from http_authority import (AuthorityConfig, JOURNAL_COMMITTED_RETENTION_SECONDS,
                            JOURNAL_MAX_SKEW_SECONDS, JOURNAL_RETENTION_SECONDS,
                            JOURNAL_TOMBSTONE_LIMIT, LEGACY_JOURNAL_FILENAME,
                            MAX_ENVELOPE_BYTES, MAX_JOURNAL_BYTES,
                            JournalFull, NativeRunner, OperationJournal, PreEffectFailure,
                            is_mutating_invocation, journal_path, principal_key, run_guarded)
from http_service import (EndpointBackend, InProcessBackend, RESULTS_LIMIT, RESULTS_MAX_BYTES,
                          UncertainOutcome, build_backend, create_server, remember_result,
                          MAX_BODY_BYTES)

TMP_ROOT = Path(os.environ.get('ORCHESTRA_TEST_TMP', str(ROOT / '.runtime' / 'test-tmp')))
ADMIN = 'root-admin'
ADMIN_PASSWORD = 'correct-horse-battery-staple'
COMMIT = 'a' * 40
BASE = 'b' * 40
BUNDLE = 'c' * 64
STUB = Path(__file__).resolve().parent / 'canonical_endpoint_stub.py'


def unique_dir(prefix):
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    while True:
        candidate = TMP_ROOT / (prefix + secrets.token_hex(6))
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue


class Response:
    def __init__(self, status, header_list, body):
        self.status = status
        self.header_list = header_list
        self.headers = {}
        for key, value in header_list:
            self.headers.setdefault(key.lower(), value)
        self.body = body
        try:
            self.data = json.loads(body) if body else None
        except ValueError:
            self.data = None

    def set_cookies(self):
        return [value for key, value in self.header_list if key.lower() == 'set-cookie']


class Harness(unittest.TestCase):
    trusted_proxies = ()

    def setUp(self):
        self.tmp = unique_dir('rev-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = Store(self.tmp / 'state.json')
        self.admin_user = Service.bootstrap_superuser(self.store, ADMIN, ADMIN_PASSWORD)
        self.service = Service(self.store)
        self.backend = self.make_backend()
        self._start_server(self.service, self.backend)

    def make_backend(self):
        return InProcessBackend(self.service)

    def _start_server(self, service, backend):
        self.httpd = create_server(service, backend, host='127.0.0.1', port=0,
                                   trusted_proxies=self.trusted_proxies,
                                   max_body=MAX_BODY_BYTES)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _stop_server(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=5)
        except Exception:  # noqa: BLE001 - cleanup must stay idempotent
            pass

    def restart(self, service, backend):
        self._stop_server()
        self.service, self.backend, self.store = service, backend, service.store
        self._start_server(self.service, self.backend)

    def request(self, method, path, body=None, token=None, cookie=None, csrf=None,
                key=None, headers=None, content_type='application/json'):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=20)
        request_headers = {}
        if body is not None:
            request_headers['Content-Type'] = content_type
        if token:
            request_headers['Authorization'] = 'Bearer ' + token
        if cookie:
            request_headers['Cookie'] = cookie
        if csrf:
            request_headers['X-CSRF-Token'] = csrf
        if key:
            request_headers['Idempotency-Key'] = key
        request_headers.update(headers or {})
        if body is None:
            payload = None
        elif isinstance(body, (bytes, str)):
            payload = body
        else:
            payload = json.dumps(body)
        try:
            client.request(method, path, body=payload, headers=request_headers)
            response = client.getresponse()
            status = response.status
            header_list = response.getheaders()
            data = response.read()
        finally:
            client.close()
        return Response(status, header_list, data)

    # -- fixtures --------------------------------------------------------------
    def login(self, username, password):
        response = self.request('POST', '/v1/sessions',
                                {'username': username, 'password': password})
        self.assertEqual(201, response.status, response.data)
        return (response.data['session']['token'], response.data['session']['csrf_token'],
                response.data['user'])

    def admin_token(self):
        return self.login(ADMIN, ADMIN_PASSWORD)[0]

    def create_account(self, admin, username, password):
        response = self.request('POST', '/v1/accounts', {'username': username}, token=admin)
        self.assertEqual(201, response.status, response.data)
        user_id = response.data['id']
        response = self.request('POST', '/v1/accounts/%s/password' % user_id,
                                {'new_password': password}, token=admin)
        self.assertEqual(200, response.status, response.data)
        return user_id

    def create_project(self, token, name, project_id=None):
        body = {'name': name}
        if project_id:
            body['project_id'] = project_id
        response = self.request('POST', '/v1/projects', body, token=token)
        self.assertEqual(201, response.status, response.data)
        return response.data['id']

    def create_task(self, token, project, title, key=None):
        return self.request('POST', '/v1/projects/%s/tasks' % project, {'title': title},
                            token=token, key=key)

    def issue_credential(self, token, project, **body):
        response = self.request('POST', '/v1/projects/%s/worker-credentials' % project,
                                body or {'label': 'worker'}, token=token)
        self.assertEqual(201, response.status, response.data)
        return response.data['credential']


# ---------------------------------------------------------------- 2. credential-authority
class CredentialAuthorityCase(Harness):
    def test_read_only_credential_never_inherits_issuer_authority(self):
        admin = self.admin_token()
        alex_id = self.create_account(admin, 'alex', 'alex-password-1')
        blair_id = self.create_account(admin, 'blair', 'blair-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        blair = self.login('blair', 'blair-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        beta = self.create_project(blair, 'Beta')

        # Issued by a superuser, explicitly read-only. It must not gain global
        # administration from the issuer, and must not write anywhere.
        issued = self.issue_credential(admin, project, label='ro', scopes=['read'])
        secret, credential_id = issued['secret'], issued['id']

        denied = [
            ('POST', '/v1/accounts', {'username': 'mallory'}),
            ('GET', '/v1/accounts', None),
            ('POST', '/v1/accounts/%s/password' % alex_id, {'new_password': 'x' * 12}),
            ('POST', '/v1/accounts/%s/reset' % alex_id, None),
            ('POST', '/v1/accounts/%s/disable' % alex_id, None),
            ('POST', '/v1/projects', {'name': 'Gamma'}),
            ('POST', '/v1/projects/%s/archive' % project, None),
            ('PUT', '/v1/projects/%s/members/%s' % (project, blair_id), {'role': 'viewer'}),
            ('DELETE', '/v1/projects/%s/members/%s' % (project, blair_id), None),
            ('POST', '/v1/projects/%s/worker-credentials' % project, {'label': 'x'}),
            ('POST', '/v1/projects/%s/worker-credentials/%s/revoke'
             % (project, credential_id), None),
            ('GET', '/v1/projects/%s/audit' % project, None),
            ('POST', '/v1/projects/%s/tasks' % project, {'title': 'sneaky'}),
            ('POST', '/v1/projects/%s/feedback' % project, {'text': 'sneaky'}),
        ]
        for method, path, body in denied:
            response = self.request(method, path, body, token=secret)
            self.assertEqual(403, response.status, (method, path, response.data))

        # A read-only credential can read its own project, but not learn another one.
        self.assertEqual(200, self.request('GET', '/v1/projects/%s/tasks' % project,
                                           token=secret).status)
        self.assertEqual(404, self.request('GET', '/v1/projects/%s/tasks' % beta,
                                           token=secret).status)
        self.assertEqual(0, self.request('GET', '/v1/projects/%s/tasks' % project,
                                         token=secret).data['total'])

        # Revoking the credential takes immediate effect.
        self.assertEqual(204, self.request('POST', '/v1/projects/%s/worker-credentials/%s/revoke'
                                           % (project, credential_id), token=admin).status)
        self.assertEqual(401, self.request('GET', '/v1/projects/%s/tasks' % project,
                                           token=secret).status)

    def test_project_scoped_credential_cannot_cross_projects(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.create_account(admin, 'blair', 'blair-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        blair = self.login('blair', 'blair-password-1')[0]
        alpha = self.create_project(alex, 'Alpha')
        beta = self.create_project(blair, 'Beta')
        issued = self.issue_credential(alex, alpha, label='writer',
                                       scopes=['tasks', 'checkpoints', 'reviews', 'feedback'])
        secret = issued['secret']
        # A credential scoped to another project must not even learn it exists.
        self.assertEqual(404, self.request('POST', '/v1/projects/%s/tasks' % beta,
                                           {'title': 'x'}, token=secret).status)
        self.assertEqual(404, self.request('GET', '/v1/projects/%s/tasks' % beta,
                                           token=secret).status)
        self.assertEqual(201, self.request('POST', '/v1/projects/%s/tasks' % alpha,
                                           {'title': 'ok'}, token=secret).status)


# ----------------------------------------------------------------- 3. idempotency-target
class IdempotencyTargetCase(Harness):
    def test_same_key_on_two_account_targets_does_not_collide(self):
        admin = self.admin_token()
        alex_id = self.create_account(admin, 'alex', 'alex-password-1')
        blair_id = self.create_account(admin, 'blair', 'blair-password-1')
        first = self.request('POST', '/v1/accounts/%s/disable' % alex_id, None,
                             token=admin, key='disable-key-0001')
        self.assertEqual(200, first.status, first.data)
        second = self.request('POST', '/v1/accounts/%s/disable' % blair_id, None,
                              token=admin, key='disable-key-0001')
        self.assertEqual(200, second.status, second.data)
        self.assertTrue(self.store.state['users'][blair_id]['disabled'])
        # An exact retry of the first target replays without error.
        retry = self.request('POST', '/v1/accounts/%s/disable' % alex_id, None,
                             token=admin, key='disable-key-0001')
        self.assertEqual(200, retry.status, retry.data)

    def test_same_key_on_two_member_targets_does_not_collide(self):
        admin = self.admin_token()
        alex_id = self.create_account(admin, 'alex', 'alex-password-1')
        blair_id = self.create_account(admin, 'blair', 'blair-password-1')
        project = self.create_project(admin, 'Alpha')
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s'
                                           % (project, alex_id), {'role': 'viewer'},
                                           token=admin, key='member-key-0001').status)
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s'
                                           % (project, blair_id), {'role': 'contributor'},
                                           token=admin, key='member-key-0001').status)
        members = self.store.state['memberships'][project]
        self.assertEqual('viewer', members[alex_id])
        self.assertEqual('contributor', members[blair_id])

    def test_conflicting_body_on_same_target_is_rejected(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        self.assertEqual(201, self.create_task(alex, project, 'one',
                                               key='task-key-0009').status)
        conflict = self.create_task(alex, project, 'two', key='task-key-0009')
        self.assertEqual(409, conflict.status)
        self.assertEqual('conflict', conflict.data['error']['code'])


# ---------------------------------------------------------------------- 4. final-owner
class FinalOwnerCase(Harness):
    def test_final_owner_invariant_includes_superuser(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.create_account(admin, 'blair', 'blair-password-1')
        alex, _, alex_user = self.login('alex', 'alex-password-1')
        _, _, blair_user = self.login('blair', 'blair-password-1')
        project = self.create_project(alex, 'Alpha')

        demote = self.request('PUT', '/v1/projects/%s/members/%s'
                              % (project, alex_user['id']), {'role': 'viewer'}, token=admin)
        self.assertEqual(409, demote.status, demote.data)
        disable = self.request('POST', '/v1/accounts/%s/disable' % alex_user['id'], None,
                               token=admin)
        self.assertEqual(409, disable.status, disable.data)
        remove = self.request('DELETE', '/v1/projects/%s/members/%s'
                              % (project, alex_user['id']), token=admin)
        self.assertEqual(409, remove.status, remove.data)
        # The project still has exactly one owner.
        self.assertEqual(1, sum(1 for role in self.store.state['memberships'][project].values()
                                if role == 'owner'))

        # Accepted recovery: install a second owner, then demote the first.
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s'
                                           % (project, blair_user['id']), {'role': 'owner'},
                                           token=admin).status)
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s'
                                           % (project, alex_user['id']), {'role': 'viewer'},
                                           token=admin).status)
        self.assertEqual(1, sum(1 for role in self.store.state['memberships'][project].values()
                                if role == 'owner'))


# ------------------------------------------------------------- 5. revocation-transaction
class PausingBackend(InProcessBackend):
    """Pause a canonical mutation at the call boundary, before any lock is taken."""

    def __init__(self, service):
        super().__init__(service)
        self.entered = threading.Event()
        self.proceed = threading.Event()
        self.pause_route = None

    def invoke(self, route, *args, **kwargs):
        if route == self.pause_route:
            self.entered.set()
            if not self.proceed.wait(timeout=20):
                raise RuntimeError('probe was never resumed')
        return super().invoke(route, *args, **kwargs)


class RevocationRaceCase(Harness):
    def make_backend(self):
        self.pausing = PausingBackend(self.service)
        return self.pausing

    def test_revocation_wins_over_an_in_flight_create(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        issued = self.issue_credential(alex, project, label='worker',
                                       scopes=['tasks', 'checkpoints', 'reviews', 'feedback'])
        secret, credential_id = issued['secret'], issued['id']

        self.pausing.pause_route = 'tasks.create'
        self.pausing.entered.clear()
        self.pausing.proceed.clear()
        outcome = {}

        def create():
            outcome['response'] = self.create_task(secret, project, 'raced task',
                                                   key='race-key-0001')

        worker = threading.Thread(target=create)
        worker.start()
        self.assertTrue(self.pausing.entered.wait(timeout=15), 'create never reached the seam')
        # Revocation completes on another request while the create is paused.
        revoked = self.request('POST', '/v1/projects/%s/worker-credentials/%s/revoke'
                               % (project, credential_id), token=alex)
        self.assertEqual(204, revoked.status, revoked.body)
        self.pausing.proceed.set()
        worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        # The create must not succeed once revocation has committed.
        self.assertEqual(401, outcome['response'].status, outcome['response'].data)
        self.assertEqual(0, self.request('GET', '/v1/projects/%s/tasks' % project,
                                         token=alex).data['total'])

    def test_uncertain_outcome_is_durable_and_reconcilable(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        self.backend.fail_next('tasks.create')
        first = self.create_task(alex, project, 'lost response', key='task-key-0010')
        self.assertEqual(503, first.status)
        retry = self.create_task(alex, project, 'lost response', key='task-key-0010')
        self.assertEqual(201, retry.status, retry.data)
        self.assertEqual(1, self.request('GET', '/v1/projects/%s/tasks' % project,
                                         token=alex).data['total'])


class StoreDurabilityCase(unittest.TestCase):
    def test_concurrent_saves_are_atomic_and_leave_no_temp(self):
        tmp = unique_dir('st-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = Store(tmp / 'state.json')
        Service.bootstrap_superuser(store, ADMIN, ADMIN_PASSWORD)
        errors = []

        def writer(number):
            try:
                for index in range(15):
                    with store.lock:
                        store.state['audit'].append({'writer': number, 'entry': index})
                        store.save()
            except Exception as error:  # noqa: BLE001 - surfaced by the assertion
                errors.append(error)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(errors, errors)
        reloaded = json.loads((tmp / 'state.json').read_text(encoding='utf-8'))
        self.assertGreaterEqual(len(reloaded['audit']), 1)
        self.assertEqual([], list(tmp.glob('*.tmp')))


# ------------------------------------------------------- 6. browser-deployment-contract
class DeploymentContractCase(Harness):
    trusted_proxies = ('127.0.0.1',)

    def test_trusted_proxy_https_sets_secure_cookie(self):
        self.create_account(self.admin_token(), 'alex', 'alex-password-1')
        response = self.request('POST', '/v1/sessions',
                                {'username': 'alex', 'password': 'alex-password-1'},
                                headers={'X-Forwarded-Proto': 'https'})
        self.assertEqual(201, response.status)
        cookies = response.set_cookies()
        self.assertTrue(cookies)
        self.assertIn('Secure', cookies[0])

    def test_owner_only_review_approval(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        self.create_account(admin, 'blair', 'blair-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        blair, _, blair_user = self.login('blair', 'blair-password-1')
        project = self.create_project(alex, 'Alpha')
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s'
                                           % (project, blair_user['id']),
                                           {'role': 'contributor'}, token=alex).status)
        task = self.create_task(alex, project, 'delivery').data
        contribution = self.request('POST', '/v1/projects/%s/tasks/%s/reviews'
                                    % (project, task['id']),
                                    {'operation': 'contribute', 'commit': COMMIT,
                                     'base_commit': BASE, 'bundle_sha256': BUNDLE,
                                     'summary': 'delivered'}, token=blair)
        self.assertEqual(201, contribution.status, contribution.data)
        denied = self.request('POST', '/v1/projects/%s/tasks/%s/reviews'
                              % (project, task['id']),
                              {'operation': 'approve', 'summary': 'lgtm'}, token=blair)
        self.assertEqual(403, denied.status, denied.data)
        allowed = self.request('POST', '/v1/projects/%s/tasks/%s/reviews'
                               % (project, task['id']),
                               {'operation': 'approve', 'summary': 'lgtm'}, token=alex)
        self.assertEqual(201, allowed.status, allowed.data)

    def test_credential_can_never_approve(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        task = self.create_task(alex, project, 'delivery').data
        issued = self.issue_credential(alex, project, label='worker',
                                       scopes=['tasks', 'checkpoints', 'reviews', 'feedback'])
        secret = issued['secret']
        self.assertEqual(201, self.request('POST', '/v1/projects/%s/tasks/%s/reviews'
                                           % (project, task['id']),
                                           {'operation': 'contribute', 'commit': COMMIT,
                                            'base_commit': BASE, 'bundle_sha256': BUNDLE,
                                            'summary': 'delivered'}, token=secret).status)
        denied = self.request('POST', '/v1/projects/%s/tasks/%s/reviews'
                              % (project, task['id']),
                              {'operation': 'approve', 'summary': 'lgtm'}, token=secret)
        self.assertEqual(403, denied.status, denied.data)

    def test_canonical_ids_with_hyphens_and_dots_route(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        response = self.request('POST', '/v1/projects',
                                {'name': 'Alpha', 'project_id': 'alpha-5bb.19'}, token=alex)
        self.assertEqual(201, response.status, response.data)
        self.assertEqual(200, self.request('GET', '/v1/projects/alpha-5bb.19',
                                           token=alex).status)
        task = self.create_task(alex, 'alpha-5bb.19', 'named task').data
        self.assertEqual(200, self.request('GET', '/v1/projects/alpha-5bb.19/tasks/%s'
                                           % task['id'], token=alex).status)


class UntrustedProxyCase(Harness):
    trusted_proxies = ('10.9.9.9',)

    def test_forwarded_proto_from_an_untrusted_peer_is_ignored(self):
        self.create_account(self.admin_token(), 'alex', 'alex-password-1')
        response = self.request('POST', '/v1/sessions',
                                {'username': 'alex', 'password': 'alex-password-1'},
                                headers={'X-Forwarded-Proto': 'https'})
        self.assertEqual(201, response.status)
        cookies = response.set_cookies()
        self.assertTrue(cookies)
        self.assertNotIn('Secure', cookies[0])


class PlaintextLoopbackCase(Harness):
    trusted_proxies = ()

    def test_plaintext_loopback_cookie_is_not_marked_secure(self):
        self.create_account(self.admin_token(), 'alex', 'alex-password-1')
        response = self.request('POST', '/v1/sessions',
                                {'username': 'alex', 'password': 'alex-password-1'},
                                headers={'X-Forwarded-Proto': 'https'})
        cookies = response.set_cookies()
        self.assertTrue(cookies)
        self.assertNotIn('Secure', cookies[0])


# ------------------------------------------------------------------ 1. canonical-backend
class CanonicalEndpointCase(Harness):
    def make_backend(self):
        self.canonical_root = self.tmp / 'canonical'
        return EndpointBackend(sys.executable, str(STUB), str(self.canonical_root),
                               service=self.service)

    def _setup_project(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        return alex, self.create_project(alex, 'Alpha')

    def test_http_flows_through_the_canonical_process(self):
        alex, project = self._setup_project()
        created = self.create_task(alex, project, 'canonical task', key='canon-key-0001')
        self.assertEqual(201, created.status, created.data)
        task_id = created.data['id']
        self.assertIn('.', task_id)  # canonical ids carry a dot
        listed = self.request('GET', '/v1/projects/%s/tasks' % project, token=alex).data
        self.assertEqual(1, listed['total'])
        self.assertEqual(200, self.request('GET', '/v1/projects/%s/tasks/%s'
                                           % (project, task_id), token=alex).status)
        # The canonical file, not service memory, holds the record.
        canonical = json.loads((self.canonical_root / 'canonical.json').read_text())
        self.assertEqual(1, len(canonical['rows']))

    def test_uncertain_write_reconciles_after_a_restart(self):
        alex, project = self._setup_project()
        self.backend.fail_next('tasks.create')
        first = self.create_task(alex, project, 'canonical task', key='canon-key-0002')
        self.assertEqual(503, first.status)
        # Rebuild the service and backend from disk: the receipt is durable.
        store = Store(self.tmp / 'state.json')
        service = Service(store)
        backend = EndpointBackend(sys.executable, str(STUB), str(self.canonical_root),
                                  service=service)
        self.restart(service, backend)
        retry = self.create_task(alex, project, 'canonical task', key='canon-key-0002')
        self.assertEqual(201, retry.status, retry.data)
        canonical = json.loads((self.canonical_root / 'canonical.json').read_text())
        self.assertEqual(1, len(canonical['rows']))

    def test_feedback_fails_closed_until_the_canonical_stream_exists(self):
        alex, project = self._setup_project()
        response = self.request('POST', '/v1/projects/%s/feedback' % project,
                                {'text': 'note'}, token=alex)
        self.assertEqual(501, response.status, response.data)
        self.assertEqual('not_implemented', response.data['error']['code'])


class BackendSelectionCase(unittest.TestCase):
    def test_build_backend_selects_endpoint_then_inprocess(self):
        tmp = unique_dir('sel-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = Store(tmp / 'state.json')
        Service.bootstrap_superuser(store, ADMIN, ADMIN_PASSWORD)
        service = Service(store)
        args = argparse.Namespace(backend='endpoint', endpoint_python=sys.executable,
                                  endpoint=str(STUB), root=str(tmp / 'root'),
                                  actor_namespace='http', endpoint_timeout=30)
        self.assertIsInstance(build_backend(service, args), EndpointBackend)
        args.backend = 'inprocess'
        self.assertIsInstance(build_backend(service, args), InProcessBackend)


# ==================================================================== round 3
# The six round-3 P1 findings. Each has a reproduction and a regression assertion
# against the real HTTP surface, including the negative cases.

CONTRIBUTION = {'repository': 'https://example.invalid/repo.git', 'commit': COMMIT,
                'base_commit': BASE, 'summary': 'delivered', 'supersedes': None,
                'delivery': {'kind': 'bundle', 'path': 'koopa:/tmp/x.bundle',
                             'sha256': BUNDLE}}


class EndpointCase(Harness):
    """Base for canonical-binding cases: the strict stub applies the real contract."""

    def make_backend(self):
        self.canonical_root = self.tmp / 'canonical'
        return EndpointBackend(sys.executable, str(STUB), str(self.canonical_root),
                               service=self.service)

    def canonical_rows(self):
        path = self.canonical_root / 'canonical.json'
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding='utf-8'))['rows']

    def setup_project(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        return alex, self.create_project(alex, 'Alpha')

    def contribute(self, token, project, task, previous=None, operation_id=None, key=None):
        body = dict(CONTRIBUTION)
        body.update({'operation': 'contribute', 'schema_version': 1,
                     'operation_id': operation_id or 'op-' + secrets.token_hex(6),
                     'previous': previous})
        return self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (project, task),
                            body, token=token, key=key)


class CanonicalProtocolCase(EndpointCase):
    """1. canonical-protocol: argv, exact review fields, and history paging."""

    def test_create_with_description_and_review_and_history_use_the_real_contract(self):
        alex, project = self.setup_project()
        described = self.request('POST', '/v1/projects/%s/tasks' % project,
                                 {'title': 'described', 'description': 'evidence body'},
                                 token=alex)
        self.assertEqual(201, described.status, described.data)
        plain = self.create_task(alex, project, 'plain')
        self.assertEqual(201, plain.status, plain.data)
        task_id = plain.data['id']
        self.assertEqual(200, self.request('POST', '/v1/projects/%s/tasks/%s/claim'
                                           % (project, task_id), {}, token=alex).status)
        review = self.contribute(alex, project, task_id)
        self.assertEqual(201, review.status, review.data)
        self.assertIn('comment_id', review.data)
        # HTTP default page size (50) must not exceed the canonical history limit (20).
        history = self.request('GET', '/v1/projects/%s/tasks/%s/history?limit=50'
                               % (project, task_id), token=alex)
        self.assertEqual(200, history.status, history.data)
        self.assertTrue(history.data['items'])
        self.assertIn('activity_cursor', history.data)

    def test_complete_create_claim_checkpoint_contribute_approve_flow(self):
        alex, project = self.setup_project()
        task_id = self.create_task(alex, project, 'flow task').data['id']
        self.assertEqual(200, self.request('POST', '/v1/projects/%s/tasks/%s/claim'
                                           % (project, task_id), {}, token=alex).status)
        history = self.request('GET', '/v1/projects/%s/tasks/%s/history?limit=5'
                               % (project, task_id), token=alex).data
        checkpoint = self.request(
            'POST', '/v1/projects/%s/tasks/%s/checkpoints' % (project, task_id),
            {'schema_version': 1, 'previous': None,
             'activity_cursor': history['activity_cursor'], 'source_commit': COMMIT,
             'branch': 'contrib/test', 'intent': 'prove the canonical checkpoint',
             'acceptance': 'accepted by the canonical validator',
             'summary': 'checkpoint accepted', 'next_action': 'contribute',
             'open_items': [], 'resolved': []}, token=alex)
        self.assertEqual(201, checkpoint.status, checkpoint.data)
        review = self.contribute(alex, project, task_id)
        self.assertEqual(201, review.status, review.data)
        contribution_id = review.data.get('comment_id') or \
            (review.data.get('contribution') or {}).get('comment_id')
        self.assertTrue(contribution_id, review.data)
        approve = self.request(
            'POST', '/v1/projects/%s/tasks/%s/reviews' % (project, task_id),
            {'operation': 'approve', 'schema_version': 1, 'previous': contribution_id,
             'operation_id': 'op-' + secrets.token_hex(6),
             'contribution': contribution_id, 'summary': 'accepted'}, token=alex)
        self.assertEqual(201, approve.status, approve.data)

    def test_no_raw_file_flag_is_emitted_and_the_body_travels_as_an_attachment(self):
        alex, project = self.setup_project()
        principal = self.service.authenticate(alex)
        action, _, args, attachments = self.backend._command(
            'tasks.create', principal, project, {'title': 't', 'description': 'body'})
        self.assertEqual('bd', action)
        self.assertIn('@attachment:0', args)
        self.assertNotIn('--body-file', args)
        self.assertEqual('--body-file', attachments['0']['flag'])
        self.assertEqual('body', attachments['0']['text'])

    def test_review_body_is_exactly_the_canonical_field_set(self):
        from review_workflow import validate
        alex, project = self.setup_project()
        principal = self.service.authenticate(alex)
        for operation, extra in (('contribute', CONTRIBUTION),
                                 ('approve', {'contribution': 'c' * 20, 'summary': 'ok'})):
            payload = {'task_id': 'task-1', 'operation': operation, 'schema_version': 1,
                       'previous': None, 'operation_id': 'op-' + operation}
            payload.update(extra)
            _, _, args, attachments = self.backend._command(
                'reviews.add', principal, project, payload, 'f' * 64)
            body = json.loads(attachments['0']['text'])
            validate(body, 'task-1')  # raises if the field set or values are wrong
            self.assertEqual('task-1', body['task'])


class CanonicalLostResultCase(EndpointCase):
    """2. canonical-lost-result: response loss before the local receipt."""

    def test_lost_response_does_not_duplicate_the_canonical_task(self):
        alex, project = self.setup_project()
        backend = self.backend
        original = backend._endpoint

        def lossy(action, project_id, actor, args, attachments=None, **kwargs):
            original(action, project_id, actor, args, attachments, **kwargs)
            raise UncertainOutcome('response lost after the canonical commit')

        backend._endpoint = lossy
        first = self.create_task(alex, project, 'lost response', key='lost-key-0100')
        self.assertEqual(503, first.status, first.data)
        backend._endpoint = original
        # Rebuild Service and EndpointBackend from disk, as after a process restart.
        store = Store(self.tmp / 'state.json')
        service = Service(store)
        self.restart(service, EndpointBackend(sys.executable, str(STUB),
                                              str(self.canonical_root), service=service))
        retry = self.create_task(alex, project, 'lost response', key='lost-key-0100')
        self.assertEqual(201, retry.status, retry.data)
        rows = self.canonical_rows()
        self.assertEqual(1, len(rows))
        # The replayed result is the committed record, not a second creation.
        self.assertEqual(rows[0]['id'], retry.data['id'])


class CanonicalRevocationCase(EndpointCase):
    """3. canonical-revocation: live authority is re-validated inside the effect."""

    def test_revocation_committed_before_the_write_blocks_the_canonical_effect(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        issued = self.issue_credential(alex, project, label='worker',
                                       scopes=['tasks', 'checkpoints', 'reviews', 'feedback'])
        secret, credential_id = issued['secret'], issued['id']
        backend = self.backend
        original = backend._run
        state = {'pause': True}
        entered, proceed = threading.Event(), threading.Event()

        def paused(*args, **kwargs):
            if state['pause']:
                entered.set()
                self.assertTrue(proceed.wait(timeout=20), 'probe was never resumed')
            return original(*args, **kwargs)

        backend._run = paused
        outcome = {}

        def create():
            outcome['response'] = self.create_task(secret, project, 'raced task',
                                                   key='race-key-0100')

        worker = threading.Thread(target=create)
        worker.start()
        self.assertTrue(entered.wait(timeout=20), 'create never reached the canonical seam')
        revoked = self.request('POST', '/v1/projects/%s/worker-credentials/%s/revoke'
                               % (project, credential_id), token=alex)
        self.assertEqual(204, revoked.status, revoked.data)
        proceed.set()
        worker.join(timeout=30)
        state['pause'] = False
        self.assertIn(outcome['response'].status, (401, 403), outcome['response'].data)
        self.assertEqual([], self.canonical_rows())

    def test_scope_denial_is_a_clean_403_before_any_canonical_write(self):
        alex, project = self.setup_project()
        issued = self.issue_credential(alex, project, label='cp', scopes=['checkpoints'])
        denied = self.create_task(issued['secret'], project, 'sneaky')
        self.assertEqual(403, denied.status, denied.data)
        self.assertEqual([], self.canonical_rows())


class LiveMembershipCase(Harness):
    """4. live-membership: a credential never outlives its issuer's role."""

    def _project_with_issuer(self):
        admin = self.admin_token()
        alex_id = self.create_account(admin, 'alex', 'alex-password-1')
        blair_id = self.create_account(admin, 'blair', 'blair-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        issued = self.issue_credential(alex, project, label='worker',
                                       scopes=['tasks', 'checkpoints', 'reviews', 'feedback'])
        return admin, alex_id, blair_id, alex, project, issued

    def test_demotion_and_removal_stop_an_issued_credential_immediately(self):
        admin, alex_id, blair_id, alex, project, issued = self._project_with_issuer()
        secret = issued['secret']
        self.assertEqual(201, self.create_task(secret, project, 'while owner').status)
        # A second owner must exist before the first can be demoted or removed.
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s'
                                           % (project, blair_id), {'role': 'owner'},
                                           token=admin).status)
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s'
                                           % (project, alex_id), {'role': 'viewer'},
                                           token=admin).status)
        denied = self.create_task(secret, project, 'after demotion')
        self.assertEqual(403, denied.status, denied.data)
        # A viewer keeps read, so the credential still reads but never writes.
        self.assertEqual(200, self.request('GET', '/v1/projects/%s/tasks' % project,
                                           token=secret).status)
        self.assertEqual(200, self.request('DELETE', '/v1/projects/%s/members/%s'
                                           % (project, alex_id), token=admin).status)
        self.assertIn(self.request('POST', '/v1/projects/%s/tasks' % project,
                                   {'title': 'after removal'}, token=secret).status, (403, 404))
        self.assertIn(self.request('GET', '/v1/projects/%s/tasks' % project,
                                   token=secret).status, (403, 404))

    def test_credential_of_a_current_member_is_unaffected(self):
        admin, alex_id, blair_id, alex, project, issued = self._project_with_issuer()
        self.assertEqual(201, self.create_task(issued['secret'], project, 'still allowed').status)


class IdempotencyAtomicityCase(Harness):
    """5. idempotency-atomicity: reservation is atomic and creation keys are stable."""

    def test_concurrent_identical_issue_creates_exactly_one_credential(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        service = self.service
        original = service.issue_credential
        barrier = threading.Barrier(2)

        def gated(*args, **kwargs):
            try:
                barrier.wait(timeout=3)
            except threading.BrokenBarrierError:
                pass
            return original(*args, **kwargs)

        service.issue_credential = gated
        results = {}
        start = threading.Barrier(3)

        def issue(number):
            start.wait(timeout=5)
            results[number] = self.request(
                'POST', '/v1/projects/%s/worker-credentials' % project, {'label': 'same'},
                token=alex, key='concurrent-key-0100')

        threads = [threading.Thread(target=issue, args=(n,)) for n in (0, 1)]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=30)
        service.issue_credential = original
        statuses = sorted(results[n].status for n in results)
        self.assertEqual(1, statuses.count(201), statuses)
        self.assertEqual(1, len(self.store.state['credentials']))

    def test_project_create_same_key_changed_body_conflicts(self):
        admin = self.admin_token()
        first = self.request('POST', '/v1/projects',
                             {'name': 'Created one', 'project_id': 'proj-one'},
                             token=admin, key='project-key-0100')
        self.assertEqual(201, first.status, first.data)
        changed = self.request('POST', '/v1/projects',
                               {'name': 'Created two', 'project_id': 'proj-two'},
                               token=admin, key='project-key-0100')
        self.assertEqual(409, changed.status, changed.data)
        self.assertEqual(1, len(self.store.state['projects']))


class AttachmentLossCase(Harness):
    """6. attachment-loss: never acknowledge a task whose evidence was discarded."""

    ATTACHMENT = {'name': 'note.txt', 'media_type': 'text/plain',
                  'content_base64': 'YXR0YWNobWVudC1wYXlsb2Fk'}

    def test_valid_attachment_is_rejected_and_not_stored(self):
        admin = self.admin_token()
        self.create_account(admin, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        response = self.request('POST', '/v1/projects/%s/tasks' % project,
                                {'title': 'with evidence',
                                 'attachments': [self.ATTACHMENT]}, token=alex)
        self.assertEqual(501, response.status, response.data)
        self.assertEqual('not_implemented', response.data['error']['code'])
        self.assertEqual(0, self.request('GET', '/v1/projects/%s/tasks' % project,
                                         token=alex).data['total'])


class EndpointAttachmentLossCase(EndpointCase):
    def test_valid_attachment_never_reaches_the_canonical_write(self):
        alex, project = self.setup_project()
        response = self.request('POST', '/v1/projects/%s/tasks' % project,
                                {'title': 'with evidence',
                                 'attachments': [AttachmentLossCase.ATTACHMENT]}, token=alex)
        self.assertEqual(501, response.status, response.data)
        self.assertEqual([], self.canonical_rows())


# ==================================================================== round 4
# The four round-4 review requests against the shared authority/identity boundary.
# Each class has the negative case (what the pre-fix revision did) and the positive
# case (the fixed boundary), so the tests fail on the revision they criticise.

def authority_state(project='p', now=None):
    """A minimal live authority document that grants usr_a and usr_b owner on project."""
    moment = time.time() if now is None else now
    return {
        'schema_version': 1,
        'users': {'usr_a': {'id': 'usr_a', 'disabled': False},
                  'usr_b': {'id': 'usr_b', 'disabled': False}},
        'sessions': {
            'sess_a': {'user_id': 'usr_a', 'revoked': False,
                       'absolute_expires': moment + 3600, 'idle_expires': moment + 3600},
            'sess_b': {'user_id': 'usr_b', 'revoked': False,
                       'absolute_expires': moment + 3600, 'idle_expires': moment + 3600}},
        'credentials': {},
        'projects': {project: {'id': project, 'archived': False}},
        'memberships': {project: {'usr_a': 'owner', 'usr_b': 'owner'}},
    }


def authority_descriptor(user, session, project='p', capability='tasks.write'):
    return {'via': 'session', 'user_id': user, 'session_hash': session, 'project': project,
            'capability': capability, 'now': time.time() + 5}


def ledger(count=None):
    """An effect that records how many times it actually ran."""
    records = []

    def effect(label='ran'):
        records.append(label)
        return {'returncode': 0, 'stdout': label, 'stderr': ''}
    return records, effect


class RequestAuthorityCase(unittest.TestCase):
    """1. endpoint-trusts-request-authority: paths are server configuration."""

    def setUp(self):
        self.tmp = unique_dir('authority4-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.server_store = self.tmp / 'server-state.json'
        self.server_store.write_text(json.dumps(authority_state()), encoding='utf-8')
        self.config = AuthorityConfig(str(self.server_store))

    def test_ssh_shaped_authority_cannot_choose_store_or_lock(self):
        # Negative case: at the pre-fix boundary this request made file_lock create the
        # caller's lock path and read the caller's store (a readability oracle).
        caller_lock = self.tmp / 'ssh-caller' / 'nested' / 'x.lock'
        caller_store = self.tmp / 'ssh-canary.json'
        caller_store.write_text('CANARY-NOT-JSON', encoding='utf-8')
        records, effect = ledger()
        request = {'project': 'p', 'actor': 'attacker', 'action': 'bd',
                   'args': ['create', 'x'], 'operation_id': 'op-ssh-authority',
                   'authority': authority_descriptor('usr_a', 'sess_a')}
        request['authority'].update({'store': str(caller_store), 'lock': str(caller_lock)})
        result = run_guarded(request, self.tmp / 'journal.json', effect)
        self.assertEqual(0, result['returncode'], result)
        self.assertEqual(1, len(records))
        self.assertFalse(caller_lock.exists(), 'caller-chosen lock path was created')
        self.assertFalse(caller_lock.parent.exists(), 'caller-chosen directory was created')

    def test_trusted_config_ignores_request_store_and_lock(self):
        caller_lock = self.tmp / 'cfg-caller' / 'nested' / 'x.lock'
        caller_store = self.tmp / 'cfg-canary.json'
        caller_store.write_text('CANARY-NOT-JSON', encoding='utf-8')
        records, effect = ledger()
        request = {'project': 'p', 'actor': 'attacker', 'action': 'bd',
                   'args': ['create', 'x'], 'operation_id': 'op-cfg-authority',
                   'authority': authority_descriptor('usr_a', 'sess_a')}
        request['authority'].update({'store': str(caller_store), 'lock': str(caller_lock)})
        result = run_guarded(request, self.tmp / 'journal.json', effect,
                             authority_config=self.config)
        # The server-side store granted the capability; the hostile paths were ignored.
        self.assertEqual(0, result['returncode'], result)
        self.assertEqual(1, len(records))
        self.assertFalse(caller_lock.exists())
        self.assertTrue((self.server_store.parent / (self.server_store.name + '.lock')).exists())

    def test_configured_mutation_refuses_a_missing_descriptor(self):
        records, effect = ledger()
        request = {'project': 'p', 'actor': 'attacker', 'action': 'bd',
                   'args': ['create', 'x'], 'operation_id': 'op-no-authority'}
        result = run_guarded(request, self.tmp / 'journal.json', effect,
                             authority_config=self.config, require_authority=True)
        self.assertEqual(126, result['returncode'], result)
        self.assertEqual([], records)

    def test_configured_mutation_refuses_an_unauthorized_descriptor(self):
        # A session that is not in the document is denied by the server-side store,
        # never by whatever the request claims.
        records, effect = ledger()
        request = {'project': 'p', 'actor': 'attacker', 'action': 'bd',
                   'args': ['create', 'x'], 'operation_id': 'op-unknown',
                   'authority': authority_descriptor('usr_missing', 'sess_missing')}
        result = run_guarded(request, self.tmp / 'journal.json', effect,
                             authority_config=self.config, require_authority=True)
        self.assertEqual(126, result['returncode'], result)
        self.assertEqual(401, result.get('authority_status'))
        self.assertEqual([], records)


class OperationIdentityCase(unittest.TestCase):
    """2. operation-identity-not-principal-bound."""

    def setUp(self):
        self.tmp = unique_dir('identity4-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.server_store = self.tmp / 'server-state.json'
        self.server_store.write_text(json.dumps(authority_state()), encoding='utf-8')
        self.config = AuthorityConfig(str(self.server_store))
        self.journal = self.tmp / 'journal.json'

    def request(self, actor, user, session, operation_id='op-shared'):
        return {'project': 'p', 'actor': actor, 'action': 'bd', 'args': ['create', 'x'],
                'operation_id': operation_id,
                'authority': authority_descriptor(user, session)}

    def test_other_principal_with_the_same_operation_id_conflicts(self):
        records, effect = ledger()
        first = run_guarded(self.request('a', 'usr_a', 'sess_a'), self.journal, effect,
                            authority_config=self.config)
        second = run_guarded(self.request('b', 'usr_b', 'sess_b'), self.journal, effect,
                             authority_config=self.config)
        self.assertEqual(0, first['returncode'], first)
        # The pre-fix boundary replayed worker-a's envelope to worker-b; now it is a
        # clean conflict and worker-b's effect never runs.
        self.assertEqual(2, second['returncode'], second)
        self.assertEqual(1, len(records))
        self.assertEqual('', second.get('stdout', ''))
        self.assertIn('principal', second.get('stderr', ''))

    def test_same_principal_exact_retry_replays_the_committed_envelope(self):
        records, effect = ledger()
        first = run_guarded(self.request('a', 'usr_a', 'sess_a'), self.journal, effect,
                            authority_config=self.config)
        retry = run_guarded(self.request('a', 'usr_a', 'sess_a'), self.journal, effect,
                            authority_config=self.config)
        self.assertEqual(0, first['returncode'], first)
        self.assertEqual(first, retry)
        self.assertEqual(1, len(records))

    def test_replayed_identity_is_bound_to_the_route_body(self):
        records, effect = ledger()
        run_guarded(self.request('a', 'usr_a', 'sess_a'), self.journal, effect,
                    authority_config=self.config)
        changed = self.request('a', 'usr_a', 'sess_a')
        changed['args'] = ['create', 'different']
        conflict = run_guarded(changed, self.journal, effect, authority_config=self.config)
        self.assertEqual(2, conflict['returncode'], conflict)
        self.assertEqual(1, len(records))

    def test_actor_label_is_not_enough_to_replay_another_principal(self):
        # Same actor label, different principal: the stable principal wins.
        records, effect = ledger()
        run_guarded(self.request('shared-label', 'usr_a', 'sess_a'), self.journal, effect,
                    authority_config=self.config)
        conflict = run_guarded(self.request('shared-label', 'usr_b', 'sess_b'), self.journal,
                               effect, authority_config=self.config)
        self.assertEqual(2, conflict['returncode'], conflict)
        self.assertEqual(1, len(records))


class ExceptionUncertaintyCase(unittest.TestCase):
    """3. exception-path-duplicates."""

    def setUp(self):
        self.tmp = unique_dir('exception4-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.journal = self.tmp / 'journal.json'

    def test_effect_exception_keeps_the_reservation_and_never_duplicates(self):
        effects = {'n': 0}
        request = {'project': 'p', 'actor': 'a', 'action': 'bd', 'args': ['create', 'x'],
                   'operation_id': 'op-raise'}

        def effect():
            effects['n'] += 1
            if effects['n'] == 1:
                raise RuntimeError('native commit then transport failure')
            return {'returncode': 0, 'stdout': 'second', 'stderr': ''}

        first = run_guarded(request, self.journal, effect)
        retry = run_guarded(request, self.journal, effect)
        # The pre-fix boundary discarded the reservation and re-ran the effect.
        self.assertEqual(124, first['returncode'], first)
        self.assertEqual(124, retry['returncode'], retry)
        self.assertEqual(1, effects['n'])
        self.assertEqual('unknown', OperationJournal(self.journal).lookup('op-raise')['state'])

    def test_a_returncode_failure_keeps_the_reservation(self):
        effects = {'n': 0}
        request = {'project': 'p', 'actor': 'a', 'action': 'bd', 'args': ['create', 'x'],
                   'operation_id': 'op-fail'}

        def effect():
            effects['n'] += 1
            return {'returncode': 1, 'stdout': '', 'stderr': 'native failed after write'}

        first = run_guarded(request, self.journal, effect)
        retry = run_guarded(request, self.journal, effect)
        self.assertEqual(1, first['returncode'])
        self.assertEqual(124, retry['returncode'], retry)
        self.assertEqual(1, effects['n'])

    def test_proven_pre_effect_failure_releases_the_identity(self):
        effects = {'n': 0}
        request = {'project': 'p', 'actor': 'a', 'action': 'bd', 'args': ['create', 'x'],
                   'operation_id': 'op-pre-effect'}

        def effect():
            effects['n'] += 1
            if effects['n'] == 1:
                raise PreEffectFailure('rejected before any write')
            return {'returncode': 0, 'stdout': 'after-release', 'stderr': ''}

        with self.assertRaises(PreEffectFailure):
            run_guarded(request, self.journal, effect)
        # The proven pre-effect failure released the identity before the retry.
        self.assertIsNone(OperationJournal(self.journal).lookup('op-pre-effect'))
        retry = run_guarded(request, self.journal, effect)
        self.assertEqual(0, retry['returncode'], retry)
        self.assertEqual(2, effects['n'])
        self.assertEqual('committed',
                         OperationJournal(self.journal).lookup('op-pre-effect')['state'])

    def test_validation_returncode_two_releases_the_identity(self):
        effects = {'n': 0}
        request = {'project': 'p', 'actor': 'a', 'action': 'bd', 'args': ['create', 'x'],
                   'operation_id': 'op-validation'}

        def effect():
            effects['n'] += 1
            return {'returncode': 2, 'stdout': '', 'stderr': 'rejected'}

        run_guarded(request, self.journal, effect)
        retry = run_guarded(request, self.journal, effect)
        self.assertEqual(2, retry['returncode'], retry)
        self.assertEqual(2, effects['n'])


class JournalBoundsCase(unittest.TestCase):
    """20. Sustained load past the old limits never refuses; tombstones are bounded.

    The store is the indexed SQLite journal (revision 7). Fixtures seed it through
    ``import_document()`` - the same importer the legacy-JSON migration uses - and the
    byte budget is read from ``stats()['bytes']`` (the retained payload the bound
    governs) rather than from a JSON document's file size.
    """

    def setUp(self):
        self.tmp = unique_dir('bounds6-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.request = {'project': 'p', 'actor': 'x', 'action': 'bd', 'args': ['create', 'x']}

    def _record(self, state='committed', at=None, envelope=None):
        return {'state': state,
                'request_hash': http_authority.operation_hash(self.request),
                'principal': principal_key(self.request),
                'at': time.time() if at is None else at,
                'envelope': envelope or {'returncode': 0, 'stdout': 'seeded', 'stderr': ''}}

    def _seed(self, path, count, age=0, state='committed', envelope=None):
        moment = time.time() - age
        document = {'schema': 2, 'high_water': time.time(),
                    'entries': {'op-fill-%05d' % index:
                                self._record(state, moment - index, envelope)
                                for index in range(count)},
                    'tombstones': {}}
        OperationJournal(str(path)).import_document(document)
        return document

    def test_sustained_load_past_the_old_entry_limit_is_never_refused(self):
        path = self.tmp / 'sustained.json'
        # The old journal refused every new identity above 2000 live records. Here the
        # limit is pinned to the old value; the new policy compacts terminal receipts
        # instead of refusing.
        self._seed(path, 2000)
        records = []

        def effect():
            records.append('ran')
            return {'returncode': 0, 'stdout': 'ran', 'stderr': ''}

        refused = 0
        for index in range(150):
            result = run_guarded(dict(self.request, operation_id='op-new-%05d' % index),
                                 path, effect, journal_options={'limit': 2000})
            if result['returncode'] != 0:
                refused += 1
        self.assertEqual(0, refused)
        self.assertEqual(150, len(records))
        stats = OperationJournal(str(path), limit=2000).stats()
        self.assertLessEqual(stats['total'], 2000)
        self.assertGreater(stats['tombstones'], 0)
        # A compacted seed is a tombstone, not a lost identity: its exact retry is
        # refused as expired and never re-runs. ``op-fill-01999`` is the oldest seed,
        # so it is the first to be compacted.
        old = run_guarded(dict(self.request, operation_id='op-fill-01999'), path, effect,
                          journal_options={'limit': 2000})
        self.assertEqual(2, old['returncode'], old)
        self.assertIn('expired', old['stderr'])
        self.assertEqual(150, len(records))

    def test_byte_budget_past_the_old_document_cap_is_compacted_not_refused(self):
        path = self.tmp / 'bytes.json'
        big = {'returncode': 0, 'stdout': 'Z' * 49152, 'stderr': ''}
        self._seed(path, 200, envelope=big)
        self.assertGreater(OperationJournal(str(path)).stats()['bytes'], MAX_JOURNAL_BYTES)
        records, effect = ledger()
        result = run_guarded(dict(self.request, operation_id='op-after-bytes'), path, effect)
        # The old byte-cap journal refused this with rc=124 while every record was live.
        self.assertEqual(0, result['returncode'], result)
        self.assertEqual(['ran'], records)
        self.assertLessEqual(OperationJournal(str(path)).stats()['bytes'], MAX_JOURNAL_BYTES)
        self.assertGreater(OperationJournal(str(path)).stats()['tombstones'], 0)

    def test_byte_pressure_keeps_every_identity_and_never_re_executes(self):
        # The P1 defect: _fit() dropped the tombstone it had just created to get under
        # MAX_JOURNAL_BYTES, so 60 KB envelopes pushed identities out of both buckets and
        # a retry sweep re-ran all of them. No identity may be lost now.
        path = self.tmp / 'pressure.json'
        envelope = {'returncode': 0, 'stdout': 'Z' * 61440, 'stderr': ''}
        request = dict(self.request)
        journal = OperationJournal(str(path))
        # ~150 x 60 KB is past the 8 MiB payload budget, so every later write is under
        # byte pressure and compacts a terminal receipt to a tombstone.
        for index in range(160):
            key = 'op-%04d' % index
            journal.reserve(key, http_authority.operation_hash(request),
                            principal_key(request))
            journal.complete(key, envelope, http_authority.operation_hash(request),
                             principal_key(request))
        stats = journal.stats()
        self.assertLessEqual(stats['bytes'], MAX_JOURNAL_BYTES)
        self.assertGreater(stats['tombstones'], 0)
        # No identity is in the "no entry and no tombstone" bucket.
        missing = [index for index in range(160)
                   if journal.lookup('op-%04d' % index) is None]
        self.assertEqual([], missing)
        # Every exact retry is either a replay of the live receipt or refused as
        # expired; none re-executes, and the count of effects never rises.
        records, effect = ledger()
        outcomes = {}
        for index in range(160):
            retry = run_guarded(dict(request, operation_id='op-%04d' % index), path, effect)
            outcomes[retry['returncode']] = outcomes.get(retry['returncode'], 0) + 1
        self.assertEqual([], records)
        self.assertEqual(set(outcomes), {0, 2})
        # A tombstoned identity stays refused on the very next write after compaction.
        reclaimed = [index for index in range(160)
                     if journal.lookup('op-%04d' % index)['state'] == 'expired']
        self.assertTrue(reclaimed)
        journal.reserve('op-later', 'hash', 'actor:x')
        for index in reclaimed:
            entry = journal.lookup('op-%04d' % index)
            self.assertEqual('expired', entry['state'])
            retry = run_guarded(dict(request, operation_id='op-%04d' % index), path, effect)
            self.assertEqual(2, retry['returncode'], retry)
        self.assertEqual([], records)

    def test_tombstones_are_count_bounded_without_dropping_a_live_one(self):
        path = self.tmp / 'tombstones.json'
        journal = OperationJournal(str(path), limit=1000, tombstone_limit=4,
                                   committed_retention=10, max_skew=10 ** 9)
        for index in range(20):
            key = 'op-%03d' % index
            journal.reserve(key, 'hash', 'actor:x')
            journal.complete(key, {'returncode': 0, 'stdout': 'x', 'stderr': ''},
                             'hash', 'actor:x')
        # Reclaim stops at the tombstone budget and leaves the rest as expired entries,
        # where they are still refused as expired. Before revision 7 it silently dropped
        # the oldest tombstones, which is what let a retry re-execute.
        self.assertEqual(4, journal.reclaim_expired(now=time.time() + 60))
        stats = journal.stats()
        self.assertEqual(4, stats['tombstones'])
        self.assertEqual(4, stats['tombstone_limit'])
        self.assertEqual(16, stats['total'])
        identities = list(journal._load()) + list(journal._tombstones())
        self.assertEqual(20, len(identities))
        self.assertEqual(20, len(set(identities)))
        records, effect = ledger()
        for index in range(20):
            retry = run_guarded(dict(self.request, operation_id='op-%03d' % index), path,
                                effect,
                                journal_options={'limit': 1000, 'tombstone_limit': 4,
                                                 'committed_retention': 10,
                                                 'max_skew': 10 ** 9})
            self.assertEqual(2, retry['returncode'], retry)
        self.assertEqual([], records)
        # A mutation that can only satisfy the entry budget by compacting a receipt to a
        # tombstone - when the tombstone budget is full - fails closed, leaves the store
        # intact and attempts no effect.
        before = [journal.lookup('op-%03d' % index) for index in range(20)]
        result = run_guarded(dict(self.request, operation_id='op-over-budget'), path, effect,
                             journal_options={'limit': 15, 'tombstone_limit': 4,
                                              'committed_retention': 10,
                                              'max_skew': 10 ** 9})
        self.assertEqual(124, result['returncode'], result)
        self.assertEqual([], records)
        self.assertIsNone(journal.lookup('op-over-budget'))
        self.assertEqual(before, [journal.lookup('op-%03d' % index) for index in range(20)])
        self.assertEqual(4, journal.stats()['tombstones'])

    def test_fail_closed_leaves_the_store_intact_and_attempts_no_effect(self):
        path = self.tmp / 'closed.json'
        journal = OperationJournal(str(path), limit=2, max_envelope=64)
        request = dict(self.request)
        for index in range(2):
            key = 'op-live-%d' % index
            journal.reserve(key, 'hash', 'actor:x')
            journal.mark_unknown(key)
        records, effect = ledger()
        before = [journal.lookup('op-live-%d' % index) for index in range(2)]
        retry = run_guarded(dict(request, operation_id='op-blocked'), path, effect,
                            journal_options={'limit': 2, 'max_envelope': 64})
        self.assertEqual(124, retry['returncode'], retry)
        self.assertIn('no effect was attempted', retry['stderr'])
        self.assertEqual([], records)
        self.assertIsNone(journal.lookup('op-blocked'))
        self.assertEqual(before, [journal.lookup('op-live-%d' % index) for index in range(2)])
        self.assertEqual(2, journal.stats()['total'])

    def test_oversized_envelope_is_reported_uncertain_not_replayed(self):
        path = self.tmp / 'big.json'
        request = dict(self.request, operation_id='op-big')
        request_hash = http_authority.operation_hash(request)
        journal = OperationJournal(str(path), max_envelope=64)
        journal.reserve('op-big', request_hash, principal_key(request))
        journal.complete('op-big', {'returncode': 0, 'stdout': 'Z' * 4096, 'stderr': ''},
                         request_hash, principal_key(request))
        entry = journal.lookup('op-big')
        self.assertTrue(entry.get('envelope_omitted'))
        self.assertIsNone(entry.get('envelope'))
        records, effect = ledger()
        retry = run_guarded(request, path, effect)
        # The pre-fix boundary replayed the whole oversized envelope as a result.
        self.assertEqual(124, retry['returncode'], retry)
        self.assertEqual([], records)

    def test_prune_is_the_only_explicit_removal(self):
        path = self.tmp / 'prune.json'
        journal = OperationJournal(str(path))
        records, effect = ledger()
        request = dict(self.request, operation_id='op-prune')
        run_guarded(request, path, effect)
        self.assertIsNotNone(journal.lookup('op-prune'))
        stats = journal.stats()
        self.assertEqual(1, stats['states']['committed'])
        removed = journal.prune(time.time() + 1)
        self.assertEqual(1, removed)
        self.assertIsNone(journal.lookup('op-prune'))

    def test_journal_full_is_raised_before_the_effect(self):
        path = self.tmp / 'raise.json'
        journal = OperationJournal(str(path), limit=1)
        journal.reserve('op-first', 'hash', 'actor:x')
        with self.assertRaises(JournalFull):
            journal.reserve('op-second', 'hash', 'actor:x')
        self.assertEqual(1, len(journal._load()))


@unittest.skipUnless(os.name == 'posix', 'endpoint.py imports fcntl; POSIX only')
class RealEndpointAuthorityCase(unittest.TestCase):
    """The SSH worker entry point itself, with a disposable canonical runtime."""

    def setUp(self):
        import importlib.util
        self.tmp = unique_dir('endpoint4-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / 'root'
        (self.root / 'bin').mkdir(parents=True)
        (self.root / 'projects' / 'probe' / '.beads').mkdir(parents=True)
        (self.root / 'projects' / 'probe' / '.beads' / 'metadata.json').write_text(
            '{}', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'probe'}), encoding='utf-8')
        bd = self.root / 'bin' / 'bd'
        bd.write_text('#!/bin/sh\necho \'{"id":"proj-1","title":"x"}\'\nexit 0\n',
                      encoding='utf-8')
        bd.chmod(0o755)
        spec = importlib.util.spec_from_file_location('real_endpoint', str(ROOT / 'endpoint.py'))
        self.endpoint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.endpoint)
        self.config = AuthorityConfig(str(self.tmp / 'authority.json'))
        self.config_path = self.tmp / 'authority.json'
        self.config_path.write_text(json.dumps(authority_state(project='probe')),
                                    encoding='utf-8')

    def test_hostile_authority_block_is_ignored_on_the_ssh_path(self):
        canary_lock = self.tmp / 'ssh-caller' / 'nested' / 'x.lock'
        canary_store = self.tmp / 'ssh-canary.json'
        canary_store.write_text('CANARY-NOT-JSON', encoding='utf-8')
        request = {'project': 'probe', 'actor': 'attacker', 'action': 'bd', 'args': ['create', 'x'],
                   'operation_id': 'op-endpoint-hostile',
                   'authority': {'via': 'session', 'user_id': 'usr_a',
                                 'session_hash': 'sess_a', 'project': 'probe',
                                 'capability': 'tasks.write',
                                 'store': str(canary_store), 'lock': str(canary_lock)}}
        result = self.endpoint.execute(self.root, request)
        self.assertEqual(0, result['returncode'], result)
        self.assertFalse(canary_lock.exists(), 'SSH request created a caller-chosen lock')
        self.assertFalse(canary_lock.parent.exists())

    def test_launch_configured_endpoint_uses_server_authority(self):
        canary_lock = self.tmp / 'http-caller' / 'nested' / 'x.lock'
        canary_store = self.tmp / 'http-canary.json'
        canary_store.write_text('CANARY-NOT-JSON', encoding='utf-8')
        request = {'project': 'probe', 'actor': 'attacker', 'action': 'bd', 'args': ['create', 'x'],
                   'operation_id': 'op-endpoint-configured',
                   'authority': {'via': 'session', 'user_id': 'usr_a',
                                 'session_hash': 'sess_a', 'project': 'probe',
                                 'capability': 'tasks.write',
                                 'store': str(canary_store), 'lock': str(canary_lock)}}
        result = self.endpoint.execute(self.root, request, authority_config=self.config,
                                       require_authority=True)
        self.assertEqual(0, result['returncode'], result)
        self.assertFalse(canary_lock.exists())
        # A descriptor-less mutation through the trusted launch is refused.
        bare = {'project': 'probe', 'actor': 'attacker', 'action': 'bd', 'args': ['create', 'x'],
                'operation_id': 'op-endpoint-bare'}
        refused = self.endpoint.execute(self.root, bare, authority_config=self.config,
                                        require_authority=True)
        self.assertEqual(126, refused['returncode'], refused)

    def test_timeout_after_a_native_commit_does_not_duplicate(self):
        marker = self.tmp / 'native-created.jsonl'
        calls = {'n': 0}
        real_run = self.endpoint.subprocess.run

        def fake_run(argv, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                marker.write_text('committed\n', encoding='utf-8')
                raise subprocess.TimeoutExpired(argv, 120)
            return real_run(argv, **kwargs)

        self.endpoint.subprocess.run = fake_run
        try:
            request = {'project': 'probe', 'actor': 'attacker', 'action': 'bd',
                       'args': ['create', 'x'], 'operation_id': 'op-endpoint-timeout',
                       'authority': authority_descriptor('usr_a', 'sess_a', project='probe')}
            first = self.endpoint.execute(self.root, request, authority_config=self.config,
                                          require_authority=True)
            retry = self.endpoint.execute(self.root, request, authority_config=self.config,
                                          require_authority=True)
        finally:
            self.endpoint.subprocess.run = real_run
        self.assertEqual(124, first['returncode'], first)
        self.assertEqual(124, retry['returncode'], retry)
        self.assertEqual(1, calls['n'], 'the native command ran a second time')
        self.assertEqual('committed\n', marker.read_text(encoding='utf-8'))


# ==================================================================== round 5
# The three round-5 review requests. Each class carries the negative case the
# pre-fix revision exhibited and the fixed behaviour, so it fails on rev4.

def runner_for(invocations=None):
    """A NativeRunner over a dispatch that records invocations and returns stdout."""
    calls = [] if invocations is None else invocations

    def dispatch(argv):
        calls.append(list(argv))
        return 'ok'

    return NativeRunner(dispatch), calls


class PreEffectValidationCase(unittest.TestCase):
    """1. validation-errors-become-uncertain."""

    def setUp(self):
        self.tmp = unique_dir('preeffect5-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.journal = self.tmp / 'journal.json'

    @staticmethod
    def _request(operation_id):
        return {'project': 'p', 'actor': 'a', 'action': 'review',
                'args': ['task-1', '@attachment:0'], 'operation_id': operation_id}

    def test_refusal_before_any_write_keeps_the_real_error_and_releases(self):
        runner, calls = runner_for()
        effects = {'n': 0}

        def effect():
            effects['n'] += 1
            runner(['export', '--all'])
            if effects['n'] == 1:
                raise ValueError('Invalid attachment')
            return {'returncode': 0, 'stdout': 'after-release', 'stderr': ''}

        request = self._request('op-refusal')
        # The pre-fix boundary flattened this to a generic 124 and held the identity,
        # so an identical retry returned 124 forever.
        with self.assertRaisesRegex(ValueError, 'Invalid attachment'):
            run_guarded(request, self.journal, effect, runner=runner)
        self.assertIsNone(OperationJournal(self.journal).lookup('op-refusal'))
        self.assertEqual([['export', '--all']], calls)
        retry = run_guarded(request, self.journal, effect, runner=runner)
        self.assertEqual(0, retry['returncode'], retry)
        self.assertEqual(2, effects['n'], 'the released identity did not re-execute')

    def test_refusal_after_a_write_stays_uncertain_and_holds_the_identity(self):
        runner, _ = runner_for()
        effects = {'n': 0}

        def effect():
            effects['n'] += 1
            runner(['comments', 'add', 'task-1', 'body'])
            raise ValueError('Owner changed during handoff')

        request = self._request('op-postwrite')
        first = run_guarded(request, self.journal, effect, runner=runner)
        self.assertEqual(124, first['returncode'], first)
        self.assertIn('Owner changed during handoff', first['stderr'])
        retry = run_guarded(request, self.journal, effect, runner=runner)
        self.assertEqual(124, retry['returncode'], retry)
        self.assertEqual(1, effects['n'])
        self.assertEqual('unknown',
                         OperationJournal(self.journal).lookup('op-postwrite')['state'])

    def test_an_unrecognized_verb_fails_safe_to_uncertain(self):
        runner, _ = runner_for()

        def effect():
            runner(['frobnicate', 'task-1'])
            raise ValueError('refused after an unknown write verb')

        first = run_guarded(self._request('op-unknown-verb'), self.journal, effect,
                            runner=runner)
        self.assertEqual(124, first['returncode'], first)
        self.assertIsNotNone(OperationJournal(self.journal).lookup('op-unknown-verb'))

    def test_without_a_runner_a_refusal_is_still_uncertain(self):
        def effect():
            raise ValueError('unproven refusal')

        result = run_guarded(self._request('op-unproven'), self.journal, effect)
        self.assertEqual(124, result['returncode'], result)
        self.assertEqual('unknown',
                         OperationJournal(self.journal).lookup('op-unproven')['state'])

    def test_verb_classification_treats_unknown_verbs_as_writes(self):
        self.assertFalse(is_mutating_invocation(['export', '--all']))
        self.assertFalse(is_mutating_invocation(['show', 'task-1', '--json']))
        self.assertFalse(is_mutating_invocation(['comments', 'task-1', '--json']))
        self.assertFalse(is_mutating_invocation(['merge-slot', 'check', '--json']))
        self.assertTrue(is_mutating_invocation(['comments', 'add', 'task-1', 'body']))
        self.assertTrue(is_mutating_invocation(['merge-slot', 'acquire', '--holder', 'a']))
        self.assertTrue(is_mutating_invocation(['update', 'task-1', '--json']))
        self.assertTrue(is_mutating_invocation(['create', '--title', 'x']))
        self.assertTrue(is_mutating_invocation(['set-state', 'task-1', 'implemented=passed']))
        self.assertTrue(is_mutating_invocation([]))
        self.assertTrue(is_mutating_invocation(['brand-new-verb']))


class JournalRetentionCase(unittest.TestCase):
    """19. Short committed receipts, durable tombstones and the clock-skew guard."""

    def setUp(self):
        self.tmp = unique_dir('retention6-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.request = {'project': 'p', 'actor': 'x', 'action': 'bd',
                        'args': ['create', 'x']}

    def _record(self, state='committed', at=None, out='seeded'):
        return {'state': state,
                'request_hash': http_authority.operation_hash(self.request),
                'principal': principal_key(self.request),
                'at': time.time() if at is None else at,
                'envelope': {'returncode': 0, 'stdout': out, 'stderr': ''}}

    def _seed(self, path, count, age=0, state='committed'):
        moment = time.time() - age
        document = {'schema': 2, 'high_water': time.time(),
                    'entries': {'op-fill-%05d' % index:
                                self._record(state, moment - index)
                                for index in range(count)},
                    'tombstones': {}}
        OperationJournal(str(path)).import_document(document)
        return document

    def _age(self, path, operation_id, seconds):
        journal = OperationJournal(str(path))
        record = journal.lookup(operation_id)
        self.assertIsNotNone(record)
        record['at'] = time.time() - seconds
        journal.import_document({'entries': {operation_id: record}})

    def test_a_full_journal_of_live_reservations_still_fails_closed(self):
        path = self.tmp / 'live.json'
        self._seed(path, 3, state='unknown')
        records, effect = ledger()
        result = run_guarded(dict(self.request, operation_id='op-new'), path, effect,
                             journal_options={'limit': 3})
        # Only genuinely live reservations fail closed: nothing is evicted, no effect
        # runs, and the oldest identity is still reported as uncertain.
        self.assertEqual(124, result['returncode'], result)
        self.assertEqual([], records)
        oldest = run_guarded(dict(self.request, operation_id='op-fill-00000'), path, effect,
                             journal_options={'limit': 3})
        self.assertEqual(124, oldest['returncode'], oldest)
        self.assertEqual([], records)

    def test_expired_identities_are_reclaimed_instead_of_locking_out(self):
        path = self.tmp / 'expired.json'
        self._seed(path, 3, age=JOURNAL_COMMITTED_RETENTION_SECONDS + 60)
        records, effect = ledger()
        result = run_guarded(dict(self.request, operation_id='op-new'), path, effect,
                             journal_options={'limit': 3})
        # The pre-fix journal returned 124 here until an operator pruned by hand.
        self.assertEqual(0, result['returncode'], result)
        self.assertEqual(['ran'], records)
        stats = OperationJournal(str(path), limit=3).stats()
        self.assertEqual(1, stats['total'])
        self.assertEqual(3, stats['tombstones'])

    def test_an_in_window_retry_replays_and_never_re_runs(self):
        path = self.tmp / 'replay.json'
        records, effect = ledger()
        request = dict(self.request, operation_id='op-replay')
        first = run_guarded(request, path, effect)
        retry = run_guarded(request, path, effect)
        self.assertEqual(0, first['returncode'], first)
        self.assertEqual('ran', retry['stdout'])
        self.assertEqual(['ran'], records)

    def test_an_expired_retry_is_refused_not_replayed_or_re_run(self):
        path = self.tmp / 'stale.json'
        request = dict(self.request, operation_id='op-stale')
        moment = time.time() - JOURNAL_COMMITTED_RETENTION_SECONDS - 60
        OperationJournal(str(path)).import_document({
            'high_water': time.time(), 'tombstones': {},
            'entries': {'op-stale': {
                'state': 'committed', 'request_hash': http_authority.operation_hash(request),
                'principal': principal_key(request), 'at': moment,
                'envelope': {'returncode': 0, 'stdout': 'stale', 'stderr': ''}}}})
        records, effect = ledger()
        result = run_guarded(request, path, effect)
        self.assertEqual(2, result['returncode'], result)
        self.assertIn('expired', result['stderr'])
        self.assertEqual([], records)
        # The expired record is retained until reclaimed or capacity pressure.
        self.assertIsNotNone(OperationJournal(str(path)).lookup('op-stale'))

    def test_reclaim_expired_and_stats_are_operator_visible(self):
        path = self.tmp / 'operator.json'
        self._seed(path, 3, age=JOURNAL_COMMITTED_RETENTION_SECONDS + 60)
        journal = OperationJournal(str(path))
        stats = journal.stats()
        self.assertEqual(3, stats['expired'])
        self.assertEqual(JOURNAL_RETENTION_SECONDS, stats['retention'])
        self.assertEqual(3, journal.reclaim_expired())
        # Reclaim writes a tombstone instead of dropping the identity, so a retry is
        # still refused rather than treated as a brand-new operation.
        tombstone = journal.lookup('op-fill-00000')
        self.assertIsNotNone(tombstone)
        self.assertEqual('expired', tombstone['state'])
        self.assertEqual(3, journal.stats()['tombstones'])
        self.assertEqual(0, journal.stats()['expired'])

    def test_reclaim_expired_keeps_in_window_identities(self):
        path = self.tmp / 'mixed.json'
        document = self._seed(path, 2, age=JOURNAL_COMMITTED_RETENTION_SECONDS + 60)
        document['entries']['op-fresh'] = self._record('committed', time.time())
        OperationJournal(str(path)).import_document({'entries':
                                                     {'op-fresh':
                                                      document['entries']['op-fresh']}})
        journal = OperationJournal(str(path))
        self.assertEqual(2, journal.reclaim_expired())
        fresh = journal.lookup('op-fresh')
        self.assertIsNotNone(fresh)
        self.assertEqual('committed', fresh['state'])
        self.assertEqual(2, journal.stats()['tombstones'])

    def test_retry_of_a_reclaimed_committed_op_creates_zero_new_effects(self):
        path = self.tmp / 'reclaimed-committed.json'
        request = dict(self.request, operation_id='op-committed')
        records, effect = ledger()
        first = run_guarded(request, path, effect)
        self.assertEqual(0, first['returncode'], first)
        self._age(path, 'op-committed', JOURNAL_COMMITTED_RETENTION_SECONDS + 60)
        self.assertEqual(1, OperationJournal(str(path)).reclaim_expired())
        retry = run_guarded(request, path, effect)
        self.assertEqual(2, retry['returncode'], retry)
        self.assertIn('expired', retry['stderr'])
        self.assertEqual(1, len(records))
        self.assertEqual('expired',
                         OperationJournal(str(path)).lookup('op-committed')['state'])

    def test_retry_of_a_reclaimed_uncertain_op_creates_zero_new_effects(self):
        path = self.tmp / 'reclaimed-uncertain.json'
        request = dict(self.request, operation_id='op-uncertain')
        records = []

        def effect():
            records.append('ran')
            raise RuntimeError('after the reservation')

        first = run_guarded(request, path, effect)
        self.assertEqual(124, first['returncode'], first)
        self._age(path, 'op-uncertain', JOURNAL_RETENTION_SECONDS + 60)
        self.assertEqual(1, OperationJournal(str(path)).reclaim_expired())
        retry = run_guarded(request, path, effect)
        self.assertEqual(2, retry['returncode'], retry)
        self.assertIn('expired', retry['stderr'])
        self.assertEqual(1, len(records))

    def test_capacity_reclaim_tombstones_instead_of_re_running(self):
        path = self.tmp / 'automatic.json'
        self._seed(path, 3, age=JOURNAL_COMMITTED_RETENTION_SECONDS + 60)
        records, effect = ledger()
        result = run_guarded(dict(self.request, operation_id='op-new'), path, effect,
                             journal_options={'limit': 3})
        self.assertEqual(0, result['returncode'], result)
        retry = run_guarded(dict(self.request, operation_id='op-fill-00000'), path, effect,
                            journal_options={'limit': 3})
        # The pre-fix automatic reclaim dropped the seed, so this retry re-ran it.
        self.assertEqual(2, retry['returncode'], retry)
        self.assertIn('expired', retry['stderr'])
        self.assertEqual(['ran'], records)

    def test_a_jumped_forward_clock_does_not_let_reclaim_drop_live_entries(self):
        path = self.tmp / 'skew.json'
        high = time.time()
        self._seed(path, 3)
        real_time = time.time
        try:
            time.time = lambda: high + 8 * 24 * 3600
            records, effect = ledger()
            result = run_guarded(dict(self.request, operation_id='op-new'), path, effect,
                                 journal_options={'limit': 3})
            stats = OperationJournal(str(path), limit=3).stats()
        finally:
            time.time = real_time
        self.assertEqual(124, result['returncode'], result)
        self.assertEqual([], records)
        self.assertTrue(stats['clock_skewed'])
        self.assertEqual(3, stats['total'])
        self.assertEqual(0, stats['tombstones'])
        # With the clock corrected the in-window receipt still replays; it never ran.
        replay = run_guarded(dict(self.request, operation_id='op-fill-00000'), path,
                             lambda: {'returncode': 0, 'stdout': 'ran', 'stderr': ''},
                             journal_options={'limit': 3})
        self.assertEqual(0, replay['returncode'], replay)
        self.assertEqual('seeded', replay['stdout'])
        self.assertEqual([], records)

    def test_repeated_saves_never_ratchet_the_high_water_mark(self):
        # The P3 defect: _advance_high_water() ran on every save as
        # max(high, min(now, high + max_skew)), so repeated writes ratcheted the mark
        # up to a jumped-forward clock and reclaim then tombstoned a 60 s-old committed
        # receipt and a live uncertain reservation.
        path = self.tmp / 'ratchet.json'
        request = dict(self.request)
        digest = http_authority.operation_hash(request)
        principal = principal_key(request)
        journal = OperationJournal(str(path))
        journal.reserve('op-k', digest, principal)
        journal.complete('op-k', {'returncode': 0, 'stdout': 'keeper', 'stderr': ''},
                         digest, principal)
        journal.reserve('op-u', digest, principal)
        journal.mark_unknown('op-u')
        start = journal.stats()['high_water']
        real = time.time()
        clock = [real]
        real_time = time.time
        records, effect = ledger()
        try:
            time.time = lambda: clock[0]
            clock[0] = real + 8 * 24 * 3600
            for index in range(6):
                result = run_guarded(dict(request, operation_id='op-skew-%d' % index), path,
                                     effect)
                self.assertEqual(0, result['returncode'], result)
                stats = OperationJournal(str(path)).stats()
                self.assertTrue(stats['clock_skewed'])
                self.assertLessEqual(stats['high_water'], start + JOURNAL_MAX_SKEW_SECONDS + 1)
                self.assertEqual(0, stats['tombstones'])
                self.assertEqual('committed',
                                 OperationJournal(str(path)).lookup('op-k')['state'])
                self.assertEqual('unknown',
                                 OperationJournal(str(path)).lookup('op-u')['state'])
            # Reclaim stays refused while the mark is implausibly behind the clock.
            self.assertEqual(0, OperationJournal(str(path)).reclaim_expired())
            self.assertEqual(0, OperationJournal(str(path)).stats()['tombstones'])
            # The six skewed writes are six genuinely new operations: each ran once.
            self.assertEqual(6, len(records))
            baseline = len(records)
            # Once the clock is corrected the committed receipt REPLAYS and the
            # uncertain reservation reports uncertainty; nothing was ever tombstoned.
            clock[0] = real + 60
            replay = run_guarded(dict(request, operation_id='op-k'), path, effect)
            self.assertEqual(0, replay['returncode'], replay)
            self.assertEqual('keeper', replay['stdout'])
            uncertain = run_guarded(dict(request, operation_id='op-u'), path, effect)
            self.assertEqual(124, uncertain['returncode'], uncertain)
            self.assertEqual(baseline, len(records))
            self.assertEqual(0, OperationJournal(str(path)).stats()['tombstones'])
            # A genuine clock correction is accepted only by the explicit operator
            # reset; reclaim then compacts closed windows to tombstones.
            clock[0] = real + 8 * 24 * 3600
            self.assertEqual(0, OperationJournal(str(path)).reclaim_expired())
            self.assertEqual(clock[0], OperationJournal(str(path)).reset_high_water())
            self.assertEqual(2, OperationJournal(str(path)).reclaim_expired())
            stats = OperationJournal(str(path)).stats()
            self.assertFalse(stats['clock_skewed'])
            self.assertEqual(2, stats['tombstones'])
            refused = run_guarded(dict(request, operation_id='op-k'), path, effect)
            self.assertEqual(2, refused['returncode'], refused)
            self.assertEqual(baseline, len(records))
        finally:
            time.time = real_time

    def test_a_legacy_json_journal_is_migrated_once_and_keeps_its_identities(self):
        path = self.tmp / 'legacy.json'
        moment = time.time() - JOURNAL_COMMITTED_RETENTION_SECONDS - 60
        request = dict(self.request)
        legacy = {'schema': 2, 'high_water': time.time(),
                  'entries': {'op-legacy-live': self._record('committed', time.time())},
                  'tombstones': {'op-legacy-tomb':
                                 {'state': 'expired',
                                  'request_hash': http_authority.operation_hash(request),
                                  'principal': principal_key(request), 'at': moment,
                                  'reclaimed_at': moment}}}
        path.write_text(json.dumps(legacy), encoding='utf-8')
        journal = OperationJournal(str(path))
        # The store is beside the document the caller named, and the legacy document is
        # imported exactly once and kept for audit under a .migrated name.
        self.assertEqual('legacy.sqlite3', journal.path.name)
        self.assertEqual('committed', journal.lookup('op-legacy-live')['state'])
        self.assertTrue(journal.path.is_file())
        self.assertTrue(path.with_name('legacy.json.migrated').is_file())
        self.assertFalse(path.exists())
        self.assertEqual('expired', journal.lookup('op-legacy-tomb')['state'])
        self.assertEqual(2, len(journal._load()) + len(journal._tombstones()))
        records, effect = ledger()
        retry = run_guarded(dict(request, operation_id='op-legacy-tomb'), path, effect)
        self.assertEqual(2, retry['returncode'], retry)
        replay = run_guarded(dict(request, operation_id='op-legacy-live'), path, effect)
        self.assertEqual(0, replay['returncode'], replay)
        self.assertEqual('seeded', replay['stdout'])
        self.assertEqual([], records)
        # The legacy flat (schema-1) shape migrates too.
        flat = self.tmp / 'flat.json'
        flat.write_text(json.dumps({'op-flat': self._record('committed', time.time())}),
                        encoding='utf-8')
        migrated = OperationJournal(str(flat))
        self.assertEqual('committed', migrated.lookup('op-flat')['state'])
        self.assertGreater(migrated.stats()['high_water'], 0)

    def test_the_default_store_path_is_the_documented_sqlite_file(self):
        project = self.tmp / 'projects' / 'p'
        project.mkdir(parents=True)
        self.assertEqual('.http-operations.sqlite3', journal_path(project).name)
        self.assertEqual('.http-operations.json', LEGACY_JOURNAL_FILENAME)
        # The live store is one indexed row per identity, not a whole-document rewrite.
        journal = OperationJournal(str(journal_path(project)))
        journal.reserve('op-one', 'hash', 'actor:x')
        journal.complete('op-one', {'returncode': 0, 'stdout': 'x', 'stderr': ''},
                         'hash', 'actor:x')
        self.assertEqual('committed', journal.lookup('op-one')['state'])
        self.assertGreater(journal_path(project).stat().st_size, 0)
        self.assertGreaterEqual(JOURNAL_TOMBSTONE_LIMIT, 1)
        self.assertEqual(JOURNAL_TOMBSTONE_LIMIT, journal.stats()['tombstone_limit'])


class SshPrincipalClaimCase(unittest.TestCase):
    """3. ssh-can-claim-principal."""

    def setUp(self):
        self.tmp = unique_dir('sshclaim5-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.server_store = self.tmp / 'server-state.json'
        self.server_store.write_text(json.dumps(authority_state()), encoding='utf-8')
        self.config = AuthorityConfig(str(self.server_store))
        self.journal = self.tmp / 'journal.json'

    @staticmethod
    def _request(actor, operation_id):
        return {'project': 'p', 'actor': actor, 'action': 'bd', 'args': ['create', 'x'],
                'operation_id': operation_id,
                'authority': authority_descriptor('usr_a', 'sess_a')}

    def test_an_ssh_descriptor_cannot_claim_another_principal(self):
        # The pre-fix boundary read the request's authority block even with no authority
        # store configured, so an SSH caller could write under another principal's
        # identity and a later authenticated retry replayed the forged envelope.
        self.assertEqual('actor:attacker', principal_key(self._request('attacker', 'x')))
        self.assertEqual('user:usr_a|cred:-|session:sess_a',
                         principal_key(self._request('attacker', 'x'), True))
        forged, effect = ledger()
        first = run_guarded(self._request('attacker', 'op-forged'), self.journal, effect)
        self.assertEqual(0, first['returncode'], first)
        self.assertEqual('actor:attacker',
                         OperationJournal(self.journal).lookup('op-forged')['principal'])
        # The authenticated HTTP call as the claimed principal is a clean conflict.
        second = run_guarded(self._request('attacker', 'op-forged'), self.journal, effect,
                             authority_config=self.config, require_authority=True)
        self.assertEqual(2, second['returncode'], second)
        self.assertEqual(1, len(forged))

    def test_a_configured_descriptor_still_binds_the_real_principal(self):
        records, effect = ledger()
        result = run_guarded(self._request('attacker', 'op-http'), self.journal, effect,
                             authority_config=self.config, require_authority=True)
        self.assertEqual(0, result['returncode'], result)
        entry = OperationJournal(self.journal).lookup('op-http')
        self.assertEqual('user:usr_a|cred:-|session:sess_a', entry['principal'])


class PreEffectValidationHttpCase(EndpointCase):
    """1. A canonical pre-write refusal is a client error, not UncertainOutcome."""

    def test_approve_without_a_contribution_is_a_clean_invalid_request(self):
        alex, project = self.setup_project()
        task_id = self.create_task(alex, project, 'approve without contribution').data['id']
        self.assertEqual(200, self.request('POST', '/v1/projects/%s/tasks/%s/claim'
                                           % (project, task_id), {}, token=alex).status)
        response = self.request(
            'POST', '/v1/projects/%s/tasks/%s/reviews' % (project, task_id),
            {'operation': 'approve', 'schema_version': 1, 'previous': None,
             'operation_id': 'op-' + secrets.token_hex(6),
             'contribution': 'c0ffee1234', 'summary': 'accepted'}, token=alex)
        # rev4 turned this real canonical refusal into UncertainOutcome (503).
        self.assertEqual(422, response.status, response.data)
        self.assertEqual('invalid_payload', response.data['error']['code'])


class JournalRetentionHttpCase(EndpointCase):
    """19. A full journal of past-window receipts must not lock out HTTP writes."""

    def test_past_window_receipts_do_not_lock_out_authenticated_http_writes(self):
        alex, project = self.setup_project()
        legacy = self.canonical_root / project / LEGACY_JOURNAL_FILENAME
        legacy.parent.mkdir(parents=True, exist_ok=True)
        moment = time.time() - JOURNAL_COMMITTED_RETENTION_SECONDS - 60
        # 2500 records is past the old 2000-entry limit; the endpoint must compact
        # them to tombstones instead of refusing the authenticated mutation. This also
        # exercises the one-time legacy-document migration through the real endpoint.
        data = {'schema': 2, 'high_water': time.time(), 'tombstones': {}, 'entries': {}}
        for index in range(2500):
            data['entries']['op-seed-%05d' % index] = {
                'state': 'committed', 'request_hash': 'seed', 'principal': 'actor:seed',
                'at': moment - index,
                'envelope': {'returncode': 0, 'stdout': 'seeded', 'stderr': ''}}
        legacy.write_text(json.dumps(data), encoding='utf-8')
        created = self.create_task(alex, project, 'after a full expired journal',
                                   key='after-full-journal-0001')
        # rev4/rev5 refused every authenticated mutation with 503/124 until an operator
        # pruned by hand.
        self.assertEqual(201, created.status, created.data)
        journal = OperationJournal(str(journal_path(legacy.parent)))
        self.assertEqual(2500, journal.stats()['tombstones'])
        # The migrated document is kept for audit and is not read a second time.
        self.assertTrue(legacy.with_name(legacy.name + '.migrated').is_file())
        self.assertFalse(legacy.exists())
        identities = list(journal._load()) + list(journal._tombstones())
        self.assertEqual(2501, len(identities))


@unittest.skipUnless(os.name == 'posix', 'endpoint.py imports fcntl; POSIX only')
class RealEndpointPreEffectCase(unittest.TestCase):
    """1. The refusal and release through the real ``endpoint.py`` entry point."""

    def setUp(self):
        self.tmp = unique_dir('endpoint5-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / 'root'
        (self.root / 'bin').mkdir(parents=True)
        (self.root / 'projects' / 'probe' / '.beads').mkdir(parents=True)
        (self.root / 'projects' / 'probe' / '.beads' / 'metadata.json').write_text(
            '{}', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'probe'}), encoding='utf-8')
        bd = self.root / 'bin' / 'bd'
        bd.write_text('#!/bin/sh\necho \'{"id":"proj-1","title":"x"}\'\nexit 0\n',
                      encoding='utf-8')
        bd.chmod(0o755)

    def _invoke(self, request):
        completed = subprocess.run([sys.executable, str(ROOT / 'endpoint.py'),
                                    '--root', str(self.root)],
                                   input=json.dumps(request), text=True, encoding='utf-8',
                                   capture_output=True, timeout=60)
        self.assertFalse(completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def _journal(self):
        return OperationJournal(str(self.root / 'projects' / 'probe'
                                    / '.http-operations.json'))

    def test_review_payload_mismatch_keeps_rc_two_and_releases(self):
        request = {'project': 'probe', 'actor': 'attacker', 'action': 'review',
                   'args': ['kittrial-1', '@attachment:0'],
                   'attachments': {'0': {'flag': '--file', 'text': '{}'}},
                   'operation_id': 'op-real-review'}
        first = self._invoke(request)
        self.assertEqual(2, first['returncode'], first)
        self.assertIn('Payload task mismatch', first['stderr'])
        self.assertIsNone(self._journal().lookup('op-real-review'))
        # The identity was released: the identical retry re-executes and refuses again
        # with the real message rather than returning 124 forever.
        second = self._invoke(request)
        self.assertEqual(2, second['returncode'], second)
        self.assertIn('Payload task mismatch', second['stderr'])

    def test_unknown_work_flag_keeps_rc_two_and_releases(self):
        request = {'project': 'probe', 'actor': 'attacker', 'action': 'work',
                   'args': ['--nope'], 'operation_id': 'op-real-work'}
        first = self._invoke(request)
        self.assertEqual(2, first['returncode'], first)
        self.assertIn('unrecognized arguments', first['stderr'])
        self.assertIsNone(self._journal().lookup('op-real-work'))
        second = self._invoke(request)
        self.assertEqual(2, second['returncode'], second)


@unittest.skipUnless(os.name == 'posix', 'admin.py operator commands are POSIX only')
class JournalOperatorCommandCase(unittest.TestCase):
    """2. The documented operator recovery path for the operation journal."""

    def setUp(self):
        self.tmp = unique_dir('adminjournal5-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / 'root'
        self.project = self.root / 'projects' / 'probe'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')

    def _run(self, *extra):
        completed = subprocess.run([sys.executable, str(ROOT / 'admin.py'),
                                    '--root', str(self.root), 'journal', 'probe', *extra],
                                   text=True, encoding='utf-8', capture_output=True, timeout=60)
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_inspect_reclaim_and_prune(self):
        legacy = self.project / LEGACY_JOURNAL_FILENAME
        legacy.write_text(json.dumps({
            'op-expired': {'state': 'committed', 'request_hash': 'h',
                           'principal': 'actor:x',
                           'at': time.time() - JOURNAL_RETENTION_SECONDS - 60,
                           'envelope': {'returncode': 0, 'stdout': '', 'stderr': ''}},
            'op-live': {'state': 'committed', 'request_hash': 'h', 'principal': 'actor:x',
                        'at': time.time(),
                        'envelope': {'returncode': 0, 'stdout': '', 'stderr': ''}}}),
            encoding='utf-8')
        inspected = self._run()
        self.assertEqual(2, inspected['stats']['total'])
        self.assertEqual(1, inspected['stats']['expired'])
        reclaimed = self._run('--reclaim-expired')
        self.assertEqual(1, reclaimed['reclaimed'])
        self.assertEqual(1, reclaimed['stats']['total'])
        self.assertEqual(1, reclaimed['stats']['tombstones'])
        pruned = self._run('--prune-before', str(time.time() + 1))
        self.assertEqual(1, pruned['pruned'])
        self.assertEqual(0, pruned['stats']['total'])

    def test_reset_high_water_is_the_explicit_clock_recovery(self):
        journal = OperationJournal(str(journal_path(self.project)))
        journal.reserve('op-committed', 'h', 'actor:x')
        journal.complete('op-committed', {'returncode': 0, 'stdout': '', 'stderr': ''},
                         'h', 'actor:x')
        record = journal.lookup('op-committed')
        record['at'] = time.time() - JOURNAL_COMMITTED_RETENTION_SECONDS - 60
        journal.import_document({'entries': {'op-committed': record}})
        real = journal.stats()['high_water']
        ahead = real + 8 * 24 * 3600
        # While the clock is implausibly ahead of the persisted mark, reclaim refuses and
        # the closed-window identity is not dropped.
        self.assertEqual(0, journal.reclaim_expired(now=ahead))
        stats = journal.stats(now=ahead)
        self.assertTrue(stats['clock_skewed'])
        self.assertEqual(0, stats['reclaimable'])
        self.assertEqual(0, stats['tombstones'])
        self.assertEqual('committed', journal.lookup('op-committed')['state'])
        # The operator reset accepts the corrected clock; reclaim then compacts the
        # closed window to a tombstone rather than dropping the identity.
        reset = self._run('--reset-high-water')
        self.assertAlmostEqual(time.time(), reset['high_water'], delta=30)
        self.assertFalse(reset['stats']['clock_skewed'])
        reclaimed = self._run('--reset-high-water', '--reclaim-expired')
        self.assertEqual(1, reclaimed['reclaimed'])
        self.assertEqual(1, reclaimed['stats']['tombstones'])
        self.assertEqual('expired',
                         OperationJournal(str(journal_path(self.project))).lookup(
                             'op-committed')['state'])


class ResultsBoundCase(EndpointCase):
    """21. The canonical result replay map is bounded and never duplicates on eviction."""

    def test_remember_result_bounds_by_count_and_bytes(self):
        results = {}
        for index in range(50):
            remember_result(results, 'k-%02d' % index, {'blob': 'Z' * 200},
                            limit=1000, max_bytes=2000)
        self.assertLess(len(results), 50)
        self.assertLessEqual(len(json.dumps(results, separators=(',', ':'))), 3000)
        counted = {}
        for index in range(12):
            remember_result(counted, 'k-%02d' % index, {'ok': True}, limit=8)
        self.assertEqual(8, len(counted))
        self.assertNotIn('k-00', counted)
        self.assertIn('k-11', counted)

    def test_results_map_does_not_grow_without_bound(self):
        alex, project = self.setup_project()
        original = http_service.RESULTS_LIMIT
        http_service.RESULTS_LIMIT = 8
        try:
            for index in range(24):
                created = self.create_task(alex, project, 'task %d' % index,
                                           key='bound-key-%05d' % index)
                self.assertEqual(201, created.status, created.data)
            results = self.service.state['canonical']['results']
            self.assertLessEqual(len(results), 8)
        finally:
            http_service.RESULTS_LIMIT = original

    def test_an_evicted_local_result_still_never_duplicates_the_effect(self):
        alex, project = self.setup_project()
        original = http_service.RESULTS_LIMIT
        http_service.RESULTS_LIMIT = 2
        try:
            first = self.create_task(alex, project, 'evict me', key='steady-key-0001')
            self.assertEqual(201, first.status, first.data)
            task_id = first.data['id']
            for index in range(6):
                self.create_task(alex, project, 'filler %d' % index,
                                 key='filler-key-%05d' % index)
            results = self.service.state['canonical']['results']
            self.assertLessEqual(len(results), 2)
            retry = self.create_task(alex, project, 'evict me', key='steady-key-0001')
            self.assertEqual(201, retry.status, retry.data)
            # The durable endpoint journal replays the same identity, so the locals
            # eviction never repeats the create.
            self.assertEqual(task_id, retry.data['id'])
            self.assertEqual(1, len([row for row in self.canonical_rows()
                                     if row.get('title') == 'evict me']))
        finally:
            http_service.RESULTS_LIMIT = original


if __name__ == '__main__':
    unittest.main()
