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
for each: a damaged audit never refuses a REMOVAL (it is set beside the runtime and a fresh
history records that), an ADD on a damaged audit is refused naming the recovery, `restore-new`'s
re-grants are recorded, the reader replays the trail against the current lists and marks the
unattributed entries, a repeated `--actor`/`--reason` is refused, the audit's own temporary
copies are cleaned up, and the entry is written BEFORE the list change (the mutant M03/M04
order), with the check and the entry inside the deployment lock (M07).

Revision 3 (review 01a11bd2-ff1a-7abf-9462-d05f3964336f) adds the four round-2 items:

1. every NEW history begins with a `baseline` holding the lists as they stand, so the reader
   replays from a known state and a listed-but-never-mentioned name really is a hand edit or an
   older kit; the cap carries a fresh baseline forward instead of losing names, and at exactly
   200 entries nothing is claimed to have been dropped. With no trail at all the reader says so,
   prints the lists and warns about nothing (`replay.agrees` null).
2. the damaged bytes are put at the `.damaged-*` name FIRST (hard link, or a copy) and the new
   history replaces the audit path, so the path is never empty: a failed write at exactly that
   point is tested, and the retry reuses the aside. `.damaged-*` files are never removed and the
   reader lists them.
3. `restore-new --actor/--reason` refuse a repeat, refuse an abbreviation, and print the one
   stderr sentence when the re-grant is unattributed; the commands a refused restore prints carry
   the flags.
4. a no-op ADD on a damaged audit is NOT refused (the check before the lock knows whether
   anything would change) and refuses nothing else either; the recovery sentence prints a real
   `.damaged-<stamp>`; the grammar is fixed; the widened leftover cleanup, the verifiers
   abbreviations and the capped trail are held by tests.
