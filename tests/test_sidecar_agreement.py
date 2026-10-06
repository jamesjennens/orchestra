"""backup-authority and restore-new decide the same, from the same copy (kittrial-5bb.150/152).

A backup has two copies of its coordination sidecar: the canonical one and the durable
last-complete copy. Each copy is put in every state below and every pair is tried. For
each pair, the copy ``backup-authority`` answers from (or that it refuses) is compared
with the copy ``restore-new`` reads (``coordination_backup``, the restore's own reader)
and with the rule written out by hand. Each copy carries a different holder, so the copy
that was used is visible in what is read, not inferred from a path.

kittrial-5bb.152 compares the OUTCOME too: answer (from which copy), refuse, or legacy.
With the canonical copy absent and the last-complete copy damaged, restore-new used to
restore as legacy, silently without any coordination data, while backup-authority
refused; both now refuse, and `restore-new --without-coordination` restores the native
tracker data alone and says what it left out.

These tests take no lock and run on every platform; only the symlink, FIFO and device
cases need what the platform can make (a device needs root).
"""
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin

STATES = ('good', 'pending', 'wrong schema', 'not JSON', 'symlink', 'directory', 'unreadable', 'fifo', 'device',
          'absent')
#: The states every platform can make, so their combinations always run.
PORTABLE = [state for state in STATES if state not in ('symlink', 'fifo', 'device')]
REPO = str(Path(__file__).resolve().parents[1])

#: What read_sidecar says about a copy in each state that cannot be used.
PROBLEMS = {
    'pending': 'its status is "pending", not complete',
    'wrong schema': 'it is not a coordination sidecar of schema 1',
    'not JSON': 'it is not valid JSON',
    'symlink': 'it is a symlink',
    'directory': 'it is not a regular file',
    'unreadable': 'it cannot be read (PermissionError)',
    'fifo': 'it is not a regular file',
    'device': 'it is not a regular file',
}


def sidecar(holder, **changes):
    record = {'schema_version': 1, 'status': 'complete',
              'files': {'.merge-context.json': {'holder': holder}},
              'operators': [holder + '-op'], 'verifiers': [holder + '-ver']}
    record.update(changes)
    return json.dumps(record)


def expected_outcome(canonical, fallback):
    """``(outcome, copy)``: ('answer', copy), ('refuse', None) or ('legacy', None).

    The restore's rule, written out (kittrial-5bb.152): a good canonical copy wins; a
    symlinked canonical copy refuses; otherwise a good last-complete copy answers; and with
    no usable copy the backup is legacy only when NEITHER copy exists, else it refuses.
    """
    copy = expected_copy(canonical, fallback)
    if copy is not None:
        return 'answer', copy
    return ('legacy', None) if (canonical, fallback) == ('absent', 'absent') else ('refuse', None)


def expected_copy(canonical, fallback):
    """The restore's rule, written out: a good canonical copy wins; a symlinked copy the
    restore reaches refuses the whole sidecar; otherwise a good last-complete copy."""
    if canonical == 'good':
        return 'canonical'
    if canonical == 'symlink':
        return None
    return 'last-complete' if fallback == 'good' else None


