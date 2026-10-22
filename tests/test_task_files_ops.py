"""Task files slice 5: operations (kittrial-5bb.380; docs/TASK_FILES_DESIGN.md 2d, 2e, 4a, 5, 6).

``TaskFilesOpsTests`` drives task_files.py's backup, restore, verify and purge on a scratch project
folder; ``PurgedHttpTests`` shows a purged file through the routes; ``ProxyCheckTests`` runs the
proxy upload check against local servers that answer as a proxy and as Orchestra would; and
``AdminTaskFilesTests`` runs ``admin.py task-files`` itself (POSIX only: admin takes fcntl locks).
The adversarial cases of the task, one each: purge never removes a blob another task still needs;
purge of a symlinked .task-files is refused; a restore without the folder; verify detects a flipped
byte and a missing blob; a dry run removes nothing. The real-bd half (admin backup, restore-new into
a new project, purge against real closed rows) is tests/test_task_files_ops_real_bd.py.
"""
import contextlib
import hashlib
import http.server
import io
import json
import os
import shutil
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import task_files
from test_http_agents import unique_dir

DAY = 24 * 3600


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Store:
    """A scratch project folder and the two moves an upload makes: store the bytes, write the entry."""

    def __init__(self, test):
        self.project = unique_dir('task-files-ops-')
        test.addCleanup(shutil.rmtree, self.project, ignore_errors=True)

    def attach(self, task, data, name='f.bin', withdrawn=False, by='coord'):
        digest, size, _ = task_files.receive(io.BytesIO(data).read, len(data), self.project)
        entry = {'schema_version': 1, 'task': task, 'sha256': digest, 'name': name, 'bytes': size,
                 'media_type': task_files.OPAQUE, 'direction': 'to-worker', 'note': None, 'by': by,
                 'at': '2026-10-22T08:00:00Z', 'record_comment': 'c1',
                 'withdrawn': {'by': by, 'at': '2026-10-22T09:00:00Z', 'reason': 'x'} if withdrawn else None}
        task_files._write_entry(self.project, entry)
        return digest

    def blob(self, digest):
        return self.project / task_files.FOLDER / digest[:2] / digest


class TaskFilesOpsTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(self)
        self.backups = unique_dir('task-files-backups-')
        self.addCleanup(shutil.rmtree, self.backups, ignore_errors=True)
        self.target = self.backups / 'p1.task-files'

    # -- backup and restore (4a A) --------------------------------------------------------------
    def test_backup_then_restore_round_trip(self):
        one = self.store.attach('t1', b'input sheet')
        two = self.store.attach('t2', b'results', withdrawn=True)
        summary = task_files.backup_folder(self.store.project, self.target, now=lambda: 'now')
        self.assertEqual((2, len(b'input sheet') + len(b'results'), 2, []),
                         (summary['files'], summary['bytes'], summary['records'], summary['problems']))
        manifest = json.loads((self.target / task_files.MANIFEST).read_text(encoding='utf-8'))
        self.assertEqual(sorted([one, two]), sorted(b['sha256'] for b in manifest['blobs']))
        restored = Store(self)
        outcome = task_files.restore_folder(self.target, restored.project)
        self.assertEqual(('restored', 2), (outcome['status'], outcome['files']))
        self.assertEqual(b'input sheet', restored.blob(one).read_bytes())
        self.assertEqual(['t1'], [e['task'] for e in task_files.live(restored.project, 't1')])
        self.assertIsNotNone(task_files.read_entry(restored.project, 't2', two)['withdrawn'])

    def test_the_backup_is_incremental_and_drops_what_the_project_no_longer_has(self):
        one = self.store.attach('t1', b'kept')
        task_files.backup_folder(self.store.project, self.target)
        stamp = os.stat(str(self.target / one[:2] / one)).st_mtime_ns
        gone = self.store.attach('t2', b'later purged')
        task_files.backup_folder(self.store.project, self.target)
        self.assertEqual(stamp, os.stat(str(self.target / one[:2] / one)).st_mtime_ns, 'not copied again')
        entry = task_files.read_entry(self.store.project, 't2', gone)
        entry['purged'] = {'by': 'op', 'at': 'now', 'reason': 'r'}
        task_files._write_entry(self.store.project, entry)
        os.unlink(str(self.store.blob(gone)))
        summary = task_files.backup_folder(self.store.project, self.target)
        self.assertEqual(1, summary['files'])
        self.assertFalse((self.target / gone[:2] / gone).exists(), 'a purged file leaves the backup too')

    def test_a_stored_file_that_rotted_is_reported_and_not_backed_up(self):
        one = self.store.attach('t1', b'original bytes')
        self.store.blob(one).write_bytes(b'Original bytes')                   # a flipped byte on the project's disk
        summary = task_files.backup_folder(self.store.project, self.target)
        self.assertEqual(0, summary['files'])
        self.assertEqual([one], [p['sha256'] for p in summary['problems']])
        self.assertFalse((self.target / one[:2] / one).exists())

    def test_a_flipped_byte_in_the_backup_refuses_the_restore_and_places_nothing(self):
        one = self.store.attach('t1', b'precious')
        self.store.attach('t2', b'other')
        task_files.backup_folder(self.store.project, self.target)
        copy = self.target / one[:2] / one
        copy.write_bytes(b'Precious')
        restored = Store(self)
        outcome = task_files.restore_folder(self.target, restored.project)
        self.assertEqual('refused', outcome['status'])
        self.assertIn(one, ' '.join(outcome['problems']))
        self.assertFalse((restored.project / task_files.FOLDER).exists(), 'nothing of the set was placed')

    def test_a_changed_record_or_no_manifest_refuses_the_restore(self):
        one = self.store.attach('t1', b'abc')
        task_files.backup_folder(self.store.project, self.target)
        record = self.target / task_files.RECORDS / task_files.task_key('t1') / (one + '.json')
        record.write_text(record.read_text(encoding='utf-8').replace('"coord"', '"mallory"'), encoding='utf-8')
        self.assertEqual('refused', task_files.restore_folder(self.target, Store(self).project)['status'])
        (self.target / task_files.MANIFEST).unlink()
        outcome = task_files.restore_folder(self.target, Store(self).project)
        self.assertEqual('refused', outcome['status'])
        self.assertIn('no complete manifest', outcome['problems'][0])

    def test_a_restore_without_the_folder_is_absent_and_places_nothing(self):
        restored = Store(self)
        outcome = task_files.restore_folder(self.backups / 'missing.task-files', restored.project)
        self.assertEqual('absent', outcome['status'])
        self.assertFalse((restored.project / task_files.FOLDER).exists())
        self.assertEqual({'files': 0, 'bytes': 0, 'records': 0, 'problems': [], 'absent': True},
                         task_files.backup_folder(Store(self).project, self.backups / 'none.task-files'))

    # -- verify (2d) ----------------------------------------------------------------------------
    def test_verify_finds_a_flipped_byte_a_missing_file_orphans_and_old_temp_files(self):
        good = self.store.attach('t1', b'good')
        flipped = self.store.attach('t1', b'flip me', name='g.bin')
        missing = self.store.attach('t2', b'gone')
        self.store.blob(flipped).write_bytes(b'Flip me')
        os.unlink(str(self.store.blob(missing)))
        orphan, _, _ = task_files.receive(io.BytesIO(b'orphan').read, 6, self.store.project)
        stale = self.store.project / task_files.FOLDER / task_files.TMP / 'upload-old'
        stale.write_bytes(b'half')
        os.utime(str(stale), (time.time() - 2 * DAY, time.time() - 2 * DAY))
        result = task_files.verify(self.store.project)
        self.assertEqual([flipped], [i['sha256'] for i in result['mismatched']])
        self.assertEqual([missing], [i['sha256'] for i in result['missing']])
        self.assertEqual([orphan], [i['sha256'] for i in result['orphans']])
        self.assertEqual(['upload-old'], result['stale_temp'])
        self.assertEqual(2, result['checked'])
        self.assertNotIn(good, json.dumps(result['mismatched'] + result['missing']))

    # -- purge (2e, Q1, Q2) ---------------------------------------------------------------------
    def rows(self, closed=('t-old',), at='2026-01-15T10:00:00Z'):
        return {task: {'id': task, 'status': 'closed', 'closed_at': at} for task in closed}

    def test_a_dry_run_removes_nothing(self):
        self.store.attach('t-old', b'old file')
        self.store.attach('t1', b'withdrawn', withdrawn=True)
        before = sorted(str(p) for p in (self.store.project / task_files.FOLDER).rglob('*'))
        plan = task_files.purge_plan(self.store.project, '2026-02-01', self.rows())
        self.assertEqual(2, len(plan['remove']))
        self.assertEqual(before, sorted(str(p) for p in (self.store.project / task_files.FOLDER).rglob('*')))

    def test_purge_never_removes_a_file_another_live_record_needs(self):
        shared = self.store.attach('t-old', b'shared bytes')
        self.assertEqual(shared, self.store.attach('t-open', b'shared bytes', name='same.bin'))
        alone = self.store.attach('t-old', b'only on the closed task', name='alone.bin')
        plan = task_files.purge_plan(self.store.project, '2026-02-01', self.rows())
        self.assertEqual([alone], [i['sha256'] for i in plan['remove']])
        self.assertEqual([shared], [i['sha256'] for i in plan['kept_referenced']])
        line = task_files.purge(self.store.project, plan, 'op', lambda: '2026-10-22T12:00:00Z')
        self.assertTrue(self.store.blob(shared).exists(), 'another task still needs it')
        self.assertFalse(self.store.blob(alone).exists())
        self.assertIsNone(task_files.read_entry(self.store.project, 't-old', shared).get('purged'),
                          'not marked: its bytes stay')
        self.assertEqual('op', task_files.read_entry(self.store.project, 't-old', alone)['purged']['by'])
        self.assertEqual([alone], [i['sha256'] for i in line['removed']])

    def test_purge_removes_withdrawn_closed_orphans_and_old_temp_files_and_writes_one_audit_line(self):
        withdrawn = self.store.attach('t1', b'withdrawn', withdrawn=True)
        closed = self.store.attach('t-old', b'closed task file')
        recent = self.store.attach('t-new', b'closed recently')
        live = self.store.attach('t1', b'live', name='live.bin')
        old_orphan, _, _ = task_files.receive(io.BytesIO(b'old orphan').read, 10, self.store.project)
        young_orphan, _, _ = task_files.receive(io.BytesIO(b'young orphan').read, 12, self.store.project)
        os.utime(str(self.store.blob(old_orphan)), (time.time() - 2 * DAY, time.time() - 2 * DAY))
        stale = self.store.project / task_files.FOLDER / task_files.TMP / 'upload-old'
        stale.write_bytes(b'half')
        os.utime(str(stale), (time.time() - 2 * DAY, time.time() - 2 * DAY))
        rows = dict(self.rows(), **self.rows(('t-new',), at='2026-03-01T00:00:00Z'))
        plan = task_files.purge_plan(self.store.project, '2026-02-01', rows)
        kinds = sorted((i['kind'], i.get('sha256') or i.get('name')) for i in plan['remove'])
        self.assertEqual(sorted([('file', withdrawn), ('file', closed), ('orphan', old_orphan), ('temp', 'upload-old')]), kinds)
        line = task_files.purge(self.store.project, plan, 'op', lambda: '2026-10-22T12:00:00Z', reason='yearly clean-up')
        for gone in (withdrawn, closed, old_orphan):
            self.assertFalse(self.store.blob(gone).exists(), gone)
        for kept in (recent, live, young_orphan):
            self.assertTrue(self.store.blob(kept).exists(), kept)
        self.assertFalse(stale.exists())
        audit = (self.store.project / task_files.FOLDER / task_files.AUDIT).read_text(encoding='utf-8').splitlines()
        self.assertEqual(1, len(audit))
        recorded = json.loads(audit[0])
        self.assertEqual(('op', '2026-02-01', 4), (recorded['actor'], recorded['closed_before'], len(recorded['removed'])))
        self.assertEqual(line, recorded)
        mark = task_files.read_entry(self.store.project, 't-old', closed)['purged']
        self.assertEqual({'by': 'op', 'at': '2026-10-22T12:00:00Z', 'reason': 'yearly clean-up'}, mark)
        self.assertEqual([], task_files.verify(self.store.project)['missing'], 'purged files are not reported missing')
        again = task_files.purge_plan(self.store.project, '2026-02-01', rows)
        self.assertEqual([], [i for i in again['remove'] if i['kind'] == 'file'], 'nothing twice')

    def test_purge_of_a_symlinked_task_files_folder_is_refused(self):
        elsewhere = unique_dir('task-files-elsewhere-')
        self.addCleanup(shutil.rmtree, elsewhere, ignore_errors=True)
        (elsewhere / 'ab').mkdir()
        (elsewhere / 'ab' / ('ab' + 'c' * 62)).write_bytes(b'not ours')
        try:
            os.symlink(str(elsewhere), str(self.store.project / task_files.FOLDER), target_is_directory=True)
        except (OSError, NotImplementedError) as failed:
            self.skipTest('this host cannot make a symlink: %s' % failed)
        with self.assertRaises(task_files.Refusal) as caught:
            task_files.purge_plan(self.store.project, '2026-02-01', {})
        self.assertIn('symlink', str(caught.exception))
        self.assertTrue((elsewhere / 'ab' / ('ab' + 'c' * 62)).exists())

    def test_a_bad_date_is_refused(self):
        with self.assertRaises(task_files.Refusal):
            task_files.purge_plan(self.store.project, '01/02/2026', {})

    def test_a_purged_entry_is_shown_as_purged_counts_for_nothing_and_takes_the_same_file_again(self):
        digest = self.store.attach('t1', b'abc')
        entry = task_files.read_entry(self.store.project, 't1', digest)
        entry['purged'] = {'by': 'op', 'at': '2026-10-22T12:00:00Z', 'reason': 'closed long ago'}
        task_files._write_entry(self.store.project, entry)
        view = task_files.view(task_files.read_entry(self.store.project, 't1', digest), may_download=True)
        self.assertEqual((False, 'op'), (view['may_download'], view['purged']['by']))
        self.assertEqual('This file was purged by an operator on 2026-10-22 (closed long ago); its record stays in the '
                         'task\'s history.', view['said'])
        self.assertEqual([], task_files.held(self.store.project, 't1'))
        self.assertEqual(1, len(task_files.live(self.store.project, 't1')))
        self.assertNotIn(digest, task_files.referenced(self.store.project))


