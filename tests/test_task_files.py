"""Files attached to a task, slice 1 (kittrial-5bb.370; docs/TASK_FILES_DESIGN.md).

Two halves. ``TaskFilesStoreTests`` drives task_files.py directly: names, limits, the streamed
receive with real streams, the folders that must not be links, and the journal that alone makes
a file attached. ``TaskFilesHttpTests`` drives the four routes over a real socket against the
in-process backend (which runs the same ``task_files.apply`` as the endpoint action), with the
adversarial cases of the task one each: another project's member; a viewer; the former
assignee after reassignment; a closed task's last assignee; a forged or other-task sha256 (and a
planted record comment); a lying Content-Length, short and long; a body over each cap; a name
with / .. or control characters; a symlinked .task-files; concurrent uploads at the transfer
cap; a withdrawn file fetched; and ``?actor=`` naming somebody else.

The canonical endpoint path (the ``task-file`` action under the project lock and live
authority, on real bd) is tests/test_task_files_real_bd.py.
"""
import hashlib
import http.client
import io
import json
import os
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import reserved_comments
import task_files
import test_http_agents as agents
from test_http_agents import unique_dir


def sha(data):
    return hashlib.sha256(data).hexdigest()


SMALL = dict(task_files.LIMIT_DEFAULTS, max_file_bytes=4096, max_files_per_task=3, max_task_bytes=6000,
             max_project_bytes=9000, min_free_bytes=0, max_transfers=4, max_transfers_per_actor=2)
SMALL_RATE = dict(task_files.LIMIT_DEFAULTS, min_transfer_bytes_per_second=1000, transfer_grace_seconds=1)


def http_auth_forbidden(message):
    import http_auth
    return http_auth.forbidden(message)


