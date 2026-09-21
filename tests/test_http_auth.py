"""Unit tests for the HTTP authentication and authorization core.

Disposable synthetic state only: every store is a temp file and every credential a
throwaway value. These tests cover the section 11 unit-test obligations of
``docs/HTTP_TRANSPORT_DESIGN.md``: password-hash policy, session/token expiry and
revocation, role decisions, actor binding, project filters, error redaction and
canonical idempotency keys.
"""
import json
import os
import secrets
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Disposable state stays out of source control (``.runtime/`` is ignored) and inside a
# writable root; some hosted/sandboxed runners deny writes under the OS temp dir.
TMP_ROOT = Path(os.environ.get('ORCHESTRA_TEST_TMP', str(ROOT / '.runtime' / 'test-tmp')))


def unique_dir(prefix):
    """Create a uniquely named directory with default ACLs.

    ``tempfile.mkdtemp`` applies mode 0700, which some sandboxed Windows runners
    translate into an ACL that forbids the process from writing inside it.
    """
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    while True:
        candidate = TMP_ROOT / (prefix + secrets.token_hex(6))
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue

from http_auth import (HttpError, Principal, Service, Store, canonical_json, hash_password,
                       needs_rehash, now_iso, request_hash, token_hash, verify_password)

ADMIN = 'root-admin'
ADMIN_PASSWORD = 'correct-horse-battery-staple'


