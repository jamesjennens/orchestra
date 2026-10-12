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

    def cli_refusal(self, *argv):
        """Run one admin.py command that must refuse; returns (message, stderr).

        stderr is returned too, because some findings are about what the kit must NOT have said
        before it refused (kittrial-5bb.229 rev-2 item 1).
        """
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                patch.object(admin, 'root_path', return_value=self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(ValueError) as refusal:
                admin.main()
        return str(refusal.exception), err.getvalue()

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

    def test_a_null_list_surviving_the_cap_is_not_turned_into_an_empty_list(self):
        # Mutant R05 (kittrial-5bb.233 finding 6): the fold must keep a null (UNKNOWN) list null, and
        # the baseline's reason and the drop warning must not claim the trail leads to it.
        limit = admin.AUTHORITY_CHANGES_MAX
        names = ['n%03d' % index for index in range(limit)]
        entries = [dict(self.entry(actor=name), at='2026-10-01T00:00:%02dZ' % (index % 60))
                   for index, name in enumerate(names)]
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': admin.AUTHORITY_CHANGES_SCHEMA, 'entries': entries,
                        'baseline': self.good_baseline(lists={'operators': [names[0]], 'verifiers': None})}),
            encoding='utf-8')
        _, err = self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        self.assertIsNone(self.baseline()['lists']['verifiers'])       # NOT an empty list
        self.assertEqual(self.baseline()['lists']['operators'], [names[0]])
        reason = self.baseline()['reason']
        self.assertEqual(reason.count('UNKNOWN'), 1)                   # the clause is said once
        self.assertNotIn('still leads to the lists', reason)
        self.assertIn('stays null (UNKNOWN)', reason)
        self.assertNotIn('still leads to the lists', err)
        self.assertIn('does not lead to it', err)

    def test_the_capped_reader_note_does_not_promise_the_null_list_either(self):
        # The same wording rule in the reader's own cap sentence.
        limit = admin.AUTHORITY_CHANGES_MAX
        names = ['n%03d' % index for index in range(limit)]
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': admin.AUTHORITY_CHANGES_SCHEMA,
                        'entries': [dict(self.entry(actor=name), at='2026-10-01T00:00:%02dZ' % (index % 60))
                                    for index, name in enumerate(names)],
                        'baseline': self.good_baseline(lists={'operators': [names[0]], 'verifiers': None})}),
            encoding='utf-8')
        report = json.loads(self.cli('authority-changes')[0])
        note = report['replay']['note']
        self.assertIn('cap of %d entries' % limit, note)
        self.assertNotIn('still leads to the lists', note)
        self.assertIn('the verifiers list stays null (UNKNOWN) there, so the trail does not lead to it', note)


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
        # kittrial-5bb.233 revision 2, review item 3.7: a restore-route mutant that stopped returning
        # this warning used to make the assertions below die by a TypeError (assertIn on None), not
        # by an assertion. The warning text is asserted only after this says it is there.
        self.assertIsNotNone(warning)
        self.assertIn('was NOT re-granted', warning)
        self.assertIn('authority-changes audit', warning)
        self.assertIn('restore-new exits %d' % admin.RESTORE_AUTHORITY_NOT_REGRANTED, warning)
        # The commands it prints carry the recording flags (round-2 review item 3), with what the
        # restore was given where it had it.
        self.assertIn('operators add op1 --actor james --reason ', warning)
        self.assertEqual(self.marker.read_bytes(), before)          # nothing was re-granted
        self.assertEqual((self.root / AUDIT).read_text(encoding='utf-8'), '{nope')

    def test_a_regrant_naming_the_placeholder_is_refused_and_records_nothing(self):
        # kittrial-5bb.233 finding 5: the restore re-grant route used to record OPERATOR as a real
        # operator even though nobody of that name is listed. A re-grant is an ADD, so it is refused,
        # and the restore reports what was not re-granted and exits 3, as for a damaged audit.
        before = self.marker.read_bytes()
        with patch.object(admin, 'missing_operators', return_value=['op1']), \
                patch.object(admin, 'missing_verifiers', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            warning = admin.restore_authority(self.root, 'lm', restore_operators=True,
                                              actor='OPERATOR', reason='after a rollback')
        self.assertIsNotNone(warning)                          # an assertion, not a TypeError (item 3.7)
        self.assertIn('was NOT re-granted', warning)
        self.assertIn('placeholder', warning)
        self.assertIn('operators add op1 --actor OPERATOR', warning)
        self.assertIn('Replace OPERATOR in --actor', warning)
        self.assertIn('restore-new exits %d' % admin.RESTORE_AUTHORITY_NOT_REGRANTED, warning)
        self.assertEqual(self.marker.read_bytes(), before)          # nothing was re-granted
        self.assertIsNone(self.audit())                             # and nothing was recorded

    def test_a_regrant_naming_a_real_operator_named_OPERATOR_is_recorded(self):
        # The placeholder is not a real name only while nobody lists it: a listed OPERATOR re-grants.
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': [OPERATOR, 'OPERATOR'],
                                           'verifiers': [VERIFIER]}), encoding='utf-8')
        with patch.object(admin, 'missing_operators', return_value=['op1']), \
                patch.object(admin, 'missing_verifiers', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            warning = admin.restore_authority(self.root, 'lm', restore_operators=True,
                                              actor='OPERATOR', reason='after a rollback')
        self.assertIsNone(warning)
        self.assertEqual(self.entries()[-1]['operator'], 'OPERATOR')
        self.assertIn('op1', self.stored()['operators'])

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


class RemovalWithUnreadableListTests(AuthorityChangesCase):
    """kittrial-5bb.229 rev-2 item 1: a REMOVAL is never refused for an unreadable list.

    The coordinator's decision: when a list cannot be read, record that list as UNKNOWN (``null``)
    in the baseline and go on; the reader then says the trail is incomplete for it. Refuse at most
    an ADD - and never a removal, for the audit or for a list.
    """

    def unusable(self, operators=(OPERATOR, 'compromised'), verifiers=5):
        """A deployment file whose ``verifiers`` value cannot be used, as the reviewer's XV used."""
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': operators,
                                           'verifiers': verifiers}), encoding='utf-8')

    def test_a_removal_is_not_refused_when_the_other_list_cannot_be_read(self):
        self.unusable()
        out, err = self.cli('operators', 'remove', 'compromised', '--confirm-revoke',
                            '--actor', OPERATOR, '--reason', 'key leaked')
        self.assertEqual(json.loads(out), {'operators': [OPERATOR]})      # the removal happened
        self.assertNotIn('compromised', self.stored()['operators'])
        self.assertNotIn('cannot both be read', err)                      # never refused for the audit
        entry = self.entries()[0]
        self.assertEqual((entry['list'], entry['actor'], entry['change'], entry['operator']),
                         ('operators', 'compromised', 'remove', OPERATOR))
        # The baseline records the list nobody could read as UNKNOWN (null), never as empty.
        self.assertEqual(self.baseline()['lists'],
                         {'operators': sorted([OPERATOR, 'compromised']), 'verifiers': None})
        self.assertIn('verifiers', self.baseline()['reason'])
        self.assertIn('UNKNOWN', self.baseline()['reason'])
        self.assertIn('incomplete', self.baseline()['reason'])
        report = json.loads(self.cli('authority-changes')[0])
        self.assertIsNone(report['current_lists']['verifiers'])
        self.assertEqual(report['replay']['state'], 'incomplete')
        self.assertIsNone(report['replay']['agrees'])
        self.assertIsNone(report['replay']['lists']['verifiers'])
        self.assertTrue(report['replay']['lists']['operators']['agrees'])
        self.assertIsNone(report['baseline']['lists']['verifiers'])
        self.assertEqual(report['baseline']['unknown_lists'], ['verifiers'])

    def test_a_verifiers_removal_is_not_refused_when_the_operators_list_cannot_be_read(self):
        # The mirror case the reviewer named: the OTHER list is the unusable one.
        self.unusable(operators=7, verifiers=[VERIFIER, 'old-host'])
        out, err = self.cli('verifiers', 'remove', 'old-host', '--confirm-revoke',
                            '--actor', OPERATOR, '--reason', 'host retired')
        self.assertEqual(json.loads(out), {'verifiers': [VERIFIER]})
        self.assertEqual(self.baseline()['lists'],
                         {'operators': None, 'verifiers': sorted([VERIFIER, 'old-host'])})
        report = json.loads(self.cli('authority-changes')[0])
        self.assertEqual(report['replay']['state'], 'incomplete')
        self.assertIsNone(report['replay']['lists']['operators'])

    def test_a_removal_on_a_damaged_audit_records_the_unreadable_list_too(self):
        self.unusable()
        (self.root / AUDIT).write_text('{nope', encoding='utf-8')
        out, err = self.cli('operators', 'remove', 'compromised', '--confirm-revoke',
                            '--actor', OPERATOR, '--reason', 'key leaked')
        self.assertEqual(json.loads(out), {'operators': [OPERATOR]})
        self.assertIn('is damaged', err)
        self.assertIn('a fresh history starts', err)         # ... and it really was written
        self.assertTrue((self.root / AUDIT).is_file())
        self.assertIn('damaged', self.entries()[0]['reason'])
        self.assertEqual(self.baseline()['lists'],
                         {'operators': sorted([OPERATOR, 'compromised']), 'verifiers': None})
        self.assertEqual(len(self.asides()), 1)

    def test_an_add_is_still_refused_while_a_list_cannot_be_read(self):
        self.unusable()
        with self.assertRaisesRegex(ValueError, 'cannot both be read') as refusal:
            self.cli('operators', 'add', 'new-op', '--actor', 'alice', '--reason', 'pilot')
        self.assertIn('verifiers', str(refusal.exception))
        self.assertIn('ADD', str(refusal.exception))
        self.assertIsNone(self.audit())                      # nothing written
        self.assertEqual(self.stored()['operators'], [OPERATOR, 'compromised'])

    def test_a_history_with_a_baseline_proceeds_with_an_unreadable_list(self):
        # Q09: the unreadable-list refusal must NOT block a change once a baseline exists.
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': 1, 'entries': [self.good_entry(actor=OPERATOR)],
                        'baseline': self.good_baseline()}), encoding='utf-8')
        self.unusable()
        out, err = self.cli('operators', 'add', 'new-op', '--actor', 'alice', '--reason', 'pilot')
        self.assertEqual(json.loads(out), {'operators': [OPERATOR, 'compromised', 'new-op']})
        self.assertEqual(self.entries()[-1]['actor'], 'new-op')
        report = json.loads(self.cli('authority-changes')[0])
        # The trail's operators list has no entry for `compromised`, so the list this read COULD
        # compare does not agree: the answer is the false, not the null an incomplete read would
        # give (kittrial-5bb.233 finding 1a). The verifiers list is unreadable, and the note says
        # the mismatch is reported although it could not be compared.
        self.assertEqual(report['replay']['state'], 'mismatch')
        self.assertFalse(report['replay']['agrees'])
        self.assertIsNone(report['replay']['lists']['verifiers'])
        self.assertIn('although the trail is incomplete for verifiers', report['replay']['note'])

    def test_a_baseline_holding_a_list_as_null_reads_incomplete_after_a_repair(self):
        self.unusable()
        self.cli('operators', 'remove', 'compromised', '--confirm-revoke')
        config = self.stored()
        config['verifiers'] = [VERIFIER]                     # the value the baseline could not read
        self.marker.write_text(json.dumps(config), encoding='utf-8')
        report = json.loads(self.cli('authority-changes')[0])
        self.assertEqual(report['current_lists']['verifiers'], [VERIFIER])
        self.assertIsNone(report['replay']['lists']['verifiers'])   # no known state to replay from
        self.assertEqual(report['replay']['state'], 'incomplete')
        self.assertIsNone(report['replay']['agrees'])
        self.assertIn('null (UNKNOWN)', report['replay']['note'])

    def test_the_fresh_history_sentence_is_not_said_when_the_change_is_refused(self):
        # Item 1: do not say a fresh history starts before that is true. An ADD on a damaged audit
        # is refused before the lock, so the sentence must not have been printed at all.
        (self.root / AUDIT).write_text('{nope', encoding='utf-8')
        message, err = self.cli_refusal('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        self.assertIn('Move the file aside by hand', message)
        self.assertNotIn('a fresh history starts', message)
        self.assertNotIn('a fresh history starts', err)
        self.assertEqual(self.asides(), [])

    def test_the_unreadable_list_is_named_first_with_a_trail(self):
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': 1, 'entries': [self.good_entry(actor='o1')],
                        'baseline': self.good_baseline(lists={'operators': ['o1'],
                                                             'verifiers': ['v1']})}),
            encoding='utf-8')
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': ['o1'], 'verifiers': 5}), encoding='utf-8')
        note = json.loads(self.cli('authority-changes')[0])['replay']['note']
        self.assertTrue(note.startswith('The trail is incomplete for verifiers'), note)
        self.assertIn('leads to the current operators list', note)
        self.assertNotIn('leads to the current lists', note)

    def test_the_unreadable_list_is_named_first_with_no_trail(self):
        self.unusable(operators=[OPERATOR], verifiers=5)
        report = json.loads(self.cli('authority-changes')[0])
        note = report['replay']['note']
        self.assertEqual(report['replay']['state'], 'no-trail')
        self.assertTrue(note.startswith('The trail is incomplete for verifiers'), note)
        self.assertIn('No trail yet', note)
        self.assertIn('A REMOVAL is never refused', note)
        # It must no longer promise that the first change starts a baseline of them.
        self.assertNotIn('the first change starts the trail with a baseline of them', note)

    def test_a_baseline_that_holds_neither_a_list_nor_null_is_not_this_history(self):
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': 1, 'entries': [],
                        'baseline': self.good_baseline(lists={'operators': 'o1', 'verifiers': []})}),
            encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'not one this kit writes'):
            self.cli('authority-changes')

    def test_both_lists_unreadable_with_a_trail_says_only_that(self):
        # No comparable list at all: the incomplete sentence stands alone, with no "leads to" claim.
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': 1, 'entries': [self.good_entry(actor='o1')]}), encoding='utf-8')
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': 7, 'verifiers': 5}), encoding='utf-8')
        report = json.loads(self.cli('authority-changes')[0])
        note = report['replay']['note']
        self.assertEqual(report['replay']['state'], 'incomplete')
        self.assertIsNone(report['replay']['agrees'])
        self.assertTrue(note.startswith('The trail is incomplete for operators and verifiers'), note)
        self.assertNotIn('leads to the current', note)
        self.assertNotIn('  ', note)