class TaskFilesStoreTests(unittest.TestCase):
    def setUp(self):
        self.project = unique_dir('task-files-store-')
        self.addCleanup(__import__('shutil').rmtree, self.project, ignore_errors=True)

    def test_names(self):
        for good in ('report.xlsx', 'a', 'x' * 128, 'Ünïcode name (2).pdf'):
            self.assertEqual(good, task_files.clean_name(good))
        for bad in ('', 'a/b', 'a\\b', '.', '..', 'x' * 129, 'tab\there', 'nl\n', '\x7f', ' lead', 'trail ', None, 3):
            with self.assertRaises(task_files.Refusal, msg=repr(bad)) as caught:
                task_files.clean_name(bad)
            self.assertEqual(422, caught.exception.status)

    def test_sha256_and_direction_and_media_type(self):
        task_files.check_sha256('a' * 64)
        for bad in ('A' * 64, 'a' * 63, '../' + 'a' * 61, None):
            with self.assertRaises(task_files.Refusal):
                task_files.check_sha256(bad)
        with self.assertRaises(task_files.Refusal):
            task_files.check_direction('sideways')
        self.assertEqual(task_files.OPAQUE, task_files.check_media_type(None))
        self.assertEqual('text/csv', task_files.check_media_type('Text/CSV'))
        with self.assertRaises(task_files.Refusal):
            task_files.check_media_type('text/html; charset=utf-8')

    def test_limits_read_defaults_and_refuse_typos_without_lifting_them(self):
        root = unique_dir('task-files-root-')
        self.addCleanup(__import__('shutil').rmtree, root, ignore_errors=True)
        self.assertEqual(task_files.LIMIT_DEFAULTS, task_files.limits(root))
        (root / 'deployment.private.json').write_text(json.dumps({'task_files': {
            'max_file_bytes': 1000, 'max_files_per_task': '20', 'max_task_bytes': -1, 'min_free_bytes': 0,
            'max_transfers': True}}), encoding='utf-8')
        warnings = []
        values = task_files.limits(root, warnings=warnings)
        self.assertEqual(1000, values['max_file_bytes'])
        self.assertEqual(0, values['min_free_bytes'])
        self.assertEqual(task_files.LIMIT_DEFAULTS['max_files_per_task'], values['max_files_per_task'])
        self.assertEqual(task_files.LIMIT_DEFAULTS['max_task_bytes'], values['max_task_bytes'])
        self.assertEqual(task_files.LIMIT_DEFAULTS['max_transfers'], values['max_transfers'])
        self.assertEqual(3, len(warnings), warnings)
        self.assertTrue(all('task_files.' in line for line in warnings), warnings)
        (root / 'deployment.private.json').write_text(json.dumps({'task_files': [1]}), encoding='utf-8')
        warnings = []
        self.assertEqual(task_files.LIMIT_DEFAULTS, task_files.limits(root, warnings=warnings))
        self.assertEqual(1, len(warnings))

    def test_receive_streams_counts_and_hashes_in_steps(self):
        data = os.urandom(200 * 1024 + 7)
        reads = []
        stream = io.BytesIO(data)

        def read(count):
            reads.append(count)
            return stream.read(count)
        digest, size, _ = task_files.receive(read, len(data), self.project)
        self.assertEqual((sha(data), len(data)), (digest, size))
        self.assertTrue(all(count <= task_files.STEP for count in reads), reads)
        stored = self.project / task_files.FOLDER / digest[:2] / digest
        self.assertEqual(data, stored.read_bytes())
        self.assertEqual([], os.listdir(self.project / task_files.FOLDER / task_files.TMP))

    def test_receive_short_body_and_digest_mismatch_store_nothing(self):
        data = b'x' * 5000
        with self.assertRaises(task_files.Refusal) as caught:
            task_files.receive(io.BytesIO(data[:3000]).read, len(data), self.project)
        self.assertEqual(400, caught.exception.status)
        self.assertIn('3000 of the 5000', str(caught.exception))
        with self.assertRaises(task_files.Refusal) as caught:
            task_files.receive(io.BytesIO(data).read, len(data), self.project, expected=sha(b'other'))
        self.assertEqual(422, caught.exception.status)
        base = self.project / task_files.FOLDER
        self.assertEqual([task_files.TMP], sorted(os.listdir(base)), 'no blob folder was made')
        self.assertEqual([], os.listdir(base / task_files.TMP))

    def test_a_reader_that_returns_more_than_asked_is_refused(self):
        with self.assertRaises(task_files.Refusal) as caught:
            task_files.receive(lambda count: b'y' * (count + 1), 10, self.project)
        self.assertEqual(400, caught.exception.status)

    def test_a_stored_blob_is_kept_once(self):
        data = b'same bytes'
        task_files.receive(io.BytesIO(data).read, len(data), self.project)
        task_files.receive(io.BytesIO(data).read, len(data), self.project)
        folder = self.project / task_files.FOLDER / sha(data)[:2]
        self.assertEqual([sha(data)], os.listdir(folder))

    def _link_or_skip(self, target, link):
        try:
            os.symlink(str(target), str(link), target_is_directory=True)
        except (OSError, NotImplementedError) as failed:
            self.skipTest('this host cannot make a symlink: %s' % failed)

    def test_a_symlinked_task_files_folder_is_refused_and_nothing_is_written_through_it(self):
        elsewhere = unique_dir('task-files-elsewhere-')
        self.addCleanup(__import__('shutil').rmtree, elsewhere, ignore_errors=True)
        self._link_or_skip(elsewhere, self.project / task_files.FOLDER)
        for call in (lambda: task_files.receive(io.BytesIO(b'abc').read, 3, self.project),
                     lambda: task_files.live(self.project, 't1'),
                     lambda: task_files.open_blob(self.project, 'a' * 64)):
            with self.assertRaises(task_files.Refusal) as caught:
                call()
            self.assertEqual(503, caught.exception.status)
            self.assertIn('symlink', str(caught.exception))
        self.assertEqual([], os.listdir(elsewhere))

    def test_a_symlinked_folder_inside_is_refused_too(self):
        task_files.folder(self.project, create=True)
        elsewhere = unique_dir('task-files-inner-')
        self.addCleanup(__import__('shutil').rmtree, elsewhere, ignore_errors=True)
        self._link_or_skip(elsewhere, self.project / task_files.FOLDER / task_files.TMP)
        with self.assertRaises(task_files.Refusal):
            task_files.receive(io.BytesIO(b'abc').read, 3, self.project)
        self.assertEqual([], os.listdir(elsewhere))

    def test_check_room_names_each_limit_and_setting(self):
        cases = [(5000, 'max_file_bytes', 413), ]
        for length, key, status in cases:
            with self.assertRaises(task_files.Refusal) as caught:
                task_files.check_room(self.project, 't1', length, SMALL, present=[])
            self.assertEqual(status, caught.exception.status)
            self.assertIn('task_files.' + key, str(caught.exception))
        three = [{'bytes': 1}] * 3
        with self.assertRaises(task_files.Refusal) as caught:
            task_files.check_room(self.project, 't1', 1, SMALL, present=three)
        self.assertEqual((409, True), (caught.exception.status, 'task_files.max_files_per_task' in str(caught.exception)))
        with self.assertRaises(task_files.Refusal) as caught:
            task_files.check_room(self.project, 't1', 3000, SMALL, present=[{'bytes': 3500}])
        self.assertIn('task_files.max_task_bytes', str(caught.exception))
        with mock.patch.object(task_files, 'project_bytes', return_value=8000):
            with self.assertRaises(task_files.Refusal) as caught:
                task_files.check_room(self.project, 't1', 2000, SMALL, present=[])
        self.assertIn('task_files.max_project_bytes', str(caught.exception))
        with mock.patch.object(task_files, 'free_bytes', return_value=10 * task_files.MIB):
            with self.assertRaises(task_files.Refusal) as caught:
                task_files.check_room(self.project, 't1', 1000, dict(SMALL, min_free_bytes=10 * task_files.MIB),
                                      present=[])
        self.assertEqual(507, caught.exception.status)
        self.assertIn('task_files.min_free_bytes', str(caught.exception))
        self.assertIsNone(task_files.check_room(self.project, 't1', 1000, SMALL, present=[]))

    def _run(self, task, rows):
        comments = []

        def run(argv):
            if argv[:1] == ['show']:
                return json.dumps([dict(rows[task], id=task)])
            if argv[:2] == ['comments', 'add']:
                comments.append(argv[3])
                return json.dumps({'id': 'c%d' % len(comments)})
            raise AssertionError(argv)
        return run, comments

    def _record(self, operation, actor, rows, data=b'hello', task='t1', name='a.txt', direction=None):
        digest, size, _ = task_files.receive(io.BytesIO(data).read, len(data), self.project)
        run, comments = self._run(task, rows)
        payload = {'schema_version': 1, 'operation': operation, 'task': task, 'sha256': digest, 'name': name,
                   'bytes': size, 'media_type': task_files.OPAQUE,
                   'direction': direction or (task_files.TO_WORKER if operation == 'coordinator-record'
                                              else task_files.FROM_WORKER), 'note': None}
        return task_files.apply(payload, actor, run, self.project, lambda: '2026-10-22T05:00:00Z'), comments, digest

    def test_apply_writes_the_comment_then_the_journal_and_a_retry_writes_nothing(self):
        rows = {'t1': {'status': 'in_progress', 'assignee': 'worker'}}
        (status, answer), comments, digest = self._record('coordinator-record', 'coord', rows)
        self.assertEqual(201, status)
        self.assertEqual(1, len(comments))
        self.assertTrue(comments[0].startswith(task_files.PREFIX))
        self.assertEqual(digest, json.loads(comments[0][len(task_files.PREFIX):])['sha256'])
        entry = task_files.read_entry(self.project, 't1', digest)
        self.assertEqual(('coord', 'c1', None), (entry['by'], entry['record_comment'], entry['withdrawn']))
        (status, answer), comments, _ = self._record('coordinator-record', 'coord', rows)
        self.assertEqual((200, True, []), (status, answer.get('replayed'), comments))

    def test_apply_reads_the_assignee_live_and_refuses_a_closed_task(self):
        rows = {'t1': {'status': 'in_progress', 'assignee': 'worker'}}
        with self.assertRaises(task_files.Refusal) as caught:
            self._record('assignee-record', 'someone-else', rows)
        self.assertEqual(403, caught.exception.status)
        rows['t1']['status'] = 'closed'
        with self.assertRaises(task_files.Refusal):
            self._record('assignee-record', 'worker', rows)
        rows['t1']['status'] = 'open'
        self.assertEqual(201, self._record('assignee-record', 'worker', rows)[0][0])

    def test_apply_refuses_the_wrong_direction_for_the_standing(self):
        rows = {'t1': {'status': 'open', 'assignee': 'worker'}}
        with self.assertRaises(task_files.Refusal):
            self._record('coordinator-record', 'coord', rows, direction=task_files.FROM_WORKER)
        with self.assertRaises(task_files.Refusal):
            self._record('assignee-record', 'worker', rows, direction=task_files.TO_WORKER)

    def test_withdraw_by_author_only_or_coordinator_and_nothing_is_erased(self):
        rows = {'t1': {'status': 'open', 'assignee': 'worker'}}
        _, _, digest = self._record('assignee-record', 'worker', rows)
        run, comments = self._run('t1', rows)
        payload = {'schema_version': 1, 'operation': 'author-withdraw', 'task': 't1', 'sha256': digest,
                   'reason': 'wrong file'}
        with self.assertRaises(task_files.Refusal) as caught:
            task_files.apply(payload, 'other', run, self.project, lambda: 'now')
        self.assertEqual(403, caught.exception.status)
        status, answer = task_files.apply(payload, 'worker', run, self.project, lambda: 'now')
        self.assertEqual((200, True), (status, answer['withdrawn']))
        self.assertTrue(comments[-1].startswith(task_files.WITHDRAWN_PREFIX))
        self.assertEqual([], task_files.live(self.project, 't1'))
        self.assertEqual(1, len(task_files.entries(self.project, 't1')), 'the entry stays, marked withdrawn')
        self.assertIsNotNone(task_files.blob_size(self.project, digest), 'bytes are removed only by a purge')
        with self.assertRaises(task_files.Refusal) as caught:
            task_files.apply(payload, 'worker', run, self.project, lambda: 'now')
        self.assertEqual(404, caught.exception.status)

    def test_a_journal_entry_that_is_not_valid_or_misnamed_is_not_honoured(self):
        rows = {'t1': {'status': 'open', 'assignee': 'worker'}}
        _, _, digest = self._record('coordinator-record', 'coord', rows)
        directory = self.project / task_files.FOLDER / task_files.RECORDS / task_files.task_key('t1')
        good = directory / (digest + '.json')
        other = 'b' * 64
        (directory / (other + '.json')).write_bytes(good.read_bytes())          # names another digest
        (directory / ('c' * 64 + '.json')).write_text('{not json', encoding='utf-8')
        self.assertEqual([digest], [e['sha256'] for e in task_files.live(self.project, 't1')])
        self.assertIsNone(task_files.read_entry(self.project, 't2', digest), 'bound to its task')
        # Copied into another task's journal folder, the entry still names t1: t2 does not get the file.
        theirs = self.project / task_files.FOLDER / task_files.RECORDS / task_files.task_key('t2')
        theirs.mkdir()
        (theirs / (digest + '.json')).write_bytes(good.read_bytes())
        self.assertIsNone(task_files.read_entry(self.project, 't2', digest))
        self.assertEqual([], task_files.live(self.project, 't2'))

    def test_apply_refuses_a_record_whose_stored_bytes_are_missing_or_another_size(self):
        rows = {'t1': {'status': 'open', 'assignee': 'worker'}}
        run, comments = self._run('t1', rows)
        payload = {'schema_version': 1, 'operation': 'coordinator-record', 'task': 't1', 'sha256': 'd' * 64,
                   'name': 'ghost.txt', 'bytes': 5, 'media_type': task_files.OPAQUE, 'direction': task_files.TO_WORKER,
                   'note': None}
        with self.assertRaises(task_files.Refusal) as caught:
            task_files.apply(payload, 'coord', run, self.project, lambda: 'now')
        self.assertEqual(409, caught.exception.status)
        digest, size, _ = task_files.receive(io.BytesIO(b'real').read, 4, self.project)
        with self.assertRaises(task_files.Refusal):
            task_files.apply(dict(payload, sha256=digest, bytes=size + 1), 'coord', run, self.project, lambda: 'now')
        self.assertEqual([], comments, 'nothing was recorded')
        self.assertIsNone(task_files.read_entry(self.project, 't1', digest))

    def test_r2_s3_names_notes_and_reasons_are_plain_text(self):
        # Review s3: a right-to-left override that shows "invoiceexe.jpg", a zero-width space, a C1 control, a tag
        # character and a variation selector are refused in a name, a note and a reason alike.
        for bad in ('invoice‮gpj.exe', 'a​b', 'a\u0085b', 'a\U000e0041b', 'a\U000e0100b', 'a⁦b'):
            with self.assertRaises(task_files.Refusal, msg=repr(bad)) as caught:
                task_files.clean_name(bad)
            self.assertEqual(422, caught.exception.status)
            for label in ('note', 'reason'):
                with self.assertRaises(task_files.Refusal, msg=repr(bad)):
                    task_files.check_text(bad, label, 1000)
        self.assertEqual('Plan ünïcode (2).xlsx', task_files.clean_name('Plan ünïcode (2).xlsx'))
        self.assertEqual('line one\nline two', task_files.check_text('line one\nline two', 'note', 1000))

    def test_r2_s2_project_bytes_counts_only_referenced_blobs_and_discard_removes_an_orphan(self):
        rows = {'t1': {'status': 'open', 'assignee': 'worker'}}
        _, _, kept = self._record('coordinator-record', 'coord', rows, data=b'kept bytes')
        orphan, size, created = task_files.receive(io.BytesIO(b'orphan!').read, 7, self.project)
        self.assertTrue(created)
        self.assertEqual(len(b'kept bytes'), task_files.project_bytes(self.project), 'the orphan is not counted')
        self.assertFalse(task_files.discard(self.project, kept), 'a referenced blob is never removed')
        self.assertTrue(task_files.discard(self.project, orphan))
        self.assertIsNone(task_files.blob_size(self.project, orphan))
        self.assertIsNotNone(task_files.blob_size(self.project, kept))
        self.assertFalse(task_files.receive(io.BytesIO(b'kept bytes').read, 10, self.project)[2], 'already stored')

    def test_r2_s1_a_reader_past_its_deadline_is_refused_408_and_nothing_is_stored(self):
        def read(count, state={'n': 0}):
            state['n'] += 1
            if state['n'] > 1:
                raise task_files.too_slow(10 ** 6, SMALL_RATE)
            return b'x' * count
        with self.assertRaises(task_files.Refusal) as caught:
            task_files.receive(read, 10 ** 6, self.project)
        self.assertEqual(408, caught.exception.status)
        self.assertIn('task_files.min_transfer_bytes_per_second', str(caught.exception))
        self.assertEqual([], os.listdir(self.project / task_files.FOLDER / task_files.TMP))
        self.assertAlmostEqual(60 + 25 * task_files.MIB / (32 * 1024.0), task_files.transfer_seconds(25 * task_files.MIB,
                               task_files.LIMIT_DEFAULTS))

    def test_record_texts_are_reserved_on_the_raw_comment_path(self):
        entry = {'name': 'a', 'sha256': 'a' * 64, 'bytes': 1, 'media_type': task_files.OPAQUE,
                 'direction': task_files.TO_WORKER, 'note': None}
        for body in (task_files.record_text(entry), task_files.withdrawn_text('a' * 64, 'why'),
                     '﻿' + task_files.record_text(entry), task_files.PREFIX.replace('\n', '\r\n') + '{}',
                     'Kind: task-file-v2\n{}', 'Kind: task-file-withdrawn-v9\n{}'):
            with self.assertRaises(ValueError, msg=repr(body[:30])):
                reserved_comments.check_comment_body(body, 'positional', actor='worker', task='t1')
        reserved_comments.check_comment_body('See Kind: task-file-v1 in the docs', 'positional')


