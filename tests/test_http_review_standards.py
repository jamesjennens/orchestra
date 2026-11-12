"""HTTP both-backends (InProcessBackend + EndpointBackend) and Node browser UI tests for Phase 1 Review Standards (kittrial-5bb.570)."""
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import http_auth
import http_authority
import http_service
import review_standards as rs
import review_workflow as rw
from http_authority import AuthorityConfig
import test_http_review_fixes
from test_http_review_fixes import Harness
from test_http_web import WEB, run_node_module
from test_lane_nudge_carrier import fill

_ORIG_HASH_PASSWORD = http_auth.hash_password
_ORIG_TMP_ROOT = test_http_review_fixes.TMP_ROOT
_MOD_TMP = None


def setUpModule():
    global _MOD_TMP
    http_auth.hash_password = lambda pw, **kw: _ORIG_HASH_PASSWORD(
        pw, n=16, r=8, p=1, minimum=kw.get('minimum', http_auth.MIN_PASSWORD)
    )
    http_auth._DUMMY_VERIFIER = http_auth.hash_password('orchestra-dummy-password')
    if 'ORCHESTRA_TEST_TMP' not in os.environ:
        _MOD_TMP = tempfile.TemporaryDirectory()
        test_http_review_fixes.TMP_ROOT = Path(_MOD_TMP.name)


def tearDownModule():
    global _MOD_TMP
    http_auth.hash_password = _ORIG_HASH_PASSWORD
    http_auth._DUMMY_VERIFIER = None
    test_http_review_fixes.TMP_ROOT = _ORIG_TMP_ROOT
    if _MOD_TMP is not None:
        _MOD_TMP.cleanup()
        _MOD_TMP = None

try:
    import endpoint
except ImportError:
    endpoint = None

POSIX = unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')


def _git(cwd, *args):
    proc = subprocess.run(
        [
            'git',
            '-c', 'user.name=Test User',
            '-c', 'user.email=test@example.invalid',
            '-c', 'commit.gpgsign=false',
            '-c', 'init.defaultBranch=main',
            *args,
        ],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError('git %s failed: %s' % (args, proc.stderr or proc.stdout))
    return proc.stdout.strip()


def _make_bundle(repo_dir, out_path, ref='HEAD'):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, 'bundle', 'create', str(out_path), ref)
    sha256 = hashlib.sha256(out_path.read_bytes()).hexdigest()
    commit = _git(repo_dir, 'rev-parse', ref)
    return commit, sha256


def _rule(
    rule_id='STD-1',
    owner='usr_0000000000000001',
    severity='must',
    statement='Validate bounds on slices.',
    rationale='Unbounded slices exhaust memory.',
    applicability=None,
    check=None,
    references=None,
):
    r = {
        'id': rule_id,
        'statement': statement,
        'rationale': rationale,
        'owner': owner,
        'severity': severity,
        'applicability': applicability if applicability is not None else {'all_changes': True},
        'check': check if check is not None else {'kind': 'judgement'},
    }
    if references is not None:
        r['references'] = list(references)
    return r