class MismatchBesideUnknownListTests(AuthorityChangesCase):
    """kittrial-5bb.233 finding 1a: a comparable mismatch is reported beside an unknown list."""

    def null_verifiers_baseline(self):
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': 1, 'entries': [self.good_entry(actor=OPERATOR)],
                        'baseline': self.good_baseline(lists={'operators': [OPERATOR], 'verifiers': None})}),
            encoding='utf-8')

    def test_a_hand_added_operator_is_a_mismatch_beside_a_null_verifiers_baseline(self):
        # The reviewer's `in/exp8.py N2` shape: the baseline holds verifiers as null (UNKNOWN) and a
        # name was added to operators by hand. replay.agrees used to stay null, so a script told to
        # read that field saw no mismatch at all.
        self.null_verifiers_baseline()
        config = self.stored()
        config['operators'] = config['operators'] + ['sneaked']
        self.marker.write_text(json.dumps(config), encoding='utf-8')
        report = json.loads(self.cli('authority-changes')[0])
        self.assertEqual(report['replay']['lists']['operators']['listed_but_not_in_trail'], ['sneaked'])
        self.assertFalse(report['replay']['agrees'])
        self.assertEqual(report['replay']['state'], 'mismatch')
        self.assertIsNone(report['replay']['lists']['verifiers'])
        note = report['replay']['note']
        self.assertTrue(note.startswith('The trail is incomplete for verifiers'), note)
        self.assertIn('sneaked', note)
        self.assertIn('although the trail is incomplete for verifiers', note)

    def test_a_hand_added_verifier_is_a_mismatch_beside_a_null_operators_baseline(self):
        # The mirror case: the OTHER list is the unknown one.
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': 1,
                        'entries': [self.good_entry(list='verifiers', actor=VERIFIER)],
                        'baseline': self.good_baseline(lists={'operators': None, 'verifiers': [VERIFIER]})}),
            encoding='utf-8')
        config = self.stored()
        config['verifiers'] = config['verifiers'] + ['sneaked']
        self.marker.write_text(json.dumps(config), encoding='utf-8')
        report = json.loads(self.cli('authority-changes')[0])
        self.assertEqual(report['replay']['lists']['verifiers']['listed_but_not_in_trail'], ['sneaked'])
        self.assertFalse(report['replay']['agrees'])
        self.assertEqual(report['replay']['state'], 'mismatch')
        self.assertIsNone(report['replay']['lists']['operators'])
        self.assertIn('although the trail is incomplete for operators', report['replay']['note'])

    def test_a_mismatch_beside_a_list_unreadable_now_is_reported_too(self):
        # The same rule when the list is unreadable NOW rather than null in the baseline.
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': 1, 'entries': [self.good_entry(actor=OPERATOR)],
                        'baseline': self.good_baseline()}), encoding='utf-8')
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': [OPERATOR, 'sneaked'], 'verifiers': 5}),
                               encoding='utf-8')
        report = json.loads(self.cli('authority-changes')[0])
        self.assertFalse(report['replay']['agrees'])
        self.assertEqual(report['replay']['state'], 'mismatch')
        self.assertIn('although the trail is incomplete for verifiers', report['replay']['note'])

    def test_no_comparable_mismatch_stays_incomplete(self):
        # The other half of the rule: null/incomplete only while no comparable list mismatches.
        self.null_verifiers_baseline()
        report = json.loads(self.cli('authority-changes')[0])
        self.assertTrue(report['replay']['lists']['operators']['agrees'])
        self.assertIsNone(report['replay']['agrees'])
        self.assertEqual(report['replay']['state'], 'incomplete')
        self.assertNotIn('does not lead to', report['replay']['note'])


