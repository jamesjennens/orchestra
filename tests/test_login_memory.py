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

    def test_a_worker_whose_thread_has_died_is_replaced(self):
        """The other half of "gone" (a mutant of the review): the same process, the thread no longer alive."""
        worker = http_auth._PasswordWorker()
        worker.scrypt(b'p', salt=b's' * 16, n=2 ** 10, r=8, p=1, dklen=32)
        dead = threading.Thread(target=lambda: None, name='password-worker')
        dead.start()
        dead.join()
        worker._thread = dead                                         # nobody reads the old queue any more
        import queue
        worker._jobs = queue.Queue()
        results = []
        caller = threading.Thread(target=lambda: results.append(
            worker.scrypt(b'p', salt=b's' * 16, n=2 ** 10, r=8, p=1, dklen=32)), daemon=True)
        caller.start()
        caller.join(10)
        self.assertFalse(caller.is_alive(), 'the computation was handed to a worker that is gone')
        self.assertEqual(len(results[0]), 32)
        self.assertIsNot(worker._thread, dead)
        self.assertTrue(worker._thread.is_alive())

    def test_the_module_has_no_other_way_to_scrypt(self):
        source = (KIT / 'http_auth.py').read_text(encoding='utf-8')
        self.assertEqual(source.count('hashlib.scrypt('), 1)
        self.assertEqual(source.count('_PASSWORD_WORKER.scrypt('), 2)