class _Completed:
    def __init__(self, stdout='', returncode=0, stderr=''):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class EndpointProjectTracker:
    """In-memory bd tracker driving real endpoint.py behind EndpointBackend."""

    def __init__(self, project_id):
        self.project_id = project_id
        self.rows = {
            '%s-merge-slot' % project_id: {
                'id': '%s-merge-slot' % project_id,
                'project_id': project_id,
                'title': 'Merge slot',
                'description': '',
                'issue_type': 'merge-slot',
                'status': 'open',
                'priority': 2,
                'assignee': None,
                'labels': ['gt:slot'],
                'comments': [],
                'dependencies': [],
                'created_by': 'system',
                'created_at': '2026-11-11T00:00:00Z',
                'updated_at': '2026-11-11T00:00:00Z',
            },
        }
        self.seq = 0
        self.cseq = 0

    def run_argv(self, argv):
        cmd = list(argv)
        actor = 'system'
        if cmd[:1] == ['--actor']:
            actor = cmd[1]
            cmd = cmd[2:]
        if cmd[:1] == ['create']:
            title = cmd[1]
            desc = ''
            priority = 2
            idx = 2
            while idx < len(cmd):
                if cmd[idx] == '--priority' and idx + 1 < len(cmd):
                    priority = int(cmd[idx + 1])
                    idx += 2
                elif cmd[idx] == '--body-file' and idx + 1 < len(cmd):
                    desc = Path(cmd[idx + 1]).read_text(encoding='utf-8')
                    idx += 2
                else:
                    idx += 1
            self.seq += 1
            tid = '%s-%d' % (self.project_id, self.seq)
            row = {
                'id': tid,
                'project_id': self.project_id,
                'title': title,
                'description': desc,
                'issue_type': 'task',
                'status': 'open',
                'priority': priority,
                'assignee': None,
                'labels': [],
                'comments': [],
                'dependencies': [],
                'created_by': actor,
                'created_at': '2026-11-11T10:00:00Z',
                'updated_at': '2026-11-11T10:00:00Z',
            }
            self.rows[tid] = row
            return json.dumps(row)
        if cmd[:2] == ['export', '--all']:
            if not self.rows:
                return ''
            return '\n'.join(json.dumps(r) for r in self.rows.values()) + '\n'
        if cmd[:1] == ['list']:
            return json.dumps(list(self.rows.values()))
        if cmd[:1] == ['show']:
            ids = [t for t in cmd[1:] if not t.startswith('--')]
            found = [self.rows[i] for i in ids if i in self.rows]
            if not found:
                raise ValueError('task not found: %s' % ids)
            if '--include-comments' in cmd or len(ids) > 1:
                return json.dumps(found)
            return json.dumps(found[0])
        if cmd[:1] == ['comments'] and cmd[1:2] != ['add']:
            tid = cmd[1]
            if tid not in self.rows:
                raise ValueError('task not found: %s' % tid)
            return json.dumps(self.rows[tid]['comments'])
        if cmd[:2] == ['comments', 'add']:
            tid = cmd[2]
            if tid not in self.rows:
                raise ValueError('task not found: %s' % tid)
            if cmd[3] == '--file':
                text = Path(cmd[4]).read_text(encoding='utf-8')
            else:
                text = cmd[3]
            self.cseq += 1
            cid = str(self.cseq)
            ts = '2026-11-11T12:%02d:%02dZ' % ((self.cseq // 60) % 60, self.cseq % 60)
            self.rows[tid]['comments'].append({
                'id': cid,
                'author': actor,
                'created_at': ts,
                'text': text,
            })
            return json.dumps({'id': cid})
        if cmd[:1] == ['update']:
            tid = cmd[1]
            if tid not in self.rows:
                raise ValueError('task not found: %s' % tid)
            row = self.rows[tid]
            idx = 2
            while idx < len(cmd):
                flag = cmd[idx]
                if flag == '--claim':
                    if row['status'] != 'open' or row.get('assignee'):
                        raise ValueError('already claimed or not open')
                    row['status'] = 'in_progress'
                    row['assignee'] = actor
                    idx += 1
                elif flag in ('--status', '--assignee', '--title', '--description'):
                    row[flag[2:]] = cmd[idx + 1]
                    idx += 2
                elif flag == '--body-file':
                    row['description'] = Path(cmd[idx + 1]).read_text(encoding='utf-8')
                    idx += 2
                elif flag == '--priority':
                    row['priority'] = int(cmd[idx + 1])
                    idx += 2
                elif flag == '--add-label':
                    lbl = cmd[idx + 1]
                    if lbl not in row['labels']:
                        row['labels'].append(lbl)
                    idx += 2
                elif flag == '--remove-label':
                    lbl = cmd[idx + 1]
                    row['labels'] = [x for x in row['labels'] if x != lbl]
                    idx += 2
                else:
                    idx += 1
            return json.dumps(row)
        return '{}'


class FastRealEndpointBackend(http_service.EndpointBackend):
    """EndpointBackend driving real endpoint.execute + AuthorityConfig + real git without Dolt startup."""

    def __init__(self, root, *, service, trackers):
        super().__init__(sys.executable, str(ROOT / 'endpoint.py'), str(root), service=service)
        self.trackers = trackers

    def _ask(self, action, project, actor, args, attachments, operation_id, authority,
             require_authority, route, check_usable, timeout, extra=None):
        if isinstance(project, str):
            verdict = http_service.project_unusable(self.service, project) if check_usable else (
                None if http_service.CANONICAL_PROJECT.fullmatch(project) else ('no-canonical', http_service.NO_CANONICAL)
            )
            if verdict is not None:
                raise http_service.conflict(verdict[1])
        store = self.service.store
        if authority and store.unsaved_since is not None:
            store.save()
        payload = {
            'project': project,
            'actor': actor,
            'action': action,
            'args': list(args or []),
            'attachments': attachments or {},
        }
        if extra:
            payload.update({k: v for k, v in extra.items() if v is not None})
        if operation_id:
            payload['operation_id'] = operation_id
        if authority:
            payload['authority'] = authority
        if route and operation_id:
            payload['route'] = route

        auth_cfg = AuthorityConfig(
            str(self.authority_store),
            str(self.authority_lock),
            self.actor_namespace,
            self.timeout,
        )

        real_sub_run = subprocess.run

        def _native_run(command, env=None, **kw):
            target_proj = project
            if target_proj is None and '--directory' in command:
                target_proj = Path(str(command[command.index('--directory') + 1])).name
            tr = self.trackers.get(target_proj)
            if tr is None:
                return _Completed(stdout='', returncode=1, stderr='unknown project')
            if '--sandbox' in command:
                rest = list(command[command.index('--sandbox') + 1:])
            else:
                rest = list(command[1:])
            try:
                out = tr.run_argv(rest)
                return _Completed(stdout=out, returncode=0, stderr='')
            except Exception as exc:
                return _Completed(stdout='', returncode=1, stderr=str(exc))

        def _sub_run(command, *a, **kw):
            if isinstance(command, (list, tuple)) and command and Path(str(command[0])).name == 'bd':
                return _native_run(command)
            return real_sub_run(command, *a, **kw)

        with mock.patch.object(endpoint.native, 'run', side_effect=_native_run), \
             mock.patch.object(endpoint.subprocess, 'run', side_effect=_sub_run):
            try:
                return endpoint.execute(
                    Path(self.root),
                    payload,
                    authority_config=auth_cfg,
                    require_authority=bool(require_authority),
                )
            except Exception as exc:
                ans = {'returncode': 2, 'stdout': '', 'stderr': '%s: %s\n' % (type(exc).__name__, exc)}
                if isinstance(exc, rw.CoordinatorRuleRefusal):
                    ans['stderr'] = 'ValueError: %s\n' % exc
                    ans['refused'] = 'coordinator-rule'
                    if type(getattr(exc, 'status', None)) is int:
                        ans['standards_status'] = exc.status
                return ans


class _HttpStandardsMixin:
    """Shared HTTP acceptance scenarios executed identically on InProcessBackend and EndpointBackend."""

    is_endpoint_backend = False

    def create_person(self, username, role=None, project_id=None):
        pw = username + '-pw-123456'
        uid = self.create_account(self.admin_tok, username, pw)
        tok, _, _ = self.login(username, pw)
        if role and project_id:
            r = self.request('PUT', '/v1/projects/%s/members/%s' % (project_id, uid), {'role': role}, token=self.owner_tok)
            self.assertEqual(200, r.status, r.data)
        return uid, tok

    def create_agent(self, owner_tok, name, projects, *, coordinate_in=()):
        r = self.request('POST', '/v1/agents', {'name': name, 'projects': list(projects)}, token=owner_tok)
        self.assertEqual(201, r.status, r.data)
        aid = r.data['agent']['id']
        secret = r.data['credential']['secret']
        for pid in coordinate_in:
            gr = self.request('PUT', '/v1/projects/%s/agents/%s/coordinator' % (pid, aid), {}, token=self.owner_tok)
            self.assertIn(gr.status, (200, 201), gr.data)
        return aid, secret

    def new_project(self, name, slug):
        if self.is_endpoint_backend:
            pdir = self.canonical_root / 'projects' / slug / '.beads'
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / 'metadata.json').write_text(json.dumps({
                'dolt_mode': 'server',
                'dolt_server_host': '127.0.0.1',
                'dolt_server_port': 3307,
                'dolt_server_user': 'root',
                'dolt_database': slug,
            }), encoding='utf-8')
            self.trackers[slug] = EndpointProjectTracker(slug)
            r = self.request('POST', '/v1/projects', {'project_id': slug, 'name': name}, token=self.admin_tok)
            self.assertEqual(201, r.status, r.data)
            # Transfer/add self.owner_uid as owner so non-superuser owner tests work cleanly.
            m = self.request('PUT', '/v1/projects/%s/members/%s' % (slug, self.owner_uid), {'role': 'owner'}, token=self.admin_tok)
            self.assertEqual(200, m.status, m.data)
            return slug
        r = self.request('POST', '/v1/projects', {'name': name}, token=self.owner_tok)
        self.assertEqual(201, r.status, r.data)
        return r.data['id']

    def make_repo_and_bundle(self, files_dict, name='b1'):
        repo = self.tmp / ('repo-' + name + '-' + secrets.token_hex(3))
        repo.mkdir(parents=True)
        _git(repo, 'init')
        for rel, content in files_dict.items():
            full = repo / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding='utf-8')
        _git(repo, 'add', '.')
        _git(repo, 'commit', '-m', 'commit ' + name)
        bpath = self.delivery_root / ('%s-%s.bundle' % (name, secrets.token_hex(4)))
        commit, sha = _make_bundle(repo, bpath)
        return repo, commit, sha, bpath

    def deliver_task_bundle(self, pid, worker_tok, files_dict, *, title='Implement feature', declared_tool=None, declared_model=None):
        cr = self.request('POST', '/v1/projects/%s/tasks' % pid, {'title': title}, token=self.owner_tok)
        self.assertEqual(201, cr.status, cr.data)
        tid = cr.data['id']
        cl = self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (pid, tid), {}, token=worker_tok)
        self.assertEqual(200, cl.status, cl.data)
        _, commit, sha, bpath = self.make_repo_and_bundle(files_dict, name=tid)
        body = {
            'schema_version': 1,
            'operation': 'contribute',
            'operation_id': 'op-' + secrets.token_hex(6),
            'previous': None,
            'supersedes': None,
            'repository': 'https://example.invalid/repo.git',
            'commit': commit,
            'base_commit': commit,
            'delivery': {'kind': 'bundle', 'path': 'server:' + str(bpath), 'sha256': sha},
            'summary': 'Delivered bundle.',
        }
        if declared_tool:
            body['declared_tool'] = declared_tool
        if declared_model:
            body['declared_model'] = declared_model
        rv = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), body, token=worker_tok)
        self.assertEqual(201, rv.status, rv.data)
        tget = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (pid, tid), token=self.owner_tok)
        self.assertEqual(200, tget.status, tget.data)
        cid = tget.data['review']['contribution']['id']
        latest_id = tget.data['review']['latest_id']
        return tid, cid, latest_id, commit

    def test_register_crud_inheritance_owner_edit_coordinator_note_and_drafts_both_backends(self):
        pid = self.pid
        alice_uid, alice_tok = self.create_person('alice', 'contributor', pid)
        coord_uid, coord_tok = self.create_person('cora', 'coordinator', pid)
        _, worker_agent = self.create_agent(alice_tok, 'alice-bot', [pid])
        _, coord_agent = self.create_agent(coord_tok, 'cora-bot', [pid], coordinate_in=[pid])

        # Coordinator creates v1 with STD-1 owned by alice_uid.
        put1 = self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 0,
            'reason': 'Create v1.',
            'rules': [_rule('STD-1', owner=alice_uid, severity='should', statement='Initial rule.')],
        }, token=coord_tok)
        self.assertEqual(200, put1.status, put1.data)
        self.assertEqual(1, put1.data['version'])

        # Agent credentials (even coordinator-granted!) are refused on all person-only standards routes (#1, #7).
        r_agent_put = self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 1, 'reason': 'Agent put', 'rules': [],
        }, token=coord_agent)
        self.assertEqual(403, r_agent_put.status)
        self.assertEqual(rs.refusal_agent_register_edit(), r_agent_put.data['error']['message'])

        r_agent_gate = self.request('PUT', '/v1/projects/%s/standards/gate' % pid, {
            'mode': 'block', 'reason': 'Agent gate',
        }, token=coord_agent)
        self.assertEqual(403, r_agent_gate.status)
        self.assertEqual(rs.refusal_gate_switch_authority(), r_agent_gate.data['error']['message'])

        # Rule owner alice edits STD-1 directly -> v2 + coordinator member note!
        patch2 = self.request('PATCH', '/v1/projects/%s/standards/rules/STD-1' % pid, {
            'expected_version': 1,
            'reason': 'Promote STD-1 to must.',
            'rule': _rule('STD-1', owner=alice_uid, severity='must', statement='Updated by owner Alice.'),
        }, token=alice_tok)
        self.assertEqual(200, patch2.status, patch2.data)
        self.assertEqual(2, patch2.data['version'])

        # Stale expected_version returns 409 (#18).
        conflict_patch = self.request('PATCH', '/v1/projects/%s/standards/rules/STD-1' % pid, {
            'expected_version': 1,
            'reason': 'Stale.',
            'rule': _rule('STD-1', owner=alice_uid, severity='must'),
        }, token=alice_tok)
        self.assertEqual(409, conflict_patch.status)
        self.assertEqual(rs.refusal_version_conflict(pid, 1, 2), conflict_patch.data['error']['message'])

        # Coordinator reads member note notifying about STD-1 edit!
        notes = self.request('GET', '/v1/projects/%s/notes?box=to' % pid, token=coord_tok)
        self.assertEqual(200, notes.status, notes.data)
        self.assertTrue(any('STD-1' in json.dumps(n) for n in notes.data.get('items', [])), notes.data)

        # Agent proposes a draft; agent cannot decide it (#1); coordinator accepts -> v3!
        d_prop = self.request('POST', '/v1/projects/%s/standards/drafts' % pid, {
            'action': 'upsert',
            'reason': 'Bot spotted missing SQL parameter check.',
            'rule': _rule('SQL-1', owner=alice_uid, severity='should'),
        }, token=worker_agent)
        self.assertEqual(201, d_prop.status, d_prop.data)
        did = d_prop.data['id']

        d_agent_dec = self.request('POST', '/v1/projects/%s/standards/drafts/%s/decision' % (pid, did), {
            'decision': 'accept', 'expected_version': 2, 'reason': 'Self accept',
        }, token=coord_agent)
        self.assertEqual(403, d_agent_dec.status)
        self.assertEqual(rs.refusal_agent_register_edit(), d_agent_dec.data['error']['message'])

        d_acc = self.request('POST', '/v1/projects/%s/standards/drafts/%s/decision' % (pid, did), {
            'decision': 'accept', 'expected_version': 2, 'reason': 'Accepted SQL-1',
        }, token=coord_tok)
        self.assertEqual(200, d_acc.status, d_acc.data)
        self.assertEqual(3, d_acc.data['register']['version'])

        vers = self.request('GET', '/v1/projects/%s/standards/versions' % pid, token=alice_tok)
        self.assertEqual(200, vers.status)
        self.assertEqual([3, 2, 1], [v['version'] for v in vers.data['items']])

    def test_warn_and_block_gate_citations_ci_unverified_and_waivers_both_backends(self):
        pid = self.pid
        alice_uid, alice_tok = self.create_person('alice2', 'contributor', pid)
        bob_uid, bob_tok = self.create_person('bob2', 'contributor', pid)
        coord_uid, coord_tok = self.create_person('cora2', 'coordinator', pid)
        ver_cred = self.request('POST', '/v1/projects/%s/worker-credentials' % pid, {
            'scopes': ['verify'], 'label': 'CI Verifier',
        }, token=self.owner_tok)
        self.assertEqual(201, ver_cred.status, ver_cred.data)
        ver_tok = ver_cred.data['credential']['secret']

        self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 0,
            'reason': 'Judgement + mechanical must rules.',
            'rules': [
                _rule('JDG-1', owner=alice_uid, severity='must'),
                _rule('MECH-1', owner=alice_uid, severity='must', check={'kind': 'mechanical', 'tool': 'pytest', 'command': 'pytest -q'}),
            ],
        }, token=coord_tok)

        proj_get = self.request('GET', '/v1/projects/%s' % pid, token=coord_tok)
        self.assertEqual('warn', proj_get.data['standards_gate']['mode'])

        # 1) Warn mode approves without checks and records would_have_refused!
        tid1, cid1, prev1, _ = self.deliver_task_bundle(pid, bob_tok, {'src/calc.py': 'x = 1\ny = 2\n'}, title='Warn task')
        w_app = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid1), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-w1',
            'previous': prev1, 'contribution': cid1, 'summary': 'Warn mode ok.',
        }, token=coord_tok)
        self.assertEqual(201, w_app.status, w_app.data)
        so1 = self.request('GET', '/v1/projects/%s/tasks/%s/sign-off' % (pid, tid1), token=bob_tok)
        self.assertEqual(200, so1.status, so1.data)
        self.assertTrue(any('JDG-1' in w for w in so1.data['would_have_refused']))
        self.assertTrue(any('MECH-1' in w for w in so1.data['would_have_refused']))

        # 2) Switch gate to block, and verify a coordinator-granted agent credential CANNOT flip it back to warn (M9)!
        g_set = self.request('PUT', '/v1/projects/%s/standards/gate' % pid, {
            'mode': 'block', 'reason': 'Enforce standards.',
        }, token=coord_tok)
        self.assertEqual(200, g_set.status, g_set.data)

        _, coord_agent_tok = self.create_agent(coord_tok, 'cora2-agent', [pid], coordinate_in=[pid])
        g_agent_flip = self.request('PUT', '/v1/projects/%s/standards/gate' % pid, {
            'mode': 'warn', 'reason': 'Agent trying to weaken gate.',
        }, token=coord_agent_tok)
        self.assertEqual(403, g_agent_flip.status, g_agent_flip.data)
        self.assertEqual(rs.refusal_gate_switch_authority(), g_agent_flip.data['error']['message'])

        tid2, cid2, prev2, commit2 = self.deliver_task_bundle(pid, bob_tok, {'src/calc.py': 'x = 1\ny = 2\n'}, title='Block task')

        # Bad citation (line 99 on 2-line file) refused with 422 (#10)!
        bad_cit = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid2), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-badcit',
            'previous': prev2, 'contribution': cid2, 'summary': 'Bad line.',
            'checks': [{'rule_id': 'JDG-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['src/calc.py:99']}}],
        }, token=coord_tok)
        self.assertEqual(422, bad_cit.status, bad_cit.data)
        self.assertIn('out of range', bad_cit.data['error']['message'])

        # Valid JDG-1 citation, but MECH-1 is still unverified -> refused in block mode (#16)!
        unv_blk = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid2), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-unvblk',
            'previous': prev2, 'contribution': cid2, 'summary': 'Missing CI.',
            'checks': [{'rule_id': 'JDG-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['src/calc.py:2']}}],
        }, token=coord_tok)
        self.assertEqual(422, unv_blk.status, unv_blk.data)
        self.assertIn('MECH-1', unv_blk.data['error']['message'])

        # Author Bob cannot waive JDG-1 on his own delivery (#15 / #14)!
        bob_waive = self.request('POST', '/v1/projects/%s/tasks/%s/waivers' % (pid, tid2), {
            'contribution_id': cid2, 'rule_id': 'JDG-1', 'expiry': {'kind': 'delivery'}, 'reason': 'Self waive',
        }, token=bob_tok)
        self.assertEqual(403, bob_waive.status)

        # Verifier records CI exit_code=1 (fail) on MECH-1, and rule owner Alice waives MECH-1 for this delivery!
        rec_ci = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit2), {
            'changed_files': ['src/calc.py'],
            'ci_checks': [{'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}],
        }, token=ver_tok)
        self.assertEqual(201, rec_ci.status, rec_ci.data)

        w_grant = self.request('POST', '/v1/projects/%s/tasks/%s/waivers' % (pid, tid2), {
            'contribution_id': cid2, 'commit': commit2, 'rule_id': 'MECH-1',
            'expiry': {'kind': 'delivery'}, 'reason': 'Known flaky fixture in legacy suite.',
        }, token=alice_tok)
        self.assertEqual(201, w_grant.status, w_grant.data)

        ok_blk = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid2), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-okblk',
            'previous': prev2, 'contribution': cid2, 'summary': 'Approved with citation + MECH-1 waiver.',
            'checks': [{'rule_id': 'JDG-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['src/calc.py:1-2']}}],
        }, token=coord_tok)
        self.assertEqual(201, ok_blk.status, ok_blk.data)

    def test_cross_model_declared_fields_recorded_and_same_model_advised_not_refused(self):
        pid = self.pid
        alice_uid, _ = self.create_person('alice3', 'contributor', pid)
        _, bob_tok = self.create_person('bob3', 'contributor', pid)
        _, coord_tok = self.create_person('cora3', 'coordinator', pid)

        self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 0, 'reason': 'v1',
            'rules': [_rule('STD-1', owner=alice_uid, severity='must')],
        }, token=coord_tok)
        self.request('PUT', '/v1/projects/%s/standards/gate' % pid, {'mode': 'block', 'reason': 'block'}, token=coord_tok)

        # Same declared_model -> advised, NOT refused!
        tid1, cid1, prev1, _ = self.deliver_task_bundle(
            pid, bob_tok, {'a.py': 'x = 1\n'}, title='Same model',
            declared_tool='claude-code', declared_model='claude-opus-4-6',
        )
        app1 = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid1), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-cm1',
            'previous': prev1, 'contribution': cid1, 'summary': 'Same model review.',
            'declared_tool': 'cursor', 'declared_model': 'claude-opus-4-6',
            'checks': [{'rule_id': 'STD-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['a.py:1']}}],
        }, token=coord_tok)
        self.assertEqual(201, app1.status, app1.data)
        so1 = self.request('GET', '/v1/projects/%s/tasks/%s/sign-off' % (pid, tid1), token=coord_tok).data
        self.assertEqual('same', so1['cross_model'])
        self.assertTrue(so1['cross_model_advisory'])
        self.assertEqual('claude-opus-4-6', so1['author']['declared_model'])
        self.assertEqual('claude-opus-4-6', so1['reviewer']['declared_model'])

        # Different declared_model -> cross_model is 'different' and no advisory!
        tid2, cid2, prev2, _ = self.deliver_task_bundle(
            pid, bob_tok, {'b.py': 'y = 2\n'}, title='Cross model',
            declared_tool='codex', declared_model='gpt-5',
        )
        app2 = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid2), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-cm2',
            'previous': prev2, 'contribution': cid2, 'summary': 'Cross model review.',
            'declared_tool': 'claude-code', 'declared_model': 'claude-opus-4-6',
            'checks': [{'rule_id': 'STD-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['b.py:1']}}],
        }, token=coord_tok)
        self.assertEqual(201, app2.status, app2.data)
        so2 = self.request('GET', '/v1/projects/%s/tasks/%s/sign-off' % (pid, tid2), token=coord_tok).data
        self.assertEqual('different', so2['cross_model'])
        self.assertIsNone(so2['cross_model_advisory'])

    def test_signoff_json_markdown_download_and_task_view_checked_against_wording(self):
        pid = self.pid
        alice_uid, _ = self.create_person('alice4', 'contributor', pid)
        _, bob_tok = self.create_person('bob4', 'contributor', pid)
        _, coord_tok = self.create_person('cora4', 'coordinator', pid)

        self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 0, 'reason': 'v1',
            'rules': [_rule('DOC-1', owner=alice_uid, severity='should')],
        }, token=coord_tok)
        tid, cid, prev, _ = self.deliver_task_bundle(pid, bob_tok, {'doc.md': '# Hello\n'}, title='Signoff export')
        app = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-so1',
            'previous': prev, 'contribution': cid, 'summary': 'Looks great.',
            'checks': [{'rule_id': 'DOC-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['doc.md:1']}}],
        }, token=coord_tok)
        self.assertEqual(201, app.status, app.data)

        j_resp = self.request('GET', '/v1/projects/%s/tasks/%s/sign-off?contribution=%s' % (pid, tid, cid), token=bob_tok)
        self.assertEqual(200, j_resp.status, j_resp.data)
        self.assertIn('Checked against standards register v1', j_resp.data['headline'])

        md_resp = self.request('GET', '/v1/projects/%s/tasks/%s/sign-off.md' % (pid, tid), token=bob_tok)
        self.assertEqual(200, md_resp.status)
        self.assertIn('text/markdown', md_resp.headers.get('content-type', ''))
        self.assertIn('attachment;', md_resp.headers.get('content-disposition', ''))
        md_text = md_resp.body.decode('utf-8')
        self.assertIn('Checked against standards register v1', md_text)
        self.assertNotIn('certified', md_text.lower())
        self.assertNotIn('guaranteed', md_text.lower())

        task_js = (WEB / 'js' / 'views' / 'task.js').read_text(encoding='utf-8')
        self.assertIn('Checked against standards register v', task_js)
        self.assertNotIn('certified', task_js.lower())

    def test_cross_project_inheritance_requires_shared_flag_and_cap_read(self):
        shared_pid = self.new_project('Shared Standards', 'sharedstd')
        private_pid = self.new_project('Private Standards', 'privatestd')
        child_pid = self.pid

        outsider_uid, outsider_tok = self.create_person('outsider', 'coordinator', child_pid)
        # Note: outsider_tok is coordinator in child_pid, has CAP_READ in shared_pid? NOT YET!
        self.request('PUT', '/v1/projects/%s/standards' % shared_pid, {
            'expected_version': 0, 'shared': True, 'reason': 'Org shared v1.',
            'rules': [_rule('ORG-1', owner=self.owner_uid, severity='must')],
        }, token=self.owner_tok)

        self.request('PUT', '/v1/projects/%s/standards' % private_pid, {
            'expected_version': 0, 'shared': False, 'reason': 'Not shared.',
            'rules': [_rule('PRV-1', owner=self.owner_uid, severity='should')],
        }, token=self.owner_tok)
        # Grant outsider viewer access on private_pid so CAP_READ passes and shared=False check fires (#6).
        self.request('PUT', '/v1/projects/%s/members/%s' % (private_pid, outsider_uid), {'role': 'viewer'}, token=self.owner_tok)

        # 1) Lack of CAP_READ on shared_pid -> refused with 403 (#21)!
        no_read = self.request('PUT', '/v1/projects/%s/standards' % child_pid, {
            'expected_version': 0, 'inherits': shared_pid, 'reason': 'Try inherit without CAP_READ.', 'rules': [],
        }, token=outsider_tok)
        self.assertEqual(403, no_read.status, no_read.data)
        self.assertEqual(rs.refusal_inheritance_no_read(shared_pid), no_read.data['error']['message'])

        # 2) Has CAP_READ on private_pid, but shared=False -> refused (#6)!
        not_shared = self.request('PUT', '/v1/projects/%s/standards' % child_pid, {
            'expected_version': 0, 'inherits': private_pid, 'reason': 'Try inherit non-shared.', 'rules': [],
        }, token=outsider_tok)
        self.assertEqual(422, not_shared.status, not_shared.data)
        self.assertEqual(rs.refusal_inheritance_not_shared(private_pid), not_shared.data['error']['message'])

        # 3) Grant viewer on shared_pid -> inheritance succeeds, and ORG-1 is read-only (#5)!
        self.request('PUT', '/v1/projects/%s/members/%s' % (shared_pid, outsider_uid), {'role': 'viewer'}, token=self.owner_tok)
        ok_inh = self.request('PUT', '/v1/projects/%s/standards' % child_pid, {
            'expected_version': 0, 'inherits': shared_pid, 'reason': 'Inherit shared_pid.', 'rules': [],
        }, token=outsider_tok)
        self.assertEqual(200, ok_inh.status, ok_inh.data)
        self.assertIn('ORG-1', [r['id'] for r in ok_inh.data['effective_rules']])

        weaken = self.request('PUT', '/v1/projects/%s/standards' % child_pid, {
            'expected_version': 1, 'inherits': shared_pid, 'reason': 'Try modifying ORG-1.',
            'rules': [_rule('ORG-1', owner=outsider_uid, severity='should')],
        }, token=outsider_tok)
        self.assertEqual(422, weaken.status)
        self.assertEqual(rs.refusal_inherited_must_readonly(child_pid, 'ORG-1', shared_pid), weaken.data['error']['message'])

    def test_recommend_carries_checks_and_web_approve_adopts_them_on_both_backends_and_task_js(self):
        # Kills M17!
        pid = self.pid
        alice_uid, alice_tok = self.create_person('alice_rec', 'contributor', pid)
        _, bob_tok = self.create_person('bob_rec', 'contributor', pid)
        _, rev_agent = self.create_agent(alice_tok, 'reviewer-bot', [pid])

        self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 0, 'reason': 'Must rule for adoption.',
            'rules': [_rule('ADP-1', owner=alice_uid, severity='must')],
        }, token=self.owner_tok)
        self.request('PUT', '/v1/projects/%s/standards/gate' % pid, {'mode': 'block', 'reason': 'block'}, token=self.owner_tok)

        tid, cid, prev, commit = self.deliver_task_bundle(
            pid, bob_tok, {'lib.py': 'def ok():\n    return True\n'}, title='Adopt rec task',
            declared_tool='codex', declared_model='gpt-5',
        )

        # Reviewing agent posts recommend with check records!
        rec_resp = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'recommend', 'operation_id': 'op-rec-1',
            'contribution': cid, 'commit': commit, 'verdict': 'approve',
            'summary': 'Verified lib.py bounds.',
            'items': [],
            'declared_tool': 'claude-code', 'declared_model': 'claude-opus-4-6',
            'checks': [{'rule_id': 'ADP-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['lib.py:1-2']}}],
        }, token=rev_agent)
        self.assertEqual(201, rec_resp.status, rec_resp.data)

        t_view = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (pid, tid), token=self.owner_tok).data
        recs = t_view['review']['recommendations']
        self.assertEqual(1, len(recs))
        rec_id = str(recs[0]['id'])
        self.assertEqual('ADP-1', recs[0]['checks'][0]['rule_id'])

        # Forged / nonexistent adopt_recommendation is refused with #24a!
        bad_adopt = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-bad-adopt',
            'previous': prev, 'contribution': cid, 'summary': 'Bad adopt.',
            'adopt_recommendation': 'nonexistent-rec-999',
        }, token=self.owner_tok)
        self.assertEqual(422, bad_adopt.status, bad_adopt.data)
        self.assertEqual(rs.refusal_invalid_adopt_recommendation('nonexistent-rec-999', cid, commit), bad_adopt.data['error']['message'])

        # Owner approves adopting rec_id without re-typing checks -> 201 in block mode!
        ok_adopt = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-ok-adopt',
            'previous': prev, 'contribution': cid, 'summary': 'Adopt independent agent review.',
            'adopt_recommendation': rec_id,
        }, token=self.owner_tok)
        self.assertEqual(201, ok_adopt.status, ok_adopt.data)

        so = self.request('GET', '/v1/projects/%s/tasks/%s/sign-off' % (pid, tid), token=self.owner_tok).data
        self.assertEqual('pass', so['checks'][0]['result'])
        self.assertIsNotNone(so['reviewer']['recommended_by'])
        self.assertEqual('claude-opus-4-6', so['reviewer']['declared_model'])
        self.assertEqual('different', so['cross_model'])

    def test_verifier_credential_and_reviewer_approver_independence_on_commit_observations(self):
        # Kills M18!
        pid = self.pid
        alice_uid, alice_tok = self.create_person('alice_v', 'contributor', pid)
        _, bob_tok = self.create_person('bob_v', 'contributor', pid)
        _, rev_tok = self.create_person('rev_v', 'coordinator', pid)
        _, indep_coord_tok = self.create_person('indep_v', 'coordinator', pid)

        self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 0, 'reason': 'Mechanical must rule.',
            'rules': [_rule('CI-1', owner=alice_uid, severity='must', check={'kind': 'mechanical', 'tool': 'pytest', 'command': 'pytest'})],
        }, token=self.owner_tok)
        self.request('PUT', '/v1/projects/%s/standards/gate' % pid, {'mode': 'block', 'reason': 'block'}, token=self.owner_tok)

        tid, cid, prev, commit = self.deliver_task_bundle(pid, bob_tok, {'m.py': 'x = 1\n'}, title='CI independence')

        # 1) Author Bob cannot record commit observation (#13)!
        bob_rec = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit), {
            'changed_files': ['m.py'], 'ci_checks': [{'tool': 'pytest', 'command': 'pytest', 'exit_code': 0}],
        }, token=bob_tok)
        self.assertEqual(403, bob_rec.status)
        self.assertEqual(rs.refusal_commit_observation_authority(commit), bob_rec.data['error']['message'])

        # 2) Coordinator rev_tok records commit observation -> rev_tok CANNOT then recommend or approve (#24b)!
        c_rec = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit), {
            'changed_files': ['m.py'], 'ci_checks': [{'tool': 'pytest', 'command': 'pytest', 'exit_code': 0}],
        }, token=rev_tok)
        self.assertEqual(201, c_rec.status, c_rec.data)

        self_app = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m18-self',
            'previous': prev, 'contribution': cid, 'summary': 'Approving own CI observation.',
        }, token=rev_tok)
        self.assertEqual(422, self_app.status, self_app.data)
        self.assertIn('unverified: no CI result recorded', self_app.data['error']['message'])

        # 3) Independent coordinator indep_coord_tok CAN approve relying on rev_tok's CI observation!
        ok_app = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m18-indep',
            'previous': prev, 'contribution': cid, 'summary': 'Independent approval.',
        }, token=indep_coord_tok)
        self.assertEqual(201, ok_app.status, ok_app.data)

        # 4) And indep_coord_tok (now the approver!) CANNOT overwrite commit observation afterwards (#13)!
        after_rec = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit), {
            'changed_files': ['m.py'], 'ci_checks': [{'tool': 'pytest', 'command': 'pytest', 'exit_code': 0}],
        }, token=indep_coord_tok)
        self.assertEqual(403, after_rec.status)
        self.assertEqual(rs.refusal_commit_observation_authority(commit), after_rec.data['error']['message'])

    def test_coordinator_issuing_verify_credential_recording_pass_then_approving_is_refused(self):
        # Plan 3 Item 3: verify credential's party == its issuing person!
        pid = self.pid
        alice_uid, _ = self.create_person('alice_p3', 'contributor', pid)
        _, bob_tok = self.create_person('bob_p3', 'contributor', pid)
        _, coord_tok = self.create_person('cora_p3', 'coordinator', pid)
        _, coord_agent = self.create_agent(coord_tok, 'cora-p3-agent', [pid], coordinate_in=[pid])

        # Coordinator agent credential cannot issue a verify credential (person-only)!
        ag_issue = self.request('POST', '/v1/projects/%s/worker-credentials' % pid, {
            'scopes': ['verify'], 'label': 'Agent tries issuing verify',
        }, token=coord_agent)
        self.assertEqual(403, ag_issue.status)

        # Coordinator person session issues verify credential!
        issued = self.request('POST', '/v1/projects/%s/worker-credentials' % pid, {
            'scopes': ['verify'], 'label': 'Cora CI Runner',
        }, token=coord_tok)
        self.assertEqual(201, issued.status, issued.data)
        cora_verify_tok = issued.data['credential']['secret']

        self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 0, 'reason': 'Mechanical rule.',
            'rules': [_rule('VRF-1', owner=alice_uid, severity='must', check={'kind': 'mechanical', 'tool': 'ruff', 'command': 'ruff check'})],
        }, token=self.owner_tok)
        self.request('PUT', '/v1/projects/%s/standards/gate' % pid, {'mode': 'block', 'reason': 'block'}, token=self.owner_tok)

        tid, cid, prev, commit = self.deliver_task_bundle(pid, bob_tok, {'v.py': 'z = 1\n'}, title='Verify party test')

        r_obs = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit), {
            'changed_files': ['v.py'], 'ci_checks': [{'tool': 'ruff', 'command': 'ruff check', 'exit_code': 0}],
        }, token=cora_verify_tok)
        self.assertEqual(201, r_obs.status, r_obs.data)

        # BOTH Cora's person session AND Cora's coordinator-granted agent are refused when approving!
        for label, tok in (('person-session', coord_tok), ('coordinator-agent', coord_agent)):
            with self.subTest(approver=label):
                att = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
                    'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-vrf-' + label,
                    'previous': prev, 'contribution': cid, 'summary': 'Same party as verify issuer.',
                }, token=tok)
                self.assertEqual(422, att.status, att.data)
                self.assertIn('unverified: no CI result recorded', att.data['error']['message'])

        # Independent project owner approves cleanly (201)!
        ok = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-vrf-owner',
            'previous': prev, 'contribution': cid, 'summary': 'Independent owner approves.',
        }, token=self.owner_tok)
        self.assertEqual(201, ok.status, ok.data)

    def test_no_register_with_draft_gate_block_and_commit_observation_approves_201_without_signoff(self):
        # PM VERIFIED binding scenario on HTTP (both backends)!
        pid = self.pid
        alice_uid, alice_tok = self.create_person('alice_nr', 'contributor', pid)
        _, coord_tok = self.create_person('cora_nr', 'coordinator', pid)

        tid, cid, prev, commit = self.deliver_task_bundle(pid, alice_tok, {'nr.py': 'x = 0\n'}, title='No register task')

        # 1) Propose draft
        d = self.request('POST', '/v1/projects/%s/standards/drafts' % pid, {
            'action': 'upsert', 'reason': 'Draft before v1', 'rule': _rule('PRE-1', owner=alice_uid, severity='must'),
        }, token=alice_tok)
        self.assertEqual(201, d.status, d.data)

        # 2) Set gate to block
        g = self.request('PUT', '/v1/projects/%s/standards/gate' % pid, {
            'mode': 'block', 'reason': 'Pre-stage block before v1',
        }, token=coord_tok)
        self.assertEqual(200, g.status, g.data)

        # 3) Record failing commit observation
        obs = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit), {
            'changed_files': ['nr.py'], 'ci_checks': [{'tool': 'pytest', 'command': 'pytest', 'exit_code': 1}],
        }, token=self.owner_tok)
        self.assertEqual(201, obs.status, obs.data)

        # Register read still reports status == 'none', and brief has NO 'standards' key!
        reg_get = self.request('GET', '/v1/projects/%s/standards' % pid, token=coord_tok)
        self.assertEqual(200, reg_get.status)
        self.assertEqual('none', reg_get.data['status'])

        br = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (pid, tid), token=coord_tok)
        self.assertEqual(200, br.status, br.data)
        self.assertNotIn('standards', br.data)

        # 4) Approve succeeds 201 with zero refusals and writes NO sign-off!
        app = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-nr-app',
            'previous': prev, 'contribution': cid, 'summary': 'Approved identically to today.',
        }, token=coord_tok)
        self.assertEqual(201, app.status, app.data)
        self.assertNotIn('sign_off', app.data)

        so_get = self.request('GET', '/v1/projects/%s/tasks/%s/sign-off' % (pid, tid), token=coord_tok)
        self.assertEqual(404, so_get.status)

    def test_r2_m1_closed_task_and_second_approve_gated_on_http(self):
        pid = self.pid
        alice_uid, alice_tok = self.create_person('alice_m1', 'contributor', pid)
        _, cora_tok = self.create_person('cora_m1', 'coordinator', pid)
        _, cara_tok = self.create_person('cara_m1', 'coordinator', pid)
        _, cora_agent = self.create_agent(cora_tok, 'cora-m1-agent', [pid], coordinate_in=[pid])

        self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 0, 'reason': 'Gate every approve.',
            'rules': [_rule('MUST-1', owner=alice_uid, severity='must')],
        }, token=self.owner_tok)
        self.request('PUT', '/v1/projects/%s/standards/gate' % pid, {'mode': 'block', 'reason': 'block'}, token=self.owner_tok)

        tid, cid, prev, _ = self.deliver_task_bundle(pid, alice_tok, {'m1.py': 'x = 1\n'}, title='M1 HTTP task')

        # 1) Close task -> BOTH coordinator person AND coordinator-granted agent approve MUST be refused 422!
        ver0 = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (pid, tid), token=cora_tok).data['task'].get('version')
        cl_body = {'status': 'closed'}
        if ver0 is not None:
            cl_body['version'] = ver0
        cl = self.request('PATCH', '/v1/projects/%s/tasks/%s' % (pid, tid), cl_body, token=cora_tok)
        self.assertEqual(200, cl.status, cl.data)

        for label, tok in (('person', cora_tok), ('coord-agent', cora_agent)):
            with self.subTest(closed_approver=label):
                bad = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
                    'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m1-cl-' + label,
                    'previous': prev, 'contribution': cid, 'summary': 'Closed bypass attempt.',
                }, token=tok)
                self.assertEqual(422, bad.status, bad.data)
                self.assertIn('MUST-1', bad.data['error']['message'])

        # Reopen -> still awaiting-review and no sign-off!
        ver1 = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (pid, tid), token=cora_tok).data['task'].get('version')
        op_body = {'status': 'open'}
        if ver1 is not None:
            op_body['version'] = ver1
        op = self.request('PATCH', '/v1/projects/%s/tasks/%s' % (pid, tid), op_body, token=cora_tok)
        self.assertEqual(200, op.status, op.data)
        br = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (pid, tid), token=cora_tok).data
        self.assertEqual('awaiting-review', br['review']['state'])
        self.assertEqual(404, self.request('GET', '/v1/projects/%s/tasks/%s/sign-off' % (pid, tid), token=cora_tok).status)

        # 2) Valid first approve succeeds -> 201!
        app1 = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m1-http-first',
            'previous': prev, 'contribution': cid, 'summary': 'First valid approve.',
            'checks': [{'rule_id': 'MUST-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['m1.py:1']}}],
        }, token=cora_tok)
        self.assertEqual(201, app1.status, app1.data)

        br_after_1 = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (pid, tid), token=cara_tok).data
        prev2 = br_after_1['review']['latest_id']

        # 3) Second approve without checks on already-approved contribution MUST be refused 422!
        app2_bad = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m1-http-second-bad',
            'previous': prev2, 'contribution': cid, 'summary': 'Second ungated approve.',
        }, token=cara_tok)
        self.assertEqual(422, app2_bad.status, app2_bad.data)
        self.assertIn('MUST-1', app2_bad.data['error']['message'])
        so_still = self.request('GET', '/v1/projects/%s/tasks/%s/sign-off' % (pid, tid), token=cara_tok).data
        self.assertEqual('op-m1-http-first', so_still['operation_id'])

        # 4) Second approve WITH valid checks succeeds 201 and updates sign-off!
        app2_ok = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m1-http-second-ok',
            'previous': prev2, 'contribution': cid, 'summary': 'Second checked approve.',
            'checks': [{'rule_id': 'MUST-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['m1.py:1']}}],
        }, token=cara_tok)
        self.assertEqual(201, app2_ok.status, app2_ok.data)
        so_updated = self.request('GET', '/v1/projects/%s/tasks/%s/sign-off' % (pid, tid), token=cara_tok).data
        self.assertEqual('op-m1-http-second-ok', so_updated['operation_id'])

    def test_r2_m2_partial_rerecord_and_predelivery_author_provenance_on_http(self):
        pid = self.pid
        alice_uid, alice_tok = self.create_person('alice_m2', 'contributor', pid)
        olga_uid, olga_tok = self.create_person('olga_m2', 'coordinator', pid)
        oscar_uid, oscar_tok = self.create_person('oscar_m2', 'coordinator', pid)
        _, cara_tok = self.create_person('cara_m2', 'coordinator', pid)
        _, cora_tok = self.create_person('cora_m2', 'coordinator', pid)

        oscar_ver = self.request('POST', '/v1/projects/%s/worker-credentials' % pid, {
            'scopes': ['verify'], 'label': 'Oscar Verifier',
        }, token=oscar_tok).data['credential']['secret']
        cora_ver = self.request('POST', '/v1/projects/%s/worker-credentials' % pid, {
            'scopes': ['verify'], 'label': 'Cora Verifier',
        }, token=cora_tok).data['credential']['secret']
        _, cora_bot = self.create_agent(cora_tok, 'cora-m2-bot', [pid])

        self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 0, 'reason': 'M2 provenance.',
            'rules': [
                _rule('STD-50', owner=alice_uid, severity='must',
                      check={'kind': 'mechanical', 'tool': 'pytest', 'command': 'pytest -q'}),
                _rule('PATH-1', owner=alice_uid, severity='must',
                      applicability={'paths': ['secret/**']}),
            ],
        }, token=self.owner_tok)
        self.request('PUT', '/v1/projects/%s/standards/gate' % pid, {'mode': 'block', 'reason': 'block'}, token=self.owner_tok)

        # (a) Opus M2: Olga records STD-50 pass; Oscar verify cred records ONLY changed_files.
        tid1, cid1, prev1, commit1 = self.deliver_task_bundle(pid, alice_tok, {'README.md': 'ok\n'}, title='M2a HTTP')
        r1 = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit1), {
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}],
        }, token=olga_tok)
        self.assertEqual(201, r1.status, r1.data)
        r2 = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit1), {
            'changed_files': ['README.md'],
        }, token=oscar_ver)
        self.assertEqual(201, r2.status, r2.data)

        olga_app = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid1), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m2a-olga',
            'previous': prev1, 'contribution': cid1, 'summary': 'Olga self-laundered CI.',
        }, token=olga_tok)
        self.assertEqual(422, olga_app.status, olga_app.data)
        self.assertIn('STD-50 (unverified: no CI result recorded', olga_app.data['error']['message'])

        cara_app = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid1), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m2a-cara',
            'previous': prev1, 'contribution': cid1, 'summary': 'Cara independent approve.',
        }, token=cara_tok)
        self.assertEqual(201, cara_app.status, cara_app.data)
        so1 = cara_app.data['sign_off']
        self.assertEqual(oscar_uid, so1['diff_recorded_by'])
        std50_row = [c for c in so1['checks'] if c['rule_id'] == 'STD-50'][0]
        self.assertEqual(olga_uid, std50_row['evidence']['recorded_by'])

        # (b) Fable MUST-2: Cora's verify cred records fake changed_files + STD-50 pass BEFORE cora-bot delivers!
        repo2 = self.tmp / 'm2b-repo'
        repo2.mkdir(parents=True, exist_ok=True)
        _git(repo2, 'init')
        (repo2 / 'secret').mkdir(parents=True, exist_ok=True)
        (repo2 / 'secret' / 'k.py').write_text('key = 1\n', encoding='utf-8')
        _git(repo2, 'add', '.')
        _git(repo2, 'commit', '-m', 'secret change')
        bpath2 = self.delivery_root / 'm2b.bundle'
        commit2, bsha2 = _make_bundle(repo2, bpath2)

        tid2 = self.create_task(self.owner_tok, pid, 'M2b pre-delivery').data['id']
        self.assertEqual(200, self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (pid, tid2), {}, token=cora_bot).status)

        # Naming task_id while cora_bot is assignee is refused 403 immediately!
        pre_with_task = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit2), {
            'task_id': tid2,
            'changed_files': ['README.md'],
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}],
        }, token=cora_ver)
        self.assertEqual(403, pre_with_task.status, pre_with_task.data)

        # Even if recorded without task_id before delivery, evaluation excludes author's party on brief and approve!
        pre_no_task = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit2), {
            'changed_files': ['README.md'],
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}],
        }, token=cora_ver)
        self.assertEqual(201, pre_no_task.status, pre_no_task.data)

        deliv2 = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid2), {
            'schema_version': 1, 'operation': 'contribute', 'operation_id': 'op-m2b-deliv',
            'previous': None, 'supersedes': None, 'repository': 'https://example.invalid/r.git',
            'commit': commit2, 'base_commit': commit2,
            'delivery': {'kind': 'bundle', 'path': 'server:' + str(bpath2), 'sha256': bsha2},
            'summary': 'Deliver secret/k.py.',
        }, token=cora_bot)
        self.assertEqual(201, deliv2.status, deliv2.data)

        br2 = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (pid, tid2), token=cara_tok).data
        self.assertEqual('diff_unverified', br2['standards']['diff_status'])
        self.assertIn('PATH-1', [r['id'] for r in br2['standards']['conditional_rules']])

        cid2 = br2['review']['contribution']['id']
        prev2 = br2['review']['latest_id']
        bad_app2 = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid2), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m2b-cara',
            'previous': prev2, 'contribution': cid2, 'summary': 'Try approving forged pre-delivery CI.',
        }, token=cara_tok)
        self.assertEqual(422, bad_app2.status, bad_app2.data)
        self.assertIn('STD-50 (unverified: no CI result recorded', bad_app2.data['error']['message'])
        self.assertIn('PATH-1', bad_app2.data['error']['message'])

    def test_r2_m3_s1_s2_inherits_detach_empty_put_and_na_over_ci_fail_on_http(self):
        pid = self.pid
        _, viewer_tok = self.create_person('vic_s1', 'viewer', pid)
        alice_uid, alice_tok = self.create_person('alice_s1', 'contributor', pid)
        _, oscar_tok = self.create_person('oscar_s2', 'coordinator', pid)
        _, cara_tok = self.create_person('cara_s2', 'coordinator', pid)

        # Opus N1: project with no register has NO standards_gate field on GET /v1/projects/{pid}!
        proj0 = self.request('GET', '/v1/projects/%s' % pid, token=viewer_tok).data
        self.assertNotIn('standards_gate', proj0)

        # s1: viewer and contributor PUT with empty rules at v0 refused 403; status stays 'none'!
        for label, tok in (('viewer', viewer_tok), ('contributor', alice_tok)):
            with self.subTest(empty_put_v0=label):
                r = self.request('PUT', '/v1/projects/%s/standards' % pid, {
                    'expected_version': 0, 'reason': 'Unprivileged empty v1', 'rules': [],
                }, token=tok)
                self.assertEqual(403, r.status, r.data)
                self.assertEqual(rs.refusal_put_register_authority(), r.data['error']['message'])
        self.assertEqual('none', self.request('GET', '/v1/projects/%s/standards' % pid, token=viewer_tok).data['status'])

        # m3: shared project with TEAM-1 (must) -> child cannot detach inherits via null or another shared project!
        sh1_pid = self.new_project('Shared One', 'shone')
        sh2_pid = self.new_project('Shared Two', 'shtwo')
        self.request('PUT', '/v1/projects/%s/standards' % sh1_pid, {
            'expected_version': 0, 'shared': True, 'reason': 'Shared must',
            'rules': [_rule('TEAM-1', owner=self.owner_uid, severity='must')],
        }, token=self.owner_tok)
        self.request('PUT', '/v1/projects/%s/standards' % sh2_pid, {
            'expected_version': 0, 'shared': True, 'reason': 'Shared should',
            'rules': [_rule('SH-2', owner=self.owner_uid, severity='should')],
        }, token=self.owner_tok)

        c_put = self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 0, 'inherits': sh1_pid, 'reason': 'Inherit sh1', 'rules': [],
        }, token=self.owner_tok)
        self.assertEqual(200, c_put.status, c_put.data)

        # s1 at v1: viewer and contributor still refused 403 on empty PUT!
        for label, tok in (('viewer', viewer_tok), ('contributor', alice_tok)):
            with self.subTest(empty_put_v1=label):
                r = self.request('PUT', '/v1/projects/%s/standards' % pid, {
                    'expected_version': 1, 'inherits': sh1_pid, 'reason': 'No-op PUT', 'rules': [],
                }, token=tok)
                self.assertEqual(403, r.status, r.data)

        for bad_inh in (None, sh2_pid):
            with self.subTest(detach_inherits=bad_inh):
                r = self.request('PUT', '/v1/projects/%s/standards' % pid, {
                    'expected_version': 1, 'inherits': bad_inh, 'reason': 'Detach TEAM-1', 'rules': [],
                }, token=self.owner_tok)
                self.assertEqual(422, r.status, r.data)
                self.assertEqual(
                    rs.refusal_detach_inherited_must(pid, sh1_pid, ['TEAM-1']),
                    r.data['error']['message'],
                )

        # s2: mechanical must rule with recorded exit_code=1 refuses not_applicable; waiver keeps ci_evidence!
        r_v2 = self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 1, 'inherits': sh1_pid, 'reason': 'Add STD-50',
            'rules': [_rule('STD-50', owner=alice_uid, severity='must',
                            check={'kind': 'mechanical', 'tool': 'ruff', 'command': 'ruff check'})],
        }, token=self.owner_tok)
        self.assertEqual(200, r_v2.status, r_v2.data)
        self.request('PUT', '/v1/projects/%s/standards/gate' % pid, {'mode': 'block', 'reason': 'block'}, token=self.owner_tok)

        oscar_ver = self.request('POST', '/v1/projects/%s/worker-credentials' % pid, {
            'scopes': ['verify'], 'label': 'Oscar S2',
        }, token=oscar_tok).data['credential']['secret']
        tid, cid, prev, commit = self.deliver_task_bundle(pid, alice_tok, {'s2.py': 'y = 1\n'}, title='S2 HTTP')

        self.assertEqual(201, self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit), {
            'changed_files': ['s2.py'],
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'ruff', 'command': 'ruff check', 'exit_code': 1}],
        }, token=oscar_ver).status)

        na_bad = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-s2-http-na',
            'previous': prev, 'contribution': cid, 'summary': 'NA over CI fail.',
            'checks': [
                {'rule_id': 'TEAM-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['s2.py:1']}},
                {'rule_id': 'STD-50', 'result': 'not_applicable', 'reason': 'Flaky today.'},
            ],
        }, token=cara_tok)
        self.assertEqual(422, na_bad.status, na_bad.data)
        self.assertEqual(rs.refusal_na_over_mechanical_fail('STD-50', commit), na_bad.data['error']['message'])

        w = self.request('POST', '/v1/projects/%s/tasks/%s/waivers' % (pid, tid), {
            'contribution_id': cid, 'commit': commit, 'rule_id': 'STD-50',
            'reason': 'Tracked upstream ruff issue', 'expiry': {'kind': 'delivery'},
        }, token=cara_tok)
        self.assertEqual(201, w.status, w.data)

        ok = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-s2-http-ok',
            'previous': prev, 'contribution': cid, 'summary': 'Approved with waiver.',
            'checks': [{'rule_id': 'TEAM-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['s2.py:1']}}],
        }, token=cara_tok)
        self.assertEqual(201, ok.status, ok.data)
        std50_so = [c for c in ok.data['sign_off']['checks'] if c['rule_id'] == 'STD-50'][0]
        self.assertEqual(1, std50_so['ci_evidence']['exit_code'])

    def test_r2_s4_viewer_owner_notification_and_adopted_fail_recommendation_parity_on_http(self):
        pid = self.pid
        vic_uid, vic_tok = self.create_person('vic_s4', 'viewer', pid)
        _, bob_tok = self.create_person('bob_s4', 'contributor', pid)
        _, carl_tok = self.create_person('carl_s4', 'contributor', pid)
        _, cara_tok = self.create_person('cara_s4', 'coordinator', pid)

        self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 0, 'reason': 'Viewer rule owner + should rule.',
            'rules': [
                _rule('VIC-1', owner=vic_uid, severity='must'),
                _rule('SHLD-1', owner=self.owner_uid, severity='should'),
            ],
        }, token=self.owner_tok)

        # s4a: viewer rule owner edits VIC-1 -> 200, notified_coordinator True, note in coordinator's inbox!
        patch_res = self.request('PATCH', '/v1/projects/%s/standards/rules/VIC-1' % pid, {
            'expected_version': 1,
            'reason': 'Lower VIC-1 to should.',
            'rule': _rule('VIC-1', owner=vic_uid, severity='should', statement='Viewer updated statement.'),
        }, token=vic_tok)
        self.assertEqual(200, patch_res.status, patch_res.data)
        self.assertTrue(patch_res.data['notified_coordinator'])
        note_id = patch_res.data.get('coordinator_note_id')
        self.assertTrue(isinstance(note_id, str) and note_id.startswith('cnote-'), patch_res.data)

        inbox = self.request('GET', '/v1/projects/%s/notes?box=to' % pid, token=cara_tok)
        self.assertEqual(200, inbox.status, inbox.data)
        inbox_ids = [n['id'] for n in (inbox.data.get('notes') or inbox.data.get('items') or [])]
        self.assertIn(note_id, inbox_ids)

        # s4b: recommender records a 'fail' check citing recommendation item 'note-1', and approver adopts it (201 on BOTH backends)!
        self.request('PUT', '/v1/projects/%s/standards/gate' % pid, {'mode': 'block', 'reason': 'block'}, token=self.owner_tok)
        tid, cid, prev, commit = self.deliver_task_bundle(pid, bob_tok, {'f.py': 'z = 2\n'}, title='S4b adopt fail')

        rec = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'recommend', 'operation_id': 'op-s4b-rec',
            'contribution': cid, 'commit': commit, 'verdict': 'approve',
            'summary': 'Looks good overall; one optional note.',
            'items': [{'id': 'note-1', 'text': 'Optional style cleanup on f.py:1.'}],
            'checks': [
                {'rule_id': 'VIC-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['f.py:1']}},
                {'rule_id': 'SHLD-1', 'result': 'fail', 'evidence': {'kind': 'review_item', 'item_id': 'note-1'}},
            ],
        }, token=carl_tok)
        self.assertEqual(201, rec.status, rec.data)
        rec_id = (rec.data.get('recommendation') or rec.data.get('review') or rec.data).get('id') or rec.data.get('comment_id')

        app = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-s4b-app',
            'previous': prev, 'contribution': cid, 'summary': 'Adopt recommendation with fail note.',
            'adopt_recommendation': rec_id,
        }, token=cara_tok)
        self.assertEqual(201, app.status, app.data)
        by_rule = {c['rule_id']: c['result'] for c in app.data['sign_off']['checks']}
        self.assertEqual({'VIC-1': 'pass', 'SHLD-1': 'fail'}, by_rule)

    def test_r2_n1_author_party_waiver_and_direct_backend_person_guards(self):
        # Kills M13c (author-party waiver over HTTP), M9b (InProcessBackend person check), and M9+
        # (dropping both web_standards_action's is_person_sess check and the per-operation person-session
        # defense-in-depth checks; single-layer M9 at review_standards.py:6239 alone is equivalent).
        pid = self.pid
        _, alice_tok = self.create_person('alice_n1', 'contributor', pid)
        cora_uid, cora_tok = self.create_person('cora_n1', 'coordinator', pid)
        cora_aid, cora_bot = self.create_agent(cora_tok, 'cora-n1-bot', [pid], coordinate_in=[pid])

        self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 0, 'reason': 'Waiver party check.',
            'rules': [_rule('WN-1', owner=cora_uid, severity='must')],
        }, token=self.owner_tok)

        tid, cid, _, commit = self.deliver_task_bundle(pid, cora_bot, {'w.py': 'w = 1\n'}, title='N1 waiver party')
        indep_tid, indep_cid, _, indep_commit = self.deliver_task_bundle(
            pid, alice_tok, {'indep.py': 'x = 2\n'}, title='N1 independent delivery'
        )
        draft_res = self.request('POST', '/v1/projects/%s/standards/drafts' % pid, {
            'action': 'upsert', 'reason': 'Agent draft', 'rule': _rule('WN-2', owner=cora_uid),
        }, token=cora_bot)
        self.assertEqual(201, draft_res.status, draft_res.data)
        draft_id = (draft_res.data.get('draft') or draft_res.data)['id']

        # M13c: Cora's person session CANNOT waive a rule on her own agent cora_bot's delivery!
        self_waive = self.request('POST', '/v1/projects/%s/tasks/%s/waivers' % (pid, tid), {
            'contribution_id': cid, 'commit': commit, 'rule_id': 'WN-1',
            'reason': 'Waiving own agent delivery', 'expiry': {'kind': 'delivery'},
        }, token=cora_tok)
        self.assertEqual(403, self_waive.status, self_waive.data)
        self.assertIn('Waiver refused for rule WN-1:', self_waive.data['error']['message'])
        self.assertIn('cannot waive a rule on their own delivery.', self_waive.data['error']['message'])

        # Direct backend-level person-only checks bypassing HTTP handler route guards (kills M9+ and M9b)!
        cred_principal = self.service.authenticate(cora_bot)
        for route_name, cap, extra_payload in (
            ('standards.put', http_authority.CAP_READ, {'payload': {'expected_version': 1, 'reason': 'x', 'rules': []}}),
            ('standards.rule-edit', http_authority.CAP_READ, {'rule_id': 'WN-1', 'payload': {'expected_version': 1, 'reason': 'x', 'rule': _rule('WN-1', owner=cora_uid)}}),
            ('standards.draft-decide', http_authority.CAP_READ, {'draft_id': draft_id, 'payload': {'decision': 'reject', 'reason': 'x'}}),
            ('standards.gate-set', http_authority.CAP_COORDINATE, {'payload': {'mode': 'block', 'reason': 'x'}}),
            ('standards.waiver-grant', http_authority.CAP_READ, {'task_id': indep_tid, 'payload': {'contribution_id': indep_cid, 'commit': indep_commit, 'rule_id': 'WN-1', 'reason': 'x'}}),
            ('standards.commit-record', http_authority.CAP_COORDINATE, {'operation': 'record-commit-coordinator', 'commit': indep_commit, 'payload': {'changed_files': ['indep.py']}}),
        ):
            with self.subTest(direct_backend_route=route_name):
                with self.assertRaises(http_auth.HttpError) as cm_direct:
                    self.backend.invoke(
                        route_name, cred_principal, pid,
                        dict(actor=cora_aid, **extra_payload),
                        'op-direct-' + route_name.replace('.', '-'),
                        capability=cap,
                    )
                self.assertEqual(403, cm_direct.exception.status)

    def test_r3_m1_no_eviction_per_party_and_256_flood_on_both_http_backends(self):
        # m1-no-eviction on both InProcessBackend and EndpointBackend:
        # 1) One party cannot exceed 32 observations even across rotating verify credentials + person session.
        # 2) Across 9 independent parties, 256 observations fill the commit cap; the 257th returns 409 with
        #    pinned sentence, nothing is evicted, and entry #0's CI FAIL + N/A refusal survive intact!
        pid = self.pid
        alice_uid, alice_tok = self.create_person('alice_r3m1', 'contributor', pid)
        ver0_uid, ver0_tok = self.create_person('ver0_r3m1', 'coordinator', pid)
        approver_uid, approver_tok = self.create_person('app_r3m1', 'coordinator', pid)

        self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 0, 'reason': 'Flood test mechanical rule.',
            'rules': [_rule(
                'STD-50', owner=alice_uid, severity='must',
                check={'kind': 'mechanical', 'tool': 'pytest', 'command': 'pytest -q'},
            )],
        }, token=self.owner_tok)
        self.request('PUT', '/v1/projects/%s/standards/gate' % pid, {'mode': 'block', 'reason': 'block'}, token=self.owner_tok)

        tid, cid, prev, commit = self.deliver_task_bundle(pid, alice_tok, {'app.py': 'x = 1\n'}, title='HTTP flood test')

        # Entry #1: independent verifier ver0 records STD-50 FAIL (exit 1).
        ver0_cred = self.request('POST', '/v1/projects/%s/worker-credentials' % pid, {
            'scopes': ['verify'], 'label': 'Ver0 CI',
        }, token=ver0_tok).data['credential']['secret']
        r0 = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit), {
            'changed_files': ['app.py'],
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}],
        }, token=ver0_cred)
        self.assertEqual(201, r0.status, r0.data)

        # Approver (not yet a participant!) issues TWO verify credentials + uses person session:
        # 15 + 10 + 7 = 32 succeed, 33rd is refused 409!
        app_v1 = self.request('POST', '/v1/projects/%s/worker-credentials' % pid, {
            'scopes': ['verify'], 'label': 'App Ver 1',
        }, token=approver_tok).data['credential']['secret']
        app_v2 = self.request('POST', '/v1/projects/%s/worker-credentials' % pid, {
            'scopes': ['verify'], 'label': 'App Ver 2',
        }, token=approver_tok).data['credential']['secret']

        for tok, count in ((app_v1, 15), (app_v2, 10), (approver_tok, 7)):
            for _ in range(count):
                res = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit), {
                    'changed_files': ['app.py'],
                }, token=tok)
                self.assertEqual(201, res.status, res.data)

        party_cap_res = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit), {
            'changed_files': ['app.py'],
        }, token=app_v2)
        self.assertEqual(409, party_cap_res.status, party_cap_res.data)
        self.assertEqual(
            rs.refusal_commit_observation_recorder_cap(commit, rs.COMMIT_OBSERVATIONS_PER_RECORDER_MAX),
            party_cap_res.data['error']['message'],
        )

        # Fill up to 256 total observations across 8 more coordinator accounts (currently 1 + 32 = 33; need 223 more: 7*31 + 6)!
        flood_creds = []
        for idx in range(8):
            _, f_tok = self.create_person('flood_%d_r3m1' % idx, 'coordinator', pid)
            f_cred = self.request('POST', '/v1/projects/%s/worker-credentials' % pid, {
                'scopes': ['verify'], 'label': 'Flood %d' % idx,
            }, token=f_tok).data['credential']['secret']
            flood_creds.append(f_cred)

        for f_cred in flood_creds[:7]:
            for _ in range(31):
                res = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit), {
                    'changed_files': ['app.py'],
                }, token=f_cred)
                self.assertEqual(201, res.status, res.data)
        for _ in range(6):
            res = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit), {
                'changed_files': ['app.py'],
            }, token=flood_creds[7])
            self.assertEqual(201, res.status, res.data)

        # Now at 256: 257th observation from flood_creds[7] (who only used 6 < 32) is refused 409!
        cap_res = self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit), {
            'changed_files': ['app.py'],
        }, token=flood_creds[7])
        self.assertEqual(409, cap_res.status, cap_res.data)
        self.assertEqual(
            rs.refusal_commit_observation_cap(commit, rs.COMMIT_OBSERVATIONS_MAX),
            cap_res.data['error']['message'],
        )

        # Verify commits/<commit>.json still holds 256 observations with ver0's FAIL at index 0!
        obs_doc = self.request('GET', '/v1/projects/%s/standards/commits/%s' % (pid, commit), token=approver_tok).data
        self.assertEqual(256, len(obs_doc['observations']))
        self.assertEqual(1, obs_doc['observations'][0]['ci_checks'][0]['exit_code'])

        # Approver's N/A attempt in block mode is STILL refused 422!
        na_res = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-m1-http-na',
            'previous': prev, 'contribution': cid, 'summary': 'Try N/A after 256 flood.',
            'checks': [{'rule_id': 'STD-50', 'result': 'not_applicable', 'reason': 'Claim N/A.'}],
        }, token=approver_tok)
        self.assertEqual(422, na_res.status, na_res.data)
        self.assertEqual(rs.refusal_na_over_mechanical_fail('STD-50', commit), na_res.data['error']['message'])

    def test_r3_m2_fails_always_count_s2p_and_w8_on_both_http_backends(self):
        # m2-fails-always-count: Opus S2P cases (1) and (2) and Fable W8 on both HTTP backends!
        pid = self.pid
        alice_uid, alice_tok = self.create_person('alice_r3m2', 'contributor', pid)
        oscar_uid, oscar_tok = self.create_person('oscar_r3m2', 'coordinator', pid)
        olga_uid, olga_tok = self.create_person('olga_r3m2', 'coordinator', pid)
        cara_uid, cara_tok = self.create_person('cara_r3m2', 'coordinator', pid)

        oscar_ver = self.request('POST', '/v1/projects/%s/worker-credentials' % pid, {
            'scopes': ['verify'], 'label': 'Oscar Ver',
        }, token=oscar_tok).data['credential']['secret']
        olga_ver = self.request('POST', '/v1/projects/%s/worker-credentials' % pid, {
            'scopes': ['verify'], 'label': 'Olga Ver',
        }, token=olga_tok).data['credential']['secret']

        self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 0, 'reason': 'S2P + W8 rule.',
            'rules': [_rule(
                'STD-50', owner=alice_uid, severity='must',
                applicability={'paths': ['src/**']},
                check={'kind': 'mechanical', 'tool': 'pytest', 'command': 'pytest -q'},
            )],
        }, token=self.owner_tok)
        self.request('PUT', '/v1/projects/%s/standards/gate' % pid, {'mode': 'block', 'reason': 'block'}, token=self.owner_tok)

        # Case (1) & Fable W8: Oscar's verify cred records CI FAIL + changed_files=['README.md'], then CI PASS.
        # When Oscar approves: changed_files & CI PASS are excluded, CI FAIL counts -> N/A refused 422, plain refused 422!
        tid1, cid1, prev1, commit1 = self.deliver_task_bundle(pid, alice_tok, {'src/x.py': 'x = 1\n'}, title='S2P1 HTTP')
        self.assertEqual(201, self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit1), {
            'changed_files': ['README.md'],
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}],
        }, token=oscar_ver).status)
        self.assertEqual(201, self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit1), {
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}],
        }, token=oscar_ver).status)

        na1 = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid1), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-s2p1-na',
            'previous': prev1, 'contribution': cid1, 'summary': 'Oscar NA over own-party fail.',
            'checks': [{'rule_id': 'STD-50', 'result': 'not_applicable', 'reason': 'Skip fail.'}],
        }, token=oscar_tok)
        self.assertEqual(422, na1.status, na1.data)
        self.assertEqual(rs.refusal_na_over_mechanical_fail('STD-50', commit1), na1.data['error']['message'])

        plain1 = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid1), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-s2p1-plain',
            'previous': prev1, 'contribution': cid1, 'summary': 'Oscar plain approve.',
        }, token=oscar_tok)
        self.assertEqual(422, plain1.status, plain1.data)
        self.assertIn('fail without waiver', plain1.data['error']['message'])

        self.assertEqual(201, self.request('POST', '/v1/projects/%s/tasks/%s/waivers' % (pid, tid1), {
            'contribution_id': cid1, 'commit': commit1, 'rule_id': 'STD-50',
            'reason': 'Cara waives for Oscar', 'expiry': {'kind': 'delivery'},
        }, token=cara_tok).status)
        ok1 = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid1), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-s2p1-ok',
            'previous': prev1, 'contribution': cid1, 'summary': 'Oscar approves with waiver.',
        }, token=oscar_tok)
        self.assertEqual(201, ok1.status, ok1.data)
        self.assertEqual('diff_unverified', ok1.data['sign_off']['diff_status'])
        row1 = [c for c in ok1.data['sign_off']['checks'] if c['rule_id'] == 'STD-50'][0]
        self.assertEqual('fail', row1['result'])
        self.assertEqual(1, row1['ci_evidence']['exit_code'])
        self.assertTrue(row1['ci_evidence']['recorded_by'])

        # Case (2): Olga's verify cred records CI FAIL, then Olga recommends, then Cara tries N/A & plain approve!
        tid2, cid2, prev2, commit2 = self.deliver_task_bundle(pid, alice_tok, {'src/y.py': 'y = 2\n'}, title='S2P2 HTTP')
        self.assertEqual(201, self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit2), {
            'changed_files': ['src/y.py'],
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}],
        }, token=olga_ver).status)
        rec2 = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid2), {
            'schema_version': 1, 'operation': 'recommend', 'operation_id': 'op-r3-s2p2-rec',
            'contribution': cid2, 'commit': commit2, 'verdict': 'approve',
            'summary': 'Code logic looks ok despite pytest fail.', 'items': [],
        }, token=olga_tok)
        self.assertEqual(201, rec2.status, rec2.data)

        na2 = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid2), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-s2p2-na',
            'previous': prev2, 'contribution': cid2, 'summary': 'Cara NA over Olga fail.',
            'checks': [{'rule_id': 'STD-50', 'result': 'not_applicable', 'reason': 'Ignore Olga fail.'}],
        }, token=cara_tok)
        self.assertEqual(422, na2.status, na2.data)
        self.assertEqual(rs.refusal_na_over_mechanical_fail('STD-50', commit2), na2.data['error']['message'])

        plain2 = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid2), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-s2p2-plain',
            'previous': prev2, 'contribution': cid2, 'summary': 'Cara plain approve.',
        }, token=cara_tok)
        self.assertEqual(422, plain2.status, plain2.data)
        self.assertIn('fail without waiver', plain2.data['error']['message'])

        self.assertEqual(201, self.request('POST', '/v1/projects/%s/tasks/%s/waivers' % (pid, tid2), {
            'contribution_id': cid2, 'commit': commit2, 'rule_id': 'STD-50',
            'reason': 'Oscar waives for Cara', 'expiry': {'kind': 'delivery'},
        }, token=oscar_tok).status)
        ok2 = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid2), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-s2p2-ok',
            'previous': prev2, 'contribution': cid2, 'summary': 'Cara approves with waiver.',
        }, token=cara_tok)
        self.assertEqual(201, ok2.status, ok2.data)
        row2 = [c for c in ok2.data['sign_off']['checks'] if c['rule_id'] == 'STD-50'][0]
        self.assertEqual('fail', row2['result'])
        self.assertEqual(1, row2['ci_evidence']['exit_code'])
        self.assertTrue(row2['ci_evidence']['recorded_by'])

    def test_r3_s1_signoff_history_tally_retry_and_n1c_pins_on_both_http_backends(self):
        # s1-signoff-history + n1(c) (MR9 diff-first recorder, R6a notified_coordinator=False, NOTE-5 deleted parent)!
        pid = self.pid
        alice_uid, alice_tok = self.create_person('alice_r3s1', 'contributor', pid)
        oscar_uid, oscar_tok = self.create_person('oscar_r3s1', 'coordinator', pid)
        olga_uid, olga_tok = self.create_person('olga_r3s1', 'coordinator', pid)
        ver_a_uid, ver_a_tok = self.create_person('vera_r3s1', 'coordinator', pid)
        ver_b_uid, ver_b_tok = self.create_person('verb_r3s1', 'coordinator', pid)

        # 1) NOTE-5: child project can detach inherits when shared parent project no longer exists!
        dead_parent = self.new_project('Ephemeral Shared', 'ephemsh')
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/standards' % dead_parent, {
            'expected_version': 0, 'shared': True, 'reason': 'Shared must',
            'rules': [_rule('EPHEM-1', owner=self.owner_uid, severity='must')],
        }, token=self.owner_tok).status)
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 0, 'inherits': dead_parent, 'reason': 'Inherit ephemsh',
            'rules': [_rule('STD-1', owner=self.owner_uid, severity='must')],
        }, token=self.owner_tok).status)

        # Simulate shared parent project deletion across both backends:
        self.service.state['projects'].pop(dead_parent, None)
        if self.is_endpoint_backend:
            shutil.rmtree(self.canonical_root / 'projects' / dead_parent, ignore_errors=True)
        detach_ok = self.request('PUT', '/v1/projects/%s/standards' % pid, {
            'expected_version': 1, 'inherits': None, 'reason': 'Leave deleted parent',
            'rules': [_rule('STD-1', owner=self.owner_uid, severity='must')],
        }, token=self.owner_tok)
        self.assertEqual(200, detach_ok.status, detach_ok.data)
        self.assertIsNone(detach_ok.data['inherits'])

        # 2) R6a: non-member superuser editing a rule gets notified_coordinator == False (kills R6a)!
        su_uid = self.create_account(self.admin_tok, 'super_nonmember', 'super-pw-123456')
        self.service.state['users'][su_uid]['superuser'] = True
        self.service.store.save()
        su_tok, _, _ = self.login('super_nonmember', 'super-pw-123456')
        su_patch = self.request('PATCH', '/v1/projects/%s/standards/rules/STD-1' % pid, {
            'expected_version': 2,
            'reason': 'Superuser non-member edit.',
            'rule': _rule('STD-1', owner=self.owner_uid, severity='must', statement='Updated by non-member superuser.'),
        }, token=su_tok)
        self.assertEqual(200, su_patch.status, su_patch.data)
        self.assertIs(False, su_patch.data['notified_coordinator'])

        # 3) MR9 / R2e: ver_a records changed_files FIRST, ver_b records ci_checks SECOND -> diff_recorded_by == ver_a!
        self.request('PUT', '/v1/projects/%s/standards/gate' % pid, {'mode': 'block', 'reason': 'block'}, token=self.owner_tok)
        tid1, cid1, prev1, commit1 = self.deliver_task_bundle(pid, alice_tok, {'k.py': 'k = 1\n'}, title='S1 HTTP')
        self.assertEqual(201, self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit1), {
            'changed_files': ['k.py'],
        }, token=ver_a_tok).status)
        self.assertEqual(201, self.request('POST', '/v1/projects/%s/standards/commits/%s' % (pid, commit1), {
            'ci_checks': [{'tool': 'pytest', 'command': 'pytest', 'exit_code': 0}],
        }, token=ver_b_tok).status)

        # 4) s1-signoff-history: Oscar approves first with STD-1: not_applicable (tally = 1).
        app1_body = {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-http-s1-first',
            'previous': prev1, 'contribution': cid1, 'summary': 'Oscar first approve.',
            'checks': [{'rule_id': 'STD-1', 'result': 'not_applicable', 'reason': 'Config path only.'}],
        }
        app1 = self.request(
            'POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid1), app1_body,
            token=oscar_tok, headers={'Idempotency-Key': 'idem-oscar-1'},
        )
        self.assertEqual(201, app1.status, app1.data)
        self.assertEqual(ver_a_uid, app1.data['sign_off']['diff_recorded_by'])
        self.assertEqual(1, app1.data['sign_off']['reviewer_must_not_applicable_total'])

        # Olga approves second on the SAME contribution with STD-1: pass!
        prev_after_app1 = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (pid, tid1), token=olga_tok).data['review']['latest_id']
        app2 = self.request(
            'POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid1), {
                'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-http-s1-second',
                'previous': prev_after_app1, 'contribution': cid1, 'summary': 'Olga second approve.',
                'checks': [{'rule_id': 'STD-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['k.py:1']}}],
            },
            token=olga_tok, headers={'Idempotency-Key': 'idem-olga-2'},
        )
        self.assertEqual(201, app2.status, app2.data)
        self.assertEqual(0, app2.data['sign_off']['reviewer_must_not_applicable_total'])

        # Exact retry of Oscar's first approve (with a NEW Idempotency-Key so it reaches the backend!)
        # returns Oscar's OWN sign-off, NOT Olga's!
        retry1 = self.request(
            'POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid1), app1_body,
            token=oscar_tok, headers={'Idempotency-Key': 'idem-oscar-retry-new-key'},
        )
        self.assertIn(retry1.status, (200, 201), retry1.data)
        self.assertEqual('op-http-s1-first', retry1.data['sign_off']['operation_id'])
        self.assertEqual(oscar_uid, retry1.data['sign_off']['reviewer']['actor'])
        self.assertEqual('not_applicable', retry1.data['sign_off']['checks'][0]['result'])
        self.assertEqual(1, retry1.data['sign_off']['reviewer_must_not_applicable_total'])

        # GET sign-off & sign-off.md return Olga as latest + Oscar in earlier_sign_offs!
        so_get = self.request('GET', '/v1/projects/%s/tasks/%s/sign-off' % (pid, tid1), token=oscar_tok).data
        self.assertEqual('op-http-s1-second', so_get['operation_id'])
        self.assertEqual(olga_uid, so_get['reviewer']['actor'])
        self.assertEqual(1, len(so_get['earlier_sign_offs']))
        self.assertEqual('op-http-s1-first', so_get['earlier_sign_offs'][0]['operation_id'])
        self.assertEqual(oscar_uid, so_get['earlier_sign_offs'][0]['reviewer']['actor'])

        so_md = self.request('GET', '/v1/projects/%s/tasks/%s/sign-off.md' % (pid, tid1), token=oscar_tok).body.decode('utf-8')
        self.assertIn('Earlier Approvals on This Delivery', so_md)
        self.assertIn(oscar_uid, so_md)

        # Oscar's must-N/A tally survived Olga's second approve: Oscar's next N/A approval shows total == 2!
        tid2, cid2, prev2, _ = self.deliver_task_bundle(pid, alice_tok, {'k2.py': 'k = 2\n'}, title='S1 HTTP T2')
        app_t2 = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid2), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-http-s1-t2',
            'previous': prev2, 'contribution': cid2, 'summary': 'Oscar second task N/A.',
            'checks': [{'rule_id': 'STD-1', 'result': 'not_applicable', 'reason': 'Also config only.'}],
        }, token=oscar_tok)
        self.assertEqual(201, app_t2.status, app_t2.data)
        self.assertEqual(2, app_t2.data['sign_off']['reviewer_must_not_applicable_total'])