class RebaselineTests(AuthorityChangesCase):
    """kittrial-5bb.233 finding 1b: authority-changes-baseline takes a FRESH history."""

    def null_baseline_history(self):
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': 1, 'entries': [self.good_entry(actor=OPERATOR)],
                        'baseline': self.good_baseline(lists={'operators': [OPERATOR], 'verifiers': None})}),
            encoding='utf-8')

    def kept_histories(self):
        return sorted(self.root.glob(AUDIT + admin.AUTHORITY_CHANGES_REPLACED + '*'))

    def test_a_null_baseline_is_left_behind_and_a_later_hand_edit_warns_again(self):
        self.null_baseline_history()
        before = (self.root / AUDIT).read_bytes()
        # Before the command the reader says incomplete for good: the baseline holds verifiers null.
        self.assertEqual(json.loads(self.cli('authority-changes')[0])['replay']['state'], 'incomplete')
        out, err = self.cli('authority-changes-baseline', '--actor', OPERATOR,
                            '--reason', 'the verifiers value was repaired')
        report = json.loads(out)
        self.assertTrue(report['replay']['agrees'])
        self.assertEqual(report['replay']['state'], 'agrees')
        self.assertEqual(self.entries(), [])                       # a fresh history, no old entries
        self.assertEqual(self.baseline()['lists'],
                         {'operators': [OPERATOR], 'verifiers': [VERIFIER]})
        self.assertEqual(self.baseline()['operator'], OPERATOR)
        self.assertIn('authority-changes-baseline', self.baseline()['reason'])
        self.assertIn('the verifiers value was repaired', self.baseline()['reason'])
        # The old history is kept beside the runtime, byte for byte, under a .replaced- name - and it
        # is NOT a .damaged- name, because the reader lists those as damage.
        kept = self.kept_histories()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), before)
        self.assertEqual(report['previous_audit_kept_as'], kept[0].name)
        self.assertIn(kept[0].name, err)
        self.assertIn('fresh authority-changes history', err)
        self.assertEqual([name for name in report['damaged_files'] if 'replaced' in name], [])
        # A later hand edit of a list warns again: the fresh baseline made the trail usable.
        config = self.stored()
        config['verifiers'] = config['verifiers'] + ['sneaked']
        self.marker.write_text(json.dumps(config), encoding='utf-8')
        report = json.loads(self.cli('authority-changes')[0])
        self.assertFalse(report['replay']['agrees'])
        self.assertEqual(report['replay']['state'], 'mismatch')
        self.assertEqual(report['replay']['lists']['verifiers']['listed_but_not_in_trail'], ['sneaked'])

    def test_a_history_with_no_audit_file_is_started_without_a_kept_file(self):
        self.assertEqual(self.audit(), None)
        out, err = self.cli('authority-changes-baseline', '--actor', OPERATOR, '--reason', 'start clean')
        report = json.loads(out)
        self.assertTrue(report['replay']['agrees'])
        self.assertIsNone(report['previous_audit_kept_as'])
        self.assertEqual(self.kept_histories(), [])
        self.assertEqual(self.entries(), [])

    def test_a_list_that_cannot_be_read_refuses_the_command(self):
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': [OPERATOR], 'verifiers': 5}), encoding='utf-8')
        before = self.marker.read_bytes()
        message, err = self.cli_refusal('authority-changes-baseline', '--actor', OPERATOR, '--reason', 'repair')
        self.assertIn('cannot both be read', message)
        self.assertIn('verifiers', message)
        self.assertEqual(self.audit(), None)
        self.assertEqual(self.marker.read_bytes(), before)
        self.assertEqual(self.kept_histories(), [])

    def test_only_a_configured_operator_may_take_the_baseline(self):
        message, err = self.cli_refusal('authority-changes-baseline', '--actor', 'nobody', '--reason', 'repair')
        self.assertIn('nobody', message)
        self.assertEqual(self.audit(), None)

    def test_the_reason_is_bounded_by_what_the_baseline_reason_can_hold(self):
        message, err = self.cli_refusal('authority-changes-baseline', '--actor', OPERATOR,
                                        '--reason', 'x' * 400)
        self.assertIn('at most', message)
        self.assertEqual(self.audit(), None)
        self.assertEqual(self.kept_histories(), [])


