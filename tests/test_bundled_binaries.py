"""Every binary a release bundles must be able to start on the host it is installed on (kittrial-5bb.161).

The pinned bd 1.2.2 is linked against glibc 2.34 and cannot start on RHEL 8 (glibc 2.28);
the release tool bundled it without looking, and nothing noticed because every installation
ran on a newer host. These tests cover what the tool reads from a binary, what it refuses
at build and at install time, the second pinned bd (``bd_static``), and that the installer
stays within Python 3.6.
"""
import ast
import io
import json
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tools'))
sys.path.insert(0, str(ROOT/'tests'))
import office_release as tool
from test_office_release import ReleaseFixture, sha, tar_bytes

LINUX = sys.platform.startswith('linux')
LOADER = '/lib64/ld-linux-x86-64.so.2'


def make_elf(needs=(('libc.so.6', ('GLIBC_2.2.5', 'GLIBC_2.34')),), interpreter=LOADER, wide=True, little=True):
    """A minimal ELF image: one loaded segment, an interpreter and a dynamic segment naming ``needs``."""
    order = '<' if little else '>'
    strings, where = [b'\0'], {}

    def name(text):
        if text not in where:
            where[text] = sum(len(part) for part in strings)
            strings.append(text.encode('ascii') + b'\0')
        return where[text]
    verneed = b''
    for index, (library, symbols) in enumerate(needs):
        aux = b''.join(struct.pack(order + 'IHHII', 0, 0, 2 + position, name(symbol),
                                   0 if position == len(symbols) - 1 else 16)
                       for position, symbol in enumerate(symbols))
        verneed += struct.pack(order + 'HHIII', 1, len(symbols), name(library), 16,
                               0 if index == len(needs) - 1 else 16 + len(aux)) + aux
    table = b''.join(strings)
    loader = (interpreter.encode('ascii') + b'\0') if interpreter else b''
    header, program = (64, 56) if wide else (52, 32)
    count = 1 + bool(interpreter) + bool(needs)
    at_loader = header + program * count
    at_table = at_loader + len(loader)
    at_verneed = at_table + len(table)
    at_dynamic = at_verneed + len(verneed)
    entries = [(tool.DT_STRTAB, at_table), (tool.DT_VERNEED, at_verneed), (tool.DT_VERNEEDNUM, len(needs)), (0, 0)]
    dynamic = b''.join(struct.pack(order + ('qQ' if wide else 'iI'), tag, value) for tag, value in entries) if needs else b''
    total = at_dynamic + len(dynamic)

    def segment(kind, offset, size):
        if wide:
            return struct.pack(order + 'IIQQQQQQ', kind, 5, offset, offset, offset, size, size, 1)
        return struct.pack(order + 'IIIIIIII', kind, offset, offset, offset, size, size, 5, 1)
    segments = segment(tool.PT_LOAD, 0, total)
    if interpreter:
        segments += segment(tool.PT_INTERP, at_loader, len(loader))
    if needs:
        segments += segment(tool.PT_DYNAMIC, at_dynamic, len(dynamic))
    ident = b'\x7fELF' + bytes([2 if wide else 1, 1 if little else 2, 1]) + b'\0' * 9
    if wide:
        head = ident + struct.pack(order + 'HHIQQQIHHHHHH', 2, 62, 1, 0, header, 0, 0, header, program, count, 0, 0, 0)
    else:
        head = ident + struct.pack(order + 'HHIIIIIHHHHHH', 2, 3, 1, 0, header, 0, 0, header, program, count, 0, 0, 0)
    return head + segments + loader + table + verneed + dynamic