class PurgedHttpTests(unittest.TestCase):
    """The routes say purged, not missing (coordinator's Q2), with the in-process backend."""

    def test_list_and_download_say_purged(self):
        import test_task_files

        class Case(test_task_files.TaskFilesHttpTests):
            def runTest(inner):
                digest = inner.given(b'old input', name='old.xlsx')
                project = inner.backend.files_project(inner.pid)
                entry = task_files.read_entry(project, inner.tid, digest)
                entry['purged'] = {'by': 'op', 'at': '2026-10-22T12:00:00Z', 'reason': None}
                task_files._write_entry(project, entry)
                os.unlink(str(task_files.blob_path(project, digest)))
                listed = inner.listing(inner.tokens['worker']).data['items'][0]
                inner.assertNotIn('missing', listed)
                inner.assertEqual('This file was purged by an operator on 2026-10-22; its record stays in the task\'s '
                                  'history.', listed['said'])
                status, _, body = inner.download(inner.tokens['coord'], digest)
                inner.assertEqual(410, status)
                inner.assertIn(b'purged by an operator on 2026-10-22', body)
        result = unittest.TestResult()
        Case().run(result)
        self.assertEqual([], [str(e) for e in result.errors + result.failures])


class ProxyCheckTests(unittest.TestCase):
    """The proxy upload check against local servers (design 5): passed, a proxy's 413, another answer, a cut."""

    def serve(self, answer):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_PUT(self):
                answer(self)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:%d' % server.server_address[1]

    @staticmethod
    def reply(handler, status, body, content_type='application/json'):
        handler.send_response(status)
        handler.send_header('Content-Type', content_type)
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('Connection', 'close')
        handler.end_headers()
        handler.wfile.write(body)

    def test_orchestras_own_401_after_the_whole_body_means_it_passed(self):
        seen = []

        def orchestra(h):
            length = int(h.headers['Content-Length'])
            got = 0
            while got < length:
                got += len(h.rfile.read(min(65536, length - got)))
            seen.append((h.path, got))
            self.reply(h, 401, json.dumps({'error': {'code': 'unauthenticated', 'message': 'Sign in'},
                                           'request_id': 'req_1'}).encode())
        result = task_files.proxy_check(self.serve(orchestra), 3 * task_files.MIB)
        self.assertTrue(result['passed'], result)
        self.assertEqual([(task_files.PROXY_PATH, 3 * task_files.MIB)], seen)
        self.assertIn('reached Orchestra', result['sentence'])

    def test_a_proxys_413_is_said_with_the_setting_to_raise(self):
        def proxy(h):
            # As nginx does: the 413 at once, then the rest of the body read and dropped (its lingering close).
            self.reply(h, 413, b'<html>413 Request Entity Too Large</html>', 'text/html')
            length = int(h.headers['Content-Length'])
            got = 0
            while got < length:
                piece = h.rfile.read(min(65536, length - got))
                if not piece:
                    break
                got += len(piece)
        result = task_files.proxy_check(self.serve(proxy), 2 * task_files.MIB)
        self.assertFalse(result['passed'])
        self.assertEqual(413, result['status'])
        self.assertIn('client_max_body_size', result['sentence'])
        self.assertIn('task_files.max_file_bytes', result['sentence'])

    def test_another_answer_is_not_taken_for_orchestras(self):
        result = task_files.proxy_check(self.serve(lambda h: self.reply(h, 401, b'<html>Unauthorized</html>', 'text/html')),
                                        64 * 1024)
        self.assertFalse(result['passed'])
        self.assertIn('not Orchestra\'s answer', result['sentence'])

    def test_a_connection_cut_while_sending_is_said(self):
        def cut(h):
            h.rfile.read(1000)
            h.connection.shutdown(socket.SHUT_RDWR)
            h.connection.close()
        result = task_files.proxy_check(self.serve(cut), 8 * task_files.MIB)
        self.assertFalse(result['passed'])
        self.assertIn('client_max_body_size', result['sentence'])