class RebaselineVerdictTests(AuthorityChangesCase):
    """kittrial-5bb.233 revision 2, review items 1 and 2a-2d: the baseline records its own verdict.

    The command replaced the whole history and the reader then agreed, so the mismatch it hid was
    lost. It now runs the READER's own verdict on the history being replaced, records the state,
    the names the trail did not explain per list and the kept name in the new baseline's reason,
    prints a WARNING and carries it in the JSON, refuses a mismatch or a damaged history without
    `--accept-unexplained`, lists the `.replaced-*` files beside the `.damaged-*` ones, and carries
    the note through the cap fold for good.
    """

    def hand_add(self, noun, name):
        config = self.stored()
        config[noun] = config[noun] + [name]
        self.marker.write_text(json.dumps(config), encoding='utf-8')

    def kept_histories(self):
        return sorted(self.root.glob(AUDIT + admin.AUTHORITY_CHANGES_REPLACED + '*'))

    def test_a_mismatch_is_recorded_in_the_new_baseline_on_stderr_and_in_the_json(self):
        # (a): state, the names the trail does not explain per list, and the kept name.
        self.cli('operators', 'add', 'bob', '--actor', OPERATOR, '--reason', 'pilot')
        self.hand_add('operators', 'sneaked')
        self.assertEqual(json.loads(self.cli('authority-changes')[0])['replay']['state'], 'mismatch')
        before = (self.root / AUDIT).read_bytes()
        out, err = self.cli('authority-changes-baseline', '--actor', OPERATOR, '--reason', 'tidying',
                            '--accept-unexplained')
        report = json.loads(out)
        reason = self.baseline()['reason']
        self.assertIn(admin.AUTHORITY_CHANGES_REBASE_MARKER, reason)
        self.assertIn('mismatch', reason)
        self.assertIn('sneaked', reason)
        kept = self.kept_histories()
        self.assertEqual(len(kept), 1)
        self.assertIn(kept[0].name, reason)
        self.assertEqual(kept[0].read_bytes(), before)          # byte for byte, as before
        self.assertEqual([item['actor'] for item in self.entries()], [])   # a fresh history
        # stderr AND the JSON carry the warning the reader alone would lose.
        self.assertIn('WARNING', err)
        self.assertIn('does NOT agree', err)
        self.assertIn('sneaked', err)
        verdict = report['previous_audit_verdict']
        self.assertEqual(verdict['state'], 'mismatch')
        self.assertEqual(verdict['unexplained'], {'operators': ['sneaked']})
        self.assertIn('sneaked', verdict['note'])
        self.assertIn('WARNING', verdict['note'])          # the JSON carries the same WARNING
        self.assertEqual(report['previous_audit_kept_as'], kept[0].name)
        # The fresh history itself agrees - and the history still says the mismatch happened.
        self.assertTrue(report['replay']['agrees'])
        self.assertIn('mismatch', json.loads(self.cli('authority-changes')[0])['baseline']['reason'])

    def test_a_mismatch_refuses_without_the_explicit_flag(self):
        # (b): the null-baseline case needs no flag; a mismatch does, and the refusal says so.
        self.cli('operators', 'add', 'bob', '--actor', OPERATOR, '--reason', 'pilot')
        self.hand_add('verifiers', 'sneaked-v')
        before = (self.root / AUDIT).read_bytes()
        message, err = self.cli_refusal('authority-changes-baseline', '--actor', OPERATOR,
                                        '--reason', 'tidying')
        self.assertIn('does not agree', message)
        self.assertIn(admin.AUTHORITY_CHANGES_ACCEPT_UNEXPLAINED, message)
        self.assertIn('verifiers', message)
        self.assertIn('sneaked-v', message)
        self.assertEqual((self.root / AUDIT).read_bytes(), before)   # nothing replaced
        self.assertEqual(self.kept_histories(), [])
        self.assertNotIn('WARNING', err)                             # it never said it wrote one

    def test_the_incomplete_null_baseline_case_needs_no_flag_and_is_recorded(self):
        # (a) again for the case the command exists for: incomplete, no flag, state recorded.
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': 1, 'entries': [self.good_entry(actor=OPERATOR)],
                        'baseline': self.good_baseline(lists={'operators': [OPERATOR], 'verifiers': None})}),
            encoding='utf-8')
        out, err = self.cli('authority-changes-baseline', '--actor', OPERATOR,
                            '--reason', 'the verifiers value was repaired')
        report = json.loads(out)
        self.assertTrue(report['replay']['agrees'])
        self.assertIn('incomplete', self.baseline()['reason'])
        self.assertEqual(report['previous_audit_verdict']['state'], 'incomplete')
        self.assertIn('incomplete', err)

    def test_a_damaged_audit_refuses_without_the_flag_and_is_kept_and_recorded_with_it(self):
        # (b) for a damaged file, and (a) saying DAMAGED and why.
        (self.root / AUDIT).write_text('{nope', encoding='utf-8')
        before = (self.root / AUDIT).read_bytes()
        message, err = self.cli_refusal('authority-changes-baseline', '--actor', OPERATOR,
                                        '--reason', 'repair')
        self.assertIn('damaged', message)
        self.assertIn(admin.AUTHORITY_CHANGES_ACCEPT_UNEXPLAINED, message)
        self.assertEqual((self.root / AUDIT).read_bytes(), before)
        self.assertEqual(self.kept_histories(), [])
        out, err = self.cli('authority-changes-baseline', '--actor', OPERATOR, '--reason', 'repair',
                            '--accept-unexplained')
        report = json.loads(out)
        reason = self.baseline()['reason']
        self.assertIn('DAMAGED', reason)
        kept = self.kept_histories()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(encoding='utf-8'), '{nope')    # byte for byte
        self.assertIn(kept[0].name, reason)
        self.assertEqual(report['previous_audit_verdict']['state'], 'damaged')
        self.assertIn('DAMAGED', err)
        self.assertTrue(report['replay']['agrees'])                       # the fresh history is clean

    def test_the_reader_lists_the_replaced_history_beside_the_damaged_ones(self):
        # (c): a readable history kept on purpose is NOT a damaged file, and is listed.
        self.cli('operators', 'add', 'bob', '--actor', OPERATOR, '--reason', 'pilot')
        self.cli('authority-changes-baseline', '--actor', OPERATOR, '--reason', 'start clean')
        damaged = self.root / (AUDIT + admin.AUTHORITY_CHANGES_DAMAGED + '20260101T000000Z')
        damaged.write_text('{nope', encoding='utf-8')
        report = json.loads(self.cli('authority-changes')[0])
        replaced = [name for name in report['replaced_files']
                    if admin.AUTHORITY_CHANGES_REPLACED in name]
        self.assertEqual(len(replaced), 1)
        self.assertEqual(report['damaged_files'], [damaged.name])
        self.assertNotIn(replaced[0], report['damaged_files'])
        self.assertIn(replaced[0], report['replay']['note'])
        self.assertIn('not damaged files', report['replay']['note'])

    def test_the_cap_fold_carries_the_rebase_note_forward(self):
        # (d): after the cap folds entries into a fresh baseline, the audit still says the mismatch
        # happened, which names were unexplained and what the replaced history was kept as.
        limit = admin.AUTHORITY_CHANGES_MAX
        rebase = ('baseline: authority-changes-baseline by alice started a fresh history from the operator and '
                  'verifier lists as they stood then; the reason given: tidying; '
                  'the history this replaced did NOT agree with the lists (mismatch): operators: sneaked; '
                  'kept as ' + AUDIT + '.replaced-20260101T000000Z')
        (self.root / AUDIT).write_text(
            json.dumps({'schema_version': 1,
                        'entries': [dict(self.good_entry(actor='n%03d' % index),
                                         at='2026-10-01T00:00:%02dZ' % (index % 60))
                                    for index in range(limit)],
                        'baseline': self.good_baseline(reason=rebase)}),
            encoding='utf-8')
        self.cli('operators', 'add', 'final', '--actor', 'alice', '--reason', 'rebase')
        folded = self.baseline()['reason']
        self.assertIn('cap', folded)
        self.assertIn(admin.AUTHORITY_CHANGES_REBASE_MARKER, folded)
        self.assertIn('sneaked', folded)
        self.assertIn('.replaced-20260101T000000Z', folded)
        self.assertTrue(admin.authority_change_baseline_record(self.baseline()))
        # Another fold keeps saying so, for good.
        self.cli('operators', 'add', 'later', '--actor', 'alice', '--reason', 'on')
        self.assertIn('sneaked', self.baseline()['reason'])
        self.assertIn('.replaced-20260101T000000Z', self.baseline()['reason'])

    def test_the_hand_added_operator_runs_the_baseline_and_the_mismatch_is_still_said(self):
        # The reviewer's own in/a.py hide: a name added to `operators` by hand, and then THAT name -
        # a listed operator now - runs authority-changes-baseline itself.
        self.cli('operators', 'add', 'bob', '--actor', OPERATOR, '--reason', 'pilot')
        self.hand_add('operators', 'o1')
        self.assertIn('o1', self.stored()['operators'])
        out, err = self.cli('authority-changes-baseline', '--actor', 'o1', '--reason', 'tidying',
                            '--accept-unexplained')
        report = json.loads(out)
        # The reader agrees for the fresh history - and the history still says the mismatch
        # happened, and which name was unexplained.
        self.assertTrue(report['replay']['agrees'])
        self.assertEqual(report['replay']['state'], 'agrees')
        reason = self.baseline()['reason']
        self.assertIn('did NOT agree', reason)
        self.assertIn('mismatch', reason)
        self.assertIn('o1', reason)
        self.assertIn('tidying', reason)
        self.assertEqual(self.baseline()['operator'], 'o1')
        self.assertIn('WARNING', err)
        self.assertIn('o1', err)


