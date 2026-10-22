"""Adversarial and contract tests for delivered code over HTTP, slice 1 (kittrial-5bb.373).

Covers:
1. viewer 403 (`test_viewer_403`)
2. member of another project 403 (`test_member_of_another_project_403`)
3. contributor and coordinator 200 (`test_contributor_and_coordinator_200`)
4. contribution id of another task 404 (`test_contribution_id_of_another_task_404`)
5. contribution id of another project 404 (`test_contribution_id_of_another_project_404`)
6. forged id 404 (`test_forged_id_404`)
7. remote delivery 422 (`test_remote_delivery_422`)
8. missing file 410 (`test_missing_file_410`)
9. file outside every root 410 (`test_file_outside_every_root_410`)
10. symlink to a real bundle 410 (`test_symlink_to_a_real_bundle_410`)
11. path naming another host 410 (`test_path_naming_another_host_410`)
12. bundle over the limit 413 (`test_bundle_over_the_limit_413`)
13. file whose sha256 differs 503 with zero bytes sent (`test_file_whose_sha256_differs_503_with_zero_bytes_sent`)
14. bundle whose head differs from the record 503 (`test_bundle_whose_head_differs_from_the_record_503`)
15. list-heads timeout 503 (`test_list_heads_timeout_503`)
16. an audit line exists for a download (`test_an_audit_line_exists_for_a_download`)
17. client refuses and deletes a body whose digest differs (`test_client_refuses_and_deletes_a_body_whose_digest_differs`)
18. client never writes a partial --out file (`test_client_never_writes_a_partial_out_file`)

Plus coordinator precisions (01a16278):
- Precision 1: HOST: prefix stripped without comparing host names (tested with a HOST:
  prefix naming a host that is not this one AND with one naming this host).
- Precision 2: CAP_APPROVE fallback runs ONLY after a 403 from CAP_REVIEWS for the same
  route project; 404/401 returned as is, and a principal with neither capability gets 403.
- Precision 3: The routes write no state except the audit line; no lock is taken.
- Precision 4: endpoint.py and client.py on SSH/local transport answer the fixed sentence
  and read nothing from disk.
"""
import copy
import hashlib
import http.client
import http.server
import io
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import admin
import client as cli_client
import http_client
import review_workflow as rw
from http_auth import Service, Store
from http_service import MAX_BODY_BYTES, InProcessBackend, create_server

try:
    import endpoint
except ImportError:
    endpoint = None

ADMIN = 'root-admin'
ADMIN_PASSWORD = 'correct-horse-battery-staple'


def _git(repo, *args):
    env = dict(os.environ)
    env.setdefault('GIT_AUTHOR_NAME', 'Test User')
    env.setdefault('GIT_AUTHOR_EMAIL', 'test@example.invalid')
    env.setdefault('GIT_COMMITTER_NAME', 'Test User')
    env.setdefault('GIT_COMMITTER_EMAIL', 'test@example.invalid')
    proc = subprocess.run(
        ['git', '-C', str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
        env=env,
    )
    return proc.stdout.strip()


def build_git_bundle_fixture(base_dir):
    """Create a small git repository with base + head1 + head2 commits and two bundles."""
    repo = Path(base_dir) / 'git-repo'
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, 'init')
    (repo / 'file.txt').write_text('base\n', encoding='utf-8')
    _git(repo, 'add', 'file.txt')
    _git(repo, 'commit', '-m', 'base commit')
    base_commit = _git(repo, 'rev-parse', 'HEAD')

    (repo / 'file.txt').write_text('base\nchange 1\n', encoding='utf-8')
    _git(repo, 'add', 'file.txt')
    _git(repo, 'commit', '-m', 'head 1')
    head1_commit = _git(repo, 'rev-parse', 'HEAD')

    drop_root = Path(base_dir) / 'drop'
    lane_dir = drop_root / 'lane'
    lane_dir.mkdir(parents=True, exist_ok=True)
    bundle1_path = lane_dir / 'task-r1.bundle'
    _git(repo, 'bundle', 'create', str(bundle1_path), 'HEAD')
    bundle1_bytes = bundle1_path.read_bytes()
    bundle1_sha256 = hashlib.sha256(bundle1_bytes).hexdigest()

    (repo / 'file.txt').write_text('base\nchange 1\nchange 2\n', encoding='utf-8')
    _git(repo, 'add', 'file.txt')
    _git(repo, 'commit', '-m', 'head 2')
    head2_commit = _git(repo, 'rev-parse', 'HEAD')
    bundle2_path = lane_dir / 'task-r2.bundle'
    _git(repo, 'bundle', 'create', str(bundle2_path), 'HEAD')
    bundle2_bytes = bundle2_path.read_bytes()
    bundle2_sha256 = hashlib.sha256(bundle2_bytes).hexdigest()

    return {
        'repo': repo,
        'drop_root': drop_root,
        'lane_dir': lane_dir,
        'base_commit': base_commit,
        'head1_commit': head1_commit,
        'bundle1_path': bundle1_path,
        'bundle1_bytes': bundle1_bytes,
        'bundle1_sha256': bundle1_sha256,
        'head2_commit': head2_commit,
        'bundle2_path': bundle2_path,
        'bundle2_bytes': bundle2_bytes,
        'bundle2_sha256': bundle2_sha256,
    }


class RawResponse:
    def __init__(self, status, header_list, body):
        self.status = status
        self.header_list = header_list
        self.headers = {k.lower(): v for k, v in header_list}
        self.body = body
        try:
            self.data = json.loads(body.decode('utf-8')) if body else None
        except (ValueError, UnicodeDecodeError):
            self.data = None


class HttpDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._class_tmp = tempfile.TemporaryDirectory(prefix='delivery-fixture-')
        cls.fixture = build_git_bundle_fixture(cls._class_tmp.name)
        cls._shared_drop = Path(cls._class_tmp.name) / 'shared-drop'
        cls._shared_lane = cls._shared_drop / 'lane'
        cls._shared_lane.mkdir(parents=True, exist_ok=True)
        cls._shared_bundle1 = cls._shared_lane / 'alpha-1-r1.bundle'
        cls._shared_bundle2 = cls._shared_lane / 'alpha-2-r1.bundle'
        cls._shared_bundle1.write_bytes(cls.fixture['bundle1_bytes'])
        cls._shared_bundle2.write_bytes(cls.fixture['bundle2_bytes'])

        # Pre-build a template Store with accounts, sessions, projects, tasks, and contributions once.
        cls._template_dir = Path(cls._class_tmp.name) / 'template-store'
        cls._template_dir.mkdir(parents=True, exist_ok=True)
        cls._store = Store(cls._template_dir / 'state.json')
        Service.bootstrap_superuser(cls._store, ADMIN, ADMIN_PASSWORD)
        cls._service = Service(cls._store)
        cls._backend = InProcessBackend(
            cls._service,
            delivery_roots=[str(cls._shared_drop)],
            delivery_serve_max_bytes=rw.DELIVERY_SERVE_MAX_BYTES,
        )
        cls._httpd = create_server(cls._service, cls._backend, host='127.0.0.1', port=0, max_body=MAX_BODY_BYTES)
        cls._port = cls._httpd.server_address[1]
        cls._thread = threading.Thread(
            target=cls._httpd.serve_forever,
            kwargs={'poll_interval': 0.05},
            daemon=True,
        )
        cls._thread.start()

        def req(method, path, body=None, token=None):
            conn = http.client.HTTPConnection('127.0.0.1', cls._port, timeout=15)
            headers = {}
            payload = None
            if body is not None:
                payload = json.dumps(body).encode('utf-8')
                headers['Content-Type'] = 'application/json'
            if token:
                headers['Authorization'] = 'Bearer ' + token
            if method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                headers['Idempotency-Key'] = 'k-' + secrets.token_hex(8)
            try:
                conn.request(method, path, body=payload, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                return RawResponse(resp.status, resp.getheaders(), raw)
            finally:
                conn.close()

        try:
            admin_token = req('POST', '/v1/sessions', {'username': ADMIN, 'password': ADMIN_PASSWORD}).data['session']['token']

            def make_account(username):
                pw = 'password-for-' + username
                uid = req('POST', '/v1/accounts', {'username': username}, token=admin_token).data['id']
                req('POST', '/v1/accounts/%s/password' % uid, {'new_password': pw}, token=admin_token)
                tok = req('POST', '/v1/sessions', {'username': username, 'password': pw}).data['session']['token']
                return uid, tok

            owner_id, cls._owner_token = make_account('owner-user')
            coord_id, cls._coord_token = make_account('coord-user')
            contrib_id, cls._contrib_token = make_account('contrib-user')
            viewer_id, cls._viewer_token = make_account('viewer-user')
            beta_id, cls._beta_contrib_token = make_account('beta-user')

            cls._alpha_id = req('POST', '/v1/projects', {'name': 'Alpha'}, token=cls._owner_token).data['id']
            cls._beta_id = req('POST', '/v1/projects', {'name': 'Beta'}, token=cls._beta_contrib_token).data['id']

            req('PUT', '/v1/projects/%s/members/%s' % (cls._alpha_id, coord_id), {'role': 'coordinator'}, token=cls._owner_token)
            req('PUT', '/v1/projects/%s/members/%s' % (cls._alpha_id, contrib_id), {'role': 'contributor'}, token=cls._owner_token)
            req('PUT', '/v1/projects/%s/members/%s' % (cls._alpha_id, viewer_id), {'role': 'viewer'}, token=cls._owner_token)

            t1 = req('POST', '/v1/projects/%s/tasks' % cls._alpha_id, {'title': 'Alpha task 1'}, token=cls._contrib_token)
            cls._task_id = t1.data['id']
            req('POST', '/v1/projects/%s/tasks/%s/claim' % (cls._alpha_id, cls._task_id), {}, token=cls._contrib_token)

            t2 = req('POST', '/v1/projects/%s/tasks' % cls._alpha_id, {'title': 'Alpha task 2'}, token=cls._contrib_token)
            cls._other_task_id = t2.data['id']
            req('POST', '/v1/projects/%s/tasks/%s/claim' % (cls._alpha_id, cls._other_task_id), {}, token=cls._contrib_token)

            tb = req('POST', '/v1/projects/%s/tasks' % cls._beta_id, {'title': 'Beta task 1'}, token=cls._beta_contrib_token)
            cls._beta_task_id = tb.data['id']
            req('POST', '/v1/projects/%s/tasks/%s/claim' % (cls._beta_id, cls._beta_task_id), {}, token=cls._beta_contrib_token)

            c1 = req(
                'POST',
                '/v1/projects/%s/tasks/%s/reviews' % (cls._alpha_id, cls._task_id),
                {
                    'schema_version': 1,
                    'operation': 'contribute',
                    'operation_id': 'alpha-t1-r1',
                    'previous': None,
                    'repository': 'https://example.invalid/repo.git',
                    'commit': cls.fixture['head1_commit'],
                    'base_commit': cls.fixture['base_commit'],
                    'delivery': {
                        'kind': 'bundle',
                        'path': str(cls._shared_bundle1),
                        'sha256': cls.fixture['bundle1_sha256'],
                    },
                    'summary': 'Deliver bundle alpha-t1-r1',
                    'supersedes': None,
                },
                token=cls._contrib_token,
            )
            cls._contribution_id = c1.data['contribution']['id']

            c2 = req(
                'POST',
                '/v1/projects/%s/tasks/%s/reviews' % (cls._alpha_id, cls._other_task_id),
                {
                    'schema_version': 1,
                    'operation': 'contribute',
                    'operation_id': 'alpha-t2-r1',
                    'previous': None,
                    'repository': 'https://example.invalid/repo.git',
                    'commit': cls.fixture['head2_commit'],
                    'base_commit': cls.fixture['head1_commit'],
                    'delivery': {
                        'kind': 'bundle',
                        'path': str(cls._shared_bundle2),
                        'sha256': cls.fixture['bundle2_sha256'],
                    },
                    'summary': 'Deliver bundle alpha-t2-r1',
                    'supersedes': None,
                },
                token=cls._contrib_token,
            )
            cls._other_task_contribution_id = c2.data['contribution']['id']

            cb = req(
                'POST',
                '/v1/projects/%s/tasks/%s/reviews' % (cls._beta_id, cls._beta_task_id),
                {
                    'schema_version': 1,
                    'operation': 'contribute',
                    'operation_id': 'beta-t1-r1',
                    'previous': None,
                    'repository': 'https://example.invalid/repo.git',
                    'commit': cls.fixture['head1_commit'],
                    'base_commit': cls.fixture['base_commit'],
                    'delivery': {
                        'kind': 'bundle',
                        'path': str(cls._shared_bundle1),
                        'sha256': cls.fixture['bundle1_sha256'],
                    },
                    'summary': 'Deliver bundle beta-t1-r1',
                    'supersedes': None,
                },
                token=cls._beta_contrib_token,
            )
            cls._other_project_contribution_id = cb.data['contribution']['id']
            cls._initial_task1_contributions = [
                dict(r) for r in cls._backend.state['contributions'][cls._task_id]
            ]
            cls._store.save()
        except Exception:
            cls._httpd.shutdown()
            cls._httpd.server_close()
            cls._thread.join(timeout=5)
            cls._class_tmp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls._httpd.shutdown()
            cls._httpd.server_close()
            cls._thread.join(timeout=5)
        finally:
            cls._class_tmp.cleanup()

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix='delivery-svc-')
        self.addCleanup(self.tmp_dir.cleanup)
        self.tmp_path = Path(self.tmp_dir.name)
        self.drop_root = self._shared_drop
        self.lane_dir = self._shared_lane
        self.bundle_path = self._shared_bundle1
        self.other_bundle_path = self._shared_bundle2
        self.bundle_path.write_bytes(self.fixture['bundle1_bytes'])
        self.other_bundle_path.write_bytes(self.fixture['bundle2_bytes'])
        self.bundle_sha256 = self.fixture['bundle1_sha256']
        self.commit = self.fixture['head1_commit']
        self.base_commit = self.fixture['base_commit']

        self.store = self._store
        self.service = self._service
        self.backend = self._backend
        self.backend.state['contributions'][self._task_id] = [
            dict(r) for r in self._initial_task1_contributions
        ]
        self.backend.delivery_roots = (str(self.drop_root),)
        self.backend.delivery_serve_max_bytes = rw.DELIVERY_SERVE_MAX_BYTES
        self.backend.bundle_git = None
        self.httpd = self._httpd
        self.port = self._port
        self.base_url = 'http://127.0.0.1:%d' % self.port

        self.alpha_id = self._alpha_id
        self.beta_id = self._beta_id
        self.owner_token = self._owner_token
        self.coord_token = self._coord_token
        self.contrib_token = self._contrib_token
        self.viewer_token = self._viewer_token
        self.beta_contrib_token = self._beta_contrib_token
        self.task_id = self._task_id
        self.other_task_id = self._other_task_id
        self.beta_task_id = self._beta_task_id
        self.contribution_id = self._contribution_id
        self.other_task_contribution_id = self._other_task_contribution_id
        self.other_project_contribution_id = self._other_project_contribution_id

    def _req(self, method, path, body=None, token=None, key=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if key or method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            headers['Idempotency-Key'] = key or ('k-' + secrets.token_hex(8))
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            return RawResponse(resp.status, resp.getheaders(), raw)
        finally:
            conn.close()

    def _contribute_bundle(self, project_id, task_id, token, *, commit, base_commit, path, sha256, operation_id, previous=None):
        res = self._req(
            'POST',
            '/v1/projects/%s/tasks/%s/reviews' % (project_id, task_id),
            {
                'schema_version': 1,
                'operation': 'contribute',
                'operation_id': operation_id,
                'previous': previous,
                'repository': 'https://example.invalid/repo.git',
                'commit': commit,
                'base_commit': base_commit,
                'delivery': {'kind': 'bundle', 'path': path, 'sha256': sha256},
                'summary': 'Deliver bundle ' + operation_id,
                'supersedes': previous,
            },
            token=token,
        )
        self.assertEqual(res.status, 201, res.data)
        return res

    # -- 18 named adversarial tests -------------------------------------------

    def test_viewer_403(self):
        """1. viewer 403 on both metadata and bundle routes, plus Precision 2 check."""
        for suffix in ('', '/bundle'):
            res = self._req(
                'GET',
                '/v1/projects/%s/tasks/%s/delivery%s' % (self.alpha_id, self.task_id, suffix),
                token=self.viewer_token,
            )
            self.assertEqual(res.status, 403, res.data)
            self.assertEqual(res.data['error']['code'], 'forbidden')
        # Precision 2: unknown project returns 404 (not 403 from fallback), and unauthenticated returns 401
        res_unknown = self._req(
            'GET',
            '/v1/projects/proj_000000000000/tasks/%s/delivery' % self.task_id,
            token=self.contrib_token,
        )
        self.assertEqual(res_unknown.status, 404, res_unknown.data)
        res_unauth = self._req('GET', '/v1/projects/%s/tasks/%s/delivery' % (self.alpha_id, self.task_id))
        self.assertEqual(res_unauth.status, 401, res_unauth.data)

    def test_member_of_another_project_403(self):
        """2. member of another project is refused before any task/record lookup."""
        for suffix in ('', '/bundle'):
            res = self._req(
                'GET',
                '/v1/projects/%s/tasks/%s/delivery%s' % (self.alpha_id, self.task_id, suffix),
                token=self.viewer_token,
            )
            self.assertEqual(res.status, 403, res.data)
            res_other = self._req(
                'GET',
                '/v1/projects/%s/tasks/%s/delivery%s' % (self.alpha_id, self.task_id, suffix),
                token=self.beta_contrib_token,
            )
            self.assertIn(res_other.status, (403, 404), res_other.data)

    def test_contributor_and_coordinator_200(self):
        """3. contributor and coordinator 200 on both metadata and bundle routes."""
        for token in (self.contrib_token, self.coord_token, self.owner_token):
            meta = self._req(
                'GET',
                '/v1/projects/%s/tasks/%s/delivery' % (self.alpha_id, self.task_id),
                token=token,
            )
            self.assertEqual(meta.status, 200, meta.data)
            self.assertEqual(meta.data['contribution'], self.contribution_id)
            self.assertEqual(meta.data['kind'], 'bundle')
            self.assertEqual(meta.data['commit'], self.commit)
            self.assertEqual(meta.data['base_commit'], self.base_commit)
            self.assertEqual(meta.data['sha256'], self.bundle_sha256)
            self.assertEqual(meta.data['bytes'], len(self.fixture['bundle1_bytes']))
            self.assertEqual(meta.data['path'], str(self.bundle_path))
            self.assertEqual(meta.data['serve_limit'], rw.DELIVERY_SERVE_MAX_BYTES)
            self.assertIn('bundle', meta.data['links'])

            bundle = self._req(
                'GET',
                '/v1/projects/%s/tasks/%s/delivery/bundle' % (self.alpha_id, self.task_id),
                token=token,
            )
            self.assertEqual(bundle.status, 200)
            self.assertEqual(bundle.headers['content-type'], 'application/octet-stream')
            self.assertEqual(bundle.headers['etag'], self.bundle_sha256)
            self.assertEqual(bundle.headers['x-contribution'], self.contribution_id)
            self.assertEqual(int(bundle.headers['content-length']), len(self.fixture['bundle1_bytes']))
            self.assertEqual(bundle.body, self.fixture['bundle1_bytes'])

    def test_contribution_id_of_another_task_404(self):
        """4. contribution id of another task in the same project answers 404."""
        for suffix in ('', '/bundle'):
            res = self._req(
                'GET',
                '/v1/projects/%s/tasks/%s/delivery%s?contribution=%s'
                % (self.alpha_id, self.task_id, suffix, self.other_task_contribution_id),
                token=self.contrib_token,
            )
            self.assertEqual(res.status, 404, res.data)
            self.assertEqual(res.data['error']['message'], 'Contribution not found')

    def test_contribution_id_of_another_project_404(self):
        """5. contribution id of another project answers 404."""
        for suffix in ('', '/bundle'):
            res = self._req(
                'GET',
                '/v1/projects/%s/tasks/%s/delivery%s?contribution=%s'
                % (self.alpha_id, self.task_id, suffix, self.other_project_contribution_id),
                token=self.contrib_token,
            )
            self.assertEqual(res.status, 404, res.data)
            self.assertEqual(res.data['error']['message'], 'Contribution not found')

    def test_forged_id_404(self):
        """6. forged contribution id answers 404."""
        for forged in ('01a00000-0000-7000-8000-000000000000', 'forged-id-999', '../bad/id'):
            for suffix in ('', '/bundle'):
                res = self._req(
                    'GET',
                    '/v1/projects/%s/tasks/%s/delivery%s?contribution=%s'
                    % (self.alpha_id, self.task_id, suffix, forged),
                    token=self.contrib_token,
                )
                self.assertEqual(res.status, 404, res.data)
                self.assertEqual(res.data['error']['message'], 'Contribution not found')

    def test_remote_delivery_422(self):
        """7. remote delivery answers 422 with the fixed sentence."""
        tr = self._req('POST', '/v1/projects/%s/tasks' % self.alpha_id, {'title': 'Remote task'}, token=self.contrib_token)
        remote_tid = tr.data['id']
        self._req('POST', '/v1/projects/%s/tasks/%s/claim' % (self.alpha_id, remote_tid), {}, token=self.contrib_token)
        cr = self._req(
            'POST',
            '/v1/projects/%s/tasks/%s/reviews' % (self.alpha_id, remote_tid),
            {
                'schema_version': 1,
                'operation': 'contribute',
                'operation_id': 'remote-r1',
                'previous': None,
                'repository': 'https://example.invalid/repo.git',
                'commit': self.commit,
                'base_commit': self.base_commit,
                'delivery': {'kind': 'remote', 'remote': 'origin', 'branch': 'feature/x'},
                'summary': 'Remote delivery',
                'supersedes': None,
            },
            token=self.contrib_token,
        )
        self.assertEqual(cr.status, 201, cr.data)
        meta = self._req('GET', '/v1/projects/%s/tasks/%s/delivery' % (self.alpha_id, remote_tid), token=self.contrib_token)
        self.assertEqual(meta.status, 200, meta.data)
        self.assertEqual(meta.data['kind'], 'remote')
        self.assertIsNone(meta.data['links']['bundle'])

        bundle = self._req('GET', '/v1/projects/%s/tasks/%s/delivery/bundle' % (self.alpha_id, remote_tid), token=self.contrib_token)
        self.assertEqual(bundle.status, 422, bundle.data)
        self.assertEqual(bundle.data['error']['message'], rw.DELIVERY_REMOTE_NO_BUNDLE)

    def test_missing_file_410(self):
        """8. missing bundle file answers 410 with the fixed sentence."""
        self.bundle_path.unlink()
        res = self._req('GET', '/v1/projects/%s/tasks/%s/delivery/bundle' % (self.alpha_id, self.task_id), token=self.contrib_token)
        self.assertEqual(res.status, 410, res.data)
        self.assertEqual(res.data['error']['message'], rw.DELIVERY_NOT_AVAILABLE)

    def test_file_outside_every_root_410(self):
        """9. bundle file outside every registered delivery_roots directory answers 410."""
        outside_dir = self.tmp_path / 'outside-root'
        outside_dir.mkdir(parents=True, exist_ok=True)
        outside_bundle = outside_dir / 'escaped.bundle'
        outside_bundle.write_bytes(self.fixture['bundle1_bytes'])
        c_out = self._contribute_bundle(
            self.alpha_id,
            self.task_id,
            self.contrib_token,
            commit=self.commit,
            base_commit=self.base_commit,
            path=str(outside_bundle),
            sha256=self.bundle_sha256,
            operation_id='alpha-t1-outside',
            previous=self.contribution_id,
        )
        res = self._req(
            'GET',
            '/v1/projects/%s/tasks/%s/delivery/bundle?contribution=%s'
            % (self.alpha_id, self.task_id, c_out.data['contribution']['id']),
            token=self.contrib_token,
        )
        self.assertEqual(res.status, 410, res.data)
        self.assertEqual(res.data['error']['message'], rw.DELIVERY_NOT_AVAILABLE)

    def test_symlink_to_a_real_bundle_410(self):
        """10. symlink to a real bundle answers 410."""
        link_path = self.lane_dir / 'symlinked.bundle'
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()
        os.symlink(self.bundle_path, link_path)
        self.addCleanup(lambda: link_path.unlink() if (link_path.exists() or link_path.is_symlink()) else None)
        c_sym = self._contribute_bundle(
            self.alpha_id,
            self.task_id,
            self.contrib_token,
            commit=self.commit,
            base_commit=self.base_commit,
            path=str(link_path),
            sha256=self.bundle_sha256,
            operation_id='alpha-t1-symlink',
            previous=self.contribution_id,
        )
        res = self._req(
            'GET',
            '/v1/projects/%s/tasks/%s/delivery/bundle?contribution=%s'
            % (self.alpha_id, self.task_id, c_sym.data['contribution']['id']),
            token=self.contrib_token,
        )
        self.assertEqual(res.status, 410, res.data)
        self.assertEqual(res.data['error']['message'], rw.DELIVERY_NOT_AVAILABLE)

    def test_path_naming_another_host_410(self):
        """11. Precision 1: HOST: prefix is stripped without comparing host names.

        Tested with a HOST: prefix naming a host that is not this one AND with one naming
        this host:
        - When the stripped path does not exist under registered delivery_roots (e.g. an
          unmounted goomba path on a koopa service), both prefixes answer 410.
        - When the stripped path DOES exist under registered delivery_roots, both a
          foreign host prefix and this host's prefix resolve and answer 200.
        """
        this_host = socket.gethostname().split('.')[0] or 'localhost'
        other_host = 'otherhost-not-' + this_host
        unmounted_path = '/srv/unmounted-drop/lane/missing.bundle'

        prev = self.contribution_id
        for idx, host_prefix in enumerate((other_host, this_host), start=1):
            c_unmounted = self._contribute_bundle(
                self.alpha_id,
                self.task_id,
                self.contrib_token,
                commit=self.commit,
                base_commit=self.base_commit,
                path='%s:%s' % (host_prefix, unmounted_path),
                sha256=self.bundle_sha256,
                operation_id='alpha-t1-host-unmounted-%d' % idx,
                previous=prev,
            )
            prev = c_unmounted.data['contribution']['id']
            res_410 = self._req(
                'GET',
                '/v1/projects/%s/tasks/%s/delivery/bundle?contribution=%s' % (self.alpha_id, self.task_id, prev),
                token=self.contrib_token,
            )
            self.assertEqual(res_410.status, 410, (host_prefix, res_410.data))
            self.assertEqual(res_410.data['error']['message'], rw.DELIVERY_NOT_AVAILABLE)

            # And when the path below HOST: IS under a registered root, both prefixes serve 200
            posix_bundle_path = os.path.splitdrive(str(self.bundle_path))[1].replace('\\', '/')
            c_mounted = self._contribute_bundle(
                self.alpha_id,
                self.task_id,
                self.contrib_token,
                commit=self.commit,
                base_commit=self.base_commit,
                path='%s:%s' % (host_prefix, posix_bundle_path),
                sha256=self.bundle_sha256,
                operation_id='alpha-t1-host-mounted-%d' % idx,
                previous=prev,
            )
            prev = c_mounted.data['contribution']['id']
            res_200 = self._req(
                'GET',
                '/v1/projects/%s/tasks/%s/delivery/bundle?contribution=%s' % (self.alpha_id, self.task_id, prev),
                token=self.contrib_token,
            )
            self.assertEqual(res_200.status, 200, host_prefix)
            self.assertEqual(res_200.body, self.fixture['bundle1_bytes'])

    def test_bundle_over_the_limit_413(self):
        """12. bundle over delivery_serve_max_bytes answers 413 with the stated number."""
        self.backend.delivery_serve_max_bytes = 64
        res = self._req('GET', '/v1/projects/%s/tasks/%s/delivery/bundle' % (self.alpha_id, self.task_id), token=self.contrib_token)
        self.assertEqual(res.status, 413, res.data)
        self.assertEqual(res.data['error']['code'], 'payload_too_large')
        self.assertIn('64', res.data['error']['message'])
        self.assertIn(str(len(self.fixture['bundle1_bytes'])), res.data['error']['message'])

    def test_file_whose_sha256_differs_503_with_zero_bytes_sent(self):
        """13. file whose sha256 differs from recorded sha256 answers 503 with zero bundle bytes sent."""
        tampered = bytearray(self.fixture['bundle1_bytes'])
        tampered[-1] ^= 0xFF
        self.bundle_path.write_bytes(bytes(tampered))
        res = self._req('GET', '/v1/projects/%s/tasks/%s/delivery/bundle' % (self.alpha_id, self.task_id), token=self.contrib_token)
        self.assertEqual(res.status, 503, res.data)
        self.assertEqual(res.data['error']['message'], rw.DELIVERY_CHECKSUM_MISMATCH)
        self.assertNotIn(b'# v2 git bundle', res.body)

    def test_bundle_whose_head_differs_from_the_record_503(self):
        """14. bundle whose advertised head commit differs from recorded commit answers 503."""
        c_head = self._contribute_bundle(
            self.alpha_id,
            self.task_id,
            self.contrib_token,
            commit=self.fixture['head1_commit'],
            base_commit=self.base_commit,
            path=str(self.other_bundle_path),
            sha256=self.fixture['bundle2_sha256'],
            operation_id='alpha-t1-head-mismatch',
            previous=self.contribution_id,
        )
        res = self._req(
            'GET',
            '/v1/projects/%s/tasks/%s/delivery/bundle?contribution=%s'
            % (self.alpha_id, self.task_id, c_head.data['contribution']['id']),
            token=self.contrib_token,
        )
        self.assertEqual(res.status, 503, res.data)
        self.assertEqual(res.data['error']['message'], rw.DELIVERY_CHECKSUM_MISMATCH)
        self.assertNotIn(b'# v2 git bundle', res.body)

    def test_list_heads_timeout_503(self):
        """15. git bundle list-heads timeout answers 503 with zero bundle bytes sent."""
        def timeout_git(args):
            raise subprocess.TimeoutExpired(cmd=['git', *args], timeout=rw.BUNDLE_GIT_TIMEOUT)

        self.backend.bundle_git = timeout_git
        try:
            res = self._req(
                'GET',
                '/v1/projects/%s/tasks/%s/delivery/bundle' % (self.alpha_id, self.task_id),
                token=self.contrib_token,
            )
            self.assertEqual(res.status, 503, res.data)
            self.assertEqual(res.data['error']['message'], rw.DELIVERY_CHECKSUM_MISMATCH)
            self.assertNotIn(b'# v2 git bundle', res.body)
        finally:
            self.backend.bundle_git = None

    def test_an_audit_line_exists_for_a_download(self):
        """16. D1: an http_auth.audit line exists for a bundle download and for a post-auth refusal."""
        ok_res = self._req(
            'GET',
            '/v1/projects/%s/tasks/%s/delivery/bundle' % (self.alpha_id, self.task_id),
            token=self.contrib_token,
        )
        self.assertEqual(ok_res.status, 200)

        self.bundle_path.unlink()
        refused_res = self._req(
            'GET',
            '/v1/projects/%s/tasks/%s/delivery/bundle' % (self.alpha_id, self.task_id),
            token=self.contrib_token,
        )
        self.assertEqual(refused_res.status, 410)

        audit_res = self._req('GET', '/v1/projects/%s/audit' % self.alpha_id, token=self.owner_token)
        self.assertEqual(audit_res.status, 200, audit_res.data)
        bundle_events = [
            e for e in audit_res.data['items'] if e.get('action') == 'delivery.bundle.read'
        ]
        self.assertEqual(len(bundle_events), 2, bundle_events)
        outcomes = {e['outcome']: e for e in bundle_events}
        self.assertIn('ok', outcomes)
        self.assertEqual(outcomes['ok']['task'], self.task_id)
        self.assertEqual(outcomes['ok']['contribution'], self.contribution_id)
        self.assertEqual(outcomes['ok']['sha256'], self.bundle_sha256)
        self.assertEqual(outcomes['ok']['bytes'], len(self.fixture['bundle1_bytes']))
        self.assertIn('refused', outcomes)
        self.assertEqual(outcomes['refused']['task'], self.task_id)
        self.assertEqual(outcomes['refused']['contribution'], self.contribution_id)
        self.assertEqual(outcomes['refused']['sha256'], self.bundle_sha256)

    def test_client_refuses_and_deletes_a_body_whose_digest_differs(self):
        """17. client refuses and deletes a body whose digest differs from metadata sha256/ETag."""
        expected_sha = self.bundle_sha256
        corrupted_body = b'# v2 git bundle\ncorrupted-bytes\n'
        meta_payload = {
            'task': self.task_id,
            'project': self.alpha_id,
            'contribution': self.contribution_id,
            'revision': 1,
            'kind': 'bundle',
            'commit': self.commit,
            'base_commit': self.base_commit,
            'sha256': expected_sha,
            'bytes': len(corrupted_body),
            'path': str(self.bundle_path),
            'serve_limit': rw.DELIVERY_SERVE_MAX_BYTES,
            'delivery': {'kind': 'bundle', 'path': str(self.bundle_path), 'sha256': expected_sha},
        }

        class TamperingHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path.endswith('/delivery'):
                    raw = json.dumps(meta_payload).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Length', str(len(corrupted_body)))
                    self.send_header('ETag', expected_sha)
                    self.send_header('X-Contribution', meta_payload['contribution'])
                    self.end_headers()
                    self.wfile.write(corrupted_body)

        srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), TamperingHandler)
        t = threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        t.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = 'http://127.0.0.1:%d' % srv.server_address[1]

        out_dir = self.tmp_path / 'client-out-17'
        out_dir.mkdir()
        out_file = out_dir / 'fetched.bundle'

        hc = http_client.Client(url)
        with self.assertRaisesRegex(ValueError, 'does not match its recorded checksum'):
            hc.delivery_bundle(self.alpha_id, self.task_id, str(out_file))
        self.assertFalse(out_file.exists())
        self.assertEqual(list(out_dir.iterdir()), [])

        # Also verify via client.py CLI `delivery TASK --out FILE`
        cfg_path = self.tmp_path / 'http-client-17.json'
        cfg_path.write_text(json.dumps({'transport': 'http', 'base_url': url, 'token': 'tok'}), encoding='utf-8')
        stderr_buf = io.StringIO()
        with mock.patch.object(
            sys,
            'argv',
            [
                'client.py',
                '--config',
                str(cfg_path),
                '--project',
                self.alpha_id,
                '--actor',
                'session-1',
                '--',
                'delivery',
                self.task_id,
                '--out',
                str(out_file),
            ],
        ), mock.patch('sys.stderr', stderr_buf):
            rc = cli_client.main()
        self.assertEqual(rc, 2)
        self.assertIn(rw.DELIVERY_CHECKSUM_MISMATCH, stderr_buf.getvalue())
        self.assertFalse(out_file.exists())
        self.assertEqual(list(out_dir.iterdir()), [])

    def test_client_never_writes_a_partial_out_file(self):
        """18. client never writes a partial --out file on mid-stream truncation or error."""
        full_body = self.fixture['bundle1_bytes']
        half_body = full_body[: len(full_body) // 2]
        meta_payload = {
            'task': self.task_id,
            'project': self.alpha_id,
            'contribution': self.contribution_id,
            'revision': 1,
            'kind': 'bundle',
            'commit': self.commit,
            'base_commit': self.base_commit,
            'sha256': self.bundle_sha256,
            'bytes': len(full_body),
            'path': str(self.bundle_path),
            'serve_limit': rw.DELIVERY_SERVE_MAX_BYTES,
            'delivery': {'kind': 'bundle', 'path': str(self.bundle_path), 'sha256': self.bundle_sha256},
        }

        class TruncatingHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path.endswith('/delivery'):
                    raw = json.dumps(meta_payload).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Length', str(len(full_body)))
                    self.send_header('ETag', meta_payload['sha256'])
                    self.send_header('X-Contribution', meta_payload['contribution'])
                    self.end_headers()
                    self.wfile.write(half_body)

        srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), TruncatingHandler)
        t = threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        t.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = 'http://127.0.0.1:%d' % srv.server_address[1]

        out_dir = self.tmp_path / 'client-out-18'
        out_dir.mkdir()
        out_file = out_dir / 'partial.bundle'

        hc = http_client.Client(url)
        with self.assertRaisesRegex(ValueError, 'does not match its recorded checksum'):
            hc.delivery_bundle(self.alpha_id, self.task_id, str(out_file))
        self.assertFalse(out_file.exists())
        self.assertEqual(list(out_dir.iterdir()), [])

        # And a valid download via client.py succeeds and writes the exact bundle bytes
        valid_out = out_dir / 'valid.bundle'
        cfg_path = self.tmp_path / 'http-client-valid.json'
        cfg_path.write_text(
            json.dumps({'transport': 'http', 'base_url': self.base_url, 'token': self.contrib_token}),
            encoding='utf-8',
        )
        stdout_buf = io.StringIO()
        with mock.patch.object(
            sys,
            'argv',
            [
                'client.py',
                '--config',
                str(cfg_path),
                '--project',
                self.alpha_id,
                '--actor',
                'session-1',
                '--',
                'delivery',
                self.task_id,
                '--out',
                str(valid_out),
            ],
        ), mock.patch('sys.stdout', stdout_buf):
            rc = cli_client.main()
        self.assertEqual(rc, 0)
        self.assertTrue(valid_out.is_file())
        self.assertEqual(valid_out.read_bytes(), self.fixture['bundle1_bytes'])
        parsed = json.loads(stdout_buf.getvalue())
        self.assertTrue(parsed['verified'])
        self.assertEqual(parsed['sha256'], self.bundle_sha256)

    # -- Coordinator precisions & deployment settings -------------------------

    def test_ssh_form_answers_fixed_sentence_and_reads_nothing_from_disk(self):
        """Precision 4: endpoint.py and client.py on SSH/local transport answer the fixed sentence."""
        cfg_path = self.tmp_path / 'ssh-client.json'
        cfg_path.write_text(
            json.dumps({'transport': 'ssh', 'host': 'localhost', 'endpoint': '/bin/true', 'root': '/tmp'}),
            encoding='utf-8',
        )
        out_file = self.tmp_path / 'never-created.bundle'
        stderr_buf = io.StringIO()
        with mock.patch.object(
            sys,
            'argv',
            [
                'client.py',
                '--config',
                str(cfg_path),
                '--project',
                self.alpha_id,
                '--actor',
                'session-1',
                '--',
                'delivery',
                self.task_id,
                '--out',
                str(out_file),
            ],
        ), mock.patch('sys.stderr', stderr_buf):
            rc = cli_client.main()
        self.assertEqual(rc, 2)
        self.assertEqual(stderr_buf.getvalue().strip(), 'ValueError: ' + rw.DELIVERY_SSH_ONLY_HTTP)
        self.assertFalse(out_file.exists())

        if endpoint is not None:
            non_existent_root = self.tmp_path / 'does-not-even-exist'
            with self.assertRaises(ValueError) as ctx:
                endpoint.execute(
                    non_existent_root,
                    {'project': 'alpha', 'actor': 'session-1', 'action': 'delivery', 'args': [self.task_id]},
                )
            self.assertEqual(str(ctx.exception), rw.DELIVERY_SSH_ONLY_HTTP)
            self.assertFalse(non_existent_root.exists())

    @unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
    def test_endpoint_delivery_record_takes_no_lock_and_writes_no_files(self):
        """Precision 3: endpoint delivery-record takes no lock and writes nothing to disk."""
        rt = self.tmp_path / 'ep-root'
        proj = rt / 'projects' / 'alpha'
        (proj / '.beads').mkdir(parents=True, exist_ok=True)
        (proj / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        row = {
            'id': 'alpha-1',
            'title': 'Task',
            'issue_type': 'task',
            'status': 'in_progress',
            'labels': [],
            'comments': [
                {
                    'id': 'c-1',
                    'author': 'session-1',
                    'created_at': '2026-10-22T00:00:00Z',
                    'text': rw.PREFIX
                    + json.dumps(
                        {
                            'schema_version': 1,
                            'operation': 'contribute',
                            'operation_id': 'op-1',
                            'task': 'alpha-1',
                            'previous': None,
                            'repository': 'https://example.invalid/repo.git',
                            'commit': self.commit,
                            'base_commit': self.base_commit,
                            'delivery': {
                                'kind': 'bundle',
                                'path': str(self.bundle_path),
                                'sha256': self.bundle_sha256,
                            },
                            'summary': 'Deliver',
                            'supersedes': None,
                        },
                        sort_keys=True,
                        separators=(',', ':'),
                    ),
                }
            ],
        }
        before = {str(p): p.read_bytes() for p in rt.rglob('*') if p.is_file()}
        with mock.patch('native.run', return_value=subprocess.CompletedProcess([], 0, json.dumps([row]), '')), \
                mock.patch.object(endpoint, 'configured_operators', return_value=set()), \
                mock.patch.object(endpoint, 'environment', return_value={}):
            with self.assertRaisesRegex(ValueError, 'delivery-record is available only to the web service'):
                endpoint.execute(
                    rt,
                    {'project': 'alpha', 'actor': 'http/read', 'action': 'delivery-record', 'args': ['alpha-1']},
                )
            reply = endpoint.execute(
                rt,
                {'project': 'alpha', 'actor': 'http/read', 'action': 'delivery-record', 'args': ['alpha-1']},
                authority_config={'store': str(rt / 'auth.json'), 'lock': str(rt / 'auth.json.lock'), 'require': False},
            )
        self.assertEqual(reply['returncode'], 0, reply)
        after = {str(p): p.read_bytes() for p in rt.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((proj / '.coordination.lock').exists())
        self.assertFalse((rt / 'auth.json.lock').exists())
        payload = json.loads(reply['stdout'])
        self.assertTrue(payload['found'])
        self.assertEqual(payload['project'], 'alpha')
        self.assertEqual(payload['task'], 'alpha-1')
        self.assertEqual(len(payload['contributions']), 1)
        self.assertEqual(payload['contributions'][0]['contribution'], 'c-1')

        # Also verify EndpointBackend.task_delivery_record and settings readers
        from http_service import EndpointBackend
        (rt / 'deployment.private.json').write_text(
            json.dumps({'delivery_roots': [str(self.drop_root)], 'delivery_serve_max_bytes': 1234567}),
            encoding='utf-8',
        )
        with mock.patch('native.run', return_value=subprocess.CompletedProcess([], 0, json.dumps([row]), '')), \
                mock.patch.object(endpoint, 'configured_operators', return_value=set()), \
                mock.patch.object(endpoint, 'environment', return_value={}), \
                mock.patch('http_service.project_unusable', return_value=None):
            ep_backend = EndpointBackend(
                sys.executable,
                str(ROOT / 'endpoint.py'),
                str(rt),
                service=self.service,
                runner=lambda action, project, actor, args, **kw: endpoint.execute(
                    rt,
                    {'project': project, 'actor': actor, 'action': action, 'args': args},
                    authority_config={'store': str(rt / 'auth.json'), 'lock': str(rt / 'auth.json.lock'), 'require': False},
                ),
            )
            self.assertEqual(ep_backend.get_delivery_roots(), (str(self.drop_root),))
            self.assertEqual(ep_backend.get_delivery_serve_max_bytes(), 1234567)
            rec = ep_backend.task_delivery_record('alpha', 'alpha-1')
            self.assertEqual(rec['contribution'], 'c-1')
            self.assertEqual(rec['commit'], self.commit)

    def test_deployment_settings_delivery_roots_and_serve_max_bytes(self):
        """admin.py delivery_roots and delivery_serve_max_bytes validate and warn on bad values."""
        rt = self.tmp_path / 'admin-root'
        rt.mkdir()
        self.assertEqual(admin.delivery_roots(rt), ())
        self.assertEqual(admin.delivery_serve_max_bytes(rt), 256 * 1024 * 1024)

        (rt / 'deployment.private.json').write_text(
            json.dumps(
                {
                    'delivery_roots': ['/srv/orchestra-drop', 'D:\\srv\\orchestra-drop', 'D:/srv/other-drop/'],
                    'delivery_serve_max_bytes': 1024 * 1024,
                }
            ),
            encoding='utf-8',
        )
        self.assertEqual(
            admin.delivery_roots(rt),
            ('/srv/orchestra-drop', 'D:\\srv\\orchestra-drop', 'D:\\srv\\other-drop'),
        )
        self.assertEqual(admin.delivery_serve_max_bytes(rt), 1024 * 1024)

        warnings = []
        (rt / 'deployment.private.json').write_text(
            json.dumps({'delivery_roots': ['relative/path'], 'delivery_serve_max_bytes': -5}),
            encoding='utf-8',
        )
        self.assertEqual(admin.delivery_roots(rt, warnings=warnings), ())
        self.assertEqual(admin.delivery_serve_max_bytes(rt, warnings=warnings), 256 * 1024 * 1024)
        self.assertEqual(len(warnings), 2)

        for bad_root in ('C:relative', 'D:\\', 'D:/', '/'):
            w_bad = []
            (rt / 'deployment.private.json').write_text(
                json.dumps({'delivery_roots': [bad_root]}),
                encoding='utf-8',
            )
            self.assertEqual(admin.delivery_roots(rt, warnings=w_bad), (), bad_root)
            self.assertEqual(len(w_bad), 1, bad_root)

    def test_windows_drive_path_under_root_served(self):
        """Cross-platform: a Windows drive path under a root is kept intact and served (200)."""
        # Verify single-letter drive prefix is never stripped as a host name
        self.assertEqual(
            rw.strip_delivery_host_prefix('D:\\a\\orchestra\\shared-drop\\lane\\b.bundle'),
            'D:\\a\\orchestra\\shared-drop\\lane\\b.bundle',
        )
        self.assertEqual(
            rw.strip_delivery_host_prefix('D:/a/orchestra/shared-drop/lane/b.bundle'),
            'D:/a/orchestra/shared-drop/lane/b.bundle',
        )

        drv = os.path.splitdrive(str(self.drop_root))[0] or 'D:'
        root_tail = os.path.splitdrive(str(self.drop_root))[1].replace('/', '\\').lstrip('\\')
        win_root = drv.upper() + '\\' + root_tail
        win_bundle_backslash = drv.upper() + '\\' + root_tail + '\\lane\\alpha-1-r1.bundle'
        # Mixed drive case, directory case, and forward slashes to exercise normcase + separator folding
        win_bundle_mixed = drv.lower() + '/' + root_tail.replace('\\', '/') + '/LANE/alpha-1-r1.bundle'
        wrong_drive = ('E:' if drv.upper() != 'E:' else 'F:') + '\\' + root_tail + '\\lane\\alpha-1-r1.bundle'

        self.backend.delivery_roots = (win_root,)
        prev = self.contribution_id
        for idx, candidate_path in enumerate((win_bundle_backslash, win_bundle_mixed), start=1):
            c_win = self._contribute_bundle(
                self.alpha_id,
                self.task_id,
                self.contrib_token,
                commit=self.commit,
                base_commit=self.base_commit,
                path=candidate_path,
                sha256=self.bundle_sha256,
                operation_id='alpha-t1-win-drive-%d' % idx,
                previous=prev,
            )
            prev = c_win.data['contribution']['id']
            meta = self._req(
                'GET',
                '/v1/projects/%s/tasks/%s/delivery?contribution=%s' % (self.alpha_id, self.task_id, prev),
                token=self.contrib_token,
            )
            self.assertEqual(meta.status, 200, (candidate_path, meta.data))
            self.assertEqual(meta.data['bytes'], len(self.fixture['bundle1_bytes']))
            bundle_res = self._req(
                'GET',
                '/v1/projects/%s/tasks/%s/delivery/bundle?contribution=%s' % (self.alpha_id, self.task_id, prev),
                token=self.contrib_token,
            )
            self.assertEqual(bundle_res.status, 200, (candidate_path, bundle_res.data))
            self.assertEqual(bundle_res.body, self.fixture['bundle1_bytes'])

        c_wrong_drv = self._contribute_bundle(
            self.alpha_id,
            self.task_id,
            self.contrib_token,
            commit=self.commit,
            base_commit=self.base_commit,
            path=wrong_drive,
            sha256=self.bundle_sha256,
            operation_id='alpha-t1-win-wrong-drive',
            previous=prev,
        )
        res_410 = self._req(
            'GET',
            '/v1/projects/%s/tasks/%s/delivery/bundle?contribution=%s'
            % (self.alpha_id, self.task_id, c_wrong_drv.data['contribution']['id']),
            token=self.contrib_token,
        )
        self.assertEqual(res_410.status, 410, res_410.data)
        self.assertEqual(res_410.data['error']['message'], rw.DELIVERY_NOT_AVAILABLE)

    def test_host_prefix_posix_srv_path_stripped_and_served(self):
        """Cross-platform: a HOST:/srv/... path has its prefix stripped and is served under a root."""
        self.assertEqual(
            rw.strip_delivery_host_prefix('goomba:/srv/orchestra-drop/lane/alpha-1-r1.bundle'),
            '/srv/orchestra-drop/lane/alpha-1-r1.bundle',
        )
        srv_root = self.tmp_path / 'srv' / 'orchestra-drop'
        srv_lane = srv_root / 'lane'
        srv_lane.mkdir(parents=True, exist_ok=True)
        srv_bundle = srv_lane / 'alpha-1-r1.bundle'
        srv_bundle.write_bytes(self.fixture['bundle1_bytes'])

        posix_srv_root = os.path.splitdrive(str(srv_root))[1].replace('\\', '/')
        posix_srv_bundle = os.path.splitdrive(str(srv_bundle))[1].replace('\\', '/')
        recorded = 'goomba:' + posix_srv_bundle
        self.assertEqual(rw.strip_delivery_host_prefix(recorded), posix_srv_bundle)

        self.backend.delivery_roots = (posix_srv_root,)
        c_srv = self._contribute_bundle(
            self.alpha_id,
            self.task_id,
            self.contrib_token,
            commit=self.commit,
            base_commit=self.base_commit,
            path=recorded,
            sha256=self.bundle_sha256,
            operation_id='alpha-t1-host-srv',
            previous=self.contribution_id,
        )
        cid = c_srv.data['contribution']['id']
        meta = self._req(
            'GET',
            '/v1/projects/%s/tasks/%s/delivery?contribution=%s' % (self.alpha_id, self.task_id, cid),
            token=self.contrib_token,
        )
        self.assertEqual(meta.status, 200, meta.data)
        self.assertEqual(meta.data['bytes'], len(self.fixture['bundle1_bytes']))
        res = self._req(
            'GET',
            '/v1/projects/%s/tasks/%s/delivery/bundle?contribution=%s' % (self.alpha_id, self.task_id, cid),
            token=self.contrib_token,
        )
        self.assertEqual(res.status, 200, res.data)
        self.assertEqual(res.body, self.fixture['bundle1_bytes'])

    def test_windows_drive_relative_path_refused_410(self):
        """Cross-platform: 'C:relative' and other drive-relative or host+drive paths are refused (410)."""
        for bad in (
            'C:relative',
            'C:relative/alpha-1-r1.bundle',
            'C:relative\\alpha-1-r1.bundle',
            'c:lane/alpha-1-r1.bundle',
            'goomba:C:relative',
            'goomba:D:\\srv\\drop\\b.bundle',
        ):
            self.assertIsNone(rw.strip_delivery_host_prefix(bad), bad)
            with self.assertRaises(rw.DeliveryUnavailableError):
                rw.resolve_delivery_bundle_path(bad, (str(self.drop_root),))

        c_rel = self._contribute_bundle(
            self.alpha_id,
            self.task_id,
            self.contrib_token,
            commit=self.commit,
            base_commit=self.base_commit,
            path='C:relative',
            sha256=self.bundle_sha256,
            operation_id='alpha-t1-c-relative',
            previous=self.contribution_id,
        )
        cid = c_rel.data['contribution']['id']
        meta = self._req(
            'GET',
            '/v1/projects/%s/tasks/%s/delivery?contribution=%s' % (self.alpha_id, self.task_id, cid),
            token=self.contrib_token,
        )
        self.assertEqual(meta.status, 200, meta.data)
        self.assertIsNone(meta.data['bytes'])
        res = self._req(
            'GET',
            '/v1/projects/%s/tasks/%s/delivery/bundle?contribution=%s' % (self.alpha_id, self.task_id, cid),
            token=self.contrib_token,
        )
        self.assertEqual(res.status, 410, res.data)
        self.assertEqual(res.data['error']['message'], rw.DELIVERY_NOT_AVAILABLE)


sys.path.insert(0, str(ROOT / 'tests'))
import test_bd_label_aliases as rb
import test_claim_held as held
import test_coordinator_grant_real as native_real


@unittest.skipIf(os.name == 'nt', 'endpoint.py imports fcntl; POSIX only')
@unittest.skipIf(rb.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(rb.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
@native_real.only_its_own_tests
class RealBdHttpDeliveryTests(held.RealStackTests):
    """Real web service, real endpoint.py, and real bd/dolt server for HTTP delivery routes."""

    def test_real_bd_delivery_metadata_and_bundle_stream_and_client_fetch(self):
        drop_dir = Path(self.tmp) / 'real-drop'
        lane_dir = drop_dir / 'casey'
        lane_dir.mkdir(parents=True, exist_ok=True)
        fixture = build_git_bundle_fixture(Path(self.tmp) / 'real-git')
        bundle_file = lane_dir / 'pp-1-r1.bundle'
        bundle_file.write_bytes(fixture['bundle1_bytes'])

        deploy_path = self.root / 'deployment.private.json'
        deploy_doc = json.loads(deploy_path.read_text(encoding='utf-8'))
        deploy_doc['delivery_roots'] = [str(drop_dir)]
        deploy_path.write_text(json.dumps(deploy_doc), encoding='utf-8')

        task_id = self.new('real-bd delivery task')
        claimed = self.claim('casey', task_id)
        self.assertEqual(claimed.status, 200, claimed.data)

        contributed = self.request(
            'POST',
            '/v1/projects/pp/tasks/%s/reviews' % task_id,
            {
                'schema_version': 1,
                'operation': 'contribute',
                'operation_id': 'real-bd-r1',
                'previous': None,
                'repository': 'https://example.invalid/repo.git',
                'commit': fixture['head1_commit'],
                'base_commit': fixture['base_commit'],
                'delivery': {
                    'kind': 'bundle',
                    'path': 'otherhost:%s' % bundle_file,
                    'sha256': fixture['bundle1_sha256'],
                },
                'summary': 'Deliver real-bd bundle',
                'supersedes': None,
            },
            token=self.tokens['casey'],
        )
        self.assertEqual(contributed.status, 201, contributed.data)
        cid = contributed.data.get('comment_id') or (contributed.data.get('contribution') or {}).get('id')
        self.assertTrue(cid, contributed.data)
        coord_lock = self.root / 'projects' / 'pp' / '.coordination.lock'
        if coord_lock.exists():
            coord_lock.unlink()

        meta = self.request('GET', '/v1/projects/pp/tasks/%s/delivery' % task_id, token=self.tokens['casey'])
        self.assertEqual(meta.status, 200, meta.data)
        self.assertEqual(meta.data['contribution'], cid)
        self.assertEqual(meta.data['sha256'], fixture['bundle1_sha256'])
        self.assertEqual(meta.data['bytes'], len(fixture['bundle1_bytes']))

        out_bundle = Path(self.tmp) / 'fetched-real.bundle'
        base_url = 'http://127.0.0.1:%d' % self.httpd.server_address[1]
        hc = http_client.Client(base_url, allow_plaintext=True).use_credential(self.tokens['casey'])
        downloaded = hc.delivery_bundle('pp', task_id, str(out_bundle))
        self.assertTrue(downloaded['verified'])
        self.assertEqual(out_bundle.read_bytes(), fixture['bundle1_bytes'])
        self.assertFalse(coord_lock.exists())


if __name__ == '__main__':
    unittest.main()