class TaskFilesHttpTests(agents.AgentHarness):
    def setUp(self):
        super().setUp()
        self.files_root = unique_dir('task-files-http-')
        self.addCleanup(__import__('shutil').rmtree, self.files_root, ignore_errors=True)
        self.backend.files_root = str(self.files_root)
        self.limit = dict(task_files.LIMIT_DEFAULTS, min_free_bytes=0)
        self.backend.files_limits = lambda **kw: dict(self.limit)
        admin = self.admin_token()
        self.admin = admin
        self.pid = self.create_project(admin, 'Files')
        self.other_pid = self.create_project(admin, 'Elsewhere')
        self.tokens, self.users = {}, {}
        for name, role, project in (('coord', 'coordinator', self.pid), ('coord2', 'coordinator', self.pid),
                                    ('worker', 'contributor', self.pid), ('worker2', 'contributor', self.pid),
                                    ('viewer', 'viewer', self.pid), ('outsider', 'coordinator', self.other_pid)):
            uid = self.create_account(admin, name, name + '-password-1')
            put = self.request('PUT', '/v1/projects/%s/members/%s' % (project, uid), {'role': role}, token=admin)
            self.assertIn(put.status, (200, 201), put.data)
            self.users[name] = uid
            self.tokens[name] = self.login(name, name + '-password-1')[0]
        made = self.create_task(self.tokens['coord'], self.pid, 'Hand-off')
        self.assertEqual(201, made.status, made.data)
        self.tid = made.data['id'] if 'id' in made.data else made.data['task']['id']
        made2 = self.create_task(self.tokens['coord'], self.pid, 'Other task')
        self.tid2 = made2.data['id'] if 'id' in made2.data else made2.data['task']['id']
        claimed = self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (self.pid, self.tid), {},
                               token=self.tokens['worker'])
        self.assertEqual(200, claimed.status, claimed.data)
        claimed2 = self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (self.pid, self.tid2), {},
                                token=self.tokens['worker2'])
        self.assertEqual(200, claimed2.status, claimed2.data)
        self.worker_actor = self.backend.state['tasks'][self.tid]['assignee']
        self.worker2_actor = self.backend.state['tasks'][self.tid2]['assignee']

    # -- helpers -----------------------------------------------------------------------------------
    def files_path(self, task=None, sha256=None, **query):
        path = '/v1/projects/%s/tasks/%s/files' % (self.pid, task or self.tid)
        if sha256:
            path += '/' + sha256
        if query:
            from urllib.parse import urlencode
            path += '?' + urlencode(query)
        return path

    def raw(self, method, path, token, body=b'', headers=None, length=None, close_after=None):
        """One request on its own socket; ``length`` may lie, ``close_after`` stops sending early."""
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=30)
        try:
            lines = ['%s %s HTTP/1.1' % (method, path), 'Host: 127.0.0.1', 'Authorization: Bearer ' + token,
                     'Content-Type: application/octet-stream']
            if length is not False:
                lines.append('Content-Length: %d' % (len(body) if length is None else length))
            for key, value in (headers or {}).items():
                lines.append('%s: %s' % (key, value))
            sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('utf-8'))
            sent = body if close_after is None else body[:close_after]
            sock.sendall(sent)
            if close_after is not None:
                sock.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(sock)
            response.begin()
            data = response.read()
            return agents.Response(response.status, response.getheaders(), data)
        finally:
            sock.close()

    def upload(self, token, data, name='report.bin', direction='to-worker', task=None, headers=None, **query):
        return self.raw('PUT', self.files_path(task, name=name, direction=direction, **query), token, data,
                        headers=headers)

    def download(self, token, sha256, task=None, **query):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        try:
            client.request('GET', self.files_path(task, sha256, **query), headers={'Authorization': 'Bearer ' + token})
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def listing(self, token, task=None):
        return self.request('GET', self.files_path(task), token=token)

    def given(self, data=b'input spreadsheet bytes', name='input.xlsx'):
        put = self.upload(self.tokens['coord'], data, name=name)
        self.assertEqual(201, put.status, put.data)
        return put.data['sha256']

    def audit_lines(self, action):
        return [event for event in self.service.state['audit'] if event.get('action') == action]

    def stored(self):
        base = self.files_root / self.pid / task_files.FOLDER
        found = []
        for child in base.iterdir() if base.exists() else ():
            if len(child.name) == 2:
                found.extend(blob.name for blob in child.iterdir())
        return sorted(found)

    # -- the ordinary hand-off ---------------------------------------------------------------------
    def test_coordinator_hands_a_file_to_the_assignee_who_downloads_it_with_the_safe_headers(self):
        data = os.urandom(150 * 1024)
        put = self.upload(self.tokens['coord'], data, name='Plan ünïcode.xlsx', headers={'X-Orchestra-SHA256': sha(data)},
                          note='the inputs')
        self.assertEqual(201, put.status, put.data)
        self.assertEqual(('Plan ünïcode.xlsx', len(data), 'to-worker', 'the inputs', sha(data)),
                         (put.data['name'], put.data['bytes'], put.data['direction'], put.data['note'], put.data['sha256']))
        self.assertEqual('close', put.headers.get('connection'))
        status, headers, body = self.download(self.tokens['worker'], sha(data))
        self.assertEqual(200, status)
        self.assertEqual(data, body)
        self.assertEqual('application/octet-stream', headers['Content-Type'])
        self.assertEqual("attachment; filename*=UTF-8''Plan%20%C3%BCn%C3%AFcode.xlsx", headers['Content-Disposition'])
        self.assertEqual('nosniff', headers['X-Content-Type-Options'])
        self.assertEqual('no-store', headers['Cache-Control'])
        self.assertEqual("sandbox; default-src 'none'", headers['Content-Security-Policy'])
        self.assertEqual(sha(data), headers['X-Orchestra-SHA256'])
        self.assertEqual(str(len(data)), headers['Content-Length'])
        listed = self.listing(self.tokens['worker'])
        self.assertEqual(200, listed.status, listed.data)
        self.assertEqual([sha(data)], [item['sha256'] for item in listed.data['items']])
        self.assertTrue(listed.data['items'][0]['may_download'])
        comments = self.backend.state['tasks'][self.tid].get('comments') or []
        self.assertTrue(any(c['text'].startswith(task_files.PREFIX) and sha(data) in c['text'] for c in comments))
        self.assertEqual(1, len(self.audit_lines('tasks.files.upload')))
        self.assertIn('sha256=' + sha(data), self.audit_lines('tasks.files.upload')[0]['reason'])
        downloads = self.audit_lines('tasks.files.download')
        self.assertEqual([('committed', self.worker_actor)], [(e['outcome'], e['actor']) for e in downloads])
        self.assertIn('task=%s sha256=%s' % (self.tid, sha(data)), downloads[0]['reason'])

    def test_the_assignee_hands_results_back_and_a_coordinator_downloads_them(self):
        data = b'results'
        put = self.upload(self.tokens['worker'], data, name='out.txt', direction='from-worker')
        self.assertEqual(201, put.status, put.data)
        self.assertEqual(self.worker_actor, put.data['by'])
        status, _, body = self.download(self.tokens['coord2'], sha(data))
        self.assertEqual((200, data), (status, body))

    def test_retry_of_the_same_file_and_name_is_200_and_writes_nothing(self):
        data = b'once only'
        first = self.upload(self.tokens['coord'], data, name='a.txt')
        self.assertEqual(201, first.status, first.data)
        again = self.upload(self.tokens['coord'], data, name='a.txt', headers={'X-Orchestra-SHA256': sha(data)})
        self.assertEqual((200, True), (again.status, again.data.get('replayed')), again.data)
        plain = self.upload(self.tokens['coord'], data, name='a.txt')
        self.assertEqual(200, plain.status, plain.data)
        other_name = self.upload(self.tokens['coord'], data, name='b.txt')
        self.assertEqual(409, other_name.status, other_name.data)
        records = [c for c in self.backend.state['tasks'][self.tid].get('comments') or []
                   if c['text'].startswith(task_files.PREFIX)]
        self.assertEqual(1, len(records))

    # -- who may: the adversarial cases ------------------------------------------------------------
    def test_another_projects_member_gets_nothing(self):
        digest = self.given()
        token = self.tokens['outsider']
        self.assertIn(self.listing(token).status, (403, 404))
        self.assertIn(self.download(token, digest)[0], (403, 404))
        self.assertIn(self.upload(token, b'x').status, (403, 404))
        self.assertNotIn(b'input', self.download(token, digest)[2])

    def test_a_viewer_lists_but_neither_downloads_nor_attaches(self):
        digest = self.given()
        listed = self.listing(self.tokens['viewer'])
        self.assertEqual(200, listed.status, listed.data)
        self.assertEqual((False, [digest]), (listed.data['may_download'], [i['sha256'] for i in listed.data['items']]))
        status, _, body = self.download(self.tokens['viewer'], digest)
        self.assertEqual(403, status)
        self.assertNotIn(b'input', body)
        self.assertEqual(403, self.upload(self.tokens['viewer'], b'x').status)
        self.assertEqual(403, self.upload(self.tokens['viewer'], b'x', direction='from-worker').status)
        self.assertEqual('denied', self.audit_lines('tasks.files.download')[-1]['outcome'])

    def test_a_contributor_who_is_not_the_assignee_may_not_download_or_attach(self):
        digest = self.given()
        before = self.stored()
        calls = []
        invoke = self.backend.invoke
        self.backend.invoke = lambda route, *a, **k: (calls.append(route), invoke(route, *a, **k))[1]
        self.assertEqual(403, self.download(self.tokens['worker2'], digest)[0])
        self.assertEqual(403, self.upload(self.tokens['worker2'], b'not stored 1', direction='from-worker').status)
        self.assertEqual(403, self.upload(self.tokens['worker'], b'not stored 2', direction='to-worker').status,
                         'the assignee does not attach files to the worker')
        self.assertEqual(before, self.stored(), 'a refused upload stores no bytes: it is refused before a byte is read')
        self.assertEqual([], [route for route in calls if route.startswith('files.')])

    def test_a_withdrawal_by_somebody_else_is_refused_before_the_backend_is_asked(self):
        mine = self.upload(self.tokens['worker'], b'mine', name='m.txt', direction='from-worker')
        calls = []
        invoke = self.backend.invoke
        self.backend.invoke = lambda route, *a, **k: (calls.append(route), invoke(route, *a, **k))[1]
        refused = self.request('POST', self.files_path(sha256=mine.data['sha256']) + '/withdraw', {'reason': 'x'},
                               token=self.tokens['worker2'])
        self.assertEqual(403, refused.status, refused.data)
        self.assertEqual([], calls)

    def test_the_declared_type_is_display_metadata_and_never_served(self):
        put = self.upload(self.tokens['coord'], b'<script>alert(1)</script>', name='x.html', media_type='text/html')
        self.assertEqual((201, 'text/html'), (put.status, put.data['media_type']), put.data)
        status, headers, _ = self.download(self.tokens['worker'], put.data['sha256'])
        self.assertEqual((200, 'application/octet-stream'), (status, headers['Content-Type']))

    def test_an_archived_project_keeps_its_files_downloadable_and_takes_no_new_one(self):
        digest = self.given()
        archived = self.request('POST', '/v1/projects/%s/archive' % self.pid, {}, token=self.admin)
        self.assertEqual(200, archived.status, archived.data)
        refused = self.upload(self.tokens['coord'], b'after the archive', name='late.txt')
        self.assertEqual(409, refused.status, refused.data)
        self.assertIn('archived', refused.data['error']['message'])
        self.assertEqual(1, len(self.stored()))
        self.assertEqual(200, self.download(self.tokens['coord'], digest)[0])

    def test_the_former_assignee_loses_access_the_moment_the_task_is_reassigned(self):
        digest = self.given()
        self.assertEqual(200, self.download(self.tokens['worker'], digest)[0])
        self.backend.state['tasks'][self.tid]['assignee'] = self.worker2_actor      # the tracker's row changes
        self.assertEqual(403, self.download(self.tokens['worker'], digest)[0])
        self.assertEqual(403, self.upload(self.tokens['worker'], b'late', direction='from-worker').status)
        self.assertEqual(200, self.download(self.tokens['worker2'], digest)[0], 'the new assignee at once')

    def test_a_closed_tasks_last_assignee_may_not_download_and_coordinators_still_may(self):
        digest = self.given()
        self.backend.state['tasks'][self.tid]['status'] = 'closed'
        self.assertEqual(403, self.download(self.tokens['worker'], digest)[0])
        self.assertEqual(200, self.download(self.tokens['coord'], digest)[0])
        self.backend.state['tasks'][self.tid]['status'] = 'open'        # reopened
        self.assertEqual(200, self.download(self.tokens['worker'], digest)[0])

    def test_a_forged_or_other_tasks_sha256_gets_nothing_and_a_planted_record_is_inert(self):
        mine = self.given(b'for task one')
        theirs = self.upload(self.tokens['coord'], b'for task two only', task=self.tid2)
        self.assertEqual(201, theirs.status, theirs.data)
        theirs = theirs.data['sha256']
        self.assertEqual(404, self.download(self.tokens['worker'], theirs)[0], 'another task\'s file')
        self.assertEqual(404, self.download(self.tokens['worker'], 'f' * 64)[0], 'never stored')
        self.assertEqual(404, self.download(self.tokens['coord'], theirs, task=self.tid)[0])
        # What a kit from before the prefix was reserved would let a worker write: a record comment
        # on its own task naming the other task's digest. Without a journal entry it grants nothing.
        planted = {'name': 'stolen.txt', 'sha256': theirs, 'bytes': 17, 'media_type': task_files.OPAQUE,
                   'direction': 'to-worker', 'note': None}
        self.backend.state['tasks'][self.tid].setdefault('comments', []).append(
            {'id': 'planted', 'issue_id': self.tid, 'author': self.users['coord'], 'text': task_files.record_text(planted)})
        status, _, body = self.download(self.tokens['worker'], theirs)
        self.assertEqual(404, status)
        self.assertNotIn(b'task two', body)
        self.assertEqual([mine], [i['sha256'] for i in self.listing(self.tokens['worker']).data['items']])
        # And the raw comment path refuses to write one at all.
        refused = self.request('POST', '/v1/projects/%s/tasks/%s/comments' % (self.pid, self.tid),
                               {'text': task_files.record_text(planted)}, token=self.tokens['worker'])
        self.assertEqual(422, refused.status, refused.data)

    def test_actor_naming_somebody_else_is_refused(self):
        digest = self.given()
        self.assertEqual(403, self.download(self.tokens['worker2'], digest, actor=self.worker_actor)[0])
        self.assertEqual(403, self.upload(self.tokens['worker2'], b'x', direction='from-worker',
                                          actor=self.worker_actor).status)
        self.assertEqual(200, self.download(self.tokens['worker'], digest, actor=self.worker_actor)[0])

    # -- the stream --------------------------------------------------------------------------------
    def test_a_lying_content_length_short_body_is_refused_and_nothing_is_stored(self):
        data = b'z' * 9000
        answer = self.raw('PUT', self.files_path(name='cut.bin', direction='to-worker'), self.tokens['coord'], data,
                          length=len(data), close_after=4000)
        self.assertEqual(400, answer.status, answer.data)
        self.assertIn('4000 of the 9000', answer.data['error']['message'])
        self.assertEqual([], self.stored())
        self.assertEqual([], self.listing(self.tokens['coord']).data['items'])

    def test_a_lying_content_length_long_body_stores_only_what_was_announced_and_never_reads_on(self):
        data = b'A' * 1000 + b'GET /v1/projects HTTP/1.1\r\nHost: x\r\n\r\n'
        answer = self.raw('PUT', self.files_path(name='short.bin', direction='to-worker'), self.tokens['coord'], data,
                          length=1000, headers={'X-Orchestra-SHA256': sha(data)})
        self.assertEqual(422, answer.status, 'its digest says the announced length was a lie')
        self.assertEqual('close', answer.headers.get('connection'))
        self.assertEqual([], self.stored())
        answer = self.raw('PUT', self.files_path(name='short.bin', direction='to-worker'), self.tokens['coord'], data,
                          length=1000)
        self.assertEqual(201, answer.status, answer.data)
        self.assertEqual((1000, sha(data[:1000])), (answer.data['bytes'], answer.data['sha256']))
        self.assertEqual('close', answer.headers.get('connection'), 'the rest is never read as a request')

    def test_no_content_length_is_411(self):
        answer = self.raw('PUT', self.files_path(name='a', direction='to-worker'), self.tokens['coord'], b'',
                          length=False)
        self.assertEqual(411, answer.status, answer.data)

    def test_a_body_over_each_cap_is_refused_before_it_is_read(self):
        self.limit.update(SMALL)
        too_big = self.upload(self.tokens['coord'], b'b' * 5000)
        self.assertEqual(413, too_big.status, too_big.data)
        self.assertIn('task_files.max_file_bytes', too_big.data['error']['message'])
        for index in range(3):
            self.assertEqual(201, self.upload(self.tokens['coord'], bytes([index]) * 1500, name='f%d' % index).status)
        fourth = self.upload(self.tokens['coord'], b'q', name='f3')
        self.assertEqual(409, fourth.status, fourth.data)
        self.assertIn('task_files.max_files_per_task', fourth.data['error']['message'])
        self.limit['max_files_per_task'] = 20
        task_bytes = self.upload(self.tokens['coord'], b'r' * 2000, name='big')
        self.assertEqual(413, task_bytes.status, task_bytes.data)
        self.assertIn('task_files.max_task_bytes', task_bytes.data['error']['message'])
        project = self.upload(self.tokens['coord'], b's' * 4096, name='p', task=self.tid2)
        self.assertEqual(201, project.status, project.data)
        over = self.upload(self.tokens['coord'], b't' * 1000, name='p2', task=self.tid2)
        self.assertEqual(413, over.status, over.data)
        self.assertIn('task_files.max_project_bytes', over.data['error']['message'])
        self.limit.update(max_project_bytes=10 ** 9, min_free_bytes=10 ** 18)
        disk = self.upload(self.tokens['coord'], b'u', name='d', task=self.tid2)
        self.assertEqual(507, disk.status, disk.data)
        self.assertIn('task_files.min_free_bytes', disk.data['error']['message'])
        self.assertEqual(4, len(self.stored()))
        self.assertGreaterEqual(len([e for e in self.audit_lines('tasks.files.upload') if e['outcome'] == 'rejected']), 5)

    def test_a_name_with_a_slash_dots_or_control_characters_is_refused(self):
        for name in ('../x', 'a/b', 'a\\b', '..', '.', 'bell\x07', 'x' * 129):
            answer = self.raw('PUT', self.files_path(name=name, direction='to-worker'), self.tokens['coord'], b'abc')
            self.assertEqual(422, answer.status, (name, answer.data))
        self.assertEqual([], self.stored())

    def test_a_symlinked_task_files_folder_is_refused(self):
        elsewhere = unique_dir('task-files-link-')
        self.addCleanup(__import__('shutil').rmtree, elsewhere, ignore_errors=True)
        project = self.files_root / self.pid
        project.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(str(elsewhere), str(project / task_files.FOLDER), target_is_directory=True)
        except (OSError, NotImplementedError) as failed:
            self.skipTest('this host cannot make a symlink: %s' % failed)
        answer = self.upload(self.tokens['coord'], b'abc')
        self.assertEqual(503, answer.status, answer.data)
        self.assertEqual(503, self.listing(self.tokens['coord']).status)
        self.assertEqual([], os.listdir(elsewhere))

    def test_concurrent_uploads_at_the_transfer_caps_get_503_with_retry_after(self):
        self.limit['max_transfers_per_project'] = 4        # the project's share has its own test (r2)
        gate = self.httpd.RequestHandlerClass.transfers
        held = []

        def start(token, name):
            body = name.encode() * 50000
            sock = socket.create_connection(('127.0.0.1', self.port), timeout=30)
            head = ('PUT %s HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\nContent-Length: %d\r\n\r\n'
                    % (self.files_path(name=name, direction='to-worker'), token, len(body)))
            sock.sendall(head.encode() + body[:1000])
            held.append((sock, body))

        def wait_for(count):
            for _ in range(500):
                if gate.held()[0] == count:
                    return
                time.sleep(0.01)
            self.fail('the gate holds %r, not %d' % (gate.held(), count))
        start(self.tokens['coord'], 'one')
        start(self.tokens['coord'], 'two')
        wait_for(2)
        third = self.upload(self.tokens['coord'], b'third', name='three')
        self.assertEqual(503, third.status, third.data)
        self.assertEqual('1', third.headers.get('retry-after'))
        self.assertIn('(task_files.max_transfers_per_actor)', third.data['error']['message'])
        start(self.tokens['coord2'], 'four')
        start(self.tokens['coord2'], 'five')
        wait_for(4)
        self.limit['max_transfers_per_actor'] = 3
        fifth = self.upload(self.tokens['coord2'], b'fifth', name='six')
        self.assertEqual(503, fifth.status, fifth.data)
        self.assertIn('(task_files.max_transfers)', fifth.data['error']['message'])
        statuses = []
        for sock, body in held:
            sock.sendall(body[1000:])
            response = http.client.HTTPResponse(sock)
            response.begin()
            response.read()
            statuses.append(response.status)
            sock.close()
        self.assertEqual([201, 201, 201, 201], statuses)
        self.assertEqual((0, {}), gate.held())

    def test_a_withdrawn_file_is_not_listed_and_not_downloadable(self):
        mine = self.upload(self.tokens['worker'], b'draft result', name='r.txt', direction='from-worker')
        self.assertEqual(201, mine.status, mine.data)
        digest = mine.data['sha256']
        stranger = self.request('POST', self.files_path(sha256=digest) + '/withdraw', {'reason': 'no'},
                                token=self.tokens['worker2'])
        self.assertEqual(403, stranger.status, stranger.data)
        withdrawn = self.request('POST', self.files_path(sha256=digest) + '/withdraw', {'reason': 'wrong file'},
                                 token=self.tokens['worker'])
        self.assertEqual(200, withdrawn.status, withdrawn.data)
        self.assertEqual(404, self.download(self.tokens['coord'], digest)[0])
        self.assertEqual([], self.listing(self.tokens['coord']).data['items'])
        again = self.request('POST', self.files_path(sha256=digest) + '/withdraw', {'reason': 'again'},
                             token=self.tokens['coord'])
        self.assertEqual(404, again.status)
        comments = self.backend.state['tasks'][self.tid].get('comments') or []
        self.assertTrue(any(c['text'].startswith(task_files.WITHDRAWN_PREFIX) for c in comments))
        self.assertEqual(['committed'], [e['outcome'] for e in self.audit_lines('tasks.files.withdraw')
                                         if e['outcome'] == 'committed'])

    # -- r2 (review 01a16364) ----------------------------------------------------------------------
    def test_r2_s1_a_trickling_upload_is_cut_off_and_gives_its_slot_back(self):
        self.limit.update(min_transfer_bytes_per_second=10 ** 6, transfer_grace_seconds=1)
        gate = self.httpd.RequestHandlerClass.transfers
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=30)
        try:
            body = b'z' * 400000
            head = ('PUT %s HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\nContent-Length: %d\r\n\r\n'
                    % (self.files_path(name='slow.bin', direction='to-worker'), self.tokens['coord'], len(body)))
            sock.sendall(head.encode() + body[:1000])
            started = time.monotonic()
            for _ in range(10):                                   # a trickle: 10 bytes a second
                try:
                    sock.sendall(b'z' * 10)
                except OSError:
                    break
                time.sleep(1)
                if gate.held()[0] == 0:
                    break
            self.assertEqual((0, {}), gate.held(), 'the slot is given back')
            self.assertLess(time.monotonic() - started, 8, 'cut off at the deadline, not by the client')
        finally:
            sock.close()
        self.assertEqual([], self.stored())
        self.assertTrue(any('request_timeout' in (e.get('reason') or '') for e in self.audit_lines('tasks.files.upload')))

    def test_r2_s1_a_download_read_too_slowly_is_cut_off_and_gives_its_slot_back(self):
        data = os.urandom(8 * task_files.MIB)
        digest = self.given(data, name='big.bin')
        self.limit.update(min_transfer_bytes_per_second=10 ** 7, transfer_grace_seconds=1)
        gate = self.httpd.RequestHandlerClass.transfers
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=30)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            sock.sendall(('GET %s HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\n\r\n'
                          % (self.files_path(sha256=digest), self.tokens['worker'])).encode())
            sock.recv(100)                                        # then reads nothing more
            for _ in range(100):
                if gate.held()[0] == 0:
                    break
                time.sleep(0.1)
            self.assertEqual((0, {}), gate.held(), 'the slot is given back')
        finally:
            sock.close()
        for _ in range(50):
            if any(e['outcome'] == 'interrupted' for e in self.audit_lines('tasks.files.download')):
                break
            time.sleep(0.1)
        self.assertIn('interrupted', [e['outcome'] for e in self.audit_lines('tasks.files.download')])

    def test_r2_s1_one_project_holds_at_most_its_share_of_the_transfers(self):
        self.limit.update(max_transfers=4, max_transfers_per_actor=4, max_transfers_per_project=1)
        gate = self.httpd.RequestHandlerClass.transfers
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=30)
        try:
            body = b'p' * 300000
            sock.sendall(('PUT %s HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\nContent-Length: %d\r\n\r\n'
                          % (self.files_path(name='one', direction='to-worker'), self.tokens['coord'], len(body))).encode()
                         + body[:1000])
            for _ in range(200):
                if gate.held()[0] == 1:
                    break
                time.sleep(0.01)
            second = self.upload(self.tokens['coord2'], b'second', name='two')
            self.assertEqual(503, second.status, second.data)
            self.assertIn('(task_files.max_transfers_per_project)', second.data['error']['message'])
            self.assertEqual({self.pid: 1}, gate.projects())
            sock.sendall(body[1000:])
            response = http.client.HTTPResponse(sock)
            response.begin()
            response.read()
            self.assertEqual(201, response.status)
        finally:
            sock.close()
        self.assertEqual({}, gate.projects())

    def test_r2_s2_a_refused_record_leaves_no_blob_and_the_listed_blobs_are_all_that_is_stored(self):
        first = self.raw('PUT', self.files_path(name='a.txt', direction='to-worker'), self.tokens['coord'], b'first file',
                         headers={'Idempotency-Key': 'files-key-0001'})
        self.assertEqual(201, first.status, first.data)
        reused = self.raw('PUT', self.files_path(name='b.txt', direction='to-worker'), self.tokens['coord'], b'other bytes',
                          headers={'Idempotency-Key': 'files-key-0001'})
        self.assertEqual(409, reused.status, reused.data)
        invoke = self.backend.invoke

        def lost_the_task(route, *a, **k):
            if route == 'files.record':
                raise http_auth_forbidden('the assignee changed meanwhile')
            return invoke(route, *a, **k)
        self.backend.invoke = lost_the_task
        refused = self.upload(self.tokens['coord'], b'refused at the record', name='c.txt')
        self.assertEqual(403, refused.status, refused.data)
        self.backend.invoke = invoke
        third = self.upload(self.tokens['coord'], b'third file', name='d.txt')
        self.assertEqual(201, third.status, third.data)
        listed = sorted(i['sha256'] for i in self.listing(self.tokens['coord']).data['items'])
        self.assertEqual(listed, self.stored(), 'exactly the listed blobs are on disk')
        self.assertEqual(len(b'first file') + len(b'third file'),
                         task_files.project_bytes(self.files_root / self.pid))

    def test_r2_s4a_an_aborted_download_gives_its_slot_back(self):
        digest = self.given(os.urandom(4 * task_files.MIB), name='abort.bin')
        gate = self.httpd.RequestHandlerClass.transfers
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=30)
        sock.sendall(('GET %s HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\n\r\n'
                      % (self.files_path(sha256=digest), self.tokens['worker'])).encode())
        sock.recv(65536)
        sock.close()                                              # gone mid-download
        for _ in range(200):
            if gate.held() == (0, {}) and any(e['outcome'] == 'interrupted' for e in self.audit_lines('tasks.files.download')):
                break
            time.sleep(0.05)
        self.assertEqual((0, {}), gate.held())
        self.assertEqual({}, gate.projects())
        self.assertIn('interrupted', [e['outcome'] for e in self.audit_lines('tasks.files.download')])

    def test_r2_s4b_transfer_encoding_is_refused_and_nothing_is_stored(self):
        chunked = b'5\r\nhello\r\n0\r\n\r\n'
        answer = self.raw('PUT', self.files_path(name='chunked.txt', direction='to-worker'), self.tokens['coord'], chunked,
                          headers={'Transfer-Encoding': 'chunked'})
        self.assertEqual(400, answer.status, answer.data)
        self.assertIn('Transfer-Encoding', answer.data['error']['message'])
        self.assertEqual([], self.stored())

    def test_r2_s4c_no_100_continue_before_the_checks_and_one_after_them(self):
        def expect(token, direction):
            sock = socket.create_connection(('127.0.0.1', self.port), timeout=30)
            try:
                sock.sendall(('PUT %s HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\nContent-Length: 5\r\n'
                              'Expect: 100-continue\r\n\r\n' % (self.files_path(name='e.txt', direction=direction), token)).encode())
                first = sock.recv(4096).decode('latin-1')
                if first.startswith('HTTP/1.1 100'):
                    sock.sendall(b'hello')
                    rest = b''
                    while b'\r\n\r\n' not in rest:
                        rest += sock.recv(4096)
                    return first, rest.decode('latin-1')
                return first, None
            finally:
                sock.close()
        first, rest = expect(self.tokens['viewer'], 'to-worker')
        self.assertTrue(first.startswith('HTTP/1.1 403'), first)
        self.assertIsNone(rest, 'no 100 Continue before the refusal')
        first, rest = expect(self.tokens['coord'], 'to-worker')
        self.assertTrue(first.startswith('HTTP/1.1 100'), first)
        self.assertTrue(rest.startswith('HTTP/1.1 201'), rest)

    def test_r2_s4d_an_archived_project_serves_its_coordinators_only_and_takes_no_withdrawal(self):
        digest = self.given()
        self.assertEqual(201, self.upload(self.tokens['worker'], b'res', name='r.txt', direction='from-worker').status)
        archived = self.request('POST', '/v1/projects/%s/archive' % self.pid, {}, token=self.admin)
        self.assertEqual(200, archived.status, archived.data)
        status, _, body = self.download(self.tokens['worker'], digest)
        self.assertEqual(403, status)
        self.assertNotIn(b'input', body)
        self.assertEqual(200, self.download(self.tokens['coord'], digest)[0])
        refused = self.request('POST', self.files_path(sha256=digest) + '/withdraw', {'reason': 'x'}, token=self.tokens['coord'])
        self.assertEqual(409, refused.status, refused.data)
        self.assertIn('archived', refused.data['error']['message'])

    def test_r2_s4e_an_audit_line_that_cannot_be_recorded_is_said_in_the_log(self):
        digest = self.given()
        import contextlib
        err = io.StringIO()
        original = self.service.audit

        def broken(*a, **k):
            if a[2] == 'tasks.files.download':
                raise OSError('disk full')
            return original(*a, **k)
        self.service.audit = broken
        try:
            with contextlib.redirect_stderr(err):
                status, _, body = self.download(self.tokens['worker'], digest)
                time.sleep(0.2)
        finally:
            self.service.audit = original
        self.assertEqual(200, status)
        self.assertIn('audit: the tasks.files.download line (committed', err.getvalue())
        self.assertIn('disk full', err.getvalue())

    def test_a_coordinator_withdraws_anyone_s_file(self):
        mine = self.upload(self.tokens['worker'], b'x1', name='r.txt', direction='from-worker')
        answer = self.request('POST', self.files_path(sha256=mine.data['sha256']) + '/withdraw', {'reason': 'secret'},
                              token=self.tokens['coord'])
        self.assertEqual(200, answer.status, answer.data)

    def test_without_a_store_every_route_answers_501(self):
        self.backend.files_root = None
        self.assertEqual(501, self.listing(self.tokens['coord']).status)
        self.assertEqual(501, self.upload(self.tokens['coord'], b'x').status)
        self.assertEqual(501, self.download(self.tokens['coord'], 'a' * 64)[0])

    def test_task_create_attachments_still_answer_501_and_point_to_the_files_route(self):
        import base64
        made = self.request('POST', '/v1/projects/%s/tasks' % self.pid,
                            {'title': 'with attachment', 'attachments': [
                                {'name': 'a.txt', 'content_base64': base64.b64encode(b'hi').decode()}]},
                            token=self.tokens['coord'])
        self.assertEqual(501, made.status, made.data)
        self.assertIn('/files', made.data['error']['message'])