class Held(test_http_agents.AgentHarness):
    def setUp(self):
        super().setUp()
        self.service.LOGIN_WAIT_SECONDS = 0          # a refusal is at once here; the wait for a place is WaitTests

    def held(self, limit, source='local', count=None):
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
                self.service.login('nobody-%d' % index, 'wrong password %d' % index, source=source)
            except HttpError as error:
                answers.append(error.status)
        count = limit if count is None else count
        threads = [threading.Thread(target=attempt, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        until = time.monotonic() + 10
        while time.monotonic() < until and (self.service._logins < count or not entered):
            time.sleep(0.01)
        self.assertEqual(self.service._logins, count)
        return release, entered, threads, answers


class BoundTests(Held):
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

    def test_a_log_in_that_is_turned_away_leaves_the_wrong_password_count_as_it_was(self):
        """It neither counts against the name nor clears what was counted (a mutant of the review)."""
        key = self.service._throttle_key(ADMIN, 'local')
        for _ in range(2):
            with self.assertRaises(HttpError):
                self.service.login(ADMIN, 'a wrong password')
        self.assertEqual(len(self.service._failures[key]), 2)
        release, entered, threads, answers = self.held(2)
        for password in (PASSWORD, 'a wrong password'):
            with self.assertRaises(HttpError) as refused:
                self.service.login(ADMIN, password)
            self.assertEqual(refused.exception.status, 503)
        self.assertEqual(len(self.service._failures[key]), 2)
        release.set()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(self.service._failures[key]), 2)
        # The lock-out comes at the same attempt as without the turned-away ones.
        for _ in range(self.service.login_max_attempts - 2):
            with self.assertRaises(HttpError) as refused:
                self.service.login(ADMIN, 'a wrong password')
            self.assertEqual(refused.exception.status, 401)
        with self.assertRaises(HttpError) as locked:
            self.service.login(ADMIN, PASSWORD)
        self.assertEqual(locked.exception.status, 429)


class ShareTests(Held):
    """Review of kittrial-5bb.170: about 20 looping connections from one address, with no credentials, held all
    16 places and kept every log-in out. One address now has a share of the places."""
    ONE, OTHER = '192.0.2.7', '192.0.2.8'

    def share(self, places, per_address):
        self.service.LOGINS_PER_ADDRESS = per_address
        release, entered, threads, answers = self.held(places, source=self.ONE, count=per_address or places)
        return release, entered, threads, answers

    def test_the_share_is_a_quarter_of_the_places(self):
        self.assertEqual((http_auth.Service.LOGINS_PER_ADDRESS, http_auth.Service.LOGINS_AT_ONCE), (4, 16))
        self.assertEqual((http_auth.Service.LOGIN_WAIT_SECONDS, http_auth.Service.LOGIN_WAITERS_PER_ADDRESS), (2.0, 12))

    def test_a_shared_source_has_no_share_and_the_places_in_all_still_bound_it(self):
        """Everybody behind a trusted proxy that forwards no address (the SSH tunnel): not one client."""
        self.service.LOGINS_PER_ADDRESS = 2
        self.service.LOGINS_AT_ONCE = 5
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
        answers = []

        def attempt(index):
            try:
                self.service.login('nobody-%d' % index, 'wrong password %d' % index, source='127.0.0.1', shared_source=True)
            except HttpError as error:
                answers.append(error.status)
        threads = [threading.Thread(target=attempt, args=(index,)) for index in range(5)]
        for thread in threads:
            thread.start()
        until = time.monotonic() + 10
        while time.monotonic() < until and self.service._logins < 5:
            time.sleep(0.01)
        self.assertEqual((self.service._logins, self.service._logins_by_address), (5, {}))    # five, where the share is two
        with self.assertRaises(HttpError) as refused:                 # the sixth meets the places in all
            self.service.login(ADMIN, PASSWORD, source='127.0.0.1', shared_source=True)
        self.assertEqual(refused.exception.message, self.service.LOGIN_BUSY)
        self.assertEqual((self.service.logins_turned_away, self.service.logins_turned_away_for_address), (1, 0))
        release.set()
        for thread in threads:
            thread.join(30)
        self.assertEqual((answers, self.service._logins, self.service._logins_by_address), ([401] * 5, 0, {}))
        self.assertEqual(self.service.login(ADMIN, PASSWORD, source='127.0.0.1', shared_source=True)['user']['username'], ADMIN)

    def test_one_address_cannot_take_every_place_and_another_address_gets_in(self):
        release, entered, threads, answers = self.share(6, 2)
        failures = dict(self.service._failures)
        audited = len(self.service.state.get('audit') or [])
        for name, password in ((ADMIN, PASSWORD), ('nobody-at-all', 'x' * 12)):
            with self.subTest(name=name), self.assertRaises(HttpError) as refused:
                self.service.login(name, password, source=self.ONE)
            error = refused.exception
            self.assertEqual((error.status, error.code, error.message, error.retry_after),
                             (503, 'busy', 'Too many log-ins from your address are being checked at this moment. '
                                           'Try again in a few seconds.', 5))
        self.assertEqual(len(entered), 1)                             # nothing was checked for them
        self.assertEqual(self.service._failures, failures)
        self.assertEqual(len(self.service.state.get('audit') or []), audited)
        self.assertEqual((self.service.logins_turned_away, self.service.logins_turned_away_for_address), (2, 2))
        # Somebody at another address has a place, and gets in when the checks before theirs are done.
        got = []
        person = threading.Thread(target=lambda: got.append(self.service.login(ADMIN, PASSWORD, source=self.OTHER)))
        person.start()
        until = time.monotonic() + 10
        while time.monotonic() < until and self.service._logins < 3:
            time.sleep(0.01)
        self.assertEqual((self.service._logins, self.service._logins_by_address), (3, {self.ONE: 2, self.OTHER: 1}))
        release.set()
        person.join(30)
        for thread in threads:
            thread.join(30)
        self.assertEqual(got[0]['user']['username'], ADMIN)
        self.assertEqual(answers, [401, 401])
        self.assertEqual((self.service._logins, self.service._logins_by_address), (0, {}))
        self.assertEqual(self.service.login(ADMIN, PASSWORD, source=self.ONE)['user']['username'], ADMIN)
        self.assertEqual(self.service._logins_by_address, {})

    def test_when_every_place_is_taken_the_sentence_is_the_general_one(self):
        self.service.LOGINS_PER_ADDRESS = 2
        release, entered, threads, answers = self.held(2, source=self.ONE)
        with self.assertRaises(HttpError) as refused:
            self.service.login(ADMIN, PASSWORD, source=self.OTHER)
        self.assertEqual(refused.exception.message, 'Too many people are logging in at this moment. Try again in a few seconds.')
        self.assertEqual((self.service.logins_turned_away, self.service.logins_turned_away_for_address), (1, 0))
        with self.assertRaises(HttpError) as refused:                 # at its share AND everything taken: its own share is said
            self.service.login(ADMIN, PASSWORD, source=self.ONE)
        self.assertIn('from your address', refused.exception.message)

    def test_the_addresses_of_one_ipv6_64_are_one_address_and_a_mapped_one_is_its_ipv4(self):
        self.service.LOGINS_PER_ADDRESS = 2
        release, entered, threads, answers = self.held(8, source='2001:db8:1:2::1', count=2)
        self.assertEqual(self.service._logins_by_address, {'2001:db8:1:2::/64': 2})
        with self.assertRaises(HttpError) as refused:
            self.service.login(ADMIN, PASSWORD, source='2001:db8:1:2:ffff::9')
        self.assertIn('from your address', refused.exception.message)
        release.set()
        for thread in threads:
            thread.join(30)
        release, entered, threads, answers = self.held(8, source='::ffff:192.0.2.7', count=2)
        self.assertEqual(self.service._logins_by_address, {'192.0.2.7': 2})
        with self.assertRaises(HttpError) as refused:
            self.service.login(ADMIN, PASSWORD, source='192.0.2.7')
        self.assertIn('from your address', refused.exception.message)

    def test_no_share_per_address_when_it_is_set_to_nought(self):
        release, entered, threads, answers = self.share(5, 0)
        self.assertEqual((self.service._logins, self.service._logins_by_address), (5, {self.ONE: 5}))
        with self.assertRaises(HttpError) as refused:
            self.service.login(ADMIN, PASSWORD, source=self.ONE)
        self.assertEqual(refused.exception.message, self.service.LOGIN_BUSY)
        self.assertEqual(self.service.logins_turned_away_for_address, 0)

    def test_over_http_the_refusal_is_503_busy_with_retry_after_and_the_same_for_any_name(self):
        """Asked with the service's method as the web route asks it, for a client address (the harness itself
        connects from the service's own host, which has no share: tests/test_address_limit.py has that over HTTP)."""
        self.service.LOGINS_PER_ADDRESS = 1
        release, entered, threads, answers = self.held(4, source=self.ONE, count=1)
        errors = []
        for name, password in ((ADMIN, PASSWORD), ('nobody-at-all', 'x' * 12)):
            with self.assertRaises(HttpError) as refused:
                self.service.login(name, password, source=self.ONE, shared_source=False)
            errors.append((refused.exception.status, refused.exception.code, refused.exception.message, refused.exception.retry_after))
        self.assertEqual(errors[0], errors[1])
        self.assertEqual(errors[0], (503, 'busy', self.service.LOGIN_BUSY_ADDRESS, 5))
        self.assertEqual(self.service.logins_turned_away_for_address, 2)

    def test_over_http_a_log_in_from_the_services_own_host_is_not_held_to_a_share(self):
        self.service.LOGINS_PER_ADDRESS = 1
        with self.service._logins_guard:
            self.service._logins_by_address['127.0.0.1'] = 1          # as if one counted log-in of that address were in flight
        for _ in range(3):
            answer = self.request('POST', '/v1/sessions', {'username': ADMIN, 'password': PASSWORD})
            self.assertEqual(answer.status, 201, answer.data)
        self.assertEqual((self.service.logins_turned_away, self.service._logins_by_address), (0, {'127.0.0.1': 1}))

    def test_a_log_in_that_ends_in_any_way_gives_the_address_its_place_back(self):
        self.service.LOGINS_PER_ADDRESS = 1
        for _ in range(3):
            with self.assertRaises(HttpError) as refused:
                self.service.login(ADMIN, 'a wrong password', source=self.ONE)
            self.assertEqual(refused.exception.status, 401)
        with mock.patch.object(self.service, '_login', side_effect=RuntimeError('broken')), self.assertRaises(RuntimeError):
            self.service.login(ADMIN, PASSWORD, source=self.ONE)
        self.assertEqual((self.service._logins, self.service._logins_by_address), (0, {}))
        self.assertEqual(self.service.login(ADMIN, PASSWORD, source=self.ONE)['user']['username'], ADMIN)
        self.assertEqual(self.service.logins_turned_away, 0)


class WaitTests(Held):
    """A log-in over its address's share waits a short time for one of that address's places (the coordinator's
    decision after the measurements): an office behind one router is one address, and an instant refusal sent six
    of its ten people away at nine in the morning. A flooding address gains nothing by it."""
    ONE, OTHER = '192.0.2.7', '192.0.2.8'

    def parked(self, address, count, within=10):
        until = time.monotonic() + within
        while time.monotonic() < until and self.service._logins_waiting.get(address, 0) != count:
            time.sleep(0.01)
        self.assertEqual(self.service._logins_waiting.get(address, 0), count)

    def test_ten_people_at_one_address_all_get_in_at_the_first_try(self):
        self.service.LOGIN_WAIT_SECONDS = http_auth.Service.LOGIN_WAIT_SECONDS
        self.assertEqual(self.service.LOGINS_PER_ADDRESS, 4)
        barrier, results, waits, most = threading.Barrier(10), [], [], []

        def person():
            barrier.wait(30)
            started = time.monotonic()
            try:
                results.append(self.service.login(ADMIN, PASSWORD, source=self.ONE)['user']['username'])
            except HttpError as error:
                results.append(error.status)
            waits.append(time.monotonic() - started)
        people = [threading.Thread(target=person) for _ in range(10)]
        for one in people:
            one.start()
        while any(one.is_alive() for one in people):
            most.append((self.service._logins_by_address.get(self.ONE, 0), self.service._logins_waiting.get(self.ONE, 0)))
            time.sleep(0.002)
        self.assertEqual(results, [ADMIN] * 10)
        self.assertLessEqual(max(in_flight for in_flight, _ in most), 4)                # the share held throughout
        self.assertLess(max(waits), http_auth.Service.LOGIN_WAIT_SECONDS + 1.5)
        self.assertEqual((self.service.logins_turned_away, self.service._logins, self.service._logins_by_address,
                          self.service._logins_waiting), (0, 0, {}, {}))

    def test_a_waiter_gets_the_place_when_one_of_its_address_is_freed(self):
        self.service.LOGINS_PER_ADDRESS = 2
        release, entered, threads, answers = self.held(6, source=self.ONE, count=2)
        self.service.LOGIN_WAIT_SECONDS = 30
        took = []

        def waiter():
            started = time.monotonic()
            try:
                self.service.login('nobody-waiting', 'a wrong password', source=self.ONE)
            except HttpError as error:
                took.append((error.status, time.monotonic() - started))
        thread = threading.Thread(target=waiter)
        thread.start()
        self.parked(self.ONE, 1)
        self.assertEqual((self.service._logins, self.service._logins_by_address), (2, {self.ONE: 2}))    # it holds no place while it waits
        release.set()
        thread.join(30)
        self.assertEqual(took[0][0], 401)                             # it was checked: it got a place, long before its wait ran out
        self.assertLess(took[0][1], 20)
        for held in threads:
            held.join(30)
        self.assertEqual((self.service._logins, self.service._logins_by_address, self.service._logins_waiting), (0, {}, {}))

    def test_a_waiter_whose_time_runs_out_is_refused_as_before_and_nothing_is_counted_or_cleared(self):
        key = self.service._throttle_key(ADMIN, self.ONE)
        self.service.LOGIN_WAIT_SECONDS = 0
        for _ in range(2):
            with self.assertRaises(HttpError):
                self.service.login(ADMIN, 'a wrong password', source=self.ONE)
        self.assertEqual(len(self.service._failures[key]), 2)
        self.service.LOGINS_PER_ADDRESS = 2
        release, entered, threads, answers = self.held(6, source=self.ONE, count=2)
        self.service.LOGIN_WAIT_SECONDS = 0.4
        audited = len(self.service.state.get('audit') or [])
        for password in (PASSWORD, 'a wrong password'):
            started = time.monotonic()
            with self.assertRaises(HttpError) as refused:
                self.service.login(ADMIN, password, source=self.ONE)
            waited = time.monotonic() - started
            error = refused.exception
            self.assertEqual((error.status, error.code, error.message, error.retry_after), (503, 'busy', self.service.LOGIN_BUSY_ADDRESS, 5))
            self.assertGreaterEqual(waited, 0.35)                      # it did wait
            self.assertLess(waited, 5)
        self.assertEqual(len(entered), 1)                              # nothing was checked for them
        self.assertEqual(len(self.service._failures[key]), 2)          # neither counted nor cleared
        self.assertEqual(len(self.service.state.get('audit') or []), audited)
        self.assertEqual((self.service.logins_turned_away, self.service.logins_turned_away_for_address, self.service._logins_waiting),
                         (2, 2, {}))
        release.set()
        for thread in threads:
            thread.join(30)

    def test_only_so_many_of_one_address_wait_and_one_more_is_refused_at_once(self):
        self.service.LOGINS_PER_ADDRESS = 2
        self.service.LOGIN_WAITERS_PER_ADDRESS = 3
        release, entered, threads, answers = self.held(8, source=self.ONE, count=2)
        self.service.LOGIN_WAIT_SECONDS = 30
        done = []

        def waiter(index):
            try:
                self.service.login('nobody-w%d' % index, 'a wrong password', source=self.ONE)
            except HttpError as error:
                done.append(error.status)
        waiters = [threading.Thread(target=waiter, args=(index,)) for index in range(3)]
        for thread in waiters:
            thread.start()
        self.parked(self.ONE, 3)
        started = time.monotonic()
        with self.assertRaises(HttpError) as refused:
            self.service.login(ADMIN, PASSWORD, source=self.ONE)
        self.assertLess(time.monotonic() - started, 2)                 # at once, not after the 30 s
        self.assertEqual(refused.exception.message, self.service.LOGIN_BUSY_ADDRESS)
        self.assertEqual(self.service._logins_waiting, {self.ONE: 3})
        release.set()
        for thread in waiters + threads:
            thread.join(30)
        self.assertEqual((sorted(done), self.service._logins_waiting, self.service._logins), ([401, 401, 401], {}, 0))

    def test_the_waiters_of_one_address_take_nothing_from_another(self):
        self.service.LOGINS_PER_ADDRESS = 2
        self.service.LOGINS_AT_ONCE = 4
        release, entered, threads, answers = self.held(4, source=self.ONE, count=2)
        self.service.LOGIN_WAIT_SECONDS = 30
        waiters = [threading.Thread(target=lambda: self.assertRaises(HttpError, self.service.login, 'nobody', 'x' * 12, source=self.ONE))
                   for _ in range(5)]
        for thread in waiters:
            thread.start()
        self.parked(self.ONE, 5)
        # Five wait; the other address has its two places, and they are places, not waits.
        others = [threading.Thread(target=lambda: self.assertRaises(HttpError, self.service.login, 'nobody', 'x' * 12, source=self.OTHER))
                  for _ in range(2)]
        for thread in others:
            thread.start()
        until = time.monotonic() + 10
        while time.monotonic() < until and self.service._logins < 4:
            time.sleep(0.01)
        self.assertEqual((self.service._logins, self.service._logins_by_address, self.service._logins_waiting),
                         (4, {self.ONE: 2, self.OTHER: 2}, {self.ONE: 5}))
        release.set()
        for thread in waiters + others + threads:
            thread.join(60)
        self.assertEqual((self.service._logins, self.service._logins_by_address, self.service._logins_waiting), (0, {}, {}))

    def test_a_shared_source_and_the_places_in_all_do_not_wait(self):
        self.service.LOGINS_AT_ONCE = 2
        release, entered, threads, answers = self.held(2, source=self.ONE)
        self.service.LOGIN_WAIT_SECONDS = 30
        for kwargs in ({'source': self.OTHER}, {'source': '127.0.0.1', 'shared_source': True}):
            started = time.monotonic()
            with self.assertRaises(HttpError) as refused:
                self.service.login(ADMIN, PASSWORD, **kwargs)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(refused.exception.message, self.service.LOGIN_BUSY)       # every place taken: at once, as before
        self.assertEqual(self.service._logins_waiting, {})
        release.set()
        for thread in threads:
            thread.join(30)


if __name__ == '__main__':
    unittest.main()
