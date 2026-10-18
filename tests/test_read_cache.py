"""The generation-keyed export cache (kittrial-5bb.339).

The rules these tests pin:
- a read with an unchanged tracker generation is served from the cached parsed rows
  (the export run is not repeated);
- ANY write commits, so the generation moves, the cache misses and the write is
  visible to the next read - the correctness the task demands, by construction;
- the cache files are mode 0600 (the export can carry credentials in comments) and
  written atomically (a reader never sees a half-written rows file);
- everything unexpected - no Dolt server coordinates, an unreadable key, a corrupt
  rows file - fails OPEN to today's behaviour: a fresh export, nothing cached.
"""
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import read_cache


def row(task='t-1', **more):
    return dict({'id': task, 'issue_type': 'task', 'title': 'a task', 'status': 'open',
                 'assignee': None, 'labels': [], 'comments': []}, **more)


class FakeRun:
    """A run() that answers exports from a list the test can change."""

    def __init__(self, rows):
        self.rows = rows
        self.exports = 0

    def __call__(self, argv):
        assert argv == ['export', '--all'], argv
        self.exports += 1
        return ''.join(json.dumps(item) + '\n' for item in self.rows)


class ReadCacheTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.project = self.root / 'projects' / 'pp'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        self.cache = self.project / read_cache.CACHE_DIR

    def serve(self, rows, key='a' * 32):
        with mock.patch.object(read_cache, 'project_generation', return_value=key):
            return read_cache.cached_export_rows(self.root, self.project, FakeRun(rows))

    def test_an_unchanged_generation_serves_the_cached_rows_without_an_export(self):
        run = FakeRun([row()])
        with mock.patch.object(read_cache, 'project_generation', return_value='a' * 32):
            first = read_cache.cached_export_rows(self.root, self.project, run)
            self.assertEqual(run.exports, 1)
            second = read_cache.cached_export_rows(self.root, self.project, FakeRun([row()]))
        self.assertEqual(first, second)
        self.assertTrue((self.cache / read_cache.ROWS_NAME).is_file())

    def test_a_write_moves_the_generation_and_is_visible(self):
        self.serve([row()])
        run = FakeRun([row(), row('t-2')])
        visible = self.serve([row(), row('t-2')], key='b' * 32)  # generation moved
        # The rows the NEXT read returns are the fresh export's, not the cache's.
        self.assertEqual([item['id'] for item in visible], ['t-1', 't-2'])
        self.assertEqual(run.exports, 0)  # the FakeRun here was never consulted
        stored = (self.cache / read_cache.KEY_NAME).read_text(encoding='utf-8').strip()
        self.assertEqual(stored, 'b' * 32)

    def test_the_cache_files_are_mode_0600(self):
        self.serve([row()])
        for name in (read_cache.ROWS_NAME, read_cache.KEY_NAME):
            mode = stat.S_IMODE((self.cache / name).stat().st_mode)
            self.assertEqual(oct(mode), '0o600', name)

    def test_no_generation_answers_a_fresh_export_and_caches_nothing(self):
        with mock.patch.object(read_cache, 'project_generation', return_value=None):
            run = FakeRun([row()])
            rows = read_cache.cached_export_rows(self.root, self.project, run)
        self.assertEqual([item['id'] for item in rows], ['t-1'])
        self.assertEqual(run.exports, 1)
        self.assertFalse(self.cache.exists())

    def test_a_corrupt_rows_file_fails_open_to_a_fresh_export(self):
        self.serve([row()])
        (self.cache / read_cache.ROWS_NAME).write_text('{not json', encoding='utf-8')
        run = FakeRun([row()])
        with mock.patch.object(read_cache, 'project_generation', return_value='a' * 32):
            rows = read_cache.cached_export_rows(self.root, self.project, run)
        self.assertEqual(run.exports, 1)
        self.assertEqual([item['id'] for item in rows], ['t-1'])

    def test_a_wrong_key_fails_open_and_refreshes_the_cache(self):
        self.serve([row()], key='a' * 32)
        run = FakeRun([row(comment=[{'id': '1', 'text': 'new'}])])
        with mock.patch.object(read_cache, 'project_generation', return_value='c' * 32):
            rows = read_cache.cached_export_rows(self.root, self.project, run)
        self.assertEqual(run.exports, 1)
        self.assertEqual(rows[0]['comment'], [{'id': '1', 'text': 'new'}])
        self.assertEqual((self.cache / read_cache.KEY_NAME).read_text(encoding='utf-8').strip(),
                         'c' * 32)

    def test_each_call_returns_a_freshly_parsed_list(self):
        self.serve([row()])
        with mock.patch.object(read_cache, 'project_generation', return_value='a' * 32):
            one = read_cache.cached_export_rows(self.root, self.project, FakeRun([row()]))
            two = read_cache.cached_export_rows(self.root, self.project, FakeRun([row()]))
        self.assertIsNot(one, two)
        one.append(row('mutated'))
        self.assertEqual(len(two), 1)

    def test_the_generation_reads_the_newest_dolt_commit(self):
        import admin
        payload = ('127.0.0.1', '49001', 'root', 'pp')
        answer = 'commit_hash\nkf0fm32s3onunon8r2u8bd1v106rk87i\n'
        with mock.patch.object(admin, 'project_server_metadata', return_value=payload), \
                mock.patch.object(admin, 'sql', return_value=answer) as sql:
            key = read_cache.project_generation(self.root, 'pp')
        self.assertEqual(key, 'kf0fm32s3onunon8r2u8bd1v106rk87i')
        self.assertIn('dolt_log', sql.call_args[0][1])

    def test_a_generation_that_cannot_be_read_is_none(self):
        import admin
        with mock.patch.object(admin, 'project_server_metadata', side_effect=ValueError):
            self.assertIsNone(read_cache.project_generation(self.root, 'pp'))
        with mock.patch.object(admin, 'project_server_metadata', return_value=None):
            self.assertIsNone(read_cache.project_generation(self.root, 'pp'))
        with mock.patch.object(admin, 'project_server_metadata',
                               return_value=('h', 1, 'root', 'not a db!')):
            self.assertIsNone(read_cache.project_generation(self.root, 'pp'))
        with mock.patch.object(admin, 'sql', side_effect=OSError):
            metadata = ('127.0.0.1', 49001, 'root', 'pp')
            with mock.patch.object(admin, 'project_server_metadata', return_value=metadata):
                self.assertIsNone(read_cache.project_generation(self.root, 'pp'))


if __name__ == '__main__':
    unittest.main()