class TaskFilesEndpointBindingTests(unittest.TestCase):
    """The canonical action's structural gates (its behaviour on real bd: test_task_files_real_bd)."""

    def test_task_file_is_a_service_only_action(self):
        import importlib
        try:
            endpoint = importlib.import_module('endpoint')
        except ImportError as missing:                                   # fcntl: POSIX only
            self.skipTest('endpoint.py needs a POSIX host: %s' % missing)
        self.assertIn('task-file', endpoint.SERVICE_ONLY_ACTIONS)
        with self.assertRaises(ValueError):
            endpoint.key_project_refusal({'action': 'task-file', 'project': 'p'}, ['p'])

    def test_each_operation_needs_its_own_capability(self):
        from http_authority import CAP_COORDINATE, CAP_TASKS, required_capability
        for operation, capability in (('coordinator-record', CAP_COORDINATE), ('assignee-record', CAP_TASKS),
                                      ('coordinator-withdraw', CAP_COORDINATE), ('author-withdraw', CAP_TASKS)):
            request = {'action': 'task-file', 'args': [json.dumps({'operation': operation})]}
            self.assertEqual(capability, required_capability(request), operation)
        self.assertIsNone(required_capability({'action': 'task-file', 'args': [json.dumps({'operation': 'x'})]}))
        self.assertIsNone(required_capability({'action': 'task-file', 'args': ['{}', '{}']}))


if __name__ == '__main__':
    unittest.main()