class HttpStandardsInProcessSuite(_HttpStandardsMixin, Harness):
    """All HTTP standards scenarios against InProcessBackend."""

    is_endpoint_backend = False

    def make_backend(self):
        self.delivery_root = self.tmp / 'bundles'
        self.delivery_root.mkdir(parents=True, exist_ok=True)
        return http_service.InProcessBackend(self.service, delivery_roots=[str(self.delivery_root)])

    def setUp(self):
        super().setUp()
        self.admin_tok = self.admin_token()
        self.owner_uid = self.create_account(self.admin_tok, 'olive', 'olive-pw-123456')
        self.owner_tok, _, _ = self.login('olive', 'olive-pw-123456')
        self.pid = self.new_project('Main Project', 'mainproj')


@POSIX
class HttpStandardsEndpointSuite(_HttpStandardsMixin, Harness):
    """All HTTP standards scenarios against EndpointBackend + real endpoint.py + real git."""

    is_endpoint_backend = True

    def make_backend(self):
        self.delivery_root = self.tmp / 'bundles'
        self.delivery_root.mkdir(parents=True, exist_ok=True)
        self.canonical_root = self.tmp / 'canonical'
        (self.canonical_root / 'projects').mkdir(parents=True, exist_ok=True)
        (self.canonical_root / 'deployment.private.json').write_text(json.dumps({
            'password': 'secret',
            'unit': 'none',
            'operators': [],
            'verifiers': [],
            'review_workflow_writes': True,
            'checkpoint_provenance_writes': True,
            'delivery_roots': [str(self.delivery_root)],
        }), encoding='utf-8')
        self.trackers = {}
        return FastRealEndpointBackend(self.canonical_root, service=self.service, trackers=self.trackers)

    def setUp(self):
        super().setUp()
        self.admin_tok = self.admin_token()
        self.owner_uid = self.create_account(self.admin_tok, 'olive', 'olive-pw-123456')
        self.owner_tok, _, _ = self.login('olive', 'olive-pw-123456')
        self.pid = self.new_project('Main Project', 'mainproj')


