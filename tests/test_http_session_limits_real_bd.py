"""The installation's session limits with a real bd behind the real running web service.

kittrial-5bb.330, the end-to-end half. ``tests/test_http_session_limits.py`` checks the
setting surface, the flags, the expiry at the configured deadlines and the browser module on
their own. What only this module shows is the whole path with a real tracker: the office
configuration's own keys (``session_idle_minutes`` / ``session_absolute_hours``) are read by
the real ``office_service`` code, the numbers it forwards are what the real web service runs
with, a real project's rows are still read through a session on the real ``endpoint.py`` and
a real bd, that session then expires at the configured deadline, and logging in again is the
same account with the same single agent: resuming creates nothing twice.

Skipped where there is no real bd (``ORCHESTRA_BD_BIN``, or a ``bd`` on PATH) and on a host
without POSIX, because ``endpoint.py`` and ``office_service.py`` import ``fcntl``.
"""
import json
import os
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_auth
import http_service
import test_bd_label_aliases as rb
import test_claim_held as held
import test_coordinator_grant_real as native

try:
    import office_service
except ImportError:  # office_service.py imports fcntl: the supervisor is POSIX only
    office_service = None

MINUTE, HOUR = 60, 3600


@unittest.skipIf(os.name == 'nt', 'endpoint.py and office_service.py import fcntl; POSIX only')
@unittest.skipIf(office_service is None, 'office_service.py imports fcntl (POSIX-only)')
@unittest.skipIf(rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@native.only_its_own_tests
class OfficeSettingRealStackTests(held.RealStackTests):
    """The real web service, the real endpoint.py, a real bd, and the office configuration."""

    def office_setting(self, idle_minutes, absolute_hours):
        """The service the office configuration starts: keys -> command -> the same numbers.

        The configuration file is read by the real ``service_config``, the command line the
        real supervisor builds is asked for its flags, and the flags are read back exactly as
        ``http_service.main`` reads them. The running service is then rebuilt with what came
        out, so the request below is answered by a service with the installation's limits.
        """
        path = self.tmp / 'office.json'
        path.write_text(json.dumps({'schema_version': 1,
                                    'session_idle_minutes': idle_minutes,
                                    'session_absolute_hours': absolute_hours}), encoding='utf-8')
        settings = office_service.service_config(path)
        command = office_service.web_command(settings, self.root, 10000, sys.executable,
                                             KIT / 'http_service.py')
        self.assertIn('--session-idle-minutes', command)
        self.assertIn('--session-absolute-hours', command)
        flags = {'--session-idle-minutes': command[command.index('--session-idle-minutes') + 1],
                 '--session-absolute-hours': command[command.index('--session-absolute-hours') + 1]}
        self.assertEqual({'--session-idle-minutes': idle_minutes,
                          '--session-absolute-hours': absolute_hours}, flags)
        idle, absolute = http_auth.session_limit_seconds(flags['--session-idle-minutes'],
                                                         flags['--session-absolute-hours'])
        service = http_service.Service(self.store, session_idle=idle, session_absolute=absolute)
        backend = http_service.EndpointBackend(sys.executable, str(KIT / 'endpoint.py'),
                                               str(self.root), service=service)
        self.restart(service, backend)
        return idle, absolute

    def test_the_office_setting_expires_a_real_session_and_a_relogin_makes_no_second_agent(self):
        # A real task in the real project first: the rows the session below reads are bd's.
        task = self.new('A real task behind the configured session')
        idle, absolute = self.office_setting(5, 1)
        self.assertEqual((5 * MINUTE, HOUR), (idle, absolute))
        tick = [self.store.now()]
        self.start = tick[0]
        self.store.clock = lambda: tick[0]
        token, _, user = self.login('alex', 'alex-password-1')
        # The real tracker, read through the session the installation's limits apply to.
        rows = self.request('GET', '/v1/projects/pp/tasks', token=token)
        self.assertEqual(200, rows.status, rows.data)
        self.assertIn(task, [row['id'] for row in rows.data['items']])
        detail = self.request('GET', '/v1/projects/pp/tasks/%s' % task, token=token)
        self.assertEqual(200, detail.status, detail.data)
        self.assertEqual('A real task behind the configured session', detail.data['title'])
        read = self.request('GET', '/v1/sessions/current', token=token)
        self.assertEqual(200, read.status, read.data)
        self.assertEqual(5 * MINUTE, read.data['idle_seconds'])
        self.assertEqual(HOUR, read.data['absolute_seconds'])
        self.assertEqual(http_auth.now_iso(self.start + 5 * MINUTE), read.data['idle_expires_at'])
        self.assertEqual(http_auth.now_iso(self.start + HOUR), read.data['absolute_expires_at'])
        # The owner leaves the set-up wizard open: the configured window runs out.
        made = self.request('POST', '/v1/agents', {'name': 'Real limit agent',
                                                   'working_directory': '/home/x/limit',
                                                   'projects': ['pp']}, token=token)
        self.assertEqual(201, made.status, made.data)
        agent = made.data['agent']['id']
        agents_before = dict(self.service.state['agents'])
        credentials_before = dict(self.service.state['credentials'])
        self.assertEqual(1, len(agents_before))
        tick[0] = self.start + 5 * MINUTE
        self.assertEqual(401, self.request('GET', '/v1/sessions/current', token=token).status)
        # Signing in again is the same account and the same single agent, and the real
        # project is still readable: nothing was created a second time by resuming.
        again, _, second_user = self.login('alex', 'alex-password-1')
        self.assertEqual(user['id'], second_user['id'])
        self.assertEqual(agents_before, self.service.state['agents'])
        self.assertEqual(credentials_before, self.service.state['credentials'])
        listed = self.request('GET', '/v1/agents', token=again)
        self.assertEqual(200, listed.status, listed.data)
        self.assertEqual([agent], [row['id'] for row in listed.data['items']])
        detail = self.request('GET', '/v1/projects/pp/tasks/%s' % task, token=again)
        self.assertEqual(200, detail.status, detail.data)
        self.assertEqual(http_auth.now_iso(tick[0] + 5 * MINUTE),
                         self.request('GET', '/v1/sessions/current', token=again).data['idle_expires_at'])


if __name__ == '__main__':
    unittest.main()
