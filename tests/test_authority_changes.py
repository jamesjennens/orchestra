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

Revision 2 (review 01a11b49-1b62-70f5-8e28-3ee0f971da2e) adds, and this file holds a test
for each: a damaged audit never refuses a REMOVAL (it is set aside and a fresh history
records that), an ADD on a damaged audit is refused naming the recovery, `restore-new`'s
re-grants are recorded, the reader replays the trail against the current lists and marks the
unattributed entries, a repeated `--actor`/`--reason` is refused, the audit's own temporary
copies are cleaned up, and the entry is written BEFORE the list change (the mutant M03/M04
order), with the check and the entry inside the deployment lock (M07).
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
DEEP = '[' * 100000 + ']' * 100000


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

    def asides(self):
        """The damaged audit files kept beside the runtime, oldest name first."""
        return sorted(self.root.glob(AUDIT + admin.AUTHORITY_CHANGES_DAMAGED + '*'))

    def good_entry(self, **fields):
        entry = {'at': '2026-10-01T00:00:00Z', 'operator': 'alice', 'list': 'operators',
                 'actor': 'bob', 'change': 'add', 'reason': 'r'}
        entry.update(fields)
        return entry


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

    def test_a_bare_verifiers_change_says_what_to_add(self):
        # The verifiers half of the bare form: the sentence must be printed for it too (M11).
        out, err = self.cli('verifiers', 'add', 'v2')
        self.assertEqual(json.loads(out), {'verifiers': [VERIFIER, 'v2']})
        self.assertIn('--actor OPERATOR', err)
        self.assertIn('--reason TEXT', err)
        self.assertIn('unattributed', err)
        _, err = self.cli('verifiers', 'remove', 'v2', '--confirm-revoke')
        self.assertIn('--actor OPERATOR', err)
        self.assertIn('--reason TEXT', err)
        self.assertEqual([item['change'] for item in self.entries()], ['add', 'remove'])

    def test_a_half_given_call_names_only_what_is_missing(self):
        _, err = self.cli('operators', 'add', 'bob', '--actor', 'alice')
        self.assertIn('--reason TEXT', err)
        self.assertNotIn('--actor OPERATOR', err)
        self.assertNotIn('unattributed', err)          # who did it IS recorded: only the why is missing
        self.assertIn('alice', err)                    # the sentence names who
        self.assertEqual(self.entries()[0]['operator'], 'alice')
        self.assertIsNone(self.entries()[0]['reason'])
        _, err = self.cli('operators', 'add', 'carol', '--reason', 'why not')
        self.assertIn('--actor OPERATOR', err)
        self.assertNotIn('--reason TEXT', err)
        self.assertNotIn('unattributed', err)
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

    def test_a_verifiers_change_that_changes_nothing_records_nothing(self):
        # The verifiers half, which the operators-only test did not hold (M16).
        before = self.marker.read_bytes()
        out, err = self.cli('verifiers', 'add', VERIFIER)
        self.assertEqual(out, json.dumps({'verifiers': [VERIFIER]}) + '\n')
        self.assertEqual(err, '')
        self.assertIsNone(self.audit())
        out, err = self.cli('verifiers', 'remove', 'nobody-here', '--confirm-revoke')
        self.assertEqual(json.loads(out), {'verifiers': [VERIFIER]})
        self.assertEqual(err, '')
        self.assertIsNone(self.audit())
        self.assertEqual(self.marker.read_bytes(), before)

    def test_the_entry_is_written_before_the_list_change(self):
        # The claimed order (entry first, list second, both inside the lock): a failure of the
        # configuration write must leave the entry and the old list, never the reverse (M03/M04).
        real = admin.atomic_private_write

        def fail_marker(path, text):
            if Path(path).name == 'deployment.private.json':
                raise OSError(28, 'No space left on device (injected)')
            return real(path, text)

        for argv, noun, actor in ((('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot'),
                                   'operators', 'bob'),
                                  (('verifiers', 'add', 'v2', '--actor', 'alice', '--reason', 'pilot'),
                                   'verifiers', 'v2')):
            with self.subTest(noun=noun):
                if (self.root / AUDIT).exists():
                    (self.root / AUDIT).unlink()
                before = self.marker.read_bytes()
                with patch.object(admin, 'atomic_private_write', side_effect=fail_marker):
                    with self.assertRaises(OSError):
                        self.cli(*argv)
                self.assertEqual(self.marker.read_bytes(), before)     # the list did not change
                self.assertEqual([(item['list'], item['actor'], item['change']) for item in self.entries()],
                                 [(noun, actor, 'add')])               # the entry did

    @unittest.skipIf(os.name != 'posix', 'flock is POSIX-only')
    def test_a_change_refused_by_the_lock_records_nothing(self):
        # The check and the entry are inside the deployment lock (M07): a change another process
        # is holding the lock for must leave no entry at all, not one the change never made.
        import fcntl
        handle = (self.root / admin.REVIEW_WRITES_LOCK).open('a')
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX)
        before = self.marker.read_bytes()
        with patch.object(admin, 'DEPLOYMENT_LOCK_WAIT_SECONDS', 0):
            with self.assertRaisesRegex(ValueError, 'Nothing was changed'):
                self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        self.assertEqual(self.marker.read_bytes(), before)
        self.assertIsNone(self.audit())

    def test_the_reader_prints_the_history_and_writes_nothing(self):
        self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        self.cli('verifiers', 'add', 'v2')
        before = {path.name: path.read_bytes() for path in sorted(self.root.iterdir()) if path.is_file()}
        out, err = self.cli('authority-changes')
        report = json.loads(out)
        self.assertEqual(report['schema_version'], admin.AUTHORITY_CHANGES_SCHEMA)
        self.assertEqual([item['actor'] for item in report['entries']], ['bob', 'v2'])
        self.assertEqual([item['at'] for item in report['entries']],
                         [item['at'] for item in self.entries()])
        after = {path.name: path.read_bytes() for path in sorted(self.root.iterdir()) if path.is_file()}
        self.assertEqual(after, before)                     # read-only: not one byte written
        self.assertIn(report['replay']['note'], err)

    def test_the_reader_does_not_rewrite_the_file_with_the_same_content(self):
        # A byte comparison cannot tell "read" from "rewritten with what it already held": the
        # inode and the modification time can (M13).
        self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        path = self.root / AUDIT
        before_bytes, before_stat = path.read_bytes(), os.stat(path)
        stamp = 946684800                                   # 2000-01-01, a rewrite cannot leave it
        os.utime(path, (stamp, stamp))
        out, _ = self.cli('authority-changes')
        self.assertEqual([item['actor'] for item in json.loads(out)['entries']], ['bob'])
        after = os.stat(path)
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(after.st_ino, before_stat.st_ino)
        self.assertEqual(after.st_mtime_ns, stamp * 10 ** 9)

    def test_the_reader_answers_an_empty_history_when_nothing_was_recorded(self):
        out, err = self.cli('authority-changes')
        report = json.loads(out)
        self.assertEqual(report['schema_version'], admin.AUTHORITY_CHANGES_SCHEMA)
        self.assertEqual(report['entries'], [])
        self.assertEqual(report['unattributed_entries'], 0)
        self.assertEqual(report['current_lists'], {'operators': [OPERATOR], 'verifiers': [VERIFIER]})
        self.assertFalse(report['replay']['agrees'])        # the trail mentions neither name
        self.assertEqual(report['replay']['lists']['operators']['listed_but_not_in_trail'], [OPERATOR])
        self.assertIn('never mentions', err)
        self.assertFalse((self.root / AUDIT).exists())      # reading never creates the file

    def test_the_reader_prints_the_lists_replays_the_trail_and_marks_the_unattributed(self):
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none'}), encoding='utf-8')
        self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        self.cli('verifiers', 'add', 'v2')                   # bare: operator null
        report = json.loads(self.cli('authority-changes')[0])
        self.assertEqual(report['current_lists'], {'operators': ['bob'], 'verifiers': ['v2']})
        self.assertTrue(report['replay']['agrees'])
        self.assertEqual(report['replay']['lists']['operators'],
                         {'agrees': True, 'current': ['bob'], 'trail_expects': ['bob'],
                          'listed_but_last_removed': [], 'listed_but_not_in_trail': [],
                          'trail_added_but_not_listed': []})
        self.assertEqual(report['unattributed_entries'], 1)
        self.assertNotIn('unattributed', report['entries'][0])       # the attributed entry stands out
        self.assertEqual(report['entries'][1]['unattributed'], True)  # this one does not
        self.assertIn('leads to the current lists', report['replay']['note'])

    def test_the_reader_reports_a_trail_that_does_not_lead_to_the_lists(self):
        self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        self.cli('operators', 'remove', 'bob', '--confirm-revoke', '--actor', 'alice', '--reason', 'undone')
        # A hand edit puts bob back: the trail's last word on him is a remove (review finding 2).
        config = self.stored()
        config['operators'].append('bob')
        self.marker.write_text(json.dumps(config), encoding='utf-8')
        report = json.loads(self.cli('authority-changes')[0])
        self.assertFalse(report['replay']['agrees'])
        self.assertEqual(report['replay']['lists']['operators']['listed_but_last_removed'], ['bob'])
        self.assertEqual(report['replay']['lists']['operators']['listed_but_not_in_trail'], [OPERATOR])
        self.assertIn('last recorded change is a remove', report['replay']['note'])
        self.assertIn('hand edit', report['replay']['note'])

    def test_the_change_that_drops_entries_says_so_on_stderr(self):
        limit = admin.AUTHORITY_CHANGES_MAX
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': admin.AUTHORITY_CHANGES_SCHEMA,
                        'entries': [dict(self.entry(actor='old-%d' % index),
                                         at='2026-10-01T00:00:%02dZ' % (index % 60))
                                    for index in range(limit)]}),
            encoding='utf-8')
        _, err = self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        self.assertIn('1 older entry was dropped', err)
        self.assertEqual(len(self.entries()), limit)
        # A hand-written 5000-entry file is cut to the cap by the next change, loudly.
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': admin.AUTHORITY_CHANGES_SCHEMA,
                        'entries': [dict(self.entry(actor='hand-%d' % index),
                                         at='2026-10-01T00:00:%02dZ' % (index % 60))
                                    for index in range(5000)]}),
            encoding='utf-8')
        _, err = self.cli('operators', 'add', 'carol', '--actor', 'alice', '--reason', 'pilot')
        self.assertIn('4801 older entries were dropped', err)
        self.assertEqual(len(self.entries()), limit)

    @unittest.skipIf(os.name != 'posix', 'the cleanup runs only where fcntl exists')
    def test_the_audits_own_temporary_copies_are_removed(self):
        # A kill inside the audit's own write leaves .authority-changes.audit.json.XXXXXXXX; the
        # next locked write removes it, as it already did for deployment.private.json.
        leftover = self.root / ('.%s.abcd1234' % AUDIT)
        leftover.write_text('{}', encoding='utf-8')
        kept = self.root / ('%s.bak' % AUDIT)
        kept.write_text('{}', encoding='utf-8')
        _, err = self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        self.assertFalse(leftover.exists())
        self.assertTrue(kept.exists())
        self.assertIn('temporary copy of %s' % AUDIT, err)


