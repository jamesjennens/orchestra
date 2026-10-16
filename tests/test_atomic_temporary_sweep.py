"""Sweep the leftover atomic temporaries a cut leaves behind (kittrial-5bb.286).

``coordination.atomic`` writes a unique ``<name>.tmp-<pid>-<16 hex>`` temporary beside
its target (kittrial-5bb.281 review item 1), so a crash between the temporary and its
rename leaves a file that is not a record. ``coordination.sweep_temporaries`` removes
exactly those files, only when older than a bound and never through a symlink;
``admin.py sweep-temporaries`` runs it over one project's own directory and families.
Nothing here touches the write path, so ``atomic`` itself is pinned by
test_read_lock_concurrency.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import admin
import coordination


def backdate(path, seconds=7200):
    """Make ``path`` old enough for a sweep bound of one hour."""
    stamp = time.time() - seconds
    os.utime(str(path), (stamp, stamp))


def symlink_or_skip(case, link, target):
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError) as exc:
        case.skipTest('symlink creation is not available: %s' % exc)


class SweepTemporariesTests(unittest.TestCase):
    """One directory: the shape, the age bound, symlinks and non-files."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='atomic-sweep-'))
        self.addCleanup(shutil.rmtree, str(self.base), True)

    def write(self, name, text='{}'):
        path = self.base / name
        path.write_text(text, encoding='utf-8')
        return path

    def test_only_the_exact_temporary_shape_is_removed(self):
        keep = ('record.json',                                   # not a temporary
                'record.json.tmp',                               # the private (mode=) form
                'record.json.tmp-12-0123456789abcdef',           # younger than the bound
                'record.json.tmp-x-0123456789abcdef',            # not a pid
                'record.json.tmp-12-0123456789ABCDEF',           # not lower-case hex
                'record.json.tmp-12-0123456789abcde',            # 15 hex digits
                '.tmp-1-0123456789abcdef',                       # no file name before the mark
                'record.json.tmp-12-0123456789abcdef.extra')     # trailing text
        for name in keep:
            self.write(name)
            if name != 'record.json.tmp-12-0123456789abcdef':
                backdate(self.base / name)
        stale = 'record.json.tmp-99-0123456789abcdef'
        self.write(stale)
        backdate(self.base / stale)

        removed = coordination.sweep_temporaries(self.base, min_age_seconds=3600)

        self.assertEqual(removed, [stale])
        self.assertFalse((self.base / stale).exists())
        for name in keep:
            self.assertTrue((self.base / name).exists(), name)

    def test_a_young_temporary_is_left_for_its_writer(self):
        name = 'record.json.tmp-7-0123456789abcdef'
        self.write(name)
        self.assertEqual(coordination.sweep_temporaries(self.base, min_age_seconds=3600), [])
        self.assertTrue((self.base / name).exists())

    def test_dry_run_names_a_candidate_without_removing_it(self):
        name = 'record.json.tmp-7-0123456789abcdef'
        self.write(name)
        backdate(self.base / name)
        self.assertEqual(
            coordination.sweep_temporaries(self.base, min_age_seconds=3600, dry_run=True), [name])
        self.assertTrue((self.base / name).exists())

    def test_a_temporary_named_symlink_is_never_followed_or_removed(self):
        outside = self.write('precious.txt', 'keep me')
        link = self.base / 'record.json.tmp-7-0123456789abcdef'
        symlink_or_skip(self, link, outside)

        self.assertEqual(coordination.sweep_temporaries(self.base, min_age_seconds=3600), [])
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_text(encoding='utf-8'), 'keep me')

    def test_a_temporary_named_directory_is_left_alone(self):
        folder = self.base / 'record.json.tmp-7-0123456789abcdef'
        folder.mkdir()
        backdate(folder)
        self.assertEqual(coordination.sweep_temporaries(self.base, min_age_seconds=3600), [])
        self.assertTrue(folder.is_dir())

    def test_a_symlinked_family_directory_is_refused(self):
        outside = self.base / 'outside'
        outside.mkdir()
        victim = outside / 'record.json.tmp-7-0123456789abcdef'
        victim.write_text('{}', encoding='utf-8')
        backdate(victim)
        link = self.base / 'family'
        symlink_or_skip(self, link, outside)

        with self.assertRaises(ValueError):
            coordination.sweep_temporaries(link, min_age_seconds=3600)
        self.assertTrue(victim.exists())

    def test_a_missing_family_is_not_an_error(self):
        self.assertEqual(
            coordination.sweep_temporaries(self.base / 'never-created', min_age_seconds=3600), [])

    def test_a_negative_bound_is_refused(self):
        with self.assertRaises(ValueError):
            coordination.sweep_temporaries(self.base, min_age_seconds=-1)


class AdminSweepTests(unittest.TestCase):
    """The operator command sweeps the project directory and its families and nothing else."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='atomic-sweep-admin-'))
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self.project = self.root / 'projects' / 'alpha'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')

    def test_the_command_sweeps_the_project_directory_and_every_family(self):
        names = ('record.json.tmp-7-0123456789abcdef', 'other.json.tmp-8-fedcba9876543210')
        for directory in admin.ATOMIC_SWEEP_DIRECTORIES:
            if directory != '.':
                (self.project / directory).mkdir()
            for name in names:
                (self.project / directory / name).write_text('{}', encoding='utf-8')
                backdate(self.project / directory / name)

        report = admin.sweep_project_temporaries(self.root, 'alpha', min_age_seconds=3600)

        self.assertEqual(report['directories'], list(admin.ATOMIC_SWEEP_DIRECTORIES))
        self.assertEqual(report['removed_total'], 2 * len(admin.ATOMIC_SWEEP_DIRECTORIES))
        for directory in admin.ATOMIC_SWEEP_DIRECTORIES:
            self.assertEqual(report['swept'][directory], sorted(names), directory)
            for name in names:
                self.assertFalse((self.project / directory / name).exists(), (directory, name))
        self.assertTrue((self.project / '.beads' / 'metadata.json').is_file())

    def test_a_record_beside_a_temporary_is_never_touched(self):
        family = '.coordination-requests'
        (self.project / family).mkdir()
        record = self.project / family / ('a' * 64 + '.json')
        record.write_text('{"keep": true}', encoding='utf-8')
        backdate(record)
        temporary = self.project / family / ('a' * 64 + '.json.tmp-7-0123456789abcdef')
        temporary.write_text('{}', encoding='utf-8')
        backdate(temporary)

        admin.sweep_project_temporaries(self.root, 'alpha', min_age_seconds=3600)

        self.assertEqual(record.read_text(encoding='utf-8'), '{"keep": true}')
        self.assertFalse(temporary.exists())

    def test_sweeping_an_untouched_project_creates_nothing(self):
        before = sorted(path.name for path in self.project.iterdir())
        admin.sweep_project_temporaries(self.root, 'alpha', min_age_seconds=3600)
        self.assertEqual(sorted(path.name for path in self.project.iterdir()), before)

    def test_an_unknown_project_is_refused(self):
        with self.assertRaises(ValueError):
            admin.sweep_project_temporaries(self.root, 'missing', min_age_seconds=3600)


if __name__ == '__main__':
    unittest.main()