class ElfNeedsTests(unittest.TestCase):
    def test_what_a_dynamic_executable_needs(self):
        needs = tool.elf_needs(make_elf())
        self.assertEqual(needs, {'static': False, 'interpreter': LOADER, 'glibc': '2.34',
                                 'versions': {'libc.so.6': ['GLIBC_2.2.5', 'GLIBC_2.34']}})
        self.assertEqual(tool.describe_needs(needs), 'it needs glibc 2.34 or later')

    def test_the_highest_version_is_found_as_a_version_not_as_text(self):
        """2.4 is older than 2.14 and 2.34; the pinned bd names all three."""
        for symbols, highest in ((('GLIBC_2.4', 'GLIBC_2.14', 'GLIBC_2.3.4'), '2.14'), (('GLIBC_2.34', 'GLIBC_2.4'), '2.34'),
                                 (('GLIBC_2.9', 'GLIBC_2.10'), '2.10'), (('GLIBC_2.2.5',), '2.2.5')):
            with self.subTest(symbols=symbols):
                self.assertEqual(tool.elf_needs(make_elf((('libc.so.6', symbols),)))['glibc'], highest)

    def test_every_library_counts_and_other_names_do_not(self):
        needs = tool.elf_needs(make_elf((('libc.so.6', ('GLIBC_2.17', 'GLIBC_PRIVATE')), ('libpthread.so.0', ('GLIBC_2.28',)),
                                         ('libz.so.1', ('ZLIB_1.2.9',)))))
        self.assertEqual(needs['glibc'], '2.28')
        self.assertEqual(sorted(needs['versions']), ['libc.so.6', 'libpthread.so.0', 'libz.so.1'])

    def test_a_static_executable(self):
        needs = tool.elf_needs(make_elf(needs=(), interpreter=None))
        self.assertEqual((needs['static'], needs['interpreter'], needs['glibc'], needs['versions']), (True, None, None, {}))
        self.assertEqual(tool.describe_needs(needs), 'it is statically linked and needs no glibc')

    def test_a_dynamic_executable_that_names_no_version(self):
        needs = tool.elf_needs(make_elf(needs=(('libfoo.so', ('FOO_1',)),)))
        self.assertEqual((needs['static'], needs['glibc']), (False, None))
        self.assertEqual(tool.describe_needs(needs), 'it is dynamically linked and names no glibc version')

    def test_32_bit_and_big_endian_files_are_read_the_same(self):
        for wide in (True, False):
            for little in (True, False):
                with self.subTest(wide=wide, little=little):
                    self.assertEqual(tool.elf_needs(make_elf(wide=wide, little=little))['glibc'], '2.34')

    def test_what_is_not_elf_and_what_is_damaged(self):
        for data in (b'#!/bin/sh\necho hi\n', b'', b'MZ\x90\x00', b'\x7fEL'):
            self.assertIsNone(tool.elf_needs(data))
        self.assertEqual(tool.describe_needs(None), 'it is not an ELF executable')
        whole = make_elf()
        for label, data in (('cut in the header', whole[:40]), ('cut in the program headers', whole[:100]),
                            ('cut in the version table', whole[:-60]), ('an unknown class', whole[:4] + b'\x07' + whole[5:]),
                            ('an unknown byte order', whole[:5] + b'\x07' + whole[6:])):
            with self.subTest(damage=label), self.assertRaises(ValueError) as refused:
                tool.elf_needs(data)
            self.assertEqual(str(refused.exception), 'Not a readable ELF file')

    @unittest.skipUnless(LINUX, 'reads this interpreter as an ELF file')
    def test_a_real_executable_agrees_with_this_host(self):
        needs = tool.elf_needs(Path(sys.executable).resolve().read_bytes())
        self.assertIsNotNone(needs)
        here = tool.host_glibc()
        self.assertRegex(here or '', r'^\d+\.\d+')
        if needs['glibc']:                                         # it runs here, so it cannot need more than is here
            self.assertLessEqual(tool.version_tuple(needs['glibc']), tool.version_tuple(here))

    def test_versions_compare_as_numbers(self):
        self.assertLess(tool.version_tuple('2.4'), tool.version_tuple('2.28'))
        self.assertLess(tool.version_tuple('2.28'), tool.version_tuple('2.34'))
        self.assertLess(tool.version_tuple('2.3.4'), tool.version_tuple('2.4'))