class Clock:
    """A deterministic clock so expiry can be tested without sleeping."""

    def __init__(self, start=1_700_000_000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


class AuthCase(unittest.TestCase):
    def setUp(self):
        # Tolerant cleanup: a constrained temp directory must not fail the suite during
        # interpreter shutdown.
        self.tmp_path = unique_dir('auth-')
        self.addCleanup(shutil.rmtree, self.tmp_path, ignore_errors=True)
        self.clock = Clock()
        self.store = Store(self.tmp_path / 'state.json', clock=self.clock)
        self.admin_user = Service.bootstrap_superuser(self.store, ADMIN, ADMIN_PASSWORD)
        self.service = Service(self.store)

    # -- helpers ---------------------------------------------------------------
    def principal_for(self, token):
        return self.service.authenticate(token)

    def make_user(self, username, password):
        """Create an account and set its password with superuser authority."""
        admin = self.principal_for(self.service.login(ADMIN, ADMIN_PASSWORD)['session_token'])
        created = self.service.create_user(admin, username)
        self.service.change_password(admin, created['id'], None, password)
        token = self.service.login(username, password)['session_token']
        return created, self.principal_for(token), token

    def make_project(self, principal, name):
        return self.service.create_project(principal, name)['id']

    # -- password verifier -----------------------------------------------------
    def test_password_round_trip_and_policy(self):
        verifier = hash_password('a-good-password')
        self.assertTrue(verify_password(verifier, 'a-good-password'))
        self.assertFalse(verify_password(verifier, 'a-good-passwerd'))
        self.assertFalse(verify_password('garbage', 'a-good-password'))
        self.assertFalse(verify_password(None, 'a-good-password'))
        self.assertFalse(needs_rehash(verifier))

    def test_password_length_is_bounded(self):
        with self.assertRaises(HttpError) as caught:
            hash_password('short')
        self.assertEqual(caught.exception.status, 422)
        with self.assertRaises(HttpError):
            hash_password('x' * 2000)

    def test_low_cost_verifier_requests_rehash(self):
        weak = hash_password('a-good-password', n=2 ** 8)
        self.assertTrue(needs_rehash(weak))

    # -- sessions --------------------------------------------------------------
    def test_session_expiry_is_enforced(self):
        _, principal, token = self.make_user('alex', 'alex-password-1')
        self.assertEqual(principal.user_id, self.principal_for(token).user_id)
        self.clock.advance(self.service.session_idle + 1)
        with self.assertRaises(HttpError) as caught:
            self.principal_for(token)
        self.assertEqual(caught.exception.status, 401)

    def test_absolute_lifetime_caps_idle_refresh(self):
        _, _, token = self.make_user('alex', 'alex-password-1')
        # Keep the session busy so idle expiry never fires.
        for _ in range(3):
            self.clock.advance(self.service.session_idle - 1)
            self.principal_for(token)
        self.clock.advance(self.service.session_absolute)
        with self.assertRaises(HttpError):
            self.principal_for(token)

    def test_logout_revokes_the_session(self):
        _, principal, token = self.make_user('alex', 'alex-password-1')
        self.service.logout(principal)
        with self.assertRaises(HttpError) as caught:
            self.principal_for(token)
        self.assertEqual(caught.exception.status, 401)

    def test_disable_revokes_sessions_and_credentials(self):
        admin = self.principal_for(self.service.login(ADMIN, ADMIN_PASSWORD)['session_token'])
        created, alex, token = self.make_user('alex', 'alex-password-1')
        # The admin stays an owner, so disabling alex does not remove the project's
        # final owner (that invariant is exercised separately).
        project = self.make_project(admin, 'Alpha')
        self.service.set_member(admin, project, alex.user_id, 'owner')
        credential = self.service.issue_credential(alex, project)['secret']
        self.service.disable_user(admin, created['id'])
        with self.assertRaises(HttpError):
            self.principal_for(token)
        with self.assertRaises(HttpError):
            self.principal_for(credential)

    def test_login_failure_is_uniform_and_throttled(self):
        self.make_user('alex', 'alex-password-1')
        for _ in range(self.service.login_max_attempts):
            with self.assertRaises(HttpError) as caught:
                self.service.login('alex', 'wrong-password')
            self.assertEqual(caught.exception.status, 401)
        with self.assertRaises(HttpError) as caught:
            self.service.login('alex', 'alex-password-1')
        self.assertEqual(caught.exception.status, 429)
        self.clock.advance(301)
        self.assertTrue(self.service.login('alex', 'alex-password-1')['session_token'])

    def test_unknown_user_cannot_be_distinguished(self):
        self.make_user('alex', 'alex-password-1')
        with self.assertRaises(HttpError) as missing:
            self.service.login('nobody', 'whatever-password')
        with self.assertRaises(HttpError) as wrong:
            self.service.login('alex', 'whatever-password')
        self.assertEqual(missing.exception.code, wrong.exception.code)
        self.assertEqual(missing.exception.message, wrong.exception.message)

    # -- accounts and reset ----------------------------------------------------
    def test_only_superuser_creates_accounts(self):
        _, principal, _ = self.make_user('alex', 'alex-password-1')
        with self.assertRaises(HttpError) as caught:
            self.service.create_user(principal, 'mallory')
        self.assertEqual(caught.exception.status, 403)

    def test_bootstrap_refuses_a_second_superuser(self):
        try:
            Service.bootstrap_superuser(self.store, 'second-root', 'another-password')
        except HttpError as error:
            self.assertEqual(error.status, 409)
        else:
            self.fail('a second bootstrap must be refused')

    def test_reset_is_single_use_and_expires(self):
        admin = self.principal_for(self.service.login(ADMIN, ADMIN_PASSWORD)['session_token'])
        created, _, old_token = self.make_user('alex', 'alex-password-1')
        issued = self.service.issue_reset(admin, created['id'])
        self.service.redeem_reset(created['id'], issued['reset_value'], 'alex-password-2')
        with self.assertRaises(HttpError):
            self.service.redeem_reset(created['id'], issued['reset_value'], 'alex-password-3')
        # Redeeming revoked the old session and the new password works.
        with self.assertRaises(HttpError):
            self.principal_for(old_token)
        self.assertTrue(self.service.login('alex', 'alex-password-2')['session_token'])
        # A fresh value expires.
        second = self.service.issue_reset(admin, created['id'])
        self.clock.advance(self.service.reset_ttl + 1)
        with self.assertRaises(HttpError):
            self.service.redeem_reset(created['id'], second['reset_value'], 'alex-password-4')

    def test_reissue_invalidates_the_previous_reset(self):
        admin = self.principal_for(self.service.login(ADMIN, ADMIN_PASSWORD)['session_token'])
        created, _, _ = self.make_user('alex', 'alex-password-1')
        first = self.service.issue_reset(admin, created['id'])
        self.service.issue_reset(admin, created['id'])
        with self.assertRaises(HttpError):
            self.service.redeem_reset(created['id'], first['reset_value'], 'alex-password-2')

    def test_superuser_cannot_be_disabled(self):
        admin = self.principal_for(self.service.login(ADMIN, ADMIN_PASSWORD)['session_token'])
        with self.assertRaises(HttpError) as caught:
            self.service.disable_user(admin, admin.user_id)
        self.assertEqual(caught.exception.status, 409)

    # -- project isolation and roles ------------------------------------------
    def test_non_member_cannot_see_a_project(self):
        _, alex, _ = self.make_user('alex', 'alex-password-1')
        _, blair, _ = self.make_user('blair', 'blair-password-1')
        project = self.make_project(alex, 'Alpha')
        self.assertEqual('owner', self.service.require_project(alex, project)[1])
        with self.assertRaises(HttpError) as caught:
            self.service.require_project(blair, project)
        self.assertEqual(caught.exception.status, 404)

    def test_role_matrix_denies_viewer_mutations(self):
        admin = self.principal_for(self.service.login(ADMIN, ADMIN_PASSWORD)['session_token'])
        _, alex, _ = self.make_user('alex', 'alex-password-1')
        created, blair, _ = self.make_user('blair', 'blair-password-1')
        project = self.make_project(alex, 'Alpha')
        self.service.set_member(alex, project, created['id'], 'viewer')
        self.assertEqual('viewer', self.service.require_project(blair, project)[1])
        with self.assertRaises(HttpError) as caught:
            self.service.require_project(blair, project, 'contributor')
        self.assertEqual(caught.exception.status, 403)
        # A viewer cannot promote itself, and only a superuser assigns owners.
        with self.assertRaises(HttpError):
            self.service.set_member(blair, project, blair.user_id, 'contributor')
        with self.assertRaises(HttpError):
            self.service.set_member(alex, project, created['id'], 'owner')
        self.service.set_member(admin, project, created['id'], 'contributor')
        self.assertEqual('contributor', self.service.require_project(blair, project)[1])

    def test_final_project_owner_cannot_be_removed_or_disabled(self):
        _, alex, _ = self.make_user('alex', 'alex-password-1')
        project = self.make_project(alex, 'Alpha')
        with self.assertRaises(HttpError) as caught:
            self.service.remove_member(alex, project, alex.user_id)
        self.assertEqual(caught.exception.status, 403)
        with self.assertRaises(HttpError) as caught:
            self.service.disable_user(alex, alex.user_id)
        self.assertEqual(caught.exception.status, 409)

    def test_archived_project_still_authorizes_its_owner(self):
        _, alex, _ = self.make_user('alex', 'alex-password-1')
        project = self.make_project(alex, 'Alpha')
        self.service.archive_project(alex, project)
        with self.assertRaises(HttpError) as caught:
            self.service.archive_project(alex, project)
        self.assertEqual(caught.exception.status, 409)

    # -- actor binding ---------------------------------------------------------
    def test_session_actor_must_match_the_account(self):
        _, alex, _ = self.make_user('alex', 'alex-password-1')
        self.assertEqual(alex.user_id, self.service.bind_actor(alex, alex.user_id))
        with self.assertRaises(HttpError) as caught:
            self.service.bind_actor(alex, 'someone-else')
        self.assertEqual(caught.exception.status, 403)

    def test_credential_actor_namespace(self):
        admin = self.principal_for(self.service.login(ADMIN, ADMIN_PASSWORD)['session_token'])
        _, alex, _ = self.make_user('alex', 'alex-password-1')
        project = self.make_project(admin, 'Alpha')
        secret = self.service.issue_credential(admin, project, actor='worker')['secret']
        worker = self.principal_for(secret)
        self.assertEqual('worker/run-9', self.service.bind_actor(worker, 'worker/run-9'))
        self.assertEqual('worker', self.service.bind_actor(worker, 'worker'))
        with self.assertRaises(HttpError):
            self.service.bind_actor(worker, 'other/run-9')

    # -- credentials -----------------------------------------------------------
    def test_credential_is_project_scoped_and_revocable(self):
        admin = self.principal_for(self.service.login(ADMIN, ADMIN_PASSWORD)['session_token'])
        alpha = self.make_project(admin, 'Alpha')
        beta = self.make_project(admin, 'Beta')
        issued = self.service.issue_credential(admin, alpha, scopes=['tasks'])
        worker = self.principal_for(issued['secret'])
        self.assertEqual('credential', worker.via)
        self.assertEqual(alpha, self.service.require_project(worker, alpha)[0]['id'])
        with self.assertRaises(HttpError) as caught:
            self.service.require_project(worker, beta)
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(HttpError) as caught:
            self.service.authenticate(issued['secret'], required_scope='reviews')
        self.assertEqual(caught.exception.status, 403)
        self.service.revoke_credential(admin, alpha, issued['id'])
        with self.assertRaises(HttpError):
            self.principal_for(issued['secret'])

    def test_credential_expiry(self):
        admin = self.principal_for(self.service.login(ADMIN, ADMIN_PASSWORD)['session_token'])
        project = self.make_project(admin, 'Alpha')
        secret = self.service.issue_credential(admin, project)['secret']
        self.clock.advance(self.service.credential_ttl + 1)
        with self.assertRaises(HttpError):
            self.principal_for(secret)

    def test_credential_issue_requires_ownership(self):
        admin = self.principal_for(self.service.login(ADMIN, ADMIN_PASSWORD)['session_token'])
        _, blair, _ = self.make_user('blair', 'blair-password-1')
        project = self.make_project(admin, 'Alpha')
        self.service.set_member(admin, project, blair.user_id, 'contributor')
        with self.assertRaises(HttpError) as caught:
            self.service.issue_credential(blair, project)
        self.assertEqual(caught.exception.status, 403)

    def test_disabled_owner_credential_stops_working(self):
        admin = self.principal_for(self.service.login(ADMIN, ADMIN_PASSWORD)['session_token'])
        created = self.service.create_user(admin, 'alex')
        self.service.change_password(admin, created['id'], None, 'alex-password-1')
        alex = self.principal_for(self.service.login('alex', 'alex-password-1')['session_token'])
        # Admin remains an owner so alex is not the project's final owner.
        project = self.make_project(admin, 'Alpha')
        self.service.set_member(admin, project, created['id'], 'owner')
        secret = self.service.issue_credential(alex, project)['secret']
        self.service.disable_user(admin, created['id'])
        with self.assertRaises(HttpError):
            self.principal_for(secret)

    # -- idempotency -----------------------------------------------------------
    def test_idempotency_replay_and_payload_conflict(self):
        _, alex, _ = self.make_user('alex', 'alex-password-1')
        project = self.make_project(alex, 'Alpha')
        body = request_hash({'title': 'one'})
        self.assertIsNone(self.service.idempotency_check(alex, project, 'tasks.create', 'k1', body))
        digest = self.service.idempotency_begin(alex, project, 'tasks.create', 'k1', body)
        self.service.idempotency_commit(digest, 201, {'id': 'task_1'})
        self.assertEqual(('replay', 201, {'id': 'task_1'}),
                         self.service.idempotency_check(alex, project, 'tasks.create', 'k1', body))
        with self.assertRaises(HttpError) as caught:
            self.service.idempotency_check(alex, project, 'tasks.create', 'k1',
                                           request_hash({'title': 'two'}))
        self.assertEqual(caught.exception.status, 409)

    def test_idempotency_unknown_state_is_reported(self):
        _, alex, _ = self.make_user('alex', 'alex-password-1')
        project = self.make_project(alex, 'Alpha')
        digest = self.service.idempotency_begin(alex, project, 'tasks.create', 'k2',
                                                request_hash({'title': 'one'}))
        self.service.idempotency_unknown(digest)
        self.assertEqual(('unknown', None, None),
                         self.service.idempotency_check(alex, project, 'tasks.create', 'k2',
                                                        request_hash({'title': 'one'})))

    def test_idempotency_key_is_scoped_per_principal(self):
        _, alex, _ = self.make_user('alex', 'alex-password-1')
        _, blair, _ = self.make_user('blair', 'blair-password-1')
        project = self.make_project(alex, 'Alpha')
        body = request_hash({'title': 'one'})
        self.service.idempotency_begin(alex, project, 'tasks.create', 'shared', body)
        # The same key from a different principal does not collide.
        self.assertIsNone(self.service.idempotency_check(blair, project, 'tasks.create',
                                                         'shared', body))

    # -- redaction and canonical helpers --------------------------------------
    def test_audit_never_records_a_secret(self):
        admin = self.principal_for(self.service.login(ADMIN, ADMIN_PASSWORD)['session_token'])
        created, _, _ = self.make_user('alex', 'alex-password-1')
        project = self.make_project(admin, 'Alpha')
        issued = self.service.issue_credential(admin, project)
        reset = self.service.issue_reset(admin, created['id'])
        blob = json.dumps(self.service.state['audit'])
        for secret in (issued['secret'], reset['reset_value'], ADMIN_PASSWORD,
                       'alex-password-1'):
            self.assertNotIn(secret, blob)

    def test_state_never_persists_a_plaintext_token(self):
        _, _, token = self.make_user('alex', 'alex-password-1')
        state = json.dumps(self.service.export_state())
        self.assertNotIn(token, state)
        self.assertIn(token_hash(token)[:12], state)

    def test_canonical_json_and_request_hash_are_order_independent(self):
        self.assertEqual(canonical_json({'a': 1, 'b': 2}), canonical_json({'b': 2, 'a': 1}))
        self.assertEqual(request_hash({'a': 1, 'b': 2}), request_hash({'b': 2, 'a': 1}))
        self.assertNotEqual(request_hash({'a': 1}), request_hash({'a': 2}))

    def test_now_iso_shape(self):
        self.assertRegex(now_iso(0), r'^1970-01-01T00:00:00Z$')


if __name__ == '__main__':
    unittest.main()
