"""Agent-only lifetime/configuration and stored-expiry rollback foundation (.214).

All accounts/keys/stores are synthetic. Extension/target-account/page coverage is
added with those interfaces; these checks never qualify the complete slice.
"""
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import http_auth
import http_service
import test_http_agents as agents

DAY = 86400


class LifetimeSettings(unittest.TestCase):
    def setUp(self):
        agents.TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='agent-life-', dir=agents.TMP_ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = http_auth.Store(self.root / 'state.json')

    def test_agent_days_do_not_change_worker_session_or_reset_lifetimes(self):
        for days in (1, 30, 90, 366):
            with self.subTest(days=days):
                service = http_auth.Service(self.store, credential_ttl=7, agent_credential_days=days)
                self.assertEqual(days * DAY, service.agent_credential_ttl)
                self.assertEqual(7, service.credential_ttl)
                self.assertEqual(http_auth.SESSION_IDLE_SECONDS, service.session_idle)
                self.assertEqual(http_auth.RESET_TTL_SECONDS, service.reset_ttl)
        self.assertEqual(7, http_auth.Service(self.store, credential_ttl=7).agent_credential_ttl)
        self.assertEqual(30 * DAY, http_auth.Service(self.store).agent_credential_ttl)

    def test_invalid_setting_is_refused_without_mutating_auth_state(self):
        before = copy.deepcopy(self.store.state)
        for value in (None, True, False, 0, -1, 367, 10**1000, 90.0, '90', [], {}):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(ValueError, 'whole number from 1 to 366'):
                    http_auth.Service(self.store, agent_credential_days=value)
                self.assertEqual(before, self.store.state)

    def test_cli_refuses_bad_days_before_opening_any_state(self):
        for value in ('0', '367', '-1', '90.0', 'true'):
            with self.subTest(value=value), mock.patch.object(http_service, 'Store') as opened, \
                    mock.patch('sys.stderr', new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    http_service.main(['--state', str(self.root / 'never-opened.json'),
                                       '--agent-credential-days', value, '--no-web'])
                self.assertEqual(2, error.exception.code)
                opened.assert_not_called()
        self.assertFalse((self.root / 'never-opened.json').exists())

    def test_expiry_overflow_and_non_finite_clock_are_refused(self):
        service = http_auth.Service(self.store, agent_credential_days=366)
        for stamp in (float('inf'), float('nan'), 1e100, 253402300790.0):
            with self.subTest(stamp=stamp), mock.patch.object(service, '_raw_now', return_value=stamp):
                with self.assertRaises(http_auth.HttpError) as error:
                    service._agent_expiry_metadata(stamp)
                self.assertEqual(422, error.exception.status)
        self.assertFalse(self.store.state['agents'])
        self.assertFalse(self.store.state['credentials'])

    @unittest.skipIf(os.name == 'nt', 'office supervisor uses POSIX fcntl')
    def test_office_setting_validation_and_forwarded_command(self):
        import office_service
        path = self.root / 'office.json'
        for value in (1, 90, 366):
            path.write_text(json.dumps(dict(schema_version=1, agent_credential_days=value)))
            settings = office_service.service_config(path)
            cmd = office_service.web_command(settings, self.root, 9001,
                                              Path('/synthetic/python'), Path('/synthetic/kit/office_service.py'))
            self.assertEqual(value, cmd[cmd.index('--agent-credential-days') + 1])
        for value in (None, True, 0, 367, 90.0, '90'):
            path.write_text(json.dumps(dict(schema_version=1, agent_credential_days=value)))
            with self.subTest(value=value), self.assertRaises(ValueError):
                office_service.service_config(path)


class Service90(http_auth.Service):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, agent_credential_days=90, **kwargs)


