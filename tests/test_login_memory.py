"""Log-in attempts must not cost memory without bound (kittrial-5bb.170, item 1).

A password check takes 16 MiB (scrypt). The checks already ran one after another, under
the service's state lock, but each connection has its own thread and the C allocator kept
the freed 16 MiB in every thread's own arena: 190 attempts left 3 GB resident. Every scrypt
computation now runs in one long-lived worker thread, and the number of log-ins in flight
is bounded before anything is checked.
"""
import hashlib
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_auth
import test_http_agents
from http_auth import HttpError

ADMIN, PASSWORD = test_http_agents.ADMIN, test_http_agents.ADMIN_PASSWORD


class WorkerTests(unittest.TestCase):
    def test_every_computation_runs_in_the_one_worker_thread(self):
        seen = []
        real = hashlib.scrypt

        def recorded(*args, **kwargs):
            seen.append(threading.current_thread())
            return real(*args, **kwargs)
        results = []

        def work(index):
            verifier = http_auth.hash_password('a password number %d' % index, n=2 ** 10)
            results.append((http_auth.verify_password(verifier, 'a password number %d' % index),
                            http_auth.verify_password(verifier, 'another password')))
        with mock.patch.object(hashlib, 'scrypt', recorded):
            callers = [threading.Thread(target=work, args=(index,)) for index in range(8)]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(30)
        self.assertEqual(results, [(True, False)] * 8)
        self.assertEqual(len(seen), 24)                               # a hash and two checks for each of eight callers
        self.assertEqual({thread.name for thread in seen}, {'password-worker'})
        self.assertEqual(len({thread.ident for thread in seen}), 1)
        self.assertNotIn(threading.current_thread(), seen)
        self.assertFalse(set(seen) & set(callers))

    def test_results_and_errors_are_what_they_were(self):
        verifier = http_auth.hash_password('correct horse battery', n=2 ** 10)
        self.assertRegex(verifier, r'^scrypt\$1024\$8\$1\$[0-9a-f]{32}\$[0-9a-f]{64}$')
        self.assertTrue(http_auth.verify_password(verifier, 'correct horse battery'))
        self.assertFalse(http_auth.verify_password(verifier, 'correct horse batterz'))
        # A verifier whose parameters scrypt refuses is "no match", as before: the error of the
        # computation is raised in the caller and handled there.
        scheme, n, r, p, salt, digest = verifier.split('$')
        for broken in ('$'.join((scheme, '1000', r, p, salt, digest)),      # n is not a power of two
                       '$'.join((scheme, n, '0', p, salt, digest)),
                       '$'.join(('md5', n, r, p, salt, digest)), 'nonsense', None):
            with self.subTest(verifier=broken):
                self.assertFalse(http_auth.verify_password(broken, 'correct horse battery'))
        for error in (MemoryError(), ValueError('x'), OverflowError('x')):
            with self.subTest(error=type(error).__name__), mock.patch.object(hashlib, 'scrypt', side_effect=error):
                self.assertFalse(http_auth.verify_password(verifier, 'correct horse battery'))
        # An error the caller does not handle reaches the caller, not the worker's void.
        with mock.patch.object(hashlib, 'scrypt', side_effect=RuntimeError('broken')), self.assertRaises(RuntimeError):
            http_auth.verify_password(verifier, 'correct horse battery')
        self.assertTrue(http_auth.verify_password(verifier, 'correct horse battery'))      # and the worker lives on

    def test_a_worker_that_is_gone_is_replaced(self):
        worker = http_auth._PasswordWorker()
        self.assertEqual(len(worker.scrypt(b'p', salt=b's' * 16, n=2 ** 10, r=8, p=1, dklen=32)), 32)
        first = worker._thread
        with mock.patch.object(http_auth.os, 'getpid', return_value=first.ident + 1):      # as after a fork
            worker.scrypt(b'p', salt=b's' * 16, n=2 ** 10, r=8, p=1, dklen=32)
            self.assertIsNot(worker._thread, first)
            self.assertEqual(worker._thread.name, 'password-worker')

    def test_the_module_has_no_other_way_to_scrypt(self):
        source = (KIT / 'http_auth.py').read_text(encoding='utf-8')
        self.assertEqual(source.count('hashlib.scrypt('), 1)
        self.assertEqual(source.count('_PASSWORD_WORKER.scrypt('), 2)


