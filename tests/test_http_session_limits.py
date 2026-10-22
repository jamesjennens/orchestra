"""The installation's session idle and absolute limits, and keeping an open page alive.

kittrial-5bb.330. Two things are checked here.

* The limits. An installation may set the idle window (5 minutes to 24 hours) and the
  absolute limit (1 hour to 7 days), and the absolute limit is never shorter than the idle
  window. The web service's ``--session-idle-minutes`` and ``--session-absolute-hours``
  carry them, the office configuration's ``session_idle_minutes`` / ``session_absolute_hours``
  forward them to the web service it starts, and both are refused with one sentence and a
  non-zero exit instead of being clamped. An installation that sets neither keeps the
  reviewed 30 minutes / 12 hours exactly as they were before this change.
* The keep-alive. The authenticated session read is the light touch: it answers the
  configured idle window and this session's deadlines, so an open set-up wizard can keep the
  session alive while it is visible and warn before the deadline. ``web/js/api.js`` is run
  under Node for the page's rule (a hidden tab polls nothing) by
  ``tests/web_session_keepalive.mjs``.

The real tracker path (a real bd, the real endpoint and a real running web service, with the
setting read from an office configuration) is ``tests/test_http_session_limits_real_bd.py``.
"""
import calendar
import contextlib
import errno
import io
import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_auth
import http_service
import test_http_agents as agents

MINUTE, HOUR, DAY = 60, 3600, 86400
REFUSAL = 'Refusing the session limits:'


def stamp(iso):
    """A deadline as the seconds the store keeps, so it is compared with a tick."""
    return calendar.timegm(time.strptime(iso, '%Y-%m-%dT%H:%M:%SZ'))


def limited_service(idle_seconds, absolute_seconds):
    """A Service with the two limits an installation setting produces.

    ``http_service.main`` hands exactly these two numbers to its ``Service``, so a class
    built here is what a configured installation runs.
    """
    class Limited(http_auth.Service):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault('session_idle', idle_seconds)
            kwargs.setdefault('session_absolute', absolute_seconds)
            super().__init__(*args, **kwargs)
    return Limited


class SettingTests(unittest.TestCase):
    """The setting surface: whole minutes and hours, the bounds, and the one refusal."""

    def test_no_setting_keeps_the_reviewed_defaults(self):
        self.assertEqual((30 * MINUTE, 12 * HOUR),
                         (http_auth.SESSION_IDLE_SECONDS, http_auth.SESSION_ABSOLUTE_SECONDS))
        self.assertEqual((30 * MINUTE, 12 * HOUR), http_auth.session_limit_seconds())
        self.assertEqual((30 * MINUTE, 12 * HOUR), http_auth.session_limit_seconds(None, None))

    def test_the_limits_are_read_in_whole_minutes_and_hours(self):
        self.assertEqual((5 * MINUTE, 24 * HOUR), http_auth.session_limit_seconds(5, 24))
        self.assertEqual((24 * HOUR, 7 * DAY), http_auth.session_limit_seconds(24 * 60, 7 * 24))
        self.assertEqual((5 * MINUTE, HOUR), http_auth.session_limit_seconds(5, 1))
        # The two ends of each bound, and the smallest and largest pairs that are in order.
        self.assertEqual((300, 3600), http_auth.session_limit_seconds(5, 1))
        self.assertEqual((86400, 604800), http_auth.session_limit_seconds(1440, 168))
        # Equal is not "shorter": a session may die at one deadline.
        self.assertEqual((24 * HOUR, 24 * HOUR), http_auth.session_limit_seconds(1440, 24))
        self.assertEqual((HOUR, HOUR), http_auth.session_limit_seconds(60, 1))

    def test_every_value_outside_the_bounds_is_refused_with_one_sentence(self):
        cases = ((4, None), (0, None), (-1, None), (1441, None), (None, 0), (None, 169),
                 (None, -1), (True, None), (None, True), (False, None), (5.0, None),
                 (None, 1.0), ('5', None), (None, '5'), ([], None), (None, {}))
        for idle, absolute in cases:
            with self.subTest(idle=idle, absolute=absolute):
                with self.assertRaises(ValueError) as refused:
                    http_auth.session_limit_seconds(idle, absolute)
                said = str(refused.exception)
                self.assertTrue(said.startswith(REFUSAL), said)
                self.assertIn('the idle limit must be a whole number of minutes from 5 to 1440 '
                              '(5 minutes to 24 hours)', said)
                self.assertIn('the absolute limit a whole number of hours from 1 to 168 '
                              '(1 hour to 7 days)', said)
                self.assertIn('the absolute limit must not be shorter than the idle limit', said)

    def test_the_refusal_is_exactly_one_sentence(self):
        """One sentence, ending in one period, for the flags, the config and the operator."""
        for idle, absolute in ((4, None), (None, 169), (1440, 12)):
            with self.subTest(idle=idle, absolute=absolute):
                with self.assertRaises(ValueError) as refused:
                    http_auth.session_limit_seconds(idle, absolute)
                said = str(refused.exception)
                self.assertEqual(1, said.count('.'), 'one sentence, not several: %r' % said)
                self.assertTrue(said.endswith('.'), said)

    def test_an_absolute_limit_shorter_than_the_idle_window_is_refused(self):
        for idle, absolute in ((24 * 60, 23), (90, 1), (1440, 1), (1440, 12)):
            with self.subTest(idle=idle, absolute=absolute):
                with self.assertRaises(ValueError) as refused:
                    http_auth.session_limit_seconds(idle, absolute)
                said = str(refused.exception)
                self.assertTrue(said.startswith(REFUSAL), said)
                self.assertIn('got idle %r and absolute %r' % (idle, absolute), said)

    def test_a_refusal_names_what_was_given(self):
        with self.assertRaises(ValueError) as refused:
            http_auth.session_limit_seconds(4, 200)
        self.assertIn('got idle 4 and absolute 200', str(refused.exception))


