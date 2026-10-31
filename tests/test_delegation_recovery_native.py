"""Lost delegation restrictions against actual bd and the canonical HTTP backend."""
import datetime
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]

import delegations
import http_service
from test_coordinator_rules_endpoint import RulesHelpers
from test_web_records import _RecordsScenarioMixin, _valid_cap_propose, fixes, fixture


@unittest.skipIf(fixture.endpoint is None, 'endpoint is POSIX-only')
@unittest.skipIf(fixture.BD is None, 'set ORCHESTRA_BD_BIN for real bd')
class NativeDelegationRecoveryTests(_RecordsScenarioMixin, RulesHelpers, unittest.TestCase):
    def setUp(self):
        RulesHelpers.setUp(self)
        self.pid = 'pp'
        root = self.root

        class Harness(fixes.Harness):
            def make_backend(harness):
                return http_service.EndpointBackend(
                    sys.executable, str(Path(__file__).resolve().parents[1] / 'endpoint.py'),
                    str(root), service=harness.service)

        self.harness = Harness(methodName='runTest')
        self.harness.setUp()
        self.addCleanup(self.harness._stop_server)
        self.owner_token = self.harness.admin_token()
        registered = self.harness.request('POST', '/v1/projects',
                                         {'project_id': self.pid, 'name': 'Delegation recovery'},
                                         token=self.owner_token)
        self.assertEqual(201, registered.status, registered.data)

    def test_lost_restriction_and_interrupted_publish_refuse_then_fresh_decision_recovers(self):
        self.deployment(operators=['ops'], coordinator_writes=True, independent_approval=False)
        h = self.harness
        people = self._setup_actors(h)
        agent = people['coord1_agent']
        granted = h.request('PUT', f'/v1/projects/{self.pid}/agents/{agent["id"]}/coordinator',
                            {}, token=self.owner_token)
        self.assertEqual(200, granted.status, granted.data)

        def run(argv):
            result = self.r.bd(*argv, actor='ops')
            self.assertEqual(0, result.returncode, result.stderr)
            return result.stdout

        def read(decision):
            return json.loads(run(['show', decision, '--json', '--include-comments']))[0]

        def accept(key, draft, decision, operation):
            return h.request('POST', f'/v1/projects/{self.pid}/capabilities/{key}/accept',
                             {'expected_revision': draft['revision'], 'record_sha256': draft['sha256'],
                              'evidence': 'Lost restriction regression', 'decision_id': decision},
                             token=agent['token'], key=operation)

        for action, crash in (('revoke', False), ('narrow', False), ('revoke', True), ('narrow', True)):
            with self.subTest(action=action, crash=crash):
                label = action + ('-crash' if crash else '-lost')
                decision = self._delegate(h, title='Recovery ' + label, key='grant-' + label)
                payload = dict(schema_version=1, operation_id='restrict-' + label, decision=decision,
                               title=None, action=action, right='record-acceptance', grantee='coordinator-grant',
                               scope=[] if action == 'revoke' else ['references'],
                               escalate='' if action == 'revoke' else 'Ask owner', reason='Restrict this grant')
                kwargs = dict(project=self.pid, journal=self.project, run=run, recorded_by='ops',
                              via='operator', account=None,
                              at=(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=1)).isoformat())
                if crash:
                    with mock.patch.object(delegations, 'publish', side_effect=OSError('publish interrupted')):
                        with self.assertRaisesRegex(OSError, 'interrupted'):
                            delegations.apply(payload, **kwargs)
                else:
                    result = delegations.apply(payload, **kwargs)
                    live, _ = delegations.current(read(decision), self.pid, h.service.state, self.project, ['ops'])
                    if action == 'revoke':
                        self.assertIsNone(live)
                    else:
                        self.assertEqual(['references'], live['scope'])
                    (self.project / delegations.JOURNAL_DIR / (result['record']['sha256'] + '.json')).unlink()

                with self.assertRaisesRegex(delegations.DelegationRefusal, 'unhonoured comment'):
                    delegations.check_acceptance(read(decision), project=self.pid, state=h.service.state,
                                                 journal=self.project, agent=agent['id'], kind='capabilities',
                                                 operators=['ops'])
                listed = h.request('GET', f'/v1/projects/{self.pid}/delegations', token=self.owner_token)
                self.assertEqual(200, listed.status, listed.data)
                item = next(item for item in listed.data['items'] if item['decision'] == decision)
                self.assertFalse(item['in_force'])
                self.assertIn('unhonoured', item['reason'])
                self.assertTrue(all(not item['covered'] for item in listed.data['granted_agents']))
                key = 'recovery.' + label
                proposed = h.request('POST', f'/v1/projects/{self.pid}/capabilities',
                                     _valid_cap_propose(key, name=label), token=people['coord1']['token'],
                                     key='propose-' + label)
                self.assertEqual(201, proposed.status, proposed.data)
                before = self.r.export_rows()
                refused = accept(key, proposed.data, decision, 'refused-' + label)
                self.assertEqual(403, refused.status, refused.data)
                self.assertIn('unhonoured', refused.data['error']['message'])
                self.assertEqual(before, self.r.export_rows())
                last = key, proposed.data, decision

        fresh = self._delegate(h, title='Fresh owner-authorized recovery', key='fresh-recovery')
        key, draft, damaged = last
        recovered = accept(key, draft, fresh, 'accepted-after-recovery')
        self.assertEqual(200, recovered.status, recovered.data)
        self.assertEqual(fresh, recovered.data['acceptance']['decision_id'])
        self.assertIsNone(delegations.current(read(damaged), self.pid, h.service.state, self.project, ['ops'])[0])