class WebAndRouteSweepStandardsTests(Harness):
    """Route-sweep confinement for verify credentials + Node browser UI execution."""

    def make_backend(self):
        self.delivery_root = self.tmp / 'bundles'
        self.delivery_root.mkdir(parents=True, exist_ok=True)
        return http_service.InProcessBackend(self.service, delivery_roots=[str(self.delivery_root)])

    def setUp(self):
        super().setUp()
        self.admin_tok = self.admin_token()
        self.owner_uid = self.create_account(self.admin_tok, 'olive', 'olive-pw-123456')
        self.owner_tok, _, _ = self.login('olive', 'olive-pw-123456')
        r = self.request('POST', '/v1/projects', {'name': 'Sweep Project'}, token=self.owner_tok)
        self.assertEqual(201, r.status, r.data)
        self.pid = r.data['id']

    def test_every_other_route_refuses_a_verify_credential(self):
        # Plan 3 Item 3: verify credential gets NO CAP_READ and NO grants, and is refused (403)
        # on every non-anonymous route except standards_commit_record!
        self.assertEqual(frozenset({'standards.verify'}), http_authority.SCOPE_CAPABILITIES['verify'])
        for role, held in http_authority.ROLE_CAPABILITIES.items():
            self.assertNotIn('standards.verify', held, role)

        issued = self.request('POST', '/v1/projects/%s/worker-credentials' % self.pid, {
            'scopes': ['verify'], 'label': 'Sweep Verifier',
        }, token=self.owner_tok)
        self.assertEqual(201, issued.status, issued.data)
        secret = issued.data['credential']['secret']
        cred_id = issued.data['credential']['id']

        # Kills MQ directly at http_authority.credential_capabilities and http_authority.decide!
        cred_rec = self.service.state['credentials'][cred_id]
        owner_user = self.service.state['users'][self.owner_uid]
        cred_caps = http_authority.credential_capabilities(self.service.state, cred_rec, owner_user, self.pid)
        self.assertEqual(frozenset({http_authority.CAP_STANDARDS_VERIFY}), cred_caps)
        self.assertNotIn(http_authority.CAP_READ, cred_caps)
        ver_principal = self.service.authenticate(secret)
        with self.assertRaises(http_authority.AuthorityDenied) as cm_mq:
            http_authority.decide(
                self.service.state,
                http_auth.authority_request(
                    ver_principal, self.pid, http_authority.CAP_READ, now=self.service._expiry_now()
                ),
            )
        self.assertEqual(403, cm_mq.exception.status)

        swept, open_routes = 0, []
        for verb, pattern, name, anonymous, _csrf in http_service.ROUTES:
            if anonymous or name in http_service.VERIFY_ROUTES:
                continue
            path = fill(pattern)
            with self.subTest(route=name):
                self.assertIsNotNone(path, 'no sample path matches %s' % pattern.pattern)
                answer = self.request(verb, path, {} if verb != 'GET' else None, token=secret)
                swept += 1
                if answer.status != 403:
                    open_routes.append((name, answer.status))
                self.assertEqual(403, answer.status, '%s answered %s' % (name, answer.status))
        self.assertGreater(swept, 100)
        self.assertEqual([], open_routes)
        self.assertEqual({'standards_commit_record'}, http_service.VERIFY_ROUTES)

    def test_web_standards_forms_gate_waiver_rule_edit_and_draft_decision_under_node(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        shim_uri = (ROOT / 'tests' / 'web_dom_shim.mjs').as_uri()
        standards_uri = (WEB / 'js' / 'views' / 'standards.js').as_uri()
        task_uri = (WEB / 'js' / 'views' / 'task.js').as_uri()
        project_uri = (WEB / 'js' / 'views' / 'project.js').as_uri()

        script = """
const [shimUrl, standardsUrl, taskUrl, projectUrl] = process.argv.slice(1);
const { Element } = await import(shimUrl);
Element.prototype.showModal = function() { this.open = true; };
Element.prototype.close = function() { this.open = false; };
globalThis.window = { isSecureContext: false, location: { origin: 'http://127.0.0.1' } };
const stdView = await import(standardsUrl);
const taskView = await import(taskUrl);
const projView = await import(projectUrl);

const calls = [];
const ctx = {
  me: { id: 'usr_0000000000000001', username: 'cora', display_name: 'Cora' },
  setDirty() {},
  render() { calls.push(['render']); },
  href: (p) => p,
  api: {
    project: async (pid) => ({ id: pid, name: 'Demo', role: 'coordinator', capabilities: ['project.read', 'project.coordinate', 'reviews.approve', 'tasks.write', 'reviews.write'], archived: false, standards_gate: { mode: 'warn' } }),
    standards: async () => ({
      status: 'active',
      version: 2,
      gate: { mode: 'warn' },
      effective_rules: [
        { id: 'STD-1', owner: 'usr_0000000000000001', severity: 'must', statement: 'Check bounds.', rationale: 'Safety.', applicability: { all_changes: true }, check: { kind: 'judgement' }, origin: { kind: 'project', version: 2 } },
      ],
      rules: [
        { id: 'STD-1', owner: 'usr_0000000000000001', severity: 'must', statement: 'Check bounds.', rationale: 'Safety.', applicability: { all_changes: true }, check: { kind: 'judgement' }, origin: { kind: 'project', version: 2 } },
      ],
      lint_warnings: [],
    }),
    standardsDrafts: async () => ({
      items: [
        { id: 'draft-0000000000000001', status: 'pending', action: 'upsert', rule_id: 'STD-2', reason: 'Add STD-2', proposed_by: 'agent_1', created_at: '2026-11-11T00:00:00Z', rule: { id: 'STD-2' } },
      ],
    }),
    standardsVersions: async () => ({ versions: [{ version: 1, author: 'usr_1', reason: 'v1', created_at: '2026-11-10T00:00:00Z' }] }),
    setStandardsGate: async (pid, body) => { calls.push(['setStandardsGate', pid, body]); return { mode: body.mode }; },
    editStandardsRule: async (pid, rid, body) => { calls.push(['editStandardsRule', pid, rid, body]); return { version: 3 }; },
    decideStandardsDraft: async (pid, did, body) => { calls.push(['decideStandardsDraft', pid, did, body]); return { register: { version: 4 } }; },
    grantTaskWaiver: async (pid, tid, body) => { calls.push(['grantTaskWaiver', pid, tid, body]); return { id: 'waiver-1' }; },
    review: async (pid, tid, body) => { calls.push(['review', pid, tid, body]); return { id: 'rev-2' }; },
    signOffMarkdownUrl: (pid, tid) => `/v1/projects/${pid}/tasks/${tid}/sign-off.md`,
    signOffJsonUrl: (pid, tid) => `/v1/projects/${pid}/tasks/${tid}/sign-off`,
  },
};

// 1) Render full standards page
const pageNode = await stdView.page(ctx, { pid: 'p1' });
const pageText = pageNode.textContent;

// 2) Submit gate form
const gf = stdView.gateForm(ctx, 'p1', { mode: 'warn' });
gf.querySelector('#sg-mode').value = 'block';
gf.querySelector('#sg-reason').value = 'Ready for block gate';
await gf.dispatch('submit');

// 3) Submit rule-edit form
const rf = stdView.ruleEditForm(ctx, 'p1', { id: 'STD-1', owner: 'usr_0000000000000001', severity: 'must', statement: 'Old', rationale: 'Why', applicability: { all_changes: true }, check: { kind: 'judgement' } }, 2);
rf.querySelector('#re-reason-STD-1').value = 'Clarify statement';
await rf.dispatch('submit');

// 4) Accept draft via draftDecisionForm
const df = stdView.draftDecisionForm(ctx, 'p1', { id: 'draft-0000000000000001' }, 2);
df.querySelector('#dd-reason-draft-0000000000000001').value = 'Looks good';
const acceptBtn = df.all((el) => el.tagName === 'BUTTON' && el.getAttribute('data-decision') === 'accept')[0];
await acceptBtn.listeners.click[0]({ currentTarget: acceptBtn, preventDefault() {} });

// 5) Submit waiverForm with date expiry
const wf = taskView.waiverForm(ctx, 'p1', 't-1', { id: '14', commit: 'a'.repeat(40) }, { applicable_rules: [{ id: 'STD-1', severity: 'must', statement: 'Check bounds.' }] });
wf.querySelector('#w-scope').value = 'until';
wf.querySelector('#w-until').value = '2026-12-31';
wf.querySelector('#w-reason').value = 'Hotfix window';
await wf.dispatch('submit');

// 6) Approve adopting recommendation via task.js reviewActions (auto-confirm modal!)
const ra = taskView.reviewActions(
  ctx, 'p1', 't-1',
  { id: '14', revision: 1, commit: 'b'.repeat(40) },
  { latest_id: '14', recommendations: [{ id: '27', author: 'agent_rev', checks: [{ rule_id: 'STD-1', result: 'pass' }] }] },
  { status: 'active' },
);
const approveBtn = ra.all((el) => el.tagName === 'BUTTON' && el.textContent === 'Approve')[0];
const approvePromise = approveBtn.listeners.click[0]({ currentTarget: approveBtn, preventDefault() {} });
await new Promise((r) => setTimeout(r, 10));
const confirmBtn = document.body.all((el) => el.tagName === 'BUTTON' && el.textContent === 'Approve').slice(-1)[0];
if (confirmBtn && confirmBtn.listeners.click) {
  await confirmBtn.listeners.click[0]({ currentTarget: confirmBtn, preventDefault() {} });
}
await approvePromise;

console.log(JSON.stringify({ pageText, calls }));
"""
        done = run_node_module(self, node, script, shim_uri, standards_uri, task_uri, project_uri)
        self.assertEqual(0, done.returncode, done.stderr)
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertIn('Effective register (v2)', out['pageText'])
        self.assertIn('STD-1', out['pageText'])

        by_kind = {}
        for item in out['calls']:
            by_kind.setdefault(item[0], []).append(item[1:])

        self.assertEqual([['p1', {'mode': 'block', 'reason': 'Ready for block gate'}]], by_kind.get('setStandardsGate'))
        self.assertEqual(2, by_kind['editStandardsRule'][0][2]['expected_version'])
        self.assertEqual([['p1', 'draft-0000000000000001', {'decision': 'accept', 'reason': 'Looks good', 'expected_version': 2}]], by_kind.get('decideStandardsDraft'))
        self.assertEqual([[
            'p1', 't-1',
            {
                'contribution_id': '14',
                'rule_id': 'STD-1',
                'reason': 'Hotfix window',
                'expiry': {'kind': 'date', 'date': '2026-12-31'},
                'commit': 'a' * 40,
            },
        ]], by_kind.get('grantTaskWaiver'))
        # Verify task.js sends adopt_recommendation as string '27', never coerced to int!
        rev_body = by_kind['review'][0][2]
        self.assertEqual('approve', rev_body['operation'])
        self.assertEqual('27', rev_body['adopt_recommendation'])

    def test_web_task_page_fetches_full_signoff_from_compact_brief_and_renders_lint_warning_messages(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        shim_uri = (ROOT / 'tests' / 'web_dom_shim.mjs').as_uri()
        standards_uri = (WEB / 'js' / 'views' / 'standards.js').as_uri()
        task_uri = (WEB / 'js' / 'views' / 'task.js').as_uri()

        script = """
const [shimUrl, standardsUrl, taskUrl] = process.argv.slice(1);
await import(shimUrl);
globalThis.window = { isSecureContext: false, location: { origin: 'http://127.0.0.1' } };
const stdView = await import(standardsUrl);
const taskView = await import(taskUrl);

let signOffCallsWithReg = 0;
let signOffCallsNoReg = 0;

// 1) Task page starting from COMPACT brief (sign_off summary has NO checks array -> must fetch taskSignOff!)
const ctxWithReg = {
  me: { id: 'usr_1', username: 'cora', display_name: 'Cora' },
  setDirty() {},
  render() {},
  href: (p) => p,
  api: {
    project: async (pid) => ({ id: pid, name: 'RegProj', role: 'coordinator', capabilities: ['project.read', 'project.coordinate', 'reviews.approve', 'tasks.write', 'reviews.write'], archived: false }),
    brief: async () => ({
      task: { id: 't-1', title: 'Approved Task', status: 'open', priority: 2, assignee: 'usr_2', version: 3 },
      review: {
        state: 'approved',
        latest_id: 'rev_9',
        contribution: { id: 'con_1', revision: 1, commit: 'a'.repeat(40), author: 'usr_2', summary: 'Fix.' },
        requests: [],
      },
      standards: {
        status: 'active',
        register_version: 2,
        gate_mode: 'block',
        diff_status: 'verified',
        applicable_rules: [],
        conditional_rules: [],
        sign_off: {
          headline: 'Checked against standards register v1',
          register_version: 1,
          gate_mode: 'block',
          would_have_refused: [],
        },
      },
      lifecycle: {},
      depends_on: [],
    }),
    history: async () => ({ items: [], next_cursor: null }),
    taskFiles: async () => ({ files: [] }),
    taskSignOff: async () => {
      signOffCallsWithReg += 1;
      return {
        contribution_id: 'con_1',
        register_version: 1,
        gate_mode: 'block',
        diff_status: 'verified',
        diff_recorded_by: 'usr_oscar',
        reviewer: { actor: 'usr_olga' },
        would_have_refused: [],
        checks: [
          {
            rule_id: 'STD-1',
            statement: 'Bounds checked.',
            severity: 'must',
            check_kind: 'judgement',
            result: 'pass',
            source: 'adopted-recommendation',
            evidence: { kind: 'citations', citations: ['src/app.py:10-14'] },
            waiver: null,
          },
          {
            rule_id: 'STD-50',
            statement: 'Ruff passes.',
            severity: 'must',
            check_kind: 'mechanical',
            result: 'fail',
            source: 'verifier-recorded',
            evidence: { kind: 'ci', status: 'fail', tool: 'ruff', command: 'ruff check', exit_code: 1, recorded_by: 'usr_oscar' },
            ci_evidence: { kind: 'ci', status: 'fail', tool: 'ruff', command: 'ruff check', exit_code: 1, recorded_by: 'usr_oscar' },
            waiver: { id: 'waiver-77', granted_by: 'usr_cora', reason: 'Legacy lint exemption' },
          },
        ],
        earlier_sign_offs: [
          {
            operation_id: 'op-prev-1',
            created_at: '2026-11-11T12:00:00Z',
            gate_mode: 'warn',
            must_not_applicable_count: 1,
            reviewer_must_not_applicable_total: 1,
            would_have_refused: ['STD-50 (unverified)'],
            reviewer: { actor: 'usr_oscar' },
            checks: [
              {
                rule_id: 'STD-1',
                statement: 'Bounds checked.',
                severity: 'must',
                check_kind: 'judgement',
                result: 'not_applicable',
                source: 'reviewer',
                evidence: { kind: 'not_applicable', reason: 'Docs only' },
                waiver: null,
              },
            ],
          },
        ],
      };
    },
  },
};

const taskDom = await taskView.detail(ctxWithReg, { pid: 'p1', tid: 't-1' });
const taskText = taskDom.textContent;

// 2) Approved task on a project WITHOUT brief.standards -> must NOT call taskSignOff (0 calls)!
const ctxNoReg = {
  ...ctxWithReg,
  api: {
    ...ctxWithReg.api,
    brief: async () => ({
      task: { id: 't-0', title: 'No Reg Task', status: 'open', priority: 2, assignee: 'usr_2', version: 2 },
      review: {
        state: 'approved',
        latest_id: 'rev_1',
        contribution: { id: 'con_0', revision: 1, commit: 'b'.repeat(40), author: 'usr_2', summary: 'Plain.' },
        requests: [],
      },
      lifecycle: {},
      depends_on: [],
    }),
    taskSignOff: async () => {
      signOffCallsNoReg += 1;
      return null;
    },
  },
};
await taskView.detail(ctxNoReg, { pid: 'p0', tid: 't-0' });

// 3) Standards page with object lint_warnings -> renders message text, never '[object Object]'!
const ctxLint = {
  ...ctxWithReg,
  api: {
    ...ctxWithReg.api,
    standards: async () => ({
      status: 'active',
      version: 1,
      gate: { mode: 'warn' },
      effective_rules: [],
      rules: [],
      lint_warnings: [{ code: 'must_count_exceeded', message: 'Register defines 6 all_changes must rules (limit 5).' }],
    }),
    standardsDrafts: async () => ({ items: [] }),
    standardsVersions: async () => ({ items: [] }),
  },
};
const stdDom = await stdView.page(ctxLint, { pid: 'p1' });
const stdText = stdDom.textContent;

console.log(JSON.stringify({ signOffCallsWithReg, signOffCallsNoReg, taskText, stdText }));
"""
        done = run_node_module(self, node, script, shim_uri, standards_uri, task_uri)
        self.assertEqual(0, done.returncode, done.stderr)
        out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(1, out['signOffCallsWithReg'])
        self.assertEqual(0, out['signOffCallsNoReg'])
        for expected in (
            'Checked against standards register v1',
            'Approver: usr_olga',
            'STD-1',
            'Bounds checked.',
            'citations: src/app.py:10-14',
            'adopted-recommendation',
            'STD-50',
            'Ruff passes.',
            'CI ruff: ruff check exit 1 (recorded by usr_oscar)',
            'Waived by usr_cora: Legacy lint exemption',
            'Diff: verified (usr_oscar)',
            'Earlier approvals on this delivery (1)',
            'Approved by usr_oscar',
            'must N/A: 1 (1 total)',
            'Warn mode — would have refused: STD-50 (unverified)',
            'reason: Docs only',
        ):
            self.assertIn(expected, out['taskText'])
        self.assertIn('Register defines 6 all_changes must rules (limit 5).', out['stdText'])
        self.assertNotIn('[object Object]', out['stdText'])


if __name__ == '__main__':
    unittest.main()