class ArchiveMemberTests(unittest.TestCase):
    def archive(self, entries):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for name, content in entries:
                item = tarfile.TarInfo(name)
                if isinstance(content, bytes):
                    item.size, item.mode = len(content), 0o755
                    archive.addfile(item, io.BytesIO(content))
                else:
                    item.type, item.linkname = tarfile.SYMTYPE, content
                    archive.addfile(item)
        return output.getvalue()

    def test_the_named_file_is_found_also_behind_links_inside_the_archive(self):
        data = self.archive([('python/bin/python3.11', b'real'), ('python/bin/python3', 'python3.11'),
                             ('python/bin/python', 'python3'), ('dolt-linux-amd64/bin/dolt', b'dolt')])
        self.assertEqual(tool.archive_member(data, 'python/bin/python3.11'), b'real')
        self.assertEqual(tool.archive_member(data, 'python/bin/python'), b'real')
        self.assertEqual(tool.archive_member(data, './dolt-linux-amd64/bin/dolt'), b'dolt')

    def test_a_missing_file_a_link_out_of_the_archive_and_a_link_loop_are_refused(self):
        data = self.archive([('bin/out', '../../etc/passwd'), ('bin/a', 'b'), ('bin/b', 'a'), ('bin/real', b'x')])
        for name in ('bin/none', 'bin/out', 'bin/a'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                tool.archive_member(data, name)

    def test_which_pin_an_archive_is(self):
        first, second = tar_bytes('bd', b'one'), tar_bytes('bd', b'two')
        lock = {'bd': {'sha256': sha(first), 'member': 'bd'}, 'bd_static': {'sha256': sha(second), 'member': 'bd'},
                'dolt': {'sha256': sha(first)}}
        self.assertEqual((tool.pin_of(lock, 'bd', first), tool.pin_of(lock, 'bd', second)), ('bd', 'bd_static'))
        self.assertEqual(tool.vendored(lock, 'bd', second), b'two')
        with self.assertRaises(ValueError) as refused:
            tool.pin_of(lock, 'bd', tar_bytes('bd', b'three'))
        self.assertEqual(str(refused.exception), 'bd archive differs from versions.json pin')
        with self.assertRaises(ValueError):
            tool.pin_of(lock, 'dolt', second)                      # dolt has no second pin, and bd's is not dolt's


class InstallerLanguageTests(unittest.TestCase):
    def test_the_installer_is_python_3_6(self):
        """It runs under /usr/libexec/platform-python on RHEL 8."""
        source = (ROOT/'tools'/'office_release.py').read_text(encoding='utf-8')
        ast.parse(source, feature_version=(3, 6))
        for newer in ('capture_output', 'text=True', ':=', 'removeprefix', 'removesuffix', 'is_relative_to', 'shlex.join',
                      'from __future__ import annotations', 'dataclass', 'f\'', 'f"'):
            self.assertNotIn(newer, source, 'the installer must run on Python 3.6: %r' % newer)

    def test_the_real_lock_pins_a_static_bd_beside_the_release_one(self):
        lock = json.loads((ROOT/'versions.json').read_text(encoding='utf-8'))
        self.assertEqual(lock['bd']['sha256'], '8140098a51d3b81d5548d1c5e6db1a2d9930e5d141efe2a4bff7d079c4d321e8')
        self.assertIn('url', lock['bd'])                           # unchanged: what the live installations run
        static = lock['bd_static']
        self.assertEqual((static['version'], static['member']), (lock['bd']['version'], 'bd'))
        self.assertRegex(static['sha256'], r'^[0-9a-f]{64}$')
        self.assertNotIn('url', static)
        self.assertIn('tools/build_bd_static.sh', static['obtain'])
        script = (ROOT/'tools'/'build_bd_static.sh').read_text(encoding='utf-8')
        for fact in (static['built_from']['image'], static['built_from']['commit'], static['built_from']['go_sum_sha256'],
                     static['built_from']['tag'], 'CGO_ENABLED=0', '-trimpath', static['archive']):
            self.assertIn(fact, script)


@unittest.skipUnless(LINUX, 'Linux release install only')
class BuildCheckTests(ReleaseFixture):
    """What `build` refuses and records."""

    def bundle(self, name, payload, pin=None):
        archive = tar_bytes(self.lock[name]['member'], payload, mode=0o755)
        getattr(self, name + '_archive').write_bytes(archive)
        self.lock[pin or name] = {'sha256': sha(archive), 'member': self.lock[name]['member']}
        (self.repo/'versions.json').write_text(json.dumps(self.lock), encoding='utf-8')
        self.commit()

    def manifest(self, package):
        with tarfile.open(str(package)) as archive:
            return json.loads(archive.extractfile('manifest.json').read().decode('utf-8'))

    def test_a_binary_that_needs_a_newer_glibc_than_the_target_is_refused(self):
        """The fault: bd 1.2.2 needs glibc 2.34, RHEL 8 has 2.28."""
        self.bundle('bd', make_elf())
        refused, output = self.try_build('for-rhel8', '--target-glibc', '2.28')
        self.assertEqual(refused.returncode, 1)
        self.assertIn('office-release: bd needs glibc 2.34; the target has glibc 2.28. It could not start there. Bundle a '
                      'build of it for the target: for bd, the archive versions.json pins as bd_static', refused.stderr)
        self.assertFalse(output.exists())
        # For a target that has it, the same archive passes the glibc check.
        accepted, output = self.try_build('for-new', '--target-glibc', '2.34', '--no-run-check')
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(self.manifest(output)['binaries']['bd'], {'static': False, 'glibc': '2.34'})
        self.assertEqual(self.manifest(output)['target_glibc'], '2.34')

    def test_every_binary_too_new_is_named_and_a_bad_target_is_refused(self):
        self.bundle('bd', make_elf())
        self.bundle('dolt', make_elf((('libc.so.6', ('GLIBC_2.30',)),)))
        refused, _ = self.try_build('both', '--target-glibc', '2.28')
        self.assertIn('bd needs glibc 2.34; dolt needs glibc 2.30; the target has glibc 2.28', refused.stderr)
        for bad in ('rhel8', '2', '2.28; rm', ''):
            with self.subTest(target=bad):
                refused, _ = self.try_build('bad', '--target-glibc', bad)
                self.assertEqual(refused.returncode, 1)
                self.assertIn('--target-glibc must be a glibc version such as 2.28', refused.stderr)

    def test_the_glibc_versions_are_compared_as_versions(self):
        self.bundle('bd', make_elf())                                  # needs 2.34
        refused, _ = self.try_build('older', '--target-glibc', '2.4', '--no-run-check')
        self.assertIn('bd needs glibc 2.34; the target has glibc 2.4.', refused.stderr)      # 2.4 is older than 2.34
        self.bundle('bd', make_elf((('libc.so.6', ('GLIBC_2.9',)),)))
        accepted, _ = self.try_build('newer', '--target-glibc', '2.28', '--no-run-check')
        self.assertEqual(accepted.returncode, 0, accepted.stderr)       # 2.9 is older than 2.28

    def test_the_bundled_python_is_checked_against_the_target_too(self):
        self.python_archive.write_bytes(tar_bytes('bin/python3', make_elf((('libc.so.6', ('GLIBC_2.17', 'GLIBC_2.35')),)),
                                                  mode=0o755))
        refused, output = self.try_build('python-too-new', '--target-glibc', '2.28')
        self.assertEqual(refused.returncode, 1)
        self.assertIn('office-release: python: it needs glibc 2.35 or later', refused.stderr)
        self.assertIn('office-release: python needs glibc 2.35; the target has glibc 2.28.', refused.stderr)
        self.assertFalse(output.exists())

    def test_a_binary_that_cannot_start_on_the_build_host_stops_the_build(self):
        self.bundle('bd', b'#!/bin/sh\necho "bd: /lib64/libc.so.6: version \\`GLIBC_2.34\' not found (required by bd)" >&2\nexit 1\n')
        refused, output = self.try_build('cannot-start')
        self.assertEqual(refused.returncode, 1)
        self.assertIn("bd cannot start on this host (exit code 1): bd: /lib64/libc.so.6: version `GLIBC_2.34' not found",
                      refused.stderr)
        self.assertIn('This was the build host; if it is older than the target and cannot run the target\'s binaries, '
                      'build with --no-run-check', refused.stderr)
        self.assertFalse(output.exists())
        accepted, output = self.try_build('unchecked', '--no-run-check')
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_the_build_says_and_records_what_each_binary_needs(self):
        self.bundle('dolt', b'#!/bin/sh\necho "dolt version 2.2.0"\n')
        done, output = self.try_build('recorded')
        self.assertEqual(done.returncode, 0, done.stderr)
        for line in ('office-release: bd archive is the pinned entry bd', 'office-release: python: it is not an ELF executable',
                     'office-release: bd: it is not an ELF executable'):
            self.assertIn(line, done.stderr)
        manifest = self.manifest(output)
        self.assertEqual((manifest['binaries'], manifest['pins'], manifest['target_glibc']),
                         ({'python': None, 'bd': None, 'dolt': None}, {'bd': 'bd', 'dolt': 'dolt'}, None))
        self.assertEqual(done.stdout.strip(), '%s  %s' % (sha(output.read_bytes()), output))     # stdout is unchanged

    def test_the_static_bd_is_bundled_when_its_archive_is_given(self):
        dynamic = self.bd_archive.read_bytes()
        self.bundle('bd', make_elf(needs=(), interpreter=None), pin='bd_static')
        self.lock['bd'] = {'sha256': sha(dynamic), 'member': 'bd'}                  # the release pin stays as it was
        (self.repo/'versions.json').write_text(json.dumps(self.lock), encoding='utf-8')
        self.commit()
        done, output = self.try_build('office', '--target-glibc', '2.28', '--no-run-check')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('office-release: bd archive is the pinned entry bd_static', done.stderr)
        self.assertIn('office-release: bd: it is statically linked and needs no glibc', done.stderr)
        manifest = self.manifest(output)
        self.assertEqual((manifest['pins']['bd'], manifest['binaries']['bd']), ('bd_static', {'static': True, 'glibc': None}))
        # The other pinned archive still builds, as the pinned entry bd.
        self.bd_archive.write_bytes(dynamic)
        done, output = self.try_build('koopa')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.manifest(output)['pins']['bd'], 'bd')
        # An archive that is neither pin is refused, as before.
        self.bd_archive.write_bytes(tar_bytes('bd', b'#!/bin/sh\necho other\n', mode=0o755))
        refused, _ = self.try_build('neither')
        self.assertIn('office-release: bd archive differs from versions.json pin', refused.stderr)

    def test_a_missing_archive_path_is_a_sentence_not_a_bare_errno_line(self):
        """kittrial-5bb.166 item 4: a mistyped path printed `[Errno 2] No such file or directory` alone."""
        output = self.base/'missing-input.tar.gz'
        arguments = ['build', '--repo', self.repo, '--commit', 'HEAD', '--build-id', 'missing-input',
                     '--python-archive', self.python_archive,
                     '--python-sha256', sha(self.python_archive.read_bytes()),
                     '--python-executable', 'bin/python3', '--bd-archive', self.bd_archive,
                     '--dolt-archive', self.dolt_archive, '--output', output]
        for option in ('--bd-archive', '--dolt-archive'):
            with self.subTest(option=option):
                missing = str(self.base/('no-such' + option.replace('--', '-') + '.tar.gz'))
                attempt = list(arguments)
                attempt[attempt.index(option)+1] = missing
                refused = self.run_tool(*attempt)
                self.assertEqual(refused.returncode, 1)
                self.assertIn('office-release: The %s file does not exist: %s' % (option, missing), refused.stderr)
                self.assertNotIn('Errno', refused.stderr)
                self.assertFalse(output.exists())

    #: A stub bd that behaves like bd's detached usage-metrics child: a tight writer keeps
    #: refilling its event lock so the run check's scratch fills again while it is being removed
    #: (kittrial-5bb.166). A separate killer stops the writer after about two seconds, so the
    #: writer is tight enough to make the old single removal fail deterministically and still
    #: stops well inside the retry budget; both children discard their stdio, so the build does
    #: not wait for them, and the marker outside HOME proves the writer ran.
    KEEPS_WRITING = (b'#!/bin/sh\n'
                     b'echo "bd version 1.2.2 (writes its event lock)"\n'
                     b'if [ -n "$K166_MARKER" ]; then : > "$K166_MARKER"; fi\n'
                     b'(\n'
                     b'  while :; do\n'
                     b'    [ -d "$HOME/.beads/eventsData" ] || mkdir -p "$HOME/.beads/eventsData"\n'
                     b'    : > "$HOME/.beads/eventsData/eventkit.lock"\n'
                     b'  done\n'
                     b') >/dev/null 2>&1 &\n'
                     b'WRITER=$!\n'
                     b'( sleep 2; kill "$WRITER" 2>/dev/null; sleep 1; kill -9 "$WRITER" 2>/dev/null ) '
                     b'>/dev/null 2>&1 &\n'
                     b'exit 0\n')

    def test_a_run_check_that_keeps_writing_its_event_lock_neither_fails_nor_litters(self):
        import os
        from unittest import mock
        self.bundle('bd', self.KEEPS_WRITING)
        marker = self.base/'writer-ran'
        prefix = 'office-release-check-'
        before = set(self.scratch.glob(prefix+'*'))                 # the tool's own scratch folder (the fixture's setUp)
        with mock.patch.dict(os.environ, {'K166_MARKER': str(marker)}):
            done, output = self.try_build('leaves-an-event-lock')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), '%s  %s' % (sha(output.read_bytes()), output))
        self.assertTrue(marker.exists(), 'the stub bd did not run its writer')
        self.assertEqual(set(self.scratch.glob(prefix+'*')) - before, set())

    def test_a_scratch_that_cannot_be_removed_is_said_and_still_builds(self):
        """kittrial-5bb.166 item 4: cleanup never fails a build, but the build must say it stayed."""
        import argparse
        from unittest import mock
        output = self.base/'says-so.tar.gz'
        args = argparse.Namespace(
            repo=str(self.repo), commit='HEAD', build_id='says-so',
            python_archive=str(self.python_archive), python_sha256=sha(self.python_archive.read_bytes()),
            python_executable='bin/python3', bd_archive=str(self.bd_archive),
            dolt_archive=str(self.dolt_archive), output=str(output), target_glibc=None, no_run_check=False)
        scratch = self.base/'office-release-check-says-so'

        def mkdtemp(prefix='', dir=None):
            scratch.mkdir(parents=True, exist_ok=True)
            return str(scratch)
        said, printed = io.StringIO(), io.StringIO()
        with mock.patch.object(tool.tempfile, 'mkdtemp', mkdtemp), \
                mock.patch.object(tool, 'remove_scratch', lambda path: False), \
                mock.patch.object(sys, 'stderr', printed), mock.patch.object(sys, 'stdout', said):
            self.assertIsNone(tool.build(args))
        self.assertIn('office-release: could not remove the run-check scratch directory %s' % scratch,
                      printed.getvalue())
        self.assertTrue(output.exists())

    def test_the_run_check_starts_every_binary_with_metrics_off(self):
        """Each program's own switch stops its detached child; both were measured (kittrial-5bb.166)."""
        from unittest import mock

        class Said:
            returncode, stdout = 0, b'bd version 1.2.2\n'

        seen = {}

        def fake_run(command, **options):
            seen.update(options.get('env') or {})
            return Said()
        with mock.patch.object(tool.subprocess, 'run', fake_run):
            self.assertEqual(tool.starts('bd', '/bin/true', ['--version'], self.base), 'bd version 1.2.2')
        self.assertEqual(seen.get('BD_DISABLE_METRICS'), '1')
        # dolt re-executes itself as `dolt send-metrics` after `dolt version` returns unless this is set.
        self.assertEqual(seen.get('DOLT_DISABLE_EVENT_FLUSH'), '1')
        self.assertEqual(seen.get('HOME'), str(self.base))


