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
from test_http_review_fixes import Harness
from test_http_web import WEB, run_node_module
from test_lane_nudge_carrier import fill

_ORIG_HASH_PASSWORD = http_auth.hash_password


def setUpModule():
    http_auth.hash_password = lambda pw, **kw: _ORIG_HASH_PASSWORD(
        pw, n=16, r=8, p=1, minimum=kw.get('minimum', http_auth.MIN_PASSWORD)
    )
    http_auth._DUMMY_VERIFIER = http_auth.hash_password('orchestra-dummy-password')


def tearDownModule():
    http_auth.hash_password = _ORIG_HASH_PASSWORD
    http_auth._DUMMY_VERIFIER = None

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
        self.assertIn(self_app.status, (403, 422), self_app.data)
        self.assertEqual(rs.refusal_self_recorded_commit_observation(cid, commit), self_app.data['error']['message'])

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
                self.assertIn(att.status, (403, 422), att.data)
                self.assertEqual(rs.refusal_self_recorded_commit_observation(cid, commit), att.data['error']['message'])

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


if __name__ == '__main__':
    unittest.main()
