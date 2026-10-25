"""Capability validation against a real disposable bd, through the actual client."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import test_bd_label_aliases as fixture
from test_capability_records import entry


@unittest.skipIf(fixture.endpoint is None, 'POSIX endpoint imports unavailable')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class CapabilityValidateRealBdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = type('CapabilityValidateRuntime', (fixture.RealBdLabelAliasTests,), {})
        cls.runtime.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.runtime.tearDownClass()

    def setUp(self):
        self.fixture = self.runtime(methodName='runTest')
        self.root = self.fixture.root
        self.project = self.fixture.project

    def execute(self, payload, command='validate'):
        return fixture.endpoint.execute(self.root, {
            'project': 'pp', 'actor': 'op', 'action': 'capability',
            'args': [command, '@attachment:0'],
            'attachments': {'0': {'flag': '--file', 'text': json.dumps(payload)}}})

    def test_real_client_validation_matches_propose_and_changes_nothing(self):
        existing = {name: value for name, value in entry(
            operation_id='real-validate-seed', key='review.real-validate').items() if name != 'operation'}
        made = self.execute(existing, 'propose')
        self.assertEqual(made['returncode'], 0, made)
        before_rows = self.fixture.export_rows()
        journal = self.project / '.capability-requests'
        before_receipts = {path.name: path.read_bytes() for path in journal.glob('*.json')}
        lock = self.project / '.coordination.lock'
        before_lock = lock.read_bytes() if lock.exists() else None

        config = self.root / 'validate-client.json'
        config.write_text(json.dumps({'transport': 'local', 'endpoint': str(KIT / 'endpoint.py'),
                                      'root': str(self.root), 'python': sys.executable}), encoding='utf-8')
        payload_file = self.root / 'validate-payload.json'
        payload = dict(existing, operation_id='real-validate-read', owner='jjbp-j02')
        payload_file.write_text(json.dumps([payload]), encoding='utf-8')
        command = [sys.executable, str(KIT / 'client.py'), '--config', str(config), '--project', 'pp',
                   '--actor', 'op', '--', 'capability', 'validate', '--file', str(payload_file), '--repo', str(KIT)]
        done = subprocess.run(command, cwd=str(KIT), env=fixture.admin.environment(self.root),
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertFalse(result['valid'])
        self.assertEqual(result['summary']['items'], 1)
        self.assertEqual(result['items'][0]['key'], 'review.real-validate')
        self.assertTrue(all(error['field'] and error['hint'] for error in result['items'][0]['errors']))

        with self.assertRaises(ValueError) as refused:
            self.execute(payload, 'propose')
        self.assertEqual(result['items'][0]['errors'][0]['message'], str(refused.exception))
        self.assertEqual(before_rows, self.fixture.export_rows())
        self.assertEqual(before_receipts, {path.name: path.read_bytes() for path in journal.glob('*.json')})
        self.assertEqual(before_lock, lock.read_bytes() if lock.exists() else None)

        valid = dict(existing, operation_id='real-validate-valid', key='review.real-validate-new',
                     anchors=['docs/REVIEWS.md#structured-contribution-delivery'])
        payload_file.write_text(json.dumps(valid), encoding='utf-8')
        done = subprocess.run(command, cwd=str(KIT), env=fixture.admin.environment(self.root),
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(json.loads(done.stdout)['valid'], done.stdout)
        self.assertEqual(before_rows, self.fixture.export_rows())
        self.assertEqual(before_receipts, {path.name: path.read_bytes() for path in journal.glob('*.json')})


if __name__ == '__main__':
    unittest.main()