@unittest.skipUnless(LINUX, 'the scratch removers are exercised on Linux')
class RemoveScratchTests(unittest.TestCase):
    """`remove_scratch` keeps looking and reports the truth (kittrial-5bb.166 item 4 mutants).

    Five mutants of the run check's scratch removal survived revision 1: not looking again
    after the first removal, stopping at the first look, always reporting success, losing the
    "could not remove" line in `build`, and `verify` no longer removing its scratch. Each test
    here pins one of those, so a mutation of that bookkeeping fails.
    """

    def scratch(self):
        made = Path(tempfile.mkdtemp(prefix='k166-remove-'))
        self.addCleanup(shutil.rmtree, str(made), True)
        return made

    def removals(self, recreate_on=()):
        """Do the real removals, and put the scratch back after the 1-based calls named."""
        from unittest import mock
        real, seen = shutil.rmtree, []

        def rmtree(path, **options):
            real(path, **options)
            seen.append(str(path))
            if len(seen) in recreate_on:
                Path(path).mkdir()
                (Path(path)/'late').write_text('x', encoding='utf-8')
        patcher = mock.patch.object(tool.shutil, 'rmtree', rmtree)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_it_keeps_looking_until_the_scratch_stays_gone(self):
        from unittest import mock
        scratch = self.scratch()
        seen = self.removals(recreate_on=(1, 2))          # a late writer undoes the first two removals
        with mock.patch.object(tool, 'SCRATCH_PAUSE', 0):
            self.assertTrue(tool.remove_scratch(scratch))
        self.assertFalse(scratch.exists())
        # It may only say success after a removal that then stayed gone for a whole quiet pause
        # (SCRATCH_QUIET = 2): the third removal is the first quiet one, the fourth confirms it.
        self.assertEqual(len(seen), 4)

    def test_it_reports_failure_when_the_scratch_never_stays_gone(self):
        from unittest import mock
        scratch = self.scratch()
        self.removals(recreate_on=range(1, 20))           # a writer that refills it after every removal
        with mock.patch.object(tool, 'SCRATCH_TRIES', 3), mock.patch.object(tool, 'SCRATCH_PAUSE', 0):
            self.assertFalse(tool.remove_scratch(scratch))
        self.assertTrue(scratch.exists())


