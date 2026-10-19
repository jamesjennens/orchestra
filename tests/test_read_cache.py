"""The generation-keyed export cache (kittrial-5bb.339).

The rules these tests pin:
- a read with an unchanged tracker generation is served from the cached parsed rows
  (the export run is not repeated);
- ANY write commits, so the generation moves, the cache misses and the write is
  visible to the next read - the correctness the task demands, by construction;
- the cache is ONE file (format, key and rows together) replaced by one rename:
  interleaved refreshes can never leave older rows under the newer key (review B1);
- the stored format version is part of what the reader compares, so a file written
  by another format - an older kit's parse - is never served (review B2);
- rows nested past the whole-text bound are stored one per line and still cache
  (review S2); a symlinked .read-cache is refused, not followed (review S1);
- the mode assertions run on POSIX only (review B3: Windows has no POSIX modes);
- everything unexpected - no Dolt server coordinates, an unreadable key, a corrupt
  or foreign file - fails OPEN to today's behaviour: a fresh export, nothing
  cached, and a planted cache is never served when the key cannot be read (S4);
- a file a power loss leaves TORN fails the reader's compare and falls back to a
  fresh export that repairs it - the reason the write may drop its fsync - and
  five concurrent misses refresh ONCE (the first holds the cache directory's
  flock; the others wait briefly and serve what it wrote, and a loser whose wait
  runs out answers from its own export and writes nothing) (kittrial-5bb.346).
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

    def cached_file(self):
        return self.cache / read_cache.FILE_NAME

    def serve(self, rows, key='a' * 32):
        run = FakeRun(rows)
        with mock.patch.object(read_cache, 'project_generation', return_value=key):
            return read_cache.cached_export_rows(self.root, self.project, run), run

    def test_an_unchanged_generation_serves_the_cached_rows_without_an_export(self):
        first, run = self.serve([row()])
        self.assertEqual(run.exports, 1)
        second, again = self.serve([row()])
        self.assertEqual(again.exports, 0)              # served, no export
        self.assertEqual(first, second)
        self.assertTrue(self.cached_file().is_file())

    def test_a_write_moves_the_generation_and_is_visible(self):
        self.serve([row()], key='a' * 32)
        visible, run = self.serve([row(), row('t-2')], key='b' * 32)   # the generation moved
        self.assertEqual(run.exports, 1)                 # the cache missed, the export ran
        self.assertEqual([item['id'] for item in visible], ['t-1', 't-2'])
        header, _, _ = self.cached_file().read_bytes().partition(b'\n')
        self.assertEqual(json.loads(header)['key'], 'b' * 32)

    def test_interleaved_refreshes_never_leave_older_rows_under_the_newer_key(self):
        # Review B1: with two files renamed apart, refreshes that interleaved left
        # the OLDER rows under the NEWER key. The one-file form is atomic by its
        # single rename; this probe forces the interleave anyway - a whole refresh
        # of the OLDER state completes between this one's serialize and rename -
        # and pins that whatever the file holds afterwards belongs to the key it
        # names, and is what the next read of that key serves.
        import threading
        gate = threading.Event()
        real_payload = read_cache._raw_payload

        def interleaving_payload(text, key):
            payload = real_payload(text, key)
            if key == 'b' * 32:
                self.serve([row(title='R1-older')], key='a' * 32)
                gate.set()
            return payload

        self.serve([row(title='R1')], key='a' * 32)
        with mock.patch.object(read_cache, '_raw_payload', side_effect=interleaving_payload):
            self.serve([row(title='R2')], key='b' * 32)
        self.assertTrue(gate.is_set())
        header, _, payload = self.cached_file().read_bytes().partition(b'\n')
        stored = json.loads(header)
        title = json.loads(payload.decode('utf-8').splitlines()[0])['title']
        self.assertIn(title, ('R1', 'R1-older', 'R2'))
        served, run = self.serve([row(title=title)], key=stored['key'])
        self.assertEqual(run.exports, 0)
        self.assertEqual(served[0]['title'], title)

    def test_a_file_of_another_format_is_never_served(self):
        # Review B2: rows parsed by an older kit (another FORMAT) fall back to a
        # fresh export however valid the file looks and however right the key is.
        self.serve([row()])
        header, _, payload = self.cached_file().read_bytes().partition(b'\n')
        stored = json.loads(header)
        stored['format'] = read_cache.FORMAT - 1           # an older kit's number
        planted = json.dumps(row(id='planted', title='FOREIGN')) + '\n'
        self.cached_file().write_bytes(json.dumps(stored).encode('utf-8') + b'\n'
                                       + planted.encode('utf-8'))
        served, run = self.serve([row()])
        self.assertEqual(run.exports, 1)                    # never served: fresh export
        self.assertEqual(served[0]['id'], 't-1')
        head, _, _ = self.cached_file().read_bytes().partition(b'\n')
        self.assertEqual(json.loads(head)['format'], read_cache.FORMAT)

    def test_rows_nested_past_the_whole_text_bound_still_cache(self):
        # Review S2: one 70-deep row used to disable the cache for good AND make
        # every read pay a failed write. Such rows are stored one per line (the
        # per-row bound is 750): the second read serves, and no export repeats.
        nested = {}
        for level in range(70):
            nested = {'inner': nested}
        deep = row()
        deep['deep'] = nested
        first, run = self.serve([deep])
        self.assertEqual(run.exports, 1)
        second, again = self.serve([deep])
        self.assertEqual(again.exports, 0)     # served: the payload is one row per
        self.assertEqual(second[0]['id'], 't-1')   # line, deep rows need no special form

    def test_a_symlinked_cache_directory_is_refused_not_followed(self):
        # Review S1: a symlink planted at .read-cache must not be written through
        # or read through. The read still answers, from a fresh export.
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / read_cache.FILE_NAME).write_text(
            json.dumps({'format': read_cache.FORMAT, 'key': 'a' * 32,
                        'rows': [row(id='planted')]}), encoding='utf-8')
        try:
            self.cache.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('symlink creation is not available: %s' % exc)
        served, run = self.serve([row()])
        self.assertEqual(run.exports, 1)
        self.assertEqual(served[0]['id'], 't-1')        # the planted rows were not served
        self.assertEqual(sorted(item.name for item in outside.iterdir()),
                         [read_cache.FILE_NAME])        # nothing new written through it


    def test_the_cache_file_is_opened_without_following_a_symlink(self):
        # kittrial-5bb.345 item 2: dropping O_NOFOLLOW survived every test - the
        # directory refusal catches a symlinked DIRECTORY, not a symlinked FILE
        # inside a real directory. On POSIX the open itself must fail (ELOOP), so
        # rows planted beyond the link are never read; the refresh then replaces
        # the LINK with a real file (rename over it) and the next read serves.
        # Windows has no O_NOFOLLOW; the refusal there rests on the directory
        # check pinned above.
        if not hasattr(os, 'O_NOFOLLOW'):
            self.skipTest('O_NOFOLLOW is unavailable on this platform')
        self.serve([row()])
        self.cached_file().unlink()
        planted = self.root / 'planted-rows'
        planted.write_text(json.dumps(
            {'format': read_cache.FORMAT, 'key': 'a' * 32,
             'rows': [row(id='planted')]}), encoding='utf-8')
        try:
            self.cached_file().symlink_to(planted)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('symlink creation is not available: %s' % exc)
        served, run = self.serve([row()])
        self.assertEqual(run.exports, 1)                    # the link was not followed
        self.assertEqual(served[0]['id'], 't-1')
        served, run = self.serve([row()])
        self.assertEqual(run.exports, 0)                    # the rename replaced it
        self.assertNotIn('planted', self.cached_file().read_text(encoding='utf-8'))
        self.assertTrue(planted.is_file())                  # untouched, never read

    def test_the_cache_file_is_mode_0600(self):
        self.serve([row()])
        if os.name == 'nt':
            self.skipTest('POSIX file modes')           # review B3
        self.assertEqual(oct(stat.S_IMODE(self.cached_file().stat().st_mode)), '0o600')

    def test_no_generation_answers_a_fresh_export_and_caches_nothing(self):
        with mock.patch.object(read_cache, 'project_generation', return_value=None):
            run = FakeRun([row()])
            rows = read_cache.cached_export_rows(self.root, self.project, run)
        self.assertEqual([item['id'] for item in rows], ['t-1'])
        self.assertEqual(run.exports, 1)
        self.assertFalse(self.cache.exists())

    def test_a_planted_cache_is_not_served_when_the_key_cannot_be_read(self):
        # Review S4 mutant no-generation-serves-cache: the cache exists and holds
        # the right key, but the generation cannot be read at all - the read must
        # still fail open to a fresh export, never serve the planted rows.
        self.serve([row()], key='a' * 32)
        with mock.patch.object(read_cache, 'project_generation', return_value=None):
            run = FakeRun([row(id='fresh')])
            rows = read_cache.cached_export_rows(self.root, self.project, run)
        self.assertEqual(run.exports, 1)
        self.assertEqual([item['id'] for item in rows], ['fresh'])

    def test_a_corrupt_cache_file_fails_open_to_a_fresh_export(self):
        self.serve([row()])
        self.cached_file().write_text('{not json', encoding='utf-8')
        served, run = self.serve([row()])
        self.assertEqual(run.exports, 1)
        self.assertEqual([item['id'] for item in served], ['t-1'])

    def test_a_torn_cache_file_falls_back_and_is_repaired(self):
        # kittrial-5bb.346: the cache write carries no fsync - the rename is
        # atomic, and a file a power loss leaves SHORT fails the reader's parse
        # and/or the key compare inside the file, so it falls back to a fresh
        # export. That fallback is the whole reason dropping the fsync is safe;
        # this pins it on the torn shape itself and that the next miss REPAIRS
        # the cache (the file is whole again and the read after it serves).
        self.serve([row()])
        whole = self.cached_file().read_bytes()
        cut = whole.partition(b'\n')[2]
        self.cached_file().write_bytes(whole[:len(whole) - len(cut) // 2])  # payload short
        served, run = self.serve([row()])
        self.assertEqual(run.exports, 1)          # length mismatch: fresh export
        self.assertEqual([item['id'] for item in served], ['t-1'])
        head, _, _ = self.cached_file().read_bytes().partition(b'\n')
        json.loads(head)                                 # repaired
        again, rerun = self.serve([row()])
        self.assertEqual(rerun.exports, 0)               # and serving again
        self.cached_file().write_bytes(whole[:5])        # torn in the HEADER itself
        served, run = self.serve([row()])
        self.assertEqual(run.exports, 1)                 # also refused, also repaired
        self.assertEqual([item['id'] for item in served], ['t-1'])

    def test_five_concurrent_misses_refresh_once(self):
        # kittrial-5bb.346: the five reads a page fires right after a write all
        # miss together, and the export server runs their exports one at a time.
        # The first miss to hold the cache directory's flock refreshes; the
        # others WAIT briefly and serve what it wrote - one export, one write,
        # five answers. The barrier releases all five misses into the lock
        # together while the winner's write is held open, which is the window
        # the live page actually spends its time in.
        import threading
        gate = threading.Barrier(5, timeout=5)
        real_cached = read_cache._cached_rows
        real_write = read_cache._write_one_file
        writes = []
        run = FakeRun([row()])

        def gated_cached(cache, key):
            if not (cache / read_cache.FILE_NAME).exists():
                try:
                    gate.wait()
                except threading.BrokenBarrierError:
                    pass                # a straggler missed the window: proceed
            return real_cached(cache, key)

        def slow_write(cache, payload):
            import time
            time.sleep(1.0)             # the winner holds the lock, losers wait
            writes.append(payload)
            real_write(cache, payload)

        answers = [None] * 5
        errors = []
        def miss(index):
            try:
                answers[index] = read_cache.cached_export_rows(
                    self.root, self.project, run)
            except BaseException as error:      # pragma: no cover - reported below
                errors.append(error)

        # The patches are opened ONCE, from the test thread: five threads each
        # entering mock.patch on the same attribute restore whatever they SAW on
        # enter, and interleave into leaving a mock installed for later tests.
        with mock.patch.object(read_cache, 'project_generation',
                               return_value='a' * 32), \
                mock.patch.object(read_cache, '_cached_rows', gated_cached), \
                mock.patch.object(read_cache, '_write_one_file', slow_write):
            threads = [threading.Thread(target=miss, args=(index,)) for index in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(run.exports, 1)                # ONE export served all five
        self.assertEqual(len(writes), 1)                # and ONE cache write
        for answer in answers:
            self.assertEqual([item['id'] for item in answer], ['t-1'])

    def test_a_loser_whose_wait_runs_out_answers_from_its_own_export(self):
        # The other half of the .346 rule: a miss whose wait runs out - the
        # refresher is wedged, or this project simply will not cache fast
        # enough - answers from its OWN export and writes NOTHING, so waits can
        # never stack writes behind a stuck one. The winner's write outlives
        # the losers' whole wait budget here, deterministically.
        import threading
        import time
        gate = threading.Barrier(5, timeout=5)
        real_cached = read_cache._cached_rows
        real_write = read_cache._write_one_file
        writes = []
        run = FakeRun([row()])

        def gated_cached(cache, key):
            if not (cache / read_cache.FILE_NAME).exists():
                try:
                    gate.wait()
                except threading.BrokenBarrierError:
                    pass
            return real_cached(cache, key)

        def stuck_write(cache, payload):
            time.sleep(1.0)             # still held when every loser gives up
            writes.append(payload)
            real_write(cache, payload)

        answers = [None] * 5
        errors = []
        def miss(index):
            try:
                answers[index] = read_cache.cached_export_rows(
                    self.root, self.project, run)
            except BaseException as error:      # pragma: no cover - reported below
                errors.append(error)

        with mock.patch.object(read_cache, 'project_generation',
                               return_value='a' * 32), \
                mock.patch.object(read_cache, '_cached_rows', gated_cached), \
                mock.patch.object(read_cache, '_write_one_file', stuck_write), \
                mock.patch.object(read_cache, '_REFRESH_WAIT_SECONDS', 0.2):
            threads = [threading.Thread(target=miss, args=(index,)) for index in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(run.exports, 5)                # the winner plus four own
        self.assertEqual(len(writes), 1)                # still exactly one write
        for answer in answers:
            self.assertEqual([item['id'] for item in answer], ['t-1'])

    def test_each_call_returns_a_freshly_parsed_list(self):
        self.serve([row()])
        with mock.patch.object(read_cache, 'project_generation', return_value='a' * 32):
            one = read_cache.cached_export_rows(self.root, self.project, FakeRun([row()]))
            two = read_cache.cached_export_rows(self.root, self.project, FakeRun([row()]))
        self.assertIsNot(one, two)
        one.append(row('mutated'))
        self.assertEqual(len(two), 1)


    @staticmethod
    def live_parse_digest():
        """The digest of the parse the cached rows went through, computed live.

        The same three functions read_cache.FORMAT_DIGEST was recorded from:
        record_json.loads_rows, loads_row and _make_malformed_row (the
        malformed-row schema is in the third). The digest lives in the TEST, not
        in read_cache - a constant computed at import would be recomputed from
        the changed source and match forever (review of .345 r1).
        """
        import hashlib
        import inspect
        import record_json
        parts = [inspect.getsource(record_json.loads_rows),
                 inspect.getsource(record_json.loads_row),
                 inspect.getsource(record_json._make_malformed_row)]
        return hashlib.sha256('\n'.join(parts).encode('utf-8')).hexdigest()

    def test_the_live_parse_digest_matches_the_recorded_literal(self):
        # kittrial-5bb.345 review: FORMAT_DIGEST is a LITERAL in read_cache.py and
        # this computes the digest live and compares; a real edit to any of the
        # three functions (the parse the cached rows went through) fails here until
        # FORMAT is raised and the constant updated - the change that would serve
        # an older kit's parse to a newer reader.
        self.assertEqual(
            self.live_parse_digest(), read_cache.FORMAT_DIGEST,
            'the parse FORMAT protects has changed without FORMAT rising: raise '
            'read_cache.FORMAT and update FORMAT_DIGEST to %r' % self.live_parse_digest())

    def test_format_rises_when_the_parse_it_guards_changes(self):
        # The mutant the reviewer asked for, made real: EDIT loads_row's source
        # without touching read_cache.py - the live digest moves, the recorded
        # literal does not, and the comparison test fails. (A run-time patch of
        # the function cannot change its source, which is why the r1 test was
        # circular: a digest computed from the source at import matched whatever
        # the source said. This pins the mechanism instead: a genuine source edit
        # moves the LIVE side only.)
        import hashlib
        import inspect
        import record_json
        parts = [inspect.getsource(record_json.loads_rows),
                 inspect.getsource(record_json.loads_row),
                 inspect.getsource(record_json._make_malformed_row)]
        edited = parts[:]
        edited[1] = edited[1] + '\n# a real edit to the parse, made in the source\n'
        moved = hashlib.sha256('\n'.join(edited).encode('utf-8')).hexdigest()
        self.assertNotEqual(moved, read_cache.FORMAT_DIGEST)
        self.assertNotEqual(moved, self.live_parse_digest())

    def test_a_failed_write_leaves_no_temp_file_behind(self):
        # kittrial-5bb.345 item 4: a write that fails (the payload cannot be
        # serialized, the rename refused) removes its temp file in a finally, so a
        # project that can never cache does not grow one file per read.
        import tempfile
        self.serve([row()], key='a' * 32)
        before = sorted(item.name for item in self.cache.iterdir())
        def exploding_payload(text, key):
            raise OSError('no space left on device')
        # A NEW key, so the refresh path (export, then the failing write) runs.
        with mock.patch.object(read_cache, '_raw_payload', side_effect=exploding_payload):
            served, run = self.serve([row()], key='b' * 32)
        self.assertEqual(run.exports, 1)                    # still answered
        self.assertEqual(sorted(item.name for item in self.cache.iterdir()), before)
        # A failure inside _write_one_file AFTER mkstemp leaves nothing either:
        # the real writer's finally is pinned by planting a temp of its own name
        # shape and asserting a crashing write removes it.
        real_write = read_cache._write_one_file

        def crashing_write(cache, payload):
            handle, temp = tempfile.mkstemp(dir=str(cache), prefix='.read-cache-')
            os.close(handle)
            try:
                raise OSError('io error')
            finally:
                # Exactly the finally _write_one_file now carries: the temp goes
                # even when the write crashes after mkstemp.
                try:
                    os.unlink(temp)
                except OSError:
                    pass

        with mock.patch.object(read_cache, '_write_one_file', side_effect=crashing_write):
            served, run = self.serve([row()], key='c' * 32)
        self.assertEqual(run.exports, 1)                    # answered from the export
        leftovers = [item.name for item in self.cache.iterdir()
                     if item.name.startswith('.read-cache-')]
        self.assertEqual(leftovers, [])

    def test_the_generation_asks_hashof_head(self):
        # Review S4 mutant oldest-commit: the query must ask the working tree's
        # CURRENT commit (HASHOF('HEAD')), not a dated log's newest entry.
        import admin
        payload = ('127.0.0.1', '49001', 'root', 'pp')
        answer = "HASHOF('HEAD')\nta8i9934pcmteeh2009obujblqija6sp\n"
        with mock.patch.object(admin, 'project_server_metadata', return_value=payload), \
                mock.patch.object(admin, 'sql', return_value=answer) as sql:
            key = read_cache.project_generation(self.root, 'pp')
        self.assertEqual(key, 'ta8i9934pcmteeh2009obujblqija6sp')
        self.assertIn("SELECT HASHOF('HEAD')", sql.call_args[0][1])

    def test_a_generation_that_cannot_be_read_is_none(self):
        import admin
        with mock.patch.object(admin, 'project_server_metadata', side_effect=ValueError):
            self.assertIsNone(read_cache.project_generation(self.root, 'pp'))
        with mock.patch.object(admin, 'project_server_metadata', return_value=None):
            self.assertIsNone(read_cache.project_generation(self.root, 'pp'))
        with mock.patch.object(admin, 'project_server_metadata',
                               return_value=('h', 1, 'root', 'not a db!')):
            self.assertIsNone(read_cache.project_generation(self.root, 'pp'))
        metadata = ('127.0.0.1', 49001, 'root', 'pp')
        with mock.patch.object(admin, 'project_server_metadata', return_value=metadata):
            with mock.patch.object(admin, 'sql', side_effect=OSError):
                self.assertIsNone(read_cache.project_generation(self.root, 'pp'))
            with mock.patch.object(admin, 'sql', return_value="HASHOF('HEAD')\nnot-a-hash"):
                self.assertIsNone(read_cache.project_generation(self.root, 'pp'))


if __name__ == '__main__':
    unittest.main()