class CommandLineTests(unittest.TestCase):
    """``http_service.py``'s two flags: refused before any state, or handed to the Service."""

    def setUp(self):
        self.root = agents.unique_dir('session-limits-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def started(self, *flags, **kwargs):
        """The ``Service`` ``main`` built, by failing the start after it was built."""
        captured = {}

        def refuse(service, backend, **unused):
            captured['service'] = service
            raise OSError(errno.EIO, 'synthetic start failure')

        with mock.patch.object(http_service, 'create_server', side_effect=refuse), \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError) as raised:
                http_service.main(['--state', str(kwargs.get('state') or self.root / 'state.json'),
                                   '--backend', 'inprocess', '--port', '0'] + list(flags))
        self.assertEqual(errno.EIO, raised.exception.errno)
        return captured['service']

    def test_a_refused_limit_is_one_sentence_a_non_zero_exit_and_no_state(self):
        cases = (('--session-idle-minutes', '4'), ('--session-idle-minutes', '1441'),
                 ('--session-idle-minutes', '-1'), ('--session-absolute-hours', '0'),
                 ('--session-absolute-hours', '169'), ('--session-idle-minutes', '1440',
                                                       '--session-absolute-hours', '12'))
        for index, case in enumerate(cases):
            with self.subTest(flags=case):
                state = self.root / ('never-opened-%d.json' % index)
                said = io.StringIO()
                with contextlib.redirect_stderr(said), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as exit_:
                        http_service.main(['--state', str(state), '--no-web'] + list(case))
                self.assertEqual(2, exit_.exception.code)
                self.assertIn(REFUSAL, said.getvalue())
                self.assertFalse(state.exists(), 'the state store is opened only after the limits are read')

    def test_the_flags_reach_the_service_and_an_absent_flag_keeps_the_default(self):
        service = self.started()
        self.assertEqual((30 * MINUTE, 12 * HOUR), (service.session_idle, service.session_absolute))
        service = self.started('--session-idle-minutes', '5', '--session-absolute-hours', '1')
        self.assertEqual((5 * MINUTE, HOUR), (service.session_idle, service.session_absolute))
        service = self.started('--session-idle-minutes', '600')
        self.assertEqual((10 * HOUR, 12 * HOUR), (service.session_idle, service.session_absolute))
        service = self.started('--session-absolute-hours', '168')
        self.assertEqual((30 * MINUTE, 7 * DAY), (service.session_idle, service.session_absolute))
        service = self.started('--session-idle-minutes', '1440', '--session-absolute-hours', '168')
        self.assertEqual((DAY, 7 * DAY), (service.session_idle, service.session_absolute))

    def test_the_help_names_both_flags_with_their_bounds(self):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            with self.assertRaises(SystemExit) as exit_:
                http_service.main(['--help'])
        self.assertEqual(0, exit_.exception.code)
        help_text = printed.getvalue()
        self.assertIn('--session-idle-minutes', help_text)
        self.assertIn('--session-absolute-hours', help_text)
        self.assertIn('5 to 1440', help_text)
        self.assertIn('1 to 168', help_text)


