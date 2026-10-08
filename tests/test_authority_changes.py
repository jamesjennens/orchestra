"""The deployment-authority audit (kittrial-5bb.192, slice 5 of the coordinators design).

`admin.py operators add|remove` and `verifiers add|remove` record every real change of the
two installation authority lists - which list, the actor added or removed, the change, the
time, the operator who made it and why - in `authority-changes.audit.json` beside
`deployment.private.json`, and the read-only `authority-changes` command prints it. The
shape follows the adoption audit of kittrial-5bb.194 (`actor-adoptions.audit.json` /
`actor-adoptions`).

A pre-existing call without `--actor`/`--reason` keeps working, because the office wrapper
`coord.sh` runs the bare `admin.py --root RT operators add ACTOR` and is not part of this
repository. Such a call still records the change, with `operator`/`reason` null, and prints
one stderr sentence naming exactly what to add: it is never silent and never pretends
somebody was named. See the plan comment on kittrial-5bb.192 for the decision and why.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import admin

OPERATOR = 'ops'
VERIFIER = 'ci-host'
AUDIT = admin.AUTHORITY_CHANGES_AUDIT


class AuthorityChangesCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.marker = self.root / 'deployment.private.json'
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': [OPERATOR], 'verifiers': [VERIFIER]}),
                               encoding='utf-8')
        for name in ('ORCHESTRA_OPERATORS', 'ORCHESTRA_VERIFIERS'):
            saved = patch.dict(os.environ)
            saved.start()
            self.addCleanup(saved.stop)
            os.environ.pop(name, None)

    def cli(self, *argv):
        """Run one admin.py command; returns (stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                patch.object(admin, 'root_path', return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            admin.main()
        return out.getvalue(), err.getvalue()

    def stored(self):
        return json.loads(self.marker.read_text(encoding='utf-8'))

    def audit(self):
        """The audit document on disk, or None when no audit file exists."""
        path = self.root / AUDIT
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding='utf-8'))

    def entries(self):
        document = self.audit()
        return [] if document is None else document['entries']

    def entry(self, **fields):
        """One audit entry as the reader compares it, without the timestamp it carries."""
        entry = {'operator': None, 'list': 'operators', 'actor': 'someone', 'change': 'add', 'reason': None}
        entry.update(fields)
        return entry

    def shapes(self):
        """The recorded entries without their timestamps, for whole-history comparisons."""
        return [{key: value for key, value in item.items() if key != 'at'} for item in self.entries()]


