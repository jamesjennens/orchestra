"""The canonical test stub's save: unique temp name and a short retry on replace (kittrial-5bb.86).

On Windows ``os.replace`` over a destination another process holds open raises
``PermissionError``; a fixed ``<destination>.tmp`` also lets two writers collide. Both are
why the HTTP tests flaked on a loaded Windows machine. These cases pin the fix and use only
portable imports, so they run on every platform.
"""
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
STUB_PATH = KIT / 'tools' / 'strict_canonical_endpoint.py'


def load_stub():
    spec = importlib.util.spec_from_file_location('strict_canonical_endpoint_save_case', STUB_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stub = load_stub()


class SaveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='stub-save-', dir=os.environ.get('TMPDIR')))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / 'canonical.json'

    def temp_files(self):
        return sorted(str(path) for path in self.tmp.glob('*.tmp'))

    def test_a_transient_permission_error_is_retried(self):
        real = os.replace
        calls = []

        def flaky(source, destination):
            calls.append(Path(source))
            if len(calls) < 3:
                raise PermissionError(13, 'The process cannot access the file', str(destination))
            return real(source, destination)

        with mock.patch.object(stub.os, 'replace', flaky):
            stub.save(self.path, {'rows': [1], 'seq': 1})

        self.assertEqual(3, len(calls))
        self.assertEqual({'rows': [1], 'seq': 1}, json.loads(self.path.read_text(encoding='utf-8')))
        self.assertEqual([], self.temp_files())

    def test_two_saves_use_different_temp_names_in_the_destination_directory(self):
        real = os.replace
        sources = []

        def capture(source, destination):
            sources.append(Path(source))
            return real(source, destination)

        with mock.patch.object(stub.os, 'replace', capture):
            stub.save(self.path, {'seq': 1})
            stub.save(self.path, {'seq': 2})

        self.assertEqual(2, len(sources))
        self.assertNotEqual(sources[0], sources[1])
        for source in sources:
            self.assertEqual(self.tmp, source.parent)          # same directory (drive letter kept)
            self.assertTrue(source.name.startswith(self.path.name + '.'))
            self.assertTrue(source.name.endswith('.tmp'))
            self.assertNotEqual(self.path.with_name(self.path.name + '.tmp'), source)

    def test_a_replace_that_never_succeeds_raises_and_leaves_no_temp(self):
        def denied(source, destination):
            raise PermissionError(13, 'The process cannot access the file', str(destination))

        with mock.patch.object(stub.os, 'replace', denied), mock.patch.object(stub.time, 'sleep'):
            with self.assertRaises(PermissionError):
                stub.save(self.path, {'seq': 1})

        self.assertFalse(self.path.exists())
        self.assertEqual([], self.temp_files())

    def test_a_successful_save_leaves_no_temp(self):
        stub.save(self.path, {'seq': 3})
        self.assertEqual({'seq': 3}, json.loads(self.path.read_text(encoding='utf-8')))
        self.assertEqual([], self.temp_files())

    def test_the_stub_imports_no_posix_only_module(self):
        source = STUB_PATH.read_text(encoding='utf-8')
        for name in ('fcntl', 'pwd', 'grp', 'termios', 'resource'):
            with self.subTest(name=name):
                self.assertIsNone(re.search(r'(?m)^\s*(?:import|from)\s+%s\b' % name, source))

    @unittest.skipUnless(sys.platform == 'win32', 'a drive letter is a Windows path rule')
    def test_a_windows_drive_letter_is_kept(self):
        real = os.replace
        sources = []

        def capture(source, destination):
            sources.append(Path(source))
            return real(source, destination)

        with mock.patch.object(stub.os, 'replace', capture):
            stub.save(self.path, {'seq': 4})
        self.assertEqual(self.path.drive, sources[0].drive)
        self.assertIn(self.path.drive, str(sources[0]))


if __name__ == '__main__':
    unittest.main()