try:
    import fcntl  # noqa: F401
    import admin
except ImportError:
    admin = None


@unittest.skipIf(admin is None, 'admin.py takes fcntl locks: POSIX only')
class AdminTaskFilesTests(unittest.TestCase):
    """``admin.py task-files`` itself: the operator, the dry run, the exit statuses."""

    def setUp(self):
        self.root = unique_dir('task-files-admin-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.project = self.root / 'projects' / 'p1'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        self.store = Store(self)
        self.store.project = self.project
        self.rows = [{'id': 't-old', 'status': 'closed', 'closed_at': '2026-01-15T10:00:00Z'},
                     {'id': 't1', 'status': 'open'}]

    def run_admin(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'task-files'] + list(argv)), \
                mock.patch.object(admin, 'root_path', return_value=self.root), \
                mock.patch.object(admin, 'operators', return_value=['op']), \
                mock.patch.object(admin, 'run_bd', return_value='\n'.join(json.dumps(r) for r in self.rows)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                admin.run_main()
            except SystemExit as stopped:
                code = stopped.code
        return code, out.getvalue(), err.getvalue()

    def test_purge_needs_an_operator_and_dry_runs_first(self):
        old = self.store.attach('t-old', b'old')
        code, out, err = self.run_admin('purge', 'p1', '--closed-before', '2026-02-01', '--actor', 'mallory', '--dry-run')
        self.assertNotEqual(0, code)
        code, out, err = self.run_admin('purge', 'p1', '--closed-before', '2026-02-01', '--actor', 'op', '--dry-run')
        self.assertEqual(0, code, err)
        self.assertIn('would remove file %s' % old, out)
        self.assertIn('nothing was removed', out)
        self.assertTrue(self.store.blob(old).exists())
        self.assertFalse((self.project / task_files.FOLDER / task_files.AUDIT).exists())
        code, out, err = self.run_admin('purge', 'p1', '--closed-before', '2026-02-01', '--actor', 'op')
        self.assertEqual(0, code, err)
        self.assertFalse(self.store.blob(old).exists())
        self.assertEqual('op', json.loads((self.project / task_files.FOLDER / task_files.AUDIT).read_text().splitlines()[0])['actor'])

    def test_verify_exits_1_on_a_flipped_byte(self):
        digest = self.store.attach('t1', b'abc')
        self.assertEqual(0, self.run_admin('verify', 'p1')[0])
        self.store.blob(digest).write_bytes(b'abd')
        code, out, _ = self.run_admin('verify', 'p1')
        self.assertEqual(1, code)
        self.assertIn('task file %s does not match its name' % digest, out)

    def test_proxy_check_needs_a_url_and_takes_no_project(self):
        self.assertNotEqual(0, self.run_admin('proxy-check')[0])
        self.assertNotEqual(0, self.run_admin('proxy-check', 'p1', '--url', 'https://office.example')[0])
        with mock.patch.object(task_files, 'proxy_check', return_value={'passed': True, 'sentence': 'ok'}) as check:
            code, out, _ = self.run_admin('proxy-check', '--url', 'https://office.example', '--bytes', '1000')
        self.assertEqual(0, code)
        self.assertEqual(('https://office.example', 1000), check.call_args[0][:2])


class ProxyCheckAgainstOrchestraTests(unittest.TestCase):
    """Straight at a real Orchestra service (no proxy): it answers the unsigned upload at once, and the check reads it."""

    def test_the_real_service_is_reached(self):
        import test_http_agents as agents

        class Case(agents.AgentHarness):
            def runTest(inner):
                result = task_files.proxy_check('http://127.0.0.1:%d' % inner.port, 4 * task_files.MIB)
                inner.assertTrue(result['passed'], result)
                inner.assertEqual(401, result['status'])
        outcome = unittest.TestResult()
        Case().run(outcome)
        self.assertEqual([], [str(e) for e in outcome.errors + outcome.failures])


class LingeringCloseTests(unittest.TestCase):
    """kittrial-5bb.380: an upload answered before its body was read is closed lingering, within bounds."""

    def run_against_service(self, body):
        import test_http_agents as agents
        outcome = unittest.TestResult()

        class Case(agents.AgentHarness):
            def runTest(inner):
                body(inner)
        Case().run(outcome)
        self.assertEqual([], [str(e) for e in outcome.errors + outcome.failures])

    def test_a_refused_upload_lingers_and_its_answer_arrives(self):
        import http_service

        def body(inner):
            before = dict(http_service.LINGERING)
            result = task_files.proxy_check('http://127.0.0.1:%d' % inner.port, 4 * task_files.MIB)
            inner.assertTrue(result['passed'], result)
            for _ in range(100):
                if http_service.LINGERING['now'] == 0:
                    break
                time.sleep(0.05)
            inner.assertEqual(before['lingered'] + 1, http_service.LINGERING['lingered'])
            inner.assertEqual(before['hard'], http_service.LINGERING['hard'])
            inner.assertEqual(0, http_service.LINGERING['now'])
        self.run_against_service(body)

    def test_beyond_the_cap_the_connection_is_closed_at_once(self):
        import http_service

        def body(inner):
            before = dict(http_service.LINGERING)
            with mock.patch.object(http_service.ApiHandler, 'LINGER_MAX', 0):
                task_files.proxy_check('http://127.0.0.1:%d' % inner.port, 4 * task_files.MIB)
            for _ in range(100):
                if http_service.LINGERING['hard'] > before['hard']:
                    break
                time.sleep(0.05)
            inner.assertEqual(before['hard'] + 1, http_service.LINGERING['hard'], 'closed at once')
            inner.assertEqual(before['lingered'], http_service.LINGERING['lingered'])
            inner.assertEqual(0, http_service.LINGERING['now'])
        self.run_against_service(body)

    def test_a_client_that_keeps_the_connection_open_is_let_go_at_the_time_bound(self):
        import http_service

        def body(inner):
            with mock.patch.object(http_service.ApiHandler, 'LINGER_SECONDS', 0.5):
                sock = socket.create_connection(('127.0.0.1', inner.port), timeout=10)
                try:
                    sock.sendall(b'PUT /v1/projects/p/tasks/t/files?name=a&direction=to-worker HTTP/1.1\r\nHost: x\r\n'
                                 b'Content-Length: 1000000\r\n\r\n')     # no credential, and no body: refused at once
                    started = time.monotonic()
                    data = b''
                    while True:
                        piece = sock.recv(65536)
                        if not piece:
                            break
                        data += piece
                    inner.assertTrue(data.startswith(b'HTTP/1.1 401'), data[:80])
                    inner.assertLess(time.monotonic() - started, 5, 'closed after LINGER_SECONDS, not held open')
                finally:
                    sock.close()
        self.run_against_service(body)
