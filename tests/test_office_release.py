"""Offline release build and unprivileged install round trip."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT/'tools'/'office_release.py'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tar_bytes(name, content, mode=0o644):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        item = tarfile.TarInfo(name)
        item.size = len(content)
        item.mode = mode
        archive.addfile(item, io.BytesIO(content))
    return output.getvalue()


def symlink_member(name, linkname):
    item = tarfile.TarInfo(name)
    item.type = tarfile.SYMTYPE
    item.linkname = linkname
    item.mode = 0o777
    return item


class ReleaseFixture(unittest.TestCase):
    """A source repository and the three archives a release is built from; no tests of its own."""

    #: Stand-ins that start and say a version, as the real binaries do (kittrial-5bb.161: the
    #: release tool starts every bundled binary at build and at install time).
    BD = b'#!/bin/sh\necho "bd version 1.2.2 (fixture)"\n'
    DOLT = b'#!/bin/sh\necho "dolt version 2.2.0"\n'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base/'source'
        self.repo.mkdir()
        subprocess.check_call(['git', 'init', '-q', str(self.repo)])
        subprocess.check_call(['git', '-C', str(self.repo), 'config', 'user.name', 'Test'])
        subprocess.check_call(['git', '-C', str(self.repo), 'config', 'user.email', 'test@example.invalid'])
        self.python_archive = self.base/'python.tar.gz'
        self.python_archive.write_bytes(tar_bytes('bin/python3',
            b'#!/bin/sh\necho Python 3.10.0\n', mode=0o755))
        self.bd_archive = self.base/'bd.tar.gz'
        self.dolt_archive = self.base/'dolt.tar.gz'
        self.bd_archive.write_bytes(tar_bytes('bd', self.BD, mode=0o755))
        self.dolt_archive.write_bytes(tar_bytes('bin/dolt', self.DOLT, mode=0o755))
        self.lock = {'bd': {'sha256': sha(self.bd_archive.read_bytes()), 'member': 'bd'},
                     'dolt': {'sha256': sha(self.dolt_archive.read_bytes()), 'member': 'bin/dolt'}}
        (self.repo/'versions.json').write_text(json.dumps(self.lock), encoding='utf-8')
        (self.repo/'VERSION').write_text('0.1.0\n', encoding='utf-8')
        (self.repo/'client.py').write_text('pass\n', encoding='utf-8')
        (self.repo/'version.py').write_text('pass\n', encoding='utf-8')
        (self.repo/'provenance.json').write_text('{"source_commit":"unknown"}\n', encoding='utf-8')
        self.commit()

    def commit(self):
        subprocess.check_call(['git', '-C', str(self.repo), 'add', '.'])
        if subprocess.call(['git', '-C', str(self.repo), 'diff', '--cached', '--quiet']):     # something changed
            subprocess.check_call(['git', '-C', str(self.repo), 'commit', '-qm', 'fixture'])
        return subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'],
                                       text=True).strip()

    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                              capture_output=True, text=True)

    def try_build(self, build_id, *more):
        output = self.base/(build_id+'.tar.gz')
        result = self.run_tool('build', '--repo', self.repo, '--commit', 'HEAD',
            '--build-id', build_id, '--python-archive', self.python_archive,
            '--python-sha256', sha(self.python_archive.read_bytes()),
            '--python-executable', 'bin/python3', '--bd-archive', self.bd_archive,
            '--dolt-archive', self.dolt_archive, '--output', output, *more)
        return result, output

    def build(self, build_id, *more):
        result, output = self.try_build(build_id, *more)
        self.assertEqual(result.returncode, 0, result.stderr)
        return output


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux release install only')
class OfficeReleaseTests(ReleaseFixture):
    def test_install_upgrade_and_rollback(self):
        first = self.build('build-a')
        install_root = self.base/'installation'
        for archive in (first,):
            result = self.run_tool('install', '--archive', archive,
                '--sha256', sha(archive.read_bytes()), '--install-root', install_root)
            self.assertEqual(result.returncode, 0, result.stderr)
        installed = install_root/'current'/'kit'
        provenance = json.loads((installed/'provenance.json').read_text(encoding='utf-8'))
        self.assertEqual(provenance['source_commit'], subprocess.check_output(
            ['git','-C',str(self.repo),'rev-parse','HEAD'], text=True).strip())
        self.assertEqual((installed/'vendor'/'bd.tar.gz').read_bytes(), self.bd_archive.read_bytes())
        (self.repo/'client.py').write_text('print(2)\n', encoding='utf-8')
        self.commit()
        second = self.build('build-b')
        result = self.run_tool('install', '--archive', second,
            '--sha256', sha(second.read_bytes()), '--install-root', install_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((install_root/'current').resolve().name, 'build-b')
        self.assertEqual((install_root/'previous').resolve().name, 'build-a')
        result = self.run_tool('rollback', '--install-root', install_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((install_root/'current').resolve().name, 'build-a')

    # -- forward again after a rollback (kittrial-5bb.189) --------------------------------

    def two_installed(self):
        """build-a then build-b installed (b current, a previous); returns (root, archive a, archive b)."""
        first = self.build('build-a')
        root = self.base/'installation'
        self.install(first, root)
        (self.repo/'client.py').write_text('print(2)\n', encoding='utf-8')
        self.commit()
        second = self.build('build-b')
        self.install(second, root)
        return root, first, second

    def install(self, archive, root, expect=0):
        result = self.run_tool('install', '--archive', archive, '--sha256', sha(archive.read_bytes()), '--install-root', root)
        self.assertEqual(result.returncode, expect, result.stderr + result.stdout)
        return result

    def links(self, root):
        return tuple((root/name).resolve().name if (root/name).is_symlink() else None for name in ('current', 'previous'))

    def tree(self, root):
        """Every file under releases/ with its content: an installed release is never written into."""
        return {str(path.relative_to(root)): path.read_bytes() for path in sorted((root/'releases').rglob('*'))
                if path.is_file() and not path.is_symlink()}

    def test_after_a_rollback_the_release_that_was_left_is_installed_again_by_its_archive(self):
        """It answered 'Release ID already installed', and an operator who had gone back could not go forward."""
        root, first, second = self.two_installed()
        back = self.run_tool('rollback', '--install-root', root)
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertEqual(self.links(root), ('build-a', 'build-b'))
        self.assertIn('To go forward again to releases/build-b: install its archive again, or `activate --release build-b`',
                      back.stdout)
        before = self.tree(root)
        forward = self.install(second, root)
        self.assertEqual(self.links(root), ('build-b', 'build-a'))
        self.assertIn('Current release is now build-b (it was installed already; its manifest is this archive\'s)', forward.stdout)
        self.assertIn('previous=releases/build-a', forward.stdout)
        self.assertIn('Restart the supervised service', forward.stdout)
        self.assertEqual(before, self.tree(root))                        # nothing was unpacked or written a second time
        self.assertEqual(sorted(p.name for p in (root/'releases').iterdir()), ['build-a', 'build-b'])

    def test_a_second_rollback_swaps_the_two_back_as_it_always_did(self):
        """What the document says was the only way forward before: nothing printed it."""
        root, first, second = self.two_installed()
        self.run_tool('rollback', '--install-root', root)
        again = self.run_tool('rollback', '--install-root', root)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.links(root), ('build-b', 'build-a'))
        self.assertIn('activate --release build-a', again.stdout)

    def test_installing_the_current_release_again_changes_nothing_and_says_so(self):
        root, first, second = self.two_installed()
        again = self.install(second, root)
        self.assertIn('Release build-b is already installed and is the current one; nothing was changed.', again.stdout)
        self.assertNotIn('Restart', again.stdout)
        self.assertEqual(self.links(root), ('build-b', 'build-a'))

    def test_going_back_to_an_older_installed_release_by_its_archive_works_the_same(self):
        """Not only the one `previous` names: any installed release, when its archive is at hand."""
        root, first, second = self.two_installed()
        (self.repo/'client.py').write_text('print(3)\n', encoding='utf-8')
        self.commit()
        third = self.build('build-c')
        self.install(third, root)
        self.assertEqual(self.links(root), ('build-c', 'build-b'))
        self.install(first, root)                                        # `rollback` could only reach build-b
        self.assertEqual(self.links(root), ('build-a', 'build-c'))

    def test_another_build_under_an_installed_id_is_refused_and_nothing_is_switched(self):
        root, first, second = self.two_installed()
        self.run_tool('rollback', '--install-root', root)
        (self.repo/'client.py').write_text('print("not the same")\n', encoding='utf-8')
        self.commit()
        second.unlink()
        other = self.build('build-b')                                    # the same id, another source
        refused = self.install(other, root, expect=1)
        self.assertIn('Release ID already installed, and what is installed under it is not this archive', refused.stderr)
        self.assertIn('A different build needs a build id of its own', refused.stderr)
        self.assertEqual(self.links(root), ('build-a', 'build-b'))

    def test_an_installed_release_that_is_incomplete_is_not_switched_to(self):
        root, first, second = self.two_installed()
        self.run_tool('rollback', '--install-root', root)
        (root/'releases'/'build-b'/'manifest.json').unlink()
        self.assertIn('already installed', self.install(second, root, expect=1).stderr)
        self.assertEqual(self.links(root), ('build-a', 'build-b'))

    def test_an_installed_release_whose_binary_no_longer_starts_is_not_switched_to(self):
        """The same check a new install makes before it switches: bd and dolt start on this host."""
        root, first, second = self.two_installed()
        self.run_tool('rollback', '--install-root', root)
        broken = tar_bytes('bd', b'#!/bin/sh\necho broken >&2\nexit 3\n', mode=0o755)
        kit = root/'releases'/'build-b'/'kit'
        lock = json.loads((kit/'versions.json').read_text(encoding='utf-8'))
        lock['bd']['sha256'] = sha(broken)
        (kit/'versions.json').write_text(json.dumps(lock), encoding='utf-8')
        (kit/'vendor'/'bd.tar.gz').write_bytes(broken)
        refused = self.install(second, root, expect=1)
        self.assertIn('bd cannot start on this host', refused.stderr)
        self.assertEqual(self.links(root), ('build-a', 'build-b'))
        refused = self.run_tool('activate', '--install-root', root, '--release', 'build-b')
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertEqual(self.links(root), ('build-a', 'build-b'))

    def test_activate_switches_to_an_installed_release_without_its_archive(self):
        root, first, second = self.two_installed()
        self.run_tool('rollback', '--install-root', root)
        before = self.tree(root)
        forward = self.run_tool('activate', '--install-root', root, '--release', 'build-b')
        self.assertEqual(forward.returncode, 0, forward.stderr)
        self.assertEqual(self.links(root), ('build-b', 'build-a'))
        self.assertIn('Current release is now build-b (it was installed already; not compared with an archive', forward.stdout)
        self.assertIn('Restart the supervised service', forward.stdout)
        self.assertEqual(before, self.tree(root))
        again = self.run_tool('activate', '--install-root', root, '--release', 'build-b')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn('already installed and is the current one; nothing was changed', again.stdout)

    def test_activate_refuses_what_is_not_an_installed_release(self):
        root, first, second = self.two_installed()
        outside = self.base/'elsewhere'
        outside.mkdir()
        (outside/'manifest.json').write_text('{"build_id": "elsewhere"}', encoding='utf-8')
        (root/'releases'/'linked').symlink_to(outside)
        (root/'releases'/'renamed').mkdir()
        (root/'releases'/'renamed'/'manifest.json').write_bytes((root/'releases'/'build-a'/'manifest.json').read_bytes())
        for release, said in (('build-z', 'No installed release build-z'), ('../build-a', 'Not a release id'),
                              ('', 'Not a release id'), ('linked', 'No installed release linked'),
                              ('renamed', 'says it is build-a')):
            with self.subTest(release=release):
                refused = self.run_tool('activate', '--install-root', root, '--release', release)
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn(said, refused.stderr)
                self.assertEqual(self.links(root), ('build-b', 'build-a'))

    def test_no_scratch_folder_is_left_by_the_start_check(self):
        root, first, second = self.two_installed()
        self.run_tool('rollback', '--install-root', root)
        scratch = Path(tempfile.gettempdir())
        before = set(scratch.glob('office-release-verify-*'))
        self.install(second, root)
        self.run_tool('rollback', '--install-root', root)
        self.assertEqual(self.run_tool('activate', '--install-root', root, '--release', 'build-b').returncode, 0)
        self.assertEqual(set(scratch.glob('office-release-verify-*')) - before, set())
        self.assertFalse((root/'releases'/'build-b'/'.dolt').exists())

    def test_bad_digest_cannot_switch_current(self):
        archive = self.build('build-a')
        install_root = self.base/'installation'
        result = self.run_tool('install', '--archive', archive,
            '--sha256', '0'*64, '--install-root', install_root)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((install_root/'current').exists())

    def test_link_chain_cannot_escape_extraction_root(self):
        from tools.office_release import safe_extract
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as output:
            for name in ('a/b/up', 'a/b/up/up2', 'a/b/up/up2/up3'):
                link = tarfile.TarInfo(name)
                link.type = tarfile.SYMTYPE
                link.linkname = '..'
                output.addfile(link)
            payload = b'escaped'
            item = tarfile.TarInfo('a/b/up/up2/up3/ESCAPED.txt')
            item.size = len(payload)
            output.addfile(item, io.BytesIO(payload))
        destination = self.base/'extract'
        with self.assertRaisesRegex(ValueError, 'symlink'):
            safe_extract(archive.getvalue(), destination)
        self.assertFalse((self.base/'ESCAPED.txt').exists())

    def test_deferred_symlink_targets_cannot_escape_extraction_root(self):
        # Each link is judged lexically when inserted (its target does not exist yet);
        # a later member completes the target so the link ends up outside the root.
        from tools.office_release import safe_extract
        variants = (
            (('l1', 'l2/..'), ('l2', '.')),
            (('l1', 'm/n/../..'), ('m', '.'), ('n', '.')),
        )
        for index, links in enumerate(variants):
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode='w') as output:
                for name, linkname in links:
                    output.addfile(symlink_member(name, linkname))
            destination = self.base/('deferred-%d' % index)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                safe_extract(archive.getvalue(), destination)

    def test_benign_symlink_still_extracts(self):
        from tools.office_release import safe_extract
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as output:
            payload = b'data'
            item = tarfile.TarInfo('pkg/real.txt')
            item.size = len(payload)
            item.mode = 0o644
            output.addfile(item, io.BytesIO(payload))
            output.addfile(symlink_member('pkg/link.txt', 'real.txt'))
            directory = tarfile.TarInfo('pkg/sub')
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            output.addfile(directory)
            output.addfile(symlink_member('pkg/sublink', 'sub'))
        destination = self.base/'benign'
        safe_extract(archive.getvalue(), destination)
        link = destination/'pkg'/'link.txt'
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(str(link)), 'real.txt')
        self.assertEqual(link.read_bytes(), b'data')
        self.assertTrue((destination/'pkg'/'sublink').is_dir())


if __name__ == '__main__':
    unittest.main()
