"""Endpoint-level reserved-label guard against bd's hidden label aliases.

kittrial-pth.26 rev3 review item hidden-label-alias: bd 1.2.2 accepts
`create --label X` as an UNDOCUMENTED alias of `--labels` (it is hidden from
`bd create --help`), so a guard table built from --help let a plain create mint
`requirement,requirement:accepted`, `brd-section` and `request:<64hex>`.

These tests drive endpoint.execute against a REAL pinned bd, not a stub, so the
flag surface is the binary's own. A real bd is used when one is available:
set `ORCHESTRA_BD_BIN` to the bd binary (a sibling `dolt` is copied too), or
have `bd` on PATH. Without one the class skips and the stub-backed
endpoint/reserved_comments tests still cover the same spellings. The suite is
POSIX-only because endpoint.py imports fcntl.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import admin
    import endpoint
except ImportError:  # pragma: no cover - endpoint needs fcntl (POSIX)
    admin = endpoint = None


def _bd_binary():
    configured = os.environ.get('ORCHESTRA_BD_BIN')
    if configured and Path(configured).is_file():
        return Path(configured)
    found = shutil.which('bd')
    return Path(found) if found else None


BD = _bd_binary()

RESERVED_VALUES = ('requirement', 'requirement:draft', 'requirement:accepted',
                   'brd-section', 'request:' + 'a' * 64,
                   'request-content:' + 'b' * 64)


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class RealBdLabelAliasTests(unittest.TestCase):
    """Every accepted create/update label spelling, driven through endpoint."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='bd-alias-')
        cls.root = Path(cls._tmp.name)
        (cls.root / 'bin').mkdir()
        cls.bd_path = cls.root / 'bin' / 'bd'
        shutil.copy(str(BD), str(cls.bd_path))
        sibling = BD.with_name('dolt')
        if sibling.is_file():
            shutil.copy(str(sibling), str(cls.root / 'bin' / 'dolt'))
        (cls.root / 'deployment.private.json').write_text(
            '{"password": "x", "unit": "none", "port": "1"}', encoding='utf-8')
        cls.project = cls.root / 'projects' / 'pp'
        cls.project.mkdir(parents=True)
        cls._home = mock.patch.dict(os.environ, {'HOME': str(cls.root)})
        cls._home.start()
        env = admin.environment(cls.root)
        env['HOME'] = str(cls.root)
        init = subprocess.run(
            [str(cls.bd_path), 'init', '--prefix', 'pp', '--skip-agents',
             '--skip-hooks', '--non-interactive'],
            cwd=cls.project, env=env, capture_output=True, text=True)
        if init.returncode:
            cls._home.stop()
            cls._tmp.cleanup()
            raise unittest.SkipTest('bd init failed: %s'
                                    % (init.stderr or init.stdout).strip()[:200])

    @classmethod
    def tearDownClass(cls):
        cls._home.stop()
        cls._tmp.cleanup()

    def bd(self, *args, actor='op'):
        env = admin.environment(self.root)
        env['HOME'] = str(self.root)
        return subprocess.run(
            [str(self.bd_path), '--directory', str(self.project), '--sandbox',
             '--actor', actor, *args],
            env=env, capture_output=True, text=True)

    def export_rows(self):
        result = self.bd('export', '--all')
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()
                if line.strip()]

    def reserved_labels(self):
        found = []
        for row in self.export_rows():
            for label in row.get('labels') or []:
                if label in ('requirement', 'brd-section') \
                        or label.startswith(('requirement:', 'request:',
                                             'request-content:')):
                    found.append((row['id'], label))
        return found

    def raw(self, args, actor='mallory'):
        return endpoint.execute(self.root, {'project': 'pp', 'actor': actor,
                                            'action': 'bd', 'args': args})

    CREATE_SPELLINGS = (['--label'], ['--labels'], ['-l'])

    def test_create_label_spellings_refuse_every_reserved_value(self):
        for flag in self.CREATE_SPELLINGS:
            for value in RESERVED_VALUES:
                args = ['create', 'mint %s %s' % (flag[0], value[:8]), flag[0],
                        value, '--json']
                with self.subTest(args=str(args)):
                    with self.assertRaisesRegex(
                            ValueError, 'Reserved coordination/requirement labels'):
                        self.raw(args)
        for flag in ('--label=', '--labels=', '-l='):
            value = 'requirement,requirement:accepted'
            args = ['create', 'mint eq %s' % flag[:6], flag + value, '--json']
            with self.subTest(args=str(args)):
                with self.assertRaisesRegex(
                        ValueError, 'Reserved coordination/requirement labels'):
                    self.raw(args)
        self.assertEqual(self.reserved_labels(), [])

    def test_update_label_spellings_refuse_every_reserved_value(self):
        target = json.loads(self.bd('create', 'victim', '--json').stdout)['id']
        for flag in ('--add-label', '--set-labels', '--remove-label'):
            for value in RESERVED_VALUES:
                args = ['update', target, flag, value, '--json']
                with self.subTest(args=str(args)):
                    with self.assertRaisesRegex(
                            ValueError, 'Reserved coordination/requirement labels'):
                        self.raw(args)
        self.assertEqual(self.reserved_labels(), [])

    def test_ordinary_labels_still_reach_real_bd(self):
        created = json.loads(self.bd('create', 'ordinary alias',
                                     '--label', 'frontend,bug', '--json').stdout)['id']
        row = next(r for r in self.export_rows() if r['id'] == created)
        self.assertEqual(sorted(row['labels']), ['bug', 'frontend'])
        short = json.loads(self.bd('create', 'ordinary short', '-l', 'docs',
                                   '--json').stdout)['id']
        row = next(r for r in self.export_rows() if r['id'] == short)
        self.assertEqual(sorted(row['labels']), ['docs'])
        result = endpoint.execute(self.root, {
            'project': 'pp', 'actor': 'mallory', 'action': 'bd',
            'args': ['update', created, '--add-label', 'reviewed', '--json']})
        self.assertEqual(result['returncode'], 0, result['stderr'])
        row = next(r for r in self.export_rows() if r['id'] == created)
        self.assertIn('reviewed', row['labels'])
        self.assertEqual(self.reserved_labels(), [])


if __name__ == '__main__':
    unittest.main()
