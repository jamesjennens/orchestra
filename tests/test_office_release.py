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
