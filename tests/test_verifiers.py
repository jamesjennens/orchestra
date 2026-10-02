"""The deployment `verifiers` list (kittrial-5bb.69; docs/CAPABILITY_INDEX_DESIGN.md section 5.2).

A second, narrow deployment-wide authority beside `operators`: it only marks
capability verifications trusted. It reuses the operator allowlist's rules: one
authority source (the file, never the environment), explicit revocation that names
what changes, a sidecar copy for information, and a restore that never re-grants it
without `--restore-verifiers`. `admin.py capability-verify` is the only route that
writes a verified capability check.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import admin
import capability_records as cr
import capability_verification as cv
from test_capability_records import entry
from test_capability_verification import VerifyNative
from test_reference_records import OPERATOR, TODAY

VERIFIER = 'ci-host'
KEY = 'review.structured-contribution'
COMMIT = '1' * 40


class VerifiersCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.project = self.root / 'projects' / 'trial'
        try:
            (self.project / '.beads').mkdir(parents=True)
        except OSError as exc:  # confined environments may forbid nested temp directories
            self.skipTest('nested temporary directory unavailable: ' + str(exc))
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        self.marker = self.root / 'deployment.private.json'
        self.configure(operators=[OPERATOR])
        self.flock = Mock()
        for patcher in (patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=self.flock, LOCK_EX=2)}),
                        patch('time.gmtime', return_value=TODAY)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ('ORCHESTRA_OPERATORS', 'ORCHESTRA_VERIFIERS'):
            saved = patch.dict(os.environ)
            saved.start()
            self.addCleanup(saved.stop)
            os.environ.pop(name, None)
        self.native = VerifyNative()
        self.native.actor = 'alice'
        cr.apply_native(entry(), 'alice', self.native, self.project)

    def configure(self, **values):
        self.marker.write_text(json.dumps(dict({'password': 'x', 'unit': 'none'}, **values)), encoding='utf-8')

    def stored(self):
        return json.loads(self.marker.read_text(encoding='utf-8'))

    def run_native(self, root, name, args):
        if args[:1] == ['--actor']:
            self.native.actor = args[1]
            args = args[2:]
        return self.native(list(args))

    def cli(self, *argv):
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'run_bd', side_effect=self.run_native), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        return out.getvalue()

    def payload(self, **extra):
        row, _ = cr.anchor_for(cr.read_rows(self.native), KEY)
        record = cr.existing_revisions(row)[1]
        results = [{'pointer': pointer, 'resolved': True, 'reason': None}
                   for pointer in record['code'] + record['tests'] + record['anchors']]
        payload = {'schema_version': 1, 'key': KEY, 'revision': 1, 'record_sha256': record['sha256'],
                   'commit': COMMIT, 'checked_at': '2026-10-01T12:00:00Z', 'source': 'ast',
                   'graph_built_at_commit': None, 'tool': {'name': 'orchestra-capability-check', 'version': '0.1.0'},
                   'results': results, 'passed': True}
        payload.update(extra)
        return payload

    def verify(self, actor, payload=None):
        path = self.root / 'payload.json'
        path.write_text(json.dumps(payload or self.payload()), encoding='utf-8')
        return json.loads(self.cli('capability-verify', 'trial', '--actor', actor, '--file', str(path)))

    def state(self):
        return cr.read(['get', KEY], self.native, admin.operators(self.root),
                       verifiers=admin.verifiers(self.root), journal=self.project)['verification']['state']


class ListCommandTests(VerifiersCase):
    def test_the_list_is_empty_by_default_and_changed_only_by_the_host_command(self):
        self.assertEqual(admin.verifiers(self.root), frozenset())
        self.assertEqual(json.loads(self.cli('verifiers', 'list')), {'verifiers': []})
        self.assertEqual(json.loads(self.cli('verifiers', 'add', VERIFIER)), {'verifiers': [VERIFIER]})
        self.assertEqual(json.loads(self.cli('verifiers', 'add', VERIFIER)), {'verifiers': [VERIFIER]})
        self.assertEqual(admin.verifiers(self.root), frozenset({VERIFIER}))
        # The operator allowlist and every other key are left exactly as they were.
        self.assertEqual((self.stored()['operators'], self.stored()['password']), ([OPERATOR], 'x'))
        if os.name == 'posix':
            import stat
            self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)
        with self.assertRaisesRegex(ValueError, 'requires an actor identity'):
            self.cli('verifiers', 'add')
        # `recovery.identity` forbids ':', so an account value can never be a verifier.
        for bad in ('account:u-1', 'has space', ''):
            with self.assertRaisesRegex(ValueError, 'Invalid verifier identity|requires an actor'):
                self.cli('verifiers', 'add', bad)

    def test_removal_is_explicit_and_names_what_changes(self):
        self.cli('verifiers', 'add', VERIFIER)
        self.assertEqual(self.verify(VERIFIER)['items'][0]['result'], 'recorded')
        self.assertEqual(self.state(), 'verified')
        with self.assertRaisesRegex(ValueError, 'confirm-revoke') as refused:
            self.cli('verifiers', 'remove', VERIFIER)
        text = str(refused.exception)
        self.assertIn('reads `reported` instead of `verified`', text)
        self.assertIn('trial/%s verified -> reported' % KEY, text)
        self.assertEqual(admin.verifiers(self.root), frozenset({VERIFIER}))
        self.assertEqual(json.loads(self.cli('verifiers', 'remove', VERIFIER, '--confirm-revoke')), {'verifiers': []})
        self.assertNotIn('verifiers', self.stored())
        self.assertEqual(self.state(), 'reported')
        # Re-adding restores the reading: nothing was deleted.
        self.cli('verifiers', 'add', VERIFIER)
        self.assertEqual(self.state(), 'verified')

    def test_revoking_a_verifier_who_is_also_an_operator_changes_nothing(self):
        self.configure(operators=[OPERATOR], verifiers=[OPERATOR])
        self.verify(OPERATOR)
        with self.assertRaisesRegex(ValueError, 'capabilities whose verification changes: none'):
            self.cli('verifiers', 'remove', OPERATOR)

    def test_the_environment_is_never_an_authority_source(self):
        self.configure(operators=[OPERATOR], verifiers=[VERIFIER])
        os.environ['ORCHESTRA_VERIFIERS'] = 'mallory'
        self.assertEqual(admin.verifiers(self.root), frozenset({VERIFIER}))   # reads ignore it
        for argv in (('verifiers', 'add', 'x1'), ('verifiers', 'remove', VERIFIER, '--confirm-revoke')):
            with self.assertRaisesRegex(ValueError, 'ORCHESTRA_VERIFIERS is set in this shell'):
                self.cli(*argv)
        with self.assertRaisesRegex(ValueError, 'ORCHESTRA_VERIFIERS is set in this shell'):
            self.verify(VERIFIER)
        self.assertEqual(self.stored()['verifiers'], [VERIFIER])
        os.environ['ORCHESTRA_VERIFIERS'] = VERIFIER      # agreeing with the file is not a mismatch
        self.assertEqual(json.loads(self.cli('verifiers', 'add', 'x1')), {'verifiers': [VERIFIER, 'x1']})

    def test_a_hand_edited_string_is_one_identity_and_a_bad_value_is_refused(self):
        self.configure(operators=[OPERATOR], verifiers='ci-host')
        self.assertEqual(admin.verifiers(self.root), frozenset({'ci-host'}))
        self.assertEqual(json.loads(self.cli('verifiers', 'add', 'x1')), {'verifiers': ['ci-host', 'x1']})
        self.configure(operators=[OPERATOR], verifiers={'ci-host': True})
        with self.assertRaisesRegex(ValueError, 'verifiers must be a list'):
            admin.verifiers(self.root)
        with self.assertRaisesRegex(ValueError, 'verifiers must be a list'):
            self.cli('verifiers', 'list')


class HostVerifyTests(VerifiersCase):
    def test_only_an_operator_or_listed_verifier_records_a_verified_check(self):
        for actor in ('mallory', VERIFIER):
            with self.assertRaisesRegex(ValueError, 'not on the deployment operator allowlist or the verifiers'):
                self.verify(actor)
        self.cli('verifiers', 'add', VERIFIER)
        self.flock.reset_mock()
        done = self.verify(VERIFIER, {'schema_version': 1, 'items': [self.payload()]})
        self.assertEqual((done['items'][0]['result'], done['recorded'], done['refused']), ('recorded', 1, 0))
        self.assertEqual(self.flock.call_count, 1)        # one hold of the coordination lock per item
        self.assertEqual(self.state(), 'verified')
        self.assertEqual(self.verify(VERIFIER)['items'][0]['result'], 'already-recorded')
        # An operator needs no verifiers entry.
        self.assertEqual(self.verify(OPERATOR, self.payload(commit='2' * 40))['items'][0]['result'], 'recorded')
        record = cv.parse(self.native.rows[0]['comments'][-1]['text'])
        self.assertEqual(record['submitter'], {'actor': OPERATOR, 'person': 'operator:' + OPERATOR,
                                               'identity': 'verified', 'submitted_by_agent': False})


class BackupRestoreTests(VerifiersCase):
    def backup(self):
        (self.root / 'backups').mkdir(exist_ok=True)
        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'trial')
        return json.loads((self.root / 'backups' / 'trial.coordination.json').read_text(encoding='utf-8'))

    def restore(self, **flags):
        (self.root / 'projects' / 'other').mkdir(exist_ok=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            admin.restore_coordination(self.root, 'trial', 'other', **flags)
        return out.getvalue()

    def test_the_sidecar_carries_the_list_and_a_restore_reports_it_without_regranting(self):
        self.configure(operators=[OPERATOR], verifiers=['zeta', VERIFIER])
        bundle = self.backup()
        self.assertEqual((bundle['operators'], bundle['verifiers']), ([OPERATOR], [VERIFIER, 'zeta']))
        self.assertEqual(admin.missing_verifiers(self.root, 'trial'), [])
        self.assertNotIn('NOT restored', self.restore())
        self.configure(operators=[OPERATOR], verifiers=['zeta'])
        self.assertEqual(admin.missing_verifiers(self.root, 'trial'), [VERIFIER])
        text = self.restore()
        self.assertIn('NOT restored: the backup records capability verifiers this host does not list: ' + VERIFIER,
                      text)
        self.assertIn('read `reported`', text)
        self.assertEqual(self.stored()['verifiers'], ['zeta'])
        # --restore-operators does not re-grant verifiers; only its own flag does.
        self.assertIn('NOT restored', self.restore(restore_operators=True))
        self.assertEqual(self.stored()['verifiers'], ['zeta'])
        text = self.restore(restore_verifiers=True)
        self.assertIn('Re-granted capability verifiers from the backup (--restore-verifiers): ' + VERIFIER, text)
        self.assertEqual((self.stored()['verifiers'], self.stored()['operators']), (['zeta', VERIFIER], [OPERATOR]))

    def test_the_restore_new_command_prints_the_not_restored_notice(self):
        """Review 01a0fe9e `smaller` (d): the notice through the real command, not only the function."""
        self.configure(operators=[OPERATOR], verifiers=[VERIFIER, 'zeta'])
        self.backup()
        (self.root / 'backups' / 'trial').mkdir(exist_ok=True)
        self.configure(operators=[OPERATOR], verifiers=['zeta'])

        def restore(*flags):
            (self.root / 'projects' / 'other').mkdir(exist_ok=True)
            with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'restore-new', 'trial', 'other',
                                            *flags]), \
                    patch.object(admin, 'root_path', return_value=self.root), \
                    patch.object(admin, 'add_project'), \
                    patch.object(admin, 'run_bd', return_value='restored'), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                admin.main()
            return out.getvalue()

        text = restore()
        self.assertIn('NOT restored: the backup records capability verifiers this host does not list: ' + VERIFIER,
                      text)
        self.assertIn('--restore-verifiers', text)
        self.assertIn('read `reported`', text)
        self.assertEqual(self.stored()['verifiers'], ['zeta'])
        self.assertNotIn('NOT restored: the backup records operator', text)   # the operators agree
        text = restore('--restore-verifiers')
        self.assertIn('Re-granted capability verifiers from the backup (--restore-verifiers): ' + VERIFIER, text)
        self.assertNotIn('NOT restored', text)
        self.assertEqual(self.stored()['verifiers'], ['zeta', VERIFIER])

    def test_restore_new_takes_the_flag_and_an_older_or_bad_sidecar_is_handled(self):
        self.configure(operators=[OPERATOR], verifiers=[VERIFIER])
        bundle = self.backup()
        self.configure(operators=[OPERATOR])
        with patch.object(admin, 'add_project'), patch.object(admin, 'restore_journal', return_value=None), \
                patch.object(admin, 'validate_backup_target', create=True):
            (self.root / 'projects' / 'other').mkdir(exist_ok=True)
            # An unknown flag would exit through argparse (SystemExit), which is not caught
            # here; a refusal further in, from the stubbed native restore, is fine.
            try:
                self.cli('restore-new', 'trial', 'other', '--restore-verifiers')
            except (ValueError, OSError):
                pass
        # A sidecar written before this slice has no verifiers key: nothing to report.
        path = self.root / 'backups' / 'trial.coordination.json'
        path.write_text(json.dumps({name: value for name, value in bundle.items() if name != 'verifiers'}),
                        encoding='utf-8')
        self.assertEqual(admin.coordination_verifiers(self.root, 'trial'), [])
        # A malformed snapshot is refused before any coordination write, like operators.
        path.write_text(json.dumps(dict(bundle, verifiers=['account:u-1'])), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Invalid verifier identity in coordination backup'):
            self.restore()


if __name__ == '__main__':
    unittest.main()
