"""Delegation reads and writes over a REAL pinned bd (kittrial-5bb.469).

The pure cases are in test_delegations. These drive delegations.apply, read_issue and listing
against a native tracker (set ORCHESTRA_BD_BIN; the class skips without one), plus the raw
`--remove-label delegation` refusal at the endpoint seam. POSIX-only because endpoint imports fcntl.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers
import delegations as d
import test_delegations as td


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no pinned native bd (set ORCHESTRA_BD_BIN)')
class NativeDelegationTests(RulesHelpers, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Path(self.tmp.name) / td.PROJECT
        self.journal.mkdir()

    def run_bd(self, argv):
        # The comment author is bd's --actor label, and a record is honoured only for its recorder.
        done = self.r.bd(*argv, actor=td.OWNER)
        if done.returncode:
            raise ValueError('bd %s failed: %s' % (argv[0], done.stderr))
        return done.stdout

    def write(self, payload, at=td.AT[1]):
        return d.apply(payload, project=td.PROJECT, journal=self.journal, run=self.run_bd,
                       recorded_by=td.OWNER, via='session', account=td.OWNER, at=at)

    def shown(self, decision):
        return d.read_issue(self.run_bd, decision)

    def grant(self):
        return self.write(td.request(decision=None, title='Native delegation', operation_id='n-grant'))

    def listed(self, decision):
        return d.listing([self.shown(decision)], td.PROJECT, td.state(), self.journal, ['ops'])

    def test_a_lost_journal_entry_is_listed_with_its_reason_over_real_bd(self):
        made = self.grant()
        decision = made['decision']
        self.assertEqual([True], [item['in_force'] for item in self.listed(decision)])
        (self.journal / d.JOURNAL_DIR / (made['record']['sha256'] + '.json')).unlink()
        items = self.listed(decision)
        self.assertEqual([decision], [item['decision'] for item in items])
        self.assertFalse(items[0]['in_force'])
        self.assertIn('unhonoured comment', items[0]['reason'])

    def test_the_label_is_set_when_a_crash_follows_publish_over_real_bd(self):
        real = d.publish

        def crash_after(journal, record):
            real(journal, record)
            raise OSError('crashed after publish')
        created = json.loads(self.run_bd(['create', 'Crash decision', '--type', 'decision', '--json']))['id']
        with mock.patch.object(d, 'publish', side_effect=crash_after):
            with self.assertRaisesRegex(OSError, 'crashed after publish'):
                self.write(td.request(decision=created, operation_id='n-crash'))
        row = self.shown(created)
        self.assertIn(d.LABEL, row['labels'])
        listing = self.listed(created)
        self.assertEqual([True], [item['in_force'] for item in listing])

    def test_a_closed_and_a_retyped_decision_over_real_bd(self):
        decision = self.grant()['decision']
        self.run_bd(['update', decision, '--type', 'task', '--json'])
        row = self.shown(decision)
        self.assertEqual('task', row['issue_type'])
        item = self.listed(decision)[0]
        self.assertTrue(item['in_force'])
        self.assertTrue(any('typed task' in note for note in item['notes']), item['notes'])
        self.run_bd(['close', decision, '--json'])
        item = self.listed(decision)[0]
        self.assertTrue(item['in_force'])
        self.assertEqual('closed', item['status'])
        self.assertTrue(any('closing does not end a delegation' in note for note in item['notes']), item['notes'])

    def test_a_raw_remove_label_delegation_is_refused_and_revoke_still_works(self):
        decision = self.grant()['decision']
        for args in (['update', decision, '--remove-label', 'delegation', '--json'],
                     ['update', decision, '--remove-label=delegation', '--json'],
                     ['update', decision, '--set-labels', 'decision', '--json']):
            with self.subTest(args=str(args)):
                with self.assertRaisesRegex(ValueError, '(?i)reserved'):
                    self.endpoint('bd', 'mallory', args)
        self.assertIn(d.LABEL, self.shown(decision)['labels'])
        self.write(td.request(action='revoke', decision=decision, operation_id='n-revoke'), at=td.AT[2])
        item = self.listed(decision)[0]
        self.assertFalse(item['in_force'])
        self.assertIn('revoked', item['reason'])


if __name__ == '__main__':
    unittest.main()