class RefusalTests(AuthorityChangesCase):
    def test_removal_still_needs_confirm_revoke_and_only_a_real_removal_is_recorded(self):
        with self.assertRaisesRegex(ValueError, 'confirm-revoke'):
            self.cli('operators', 'remove', OPERATOR, '--actor', 'alice', '--reason', 'gone')
        self.assertIsNone(self.audit())                     # refused before anything was written
        self.assertEqual(self.stored()['operators'], [OPERATOR])

    def test_a_damaged_audit_refuses_an_add_and_the_reader_and_names_the_recovery(self):
        (self.root / AUDIT).write_text('{"schema_version": 1, "entries": [{"at": 1}]}', encoding='utf-8')
        before = self.marker.read_bytes()
        for argv in (('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot'),
                     ('verifiers', 'add', 'v2', '--actor', 'alice', '--reason', 'pilot'),
                     ('authority-changes',)):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(ValueError, 'not the history this kit writes') as refusal:
                    self.cli(*argv)
                self.assertIn('Move the file aside by hand', str(refusal.exception))
                self.assertIn('.damaged-STAMP', str(refusal.exception))
        self.assertEqual(self.marker.read_bytes(), before)
        self.assertEqual((self.root / AUDIT).read_text(encoding='utf-8'),
                         '{"schema_version": 1, "entries": [{"at": 1}]}')
        self.assertEqual(self.asides(), [])                 # an ADD was refused, nothing set aside

    def test_a_damaged_audit_never_refuses_a_removal(self):
        # The coordinator's decision (review item 1): every damage shape this reviewer used.
        good = self.good_entry()
        shapes = [
            ('not JSON', '{nope'),
            ('empty file', ''),
            ('a list', '[]'),
            ('null', 'null'),
            ('schema 2', json.dumps({'schema_version': 2, 'entries': [good]})),
            ('schema true', json.dumps({'schema_version': True, 'entries': []})),
            ('schema 1.0', json.dumps({'schema_version': 1.0, 'entries': []})),
            ('unknown top-level key', json.dumps({'schema_version': 1, 'entries': [good], 'note': 'x'})),
            ('unknown entry field', json.dumps({'schema_version': 1, 'entries': [dict(good, extra=1)]})),
            ('missing entry field',
             json.dumps({'schema_version': 1, 'entries': [{k: v for k, v in good.items() if k != 'reason'}]})),
            ('100000 levels deep', DEEP),
            ('a BOM', '\ufeff' + json.dumps({'schema_version': 1, 'entries': [good]})),
            ('NaN', '{"schema_version":1,"entries":[NaN]}'),
        ]
        for label, text in shapes:
            with self.subTest(shape=label):
                self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                                   'operators': [OPERATOR, 'compromised'],
                                                   'verifiers': [VERIFIER]}), encoding='utf-8')
                (self.root / AUDIT).write_text(text, encoding='utf-8')
                before = set(self.asides())
                out, err = self.cli('operators', 'remove', 'compromised', '--confirm-revoke',
                                    '--actor', OPERATOR, '--reason', 'key leaked')
                self.assertEqual(json.loads(out), {'operators': [OPERATOR]})   # the removal happened
                self.assertNotIn('compromised', self.stored()['operators'])
                new = set(self.asides()) - before
                self.assertEqual(len(new), 1, label)
                aside = new.pop()                                              # kept beside the runtime
                self.assertEqual(aside.read_text(encoding='utf-8'), text)      # the bytes are kept
                self.assertIn('is damaged', err)
                self.assertIn(aside.name, err)
                entry = self.entries()[0]
                self.assertEqual((entry['list'], entry['actor'], entry['change'], entry['operator']),
                                 ('operators', 'compromised', 'remove', OPERATOR))
                self.assertIn('damaged', entry['reason'])                      # the fresh history records it
                self.assertIn('kept beside the runtime', entry['reason'])
                self.assertIn('key leaked', entry['reason'])
                self.assertEqual(len(self.entries()), 1)

    def test_a_damaged_verifiers_audit_never_refuses_a_verifiers_removal(self):
        (self.root / AUDIT).write_text('{nope', encoding='utf-8')
        out, err = self.cli('verifiers', 'remove', VERIFIER, '--confirm-revoke',
                            '--actor', OPERATOR, '--reason', 'host retired')
        self.assertEqual(json.loads(out), {'verifiers': []})
        self.assertEqual(self.entries()[0]['list'], 'verifiers')
        self.assertIn('damaged', self.entries()[0]['reason'])
        self.assertEqual(len(self.asides()), 1)

    def test_an_audit_that_cannot_be_read_at_all_is_set_aside_too(self):
        cases = [('non-UTF-8 bytes', b'{"schema_version":1,"entries":[],"x":"\xe9\xff"}')]
        if os.name == 'posix':          # mode 000 is a POSIX permission, not a Windows attribute
            cases.append(('mode 000', None))
        for label, raw in cases:
            with self.subTest(shape=label):
                self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                                   'operators': [OPERATOR, 'compromised']}), encoding='utf-8')
                path = self.root / AUDIT
                if raw is None:
                    path.write_text(json.dumps({'schema_version': 1, 'entries': []}), encoding='utf-8')
                    os.chmod(path, 0)
                    self.addCleanup(lambda: os.chmod(path, 0o600) if path.exists() else None)
                else:
                    path.write_bytes(raw)
                before = set(self.asides())
                out, err = self.cli('operators', 'remove', 'compromised', '--confirm-revoke',
                                    '--actor', OPERATOR, '--reason', 'key leaked')
                self.assertNotIn('compromised', self.stored()['operators'], label)
                new = set(self.asides()) - before
                self.assertEqual(len(new), 1, label)
                self.assertIn('is damaged', err)
                if raw is not None:
                    self.assertEqual(new.pop().read_bytes(), raw)

    def test_an_audit_path_that_is_not_a_regular_file_is_one_refusal(self):
        path = self.root / AUDIT
        os.mkdir(path)
        for argv in (('authority-changes',), ('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'r'),
                     ('operators', 'remove', OPERATOR, '--confirm-revoke')):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(ValueError, 'not a regular file') as refusal:
                    self.cli(*argv)
                self.assertNotIn('Traceback', str(refusal.exception))
        self.assertTrue(path.is_dir())
        os.rmdir(path)

    @unittest.skipIf(os.name != 'posix', 'symlinks and fifos are POSIX-only')
    def test_a_symlink_or_fifo_audit_path_is_one_refusal_and_is_not_replaced(self):
        path = self.root / AUDIT
        (self.root / 'elsewhere.json').write_text('{"schema_version": 1, "entries": []}', encoding='utf-8')
        os.symlink(self.root / 'elsewhere.json', path)
        with self.assertRaisesRegex(ValueError, 'not a regular file'):
            self.cli('authority-changes')
        with self.assertRaisesRegex(ValueError, 'not a regular file'):
            self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'r')
        self.assertTrue(path.is_symlink())                  # never replaced by a regular file
        os.unlink(path)
        os.symlink(self.root / 'nowhere.json', path)        # dangling
        with self.assertRaisesRegex(ValueError, 'not a regular file'):
            self.cli('authority-changes')
        self.assertTrue(path.is_symlink())
        os.unlink(path)
        os.mkfifo(path)
        with self.assertRaisesRegex(ValueError, 'not a regular file'):
            self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'r')
        import stat as stat_module
        self.assertTrue(stat_module.S_ISFIFO(os.lstat(path).st_mode))
        os.unlink(path)

    def test_a_repeated_actor_or_reason_is_refused(self):
        for argv in (('operators', 'add', 'bob', '--actor', 'a', '--actor', 'b', '--reason', 'r'),
                     ('operators', 'add', 'bob', '--actor', 'a', '--reason', 'one', '--reason', 'two'),
                     ('verifiers', 'add', 'v2', '--actor', 'a', '--actor', 'b')):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(ValueError, 'was given more than once'):
                    self.cli(*argv)
        self.assertIsNone(self.audit())
        self.assertEqual(self.stored()['operators'], [OPERATOR])

    def test_abbreviations_are_off_and_the_pre_existing_ones_still_work(self):
        for argv in (('operators', 'add', 'bob', '--act', 'a', '--reason', 'r'),
                     ('operators', 'add', 'bob', '--actor', 'a', '--reas', 'r')):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as refused:
                    self.cli(*argv)
                self.assertEqual(refused.exception.code, 2)
        self.assertIsNone(self.audit())
        # The abbreviations an existing call used still work: --a and --all mean --all-revoked,
        # --confirm means --confirm-revoke.
        out, err = self.cli('operators', 'remove', OPERATOR, '--confirm', '--a',
                            '--actor', 'alice', '--reason', 'the abbreviation still works')
        self.assertEqual(json.loads(out), {'operators': []})
        self.assertEqual(err, '')
        self.cli('verifiers', 'remove', VERIFIER, '--confirm')
        self.assertEqual([item['change'] for item in self.entries()], ['remove', 'remove'])

    def test_the_recording_flags_are_refused_on_list(self):
        for argv in (('operators', 'list', '--actor', 'a'),
                     ('operators', 'list', '--reason', 'r'),
                     ('verifiers', 'list', '--actor', 'a', '--reason', 'r')):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(ValueError, 'takes no'):
                    self.cli(*argv)
        self.assertIsNone(self.audit())
        out, err = self.cli('operators', 'list')
        self.assertEqual(json.loads(out), {'operators': [OPERATOR]})
        self.assertEqual(err, '')

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

    def test_an_unknown_entry_field_is_refused(self):
        # A file with a field this kit does not write is not this kit's history (M27).
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': 1, 'entries': [dict(self.good_entry(), extra=1)]}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'not the history this kit writes'):
            self.cli('authority-changes')

    def test_an_unknown_top_level_key_or_a_non_stamp_at_is_refused(self):
        good = self.good_entry()
        cases = (('an unknown top-level key', {'schema_version': 1, 'entries': [good], 'note': 'x'}),
                 ('at is not a stamp', {'schema_version': 1, 'entries': [dict(good, at='yesterday')]}),
                 ('at is missing', {'schema_version': 1, 'entries': [{k: v for k, v in good.items() if k != 'at'}]}))
        for label, document in cases:
            with self.subTest(case=label):
                (self.root / AUDIT).write_text(json.dumps(document), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'not the history this kit writes'):
                    self.cli('authority-changes')

    def test_another_schema_version_is_refused_not_read_as_this_one(self):
        # A later kit's file, or a hand-set true/1.0/"1": none of them is this history (M26).
        for version in (2, '1', True, 1.0, None, 3):
            with self.subTest(version=version):
                (self.root / AUDIT).write_text(
                    json.dumps({'schema_version': version, 'entries': []}), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'not the history this kit writes'):
                    self.cli('authority-changes')
                with self.assertRaisesRegex(ValueError, 'not the history this kit writes'):
                    self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
                self.assertEqual(self.stored()['operators'], [OPERATOR])

    @unittest.skipIf(os.name != 'posix', 'mode bits are POSIX-only')
    def test_the_audit_is_private(self):
        import stat
        self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        self.assertEqual(stat.S_IMODE((self.root / AUDIT).stat().st_mode), 0o600)