class LimitedHarness(agents.AgentHarness):
    """The web service built with the limits an installation setting produces."""

    #: (idle minutes, absolute hours), read through the same helper the flags use.
    setting = (5, 24)

    def setUp(self):
        idle, absolute = http_auth.session_limit_seconds(*self.setting)
        with mock.patch.object(agents, 'Service', limited_service(idle, absolute)):
            super().setUp()
        self.tick = [self.store.now()]
        self.start = self.tick[0]
        self.store.clock = lambda: self.tick[0]

    def session(self, token=None):
        return self.request('GET', '/v1/sessions/current', token=token)

    def login_with_session(self):
        response = self.request('POST', '/v1/sessions',
                                {'username': agents.ADMIN, 'password': agents.ADMIN_PASSWORD})
        self.assertEqual(201, response.status, response.data)
        return response


class FiveMinuteIdleTests(LimitedHarness):
    """The shortest idle window an installation may choose, and a day of absolute limit."""

    setting = (5, 24)

    def test_the_login_and_the_session_read_carry_the_configured_window(self):
        login = self.login_with_session()
        self.assertEqual(int(self.start + 5 * MINUTE), stamp(login.data['session']['idle_expires_at']))
        self.assertEqual(int(self.start + 24 * HOUR), stamp(login.data['session']['expires_at']))
        token = login.data['session']['token']
        read = self.session(token)
        self.assertEqual(200, read.status, read.data)
        self.assertEqual(5 * MINUTE, read.data['idle_seconds'])
        self.assertEqual(24 * HOUR, read.data['absolute_seconds'])
        self.assertEqual(login.data['session']['idle_expires_at'], read.data['idle_expires_at'])
        self.assertEqual(login.data['session']['expires_at'], read.data['absolute_expires_at'])

    def test_the_session_ends_at_the_configured_idle_deadline_and_not_before(self):
        early = self.login_with_session().data['session']['token']
        late = self.login_with_session().data['session']['token']
        # A session used at the last second is alive, and a full window from that use.
        self.tick[0] = self.start + 5 * MINUTE - 1
        refreshed = self.session(early)
        self.assertEqual(200, refreshed.status, 'the window is not shorter than the setting')
        self.assertEqual(http_auth.now_iso(self.tick[0] + 5 * MINUTE), refreshed.data['idle_expires_at'])
        # An untouched session dies exactly at the configured deadline; no read can have
        # moved this one, and the clock only ever moves forward (the store's monotone floor).
        self.tick[0] = self.start + 5 * MINUTE
        self.assertEqual(401, self.session(late).status)
        self.tick[0] = self.start + 5 * MINUTE - 1 + 5 * MINUTE
        self.assertEqual(401, self.session(early).status)


class OneHourAbsoluteTests(LimitedHarness):
    """The shortest absolute limit an installation may choose: no use extends it."""

    setting = (5, 1)

    def test_the_session_ends_at_the_configured_absolute_deadline_however_often_it_is_used(self):
        token = self.login_with_session().data['session']['token']
        used = 0
        while self.tick[0] + 4 * MINUTE < self.start + HOUR:
            self.tick[0] += 4 * MINUTE
            self.assertEqual(200, self.session(token).status)
            used += 1
        self.assertGreaterEqual(used, 14, 'the session was used throughout its hour')
        self.tick[0] = self.start + HOUR
        self.assertEqual(401, self.session(token).status)


class SevenDayAbsoluteTests(LimitedHarness):
    """The longest absolute limit an installation may choose: a day of idle, seven days in all."""

    setting = (24 * 60, 7 * 24)

    def test_the_longest_absolute_limit_is_honoured_to_its_second(self):
        login = self.login_with_session()
        token = login.data['session']['token']
        self.assertEqual(http_auth.now_iso(self.start + 7 * DAY),
                         self.session(token).data['absolute_expires_at'])
        # Used every twenty hours, the session stays inside its day-long idle window all week...
        for hour in range(20, 168, 20):
            self.tick[0] = self.start + hour * HOUR
            self.assertEqual(200, self.session(token).status)
        self.tick[0] = self.start + 7 * DAY - 2 * HOUR
        self.assertEqual(200, self.session(token).status)
        # ...and still dies at the absolute deadline, long after that last use.
        self.tick[0] = self.start + 7 * DAY - 1
        self.assertEqual(200, self.session(token).status)
        self.tick[0] = self.start + 7 * DAY
        self.assertEqual(401, self.session(token).status)


