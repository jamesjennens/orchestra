"""Disposable tests for the standard-library office HTTP client transport.

They run the real service on loopback and drive it only through :class:`http_client.Client`,
including the uncertain-write reconciliation path and the plaintext refusal.
"""
import os
import secrets
import shutil
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from http_auth import Service, Store
from http_client import Client, HttpApiError, UncertainOutcome, new_idempotency_key
from http_service import InProcessBackend, create_server

TMP_ROOT = Path(os.environ.get('ORCHESTRA_TEST_TMP', str(ROOT / '.runtime' / 'test-tmp')))
ADMIN = 'root-admin'
ADMIN_PASSWORD = 'correct-horse-battery-staple'


def unique_dir(prefix):
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    while True:
        candidate = TMP_ROOT / (prefix + secrets.token_hex(6))
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue


class ClientCase(unittest.TestCase):
    def setUp(self):
        self.tmp_path = unique_dir('cli-')
        self.addCleanup(shutil.rmtree, self.tmp_path, ignore_errors=True)
        self.store = Store(self.tmp_path / 'state.json')
        Service.bootstrap_superuser(self.store, ADMIN, ADMIN_PASSWORD)
        self.service = Service(self.store)
        self.backend = InProcessBackend(self.service)
        self.httpd = create_server(self.service, self.backend, host='127.0.0.1', port=0)
        self.url = 'http://127.0.0.1:%d' % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def client(self, **kwargs):
        return Client(self.url, **kwargs)

    def make_user(self, username, password):
        admin = self.client()
        admin.login(ADMIN, ADMIN_PASSWORD)
        created = admin.create_account(username)
        admin.change_password(created['id'], password)
        return created

    # -- transport policy ------------------------------------------------------
    def test_plaintext_is_refused_off_loopback(self):
        with self.assertRaises(ValueError):
            Client('http://office.example.invalid')
        # https anywhere and http on loopback are allowed.
        Client('https://office.example.invalid')
        Client('http://127.0.0.1:8080')
        Client('http://office.example.invalid', allow_plaintext=True)

    def test_errors_are_clean_and_identifiable(self):
        self.make_user('alex', 'alex-password-1')
        client = self.client()
        with self.assertRaises(HttpApiError) as caught:
            client.login('alex', 'wrong-password')
        self.assertEqual(401, caught.exception.status)
        self.assertEqual('unauthenticated', caught.exception.code)
        self.assertTrue(caught.exception.request_id)

    def test_cookie_mode_sends_csrf(self):
        self.make_user('alex', 'alex-password-1')
        client = self.client(use_cookie=True)
        client.login('alex', 'alex-password-1')
        self.assertTrue(client.cookie)
        project = client.create_project('Cookie Project')
        self.assertEqual('Cookie Project', project['name'])

    # -- flows -----------------------------------------------------------------
    def test_worker_flow_through_the_client(self):
        admin = self.client()
        admin.login(ADMIN, ADMIN_PASSWORD)
        created = admin.create_account('alex')
        admin.change_password(created['id'], 'alex-password-1')
        alex = self.client()
        alex.login('alex', 'alex-password-1')

        project = alex.create_project('Alpha')['id']
        task = alex.create_task(project, 'office API')['id']
        issued = alex.issue_credential(project, label='worker', actor='worker')
        secret = issued['credential']['secret']

        worker = self.client().use_credential(secret)
        claimed = worker.claim_task(project, task, actor='worker/run-1')
        self.assertEqual('worker/run-1', claimed['assignee'])
        worker.add_checkpoint(project, task, previous=None, summary='implemented')
        worker.add_review(project, task, 'contribute', commit='a' * 40, base_commit='b' * 40,
                          bundle_sha256='c' * 64, summary='delivered')
        self.assertEqual('awaiting-review', alex.get_task(project, task)['review_state'])
        alex.add_review(project, task, 'approve', summary='accepted')
        self.assertEqual('approved', alex.get_task(project, task)['review_state'])
        self.assertEqual(1, alex.list_tasks(project)['total'])
        self.assertTrue(alex.history(project, task)['items'])
        worker.add_feedback(project, 'worker note')
        self.assertEqual(1, alex.list_feedback(project)['total'])
        self.assertTrue(alex.audit(project)['items'])
        self.assertEqual('session', alex.whoami()['via'])
        alex.logout()

    def test_uncertain_outcome_must_be_retried_with_the_same_key(self):
        self.make_user('alex', 'alex-password-1')
        client = self.client()
        client.login('alex', 'alex-password-1')
        project = client.create_project('Alpha')['id']
        key = new_idempotency_key()
        self.backend.fail_next('tasks.create')
        with self.assertRaises(UncertainOutcome) as caught:
            client.create_task(project, 'flaky', key=key)
        self.assertEqual(key, caught.exception.key)
        # The identical retry reconciles instead of duplicating the task.
        reconciled = client.create_task(project, 'flaky', key=key)
        self.assertEqual(1, client.list_tasks(project)['total'])
        self.assertEqual(reconciled['id'], client.list_tasks(project)['items'][0]['id'])


if __name__ == '__main__':
    unittest.main()