class AgentLifetimeHttp(agents.AgentHarness):
    def setUp(self):
        with mock.patch.object(agents, 'Service', Service90):
            super().setUp()
        self.tick = [self.store.now()]
        self.start = self.tick[0]
        self.store.clock = lambda: self.tick[0]

    def made(self, name='Long-lived synthetic agent'):
        _, secret, result = self.agent_secret(self.admin_token(), name=name, scopes=['read'])
        credential = self.service.state['credentials'][result['credential']['id']]
        return secret, credential, result

    def test_ninety_day_key_survives_thirty_and_setting_change_does_not_rewrite_it(self):
        secret, credential, result = self.made()
        self.assertEqual(90 * DAY, credential['expires_at'] - self.start)
        self.assertNotIn('issued_raw', credential)
        self.assertEqual(self.start, credential['agent_issued_raw'])
        before = (credential['expires_at'], credential['agent_raw_expires_at'])
        self.service.agent_credential_ttl = DAY
        self.tick[0] = self.start + 31 * DAY
        self.assertEqual(200, self.request('GET', '/v1/sessions/current', token=secret).status)
        self.assertEqual(before, (credential['expires_at'], credential['agent_raw_expires_at']))
        self.tick[0] = self.start + 90 * DAY
        self.assertEqual(401, self.request('GET', '/v1/sessions/current', token=secret).status)
        self.assertEqual(['read'], result['credential']['scopes'])

    def test_default_thirty_day_key_keeps_legacy_raw_stamp(self):
        self.service.agent_credential_ttl = 30 * DAY
        secret, credential, _ = self.made()
        self.assertEqual(self.start, credential['issued_raw'])
        self.assertEqual(30 * DAY, credential['expires_at'] - self.start)
        self.tick[0] = self.start + 30 * DAY
        self.assertEqual(401, self.request('GET', '/v1/sessions/current', token=secret).status)

    def test_raw_deadline_still_expires_auth_when_monotone_floor_is_ahead(self):
        secret, credential, _ = self.made()
        floor = self.start + 1000 * DAY
        with mock.patch.object(self.service, '_expiry_now', return_value=floor):
            credential.update(self.service._agent_expiry_metadata(floor))
            self.tick[0] = self.start + 90 * DAY
            self.assertLess(floor, credential['expires_at'])
            self.assertEqual(401, self.request('GET', '/v1/sessions/current', token=secret).status)

    def test_damaged_agent_raw_deadline_refuses_authentication(self):
        secret, credential, _ = self.made()
        for value in (None, True, float('inf'), float('nan'), 'tomorrow', {}):
            credential['agent_raw_expires_at'] = value
            with self.subTest(value=repr(value)):
                self.assertEqual(401, self.request('GET', '/v1/sessions/current', token=secret).status)

    def test_worker_keys_do_not_take_the_agent_metadata_path(self):
        worker = {'revoked': False, 'agent_id': None, 'issued_raw': self.start,
                  'expires_at': self.start + 90 * DAY,
                  'agent_raw_expires_at': self.start + 90 * DAY}
        self.tick[0] = self.start + 31 * DAY
        self.assertFalse(self.service._credential_live(worker))

    def test_expiry_refusal_precedes_new_agent_or_credential_state(self):
        token = self.admin_token()
        before = (copy.deepcopy(self.service.state['agents']),
                  copy.deepcopy(self.service.state['credentials']))
        with mock.patch.object(self.service, '_agent_expiry_metadata',
                               side_effect=http_auth.invalid('synthetic expiry overflow')):
            result = self.create_agent(token, name='No partial agent')
        self.assertEqual(422, result.status)
        self.assertEqual(before, (self.service.state['agents'], self.service.state['credentials']))

    def test_current_reload_and_actual_old_reader_honour_longer_stored_expiry(self):
        previous = os.environ.get('ORCHESTRA_CREDENTIAL_PREVIOUS_KIT')
        if not previous:
            self.skipTest('set ORCHESTRA_CREDENTIAL_PREVIOUS_KIT to the owned actual released reader')
        previous = Path(previous).resolve()
        self.assertTrue((previous / 'http_auth.py').is_file())
        secret, credential, result = self.made()
        self.tick[0] = self.start + 31 * DAY
        reloaded = http_auth.Service(http_auth.Store(self.store.path, clock=lambda: self.tick[0]),
                                      agent_credential_days=1)
        self.assertEqual(result['agent']['id'], reloaded.authenticate(secret).actor)
        code = ('import json,sys;sys.path.insert(0,sys.argv[1]);from http_auth import Service,Store;'
                'v=json.load(sys.stdin);p=Service(Store(v["state"],clock=lambda:v["now"])).authenticate(v["secret"]);'
                'print(json.dumps({"actor":p.actor,"via":p.via}))')
        payload = dict(state=str(self.store.path), now=self.tick[0], secret=secret)
        child = subprocess.run([sys.executable, '-B', '-c', code, str(previous)],
                               cwd=previous, input=json.dumps(payload), capture_output=True, text=True,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), timeout=45)
        self.assertEqual(0, child.returncode, child.stderr)
        answer = json.loads(child.stdout)
        self.assertEqual(dict(actor=result['agent']['id'], via='credential'), answer)
        self.assertNotIn(secret, child.stdout + child.stderr)
        self.assertEqual(credential['token_hash'], reloaded.state['credentials'][credential['id']]['token_hash'])


if __name__ == '__main__':
    unittest.main()