@unittest.skipUnless(LINUX, 'Linux release install only')
class InstallCheckTests(ReleaseFixture):
    """What `install` and `verify` do on the host itself."""

    FAILS = (b'#!/bin/sh\necho "bd: /lib64/libc.so.6: version \\`GLIBC_2.32\' not found (required by bd)" >&2\n'
             b'echo "bd: /lib64/libc.so.6: version \\`GLIBC_2.34\' not found (required by bd)" >&2\nexit 1\n')

    def install(self, package, root):
        return self.run_tool('install', '--archive', package, '--sha256', sha(package.read_bytes()), '--install-root', root)

    def rebundle(self, name, payload):
        archive = tar_bytes(self.lock[name]['member'], payload, mode=0o755)
        getattr(self, name + '_archive').write_bytes(archive)
        self.lock[name] = {'sha256': sha(archive), 'member': self.lock[name]['member']}
        (self.repo/'versions.json').write_text(json.dumps(self.lock), encoding='utf-8')
        self.commit()

    def test_a_release_whose_bd_cannot_start_here_is_not_installed(self):
        root = self.base/'installation'
        good = self.build('good')
        self.assertEqual(self.install(good, root).returncode, 0)
        self.rebundle('bd', self.FAILS)
        bad = self.build('bad', '--no-run-check')
        refused = self.install(bad, root)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("office-release: bd cannot start on this host (exit code 1): bd: /lib64/libc.so.6: version "
                      "`GLIBC_2.32' not found (required by bd). The file says it is not an ELF executable; this host has glibc ",
                      refused.stderr)
        self.assertIn('A release must carry a build of it for this host (docs/OFFICE_SERVICE.md)', refused.stderr)
        # Nothing was switched and nothing of the refused release is left behind.
        self.assertEqual((root/'current').resolve().name, 'good')
        self.assertFalse((root/'previous').exists())
        self.assertEqual(sorted(entry.name for entry in (root/'releases').iterdir()), ['good'])

    def test_dolt_and_the_bundled_python_are_checked_the_same_way(self):
        root = self.base/'installation'
        self.rebundle('dolt', b'#!/bin/sh\nexit 3\n')
        refused = self.install(self.build('no-dolt', '--no-run-check'), root)
        self.assertEqual(refused.returncode, 1)
        self.assertIn('office-release: dolt cannot start on this host (exit code 3): it said nothing.', refused.stderr)
        self.assertFalse((root/'current').exists())
        self.rebundle('dolt', self.DOLT)
        self.python_archive.write_bytes(tar_bytes('bin/python3', b'#!/bin/sh\necho "no such loader" >&2\nexit 127\n', mode=0o755))
        refused = self.install(self.build('no-python'), root)
        self.assertEqual(refused.returncode, 1)
        self.assertIn('office-release: The bundled Python cannot start on this host (exit code 127): no such loader.',
                      refused.stderr)
        self.assertFalse((root/'current').exists())

    def test_a_file_that_is_no_program_at_all_is_refused_with_the_reason(self):
        root = self.base/'installation'
        self.rebundle('bd', make_elf())                              # an ELF header and nothing to run
        refused = self.install(self.build('not-runnable', '--no-run-check'), root)
        self.assertEqual(refused.returncode, 1)
        self.assertIn('office-release: bd cannot start on this host', refused.stderr)
        self.assertIn('The file says it needs glibc 2.34 or later; this host has glibc ', refused.stderr)
        self.assertFalse((root/'current').exists())

    def test_a_program_named_by_a_relative_path_is_still_started(self):
        """It is started from a scratch directory; found on the real binaries inside the glibc 2.28 container."""
        import os
        with tempfile.TemporaryDirectory() as scratch, tempfile.TemporaryDirectory() as elsewhere:
            program = Path(elsewhere)/'bd'
            program.write_bytes(self.BD)
            program.chmod(0o755)
            before = os.getcwd()
            os.chdir(elsewhere)
            try:
                self.assertEqual(tool.starts('bd', 'bd', ['--version'], scratch), 'bd version 1.2.2 (fixture)')
            finally:
                os.chdir(before)

    def test_the_scratch_copies_are_removed_and_the_installed_release_is_whole(self):
        root = self.base/'installation'
        self.assertEqual(self.install(self.build('clean'), root).returncode, 0)
        release = root/'releases'/'clean'
        self.assertEqual(sorted(entry.name for entry in release.iterdir()), ['kit', 'manifest.json', 'python-runtime'])
        self.assertEqual(sorted(entry.name for entry in (release/'kit'/'vendor').iterdir()), ['bd.tar.gz', 'dolt.tar.gz'])

    def test_verify_says_which_bd_the_release_carries_and_that_it_starts(self):
        root = self.base/'installation'
        for name in ('admin.py', 'office_service.py', 'http_service.py'):
            (self.repo/name).write_text('pass\n', encoding='utf-8')
        self.python_archive.write_bytes(tar_bytes(
            'bin/python3', b'#!/bin/sh\nif [ "$1" = "--version" ]; then echo Python 3.10.0; fi\nexit 0\n', mode=0o755))
        static = tar_bytes('bd', b'#!/bin/sh\necho "bd version 1.2.2 (static fixture)"\n', mode=0o755)
        self.lock['bd_static'] = {'sha256': sha(static), 'member': 'bd'}
        (self.repo/'versions.json').write_text(json.dumps(self.lock), encoding='utf-8')
        self.commit()
        self.bd_archive.write_bytes(static)
        self.assertEqual(self.install(self.build('with-static'), root).returncode, 0)
        verified = self.run_tool('verify', '--install-root', root)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn('bd starts on this host: (bd_static) bd version 1.2.2 (static fixture)', verified.stdout)
        self.assertIn('dolt starts on this host: dolt version 2.2.0', verified.stdout)
        # A release that stops starting (the host changed under it) fails verify with the same sentence.
        (root/'current'/'kit'/'vendor'/'bd.tar.gz').write_bytes(tar_bytes('bd', self.FAILS, mode=0o755))
        lock = dict(self.lock, bd={'sha256': sha(tar_bytes('bd', self.FAILS, mode=0o755)), 'member': 'bd'})
        (root/'current'/'kit'/'versions.json').write_text(json.dumps(lock), encoding='utf-8')
        failed = self.run_tool('verify', '--install-root', root)
        self.assertEqual(failed.returncode, 1)
        self.assertIn('office-release: bd cannot start on this host (exit code 1)', failed.stderr)

    def test_verify_removes_its_own_scratch_directory(self):
        """kittrial-5bb.166 item 4: verify must remove the scratch it made, not only the one inside it."""
        root = self.base/'installation'
        self.assertEqual(self.install(self.build('verify-scratch'), root).returncode, 0)
        prefix = 'office-release-verify-'
        before = set(self.scratch.glob(prefix+'*'))                 # the tool's own scratch folder (the fixture's setUp)
        verified = self.run_tool('verify', '--install-root', root)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(set(self.scratch.glob(prefix+'*')) - before, set())


