"""backup-authority answers from exactly the sidecar copy restore-new uses (kittrial-5bb.150).

A backup has two copies of its coordination sidecar: the canonical one and the durable
last-complete copy. Each copy is put in every state below and every pair is tried. For
each pair, the copy ``backup-authority`` answers from (or that it refuses) is compared
with the copy ``restore-new`` reads (``coordination_backup``, the restore's own reader)
and with the rule written out by hand. Each copy carries a different holder, so the copy
that was used is visible in what is read, not inferred from a path.

These tests take no lock and run on every platform; only the symlink and FIFO cases need
what the platform can make.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin

STATES = ('good', 'pending', 'wrong schema', 'not JSON', 'symlink', 'directory', 'unreadable', 'absent')

#: What read_sidecar says about a copy in each state that cannot be used.
PROBLEMS = {
    'pending': 'its status is "pending", not complete',
    'wrong schema': 'it is not a coordination sidecar of schema 1',
    'not JSON': 'it is not valid JSON',
    'symlink': 'it is a symlink',
    'directory': 'it is not a regular file',
    'unreadable': 'it cannot be read (PermissionError)',
}


def sidecar(holder, **changes):
    record = {'schema_version': 1, 'status': 'complete',
              'files': {'.merge-context.json': {'holder': holder}},
              'operators': [holder + '-op'], 'verifiers': [holder + '-ver']}
    record.update(changes)
    return json.dumps(record)


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
        try:
            files = admin.coordination_backup(root, 'alpha')
        except ValueError:
            return None
        return None if files is None else files['.merge-context.json']['holder']

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
        # Every combination without a symlink runs everywhere.
        self.assertGreaterEqual(tried, (len(STATES) - 1) ** 2)

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
