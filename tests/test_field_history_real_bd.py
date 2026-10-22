"""Real bd evidence for the field-change read (kittrial-5bb.354).

Opt in with ``ORCHESTRA_BD_BIN`` (and a sibling ``dolt``), exactly as the
``tests/test_bd_label_aliases.py`` fixture does: a scratch Dolt server under its own
temporary HOME and a fresh tracker; nothing here touches a live runtime. It pins what
field_history.py relies on, measured on the real binary: ``bd history ID --json --limit N
--readonly`` is accepted, lists one commit per write newest first with the whole row,
names no author, and the reading finds the value a field had at a given moment.
"""
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import field_history
import test_bd_label_aliases as rb
from activity import utc_text
from datetime import datetime, timezone


@unittest.skipIf(rb.BD is None, 'real bd field-history test needs ORCHESTRA_BD_BIN or bd on PATH')
class RealBdFieldHistoryTests(unittest.TestCase):
    bd = rb.RealBdLabelAliasTests.bd

    @classmethod
    def setUpClass(cls):
        base = rb.RealBdLabelAliasTests
        base.setUpClass()
        for name in ('_tmp', '_signals', 'root', 'bd_path', 'port', 'project', 'server'):
            setattr(cls, name, getattr(base, name))

    @classmethod
    def tearDownClass(cls):
        rb.RealBdLabelAliasTests.tearDownClass()

    def run_ok(self, *args, actor='coord'):
        done = self.bd(*args, actor=actor)
        self.assertEqual(0, done.returncode, done.stderr)
        return done.stdout

    def test_the_history_names_the_previous_value_and_no_author(self):
        task = json.loads(self.run_ok('create', 'Field task', '-d', 'first desc', '--assignee', 'worker', '--json'))['id']
        time.sleep(1.1)
        checkpoint_at = utc_text(datetime.now(timezone.utc))      # a moment between create and the edits
        time.sleep(1.1)
        self.run_ok('update', task, '--description', 'second desc', actor='coord')
        self.run_ok('update', task, '--description', 'third desc', actor='worker')
        self.run_ok('update', task, '--status', 'in_progress', actor='worker')       # a claim-like change
        text = self.run_ok(*field_history.command(task), actor='probe')
        commits = field_history.parse(text)
        self.assertIsNotNone(commits, text[:500])
        self.assertGreaterEqual(len(commits), 4)
        self.assertEqual({'beads'}, {item['Committer'] for item in json.loads(text)})   # who is not recorded
        row = {'title': 'Field task', 'description': 'third desc', 'acceptance_criteria': ''}
        fields, note = field_history.changes(text, row, checkpoint_at)
        self.assertIsNone(note)
        [field] = fields
        self.assertEqual(('description', 'first desc', 'third desc', 2, None),
                         (field['field'], field['previous']['text'], field['current']['text'],
                          field['edits'], field['author']))

    def test_readonly_refuses_nothing_a_read_needs_and_writes_nothing(self):
        task = json.loads(self.run_ok('create', 'Read only', '--json'))['id']
        before = self.run_ok('history', task, '--json')
        self.run_ok(*field_history.command(task), actor='probe')
        self.assertEqual(before, self.run_ok('history', task, '--json'))


if __name__ == '__main__':
    unittest.main()