class DefaultLimitTests(agents.AgentHarness):
    """No setting at all: the reviewed 30 minutes and 12 hours, unchanged."""

    def setUp(self):
        super().setUp()
        self.tick = [self.store.now()]
        self.start = self.tick[0]
        self.store.clock = lambda: self.tick[0]

    def test_an_installation_that_sets_nothing_keeps_the_reviewed_defaults(self):
        login = self.request('POST', '/v1/sessions',
                             {'username': agents.ADMIN, 'password': agents.ADMIN_PASSWORD})
        self.assertEqual(201, login.status, login.data)
        token = login.data['session']['token']
        read = self.request('GET', '/v1/sessions/current', token=token)
        self.assertEqual(200, read.status, read.data)
        self.assertEqual((30 * MINUTE, 12 * HOUR), (read.data['idle_seconds'], read.data['absolute_seconds']))
        self.assertEqual(http_auth.now_iso(self.start + 30 * MINUTE), read.data['idle_expires_at'])
        self.assertEqual(http_auth.now_iso(self.start + 12 * HOUR), read.data['absolute_expires_at'])
        # An untouched session is alive half an hour later to the second and gone at the deadline.
        early = self.login_with_session().data['session']['token']
        gone = self.login_with_session().data['session']['token']
        self.tick[0] = self.start + 30 * MINUTE - 1
        self.assertEqual(200, self.request('GET', '/v1/sessions/current', token=early).status)
        self.tick[0] = self.start + 30 * MINUTE
        self.assertEqual(401, self.request('GET', '/v1/sessions/current', token=gone).status)

    def login_with_session(self):
        response = self.request('POST', '/v1/sessions',
                                {'username': agents.ADMIN, 'password': agents.ADMIN_PASSWORD})
        self.assertEqual(201, response.status, response.data)
        return response


class CredentialReadTests(LimitedHarness):
    """A worker or agent credential has no browser session, so the read adds nothing for it."""

    setting = (5, 24)

    def test_a_credential_read_carries_no_browser_session_window(self):
        token = self.admin_token()
        _, secret, _ = self.agent_secret(token)
        read = self.request('GET', '/v1/sessions/current', token=secret)
        self.assertEqual(200, read.status, read.data)
        for absent in ('idle_seconds', 'absolute_seconds', 'idle_expires_at', 'absolute_expires_at'):
            self.assertNotIn(absent, read.data)
        self.assertIn('credential_expiry', read.data)


class ReloginTests(LimitedHarness):
    """Being signed out mid set-up: the next log-in is the same account and the same agent."""

    setting = (5, 24)

    def test_a_relogin_after_an_expired_session_keeps_the_same_user_and_makes_no_second_agent(self):
        first = self.login_with_session()
        token, user = first.data['session']['token'], first.data['user']
        agent_id, _, _ = self.agent_secret(token)
        agents_before = dict(self.service.state['agents'])
        credentials_before = dict(self.service.state['credentials'])
        self.assertEqual(1, len(agents_before))
        # The owner left the wizard open and the idle window ran out.
        self.tick[0] = self.start + 5 * MINUTE
        self.assertEqual(401, self.session(token).status)
        again = self.login_with_session()
        self.assertEqual(201, again.status, again.data)
        self.assertEqual(user['id'], again.data['user']['id'])
        self.assertNotEqual(token, again.data['session']['token'])
        self.assertEqual(agents_before, self.service.state['agents'])
        self.assertEqual(credentials_before, self.service.state['credentials'])
        self.assertEqual([agent_id], [row['id'] for row in self.service.state['agents'].values()])
        listed = self.request('GET', '/v1/agents', token=again.data['session']['token'])
        self.assertEqual(200, listed.status, listed.data)
        self.assertEqual([agent_id], [row['id'] for row in listed.data['items']])
        # The new session carries the same configured window from its own log-in.
        read = self.session(again.data['session']['token'])
        self.assertEqual(200, read.status, read.data)
        self.assertEqual(http_auth.now_iso(self.tick[0] + 5 * MINUTE), read.data['idle_expires_at'])