@unittest.skipUnless(LINUX, 'bootstrap installs Linux binaries only')
class BootstrapPinTests(unittest.TestCase):
    """`bootstrap.install` with one or two pinned archives for bd."""

    def setUp(self):
        import bootstrap
        self.bootstrap = bootstrap
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.assets = self.base/'vendor'
        self.assets.mkdir()
        self.release, self.static = tar_bytes('bd', b'release bd'), tar_bytes('bd', b'static bd')
        self.dolt = tar_bytes('bin/dolt', b'dolt')
        (self.assets/'dolt.tar.gz').write_bytes(self.dolt)
        self.lock = {'bd': {'version': '1.2.2', 'sha256': sha(self.release), 'member': 'bd', 'url': 'http://127.0.0.1:9/none'},
                     'bd_static': {'version': '1.2.2', 'sha256': sha(self.static), 'member': 'bd'},
                     'dolt': {'version': '2.2.0', 'sha256': sha(self.dolt), 'member': 'bin/dolt', 'url': 'http://127.0.0.1:9/none'}}

    def install(self, root, bd):
        from unittest import mock
        (self.assets/'bd.tar.gz').write_bytes(bd)
        real = Path.read_text

        def read_text(path, *args, **kwargs):
            if Path(path).name == 'versions.json' and Path(path).parent == Path(self.bootstrap.__file__).parent:
                return json.dumps(self.lock)
            return real(path, *args, **kwargs)
        printed = io.StringIO()
        with mock.patch.object(Path, 'read_text', read_text), mock.patch.object(sys, 'stdout', printed):
            self.bootstrap.install(root, asset_dir=self.assets)
        return printed.getvalue()

    def receipt(self, root):
        return json.loads((root/'bin'/'bd.receipt.json').read_text())

    def test_the_bundled_archive_is_installed_whichever_pin_it_is(self):
        for label, archive, payload, pin in (('static', self.static, b'static bd', 'bd_static'), ('release', self.release, b'release bd', 'bd')):
            with self.subTest(bundled=label):
                root = self.base/('runtime-' + label)
                said = self.install(root, archive)
                self.assertEqual((root/'bin'/'bd').read_bytes(), payload)
                self.assertEqual((self.receipt(root)['pin'], self.receipt(root)['archive_sha256']), (pin, sha(archive)))
                self.assertIn('bd: installed verified 1.2.2' + (' (bd_static)' if pin == 'bd_static' else ''), said)
                self.assertIn('bd: verified existing 1.2.2', self.install(root, archive))      # again: nothing replaced

    def test_an_archive_that_is_neither_pin_is_refused(self):
        with self.assertRaises(SystemExit) as refused:
            self.install(self.base/'runtime', tar_bytes('bd', b'something else'))
        self.assertEqual(str(refused.exception), 'bd: archive checksum mismatch')
        self.assertFalse((self.base/'runtime'/'bin'/'bd').exists())

    def test_a_runtime_in_use_keeps_the_bd_it_has(self):
        """prepare never swaps one pinned bd for the other under a running installation."""
        root = self.base/'runtime'
        self.install(root, self.release)
        said = self.install(root, self.static)                      # the next release bundles the static one
        self.assertEqual((root/'bin'/'bd').read_bytes(), b'release bd')
        self.assertIn('bd: verified existing 1.2.2\n', said)
        self.assertEqual(self.receipt(root)['pin'], 'bd')

    def test_a_binary_that_was_changed_or_whose_pin_is_gone_is_refused(self):
        root = self.base/'runtime'
        self.install(root, self.static)
        (root/'bin'/'bd').write_bytes(b'tampered')
        with self.assertRaises(SystemExit) as refused:
            self.install(root, self.static)
        self.assertIn('Binary/pin mismatch', str(refused.exception))
        (root/'bin'/'bd').write_bytes(b'static bd')
        del self.lock['bd_static']
        with self.assertRaises(SystemExit) as refused:
            self.install(root, self.static)
        self.assertIn('Binary/pin mismatch', str(refused.exception))

    def test_a_download_is_only_ever_the_release_pin(self):
        """With no bundled archives the url of "bd" is fetched; the static pin has none and is never accepted for it."""
        from unittest import mock

        class Response:
            def __init__(self, data):
                self.data = data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return self.data
        served = {'bd': self.static}                                # a server handing out the other pinned archive
        real = Path.read_text

        def read_text(path, *args, **kwargs):
            if Path(path).name == 'versions.json':
                return json.dumps(self.lock)
            return real(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', read_text), \
                mock.patch.object(self.bootstrap.urllib.request, 'urlopen', lambda url, timeout: Response(served['bd'])), \
                self.assertRaises(SystemExit) as refused:
            self.bootstrap.install(self.base/'runtime')
        self.assertEqual(str(refused.exception), 'bd: archive checksum mismatch')


@unittest.skipUnless(LINUX, 'starts a stand-in program')
class VerifyRecordTests(unittest.TestCase):
    """tools/office_verify.py says which pinned bd the runtime carries and whether it starts."""

    def setUp(self):
        import office_verify
        self.verify = office_verify
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root, self.kit = Path(self.tmp.name)/'runtime', Path(self.tmp.name)/'kit'
        (self.root/'bin').mkdir(parents=True)
        self.kit.mkdir()
        (self.kit/'versions.json').write_text(json.dumps({'bd': {'sha256': 'a' * 64}, 'bd_static': {'sha256': 'b' * 64}}))

    def carry(self, script, archive_sha):
        program = self.root/'bin'/'bd'
        program.write_bytes(script)
        program.chmod(0o755)
        (self.root/'bin'/'bd.receipt.json').write_text(json.dumps({'archive_sha256': archive_sha}))
        return self.verify.bundled_bd(self.root, self.kit)

    def test_the_record(self):
        self.assertEqual(self.carry(b'#!/bin/sh\necho "bd version 1.2.2 (6c124203e)"\n', 'b' * 64),
                         {'pin': 'bd_static', 'starts': True, 'says': 'bd version 1.2.2 (6c124203e)'})
        self.assertEqual(self.carry(b'#!/bin/sh\necho "bd version 1.2.2 (6c124203e)"\n', 'a' * 64)['pin'], 'bd')
        failed = self.carry(b'#!/bin/sh\necho "bd: /lib64/libc.so.6: version \\`GLIBC_2.34\' not found" >&2\nexit 1\n', 'a' * 64)
        self.assertEqual((failed['pin'], failed['starts']), ('bd', False))
        self.assertIn("GLIBC_2.34' not found", failed['says'])
        self.assertEqual(self.carry(b'#!/bin/sh\necho hi\n', 'c' * 64)['pin'], None)       # an archive that is neither pin
        (self.root/'bin'/'bd').unlink()
        missing = self.verify.bundled_bd(self.root, self.kit)
        self.assertEqual((missing['starts'], bool(missing['says'])), (False, True))


@unittest.skipUnless(LINUX, 'reads an installed release and starts a stand-in program')
class VerifyMainWiringTests(unittest.TestCase):
    """`office_verify.main` puts `bundled_bd` in the record and its outcome in the exit status.

    `bundled_bd` itself is covered above; kittrial-5bb.166 item 3: two mutations survived
    because only the function was tested, not its wiring into the entry point the operator
    runs — dropping the `bd` entry from the record, and main ignoring a bd that cannot start.
    """

    def setUp(self):
        import office_verify
        self.verify = office_verify
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base/'runtime'
        self.install = base/'installation'
        (self.root/'bin').mkdir(parents=True)
        release = self.install/'releases'/'build-1'
        kit = release/'kit'
        kit.mkdir(parents=True)
        contents = {'client.py': b'pass\n'}
        for name, data in contents.items():
            (kit/name).write_bytes(data)
        (kit/'VERSION').write_text('0.1.0\n', encoding='utf-8')
        (kit/'provenance.json').write_text(json.dumps({
            'schema_version': 1, 'component': 'orchestra-kit', 'version': '0.1.0',
            'source_commit': 'unknown', 'build_id': 'build-1',
            'files': {name: sha(data) for name, data in contents.items()}}), encoding='utf-8')
        (kit/'versions.json').write_text(json.dumps({'bd': {'sha256': 'a' * 64}}), encoding='utf-8')
        (release/'manifest.json').write_text(json.dumps(
            {'build_id': 'build-1', 'source_commit': 'unknown'}), encoding='utf-8')
        (self.install/'current').symlink_to('releases/build-1')

    def carry(self, script):
        program = self.root/'bin'/'bd'
        program.write_bytes(script)
        program.chmod(0o755)
        (self.root/'bin'/'bd.receipt.json').write_text(json.dumps({'archive_sha256': 'a' * 64}),
                                                       encoding='utf-8')

    def run_main(self):
        from unittest import mock
        import office_service
        printed = io.StringIO()
        # main's tools/office_verify.py calls health(root, port, settings) since kittrial-5bb.163;
        # a two-argument stub raises TypeError on the merge even though it passes on the old base.
        healthy = lambda root, port, settings=None: ('version=0.1.0 db=up web=up backup=complete backup_at=x', 0)
        with mock.patch.object(office_service, 'health', healthy), mock.patch.object(sys, 'stdout', printed):
            code = self.verify.main(['--install-root', str(self.install), '--root', str(self.root), '--port', '1'])
        return code, json.loads(printed.getvalue())

    def test_a_bd_that_cannot_start_is_in_the_record_and_makes_main_exit_nonzero(self):
        self.carry(b'#!/bin/sh\necho "bd: /lib64/libc.so.6: version \\`GLIBC_2.34\' not found" >&2\nexit 1\n')
        code, record = self.run_main()
        self.assertEqual(record['bd']['pin'], 'bd')
        self.assertFalse(record['bd']['starts'])
        self.assertIn("GLIBC_2.34' not found", record['bd']['says'])
        self.assertEqual(code, 1)

    def test_a_bd_that_starts_is_in_the_record_and_main_exits_zero(self):
        self.carry(b'#!/bin/sh\necho "bd version 1.2.2 (6c124203e)"\n')
        code, record = self.run_main()
        self.assertEqual(record['bd'], {'pin': 'bd', 'starts': True, 'says': 'bd version 1.2.2 (6c124203e)'})
        self.assertEqual(code, 0)


if __name__ == '__main__':
    unittest.main()