class RecordingTests(AuthorityChangesCase):
    def test_add_and_remove_record_who_changed_which_list_and_why(self):
        out, err = self.cli('operators', 'add', 'bob', '--actor', 'alice',
                            '--reason', 'second coordinator for the pilot')
        self.assertEqual(json.loads(out), {'operators': [OPERATOR, 'bob']})
        self.assertEqual(err, '')
        self.assertEqual(self.shapes(), [self.entry(operator='alice', actor='bob',
                                                    reason='second coordinator for the pilot')])
        self.assertRegex(self.entries()[0]['at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        self.cli('verifiers', 'add', 'v2', '--actor', 'alice', '--reason', 'new ci host')
        self.cli('verifiers', 'remove', VERIFIER, '--confirm-revoke', '--actor', 'alice',
                 '--reason', 'host retired')
        self.cli('operators', 'remove', 'bob', '--confirm-revoke', '--actor', 'alice',
                 '--reason', 'replaced by carol')
        self.assertEqual([(item['list'], item['actor'], item['change']) for item in self.entries()],
                         [('operators', 'bob', 'add'), ('verifiers', 'v2', 'add'),
                          ('verifiers', VERIFIER, 'remove'), ('operators', 'bob', 'remove')])
        self.assertEqual([item['reason'] for item in self.entries()],
                         ['second coordinator for the pilot', 'new ci host', 'host retired',
                          'replaced by carol'])
        self.assertEqual(self.audit()['schema_version'], admin.AUTHORITY_CHANGES_SCHEMA)
        # The audit answers the design's question (time, operator, name, change, reason) and
        # lives beside deployment.private.json, runtime-level, as the adoption audit does.
        self.assertEqual(set(self.entries()[0]), set(admin.AUTHORITY_CHANGES_FIELDS))
        self.assertTrue((self.root / AUDIT).is_file())

    def test_a_bare_call_keeps_working_and_says_what_to_add(self):
        # The office wrapper's exact call, and the same for verifiers.
        out, err = self.cli('operators', 'add', 'bob')
        self.assertEqual(json.loads(out), {'operators': [OPERATOR, 'bob']})   # unchanged stdout
        self.assertEqual(self.stored()['operators'], [OPERATOR, 'bob'])       # the change applies
        self.assertIn('--actor OPERATOR', err)
        self.assertIn('--reason TEXT', err)
        self.assertIn('unattributed', err)
        self.assertEqual(self.shapes(), [self.entry(actor='bob')])
        out, err = self.cli('verifiers', 'remove', VERIFIER, '--confirm-revoke')
        self.assertEqual(json.loads(out), {'verifiers': []})
        self.assertEqual(self.shapes()[-1], self.entry(list='verifiers', actor=VERIFIER, change='remove'))

    def test_a_half_given_call_names_only_what_is_missing(self):
        _, err = self.cli('operators', 'add', 'bob', '--actor', 'alice')
        self.assertIn('--reason TEXT', err)
        self.assertNotIn('--actor OPERATOR', err)
        self.assertEqual(self.entries()[0]['operator'], 'alice')
        self.assertIsNone(self.entries()[0]['reason'])
        _, err = self.cli('operators', 'add', 'carol', '--reason', 'why not')
        self.assertIn('--actor OPERATOR', err)
        self.assertNotIn('--reason TEXT', err)
        self.assertIsNone(self.entries()[1]['operator'])
        self.assertEqual(self.entries()[1]['reason'], 'why not')

    def test_a_change_that_changes_nothing_records_nothing_and_warns_nothing(self):
        before = self.marker.read_bytes()
        out, err = self.cli('operators', 'add', OPERATOR)
        self.assertEqual(out, json.dumps({'operators': [OPERATOR]}) + '\n')
        self.assertEqual(err, '')
        self.assertIsNone(self.audit())
        out, err = self.cli('operators', 'remove', 'nobody-here', '--confirm-revoke')
        self.assertEqual(json.loads(out), {'operators': [OPERATOR]})
        self.assertEqual(err, '')
        self.assertIsNone(self.audit())
        self.assertEqual(self.marker.read_bytes(), before)

    def test_the_reader_prints_the_history_and_writes_nothing(self):
        self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        self.cli('verifiers', 'add', 'v2')
        before = {path.name: path.read_bytes() for path in sorted(self.root.iterdir()) if path.is_file()}
        out, err = self.cli('authority-changes')
        self.assertEqual(err, '')
        self.assertEqual(json.loads(out), {'schema_version': admin.AUTHORITY_CHANGES_SCHEMA,
                                           'entries': self.entries()})
        after = {path.name: path.read_bytes() for path in sorted(self.root.iterdir()) if path.is_file()}
        self.assertEqual(after, before)                     # read-only: not one byte written

    def test_the_reader_answers_an_empty_history_when_nothing_was_recorded(self):
        out, err = self.cli('authority-changes')
        self.assertEqual(err, '')
        self.assertEqual(json.loads(out), {'schema_version': admin.AUTHORITY_CHANGES_SCHEMA, 'entries': []})
        self.assertFalse((self.root / AUDIT).exists())      # reading never creates the file


class RefusalTests(AuthorityChangesCase):
    def test_removal_still_needs_confirm_revoke_and_only_a_real_removal_is_recorded(self):
        with self.assertRaisesRegex(ValueError, 'confirm-revoke'):
            self.cli('operators', 'remove', OPERATOR, '--actor', 'alice', '--reason', 'gone')
        self.assertIsNone(self.audit())                     # refused before anything was written
        self.assertEqual(self.stored()['operators'], [OPERATOR])

    def test_a_damaged_audit_refuses_every_mutation_before_anything_is_written(self):
        (self.root / AUDIT).write_text('{"schema_version": 1, "entries": [{"at": 1}]}', encoding='utf-8')
        before = self.marker.read_bytes()
        with self.assertRaisesRegex(ValueError, 'authority-changes audit'):
            self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        with self.assertRaisesRegex(ValueError, 'authority-changes audit'):
            self.cli('verifiers', 'add', 'v2')
        with self.assertRaisesRegex(ValueError, 'authority-changes audit'):
            self.cli('authority-changes')                   # the reader refuses too, never "empty"
        self.assertEqual(self.marker.read_bytes(), before)
        self.assertEqual((self.root / AUDIT).read_text(encoding='utf-8'),
                         '{"schema_version": 1, "entries": [{"at": 1}]}')

    def test_a_blank_or_overlong_reason_and_a_bad_operator_are_refused(self):
        for argv, message in ((('operators', 'add', 'bob', '--reason', '   '), 'not blank'),
                              (('operators', 'add', 'bob', '--reason', 'x' * 401), 'at most 400'),
                              (('operators', 'add', 'bob', '--actor', 'not a name!'), 'Invalid operator identity')):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(ValueError, message):
                    self.cli(*argv)
        self.assertIsNone(self.audit())
        self.assertEqual(self.stored()['operators'], [OPERATOR])

    def test_a_reason_is_trimmed_before_it_is_recorded(self):
        self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', '  spaced  ')
        self.assertEqual(self.entries()[0]['reason'], 'spaced')


class HistoryTests(AuthorityChangesCase):
    def test_the_history_is_capped_like_the_adoption_audit(self):
        limit = admin.AUTHORITY_CHANGES_MAX
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': admin.AUTHORITY_CHANGES_SCHEMA,
                        'entries': [dict(self.entry(actor='old-%d' % index),
                                         at='2026-10-01T00:00:%02dZ' % (index % 60))
                                    for index in range(limit)]}),
            encoding='utf-8')
        self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        entries = self.entries()
        self.assertEqual(len(entries), limit)
        self.assertEqual(entries[0]['actor'], 'old-1')       # the oldest entry made room
        self.assertEqual(entries[-1]['actor'], 'bob')
        self.assertNotIn('old-0', [item['actor'] for item in entries])

    def test_a_damaged_entry_shape_is_a_refusal_not_an_empty_history(self):
        entry = self.entry()
        entry.pop('reason')
        (self.root / AUDIT).write_text(json.dumps({'schema_version': 1, 'entries': [entry]}),
                                       encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'not the history this kit writes'):
            self.cli('authority-changes')

    @unittest.skipIf(os.name != 'posix', 'mode bits are POSIX-only')
    def test_the_audit_is_private(self):
        import stat
        self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        self.assertEqual(stat.S_IMODE((self.root / AUDIT).stat().st_mode), 0o600)


class DocumentationTests(unittest.TestCase):
    def test_operations_documents_the_audit_the_reader_and_the_keep_working_rule(self):
        text = (KIT / 'docs' / 'OPERATIONS.md').read_text(encoding='utf-8')
        for phrase in ('authority-changes.audit.json', '`authority-changes`', '--reason TEXT',
                       'coord.sh', '`operators add|remove` and `verifiers add|remove` take'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


if __name__ == '__main__':
    unittest.main()