@unittest.skipIf(os.name == 'nt', 'office_service.py imports fcntl; the supervisor is POSIX only')
class OfficeConfigurationTests(unittest.TestCase):
    """The office installation's own config keys, and what it hands the web service."""

    def setUp(self):
        self.root = agents.unique_dir('office-session-limits-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.path = self.root / 'office.json'

    def configured(self, **settings):
        body = dict(schema_version=1)
        body.update(settings)
        self.path.write_text(json.dumps(body), encoding='utf-8')
        import office_service
        return office_service.service_config(self.path)

    def command(self, **settings):
        import office_service
        path = self.root / 'kit/office_service.py'
        return [str(part) for part in office_service.web_command(
            self.configured(**settings), self.root, 10000, '/synthetic/python', path)]

    def test_the_config_keys_are_validated_and_forwarded_to_the_web_service(self):
        for idle, absolute in ((5, 1), (600, 168), (1440, 24), (24 * 60, 7 * 24)):
            with self.subTest(idle_minutes=idle, absolute_hours=absolute):
                command = self.command(session_idle_minutes=idle, session_absolute_hours=absolute)
                self.assertEqual(str(idle), command[command.index('--session-idle-minutes') + 1])
                self.assertEqual(str(absolute), command[command.index('--session-absolute-hours') + 1])
        # One key alone, the other limit staying the reviewed default.
        command = self.command(session_idle_minutes=5)
        self.assertEqual(['5', '12'], [command[command.index('--session-idle-minutes') + 1],
                                       command[command.index('--session-absolute-hours') + 1]])
        command = self.command(session_absolute_hours=168)
        self.assertEqual(['30', '168'], [command[command.index('--session-idle-minutes') + 1],
                                         command[command.index('--session-absolute-hours') + 1]])
        # No key at all: neither flag is on the command line, exactly as before this change.
        command = self.command()
        self.assertNotIn('--session-idle-minutes', command)
        self.assertNotIn('--session-absolute-hours', command)

    def test_a_bad_office_setting_is_the_same_one_sentence_and_starts_nothing(self):
        cases = (dict(session_idle_minutes=4), dict(session_idle_minutes=1441),
                 dict(session_absolute_hours=0), dict(session_absolute_hours=169),
                 dict(session_idle_minutes=1440, session_absolute_hours=12),
                 dict(session_idle_minutes=True), dict(session_absolute_hours=5.0),
                 dict(session_absolute_hours='5'))
        for settings in cases:
            with self.subTest(**{str(k): v for k, v in settings.items()}):
                with self.assertRaises(ValueError) as refused:
                    self.configured(**settings)
                self.assertIn(REFUSAL, str(refused.exception))
        # A refused configuration starts nothing and writes nothing of its own: the file the
        # test wrote is the only thing in the runtime directory.
        self.assertEqual(['office.json'], sorted(entry.name for entry in self.root.iterdir()))


class KeepAlivePageTests(unittest.TestCase):
    """The real ``web/js/api.js`` keep-alive, run under Node: a hidden tab polls nothing."""

    def test_the_page_touches_only_while_visible_and_warns_before_the_deadline(self):
        from test_http_web import run_node_module
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is unavailable; the real page module is not exercised')
        done = run_node_module(self, node, 'await import(process.argv[1])',
                               (KIT / 'tests/web_session_keepalive.mjs').as_uri(),
                               (KIT / 'web/js/api.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        observed = json.loads(done.stdout.strip().splitlines()[-1])
        for name, value in observed.items():
            self.assertTrue(value, 'the page module reported %s=%r' % (name, value))
        self.assertEqual({'keepAliveIsOneSessionRead', 'keepAliveAnswersTheWindow', 'keepAliveTouchesAgain',
                          'touchesWhileVisible', 'hiddenTabDoesNotPoll', 'touchesAgainWhenVisible',
                          'stopEndsTheLoop', 'stopRemovesTheListener', 'noTouchWhileHidden',
                          'firstTouchWhenShown', 'failedTouchIsRetried', 'warnsBeforeTheDeadline',
                          'warningCarriesTheWindow', 'warningIsTheDocumentEvent', 'signOutStopsTheLoop',
                          'touchedBeforeSignOut', 'signOutDeletedTheSession', 'touchedBeforeExpiry',
                          'expiredSessionStopsTheLoop'}, set(observed))


if __name__ == '__main__':
    unittest.main()