class BoundTests(test_http_agents.AgentHarness):
    def held(self, limit):
        """Log-ins that stay in flight: the worker does not answer until `release` is set."""
        release, entered = threading.Event(), []
        real = http_auth._PASSWORD_WORKER.scrypt

        def waiting(password, **parameters):
            entered.append(password)
            release.wait(30)
            return real(password, **parameters)
        patcher = mock.patch.object(http_auth._PASSWORD_WORKER, 'scrypt', waiting)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(release.set)
        self.service.LOGINS_AT_ONCE = limit
        answers = []

        def attempt(index):
            try:
                self.service.login('nobody-%d' % index, 'wrong password %d' % index)
            except HttpError as error:
                answers.append(error.status)
        threads = [threading.Thread(target=attempt, args=(index,)) for index in range(limit)]
        for thread in threads:
            thread.start()
        until = time.monotonic() + 10
        while time.monotonic() < until and (self.service._logins < limit or not entered):
            time.sleep(0.01)
        self.assertEqual(self.service._logins, limit)
        return release, entered, threads, answers

    def test_one_more_log_in_than_the_bound_is_answered_busy_before_anything_is_checked(self):
        release, entered, threads, answers = self.held(3)
        self.assertEqual(len(entered), 1)                             # one is being checked; two wait their turn
        failures = dict(self.service._failures)
        audited = len(self.service.state.get('audit') or [])
        for name, password in ((ADMIN, PASSWORD), (ADMIN, 'a wrong password'), ('nobody-at-all', 'x' * 12)):
            with self.subTest(name=name), self.assertRaises(HttpError) as refused:
                self.service.login(name, password)
            error = refused.exception
            self.assertEqual((error.status, error.code, error.message, error.retry_after),
                             (503, 'busy', 'Too many people are logging in at this moment. Try again in a few seconds.', 5))
        self.assertEqual(len(entered), 1)                             # nothing was checked for them
        self.assertEqual(self.service._failures, failures)            # and nothing counted against a name
        self.assertEqual(len(self.service.state.get('audit') or []), audited)
        self.assertEqual((self.service.logins_turned_away, self.service._logins), (3, 3))
        release.set()
        for thread in threads:
            thread.join(30)
        self.assertEqual(answers, [401, 401, 401])
        self.assertEqual(self.service._logins, 0)                     # the places are free again
        self.assertEqual(self.service.login(ADMIN, PASSWORD)['user']['username'], ADMIN)

    def test_over_http_it_is_503_busy_with_retry_after_and_the_same_for_any_name(self):
        release, entered, threads, answers = self.held(2)
        bodies = []
        for name, password in ((ADMIN, PASSWORD), ('nobody-at-all', 'x' * 12)):
            answer = self.request('POST', '/v1/sessions', {'username': name, 'password': password})
            self.assertEqual((answer.status, answer.headers.get('retry-after')), (503, '5'))
            bodies.append({k: v for k, v in answer.data['error'].items()})
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[0], {'code': 'busy',
                                     'message': 'Too many people are logging in at this moment. Try again in a few seconds.'})
        # Somebody who is logged in is not turned away: the bound is on log-ins.
        release.set()
        for thread in threads:
            thread.join(30)
        token = self.admin_token()
        self.assertEqual(200, self.request('GET', '/v1/sessions/current', token=token).status)

    def test_a_log_in_that_ends_in_any_way_frees_its_place(self):
        self.service.LOGINS_AT_ONCE = 1
        for _ in range(3):
            with self.assertRaises(HttpError) as refused:
                self.service.login(ADMIN, 'a wrong password')
            self.assertEqual(refused.exception.status, 401)
        with mock.patch.object(self.service, '_check_throttle', side_effect=http_auth.throttled()), \
                self.assertRaises(HttpError) as limited:
            self.service.login(ADMIN, PASSWORD)
        self.assertEqual(limited.exception.status, 429)
        with mock.patch.object(self.service, '_login', side_effect=RuntimeError('broken')), self.assertRaises(RuntimeError):
            self.service.login(ADMIN, PASSWORD)
        self.assertEqual(self.service._logins, 0)
        self.assertEqual(self.service.login(ADMIN, PASSWORD)['user']['username'], ADMIN)
        self.assertEqual(self.service.logins_turned_away, 0)

    def test_the_log_in_page_shows_the_servers_sentence_for_busy(self):
        """Any other 5xx reads "could not confirm whether this was saved", which is not true of a log-in turned away."""
        source = (KIT / 'web' / 'js' / 'views' / 'auth.js').read_text(encoding='utf-8')
        self.assertIn("const busy = error.status === 503 && error.code === 'busy' && error.message;", source)
        self.assertIn(": (busy || describe(error));", source)

    def test_the_bound_is_sixteen(self):
        self.assertEqual(http_auth.Service.LOGINS_AT_ONCE, 16)


if __name__ == '__main__':
    unittest.main()