class SidecarAgreementTests(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.unreadable = set()
        self.real_open = os.open

    def deployment(self, name):
        root = self.base / name
        (root / 'backups' / 'alpha').mkdir(parents=True)
        (root / 'deployment.private.json').write_text(
            json.dumps({'password': 'test-only', 'operators': ['canonical-op']}), encoding='utf-8')
        return root

    def make(self, path, state, holder):
        """Put one copy in ``state``; False when this platform cannot make it."""
        if state == 'good':
            path.write_text(sidecar(holder), encoding='utf-8')
        elif state == 'pending':
            path.write_text(sidecar(holder, status='pending'), encoding='utf-8')
        elif state == 'wrong schema':
            path.write_text(sidecar(holder, schema_version=2), encoding='utf-8')
        elif state == 'not JSON':
            path.write_text('{"schema_version": 1, "status": "complete", "files"', encoding='utf-8')
        elif state == 'symlink':
            # A link to a GOOD sidecar: following it would answer from it.
            target = path.parent.parent / ('target-' + path.name)
            target.write_text(sidecar(holder), encoding='utf-8')
            try:
                os.symlink(str(target), str(path))
            except (OSError, NotImplementedError):
                return False
        elif state == 'directory':
            path.mkdir()
        elif state == 'fifo':
            if not hasattr(os, 'mkfifo'):
                return False
            os.mkfifo(str(path))
        elif state == 'device':
            # A real character device (the /dev/null numbers); making one needs root.
            try:
                os.mknod(str(path), stat.S_IFCHR | 0o600, os.makedev(1, 3))
            except (AttributeError, OSError):
                return False
        elif state == 'unreadable':
            # Modes do not stop root or Windows, so the open itself is refused here.
            path.write_text(sidecar(holder), encoding='utf-8')
            self.unreadable.add(os.path.normcase(str(path)))
        return True

    def guarded_open(self, path, *args, **kwargs):
        if os.path.normcase(str(path)) in self.unreadable:
            raise PermissionError(13, 'Permission denied', str(path))
        return self.real_open(path, *args, **kwargs)

    def restore_choice(self, root):
        """The copy restore-new restores from: the holder in what coordination_backup returns."""
        return self.restore_outcome(root)[1]

    def restore_outcome(self, root):
        """``(outcome, copy)`` of the restore's own reader, as restore_outcome is defined above."""
        try:
            files = admin.coordination_backup(root, 'alpha')
        except ValueError as error:
            self.assertTrue(str(error).startswith('Incomplete coordination backup: no copy of the coordination '
                                                  'sidecar of backup alpha can be used ('), str(error))
            self.assertIn('--without-coordination', str(error))
            return 'refuse', None
        if files is None:
            return 'legacy', None
        return 'answer', files['.merge-context.json']['holder']

    def authority_outcome(self, root):
        try:
            result = admin.backup_authority(root, 'alpha')
        except ValueError:
            return 'refuse', None
        if result['sidecar'] is None:
            return 'legacy', None
        return 'answer', ('canonical' if result['sidecar'].endswith('.coordination.json') else 'last-complete')

    def test_backup_authority_and_restore_new_choose_the_same_copy_in_every_combination(self):
        tried = skipped = 0
        for index, (canonical, fallback) in enumerate((c, f) for c in STATES for f in STATES):
            root = self.deployment('combo-%d' % index)
            copies = {'canonical': root / 'backups' / 'alpha.coordination.json',
                      'last-complete': admin.last_complete_sidecar_path(root, 'alpha')}
            if not (self.make(copies['canonical'], canonical, 'canonical')
                    and self.make(copies['last-complete'], fallback, 'last-complete')):
                skipped += 1
                continue
            tried += 1
            with self.subTest(canonical=canonical, last_complete=fallback), \
                    patch.object(admin.os, 'open', side_effect=self.guarded_open):
                # The OUTCOME (kittrial-5bb.152): the same for both commands and the rule.
                outcome = expected_outcome(canonical, fallback)
                self.assertEqual(self.restore_outcome(root), outcome, 'restore-new')
                self.assertEqual(self.authority_outcome(root), outcome, 'backup-authority')
                want = expected_copy(canonical, fallback)
                self.assertEqual(self.restore_choice(root), want, 'restore-new')
                path, _ = (None, None)
                try:
                    path, _ = admin.coordination_sidecar_source(root, 'alpha')
                except ValueError:
                    pass
                self.assertEqual(None if path is None else
                                 ('canonical' if path == copies['canonical'] else 'last-complete'), want,
                                 'coordination_sidecar_source')
                states = {'canonical': canonical, 'last-complete': fallback}
                damaged = [(name, states[name]) for name in ('canonical', 'last-complete')
                           if states[name] not in ('good', 'absent')]
                shown = {name: copies[name].relative_to(root).as_posix() for name in copies}
                try:
                    result = admin.backup_authority(root, 'alpha')
                except ValueError as error:
                    # Refused: only when no copy is usable and one is damaged, and always as
                    # the damage report naming every damaged copy, never another error.
                    self.assertIsNone(want)
                    self.assertTrue(damaged)
                    self.assertTrue(str(error).startswith('The coordination sidecar of backup alpha is damaged: '),
                                    str(error))
                    for name, state in damaged:
                        self.assertIn('%s: %s' % (shown[name], PROBLEMS[state]), str(error))
                    continue
                chosen = {None: None, shown['canonical']: 'canonical',
                          shown['last-complete']: 'last-complete'}[result['sidecar']]
                self.assertEqual(chosen, want, 'backup-authority')
                # Its lists come from that copy and no other.
                self.assertEqual(result['operators']['recorded'], [want + '-op'] if want else [])
                self.assertEqual(result['verifiers']['recorded'], [want + '-ver'] if want else [])
                self.assertEqual(result.get('unusable', []),
                                 [{'copy': shown[name], 'problem': PROBLEMS[state]} for name, state in damaged])
                if want is None:
                    self.assertEqual(damaged, [])
                    self.assertIn('legacy backup', result['note'])
                # POSIX-style paths on every platform, Windows included.
                for text in [result['sidecar'] or ''] + [item['copy'] for item in result.get('unusable', [])]:
                    self.assertNotIn('\\', text)
        self.assertEqual(tried + skipped, len(STATES) ** 2)
        # Every combination of the portable states runs everywhere.
        self.assertGreaterEqual(tried, len(PORTABLE) ** 2)

    def test_a_good_canonical_copy_with_a_symlinked_last_complete_copy(self):
        # The reported case: restore-new restores from the canonical copy, and backup-authority
        # used to refuse the whole backup as damaged.
        root = self.deployment('reported')
        if not self.make(admin.last_complete_sidecar_path(root, 'alpha'), 'symlink', 'last-complete'):
            self.skipTest('symlinks unavailable')
        self.make(root / 'backups' / 'alpha.coordination.json', 'good', 'canonical')
        result = admin.backup_authority(root, 'alpha')
        self.assertEqual(result['sidecar'], 'backups/alpha.coordination.json')
        self.assertEqual(result['operators']['recorded'], ['canonical-op'])
        self.assertEqual(result['unusable'], [{'copy': 'backups/alpha.coordination.last-complete.json',
                                               'problem': 'it is a symlink'}])
        self.assertEqual(self.restore_choice(root), 'canonical')


@unittest.skipIf(sys.platform == 'win32', 'admin.py host commands take the POSIX lock')
class RestoreNewOutcomeTests(SidecarAgreementTests):
    """The real restore-new command, native steps faked, in every combination (kittrial-5bb.152)."""

    # Only the tests below; the inherited ones already ran in SidecarAgreementTests.
    test_backup_authority_and_restore_new_choose_the_same_copy_in_every_combination = None
    test_a_good_canonical_copy_with_a_symlinked_last_complete_copy = None

    def run_restore(self, root, *flags):
        """restore-new alpha beta through run_main: ``(rc, output, add_project calls)``,
        stdout and stderr in one stream in the order they were written."""
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_restore_native_sql as native
        calls = []

        def add_project(root_, name):
            calls.append(name)
            native.make_destination(root_, name)

        def spawn(command, handle, **kwargs):
            handle.pid, handle.finished = 77, True
            return ''

        both = io.StringIO()
        code = 0
        with patch.object(admin, 'add_project', side_effect=add_project), \
                patch.object(admin, 'run_bd', return_value='ok'), \
                patch.object(admin, 'spawn_sync_client', side_effect=spawn), \
                patch.object(admin, 'sql', side_effect=native.identity_sql(native.SOURCE_ID)), \
                patch.object(admin.os, 'open', side_effect=self.guarded_open), \
                patch.object(sys, 'argv', ['admin.py', '--root', str(root), 'restore-new', 'alpha', 'beta', *flags]), \
                patch.object(admin, 'root_path', return_value=root), \
                contextlib.redirect_stdout(both), contextlib.redirect_stderr(both):
            try:
                admin.run_main()
            except SystemExit as exit:
                code = exit.code if isinstance(exit.code, int) else 1
                if isinstance(exit.code, str):
                    both.write(exit.code + '\n')
        return code, both.getvalue(), calls

    def combination(self, index, canonical, fallback):
        root = self.deployment('restore-%d' % index)
        (root / 'projects').mkdir()
        made = (self.make(root / 'backups' / 'alpha.coordination.json', canonical, 'canonical')
                and self.make(admin.last_complete_sidecar_path(root, 'alpha'), fallback, 'last-complete'))
        return root if made else None

    def test_restore_new_and_backup_authority_reach_the_same_outcome_in_every_combination(self):
        tried = 0
        for index, (canonical, fallback) in enumerate((c, f) for c in STATES for f in STATES):
            for flag in ((), ('--without-coordination',)):
                root = self.combination(index * 2 + len(flag), canonical, fallback)
                if root is None:
                    continue
                tried += 1
                with self.subTest(canonical=canonical, last_complete=fallback, flags=flag):
                    outcome, copy = expected_outcome(canonical, fallback)
                    with patch.object(admin.os, 'open', side_effect=self.guarded_open):
                        self.assertEqual(self.authority_outcome(root), (outcome, copy), 'backup-authority')
                    code, output, calls = self.run_restore(root, *flag)
                    destination = root / 'projects' / 'beta'
                    context = destination / '.merge-context.json'
                    lines = output.rstrip('\n').splitlines()
                    if outcome == 'refuse' and not flag:
                        # Refused before anything exists, naming every damaged copy and the flag.
                        self.assertEqual(code, 1, output)
                        self.assertEqual((calls, destination.exists()), ([], False))
                        self.assertIn('ValueError: Incomplete coordination backup: no copy of the coordination '
                                      'sidecar of backup alpha can be used (', output)
                        self.assertIn('restore-new alpha DEST --without-coordination', output)
                        for name, state in (('alpha.coordination.json', canonical),
                                            ('alpha.coordination.last-complete.json', fallback)):
                            if state not in ('good', 'absent'):
                                self.assertIn('backups/%s: %s' % (name, PROBLEMS[state]), output)
                    elif outcome == 'refuse':
                        # --without-coordination: the native data only, and its last lines say so.
                        self.assertEqual(code, 0, output)
                        self.assertEqual(calls, ['beta'])
                        self.assertFalse(context.exists())
                        self.assertTrue(lines[-2].startswith('NOT restored (--without-coordination): the coordination '
                                                             'sidecar of backup alpha could not be used ('), lines[-2:])
                        self.assertEqual(lines[-1], 'The operation journal was not restored: the backup has no '
                                                    'operation-journal snapshot.')
                    elif outcome == 'answer' and flag:
                        # The flag is refused for a usable sidecar: it would drop restorable data.
                        self.assertEqual(code, 1, output)
                        self.assertIn('--without-coordination is only for a backup whose coordination sidecar '
                                      'cannot be used', output)
                        self.assertEqual((calls, destination.exists()), ([], False))
                    elif outcome == 'answer':
                        self.assertEqual(code, 0, output)
                        self.assertEqual(json.loads(context.read_text(encoding='utf-8')), {'holder': copy})
                        self.assertNotIn('NOT restored (--without-coordination)', output)
                    else:
                        # Legacy: no copy at all restores exactly as before, with or without the flag.
                        self.assertEqual(code, 0, output)
                        self.assertIn('Legacy backup has no coordination journal', output)
                        self.assertFalse(context.exists())
                        self.assertNotIn('NOT restored (--without-coordination)', output)
        self.assertGreaterEqual(tried, 2 * len(PORTABLE) ** 2)

    def test_the_five_combinations_that_used_to_restore_silently(self):
        # The reviewer's finding: canonical absent, last-complete damaged, restored as legacy
        # with no coordination data and rc 0. Now refused; with the flag, said at the end.
        for index, state in enumerate(('pending', 'not JSON', 'directory', 'unreadable', 'fifo')):
            root = self.combination(500 + index, 'absent', state)
            if root is None:
                continue
            with self.subTest(last_complete=state):
                code, output, calls = self.run_restore(root)
                self.assertEqual((code, calls), (1, []), output)
                self.assertNotIn('Legacy backup', output)

    def test_without_coordination_still_restores_a_journal_snapshot_and_says_so(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_backup_native_sync import journal_ids, write_live_journal
        root = self.combination(600, 'pending', 'absent')
        write_live_journal(admin.journal_snapshot_path(root, 'alpha'), ['op-1'])
        code, output, _ = self.run_restore(root, '--without-coordination')
        self.assertEqual(code, 0, output)
        self.assertEqual(journal_ids(root / 'projects' / 'beta' / admin.JOURNAL_STORE_NAME), {'op-1'})
        self.assertEqual(output.rstrip('\n').splitlines()[-1],
                         'The operation journal was restored from backups/alpha.http-operations.sqlite3.')

    def test_without_coordination_refuses_the_authority_flags(self):
        root = self.combination(700, 'absent', 'not JSON')
        for flag in ('--restore-operators', '--restore-verifiers'):
            with self.subTest(flag=flag):
                code, output, calls = self.run_restore(root, '--without-coordination', flag)
                self.assertEqual((code, calls), (1, []), output)
                self.assertIn('cannot be combined with --without-coordination', output)


class ReadSidecarTests(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)

    def bounded(self, call):
        """``call()`` in a daemon thread: a read that blocks fails the test instead of hanging it."""
        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(call()), daemon=True)
        worker.start()
        worker.join(20)
        self.assertFalse(worker.is_alive(), 'the read blocked')
        return outcome[0]

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFOs are POSIX-only')
    def test_a_fifo_in_place_of_a_sidecar_answers_at_once(self):
        fifo = self.base / 'alpha.coordination.json'
        os.mkfifo(str(fifo))
        self.assertEqual(self.bounded(lambda: admin.read_sidecar(fifo)), (None, 'it is not a regular file'))
        self.assertIsNone(self.bounded(lambda: admin.complete_sidecar(fifo)))
        # Even when the FIFO replaces a regular file after the type check: the open does
        # not block and the descriptor is checked.
        with patch.object(Path, 'is_file', return_value=True):
            self.assertEqual(self.bounded(lambda: admin.read_sidecar(fifo)), (None, 'it is not a regular file'))

    @unittest.skipUnless(os.path.exists('/dev/null'), 'no /dev/null device here')
    def test_a_device_in_place_of_a_sidecar_answers_at_once(self):
        device = Path('/dev/null')
        self.assertEqual(self.bounded(lambda: admin.read_sidecar(device)), (None, 'it is not a regular file'))
        with patch.object(Path, 'is_file', return_value=True):
            self.assertEqual(self.bounded(lambda: admin.read_sidecar(device)), (None, 'it is not a regular file'))

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFOs are POSIX-only')
    def test_backup_status_require_complete_with_a_fifo_for_a_sidecar_answers(self):
        # kittrial-5bb.152: the gate's own pair check opened the sidecar with read_text and
        # waited on the FIFO until a 90 s limit killed it. A real process, with a timeout,
        # so a regression fails here instead of hanging CI.
        root = self.base / 'status'
        (root / 'projects' / 'alpha' / '.beads').mkdir(parents=True)
        (root / 'projects' / 'alpha' / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (root / 'backups' / 'alpha').mkdir(parents=True)
        os.mkfifo(str(root / 'backups' / 'alpha.coordination.json'))
        record = admin.backup_status_record([{'name': 'alpha', 'status': 'complete',
                                              'completed_at': '2026-10-06T00:00:00Z'}], 'all', '2026-10-06T00:00:00Z')
        (root / 'backups' / admin.BACKUP_STATUS_NAME).write_text(json.dumps(record), encoding='utf-8')
        self.assertEqual(self.bounded(lambda: admin.backup_pair_state(root, 'alpha')),
                         (False, 'coordination sidecar is not a regular file'))
        done = subprocess.run([sys.executable, str(Path(REPO) / 'admin.py'), '--root', str(root), 'backup-status',
                               '--require-complete'], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn('alpha: coordination sidecar is not a regular file', done.stderr)

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'symlink'), 'needs O_NOFOLLOW and symlinks')
    def test_a_symlink_swapped_in_after_the_check_is_not_followed(self):
        target = self.base / 'target.json'
        target.write_text(sidecar('elsewhere'), encoding='utf-8')
        link = self.base / 'alpha.coordination.json'
        os.symlink(str(target), str(link))
        with patch.object(Path, 'is_symlink', return_value=False):      # the check already passed
            record, problem = admin.read_sidecar(link)
        self.assertIsNone(record)
        self.assertTrue(problem.startswith('it cannot be read ('), problem)

    def test_a_read_error_after_the_open_answers_instead_of_raising(self):
        copy = self.base / 'alpha.coordination.json'
        copy.write_text(sidecar('canonical'), encoding='utf-8')

        class Failing(io.RawIOBase):
            def __init__(self, fd):
                os.close(fd)
            def read(self, *args):
                raise OSError(5, 'Input/output error')

        with patch.object(admin.os, 'fdopen', side_effect=lambda fd, *a, **k: Failing(fd)):
            self.assertEqual(admin.read_sidecar(copy), (None, 'it cannot be read (OSError)'))

    @unittest.skipUnless(os.path.isdir('/proc/self/fd') and hasattr(os, 'mkfifo'), 'needs /proc/self/fd and FIFOs')
    def test_the_descriptor_is_closed_when_the_type_check_refuses(self):
        fifo = self.base / 'alpha.coordination.json'
        os.mkfifo(str(fifo))
        before = len(os.listdir('/proc/self/fd'))
        with patch.object(Path, 'is_file', return_value=True):          # reach the open and fstat
            for _ in range(20):
                self.assertEqual(admin.read_sidecar(fifo), (None, 'it is not a regular file'))
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)

    def deployment_with(self, **record):
        root = self.base / ('pins-%d' % len(list(self.base.iterdir())))
        (root / 'backups' / 'alpha').mkdir(parents=True)
        (root / 'deployment.private.json').write_text(json.dumps({'password': 'test-only'}), encoding='utf-8')
        (root / 'backups' / 'alpha.coordination.json').write_text(sidecar('canonical', **record), encoding='utf-8')
        return root

    def test_the_names_in_the_chosen_copy_are_validated(self):
        for key in ('operators', 'verifiers'):
            for bad in (['bad name'], ['-x'], 'not-a-list', [7]):
                with self.subTest(key=key, value=bad):
                    root = self.deployment_with(**{key: bad})
                    with self.assertRaises(ValueError):
                        admin.backup_authority(root, 'alpha')

    def test_the_verifiers_are_read_from_their_own_key(self):
        root = self.deployment_with(operators=['op-only'], verifiers=['ver-only'])
        result = admin.backup_authority(root, 'alpha')
        self.assertEqual((result['operators']['recorded'], result['verifiers']['recorded']),
                         (['op-only'], ['ver-only']))

    def test_a_sidecar_larger_than_30_mb_is_read(self):
        root = self.base / 'large'
        (root / 'backups' / 'alpha').mkdir(parents=True)
        (root / 'deployment.private.json').write_text(json.dumps({'password': 'test-only'}), encoding='utf-8')
        bundle = root / 'backups' / 'alpha.coordination.json'
        bundle.write_text(json.dumps({'schema_version': 1, 'status': 'complete', 'operators': ['big-op'],
                                      'files': {'.merge-context.json': {'holder': 'x' * (31 * 1024 * 1024)}}}),
                          encoding='utf-8')
        self.assertGreater(bundle.stat().st_size, 30 * 1024 * 1024)
        record, problem = admin.read_sidecar(bundle)
        self.assertIsNone(problem)
        self.assertEqual(len(record['files']['.merge-context.json']['holder']), 31 * 1024 * 1024)
        result = admin.backup_authority(root, 'alpha')
        self.assertEqual((result['sidecar'], result['operators']['recorded']),
                         ('backups/alpha.coordination.json', ['big-op']))


if __name__ == '__main__':
    unittest.main()
