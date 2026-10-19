"""The generation-keyed export cache on a real tracker (kittrial-5bb.339).

A real bd in server mode: the work read populates the cache, the second read is
served from it, a native write moves the generation and the next read sees the
write (the correctness the task demands), and the cache files are mode 0600.
"""
import json
import os
import stat
import sys
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import test_bd_label_aliases as fixture


@unittest.skipIf(fixture.endpoint is None, 'POSIX endpoint imports unavailable')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class ReadCacheRealBdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = type('ReadCacheRuntime', (fixture.RealBdLabelAliasTests,), {})
        cls.runtime.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.runtime.tearDownClass()

    def setUp(self):
        self.fixture = self.runtime(methodName='runTest')
        self.root = self.fixture.root
        self.project = self.fixture.project

    def work_read(self):
        """One endpoint work read, with the export parses counted.

        ``record_json.loads_rows`` is the one parser the cache's fresh path feeds the
        export text to (the classify step uses ``loads_array_rows`` on bd list output,
        a different function), so counting its STRING inputs counts exports.
        """
        import endpoint
        import record_json
        from unittest import mock
        real = record_json.loads_rows
        counts = {'exports': 0}

        def counting(text, **options):
            if isinstance(text, str):
                counts['exports'] += 1
            return real(text, **options)

        with mock.patch.object(record_json, 'loads_rows', side_effect=counting):
            answer = endpoint.execute(self.root, {'project': 'pp', 'actor': 'op',
                                                  'action': 'work',
                                                  'args': ['--limit', '20', '--json']})
        return answer, counts

    def test_the_second_read_is_served_and_a_write_is_visible(self):
        import read_cache
        made = self.fixture.bd('create', 'cache probe task', '--json')
        task = json.loads(made.stdout)['id']
        cache = self.project / read_cache.CACHE_DIR

        # First read: exports and populates the cache.
        answer, exports = self.work_read()
        self.assertEqual(answer['returncode'], 0, answer)
        self.assertEqual(exports['exports'], 1)
        stored_file = cache / read_cache.FILE_NAME
        self.assertTrue(stored_file.is_file())
        if os.name != 'nt':                               # review B3: POSIX file modes
            self.assertEqual(stat.S_IMODE(stored_file.stat().st_mode), 0o600)
        container = json.loads(stored_file.read_text(encoding='utf-8'))
        self.assertEqual(container['format'], read_cache.FORMAT)
        key_after_first = container['key']

        # Second read with no write between: served from the cache, no export.
        answer, exports = self.work_read()
        self.assertEqual(exports['exports'], 0)
        page = json.loads(answer['stdout'])
        self.assertIn(task, [item['task'] for item in page['items']])
        self.assertEqual(json.loads(stored_file.read_text(encoding='utf-8'))['key'],
                         key_after_first)

        # A native write that the WORK ANSWER itself shows: the task's title changes,
        # so a stale cache would answer with the old one (review S4: assert on the
        # answer, not on a side export).
        written = self.fixture.bd('update', task, '--title=Write probe kittrial-5bb.339',
                                  '--json')
        self.assertEqual(written.returncode, 0, written.stderr)
        answer, exports = self.work_read()
        self.assertEqual(exports['exports'], 1)            # the cache missed
        page = json.loads(answer['stdout'])
        item = next(entry for entry in page['items'] if entry['task'] == task)
        self.assertEqual(item['title'], 'Write probe kittrial-5bb.339')
        key_after_write = json.loads(stored_file.read_text(encoding='utf-8'))['key']
        self.assertNotEqual(key_after_write, key_after_first)
        # And the read after THAT is cached again, still answering the new title.
        answer, exports = self.work_read()
        self.assertEqual(exports['exports'], 0)
        page = json.loads(answer['stdout'])
        item = next(entry for entry in page['items'] if entry['task'] == task)
        self.assertEqual(item['title'], 'Write probe kittrial-5bb.339')


if __name__ == '__main__':
    unittest.main()