class PlaceholderOnRemovalTests(AuthorityChangesCase):
    """kittrial-5bb.229 rev-2 item 3: a removal is never harder WITH the flags than without."""

    def test_a_removal_with_the_placeholders_is_recorded_unattributed(self):
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': [OPERATOR, 'compromised'],
                                           'verifiers': [VERIFIER]}), encoding='utf-8')
        out, err = self.cli('operators', 'remove', 'compromised', '--confirm-revoke',
                            '--actor', 'OPERATOR', '--reason', 'key leaked')
        self.assertEqual(json.loads(out), {'operators': [OPERATOR]})       # it happened
        entry = self.entries()[0]
        self.assertIsNone(entry['operator'])                 # the placeholder is not a real name
        self.assertEqual(entry['reason'], 'key leaked')      # a real reason IS recorded
        self.assertIn('--actor OPERATOR', err)
        self.assertIn('placeholder', err)
        self.assertIn('unattributed', err)

    def test_a_removal_with_both_placeholders_records_neither(self):
        out, err = self.cli('operators', 'remove', OPERATOR, '--confirm-revoke',
                            '--actor', 'OPERATOR', '--reason', 'TEXT')
        self.assertEqual(json.loads(out), {'operators': []})
        entry = self.entries()[0]
        self.assertIsNone(entry['operator'])
        self.assertIsNone(entry['reason'])
        self.assertIn('--actor OPERATOR and --reason TEXT', err)
        self.assertIn('unattributed', err)

    def test_a_removal_with_only_the_reason_placeholder_keeps_the_named_operator(self):
        out, err = self.cli('operators', 'remove', OPERATOR, '--confirm-revoke',
                            '--actor', 'alice', '--reason', 'TEXT')
        entry = self.entries()[0]
        self.assertEqual(entry['operator'], 'alice')         # only the placeholder was dropped
        self.assertIsNone(entry['reason'])
        self.assertIn('--reason TEXT', err)
        self.assertNotIn('unattributed', err)

    def test_a_verifiers_removal_with_a_placeholder_is_accepted_too(self):
        out, err = self.cli('verifiers', 'remove', VERIFIER, '--confirm-revoke',
                            '--actor', 'OPERATOR', '--reason', 'host retired')
        self.assertEqual(json.loads(out), {'verifiers': []})
        self.assertIsNone(self.entries()[0]['operator'])
        self.assertIn('placeholder', err)

    def test_a_listed_operator_named_OPERATOR_can_attribute_a_change(self):
        # operators add OPERATOR is accepted, so the literal --actor OPERATOR must not lock that
        # real operator out: it is a placeholder only while no listed operator carries the name.
        self.cli('operators', 'add', 'OPERATOR')
        self.assertIn('OPERATOR', self.stored()['operators'])
        self.cli('operators', 'add', 'bob', '--actor', 'OPERATOR', '--reason', 'pilot')
        self.assertEqual(self.entries()[-1]['operator'], 'OPERATOR')
        # Only the first, bare add is unattributed.
        self.assertEqual(json.loads(self.cli('authority-changes')[0])['unattributed_entries'], 1)

    def test_the_printed_re_grant_commands_are_still_refused_on_an_add(self):
        # Finding 3 stands: those commands ARE adds, and a literal placeholder on an add is refused.
        command = admin.authority_regrant_commands(self.root, ['op1'], ['v1'])[0]
        self.assertIn('--actor OPERATOR', command)
        self.assertIn('--reason TEXT', command)
        for argv in (('operators', 'add', 'op1', '--actor', 'OPERATOR', '--reason', 'TEXT'),
                     ('verifiers', 'add', 'v1', '--actor', 'OPERATOR', '--reason', 'pilot')):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(ValueError, 'placeholder'):
                    self.cli(*argv)
        self.assertIsNone(self.audit())

    def test_the_removal_placeholder_sentence_says_the_entry_stays_unattributed(self):
        # kittrial-5bb.233 finding 4: the sentence used to say to run the removal again "with the real
        # value", but the name is already gone, so running it again is a no-op that records nothing.
        out, err = self.cli('operators', 'remove', OPERATOR, '--confirm-revoke',
                            '--actor', 'OPERATOR', '--reason', 'TEXT')
        self.assertIn('recorded WITHOUT an operator', err)
        self.assertIn('stays unattributed', err)
        self.assertIn('the name is already gone', err)
        self.assertIn('no-op', err)
        self.assertIn('next real change', err)
        self.assertNotIn('Replace it with the real value', err)
        # What it says is true: the removal happened, and running it again records nothing.
        entries = self.entries()
        self.cli('operators', 'remove', OPERATOR, '--confirm-revoke', '--actor', 'alice', '--reason', 'again')
        self.assertEqual(self.entries(), entries)

    def racing(self, remove):
        """``deployment_config_lock`` that removes ``remove`` from the operators list on entry."""
        real = admin.deployment_config_lock

        def wrapper(root):
            config = json.loads(self.marker.read_text(encoding='utf-8'))
            config['operators'] = [name for name in config['operators'] if name != remove]
            self.marker.write_text(json.dumps(config), encoding='utf-8')
            return real(root)

        return wrapper

    def test_the_placeholder_check_is_remade_under_the_deployment_lock(self):
        # kittrial-5bb.233 finding 5, the reviewer's `in/race.py`: OPERATOR is listed when the
        # arguments are checked, and another change removes OPERATOR before the lock is taken. The add
        # must not be recorded with an operator nobody lists.
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': [OPERATOR, 'OPERATOR'],
                                           'verifiers': [VERIFIER]}), encoding='utf-8')
        with patch.object(admin, 'deployment_config_lock', self.racing('OPERATOR')):
            message, err = self.cli_refusal('operators', 'add', 'bob',
                                            '--actor', 'OPERATOR', '--reason', 'pilot')
        self.assertIn('placeholder', message)
        self.assertIsNone(self.audit())                        # nothing was recorded
        self.assertEqual(self.stored()['operators'], [OPERATOR])   # the concurrent removal stands
        self.assertNotIn('bob', self.stored()['operators'])

    def test_the_placeholder_check_is_remade_for_verifiers_too(self):
        # The same race on the verifiers command, whose placeholder is judged against the OPERATOR
        # list: it must be re-read under the lock too.
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': [OPERATOR, 'OPERATOR'],
                                           'verifiers': [VERIFIER]}), encoding='utf-8')
        with patch.object(admin, 'deployment_config_lock', self.racing('OPERATOR')):
            message, err = self.cli_refusal('verifiers', 'add', 'v2',
                                            '--actor', 'OPERATOR', '--reason', 'pilot')
        self.assertIn('placeholder', message)
        self.assertIsNone(self.audit())
        self.assertEqual(self.stored()['verifiers'], [VERIFIER])

    def test_a_real_listed_operator_named_OPERATOR_survives_the_recheck(self):
        # The recheck must not lock out a real operator named OPERATOR: with the name still listed
        # under the lock, the add is recorded and attributed to it.
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': [OPERATOR, 'OPERATOR'],
                                           'verifiers': [VERIFIER]}), encoding='utf-8')
        self.cli('operators', 'add', 'bob', '--actor', 'OPERATOR', '--reason', 'pilot')
        self.assertEqual(self.entries()[-1]['operator'], 'OPERATOR')
        self.assertEqual(self.stored()['operators'], [OPERATOR, 'OPERATOR', 'bob'])

    def test_the_removal_placeholder_check_is_remade_under_the_deployment_lock(self):
        # kittrial-5bb.233 revision 2, review item 3.1: the REMOVAL side of the race used to stay
        # open. OPERATOR is a real listed operator when the arguments are read, and a concurrent
        # change removes it before the lock; the removal must drop it there, not record an operator
        # nobody lists.
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': [OPERATOR, 'OPERATOR', 'bob'],
                                           'verifiers': [VERIFIER]}), encoding='utf-8')
        with patch.object(admin, 'deployment_config_lock', self.racing('OPERATOR')):
            out, err = self.cli('operators', 'remove', 'bob', '--confirm-revoke',
                                '--actor', 'OPERATOR', '--reason', 'key leaked')
        self.assertEqual(json.loads(out), {'operators': [OPERATOR]})   # the removal happened
        entry = self.entries()[0]
        self.assertIsNone(entry['operator'])           # dropped under the lock, not recorded
        self.assertEqual(entry['actor'], 'bob')
        self.assertEqual(entry['reason'], 'key leaked')  # the real reason IS recorded
        self.assertIn('placeholder', err)
        self.assertIn('unattributed', err)

    def test_the_removal_placeholder_check_is_remade_for_verifiers_too(self):
        # The same race on the verifiers command, whose placeholder is judged against the OPERATOR
        # list (review item 3.1).
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': [OPERATOR, 'OPERATOR'],
                                           'verifiers': [VERIFIER, 'v2']}), encoding='utf-8')
        with patch.object(admin, 'deployment_config_lock', self.racing('OPERATOR')):
            out, err = self.cli('verifiers', 'remove', 'v2', '--confirm-revoke',
                                '--actor', 'OPERATOR', '--reason', 'host retired')
        self.assertEqual(json.loads(out), {'verifiers': [VERIFIER]})
        self.assertIsNone(self.entries()[0]['operator'])
        self.assertEqual(self.entries()[0]['actor'], 'v2')
        self.assertIn('placeholder', err)