class RestoreRegrantTests(AuthorityChangesCase):
    """Review item 2: the one route that undoes a revocation is recorded in the same audit."""

    def test_restore_authority_records_every_regranted_name(self):
        stream = io.StringIO()
        with patch.object(admin, 'missing_operators', return_value=['op1']), \
                patch.object(admin, 'missing_verifiers', return_value=['v1']), \
                contextlib.redirect_stdout(stream):
            warning = admin.restore_authority(self.root, 'lm', restore_operators=True,
                                              restore_verifiers=True, actor='james', reason='after a rollback')
        self.assertIsNone(warning)
        self.assertEqual(self.stored()['operators'], [OPERATOR, 'op1'])
        self.assertEqual(self.stored()['verifiers'], [VERIFIER, 'v1'])
        self.assertEqual(self.shapes(), [
            self.entry(operator='james', actor='op1', reason='restore-new lm: after a rollback'),
            self.entry(operator='james', list='verifiers', actor='v1',
                       reason='restore-new lm: after a rollback')])
        self.assertIn('Re-granted operator allowlist entries', stream.getvalue())

    def test_a_regrant_without_actor_records_null_and_still_names_restore_new(self):
        with patch.object(admin, 'missing_operators', return_value=['op1']), \
                patch.object(admin, 'missing_verifiers', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            admin.restore_authority(self.root, 'lm', restore_operators=True)
        self.assertEqual(self.entries()[0]['operator'], None)
        self.assertEqual(self.entries()[0]['reason'],
                         'restore-new lm re-granted this name from the backup '
                         '(--restore-operators/--restore-verifiers)')

    def test_a_damaged_audit_refuses_the_regrant_and_leaves_the_restore_complete(self):
        (self.root / AUDIT).write_text('{nope', encoding='utf-8')
        before = self.marker.read_bytes()
        with patch.object(admin, 'missing_operators', return_value=['op1']), \
                patch.object(admin, 'missing_verifiers', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            warning = admin.restore_authority(self.root, 'lm', restore_operators=True,
                                              actor='james', reason='after a rollback')
        self.assertIn('was NOT re-granted', warning)
        self.assertIn('authority-changes audit', warning)
        self.assertIn('restore-new exits %d' % admin.RESTORE_AUTHORITY_NOT_REGRANTED, warning)
        self.assertEqual(self.marker.read_bytes(), before)          # nothing was re-granted
        self.assertEqual((self.root / AUDIT).read_text(encoding='utf-8'), '{nope')

    def test_merge_operators_and_merge_verifiers_record_too(self):
        # The kit's other two list writers, so the capability's name is true for every route.
        self.assertEqual(admin.merge_operators(self.root, ['op1']), ['op1'])
        self.assertEqual(admin.merge_verifiers(self.root, ['v1']), ['v1'])
        self.assertEqual([(item['list'], item['actor'], item['change']) for item in self.entries()],
                         [('operators', 'op1', 'add'), ('verifiers', 'v1', 'add')])
        self.assertIn('merge_operators', self.entries()[0]['reason'])

    def test_restore_new_refuses_actor_or_reason_without_a_restore_flag(self):
        # The flags would record nothing without --restore-operators/--restore-verifiers.
        with self.assertRaisesRegex(ValueError, 'record nothing'):
            self.cli('restore-new', 'lm', 'lmr', '--actor', 'james', '--reason', 'r')


class DocumentationTests(unittest.TestCase):
    def test_operations_documents_the_audit_the_reader_and_the_keep_working_rule(self):
        text = (KIT / 'docs' / 'OPERATIONS.md').read_text(encoding='utf-8')
        for phrase in ('authority-changes.audit.json', '`authority-changes`', '--reason TEXT',
                       'coord.sh', '`operators add|remove` and `verifiers add|remove` take',
                       'authority-changes.audit.json.damaged-<UTC date-time>',
                       'NEVER refused for it', 'restore-new SRC DST --restore-operators',
                       'not a regular file', 'What the audit cannot see'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_the_recipes_show_the_recording_flags(self):
        operations = (KIT / 'docs' / 'OPERATIONS.md').read_text(encoding='utf-8')
        reviews = (KIT / 'docs' / 'REVIEWS.md').read_text(encoding='utf-8')
        self.assertNotIn('operators add OPERATOR\n', operations)
        self.assertIn('operators add OPERATOR --actor OPERATOR --reason', operations)
        self.assertIn('operators add NAME --actor OPERATOR --reason', reviews)


if __name__ == '__main__':
    unittest.main()