"""
import contextlib
import io
import json
import os
import re
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
STAMP = re.compile(r'\.damaged-\d{8}T\d{6}Z(\.\d+)?$')


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

    def baseline(self):
        """The baseline the trail begins with, or None when it has none."""
        document = self.audit()
        return None if document is None else document.get('baseline')

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

    def good_baseline(self, **fields):
        baseline = {'at': '2026-10-01T00:00:00Z', 'operator': 'alice', 'reason': 'baseline: a test',
                    'lists': {'operators': [OPERATOR], 'verifiers': [VERIFIER]}}
        baseline.update(fields)
        return baseline


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
        # The history BEGINS with a baseline of the lists as they stood (round-2 review item 1):
        # this installation listed OPERATOR and VERIFIER before the first change.
        self.assertEqual(set(self.baseline()), set(admin.AUTHORITY_CHANGES_BASELINE_FIELDS))
        self.assertEqual(self.baseline()['lists'], {'operators': [OPERATOR], 'verifiers': [VERIFIER]})
        self.assertEqual(self.baseline()['operator'], 'alice')
        self.assertIn('baseline', self.baseline()['reason'])

    def test_the_first_change_on_an_installation_from_before_this_kit_baselines_the_lists(self):
        # The reviewer's finding 1: a runtime written by the release before, one operator listed
        # and no audit at all. The first change starts a history from a KNOWN state, so the two
        # names that were already there are in the trail and the reader does not cry wolf.
        self.assertEqual(self.audit(), None)
        self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        self.assertEqual(self.baseline()['lists'], {'operators': [OPERATOR], 'verifiers': [VERIFIER]})
        report = json.loads(self.cli('authority-changes')[0])
        self.assertTrue(report['replay']['agrees'])
        self.assertEqual(report['replay']['lists']['operators']['listed_but_not_in_trail'], [])
        self.assertEqual(report['replay']['lists']['operators']['trail_expects'], ['bob', OPERATOR])
        self.assertEqual(report['unattributed_entries'], 0)
        self.assertIn('leads to the current lists', report['replay']['note'])
        self.assertNotIn('WARNING', report['replay']['note'])
        # A no-op add of the name written by the release before writes no entry and no new history.
        before = (self.root / AUDIT).read_bytes()
        self.cli('operators', 'add', OPERATOR)
        self.assertEqual((self.root / AUDIT).read_bytes(), before)
        self.assertTrue(json.loads(self.cli('authority-changes')[0])['replay']['agrees'])

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

    def test_the_reader_says_there_is_no_trail_yet_and_warns_about_nothing(self):
        # Round-2 review item 1, the coordinator's decision: with no audit file at all, say there
        # is no trail yet and print the lists - no warning. The exit code is 0 either way, which
        # is why a script reads replay.agrees instead.
        out, err = self.cli('authority-changes')
        report = json.loads(out)
        self.assertEqual(report['schema_version'], admin.AUTHORITY_CHANGES_SCHEMA)
        self.assertEqual(report['entries'], [])
        self.assertEqual(report['baseline'], None)
        self.assertEqual(report['unattributed_entries'], 0)
        self.assertEqual(report['current_lists'], {'operators': [OPERATOR], 'verifiers': [VERIFIER]})
        self.assertIsNone(report['replay']['agrees'])            # no trail: neither true nor false
        self.assertEqual(report['replay']['state'], 'no-trail')
        self.assertIn('No trail yet', report['replay']['note'])
        self.assertNotIn('WARNING', report['replay']['note'])
        self.assertNotIn('never mentions', err)
        self.assertFalse((self.root / AUDIT).exists())          # reading never creates the file

    def test_the_reader_reports_a_mismatch_once_the_baseline_is_wrong_too(self):
        # A hand-started history with no baseline still replays, and says the trail is not known
        # to be whole: the file may simply be older than the lists.
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': 1, 'entries': [self.good_entry(actor='legacy')]}), encoding='utf-8')
        report = json.loads(self.cli('authority-changes')[0])
        self.assertFalse(report['replay']['agrees'])
        self.assertEqual(report['replay']['state'], 'mismatch')
        self.assertEqual(report['baseline'], None)
        self.assertIn('does not begin with a baseline', report['replay']['note'])
        self.assertEqual(report['replay']['lists']['operators']['listed_but_not_in_trail'], [OPERATOR])

    def test_an_empty_history_file_reads_as_no_trail_too(self):
        (self.root / AUDIT).write_text(json.dumps({'schema_version': 1, 'entries': []}), encoding='utf-8')
        report = json.loads(self.cli('authority-changes')[0])
        self.assertEqual(report['replay']['state'], 'no-trail')
        self.assertIsNone(report['replay']['agrees'])
        self.assertIn('No trail yet', report['replay']['note'])

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
                          'trail_added_but_not_listed': [], 'listed_more_than_once': []})
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
        self.assertEqual(report['replay']['state'], 'mismatch')
        self.assertEqual(report['replay']['lists']['operators']['listed_but_last_removed'], ['bob'])
        # OPERATOR was listed before the trail began, so the BASELINE mentions it: a name the
        # trail never mentions now really is one nobody recorded (round-2 review item 1).
        self.assertEqual(report['replay']['lists']['operators']['listed_but_not_in_trail'], [])
        self.assertIn('last recorded change is a remove', report['replay']['note'])
        self.assertIn('hand edit', report['replay']['note'])
        # A name added to the file by hand after the baseline IS the mismatch it should be.
        config = self.stored()
        config['operators'].append('sneaked')
        self.marker.write_text(json.dumps(config), encoding='utf-8')
        report = json.loads(self.cli('authority-changes')[0])
        self.assertEqual(report['replay']['lists']['operators']['listed_but_not_in_trail'], ['sneaked'])
        self.assertIn('sneaked', report['replay']['note'])

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
        # next locked write removes it, as it already did for deployment.private.json. The
        # adoption and review-writes audits are written the same way under the same lock, so
        # their leftovers go too (round-2 review item 4 / mutant N16b).
        leftovers = {}
        for target in (AUDIT, admin.ACTOR_ADOPTIONS_AUDIT, admin.REVIEW_WRITES_AUDIT):
            leftovers[target] = self.root / ('.%s.abcd1234' % target)
            leftovers[target].write_text('{}', encoding='utf-8')
        kept = self.root / ('%s.bak' % AUDIT)
        kept.write_text('{}', encoding='utf-8')
        _, err = self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        for target, path in leftovers.items():
            with self.subTest(target=target):
                self.assertFalse(path.exists())
                self.assertIn('temporary copy of %s' % target, err)
        self.assertTrue(kept.exists())


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
                # A REAL stamp, not the literal word STAMP (round-2 review item 4): the command as
                # printed can be followed, and following it twice cannot overwrite the first file.
                self.assertNotIn('STAMP', str(refusal.exception))
                self.assertRegex(str(refusal.exception), r'mv \S+ \S+\.damaged-\d{8}T\d{6}Z')
        # The grammar the reviewer named: the path is followed by a verb, never by "it cannot be
        # read" or by another subject (round-2 review item 4).
        with self.assertRaises(ValueError) as refusal:
            self.cli('authority-changes')
        self.assertIn('The authority-changes audit %s is not the history' % (self.root / AUDIT),
                      str(refusal.exception))
        self.assertNotIn('audit %s it ' % (self.root / AUDIT), str(refusal.exception))
        self.assertEqual(self.marker.read_bytes(), before)
        self.assertEqual((self.root / AUDIT).read_text(encoding='utf-8'),
                         '{"schema_version": 1, "entries": [{"at": 1}]}')
        self.assertEqual(self.asides(), [])                 # an ADD was refused, nothing set aside

    def test_a_no_op_add_on_a_damaged_audit_is_not_refused(self):
        # Round-2 review item 4: the release before this audit exits 0 for a no-op `operators add`
        # (the name is already listed), so a wrapper that re-runs its bare operators must not
        # start failing. Only an add that WOULD change the list is refused - and that refusal
        # happens BEFORE the lock, so it costs nothing at all: no lock file, nothing written.
        (self.root / AUDIT).write_text('{nope', encoding='utf-8')
        before_marker = self.marker.read_bytes()
        self.assertFalse((self.root / admin.REVIEW_WRITES_LOCK).exists())
        # An add that WOULD change either list is refused, and without taking the lock at all
        # (M24/M25): the pre-lock check is what makes that refusal cost nothing.
        for argv in (('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot'),
                     ('verifiers', 'add', 'v2', '--actor', 'alice', '--reason', 'pilot')):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(ValueError, 'Move the file aside by hand') as refusal:
                    self.cli(*argv)
                self.assertIn('nothing was changed', str(refusal.exception))
                self.assertFalse((self.root / admin.REVIEW_WRITES_LOCK).exists())
                self.assertEqual(self.marker.read_bytes(), before_marker)
                self.assertEqual(self.asides(), [])
        # The requests that change nothing are not refused at all: rc 0, the list printed, no
        # sentence, and the damaged file untouched.
        for argv, noun in ((('operators', 'add', OPERATOR), 'operators'),
                           (('verifiers', 'add', VERIFIER), 'verifiers'),
                           (('operators', 'remove', 'nobody-here', '--confirm-revoke'), 'operators'),
                           (('verifiers', 'remove', 'nobody-here', '--confirm-revoke'), 'verifiers')):
            with self.subTest(argv=argv):
                out, err = self.cli(*argv)
                self.assertEqual(json.loads(out), {noun: self.stored()[noun]})
                self.assertEqual(err, '')                   # nothing changed, so no notice either
        self.assertEqual(self.marker.read_bytes(), before_marker)
        self.assertEqual((self.root / AUDIT).read_text(encoding='utf-8'), '{nope')
        self.assertEqual(self.asides(), [])
        # The reader's own refusal has the fixed grammar too, and a read says nothing was READ.
        with self.assertRaises(ValueError) as refusal:
            self.cli('authority-changes')
        self.assertIn('The authority-changes audit %s cannot be read:' % (self.root / AUDIT),
                      str(refusal.exception))
        self.assertIn('nothing was read', str(refusal.exception))

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
                # The fresh history also CARRIES A BASELINE of the lists as they stood before the
                # removal - the two names that remain are in the trail, so the reader does not call
                # them a hand edit (round-2 review item 1) - and the FIRST record names the aside.
                self.assertEqual(self.baseline()['lists'],
                                 {'operators': sorted([OPERATOR, 'compromised']), 'verifiers': [VERIFIER]})
                self.assertIn(aside.name, self.baseline()['reason'])
                report = json.loads(self.cli('authority-changes')[0])
                self.assertTrue(report['replay']['agrees'], label)
                self.assertEqual(report['replay']['lists']['operators']['listed_but_not_in_trail'], [])
                self.assertIn(aside.name, report['damaged_files'])
                self.assertIn(aside.name, report['replay']['note'])

    def test_a_damaged_audit_never_refuses_a_verifiers_removal(self):
        (self.root / AUDIT).write_text('{nope', encoding='utf-8')
        out, err = self.cli('verifiers', 'remove', VERIFIER, '--confirm-revoke',
                            '--actor', OPERATOR, '--reason', 'host retired')
        self.assertEqual(json.loads(out), {'verifiers': []})
        self.assertEqual(self.entries()[0]['list'], 'verifiers')
        self.assertIn('damaged', self.entries()[0]['reason'])
        self.assertEqual(len(self.asides()), 1)
        self.assertEqual(self.baseline()['lists'], {'operators': [OPERATOR], 'verifiers': [VERIFIER]})
        self.assertIn(self.asides()[0].name, self.baseline()['reason'])

    def test_the_audit_path_is_never_empty_when_the_new_history_cannot_be_written(self):
        # Round-2 review item 2: the bytes go to the aside name FIRST and the atomic write of the
        # new history replaces the path. A failed write at exactly that point must leave the
        # damaged file where it was - never an absent path that reads as an empty history.
        path = self.root / AUDIT
        path.write_text('{nope', encoding='utf-8')
        real = admin.atomic_private_write

        def fail_audit(target, text):
            if Path(target).name == AUDIT:
                raise OSError(28, 'No space left on device (injected)')
            return real(target, text)

        with patch.object(admin, 'atomic_private_write', side_effect=fail_audit):
            with self.assertRaises(OSError):
                self.cli('operators', 'remove', OPERATOR, '--confirm-revoke',
                         '--actor', OPERATOR, '--reason', 'key leaked')
        self.assertTrue(path.is_file())                       # the path is never absent
        self.assertEqual(path.read_text(encoding='utf-8'), '{nope')
        self.assertIn(OPERATOR, self.stored()['operators'])   # nothing was removed either
        kept = self.asides()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(encoding='utf-8'), '{nope')
        # The path still holds the damaged bytes, so the reader REFUSES rather than answering an
        # empty history: an absent path is exactly what this fix removes.
        with self.assertRaisesRegex(ValueError, 'cannot be read:'):
            self.cli('authority-changes')
        # The retry reuses that name - no pile of copies of the same bytes - and the fresh history
        # names it in its first record.
        out, err = self.cli('operators', 'remove', OPERATOR, '--confirm-revoke',
                            '--actor', OPERATOR, '--reason', 'key leaked')
        self.assertEqual(json.loads(out), {'operators': []})
        self.assertEqual(self.asides(), kept)
        self.assertIn(kept[0].name, self.baseline()['reason'])
        self.assertEqual(kept[0].read_text(encoding='utf-8'), '{nope')

    def test_the_bytes_are_copied_when_the_filesystem_has_no_hard_links(self):
        # os.link is the first choice; a filesystem without it gets a plain copy, and the path is
        # still never absent (the copy is made BEFORE the new history is written).
        path = self.root / AUDIT
        path.write_text('{nope', encoding='utf-8')
        with patch.object(admin.os, 'link', side_effect=OSError(1, 'Operation not permitted (injected)')):
            out, err = self.cli('operators', 'remove', OPERATOR, '--confirm-revoke',
                                '--actor', OPERATOR, '--reason', 'key leaked')
        self.assertEqual(json.loads(out), {'operators': []})
        kept = self.asides()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(encoding='utf-8'), '{nope')
        self.assertFalse(admin.os.path.samefile(path, kept[0]))     # a copy, not the same inode
        self.assertIn(kept[0].name, self.baseline()['reason'])
        self.assertTrue(json.loads(self.cli('authority-changes')[0])['replay']['agrees'])

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
        # Both commands, both flags: the verifiers half was not held before (round-2 review item 4,
        # mutant N19b).
        for argv in (('operators', 'add', 'bob', '--act', 'a', '--reason', 'r'),
                     ('operators', 'add', 'bob', '--actor', 'a', '--reas', 'r'),
                     ('verifiers', 'add', 'v2', '--act', 'a', '--reason', 'r'),
                     ('verifiers', 'add', 'v2', '--actor', 'a', '--reas', 'r'),
                     ('verifiers', 'add', 'v2', '--acto', 'a', '--reaso', 'r')):
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
    def capped(self, count, actor='old-%d'):
        """A hand-written capped trail of ``count`` adds, with no baseline."""
        return {'schema_version': admin.AUTHORITY_CHANGES_SCHEMA,
                'entries': [dict(self.entry(actor=actor % index), at='2026-10-01T00:00:%02dZ' % (index % 60))
                            for index in range(count)]}

    def test_the_history_is_capped_like_the_adoption_audit(self):
        limit = admin.AUTHORITY_CHANGES_MAX
        (self.root / AUDIT).write_text(json.dumps(self.capped(limit)), encoding='utf-8')
        self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        entries = self.entries()
        self.assertEqual(len(entries), limit)
        self.assertEqual(entries[0]['actor'], 'old-1')       # the oldest entry made room
        self.assertEqual(entries[-1]['actor'], 'bob')
        self.assertNotIn('old-0', [item['actor'] for item in entries])
        # What the dropped entry recorded is CARRIED FORWARD in the baseline, not lost: old-0 is
        # still a listed name the trail knows about (round-2 review item 1).
        self.assertIn('old-0', self.baseline()['lists']['operators'])
        self.assertIn('cut to its %d-entry cap' % limit, self.baseline()['reason'])
        self.assertEqual(self.baseline()['operator'], 'alice')

    def test_a_change_at_the_cap_does_not_make_the_reader_cry_mismatch(self):
        # The reviewer's own 201-change case: the oldest entry is dropped by the cap, and the trail
        # must still lead to the lists. It used to read `listed_but_not_in_trail: <the oldest>`.
        limit = admin.AUTHORITY_CHANGES_MAX
        names = ['n%03d' % index for index in range(limit - 1)]
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none', 'operators': names}),
                               encoding='utf-8')
        entries = [dict(self.entry(actor=name), at='2026-10-01T00:00:%02dZ' % (index % 60))
                   for index, name in enumerate(names)]
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': admin.AUTHORITY_CHANGES_SCHEMA, 'entries': entries}),
            encoding='utf-8')
        # `u1` fills the trail to the cap exactly: nothing dropped yet, and the reader may not
        # claim that anything was dropped (round-2 review item 1).
        _, err = self.cli('operators', 'add', 'u1', '--actor', 'alice', '--reason', 'pilot')
        self.assertEqual(err, '')
        self.assertEqual(len(self.entries()), limit)
        report = json.loads(self.cli('authority-changes')[0])
        self.assertTrue(report['replay']['agrees'])
        self.assertIn('cap of %d entries' % limit, report['replay']['note'])
        self.assertNotIn('dropped', report['replay']['note'])
        self.assertNotIn('WARNING', report['replay']['note'])
        # The 201st change drops the oldest entry into the baseline: still no mismatch.
        _, err = self.cli('operators', 'add', 'u2', '--actor', 'alice', '--reason', 'pilot')
        self.assertIn('1 older entry was dropped', err)
        self.assertIn('folded into the baseline', err)
        self.assertEqual(len(self.entries()), limit)
        self.assertIn(names[0], self.baseline()['lists']['operators'])
        report = json.loads(self.cli('authority-changes')[0])
        self.assertTrue(report['replay']['agrees'])
        self.assertEqual(report['replay']['lists']['operators']['listed_but_not_in_trail'], [])
        self.assertNotIn('WARNING', report['replay']['note'])
        # This trail never had a baseline (it was hand-written): the reader says so plainly, and
        # after the cap carried one forward it no longer needs to.
        self.assertIn('cap of %d entries' % limit, report['replay']['note'])

    def test_a_bad_baseline_is_not_this_history(self):
        for label, baseline in (('a missing field', {k: v for k, v in self.good_baseline().items()
                                                     if k != 'reason'}),
                                ('an unknown field', dict(self.good_baseline(), extra=1)),
                                ('an at that is not a stamp', dict(self.good_baseline(), at='yesterday')),
                                ('a list that is not a list', dict(self.good_baseline(), lists={'operators': 'ops',
                                                                                                'verifiers': []})),
                                ('an unknown list', dict(self.good_baseline(), lists={'operators': [], 'x': []})),
                                ('a blank name', dict(self.good_baseline(), lists={'operators': [''], 'verifiers': []}))):
            with self.subTest(case=label):
                (self.root / AUDIT).write_text(
                    json.dumps({'schema_version': 1, 'baseline': baseline, 'entries': []}), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'baseline is not one this kit writes'):
                    self.cli('authority-changes')

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
        err = io.StringIO()
        with patch.object(admin, 'missing_operators', return_value=['op1']), \
                patch.object(admin, 'missing_verifiers', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            admin.restore_authority(self.root, 'lm', restore_operators=True)
        self.assertEqual(self.entries()[0]['operator'], None)
        self.assertEqual(self.entries()[0]['reason'],
                         'restore-new lm re-granted this name from the backup '
                         '(--restore-operators/--restore-verifiers)')
        # Round-2 review item 3: a bare re-grant prints the same one sentence the four list
        # commands print, so it is never silently unattributed.
        self.assertIn('WARNING', err.getvalue())
        self.assertIn('--actor OPERATOR', err.getvalue())
        self.assertIn('--reason TEXT', err.getvalue())
        self.assertIn('unattributed', err.getvalue())

    def test_a_regrant_that_names_who_and_why_prints_no_sentence(self):
        err = io.StringIO()
        with patch.object(admin, 'missing_operators', return_value=['op1']), \
                patch.object(admin, 'missing_verifiers', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            admin.restore_authority(self.root, 'lm', restore_operators=True,
                                    actor='james', reason='after a rollback')
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(self.entries()[0]['operator'], 'james')

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
        # The commands it prints carry the recording flags (round-2 review item 3), with what the
        # restore was given where it had it.
        self.assertIn('operators add op1 --actor james --reason ', warning)
        self.assertEqual(self.marker.read_bytes(), before)          # nothing was re-granted
        self.assertEqual((self.root / AUDIT).read_text(encoding='utf-8'), '{nope')

    def test_the_regrant_commands_a_refused_restore_prints_are_never_bare(self):
        # Round-2 review item 3: the bare form records a null operator and prints the warning, so
        # the remedy would leave the audit less complete than the restore tried to.
        self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        warning = admin.authority_not_regranted(self.root, 'lm', ['op1'], ['v1'])
        self.assertIn('operators add op1 --actor OPERATOR --reason TEXT', warning)
        self.assertIn('verifiers add v1 --actor OPERATOR --reason TEXT', warning)
        with patch.object(admin, 'missing_operators', return_value=['op1']), \
                patch.object(admin, 'missing_verifiers', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            warning = admin.authority_not_regranted(self.root, 'lm', ['op1'], [], actor='james',
                                                    reason='a rollback')
        self.assertIn("operators add op1 --actor james --reason 'a rollback'", warning)

    def test_merge_operators_and_merge_verifiers_record_too(self):
        # The kit's other two list writers, so the capability's name is true for every route. They
        # have no caller in the kit (restore-new uses merge_authority for one lock wait); they stay
        # because the lock matrix and the recovery tests hold them directly (round-2 review item 4).
        self.assertEqual(admin.merge_operators(self.root, ['op1']), ['op1'])
        self.assertEqual(admin.merge_verifiers(self.root, ['v1']), ['v1'])
        self.assertEqual([(item['list'], item['actor'], item['change']) for item in self.entries()],
                         [('operators', 'op1', 'add'), ('verifiers', 'v1', 'add')])
        self.assertIn('merge_operators', self.entries()[0]['reason'])

    def test_restore_new_refuses_actor_or_reason_without_a_restore_flag(self):
        # The flags would record nothing without --restore-operators/--restore-verifiers.
        with self.assertRaisesRegex(ValueError, 'record nothing'):
            self.cli('restore-new', 'lm', 'lmr', '--actor', 'james', '--reason', 'r')


class RestoreNewFlagTests(AuthorityChangesCase):
    """Round-2 review item 3: the two flags on `restore-new`, held through the command line.

    The flags used to reach the re-grant unchecked: no repeat refused, `--act`/`--reas` accepted,
    the reason ceiling and the actor's form not held through the CLI (mutants N26, N27, N28), and
    a bare re-grant silent. ``finish_restore`` is the step that passes them on, so it is called
    here with the same command-line namespace ``main()`` builds.
    """

    def namespace(self, **fields):
        import argparse
        args = argparse.Namespace(project='lm', destination='lmr', restore_operators=True,
                                  restore_verifiers=False, actor=None, reason=None, native_only=None)
        for key, value in fields.items():
            setattr(args, key, value)
        return args

    def finish(self, args):
        """`finish_restore` with everything but the authority step stubbed; returns its calls."""
        calls = []
        (self.root / 'projects').mkdir(exist_ok=True)
        (self.root / 'projects' / 'lmr').mkdir(exist_ok=True)
        with patch.object(admin, 'run_bd', side_effect=lambda *a: 'ok'), \
                patch.object(admin, 'restore_coordination', return_value=True), \
                patch.object(admin, 'restore_journal', return_value='a journal'), \
                patch.object(admin, 'restore_authority',
                             side_effect=lambda root, source, **kwargs: calls.append((source, kwargs))):
            admin.finish_restore(self.root, args, self.root / 'snapshot.sqlite3')
        return calls

    def test_the_flags_reach_the_regrant(self):
        # N26: the CLI used to drop --actor/--reason on the way to the re-grant.
        calls = self.finish(self.namespace(actor='james', reason='after a rollback'))
        self.assertEqual(calls, [('lm', {'restore_operators': True, 'restore_verifiers': False,
                                         'actor': 'james', 'reason': 'after a rollback'})])
        calls = self.finish(self.namespace(restore_operators=False, restore_verifiers=True))
        self.assertEqual(calls, [('lm', {'restore_operators': False, 'restore_verifiers': True,
                                         'actor': None, 'reason': None})])

    def test_the_reason_ceiling_and_the_actor_are_checked_before_anything_is_restored(self):
        # N27 (the ceiling) and N28 (the actor's form): both refused before the restore starts,
        # so a bad value is never discovered after the native restore.
        limit = admin.AUTHORITY_CHANGES_REASON_MAX - len('restore-new lm: ')
        with self.assertRaisesRegex(ValueError, 'at most %d characters here' % limit):
            self.cli('restore-new', 'lm', 'lmr', '--restore-operators', '--reason', 'x' * (limit + 1))
        with self.assertRaisesRegex(ValueError, 'Invalid operator identity'):
            self.cli('restore-new', 'lm', 'lmr', '--restore-operators', '--actor', 'not a name!')
        self.assertEqual((self.root / 'projects' / 'lmr').exists(), False)
        with self.assertRaisesRegex(ValueError, 'not blank'):
            self.cli('restore-new', 'lm', 'lmr', '--restore-operators', '--reason', '   ')
        self.assertEqual((self.root / 'projects' / 'lmr').exists(), False)

    def test_a_repeat_or_an_abbreviation_of_the_two_flags_is_refused(self):
        for argv in (('lm', 'lmr', '--restore-operators', '--actor', 'a', '--actor', 'b'),
                     ('lm', 'lmr', '--restore-operators', '--reason', 'one', '--reason', 'two')):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(ValueError, 'was given more than once'):
                    self.cli('restore-new', *argv)
        # Abbreviation is off for this subparser, exactly as for operators/verifiers: the two
        # recording flags are refused, and so is every prefix of the older ones (nothing in this
        # repository or its recipes spells one that way).
        for argv in (('lm', 'lmr', '--restore-operators', '--act', 'a'),
                     ('lm', 'lmr', '--restore-operators', '--reas', 'r'),
                     ('lm', 'lmr', '--restore-operators', '--a', 'a'),
                     ('lm', 'lmr', '--restore-operators', '--actor', 'a', '--reaso', 'r'),
                     ('lm', 'lmr', '--restore-o', '--actor', 'a'),
                     ('lm', 'lmr', '--without-c')):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as refused:
                    self.cli('restore-new', *argv)
                self.assertEqual(refused.exception.code, 2)
        # The spelled-out flags are unaffected: this reaches the restore itself.
        with self.assertRaisesRegex(ValueError, 'Source backup missing'):
            self.cli('restore-new', 'lm', 'lmr', '--restore-operators', '--without-coordination',
                     '--actor', 'james', '--reason', 'a rollback')

    def test_a_bare_regrant_prints_the_unattributed_sentence(self):
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none'}), encoding='utf-8')
        err = io.StringIO()
        with patch.object(admin, 'missing_operators', return_value=['op1']), \
                patch.object(admin, 'missing_verifiers', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            warning = admin.restore_authority(self.root, 'lm', restore_operators=True)
        self.assertIsNone(warning)
        self.assertIn('unattributed', err.getvalue())
        self.assertIn('--actor OPERATOR', err.getvalue())
        self.assertIn('--reason TEXT', err.getvalue())
        self.assertEqual(self.entries()[0]['operator'], None)
        self.assertEqual(self.entries()[0]['actor'], 'op1')


class SymlinkSetAsideTests(AuthorityChangesCase):
    """kittrial-5bb.229 finding 1: the set-aside and the reader never follow a symlink.

    A ``.damaged-*`` symlink to an outside file used to be listed as a file the kit kept, and a
    symlink at the aside name pointing at the audit path used to make ``samefile`` true so nothing
    was linked or copied and the damaged bytes were lost when the history was replaced. A dangling
    symlink at the aside name made ``exists`` false, ``os.link`` fail with ``EEXIST`` and the
    fallback copy follow the link, writing the damaged bytes OUTSIDE the runtime.
    """

    def outside(self, tag):
        path = self.root.parent / ('outside-229-%s-%d.json' % (tag, os.getpid()))
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    @unittest.skipIf(os.name != 'posix', 'symlinks are POSIX-only')
    def test_a_damaged_symlink_to_an_outside_file_is_not_listed(self):
        outside = self.outside('list')
        outside.write_text('OUTSIDE', encoding='utf-8')
        link = self.root / (AUDIT + admin.AUTHORITY_CHANGES_DAMAGED + 'outside')
        os.symlink(outside, link)
        report = json.loads(self.cli('authority-changes')[0])
        self.assertNotIn(link.name, report['damaged_files'])

    @unittest.skipIf(os.name != 'posix', 'symlinks are POSIX-only')
    def test_a_damaged_symlink_to_the_audit_path_is_never_reused_and_the_bytes_are_kept(self):
        path = self.root / AUDIT
        path.write_text('{nope', encoding='utf-8')
        link = self.root / (AUDIT + admin.AUTHORITY_CHANGES_DAMAGED + 'keep')
        os.symlink(path, link)                      # samefile(path, link) was true before the fix
        out, err = self.cli('operators', 'remove', OPERATOR, '--confirm-revoke',
                            '--actor', OPERATOR, '--reason', 'key leaked')
        self.assertEqual(json.loads(out), {'operators': []})
        kept = [item for item in self.asides() if not item.is_symlink()]
        self.assertEqual(len(kept), 1)              # a real regular file holds the damaged bytes
        self.assertEqual(kept[0].read_text(encoding='utf-8'), '{nope')
        self.assertNotIn(link.name, json.loads(self.cli('authority-changes')[0])['damaged_files'])

    @unittest.skipIf(os.name != 'posix', 'symlinks are POSIX-only')
    def test_a_dangling_symlink_at_the_aside_name_is_never_copied_through(self):
        from datetime import datetime, timezone
        path = self.root / AUDIT
        path.write_text('{nope', encoding='utf-8')
        outside = self.outside('dangling')
        fixed = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
        with patch('datetime.datetime') as clock:
            clock.now.return_value = fixed
            name = admin.authority_change_aside_name(self.root)
            os.symlink(outside, name)               # dangling: the target does not exist
            out, err = self.cli('operators', 'remove', OPERATOR, '--confirm-revoke',
                                '--actor', OPERATOR, '--reason', 'key leaked')
        self.assertEqual(json.loads(out), {'operators': []})
        self.assertFalse(os.path.exists(outside))   # the bytes never reached the link's target
        self.assertTrue(os.path.islink(name))       # the dangling link is left alone
        kept = [item for item in self.asides() if not item.is_symlink()]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(encoding='utf-8'), '{nope')

    @unittest.skipIf(os.name != 'posix', 'symlinks are POSIX-only')
    def test_the_copy_fallback_does_not_follow_a_symlink_at_the_aside_name(self):
        from datetime import datetime, timezone
        path = self.root / AUDIT
        path.write_text('{nope', encoding='utf-8')
        outside = self.outside('copy')
        fixed = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
        with patch('datetime.datetime') as clock:
            clock.now.return_value = fixed
            name = admin.authority_change_aside_name(self.root)
            os.symlink(outside, name)
            with patch.object(admin.os, 'link', side_effect=OSError(1, 'no hard links (injected)')):
                out, err = self.cli('operators', 'remove', OPERATOR, '--confirm-revoke',
                                    '--actor', OPERATOR, '--reason', 'key leaked')
        self.assertEqual(json.loads(out), {'operators': []})
        self.assertFalse(os.path.exists(outside))
        kept = [item for item in self.asides() if not item.is_symlink()]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(encoding='utf-8'), '{nope')

    def test_the_copy_fallback_compares_bytes_before_copying_again(self):
        # Finding 4: without hard links a retry reuses the copy already holding these bytes.
        path = self.root / AUDIT
        path.write_text('{nope', encoding='utf-8')
        with patch.object(admin.os, 'link', side_effect=OSError(1, 'no hard links (injected)')):
            first = admin.set_aside_damaged_authority_changes(self.root, ('unreadable', 'cannot be read'))
            second = admin.set_aside_damaged_authority_changes(self.root, ('unreadable', 'cannot be read'))
        self.assertEqual(first, second)
        kept = [item for item in self.asides() if not item.is_symlink()]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(encoding='utf-8'), '{nope')


class UnreadableListTests(AuthorityChangesCase):
    """kittrial-5bb.229 finding 2: never an empty list for a list that could not be read."""

    def test_an_unreadable_verifiers_list_is_refused_and_never_baselined_empty(self):
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': ['o1', 'o2'], 'verifiers': 5}), encoding='utf-8')
        before = self.marker.read_bytes()
        with self.assertRaisesRegex(ValueError, 'cannot both be read') as refusal:
            self.cli('operators', 'add', 'o3', '--actor', 'alice', '--reason', 'pilot')
        self.assertIn('verifiers', str(refusal.exception))
        self.assertEqual(self.audit(), None)                 # no history with an empty baseline
        self.assertEqual(self.marker.read_bytes(), before)    # nothing changed
        report = json.loads(self.cli('authority-changes')[0])
        self.assertEqual(report['current_lists']['operators'], ['o1', 'o2'])
        self.assertIsNone(report['current_lists']['verifiers'])
        self.assertEqual(report['replay']['state'], 'no-trail')
        self.assertIsNone(report['replay']['agrees'])
        self.assertIsNone(report['replay']['lists']['verifiers'])
        self.assertIn('No trail yet', report['replay']['note'])
        self.assertIn('incomplete', report['replay']['note'])
        self.assertIn('verifiers', report['replay']['note'])

    def test_an_unreadable_operators_list_is_refused_and_read_incomplete(self):
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none', 'operators': 7,
                                           'verifiers': ['v1']}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'operators'):
            self.cli('verifiers', 'add', 'v2', '--actor', 'alice', '--reason', 'pilot')
        self.assertEqual(self.audit(), None)
        report = json.loads(self.cli('authority-changes')[0])
        self.assertIsNone(report['current_lists']['operators'])
        self.assertEqual(report['current_lists']['verifiers'], ['v1'])
        self.assertEqual(report['replay']['state'], 'no-trail')
        self.assertIn('incomplete', report['replay']['note'])

    def test_a_trail_with_an_unreadable_list_reads_incomplete(self):
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': ['o1'], 'verifiers': 5}), encoding='utf-8')
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': 1, 'entries': [self.good_entry(actor='o1')]}), encoding='utf-8')
        report = json.loads(self.cli('authority-changes')[0])
        self.assertEqual(report['replay']['state'], 'incomplete')
        self.assertIsNone(report['replay']['agrees'])
        self.assertIsNone(report['replay']['lists']['verifiers'])
        self.assertIsNotNone(report['replay']['lists']['operators'])
        self.assertIn('The trail is incomplete for verifiers', report['replay']['note'])

    def test_a_repaired_file_lets_the_history_start_from_the_real_lists(self):
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': ['o1', 'o2'], 'verifiers': 5}), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.cli('operators', 'add', 'o3', '--actor', 'alice', '--reason', 'pilot')
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': ['o1', 'o2'], 'verifiers': ['v1']}), encoding='utf-8')
        self.cli('operators', 'add', 'o3', '--actor', 'alice', '--reason', 'pilot')
        self.assertEqual(self.baseline()['lists'], {'operators': ['o1', 'o2'], 'verifiers': ['v1']})
        report = json.loads(self.cli('authority-changes')[0])
        self.assertTrue(report['replay']['agrees'])
        self.assertEqual(report['replay']['lists']['operators']['listed_but_not_in_trail'], [])


class PlaceholderTests(AuthorityChangesCase):
    """kittrial-5bb.229 finding 3: the literal re-grant placeholders are refused."""

    def test_the_literal_placeholders_are_refused_and_nothing_is_recorded(self):
        for argv in (('operators', 'add', 'op1', '--actor', 'OPERATOR', '--reason', 'pilot'),
                     ('operators', 'add', 'op1', '--actor', 'alice', '--reason', 'TEXT'),
                     ('verifiers', 'add', 'v1', '--actor', 'OPERATOR', '--reason', 'TEXT')):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(ValueError, 'placeholder'):
                    self.cli(*argv)
        self.assertIsNone(self.audit())
        self.assertEqual(self.stored()['operators'], [OPERATOR])
        self.assertEqual(self.stored()['verifiers'], [VERIFIER])

    def test_the_replaced_placeholders_record_an_attributed_entry(self):
        self.cli('operators', 'add', 'op1', '--actor', 'james', '--reason', 'after a rollback')
        entry = self.entries()[0]
        self.assertEqual((entry['operator'], entry['actor']), ('james', 'op1'))
        self.assertEqual(json.loads(self.cli('authority-changes')[0])['unattributed_entries'], 0)

    def test_the_regrant_warning_says_the_placeholders_must_be_replaced(self):
        warning = admin.authority_not_regranted(self.root, 'lm', ['op1'], ['v1'])
        self.assertIn('operators add op1 --actor OPERATOR --reason TEXT', warning)
        self.assertIn('Replace OPERATOR in --actor and TEXT in --reason', warning)


class LateBaselineTests(AuthorityChangesCase):
    """kittrial-5bb.229 finding 6: a baseline taken late is worded truthfully."""

    def test_a_history_without_a_baseline_reads_incomplete(self):
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': 1, 'entries': [self.good_entry(actor='legacy')]}), encoding='utf-8')
        report = json.loads(self.cli('authority-changes')[0])
        self.assertIn('does not begin with a baseline', report['replay']['note'])
        self.assertIn('incomplete', report['replay']['note'])

    def test_a_late_baseline_does_not_claim_to_hold_the_original_lists(self):
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': 1, 'entries': [self.good_entry(actor='legacy')]}), encoding='utf-8')
        self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        reason = self.baseline()['reason']
        self.assertIn('as they stand at this change', reason)
        self.assertIn('incomplete', reason)
        self.assertNotIn('as they stood when this history began', reason)


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

    def test_operations_documents_the_round_two_answers(self):
        # Round-2 review: the baseline, the never-empty path, the files never removed, the flagged
        # re-grant commands, the no-op add, and that a script reads replay.agrees (the reader
        # exits 0 on a mismatch and on an unreadable deployment file).
        text = (KIT / 'docs' / 'OPERATIONS.md').read_text(encoding='utf-8')
        for phrase in ('**The baseline: every new history starts from a known state.**',
                       'is carried forward, never cut',
                       'The `.damaged-*` files are never removed',
                       'The path is therefore never absent',
                       'a script reads `replay.agrees`',
                       'exits 0 whether the',
                       'no trail yet',
                       'is not refused at all',
                       'carry `--actor`/`--reason`',
                       'lists them (in its JSON, under `damaged_files`'):
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