class SmallerRevisionTests(AuthorityChangesCase):
    """kittrial-5bb.233 revision 2, review items 3.2 to 3.7."""

    def test_the_baseline_operator_is_checked_again_under_the_lock(self):
        # Item 3.2: an operator revoked between the check before the lock and the write must not be
        # recorded as the one who took the baseline.
        real = admin.deployment_config_lock

        def wrapper(root):
            config = json.loads(self.marker.read_text(encoding='utf-8'))
            config['operators'] = [name for name in config['operators'] if name != OPERATOR]
            self.marker.write_text(json.dumps(config), encoding='utf-8')
            return real(root)

        self.cli('operators', 'add', 'bob', '--actor', OPERATOR, '--reason', 'pilot')
        before = (self.root / AUDIT).read_bytes()
        with patch.object(admin, 'deployment_config_lock', wrapper):
            message, err = self.cli_refusal('authority-changes-baseline', '--actor', OPERATOR,
                                            '--reason', 'tidying')
        self.assertIn('not a server-side configured operator', message)
        self.assertEqual((self.root / AUDIT).read_bytes(), before)
        self.assertEqual(sorted(self.root.glob(AUDIT + admin.AUTHORITY_CHANGES_REPLACED + '*')), [])

    def test_a_runtime_that_cannot_be_written_is_a_sentence_not_a_traceback(self):
        # Item 3.3: a PermissionError from the copy of the history to keep (the old admin.py:7129)
        # used to escape as a traceback.
        path = self.root / AUDIT
        path.write_text('{nope', encoding='utf-8')
        before = self.marker.read_bytes()
        real_open = admin.os.open

        def deny_open(target, *args, **kwargs):
            # Only the copy's own create call is denied: reading the deployment file uses os.open
            # too, and the point is a runtime directory that cannot be WRITTEN.
            if '.tmp-' in str(target):
                raise PermissionError(13, 'Permission denied (injected)')
            return real_open(target, *args, **kwargs)

        with patch.object(admin.os, 'link',
                          side_effect=PermissionError(13, 'Permission denied (injected)')), \
                patch.object(admin.os, 'open', side_effect=deny_open):
            message, err = self.cli_refusal('operators', 'remove', OPERATOR, '--confirm-revoke')
            message, err = self.cli_refusal('operators', 'remove', OPERATOR, '--confirm-revoke')
        self.assertIn('could not write a copy', message)
        self.assertIn('Nothing was changed', message)
        self.assertNotIn('Traceback', message)
        self.assertEqual(self.marker.read_bytes(), before)              # the operator was NOT removed
        self.assertEqual(path.read_text(encoding='utf-8'), '{nope')
        self.assertEqual(self.asides(), [])

    def test_the_placeholder_refusal_composes_with_a_full_stop(self):
        # Item 3.4: '...as a real name A re-grant...' was missing its stop.
        refusal = admin.authority_change_placeholder_refusal('OPERATOR', 'pilot', 'add', ())
        self.assertTrue(refusal.endswith('as a real name.'))
        warning = admin.authority_not_regranted_placeholder(self.root, 'lm', ['op1'], [], refusal,
                                                           actor='OPERATOR')
        self.assertIn('as a real name. A re-grant is an ADD', warning)

    def test_a_reason_holding_a_line_separator_is_refused(self):
        # Item 3.6: the recorded reason is one line, so a newline (and any other separator) is
        # refused with one sentence, before anything is written.
        for argv in (('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'one\ntwo'),
                     ('verifiers', 'add', 'v2', '--actor', 'alice', '--reason', 'one\rtwo'),
                     ('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'one\u2028two'),
                     ('authority-changes-baseline', '--actor', OPERATOR, '--reason', 'one\ntwo'),
                     ('restore-new', 'lm', 'lmr', '--restore-operators', '--actor', 'james',
                      '--reason', 'one\ntwo')):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(ValueError, 'one line'):
                    self.cli(*argv)
        self.assertIsNone(self.audit())


class SmallerFindingTests(AuthorityChangesCase):
    """kittrial-5bb.229 rev-2 item 4: the leftovers, the sentence, and the missing mutants."""

    @unittest.skipIf(os.name != 'posix', 'the cleanup runs only where fcntl exists')
    def test_a_leftover_of_a_kill_inside_the_copy_is_cleaned_by_the_next_write(self):
        # A7: a kill inside the copy leaves this name, which the 8-character pattern never matched.
        killed = self.root / ('.%s%s20260101T000000Z.tmp-%s'
                              % (AUDIT, admin.AUTHORITY_CHANGES_DAMAGED, 'a' * 16))
        killed.write_text('{}', encoding='utf-8')
        other = self.root / ('.%s.bcd12345' % AUDIT)         # the older leftover shape
        other.write_text('{}', encoding='utf-8')
        unrelated = self.root / ('.deployment.private.json%s20260101T000000Z.tmp-%s'
                                 % (admin.AUTHORITY_CHANGES_DAMAGED, 'b' * 16))
        unrelated.write_text('{}', encoding='utf-8')
        _, err = self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        self.assertFalse(killed.exists())
        self.assertFalse(other.exists())
        self.assertIn(killed.name, err)
        self.assertTrue(unrelated.exists())                  # only THIS audit's copy is a leftover

    def test_the_temporary_copy_is_removed_when_the_copy_fails(self):
        # Q18: the finally must unlink the temporary name when the copy itself fails.
        (self.root / AUDIT).write_text('{nope', encoding='utf-8')

        def fail(*args, **kwargs):
            raise OSError(5, 'Input/output error (injected)')

        with patch.object(admin.os, 'link', side_effect=OSError(1, 'Operation not permitted (injected)')), \
                patch('shutil.copyfileobj', side_effect=fail):
            with self.assertRaises(OSError):
                self.cli('operators', 'remove', OPERATOR, '--confirm-revoke')
        self.assertEqual([name for name in os.listdir(str(self.root)) if '.tmp-' in name], [])

    def test_a_taken_temporary_name_is_never_written_through(self):
        # Q03: O_EXCL is what refuses a taken name; O_NOFOLLOW adds nothing beside it.
        (self.root / AUDIT).write_text('{nope', encoding='utf-8')
        destination = self.root / (AUDIT + admin.AUTHORITY_CHANGES_DAMAGED + '20260101T000000Z')
        taken = self.root / ('.%s.tmp-%s' % (destination.name, 'a' * 16))
        taken.write_text('taken', encoding='utf-8')
        names = iter(['a' * 16, 'b' * 16])
        # The widened leftover cleanup runs at lock time and would remove this same name (it IS the
        # shape a kill inside the copy leaves - rev-2 item 4), so the copy's own O_EXCL is what is
        # under test here.
        with patch.object(admin, 'remove_private_write_leftovers', lambda root: []), \
                patch.object(admin, 'authority_change_aside_name', return_value=destination), \
                patch.object(admin.os, 'link', side_effect=OSError(1, 'Operation not permitted (injected)')), \
                patch.object(admin.secrets, 'token_hex', side_effect=lambda size: next(names)):
            out, err = self.cli('operators', 'remove', OPERATOR, '--confirm-revoke')
        self.assertEqual(json.loads(out), {'operators': []})
        self.assertEqual(taken.read_text(encoding='utf-8'), 'taken')    # never opened or truncated
        self.assertEqual([item.name for item in self.asides()], [destination.name])
        self.assertEqual(self.asides()[0].read_text(encoding='utf-8'), '{nope')

    def test_a_same_size_file_holding_other_bytes_is_not_the_kept_copy(self):
        # Q11: "the same bytes" is the BYTES, not the size - a same-size file would lose the bytes.
        path = self.root / AUDIT
        path.write_text('{nope', encoding='utf-8')
        decoy = self.root / (AUDIT + admin.AUTHORITY_CHANGES_DAMAGED + '20260101T000000Z')
        decoy.write_text('xxxxx', encoding='utf-8')          # same size, other bytes
        out, err = self.cli('operators', 'remove', OPERATOR, '--confirm-revoke')
        self.assertEqual(json.loads(out), {'operators': []})
        self.assertEqual(decoy.read_text(encoding='utf-8'), 'xxxxx')    # untouched
        kept = [item for item in self.asides() if item.name != decoy.name]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(encoding='utf-8'), '{nope')  # the bytes were kept
        report = json.loads(self.cli('authority-changes')[0])
        self.assertIn(decoy.name, report['damaged_files'])
        self.assertIn(kept[0].name, report['damaged_files'])

    def test_the_aside_name_search_giving_up_is_a_sentence_not_a_traceback(self):
        path = self.root / AUDIT
        path.write_text('{nope', encoding='utf-8')
        before = self.marker.read_bytes()
        with patch.object(admin.os, 'link', side_effect=FileExistsError(17, 'File exists (injected)')):
            message, err = self.cli_refusal('operators', 'remove', OPERATOR, '--confirm-revoke')
        self.assertIn('could not keep its bytes beside the runtime', message)
        self.assertIn('Nothing was changed', message)
        self.assertIn(str(admin.AUTHORITY_CHANGE_ASIDE_ATTEMPTS), message)
        self.assertNotIn('Traceback', message)
        self.assertEqual(self.marker.read_bytes(), before)   # the operator was NOT removed
        self.assertEqual(path.read_text(encoding='utf-8'), '{nope')
        self.assertEqual(self.asides(), [])

    def test_a_name_listed_twice_is_reported(self):
        # Q16: the duplicate-name sentence is held by a test.
        self.marker.write_text(json.dumps({'password': 'x', 'unit': 'none',
                                           'operators': [OPERATOR, OPERATOR],
                                           'verifiers': [VERIFIER]}), encoding='utf-8')
        self.cli('operators', 'add', 'bob', '--actor', 'alice', '--reason', 'pilot')
        report = json.loads(self.cli('authority-changes')[0])
        self.assertEqual(report['replay']['lists']['operators']['listed_more_than_once'], [OPERATOR])
        self.assertEqual(report['replay']['lists']['operators']['current'], sorted([OPERATOR, 'bob']))
        self.assertIn('more than once', report['replay']['note'])

    def test_the_fresh_history_sentence_comes_after_the_write(self):
        # Mutant R06 (kittrial-5bb.233 finding 6): the sentence saying a fresh history starts must be
        # printed only once that history is on disk. A write that fails must not have said it, while
        # the damaged bytes are still kept aside first.
        path = self.root / AUDIT
        path.write_text('{nope', encoding='utf-8')
        real = admin.atomic_private_write

        def fail_audit(target, text):
            if Path(target).name == AUDIT:
                raise OSError(28, 'No space left on device (injected)')
            return real(target, text)

        err = io.StringIO()
        with patch.object(admin, 'atomic_private_write', side_effect=fail_audit), \
                patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'operators', 'remove',
                                           OPERATOR, '--confirm-revoke', '--actor', 'alice', '--reason', 'gone']), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(OSError):
                admin.main()
        self.assertNotIn('a fresh history starts', err.getvalue())
        self.assertEqual(path.read_text(encoding='utf-8'), '{nope')     # the path is never absent
        self.assertEqual(len(self.asides()), 1)                         # the bytes were kept first

    def test_the_temporary_copy_giving_up_is_a_sentence_not_a_traceback(self):
        # Mutant R13b (kittrial-5bb.233 finding 6): when every one of the 64 temporary copy names is
        # taken, the refusal is a sentence naming the count, not a traceback.
        path = self.root / AUDIT
        path.write_text('{nope', encoding='utf-8')
        destination = self.root / (AUDIT + admin.AUTHORITY_CHANGES_DAMAGED + '20260101T000000Z')
        taken = self.root / ('.%s.tmp-%s' % (destination.name, 'a' * 16))
        taken.write_text('taken', encoding='utf-8')
        with patch.object(admin, 'remove_private_write_leftovers', lambda root: []), \
                patch.object(admin, 'authority_change_aside_name', return_value=destination), \
                patch.object(admin.os, 'link', side_effect=OSError(1, 'Operation not permitted (injected)')), \
                patch.object(admin.secrets, 'token_hex', return_value='a' * 16):
            message, err = self.cli_refusal('operators', 'remove', OPERATOR, '--confirm-revoke')
        self.assertIn('could not write a temporary copy', message)
        self.assertIn(str(admin.AUTHORITY_CHANGE_ASIDE_ATTEMPTS), message)
        self.assertNotIn('Traceback', message)
        self.assertEqual(taken.read_text(encoding='utf-8'), 'taken')     # never opened or truncated
        self.assertEqual(path.read_text(encoding='utf-8'), '{nope')


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

    def test_operations_states_one_rule_for_the_audit_and_the_lists(self):
        # kittrial-5bb.229 rev-2 item 2: the document says one rule again around findings 2 and A.
        text = (KIT / 'docs' / 'OPERATIONS.md').read_text(encoding='utf-8')
        for phrase in ('The ONE rule for both the audit',
                       'a removal is never refused for either, and only an add is',
                       'recorded in the baseline as `null`',
                       'and names it FIRST',
                       'the entry is recorded without it, and one stderr',
                       '`O_EXCL` is the',
                       'not the size'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_operations_documents_the_fresh_baseline_and_the_downgrade(self):
        # kittrial-5bb.233 findings 1b, 3, 4 and 6.
        text = (KIT / 'docs' / 'OPERATIONS.md').read_text(encoding='utf-8')
        for phrase in ('authority-changes-baseline',
                       'authority-changes.audit.json.replaced-<UTC date-time>',
                       'A removal FROM the list whose own value cannot be read is',
                       'is not history for an older kit',
                       'cannot tell such a file apart from',
                       'a second removal is a no-op',
                       'the mismatch is',
                       'although the other list could not be compared'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_operations_documents_the_baseline_verdict_and_the_flag(self):
        # kittrial-5bb.233 revision 2, review item 2e: the document says all of it, including that
        # the command is not a way to make a warning go away.
        text = (KIT / 'docs' / 'OPERATIONS.md').read_text(encoding='utf-8')
        for phrase in ('It is not a way to make a warning go away',
                       '--accept-unexplained',
                       'previous_audit_verdict',
                       '`replaced_files`',
                       'CARRIES',
                       'FORWARD',
                       'A `--reason` holding a newline',
                       'the first eight of',
                       '(+N more)',
                       'are NOT damaged'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_operations_names_the_kits_the_null_baseline_downgrade_affects(self):
        # kittrial-5bb.233 revision 2, review item 3.5: main 46ebcad reads a null baseline; the
        # older-kit sentence is true of caab668 and older.
        text = (KIT / 'docs' / 'OPERATIONS.md').read_text(encoding='utf-8')
        for phrase in ('46ebcad', 'caab668', 'reads a `null` baseline'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_the_capability_payload_summary_matches_the_behaviour(self):
        # Item 2: the payload is a REVISION of the behaviour, not a propose. It carries `revision`
        # and `expected_sha256` (the accepted revision it revises), so the field set must ALLOW both
        # and the payload must REQUIRE them; the old form pinned the propose shape, and a corrected
        # file failed it (kittrial-5bb.233 finding 2).
        payload = json.loads((KIT / 'capability-proposals' /
                              'deployment.list-changes-audited.json').read_text(encoding='utf-8'))
        self.assertEqual(set(payload) - {'schema_version', 'operation_id', 'key', 'name', 'aliases',
                                        'summary', 'requirements', 'anchors', 'code', 'tests',
                                        'owner', 'tags', 'revision', 'expected_sha256'}, set())
        self.assertIn('revision', payload)
        self.assertIn('expected_sha256', payload)
        self.assertIsInstance(payload['revision'], int)
        self.assertGreaterEqual(payload['revision'], 2)
        self.assertRegex(payload['expected_sha256'], r'^[0-9a-f]{64}$')
        self.assertLessEqual(len(payload['summary']), 1200)
        self.assertIn('A damaged audit never refuses a REMOVAL', payload['summary'])
        self.assertIn('null (UNKNOWN)', payload['summary'])
        self.assertIn('incomplete', payload['summary'])
        self.assertIn('recorded unattributed', payload['summary'])
        self.assertIn('a REMOVAL never is, for the audit or the OTHER list', payload['summary'])
        self.assertIn('authority-changes-baseline', payload['summary'])
        # kittrial-5bb.233 revision 2: the capability's summary says what the command now records
        # and what it refuses.
        self.assertIn('--accept-unexplained', payload['summary'])
        self.assertIn('replaced_files', payload['summary'])


if __name__ == '__main__':
    unittest.main()
