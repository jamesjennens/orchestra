"""Sign-in hardening (kittrial-5bb.501, from the audit kittrial-5bb.499).

Every test here runs the real service on a real store, and the ones that name HTTP send real
requests to the real server on 127.0.0.1 (a trusted proxy on loopback forwarding
``X-Forwarded-For``, the documented deployment). Time is a clock the test moves; nothing reads
the real clock for a date.

F1  an invited account costs the same one scrypt as a missing account and a wrong password
F2  a password change deletes this account's pending reset values
F3  per-account and per-source counts beside the pair, keyed by the source group
F4  password change shares the counts
    and the installation's password minimum is 12 unless the operator sets another.
"""
import http.client
import io
import json
import secrets
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_auth
import http_service
from http_auth import HttpError, Service, Store

ADMIN = 'root-admin'
ADMIN_PASSWORD = 'correct-horse-battery-staple'


class Clock:
    def __init__(self, start=1_700_000_000.0):
        self.t = start
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.t

    def advance(self, seconds):
        with self._lock:
            self.t += seconds


def cheap_dummy():
    """The dummy verifier of a missing account at a toy cost, for tests that sign in with thousands of
    made-up names. The cost is a property of the verifier, so the code path is the real one."""
    return mock.patch.object(http_auth, '_DUMMY_VERIFIER',
                             http_auth.hash_password('orchestra-dummy-password', n=2 ** 4))


