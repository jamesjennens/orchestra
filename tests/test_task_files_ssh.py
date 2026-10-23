"""Task files over the SSH transport (kittrial-5bb.384): ``client.py files list|get|put`` against the real
endpoint.py (the local transport runs the same endpoint as SSH does) and a real bd.

The framed request is one JSON header line, then exactly ``bytes`` raw bytes; a get answers one JSON line
and then the raw bytes. Each adversarial case here is one that a client or a network could produce.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import task_files
import test_bd_label_aliases as fixture

MIB = 1024 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipIf(fixture.endpoint is None, 'task files over SSH need POSIX endpoint imports')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class TaskFilesOverSshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = type('SshFilesRuntime', (fixture.RealBdLabelAliasTests,), {})
        cls.runtime.setUpClass()
        cls.r = cls.runtime(methodName='runTest')
        for key, value in fixture.admin.PROJECT_SETTINGS:
            made = cls.r.bd('config', 'set', key, value)
            assert made.returncode == 0, made.stderr
        cls.root = cls.r.root
        cls.project = cls.r.project
        document = json.loads((cls.root / 'deployment.private.json').read_text(encoding='utf-8'))
        document['operators'] = ['ops']
        (cls.root / 'deployment.private.json').write_text(json.dumps(document), encoding='utf-8')
        cls.config = cls.root / 'ssh-client.json'
        cls.config.write_text(json.dumps(dict(transport='local', python=sys.executable,
                                              endpoint=str(KIT / 'endpoint.py'), root=str(cls.root))),
                              encoding='utf-8')
        cls.casey = cls.register('casey')
        cls.drew = cls.register('drew')

    @classmethod
    def tearDownClass(cls):
        cls.runtime.tearDownClass()

    @classmethod
    def register(cls, name):
        answer = fixture.endpoint.execute(cls.root, dict(project='pp', action='session',
                                                         args=['register', '--name', name, '--request-id', str(uuid.uuid4())]))
        assert answer['returncode'] == 0, answer
        return json.loads(answer['stdout'])['session']['actor']

    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix='files-out-', dir=str(self.root)))
        self.task = self.new_task(self.casey)

    def new_task(self, assignee):
        made = self.r.bd('create', 'task for files', '--assignee', assignee, '--json')
        self.assertEqual(made.returncode, 0, made.stderr)
        return json.loads(made.stdout)['id']

    # -- helpers -----------------------------------------------------------------------------------
    def cli(self, actor, *args, project='pp', timeout=150):
        return subprocess.run([sys.executable, str(KIT / 'client.py'), '--config', str(self.config), '--project', project,
                               '--actor', actor, '--', 'files', *args], capture_output=True, text=True, timeout=timeout)

    def raw(self, header, body=b'', actor=None):
        """The endpoint itself, spoken to by hand: ``(answer dict, the bytes after its line)``."""
        header = dict(header)
        header.setdefault('project', 'pp')
        header.setdefault('actor', actor or 'ops')
        wire = json.dumps(header).encode() + b'\n' + body
        done = subprocess.run([sys.executable, str(KIT / 'endpoint.py'), '--root', str(self.root)], input=wire,
                              capture_output=True, timeout=150)
        line, _, rest = done.stdout.partition(b'\n')
        return json.loads(line), rest

    def put_file(self, actor, data, name='input.bin', direction='to-worker', task=None):
        path = self.out / ('src-' + sha(data)[:8])
        path.write_bytes(data)
        named = self.out / name
        if named != path:
            path.rename(named)
        return self.cli(actor, 'put', task or self.task, str(named), '--direction', direction)

    def stored(self, task=None, data=b''):
        return task_files.read_entry(self.project, task or self.task, sha(data))

    # -- the round trip ----------------------------------------------------------------------------
    def test_a_coordinator_hands_a_file_to_the_assignee_who_gets_it_and_hands_one_back(self):
        data = b'spreadsheet ' * 30000
        put = self.put_file('ops', data, name='inputs.xlsx')
        self.assertEqual(0, put.returncode, put.stderr)
        record = json.loads(put.stdout)
        self.assertEqual((201, sha(data), len(data)), (record['status'], record['sha256'], record['bytes']))
        again = self.put_file('ops', data, name='inputs.xlsx')
        self.assertEqual((0, 200, True), (again.returncode, json.loads(again.stdout)['status'], json.loads(again.stdout)['replayed']))
        listed = json.loads(self.cli(self.casey, 'list', self.task).stdout)
        self.assertEqual(([sha(data)], True), ([i['sha256'] for i in listed['items']], listed['may_download']))
        dest = self.out / 'dest'
        dest.mkdir()
        got = self.cli(self.casey, 'get', self.task, sha(data), '--out', str(dest))
        self.assertEqual(0, got.returncode, got.stderr)
        self.assertEqual(data, (dest / 'inputs.xlsx').read_bytes())
        self.assertEqual(0o600, (dest / 'inputs.xlsx').stat().st_mode & 0o777)
        back = b'result bytes'
        self.assertEqual(0, self.put_file(self.casey, back, name='out.txt', direction='from-worker').returncode)
        self.assertEqual(0, self.cli('ops', 'get', self.task, sha(back), '--out', str(dest)).returncode)
        self.assertEqual(back, (dest / 'out.txt').read_bytes())
        comments = self.r.bd('comments', self.task, '--json').stdout
        self.assertIn(sha(data), comments)

    # -- the framed stream -------------------------------------------------------------------------
    def test_a_body_shorter_than_announced_stores_nothing(self):
        data = b'x' * 10
        answer, _ = self.raw(dict(action='file-put', task=self.task, name='a.bin', direction='to-worker', bytes=100,
                                  sha256=sha(data)), data)
        self.assertEqual(2, answer['returncode'], answer)
        self.assertIsNone(self.stored(data=data))
        self.assertEqual(0, self.cli('ops', 'list', self.task).returncode)
        self.assertEqual([], json.loads(self.cli('ops', 'list', self.task).stdout)['items'])

    def test_a_body_longer_than_announced_is_refused_and_stores_nothing(self):
        announced = b'abcde'
        answer, _ = self.raw(dict(action='file-put', task=self.task, name='a.bin', direction='to-worker', bytes=5,
                                  sha256=sha(announced)), announced + b'MORE' * 5000)
        self.assertEqual((2, 400), (answer['returncode'], answer['task_file_status']), answer)
        self.assertIsNone(self.stored(data=announced))
        self.assertIsNone(task_files.blob_size(self.project, sha(announced)))

    def test_a_task_that_is_not_an_identifier_is_refused_before_anything_runs(self):
        for task in ('--directory=/tmp', '--help', '-x', 'a b', '../x', ''):
            answer, _ = self.raw(dict(action='file-list', task=task))
            self.assertEqual((2, 422), (answer['returncode'], answer['task_file_status']), task)

    def test_a_reader_that_stops_mid_get_frees_its_slot_by_the_deadline(self):
        path = self.root / 'deployment.private.json'
        original = path.read_text(encoding='utf-8')
        document = json.loads(original)
        document['task_files'] = {'transfer_grace_seconds': 2, 'min_transfer_bytes_per_second': 1048576}
        path.write_text(json.dumps(document), encoding='utf-8')
        try:
            data = os.urandom(6 * MIB)
            self.assertEqual(0, self.put_file('ops', data, name='big.bin').returncode)
            wire = json.dumps(dict(action='file-get', project='pp', actor='ops', task=self.task,
                                   sha256=sha(data))).encode() + b'\n'
            proc = subprocess.Popen([sys.executable, str(KIT / 'endpoint.py'), '--root', str(self.root)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            try:
                proc.stdin.write(wire)
                proc.stdin.close()
                proc.stdout.readline()
                proc.stdout.read(4096)
                try:
                    proc.wait(30)
                except subprocess.TimeoutExpired:
                    self.fail('a stalled reader kept the endpoint (and its slot) past the deadline')
            finally:
                proc.kill()
                proc.wait()
                proc.stdout.close()
            held = self.hold_slots(task_files.LIMIT_DEFAULTS['max_transfers'])
            for handle in held:
                handle.close()
        finally:
            path.write_text(original, encoding='utf-8')

    def test_old_upload_temporaries_are_swept_and_fresh_ones_kept(self):
        folder = self.project / '.task-files' / 'tmp'
        folder.mkdir(parents=True, exist_ok=True)
        old, fresh = folder / 'upload-old', folder / 'upload-fresh'
        old.write_bytes(b'x')
        fresh.write_bytes(b'x')
        os.utime(str(old), (1, 1))
        task_files.sweep_uploads(self.project)
        self.assertEqual((False, True), (old.exists(), fresh.exists()))

    def test_a_reserved_windows_name_is_not_written_by_get(self):
        data = b'reserved'
        self.assertEqual(0, self.put_file('ops', data, name='NUL.txt').returncode)
        dest = self.out / 'dest'
        dest.mkdir()
        done = self.cli(self.casey, 'get', self.task, sha(data), '--out', str(dest))
        self.assertNotEqual(0, done.returncode)
        self.assertEqual([], list(dest.iterdir()))

    def test_a_header_over_its_size_limit_is_refused_before_a_body_is_read(self):
        wire = b'{"action": "file-put", "note": "' + b'x' * (task_files.HEADER_LIMIT + 10)
        done = subprocess.run([sys.executable, str(KIT / 'endpoint.py'), '--root', str(self.root)], input=wire,
                              capture_output=True, timeout=150)
        answer = json.loads(done.stdout.partition(b'\n')[0])
        self.assertEqual((2, 413), (answer['returncode'], answer['task_file_status']))
        self.assertEqual([], json.loads(self.cli('ops', 'list', self.task).stdout)['items'])

    def test_a_body_over_the_per_file_cap_is_refused_unread(self):
        size = task_files.LIMIT_DEFAULTS['max_file_bytes'] + 1
        answer, _ = self.raw(dict(action='file-put', task=self.task, name='big.bin', direction='to-worker', bytes=size,
                                  sha256='0' * 64))
        self.assertEqual((2, 413), (answer['returncode'], answer['task_file_status']))

    def test_a_digest_that_does_not_match_the_bytes_stores_nothing(self):
        data = b'real bytes'
        answer, _ = self.raw(dict(action='file-put', task=self.task, name='a.bin', direction='to-worker',
                                  bytes=len(data), sha256=sha(b'other bytes')), data)
        self.assertEqual(2, answer['returncode'], answer)
        self.assertIsNone(self.stored(data=data))
        self.assertIsNone(self.stored(data=b'other bytes'))
        self.assertEqual([], json.loads(self.cli('ops', 'list', self.task).stdout)['items'])

    def test_a_header_with_the_wrong_fields_or_a_name_with_separators_is_refused(self):
        data = b'abc'
        for extra in (dict(name='../escape.txt'), dict(name='a/b.txt'), dict(name='a\\b.txt'), dict(name='')):
            header = dict(action='file-put', task=self.task, name='a.txt', direction='to-worker', bytes=3, sha256=sha(data))
            header.update(extra)
            answer, _ = self.raw(header, data)
            self.assertEqual((2, 422), (answer['returncode'], answer['task_file_status']), extra)
        answer, _ = self.raw(dict(action='file-list', task=self.task, surprise=1))
        self.assertEqual((2, 422), (answer['returncode'], answer['task_file_status']))
        self.assertIsNone(self.stored(data=data))

    def test_a_path_with_directories_is_sent_under_its_clean_name(self):
        data = b'nested'
        nested = self.out / 'deep' / 'er'
        nested.mkdir(parents=True)
        (nested / 'n.txt').write_bytes(data)
        put = self.cli('ops', 'put', self.task, str(nested / 'n.txt'), '--direction', 'to-worker')
        self.assertEqual(0, put.returncode, put.stderr)
        self.assertEqual('n.txt', json.loads(put.stdout)['name'])

    # -- who may ---------------------------------------------------------------------------------
    def test_somebody_who_is_not_the_assignee_or_a_coordinator_is_refused(self):
        data = b'secret'
        self.assertEqual(0, self.put_file('ops', data).returncode)
        listed = self.cli(self.drew, 'list', self.task)
        self.assertEqual((0, False), (listed.returncode, json.loads(listed.stdout)['may_download']))
        got = self.cli(self.drew, 'get', self.task, sha(data), '--out', str(self.out))
        self.assertNotEqual(0, got.returncode)
        self.assertIn('may download', got.stderr)
        refused = self.put_file(self.drew, b'unwanted', name='u.bin', direction='from-worker')
        self.assertNotEqual(0, refused.returncode)
        self.assertIsNone(self.stored(data=b'unwanted'))
        by_assignee_to_worker = self.put_file(self.casey, b'sneaky', name='s.bin', direction='to-worker')
        self.assertNotEqual(0, by_assignee_to_worker.returncode)
        self.assertIsNone(self.stored(data=b'sneaky'))

    def test_the_former_assignee_loses_access_the_moment_the_task_is_reassigned(self):
        data = b'for the assignee'
        self.assertEqual(0, self.put_file('ops', data).returncode)
        first = self.out / 'first'
        first.mkdir()
        self.assertEqual(0, self.cli(self.casey, 'get', self.task, sha(data), '--out', str(first)).returncode)
        moved = self.r.bd('update', self.task, '--assignee', self.drew)
        self.assertEqual(0, moved.returncode, moved.stderr)
        dest = self.out / 'again'
        dest.mkdir()
        self.assertNotEqual(0, self.cli(self.casey, 'get', self.task, sha(data), '--out', str(dest)).returncode)
        self.assertEqual([], list(dest.iterdir()))
        self.assertEqual(0, self.cli(self.drew, 'get', self.task, sha(data), '--out', str(dest)).returncode)

    def test_another_project_is_refused_and_nothing_is_stored(self):
        data = b'elsewhere'
        path = self.out / 'e.bin'
        path.write_bytes(data)
        done = self.cli('ops', 'put', self.task, str(path), '--direction', 'to-worker', project='zz')
        self.assertNotEqual(0, done.returncode)
        self.assertIsNone(self.stored(data=data))

    # -- where a get writes ------------------------------------------------------------------------
    def test_out_must_be_a_real_folder_and_an_existing_file_is_kept_unless_forced(self):
        data = b'kept or replaced'
        self.assertEqual(0, self.put_file('ops', data, name='keep.txt').returncode)
        link = self.out / 'link'
        os.symlink(str(self.out), str(link))
        refused = self.cli(self.casey, 'get', self.task, sha(data), '--out', str(link))
        self.assertNotEqual(0, refused.returncode)
        missing = self.cli(self.casey, 'get', self.task, sha(data), '--out', str(self.out / 'nope'))
        self.assertNotEqual(0, missing.returncode)
        dest = self.out / 'dest'
        dest.mkdir()
        (dest / 'keep.txt').write_bytes(b'mine')
        again = self.cli(self.casey, 'get', self.task, sha(data), '--out', str(dest))
        self.assertNotEqual(0, again.returncode)
        self.assertEqual(b'mine', (dest / 'keep.txt').read_bytes())
        forced = self.cli(self.casey, 'get', self.task, sha(data), '--out', str(dest), '--force')
        self.assertEqual(0, forced.returncode, forced.stderr)
        self.assertEqual(data, (dest / 'keep.txt').read_bytes())
        self.assertEqual(['keep.txt'], sorted(p.name for p in dest.iterdir()))

    def test_a_symlink_at_the_target_is_never_replaced_even_with_force(self):
        data = b'through a link'
        self.assertEqual(0, self.put_file('ops', data, name='t.txt').returncode)
        dest = self.out / 'dest'
        dest.mkdir()
        victim = self.out / 'victim'
        victim.write_bytes(b'untouched')
        os.symlink(str(victim), str(dest / 't.txt'))
        for extra in ([], ['--force']):
            done = self.cli(self.casey, 'get', self.task, sha(data), '--out', str(dest), *extra)
            self.assertNotEqual(0, done.returncode)
        self.assertEqual(b'untouched', victim.read_bytes())
        self.assertEqual(['t.txt'], sorted(p.name for p in dest.iterdir()))

    def test_a_get_whose_bytes_do_not_match_what_was_asked_for_keeps_nothing(self):
        data = b'honest bytes'
        self.assertEqual(0, self.put_file('ops', data, name='h.txt').returncode)
        dest = self.out / 'dest'
        dest.mkdir()
        wrong = self.cli(self.casey, 'get', self.task, 'f' * 64, '--out', str(dest))
        self.assertNotEqual(0, wrong.returncode)
        self.assertEqual([], list(dest.iterdir()))

    # -- concurrency and the transfer slots --------------------------------------------------------
    def test_a_put_and_a_withdraw_at_the_same_time_leave_a_consistent_journal(self):
        first = b'first file ' * 1000
        self.assertEqual(0, self.put_file('ops', first, name='one.txt').returncode)

        def run(argv):
            done = self.r.bd(*argv, actor='ops')
            self.assertEqual(0, done.returncode, done.stderr)
            return done.stdout
        result = {}

        def withdraw():
            with fixture.endpoint.held_coordination_lock(self.project):
                result['withdraw'] = task_files.apply(
                    {'schema_version': 1, 'operation': 'coordinator-withdraw', 'task': self.task, 'sha256': sha(first),
                     'reason': 'replaced'}, 'ops', run, self.project, lambda: '2026-10-22T05:00:00Z')
        thread = threading.Thread(target=withdraw)
        thread.start()
        second = b'second file ' * 1000
        put = self.put_file('ops', second, name='two.txt')
        thread.join(120)
        self.assertEqual(0, put.returncode, put.stderr)
        self.assertEqual(200, result['withdraw'][0])
        listed = json.loads(self.cli('ops', 'list', self.task).stdout)
        self.assertEqual([sha(second)], [i['sha256'] for i in listed['items']])
        self.assertEqual(2, len(task_files.entries(self.project, self.task)), 'a withdrawn file is marked, not erased')

    def hold_slots(self, count):
        folder = self.root / '.task-file-slots'
        folder.mkdir(mode=0o700, exist_ok=True)
        import fcntl  # POSIX only: imported here so the module still imports on Windows (its tests skip there)
        handles = []
        for number in range(count):
            handle = open(str(folder / ('slot-%d' % number)), 'a+b')
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handles.append(handle)
        return handles

    def test_the_transfer_after_the_last_free_slot_is_refused_and_a_freed_slot_serves_again(self):
        limit = task_files.LIMIT_DEFAULTS['max_transfers']
        data = b'slotted'
        held = self.hold_slots(limit)
        try:
            answer, _ = self.raw(dict(action='file-put', task=self.task, name='s.bin', direction='to-worker',
                                      bytes=len(data), sha256=sha(data)), data)
            self.assertEqual((2, 503), (answer['returncode'], answer['task_file_status']))
            self.assertIn('already moving', answer['stderr'])
            self.assertIsNone(self.stored(data=data))
        finally:
            for handle in held:
                handle.close()
        answer, _ = self.raw(dict(action='file-put', task=self.task, name='s.bin', direction='to-worker',
                                  bytes=len(data), sha256=sha(data)), data)
        self.assertEqual(0, answer['returncode'], answer)

    def test_a_slot_is_freed_when_the_process_that_held_it_dies(self):
        limit = task_files.LIMIT_DEFAULTS['max_transfers']
        self.hold_slots(0)
        holder = subprocess.Popen([sys.executable, '-c', (
            'import fcntl,sys,time\n'
            'folder=sys.argv[1]\n'
            'hs=[]\n'
            'for n in range(%d):\n'
            '    h=open(folder+"/slot-%%d"%%n,"a+b");fcntl.flock(h,fcntl.LOCK_EX|fcntl.LOCK_NB);hs.append(h)\n'
            'print("held",flush=True);time.sleep(600)\n') % limit, str(self.root / '.task-file-slots')],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual('held', holder.stdout.readline().strip())
            data = b'while held'
            answer, _ = self.raw(dict(action='file-put', task=self.task, name='h.bin', direction='to-worker',
                                      bytes=len(data), sha256=sha(data)), data)
            self.assertEqual(503, answer.get('task_file_status'))
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        answer, _ = self.raw(dict(action='file-put', task=self.task, name='h.bin', direction='to-worker',
                                  bytes=len(data), sha256=sha(data)), data)
        self.assertEqual(0, answer['returncode'], answer)

    def test_a_framed_action_with_the_ordinary_envelope_fields_is_refused_not_run(self):
        answer, _ = self.raw(dict(action='file-list', task=self.task, args=[]))
        self.assertEqual((2, 422), (answer['returncode'], answer['task_file_status']))


@unittest.skipIf(fixture.endpoint is None, 'task files over SSH need POSIX endpoint imports')
class FramedHeaderDeadlineTests(unittest.TestCase):
    def test_a_head_that_can_still_become_a_framed_header_is_recognised(self):
        could = fixture.endpoint.could_be_framed
        for head in (b'', b'  ', b'{', b'{"ac', b'{ "action" :', b'{"action": "fi', b'{"action": "file-', b'{"action": "file-g',
                     b'{"action": "file-get"', b'{"action": "file-put", "task": "x"'):
            self.assertTrue(could(head), head)
        for head in (b'[', b'{"project"', b'{"action": "run"', b'{"action": "file-x', b'hello'):
            self.assertFalse(could(head), head)

    def test_a_header_that_stalls_before_its_action_is_known_is_cut_off(self):
        from unittest import mock
        for sent in (b'', b'{"act'):
            read, write = os.pipe()
            try:
                os.write(write, sent)
                # The first reading starts the 60 s deadline; every later one is past it. A callable,
                # not a list: other threads of the suite read time.monotonic while it is patched, and
                # a list they use up ends in StopIteration (coordinator, 1023e suite).
                readings = iter([0])
                with mock.patch('time.monotonic', side_effect=lambda: next(readings, 100)):
                    request, frame, refusal = fixture.endpoint.read_request(read)
            finally:
                os.close(read)
                os.close(write)
            self.assertEqual((None, None, 408), (request, frame, refusal.status), sent)


if __name__ == '__main__':
    unittest.main()
