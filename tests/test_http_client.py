"""Disposable tests for the standard-library office HTTP client transport.

They run the real service on loopback and drive it only through :class:`http_client.Client`,
including the uncertain-write reconciliation path and the plaintext refusal.
"""
import contextlib
import copy
import io
import json
import os
import secrets
import shutil
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import http_client
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

    def test_the_servers_message_is_escaped_before_it_is_printed(self):
        """kittrial-5bb.241: HttpApiError carries the SERVER's own message, and a kit program that
        prints the exception (tools/demonstrate_review_targeting.py prints `{error}`) must not let a
        server rewrite or reorder the terminal."""
        error = HttpApiError(422, 'invalid', 'bad \x1b[31m \x07 \u202e \u2028 text', request_id='req-1')
        said = str(error)
        for character in ('\x1b', '\x07', '\u202e', '\u2028'):
            self.assertNotIn(character, said)
        self.assertIn('\\x1b[31m', said)
        self.assertIn('\\x07', said)
        self.assertIn('\\u202e', said)
        self.assertIn('\\u2028', said)
        self.assertIn('(invalid)', said)
        self.assertIn('[req-1]', said)
        # The data a caller works with is unchanged: only the printed form is escaped.
        self.assertIn('\x1b', error.message)
        self.assertEqual('bad \x1b[31m \x07 \u202e \u2028 text', error.message)

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

    def test_integration_check_and_ledger_are_read_only_client_reads(self):
        """kittrial-5bb.383: the check and its step ledger through the copied client and CLI."""
        self.make_user('alex', 'alex-password-1')
        client = self.client()
        client.login('alex', 'alex-password-1')
        project = client.create_project('Alpha')['id']
        task = client.create_task(project, 'ledger read')['id']
        before = copy.deepcopy(self.service.state['canonical']['tasks'][task])
        check = client.integration_check(project, task, 'main')
        self.assertEqual([row['check'] for row in check['checks']],
                         ['approval', 'source_commit', 'slot', 'hold', 'reverted'])
        self.assertEqual([step['step'] for step in check['next']],
                         ['scope', 'reviewed', 'integrated', 'integrated-mark', 'slot-release'])
        ledger = client.ledger(project, task, 'main')
        self.assertEqual(set(ledger), {'task', 'target', 'merge_ready', 'next'})
        self.assertEqual(ledger['target'], 'main')
        self.assertEqual(ledger['next'], check['next'])
        for command, expected in (('integration-check', 'checks'), ('ledger', 'next')):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = http_client.main(['--base-url', self.url, '--token=' + client.token,
                                         command, project, task, '--target', 'main'])
            self.assertEqual(code, 0)
            self.assertIn(expected, json.loads(output.getvalue()))
        self.assertEqual(self.service.state['canonical']['tasks'][task], before)

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


DASH_TOKEN = '-abc.def_-ghi--jkl'


class CLICredentialArgumentTests(unittest.TestCase):
    """A bearer credential may begin with '-' (kittrial-5bb.296).

    argparse reads a *separate* '-abc...' argument as an option, which is how CI run
    38765801091 failed with "argument --token: expected one argument" when a URL-safe
    random token happened to begin with '-'.  The credential therefore travels as
    --flag=VALUE, and that spelling is pinned here for both credential flags.
    """

    def parser(self):
        return http_client._build_parser()

    def help_text(self, *command):
        """The help argparse prints for the parser (or a subcommand), as data."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit):
                self.parser().parse_args(list(command) + ['--help'])
        return out.getvalue()

    def test_help_says_the_equals_form_is_accepted(self):
        self.assertIn('--token=VALUE', self.help_text())
        self.assertIn('--credential=VALUE', self.help_text('call'))

    def test_a_token_beginning_with_a_dash_is_one_argument(self):
        args = self.parser().parse_args(
            ['--base-url', 'http://127.0.0.1:1', '--token=' + DASH_TOKEN, 'whoami'])
        self.assertEqual(DASH_TOKEN, args.token)

    def test_a_credential_beginning_with_a_dash_is_one_argument(self):
        args = self.parser().parse_args(
            ['--base-url', 'http://127.0.0.1:1', 'call', 'GET', '/v1/projects',
             '--credential=' + DASH_TOKEN])
        self.assertEqual(DASH_TOKEN, args.credential)

    def test_the_separate_argument_form_reads_the_dash_as_an_option(self):
        """The flaky failure: ('--token', token) when token begins with '-'."""
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                self.parser().parse_args(
                    ['--base-url', 'http://127.0.0.1:1', '--token', DASH_TOKEN, 'whoami'])
        self.assertIn('expected one argument', stderr.getvalue())

    def test_main_passes_a_dash_leading_token_to_the_client(self):
        made = []

        class RecordingClient:
            def __init__(self, base_url, **kwargs):
                self.base_url = base_url
                self.token = None
                made.append(self)

            def whoami(self):
                return {'via': 'session'}

        with mock.patch.object(http_client, 'Client', RecordingClient), \
                contextlib.redirect_stdout(io.StringIO()):
            code = http_client.main(['--base-url', 'http://127.0.0.1:1',
                                     '--token=' + DASH_TOKEN, 'whoami'])
        self.assertEqual(0, code)
        self.assertEqual(DASH_TOKEN, made[0].token)

    def test_a_generated_idempotency_key_can_never_start_with_a_dash(self):
        # The other random value the CLI documents after a flag already carries a
        # safe prefix, so the two-argument form is safe for it too.
        for _ in range(2000):
            self.assertFalse(new_idempotency_key().startswith('-'))


if __name__ == '__main__':
    unittest.main()