class ServerCase(unittest.TestCase):
    """The real service behind the real HTTP server, a trusted proxy on loopback, a clock the test moves."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock = Clock()
        self.store = Store(Path(tmp.name) / 'state.json', clock=self.clock)
        Service.bootstrap_superuser(self.store, ADMIN, ADMIN_PASSWORD)
        self.service = Service(self.store)
        self.httpd = http_service.create_server(
            self.service, http_service.InProcessBackend(self.service), host='127.0.0.1', port=0,
            web_root=None, trusted_proxies=('127.0.0.1/32',))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.httpd.server_address[1]
        self.counter = 0
        self.admin = self.session(ADMIN, ADMIN_PASSWORD, '192.0.2.1')

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None, key=False):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=120)
        headers = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body)
            headers['Content-Type'] = 'application/json'
        if key:
            headers['Idempotency-Key'] = 'k' + secrets.token_hex(8)
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None), dict(response.getheaders())
        finally:
            connection.close()

    def attempt(self, username, password, source=None):
        """One sign-in over HTTP; ``source`` is the address the trusted proxy forwards (None: it forwards none)."""
        headers = {'X-Forwarded-For': source} if source else {}
        return self.request('POST', '/v1/sessions', {'username': username, 'password': password}, headers)

    def session(self, username, password, source=None):
        status, body, _ = self.attempt(username, password, source)
        self.assertEqual(status, 201, body)
        token = body['session']['token']
        return {'bearer': {'Authorization': 'Bearer ' + token}, 'user': body['user']}

    def make_account(self, username, password=None):
        status, body, _ = self.request('POST', '/v1/accounts', {'username': username}, self.admin['bearer'])
        self.assertEqual(status, 201, body)
        user_id = body['id']
        if password:
            status, issued, _ = self.request('POST', '/v1/accounts/%s/reset' % user_id, {},
                                             self.admin['bearer'], key=True)
            self.assertEqual(status, 201, issued)
            status, body, _ = self.request('POST', '/v1/accounts/%s/reset/redeem' % user_id,
                                           {'reset_value': issued['reset_value'], 'new_password': password})
            self.assertEqual(status, 200, body)
        return user_id

    def address(self):
        """A fresh IPv4 address from the documentation ranges, for each call."""
        self.counter += 1
        return '10.%d.%d.%d' % (self.counter // 62500 % 250, self.counter // 250 % 250, self.counter % 250 + 1)

    def change_password(self, headers, user_id, current, new, source=None):
        headers = dict(headers, **({'X-Forwarded-For': source} if source else {}))
        body = {'new_password': new}
        if current is not None:
            body['current_password'] = current
        return self.request('POST', '/v1/accounts/%s/password' % user_id, body, headers)


class InvitedAccountTimingTests(ServerCase):
    def test_a_failed_sign_in_costs_one_scrypt_for_a_missing_an_invited_and_an_active_account(self):
        """F1. Counted calls, not seconds: the work done is the same, so the time cannot tell them apart."""
        self.make_account('ivy')                                       # invited: no password yet
        self.make_account('dan', 'dan-pass-1234')                      # active
        worker = http_auth._PASSWORD_WORKER
        real = worker.scrypt
        calls = []

        def counted(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)
        http_auth._DUMMY_VERIFIER = None                               # build the dummy before counting
        http_auth._dummy_verifier()
        with mock.patch.object(worker, 'scrypt', counted):
            for username, password in (('nobody-here', 'a password'), ('ivy', 'a password'), ('dan', 'wrong password!')):
                with self.subTest(account=username):
                    del calls[:]
                    status, body, _ = self.attempt(username, password, self.address())
                    self.assertEqual((status, body['error']['message']), (401, 'Invalid credentials'))
                    self.assertEqual(len(calls), 1)

    def test_an_invited_account_still_cannot_sign_in_with_anything(self):
        self.make_account('ivy')
        for password in ('', 'orchestra-dummy-password', 'ivy'):
            self.assertEqual(self.attempt('ivy', password, self.address())[0], 401)


class ResetDeletionTests(ServerCase):
    def issue(self, user_id):
        status, issued, _ = self.request('POST', '/v1/accounts/%s/reset' % user_id, {}, self.admin['bearer'], key=True)
        self.assertEqual(status, 201, issued)
        return issued['reset_value']

    def redeem(self, user_id, value, new):
        return self.request('POST', '/v1/accounts/%s/reset/redeem' % user_id,
                            {'reset_value': value, 'new_password': new})

    def test_a_pending_reset_value_does_not_survive_the_users_own_password_change(self):
        """F2, own change."""
        dan = self.make_account('dan', 'dan-pass-1234')
        session = self.session('dan', 'dan-pass-1234', self.address())
        old = self.issue(dan)
        status, body, _ = self.change_password(session['bearer'], dan, 'dan-pass-1234', 'dan-pass-5678')
        self.assertEqual(status, 200, body)
        status, body = self.redeem(dan, old, 'attacker-pass-9999')[:2]
        self.assertEqual(status, 401, body)
        self.assertEqual(body['error']['message'], 'Invalid or expired reset value')
        self.assertEqual(self.attempt('dan', 'attacker-pass-9999', self.address())[0], 401)
        self.assertEqual(self.attempt('dan', 'dan-pass-5678', self.address())[0], 201)

    def test_a_pending_reset_value_does_not_survive_a_superusers_password_change(self):
        """F2, a superuser sets the password."""
        dan = self.make_account('dan', 'dan-pass-1234')
        old = self.issue(dan)
        status, body, _ = self.change_password(self.admin['bearer'], dan, None, 'set-by-the-admin')
        self.assertEqual(status, 200, body)
        status, body = self.redeem(dan, old, 'attacker-pass-9999')[:2]
        self.assertEqual(status, 401, body)
        self.assertEqual(body['error']['message'], 'Invalid or expired reset value')
        self.assertEqual(self.attempt('dan', 'set-by-the-admin', self.address())[0], 201)

    def test_a_password_change_leaves_another_accounts_pending_value_alone(self):
        dan, eve = self.make_account('dan', 'dan-pass-1234'), self.make_account('eve', 'eve-pass-1234')
        eves = self.issue(eve)
        self.assertEqual(self.change_password(self.admin['bearer'], dan, None, 'set-by-the-admin')[0], 200)
        self.assertEqual(self.redeem(eve, eves, 'eve-pass-5678')[0], 200)


class PairKeyTests(ServerCase):
    def test_sixty_addresses_in_one_ipv6_64_are_one_source_and_one_pair(self):
        """F3: the pair is keyed by the address group, so a /64 is one client (the control: one address)."""
        self.make_account('bob', 'bob-pass-1234')
        control = [self.attempt('bob', 'guess %d' % i, '198.51.100.7')[0] for i in range(12)]
        self.assertEqual(control, [401] * 10 + [429] * 2)
        codes = [self.attempt('bob', 'guess %d' % i, '2001:db8:1:2::%x' % (i + 1))[0] for i in range(60)]
        self.assertEqual(codes, [401] * 10 + [429] * 50)
        self.assertEqual(self.attempt('bob', 'bob-pass-1234', '2001:db8:1:2::ffff')[0], 429)
        self.assertEqual(self.attempt('bob', 'bob-pass-1234', '2001:db8:1:3::1')[0], 201)     # another /64


class AggregateCountTests(ServerCase):
    def test_a_hundred_addresses_against_one_account_are_slowed_and_a_known_source_still_gets_in(self):
        """F3 per account: 30 failures from anywhere, then one checked attempt per 20 s, with Retry-After."""
        self.make_account('bob', 'bob-pass-1234')
        self.assertEqual(self.attempt('bob', 'bob-pass-1234', '192.0.2.50')[0], 201)         # 192.0.2.50 is now bob's usual place
        codes = [self.attempt('bob', 'guess %d' % i, self.address()) for i in range(100)]
        statuses = [code[0] for code in codes]
        self.assertEqual(statuses, [401] * 31 + [429] * 69)           # 30, then the one attempt the slot allows
        self.assertEqual(codes[40][2].get('Retry-After'), '20')
        self.assertIn('Try again in 20 seconds', codes[40][1]['error']['message'])
        # The user, from the usual place, is not affected; a stranger with the right password waits for a slot.
        self.assertEqual(self.attempt('bob', 'bob-pass-1234', '192.0.2.50')[0], 201)
        self.assertEqual(self.attempt('bob', 'bob-pass-1234', self.address())[0], 429)
        self.clock.advance(20)
        self.assertEqual(self.attempt('bob', 'bob-pass-1234', self.address())[0], 201)       # one checked attempt: it is let in

    def test_one_address_spraying_many_names_is_slowed_after_a_hundred(self):
        """F3 per source: 100 failures from one source group, then one checked attempt per 5 s."""
        with cheap_dummy():
            codes = [self.attempt('user%03d' % i, 'Password1234', '198.51.100.99') for i in range(120)]
        self.assertEqual([code[0] for code in codes], [401] * 101 + [429] * 19)
        self.assertEqual(codes[110][2].get('Retry-After'), '5')
        self.clock.advance(5)
        with cheap_dummy():
            self.assertEqual(self.attempt('user200', 'Password1234', '198.51.100.99')[0], 401)   # a checked attempt
            self.assertEqual(self.attempt('user201', 'Password1234', '198.51.100.99')[0], 429)
        self.assertEqual(self.attempt(ADMIN, ADMIN_PASSWORD, '203.0.113.5')[0], 201)         # another source is untouched

    def test_tunnel_users_sign_in_while_the_shared_source_is_over_its_limit(self):
        """F3, the shared case: everybody on 127.0.0.1 is one source, limit 300, and known users skip it."""
        names = ['person%02d' % i for i in range(30)]
        for name in names:
            self.make_account(name, name + '-pass-1234')
            self.session(name, name + '-pass-1234')                                           # signed in through the tunnel before
        with cheap_dummy():
            for i in range(self.service_shared_limit() + 5):
                self.attempt('stranger%03d' % i, 'Password1234')
            late = self.attempt('stranger-late', 'Password1234')
            self.assertEqual((late[0], late[2].get('Retry-After')), (429, '5'))                # the tunnel is slowed
        for name in names:
            with self.subTest(person=name):
                self.assertEqual(self.attempt(name, name + '-pass-1234')[0], 201)             # known: not affected
        self.assertEqual(self.attempt('newcomer', 'Password1234')[0], 429)

    def service_shared_limit(self):
        self.assertEqual(http_auth.SHARED_SOURCE_FAIL_LIMIT, 300)
        self.assertEqual(http_auth.SOURCE_FAIL_LIMIT, 100)
        return http_auth.SHARED_SOURCE_FAIL_LIMIT

    def test_a_success_clears_only_its_pair_so_an_attackers_own_account_cannot_reset_the_counts(self):
        """Verified item 1: fail against other names, succeed on your own: the source count is unchanged."""
        self.make_account('mallory', 'mallory-pass-1234')
        source = '198.51.100.77'
        with cheap_dummy():
            for i in range(60):
                self.attempt('victim%02d' % i, 'Password1234', source)
        self.assertEqual(self.attempt('mallory', 'wrong password!', source)[0], 401)
        before = (len(self.service._failures_source.get(source, ())), len(self.service._failures_account.get('mallory', ())),
                  len(self.service._failures.get('mallory|' + source, ())))
        self.assertEqual(before, (61, 1, 1))
        self.assertEqual(self.attempt('mallory', 'mallory-pass-1234', source)[0], 201)
        self.assertEqual((len(self.service._failures_source.get(source, ())),
                          len(self.service._failures_account.get('mallory', ()))), (61, 1))                                                             # aggregates stand
        self.assertNotIn('mallory|' + source, self.service._failures)                         # the pair is cleared, as before

    def test_failures_from_a_known_source_still_count_toward_the_aggregates(self):
        """Verified item 2: a known source skips the aggregate limits for the account, not the counting."""
        self.make_account('bob', 'bob-pass-1234')
        usual = '192.0.2.50'
        self.assertEqual(self.attempt('bob', 'bob-pass-1234', usual)[0], 201)
        for i in range(6):                                                                     # under the pair limit of 10
            self.assertEqual(self.attempt('bob', 'typo %d' % i, usual)[0], 401)
        self.assertEqual(len(self.service._failures_account.get('bob', ())), 6)
        self.assertEqual(len(self.service._failures_source.get(usual, ())), 6)
        self.assertEqual(len(self.service._failures['bob|' + usual]), 6)
        # They add to what a stranger meets: 24 more from elsewhere reach 30, and the stranger is slowed.
        for i in range(24):
            self.assertEqual(self.attempt('bob', 'guess %d' % i, self.address())[0], 401)
        self.assertEqual(self.attempt('bob', 'guess last', self.address())[0], 401)           # the one slot attempt
        self.assertEqual(self.attempt('bob', 'guess more', self.address())[0], 429)

    def test_the_slow_mode_slot_is_taken_atomically_and_a_busy_refusal_takes_none(self):
        """Verified item 3: with the count over its limit, simultaneous attempts share one slot."""
        self.make_account('bob', 'bob-pass-1234')
        for i in range(http_auth.ACCOUNT_FAIL_LIMIT):
            self.service._record_failure('bob', self.address())
        # A busy refusal (nothing checked) takes no slot.
        self.service.LOGINS_AT_ONCE = 0
        for _ in range(3):
            with self.assertRaises(HttpError) as refused:
                self.service.login('bob', 'guess', source=self.address())
            self.assertEqual(refused.exception.status, 503)
        self.assertEqual(self.service._slot_account, {})
        self.service.LOGINS_AT_ONCE = 16
        # Sixteen at once, from sixteen sources, with the slot free: exactly one is let through.
        outcomes, gate = [], threading.Barrier(16)
        sources = [self.address() for _ in range(16)]

        def one(source):
            gate.wait(10)
            try:
                self.service._check_throttle('bob', source)
                outcomes.append('through')
            except HttpError as error:
                outcomes.append(error.status)
        threads = [threading.Thread(target=one, args=(source,)) for source in sources]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(outcomes, key=str), [429] * 15 + ['through'])


class KeyCapTests(unittest.TestCase):
    """Every sign-in map is bounded, and the bound cannot be used to wipe a target's count."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock = Clock()
        self.store = Store(Path(tmp.name) / 'state.json', clock=self.clock)
        Service.bootstrap_superuser(self.store, ADMIN, ADMIN_PASSWORD)
        self.service = Service(self.store)

    def wrong(self, name, source):
        """What a wrong-password sign-in does to the counts: the check, then (if let through) the failure.
        The two real methods ``login`` calls, without the scrypt and the audited, synced state write of each
        request, which would make twenty thousand of them take a quarter of an hour."""
        try:
            self.service._check_throttle(name, source)
        except HttpError as refused:
            return refused.status
        self.service._record_failure(name, source)
        return 401

    def maps(self):
        service = self.service
        return (service._failures, service._failures_account, service._failures_source,
                service._slot_account, service._slot_source, service._known_sources)

    def test_twenty_thousand_names_from_twenty_thousand_addresses_leave_every_map_at_the_cap(self):
        for i in range(20000):
            self.assertEqual(self.wrong('name%05d' % i, '10.%d.%d.%d' % (i // 62500, i // 250 % 250, i % 250 + 1)), 401)
        for table in self.maps()[:3]:
            self.assertLessEqual(len(table), http_auth.THROTTLE_KEY_CAP)
        self.assertGreater(len(self.service._failures_account), http_auth.THROTTLE_KEY_CAP * 0.85)    # evicted a tenth at a time
        for table in (self.service._failures, self.service._failures_account, self.service._failures_source):
            for times in table.values():
                self.assertLessEqual(len(times), http_auth.LOGIN_MAX_ATTEMPTS + 1 if table is self.service._failures else 101)

    def test_names_that_each_failed_once_cannot_push_out_a_targets_count(self):
        """Verified item 4. A target at 30 failures is evicted only by CAP other keys holding at least 30 each:
        10,000 x 30 = 300,000 failures in one 15-minute window is 333 a second, where the gate lets through
        about 20 a second (one scrypt of ~47 ms at a time). Fresh names, which hold 1 each, evict one another."""
        for i in range(http_auth.ACCOUNT_FAIL_LIMIT):
            self.service._record_failure('target', '192.0.2.%d' % (i + 1))
        for i in range(2 * http_auth.THROTTLE_KEY_CAP + 50):
            self.assertEqual(self.wrong('spray%05d' % i, '10.%d.%d.%d' % (i // 62500, i // 250 % 250, i % 250 + 1)), 401)
        self.assertLessEqual(len(self.service._failures_account), http_auth.THROTTLE_KEY_CAP)
        self.assertEqual(len(self.service._failures_account.get('target', ())), http_auth.ACCOUNT_FAIL_LIMIT)
        with self.assertRaises(HttpError) as first:                                            # one slot attempt, then slowed
            self.service.login('target', 'guess', source='203.0.113.9')
        self.assertEqual(first.exception.status, 401)
        with self.assertRaises(HttpError) as second:
            self.service.login('target', 'guess', source='203.0.113.10')
        self.assertEqual(second.exception.status, 429)

    def test_stale_keys_are_swept_before_any_key_is_evicted(self):
        for i in range(http_auth.THROTTLE_KEY_CAP - 1):
            self.service._record_failure('old%05d' % i, '10.0.%d.%d' % (i // 250 % 250, i % 250 + 1))
        self.clock.advance(800)
        self.service._record_failure('live', '192.0.2.1')                                      # the table is now full
        self.assertEqual(len(self.service._failures_account), http_auth.THROTTLE_KEY_CAP)
        self.clock.advance(101)                                                                 # the old ones left their window
        self.service._record_failure('new', '192.0.2.2')
        self.assertEqual(sorted(self.service._failures_account), ['live', 'new'])             # the stale went, nothing live did


class PasswordChangeCountTests(ServerCase):
    def test_wrong_current_passwords_feed_the_counts_and_the_eleventh_is_refused(self):
        """F4: guesses of the current password are held to the sign-in counts."""
        bob = self.make_account('bob', 'bob-pass-1234')
        source = '198.51.100.7'
        session = self.session('bob', 'bob-pass-1234', source)
        codes = [self.change_password(session['bearer'], bob, 'guess %d' % i, 'bob-pass-5678', source)[0]
                 for i in range(10)]
        self.assertEqual(codes, [403] * 10)
        status, body, _ = self.change_password(session['bearer'], bob, 'bob-pass-1234', 'bob-pass-5678', source)
        self.assertEqual(status, 429, body)                                                    # even the right one waits
        self.assertEqual(self.attempt('bob', 'bob-pass-1234', source)[0], 429)               # and so does a sign-in from there
        # Another place is not held by that pair, and the session can still change the password from it.
        self.assertEqual(self.change_password(session['bearer'], bob, 'bob-pass-1234', 'bob-pass-5678', '203.0.113.9')[0], 200)

    def test_a_superusers_change_without_a_current_password_is_not_counted_or_throttled(self):
        """Verified item 6: nothing to guess, so no count."""
        bob = self.make_account('bob', 'bob-pass-1234')
        for i in range(12):
            status, body, _ = self.change_password(self.admin['bearer'], bob, None, 'set-by-admin-%02d' % i,
                                                   '198.51.100.7')
            self.assertEqual(status, 200, body)
        self.assertEqual(self.service._failures, {})
        self.assertEqual(self.service._failures_source, {})


class MinimumLengthTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.clock = Clock()
        self.store = Store(self.tmp / 'state.json', clock=self.clock)

    def test_a_default_service_refuses_eleven_characters_and_accepts_twelve(self):
        Service.bootstrap_superuser(self.store, ADMIN, ADMIN_PASSWORD)
        service = Service(self.store)
        self.assertEqual(service.password_min_length, 12)
        admin = service.authenticate(service.login(ADMIN, ADMIN_PASSWORD)['session_token'])
        user = service.create_user(admin, 'alex')
        with self.assertRaises(HttpError) as caught:
            service.change_password(admin, user['id'], None, 'x' * 11)
        self.assertEqual((caught.exception.status, caught.exception.message), (422, 'Password must be 12-1024 characters'))
        self.assertEqual(service.change_password(admin, user['id'], None, 'x' * 12)['changed'], True)
        self.assertTrue(service.login('alex', 'x' * 12)['session_token'])
        # The bootstrap default is the same rule.
        with self.assertRaises(HttpError):
            Service.bootstrap_superuser(Store(self.tmp / 'other.json', clock=self.clock), 'root', 'x' * 11)

    def test_an_installation_that_sets_a_lower_minimum_keeps_it(self):
        Service.bootstrap_superuser(self.store, ADMIN, 'abc', password_min_length=1)
        service = Service(self.store, password_min_length=1)
        self.assertTrue(service.login(ADMIN, 'abc')['session_token'])
        for bad in (0, 1025, True, '12'):
            with self.assertRaises(ValueError):
                Service(self.store, password_min_length=bad)

    def test_a_stored_short_password_still_signs_in_under_the_new_default(self):
        """The rule applies when a password is set, never when one is checked or re-hashed."""
        Service.bootstrap_superuser(self.store, ADMIN, 'abc', password_min_length=1)
        user = next(iter(self.store.state['users'].values()))
        old = http_auth.hash_password('abc', n=2 ** 10)                                        # also a weaker, older hash: rehashed on sign-in
        user['password'] = old
        service = Service(self.store)
        self.assertEqual(service.password_min_length, 12)
        self.assertEqual(service.login(ADMIN, 'abc')['user']['username'], ADMIN)
        self.assertNotEqual(user['password'], old)                                             # re-hashed, without a length check
        self.assertTrue(http_auth.verify_password(user['password'], 'abc'))
        self.assertEqual(service.login(ADMIN, 'abc')['user']['username'], ADMIN)

    def test_the_service_flag_defaults_to_twelve_and_the_advice_names_it(self):
        import argparse
        self.assertEqual(http_service.bootstrap_refusal_advice(HttpError(422, 'invalid', 'Password must be 12-1024 characters')),
                         'Run the command again with a password of 12 to 1024 characters.')
        # The flag's default, read from the real parser by asking for its help.
        out = io.StringIO()
        with mock.patch.object(sys, 'stdout', out), self.assertRaises(SystemExit):
            http_service.main(['--help'])
        text = ' '.join(out.getvalue().split())
        self.assertIn('--password-min-length N the installation password minimum: 1 to 1024 characters (default 12)', text)

    @unittest.skipUnless(sys.platform != 'win32', 'the host bootstrap is POSIX only')
    def test_the_host_bootstrap_prompt_names_the_minimum_and_a_short_password_gets_one_sentence(self):
        import contextlib
        root = self.tmp / 'runtime'
        root.mkdir()
        prompts = []

        def getpass(prompt):
            prompts.append(prompt)
            return 'short-pass'
        stderr = io.StringIO()
        with mock.patch('getpass.getpass', getpass), contextlib.redirect_stderr(stderr), \
                contextlib.redirect_stdout(io.StringIO()):
            code = http_service.main(['--state', str(self.tmp / 'http-state.json'), '--root', str(root),
                                      '--bootstrap-user', 'admin'])
        self.assertEqual(code, 1)
        self.assertEqual(prompts[0], 'New superuser password (12 to 1024 characters): ')
        self.assertIn('Password must be 12-1024 characters', stderr.getvalue())
        self.assertIn('Run the command again with a password of 12 to 1024 characters.', stderr.getvalue())


class SharedPairSlowModeTests(ServerCase):
    """r2, review item a: on the shared source (the SSH tunnel) nobody can lock an account out by name."""

    def test_on_the_tunnel_ten_failures_by_name_no_longer_lock_the_real_user_out(self):
        self.make_account('bob', 'bob-pass-1234')
        self.session('bob', 'bob-pass-1234')                                   # bob signed in through the tunnel before
        codes = [self.attempt('bob', 'guess %d' % i)[0] for i in range(10)]   # an insider on the tunnel, by bob's name
        self.assertEqual(codes, [401] * 10)
        self.assertEqual(self.attempt('bob', 'bob-pass-1234')[0], 201)         # the review's lockout probe: now gets in

    def test_on_the_tunnel_guessing_one_name_stays_bounded_at_one_per_slot(self):
        self.make_account('bob', 'bob-pass-1234')
        for i in range(10):
            self.attempt('bob', 'guess %d' % i)
        checked, refused = 0, []
        for second in range(60):                                               # one guess a second for a minute
            status, _, headers = self.attempt('bob', 'more %d' % second)
            if status == 401:
                checked += 1
            else:
                refused.append((status, headers.get('Retry-After')))
            self.clock.advance(1)
        self.assertEqual(checked, 3)                                           # one per 20 s: about 180 an hour
        self.assertEqual(refused[0], (429, '19'))
        self.assertTrue(all(code == 429 and int(after) >= 1 for code, after in refused))

    def test_the_real_user_gets_the_next_slot_after_the_insiders_guess(self):
        self.make_account('bob', 'bob-pass-1234')
        self.session('bob', 'bob-pass-1234')
        for i in range(11):
            self.attempt('bob', 'guess %d' % i)                                # the eleventh takes the slot
        status, _, headers = self.attempt('bob', 'bob-pass-1234')
        self.assertEqual((status, headers.get('Retry-After')), (429, '20'))
        self.clock.advance(20)
        self.assertEqual(self.attempt('bob', 'bob-pass-1234')[0], 201)

    def test_off_the_tunnel_the_pair_limit_still_refuses_and_now_says_when(self):
        self.make_account('bob', 'bob-pass-1234')
        source = '198.51.100.7'
        for i in range(10):
            self.attempt('bob', 'guess %d' % i, source)
        self.clock.advance(100)
        status, _, headers = self.attempt('bob', 'bob-pass-1234', source)
        self.assertEqual((status, headers.get('Retry-After')), (429, '201'))   # the window is 300 s, inclusive
        self.clock.advance(200)
        self.assertEqual(self.attempt('bob', 'bob-pass-1234', source)[0], 429)  # one second early: still refused
        self.clock.advance(1)
        self.assertEqual(self.attempt('bob', 'bob-pass-1234', source)[0], 201)

    def test_an_ipv4_address_and_its_ipv6_mapped_form_are_one_pair(self):
        self.make_account('bob', 'bob-pass-1234')
        codes = [self.attempt('bob', 'guess %d' % i, '198.51.100.9' if i % 2 else '::ffff:198.51.100.9')[0]
                 for i in range(12)]
        self.assertEqual(codes, [401] * 10 + [429] * 2)


class DisableResetTests(ServerCase):
    """r2, review item b: a disable deletes pending reset values, and redeem refuses a disabled account."""

    def issue(self, user_id):
        status, issued, _ = self.request('POST', '/v1/accounts/%s/reset' % user_id, {}, self.admin['bearer'], key=True)
        self.assertEqual(status, 201, issued)
        return issued['reset_value']

    def disable(self, user_id):
        status, body, _ = self.request('POST', '/v1/accounts/%s/disable' % user_id, {}, self.admin['bearer'], key=True)
        self.assertEqual(status, 200, body)

    def redeem(self, user_id, value):
        return self.request('POST', '/v1/accounts/%s/reset/redeem' % user_id,
                            {'reset_value': value, 'new_password': 'attacker-pass-9999'})[:2]

    def pending(self, user_id):
        return [r for r in self.service.state['reset_tokens'].values() if r['user_id'] == user_id and not r['used']]

    def test_a_reset_issued_before_a_disable_is_deleted_by_it(self):
        dan = self.make_account('dan', 'dan-pass-1234')
        old = self.issue(dan)
        self.disable(dan)
        self.assertEqual(self.pending(dan), [])
        status, body = self.redeem(dan, old)
        self.assertEqual((status, body['error']['message']), (401, 'Invalid or expired reset value'))
        self.assertTrue(http_auth.verify_password(self.service.state['users'][dan]['password'], 'dan-pass-1234'))

    def test_a_reset_issued_after_a_disable_sets_no_password(self):
        dan = self.make_account('dan', 'dan-pass-1234')
        self.disable(dan)
        value = self.issue(dan)
        status, body = self.redeem(dan, value)
        self.assertEqual((status, body['error']['message']), (401, 'Invalid or expired reset value'))
        self.assertTrue(http_auth.verify_password(self.service.state['users'][dan]['password'], 'dan-pass-1234'))

    def test_a_disable_leaves_another_accounts_pending_value_alone(self):
        dan, eve = self.make_account('dan', 'dan-pass-1234'), self.make_account('eve', 'eve-pass-1234')
        eves = self.issue(eve)
        self.disable(dan)
        self.assertEqual(self.redeem(eve, eves)[0], 200)


if __name__ == '__main__':
    unittest.main()
