"""Unit, endpoint, admin, and git bundle/branch tests for Phase 1 Review Standards (kittrial-5bb.570)."""
import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))

import admin
import briefing
import review_recommendations as rr
import review_standards as rs
import review_workflow as rw
import sessions
import work
from requirements import canonical_bytes

try:
    import endpoint
except ImportError:
    endpoint = None

POSIX = unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')


class _Completed:
    def __init__(self, stdout='', returncode=0, stderr=''):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class HostTracker:
    """In-memory bd tracker driving endpoint.execute, admin_standards_command, work, and briefing."""

    def __init__(self, project='proj'):
        self.project = project
        self.rows = {}
        self.seq = 0
        self.cseq = 0
        self.uuid_comments = False

    def create_task(self, title='Implement feature', assignee='worker-bob', labels=None, issue_type='task'):
        self.seq += 1
        tid = '%s-%d' % (self.project, self.seq)
        row = {
            'id': tid,
            'title': title,
            'description': 'Task description.',
            'issue_type': issue_type,
            'status': 'in_progress',
            'priority': 2,
            'assignee': assignee,
            'labels': list(labels or []),
            'comments': [],
            'dependencies': [],
            'created_by': 'cora-op',
            'created_at': '2026-11-10T00:00:00Z',
            'updated_at': '2026-11-10T00:00:00Z',
        }
        self.rows[tid] = row
        return tid

    def run_argv(self, argv):
        """Execute internal bd argv list (without --sandbox prefix)."""
        cmd = list(argv)
        actor = 'system'
        if cmd[:1] == ['--actor']:
            actor = cmd[1]
            cmd = cmd[2:]
        if cmd[:2] == ['export', '--all']:
            if not self.rows:
                return ''
            return '\n'.join(json.dumps(r) for r in self.rows.values()) + '\n'
        if cmd[:1] == ['show']:
            ids = [t for t in cmd[1:] if not t.startswith('--')]
            found = [self.rows[i] for i in ids if i in self.rows]
            if not found:
                raise ValueError('task not found: %s' % ids)
            if '--include-comments' in cmd or len(ids) > 1:
                return json.dumps(found)
            return json.dumps(found[0])
        if cmd[:1] == ['list']:
            return json.dumps(list(self.rows.values()))
        if cmd[:2] == ['comments', 'add']:
            tid = cmd[2]
            text = cmd[3]
            if tid not in self.rows:
                raise ValueError('task not found: %s' % tid)
            self.cseq += 1
            cid = (
                '01a1cfb5-7594-750b-b717-%012x' % self.cseq
                if self.uuid_comments
                else '%s-c%04d' % (tid, self.cseq)
            )
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
            row = self.rows[tid]
            idx = 2
            while idx < len(cmd):
                flag = cmd[idx]
                if flag in ('--status', '--assignee', '--title', '--description'):
                    row[flag[2:]] = cmd[idx + 1]
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

    def native_run(self, command, env=None, **kwargs):
        if '--sandbox' in command:
            rest = list(command[command.index('--sandbox') + 1:])
        else:
            rest = list(command[1:])
        try:
            out = self.run_argv(rest)
            return _Completed(stdout=out, returncode=0, stderr='')
        except Exception as exc:
            return _Completed(stdout='', returncode=1, stderr=str(exc))


def _build_admin_standards_parser():
    p = argparse.ArgumentParser(allow_abbrev=False)
    p.add_argument('standards_op', choices=[
        'show', 'history', 'lint', 'draft-list', 'waivers', 'sign-off',
        'notifications', 'put', 'rule-edit',
        'set-gate', 'waive', 'draft-decide', 'record-commit', 'owner-departed',
    ])
    p.add_argument('--project', required=True)
    p.add_argument('--actor', default=None)
    p.add_argument('--file', default=None)
    p.add_argument('--reason', default=None)
    p.add_argument('--mode', choices=['warn', 'block'], default=None)
    p.add_argument('--rule', default=None)
    p.add_argument('--task', default=None)
    p.add_argument('--contribution', default=None)
    p.add_argument('--commit', default=None)
    p.add_argument('--until', default=None)
    p.add_argument('--draft', default=None)
    p.add_argument('--decision', choices=['accept', 'reject'], default=None)
    p.add_argument('--status', choices=['pending', 'accepted', 'rejected'], default=None)
    p.add_argument('--format', choices=['json', 'text', 'markdown'], default='json', dest='std_format')
    p.add_argument('--owner', default=None)
    p.add_argument('--version', type=int, default=None)
    p.add_argument('--expected-version', type=int, default=None, dest='expected_version')
    return p


_ADMIN_STD_PARSER = _build_admin_standards_parser()


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


def _bundle_delivery_path(bpath):
    s = str(bpath)
    return 'server:' + s if s.startswith('/') else s


def _sample_rule(
    rule_id='STD-1',
    owner='person:alice',
    severity=None,
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
        'applicability': applicability if applicability is not None else {'all_changes': True},
        'check': check if check is not None else {'kind': 'judgement'},
    }
    if severity is not None:
        r['severity'] = severity
    if references is not None:
        r['references'] = list(references)
    return r


class ReviewStandardsHostCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'runtime'
        self.delivery_root = Path(self.tmp.name) / 'bundles'
        self.delivery_root.mkdir(parents=True)
        self.pid = 'proj'
        self.project_path = self.root / 'projects' / self.pid
        (self.project_path / '.beads').mkdir(parents=True)
        (self.project_path / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        self.shared_pid = 'sharedstd'
        self.shared_path = self.root / 'projects' / self.shared_pid
        (self.shared_path / '.beads').mkdir(parents=True)
        (self.shared_path / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        self.operators = ['cora-op', 'olive-op']
        self.verifiers = ['ci-verifier']
        self.serving_repos = {}
        self._write_deployment()
        self.trackers = {
            self.pid: HostTracker(self.pid),
            self.shared_pid: HostTracker(self.shared_pid),
        }

    def _write_deployment(self, **extra):
        cfg = {
            'password': 'secret',
            'unit': 'none',
            'operators': list(self.operators),
            'verifiers': list(self.verifiers),
            'review_workflow_writes': True,
            'checkpoint_provenance_writes': True,
            'delivery_roots': [str(self.delivery_root)],
            'serving_repos': dict(self.serving_repos),
            'coordinator_rules': {
                self.pid: {
                    'agent_approval': 'by-agent',
                    'require_independent_party': True,
                }
            },
        }
        cfg.update(extra)
        (self.root / 'deployment.private.json').write_text(json.dumps(cfg), encoding='utf-8')

    def store(self, pid=None):
        target_pid = pid or self.pid
        p_dir = self.root / 'projects' / target_pid

        def _shared(spid):
            sp = self.root / 'projects' / spid
            if not (sp / '.beads' / 'metadata.json').is_file():
                return None
            return rs.HostStandardsStore(sp, spid)

        return rs.HostStandardsStore(p_dir, target_pid, shared_loader=_shared)

    def init_git_repo(self, repo_path, files):
        repo = Path(repo_path)
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, 'init')
        for rel, content in files.items():
            fp = repo / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding='utf-8')
            _git(repo, 'add', rel)
        _git(repo, 'commit', '-m', 'init')

    def admin_std(self, op, *rest, project=None, data=None, actor=None):
        target_pid = project or self.pid
        p_dir = self.root / 'projects' / target_pid
        tr = self.trackers[target_pid]
        argv = [op, '--project', target_pid] + [str(x) for x in rest]
        if data is not None:
            self._admin_tmp_seq = getattr(self, '_admin_tmp_seq', 0) + 1
            tf = Path(self.tmp.name) / ('admin-data-%d.json' % self._admin_tmp_seq)
            tf.write_text(json.dumps(data), encoding='utf-8')
            argv.extend(['--file', str(tf)])
        if actor is not None:
            argv.extend(['--actor', actor])
        elif '--actor' not in argv and op in rs.PERSON_ONLY_SUBCOMMANDS:
            argv.extend(['--actor', 'cora-op'])
        parsed = _ADMIN_STD_PARSER.parse_args(argv)
        return rs.admin_standards_command(
            self.root,
            p_dir,
            target_pid,
            parsed,
            lambda _r, _p, argv_in: tr.run_argv(argv_in),
            list(self.operators),
            list(self.verifiers),
        )

    def endpoint_call(self, action, args, *, actor='worker-bob', project=None, attachments=None, key_principal=None, extra=None, raw_raise=False):
        target_pid = project or self.pid
        tr = self.trackers[target_pid]
        req = {
            'project': target_pid,
            'actor': actor,
            'action': action,
            'args': list(args),
        }
        if attachments is not None:
            if isinstance(attachments, list):
                req['attachments'] = {str(i): {'flag': '--file', 'text': v} for i, v in enumerate(attachments)}
            else:
                req['attachments'] = dict(attachments)
        if extra:
            req.update(extra)
        with mock.patch.object(endpoint.native, 'run', side_effect=tr.native_run):
            if raw_raise:
                return endpoint.execute(self.root, req, key_principal=key_principal)
            try:
                return endpoint.execute(self.root, req, key_principal=key_principal)
            except (ValueError, rw.CoordinatorRuleRefusal) as exc:
                ans = {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: %s\n' % exc}
                if isinstance(exc, rw.CoordinatorRuleRefusal):
                    ans['refused'] = 'coordinator-rule'
                    if type(getattr(exc, 'status', None)) is int:
                        ans['standards_status'] = exc.status
                return ans

    def chain_info(self, tid, project=None):
        import review_workflow
        target_pid = project or self.pid
        issue = self.trackers[target_pid].rows[tid]
        ordered, voids, invalid, refused, positions, reverts, invalid_reverts = review_workflow.history(
            issue, self.operators, self.root / 'projects' / target_pid
        )
        return review_workflow.projection(
            ordered, voids, invalid, refused, positions, reverts, invalid_reverts,
            closed=(issue.get('status') == 'closed'),
        )

    def review_op(self, payload, *, actor='cora-op', project=None, key_principal=None):
        p = dict(payload)
        if p.get('operation') == 'approve':
            if 'target' in p and 'contribution' not in p:
                p['contribution'] = p.pop('target')
            if 'previous' not in p:
                p['previous'] = self.chain_info(p['task'], project=project)['latest_comment_id']
        raw = json.dumps(p)
        return self.endpoint_call(
            'review',
            [p['task'], '@attachment:0'],
            actor=actor,
            project=project,
            attachments=[raw],
            key_principal=key_principal,
        )

    def contribute_bundle(self, tid, repo_dir, *, actor='worker-bob', base_commit=None, summary='Delivered fix', op_id='op-contrib-1', extra_fields=None):
        bpath = self.delivery_root / ('%s-%s.bundle' % (tid, op_id))
        commit, sha = _make_bundle(repo_dir, bpath)
        cinfo = self.chain_info(tid)
        cur_cid = (cinfo.get('contribution') or {}).get('comment_id')
        payload = {
            'schema_version': 1,
            'operation': 'contribute',
            'operation_id': op_id,
            'task': tid,
            'previous': cinfo['latest_comment_id'],
            'supersedes': cur_cid,
            'repository': 'https://example.invalid/repo.git',
            'commit': commit,
            'base_commit': base_commit or commit,
            'delivery': {
                'kind': 'bundle',
                'path': _bundle_delivery_path(bpath),
                'sha256': sha,
            },
            'summary': summary,
        }
        if extra_fields:
            payload.update(extra_fields)
        ans = self.review_op(payload, actor=actor)
        return ans, commit, bpath


@POSIX
class ReviewStandardsSuite(ReviewStandardsHostCase):

    def test_project_without_register_behaves_identically_to_today(self):
        tid = self.trackers[self.pid].create_task('Baseline task')
        repo = Path(self.tmp.name) / 'repo-noreg'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'app.py').write_text('x = 1\n', encoding='utf-8')
        _git(repo, 'add', 'app.py')
        _git(repo, 'commit', '-m', 'init')
        base = _git(repo, 'rev-parse', 'HEAD')
        (repo / 'app.py').write_text('x = 2\n', encoding='utf-8')
        _git(repo, 'add', 'app.py')
        _git(repo, 'commit', '-m', 'step2')

        # Before any .standards/ directory exists: brief has no 'standards' key and writes nothing.
        b_ans = self.endpoint_call('brief', ['--json', tid], actor='worker-bob')
        self.assertEqual(0, b_ans['returncode'], b_ans['stderr'])
        b_data = json.loads(b_ans['stdout'])
        self.assertNotIn('standards', b_data)
        self.assertFalse((self.project_path / '.standards').exists())

        ans, commit, _ = self.contribute_bundle(
            tid, repo, base_commit=base, op_id='op-c0',
            extra_fields={'declared_tool': 'codex', 'declared_model': 'gpt-5'},
        )
        self.assertEqual(0, ans['returncode'], ans['stderr'])
        contrib_id = json.loads(ans['stdout'])['comment_id']
        self.assertFalse((self.project_path / '.standards').exists())

        # PM VERIFIED BINDING SCENARIO:
        # On a project with NO register (no head.json and no versions/), run:
        # 1) draft-propose, 2) set-gate block, 3) record-commit, 4) approve.
        # It MUST behave identically to today: rc=0, no sign-off written, nothing refused,
        # and brief still carries zero 'standards' key!
        draft_body = json.dumps({
            'action': 'upsert',
            'reason': 'Propose rule before v1 exists.',
            'rule': _sample_rule('PRE-1', severity='must'),
        })
        d_ans = self.endpoint_call(
            'standards', ['draft-propose', '@attachment:0'],
            actor='worker-bob', attachments=[draft_body],
        )
        self.assertEqual(0, d_ans['returncode'], d_ans['stderr'])

        self.admin_std('set-gate', '--mode', 'block', '--actor', 'cora-op', '--reason', 'Pre-stage gate')

        ci_file = Path(self.tmp.name) / 'ci-pre.json'
        ci_file.write_text(json.dumps({
            'changed_files': ['app.py'],
            'ci_checks': [{'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}],
        }), encoding='utf-8')
        self.admin_std('record-commit', '--commit', commit, '--file', str(ci_file), '--actor', 'ci-verifier')

        # Confirm .standards/ exists on disk with drafts/, gate.json, commits/ — yet register status is 'none'!
        self.assertTrue((self.project_path / '.standards').is_dir())
        self.assertEqual('none', rs.resolve_effective_register(self.store())['status'])
        self.assertEqual('none', self.store().inspect_register_raw()[0])

        b_after = json.loads(self.endpoint_call('brief', ['--json', tid], actor='cora-op')['stdout'])
        self.assertNotIn('standards', b_after)

        app_ans = self.review_op({
            'schema_version': 1,
            'operation': 'approve',
            'operation_id': 'op-app-noreg',
            'task': tid,
            'target': contrib_id,
            'summary': 'Looks good with no register.',
            'checks': [{'rule_id': 'STD-99', 'result': 'pass'}],
            'declared_tool': 'claude',
            'declared_model': 'claude-opus-4-6',
        }, actor='cora-op')
        self.assertEqual(0, app_ans['returncode'], app_ans['stderr'])
        app_out = json.loads(app_ans['stdout'])
        self.assertNotIn('sign_off', app_out)
        self.assertFalse((self.project_path / '.standards' / 'signoffs').exists())

    def test_register_versioning_append_only_and_prior_versions_readable(self):
        p1 = Path(self.tmp.name) / 'reg-v1.json'
        p1.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Initial register v1.',
            'rules': [_sample_rule('STD-1', owner='person:alice', severity='should')],
        }), encoding='utf-8')
        out1 = self.admin_std('put', '--file', str(p1), '--actor', 'cora-op')
        self.assertEqual(1, out1['version'])

        p2 = Path(self.tmp.name) / 'reg-v2.json'
        p2.write_text(json.dumps({
            'expected_version': 1,
            'reason': 'Add second rule in v2.',
            'rules': [
                _sample_rule('STD-1', owner='person:alice', severity='must'),
                _sample_rule('STD-2', owner='person:bob', severity='should'),
            ],
        }), encoding='utf-8')
        out2 = self.admin_std('put', '--file', str(p2), '--actor', 'cora-op')
        self.assertEqual(2, out2['version'])

        # Both versions remain readable and immutable on disk.
        v1_ans = json.loads(self.endpoint_call('standards', ['show', '--version', '1'])['stdout'])
        v2_ans = json.loads(self.endpoint_call('standards', ['show', '--version', '2'])['stdout'])
        self.assertEqual(['STD-1'], [r['id'] for r in v1_ans['rules']])
        self.assertEqual('should', v1_ans['rules'][0]['severity'])
        self.assertEqual(['STD-1', 'STD-2'], [r['id'] for r in v2_ans['rules']])
        self.assertEqual('must', v2_ans['rules'][0]['severity'])

        hist = json.loads(self.endpoint_call('standards', ['history'])['stdout'])
        self.assertEqual([2, 1], [v['version'] for v in hist['items']])

    def test_shared_register_inheritance_origin_and_must_non_weakening(self):
        # First create shared register WITHOUT shared: true -> child inheritance refused.
        sp1 = Path(self.tmp.name) / 'shared-v1.json'
        sp1.write_text(json.dumps({
            'expected_version': 0,
            'shared': False,
            'reason': 'Private register first.',
            'rules': [_sample_rule('ORG-1', owner='person:olive', severity='must',
                                   applicability={'paths': ['auth/**']})],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(sp1), '--actor', 'olive-op', project=self.shared_pid)

        cp_bad = Path(self.tmp.name) / 'child-bad.json'
        cp_bad.write_text(json.dumps({
            'expected_version': 0,
            'inherits': self.shared_pid,
            'reason': 'Try inheriting non-shared.',
            'rules': [],
        }), encoding='utf-8')
        with self.assertRaises(rs.StandardsRefusal) as ctx_unshared:
            self.admin_std('put', '--file', str(cp_bad), '--actor', 'cora-op')
        self.assertIn('does not publish a shared standards register', str(ctx_unshared.exception))

        # Mark shared_pid as shared: true with one must rule and one should rule.
        sp2 = Path(self.tmp.name) / 'shared-v2.json'
        sp2.write_text(json.dumps({
            'expected_version': 1,
            'shared': True,
            'reason': 'Publish shared register.',
            'rules': [
                _sample_rule('ORG-1', owner='person:olive', severity='must',
                             applicability={'paths': ['auth/**']}),
                _sample_rule('ORG-2', owner='person:olive', severity='should'),
            ],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(sp2), '--actor', 'olive-op', project=self.shared_pid)

        # Child inherits cleanly and adds its own rule; origin tags distinguish inherited vs local.
        cp_ok = Path(self.tmp.name) / 'child-ok.json'
        cp_ok.write_text(json.dumps({
            'expected_version': 0,
            'inherits': self.shared_pid,
            'reason': 'Inherit sharedstd.',
            'rules': [
                _sample_rule('LOC-1', owner='person:alice', severity='should'),
            ],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(cp_ok), '--actor', 'cora-op')

        eff = json.loads(self.endpoint_call('standards', ['show', '--effective'])['stdout'])
        by_id = {r['id']: r for r in eff['effective_rules']}
        self.assertEqual(self.shared_pid, by_id['ORG-1']['origin']['project'])
        self.assertEqual('inherited', by_id['ORG-1']['origin']['kind'])
        self.assertEqual(self.pid, by_id['LOC-1']['origin']['project'])
        self.assertEqual('project', by_id['LOC-1']['origin']['kind'])

        # Kills M8: EVERY attempt in child to re-declare, weaken severity, narrow applicability,
        # downgrade check.kind, drop, or alter statement/owner of inherited 'must' rule 'ORG-1' is refused!
        for mut_desc, mutated_rule in (
            ('severity downgrade', _sample_rule('ORG-1', owner='person:olive', severity='should', applicability={'paths': ['auth/**']})),
            ('applicability narrow', _sample_rule('ORG-1', owner='person:olive', severity='must', applicability={'paths': ['auth/sub/**']})),
            ('statement edit', _sample_rule('ORG-1', owner='person:olive', severity='must', statement='Changed text.', applicability={'paths': ['auth/**']})),
            ('re-own', _sample_rule('ORG-1', owner='person:alice', severity='must', applicability={'paths': ['auth/**']})),
        ):
            with self.subTest(mut_desc=mut_desc):
                cp_mut = Path(self.tmp.name) / 'child-mut.json'
                cp_mut.write_text(json.dumps({
                    'expected_version': 1,
                    'inherits': self.shared_pid,
                    'reason': 'Attempt ' + mut_desc,
                    'rules': [mutated_rule],
                }), encoding='utf-8')
                with self.assertRaises(rs.StandardsRefusal) as cm:
                    self.admin_std('put', '--file', str(cp_mut), '--actor', 'cora-op')
                self.assertEqual(
                    rs.refusal_inherited_must_readonly(self.pid, 'ORG-1', self.shared_pid),
                    str(cm.exception),
                )

        # Child overrides inherited 'should' rule ('ORG-2'), then shared_pid promotes 'ORG-2' to 'must':
        # effective resolution takes shared_pid's 'must' verbatim and lint warns about shadowed override!
        cp_override = Path(self.tmp.name) / 'child-override.json'
        cp_override.write_text(json.dumps({
            'expected_version': 1,
            'inherits': self.shared_pid,
            'reason': 'Override ORG-2 while it is should.',
            'rules': [
                _sample_rule('ORG-2', owner='person:alice', severity='should', statement='Child custom style.'),
            ],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(cp_override), '--actor', 'cora-op')

        sp3 = Path(self.tmp.name) / 'shared-v3.json'
        sp3.write_text(json.dumps({
            'expected_version': 2,
            'shared': True,
            'reason': 'Promote ORG-2 to must.',
            'rules': [
                _sample_rule('ORG-1', owner='person:olive', severity='must', applicability={'paths': ['auth/**']}),
                _sample_rule('ORG-2', owner='person:olive', severity='must', statement='Org mandatory style.'),
            ],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(sp3), '--actor', 'olive-op', project=self.shared_pid)

        eff_after = json.loads(self.endpoint_call('standards', ['show', '--effective'])['stdout'])
        by_id_after = {r['id']: r for r in eff_after['effective_rules']}
        self.assertEqual('must', by_id_after['ORG-2']['severity'])
        self.assertEqual('Org mandatory style.', by_id_after['ORG-2']['statement'])
        lint_out = json.loads(self.endpoint_call('standards', ['lint'])['stdout'])
        self.assertTrue(any('ORG-2' in w.get('message', '') for w in lint_out['warnings']), lint_out)

    def test_new_rule_defaults_to_should_and_lint_warns_above_all_changes_must_limit(self):
        rules = [_sample_rule('DEF-1', owner='person:alice', severity=None)]
        for i in range(6):
            rules.append(_sample_rule('MUST-%d' % (i + 1), owner='person:alice', severity='must'))
        f = Path(self.tmp.name) / 'reg-lint.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Seed default + 6 all_changes must rules.',
            'rules': rules,
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')

        shown = json.loads(self.endpoint_call('standards', ['show'])['stdout'])
        by_id = {r['id']: r for r in shown['rules']}
        self.assertEqual('should', by_id['DEF-1']['severity'])

        lint = json.loads(self.endpoint_call('standards', ['lint'])['stdout'])
        self.assertTrue(any('all_changes' in w.get('message', '') and '5' in w.get('message', '') for w in lint['warnings']), lint)

        # Unknown language outside KNOWN_LANGUAGES is refused at write time.
        bad_lang = Path(self.tmp.name) / 'bad-lang.json'
        bad_lang.write_text(json.dumps({
            'expected_version': 1,
            'reason': 'Typo language.',
            'rules': [_sample_rule('LANG-1', applicability={'languages': ['pythno']})],
        }), encoding='utf-8')
        with self.assertRaises((rs.StandardsRefusal, ValueError)):
            self.admin_std('put', '--file', str(bad_lang), '--actor', 'cora-op')

    def test_rule_owner_must_be_person_edits_own_rule_and_notifies_coordinator(self):
        st = self.store()
        for bot_owner in ('agent_0123456789abcdef', 'bot:linter', 'team-security', ''):
            with self.subTest(bot_owner=bot_owner):
                with self.assertRaises((rs.StandardsRefusal, ValueError)):
                    rs.put_register(
                        st,
                        {'expected_version': 0, 'reason': 'bad owner', 'rules': [_sample_rule('STD-1', owner=bot_owner)]},
                        actor='cora-op', author_person='cora-op',
                        is_person_session=True, is_coordinator_or_owner=True,
                    )

        rs.put_register(
            st,
            {
                'expected_version': 0,
                'reason': 'Initial rules.',
                'rules': [
                    _sample_rule('OWN-1', owner='person:alice', severity='should'),
                    _sample_rule('OWN-2', owner='person:bob', severity='should'),
                ],
            },
            actor='cora-op', author_person='cora-op',
            is_person_session=True, is_coordinator_or_owner=True,
        )

        notes = []
        res = rs.edit_rule(
            st, 'OWN-1',
            {
                'expected_version': 1,
                'reason': 'Tighten wording.',
                'rule': _sample_rule('OWN-1', owner='person:alice', severity='must', statement='Updated by Alice.'),
            },
            actor='person:alice', author_person='person:alice',
            is_person_session=True, is_coordinator_or_owner=False,
            notify_coordinator_cb=notes.append,
        )
        self.assertEqual(2, res['version'])
        self.assertEqual(1, len(notes))
        self.assertIn('OWN-1', json.dumps(notes[0]))

        # Host coordinator notification log is also readable via CLI/endpoint 'standards notifications'.
        notif = json.loads(self.endpoint_call('standards', ['notifications'])['stdout'])
        self.assertTrue(any(item.get('rule_id') == 'OWN-1' for item in notif['items']))

        # Alice cannot edit Bob's rule.
        with self.assertRaises(rs.StandardsRefusal) as cm:
            rs.edit_rule(
                st, 'OWN-2',
                {'expected_version': 2, 'reason': 'Edit Bob rule', 'rule': _sample_rule('OWN-2', owner='person:bob')},
                actor='person:alice', author_person='person:alice',
                is_person_session=True, is_coordinator_or_owner=False,
            )
        self.assertIn('person:bob', str(cm.exception))

    def test_only_coordinator_or_named_editor_adds_removes_or_reowns_rules(self):
        st = self.store()
        rs.put_register(
            st,
            {
                'expected_version': 0,
                'reason': 'Seed with named editor.',
                'named_editors': ['person:ned'],
                'rules': [_sample_rule('STD-1', owner='person:alice')],
            },
            actor='cora-op', author_person='cora-op',
            is_person_session=True, is_coordinator_or_owner=True,
        )

        # Rule owner Alice cannot call put_register (add/remove/re-own).
        with self.assertRaises(rs.StandardsRefusal):
            rs.put_register(
                st,
                {
                    'expected_version': 1,
                    'reason': 'Alice tries adding STD-2.',
                    'named_editors': ['person:ned'],
                    'rules': [_sample_rule('STD-1', owner='person:alice'), _sample_rule('STD-2', owner='person:alice')],
                },
                actor='person:alice', author_person='person:alice',
                is_person_session=True, is_coordinator_or_owner=False,
            )

        # Named editor 'person:ned' CAN add/re-own rules without being coordinator!
        res = rs.put_register(
            st,
            {
                'expected_version': 1,
                'reason': 'Named editor adds STD-2 and re-owns STD-1.',
                'named_editors': ['person:ned'],
                'rules': [
                    _sample_rule('STD-1', owner='person:bob'),
                    _sample_rule('STD-2', owner='person:alice'),
                ],
            },
            actor='person:ned', author_person='person:ned',
            is_person_session=True, is_coordinator_or_owner=False,
        )
        self.assertEqual(2, res['version'])

    def test_departed_owner_falls_back_to_coordinator_and_agent_draft_lifecycle(self):
        f = Path(self.tmp.name) / 'reg-dep.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Initial rule owned by Alice.',
            'rules': [_sample_rule('DEP-1', owner='person:alice', severity='should')],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')

        # Mark person:alice departed via admin.py standards owner-departed.
        dep = self.admin_std('owner-departed', '--owner', 'person:alice', '--actor', 'cora-op', '--reason', 'Left team')
        self.assertIn('person:alice', dep['departed_owners'])

        # Departed owner can no longer edit directly; coordinator edits on fallback.
        with self.assertRaises(rs.StandardsRefusal):
            rs.edit_rule(
                self.store(), 'DEP-1',
                {'expected_version': 1, 'reason': 'Stale edit', 'rule': _sample_rule('DEP-1', owner='person:alice')},
                actor='person:alice', author_person='person:alice',
                is_person_session=True, is_coordinator_or_owner=False,
            )

        edit_f = Path(self.tmp.name) / 'edit-dep.json'
        edit_f.write_text(json.dumps({
            'expected_version': 1,
            'reason': 'Coordinator maintains departed owner rule.',
            'rule': _sample_rule('DEP-1', owner='person:alice', severity='must', statement='Maintained by coordinator.'),
        }), encoding='utf-8')
        res_coord = self.admin_std('rule-edit', '--rule', 'DEP-1', '--file', str(edit_f), '--actor', 'cora-op')
        self.assertEqual(2, res_coord['version'])

        # Agent proposes a draft over SSH endpoint; unauthorized non-owner/non-coordinator refused (#17);
        # empty reject reason refused (#22); coordinator accepts -> v3; second decision refused (#22).
        draft_payload = {
            'action': 'upsert',
            'reason': 'Observed repeated SQL string formatting.',
            'rule': _sample_rule('SQL-1', owner='person:bob', severity='should'),
        }
        d_ans = self.endpoint_call(
            'standards', ['draft-propose', '--file', '@attachment:0'],
            actor='agent-worker-1', attachments=[json.dumps(draft_payload)],
        )
        self.assertEqual(0, d_ans['returncode'], d_ans['stderr'])
        draft_id = json.loads(d_ans['stdout'])['id']

        listed = json.loads(self.endpoint_call('standards', ['draft-list', '--status', 'pending'])['stdout'])
        self.assertEqual([draft_id], [d['id'] for d in listed['items']])

        # Non-coordinator Bob cannot accept new-rule draft (#17).
        with self.assertRaises(rs.StandardsRefusal) as cm17:
            rs.decide_draft(
                self.store(), draft_id,
                {'decision': 'accept', 'expected_version': 2, 'reason': 'Bob tries accept'},
                actor='person:bob', author_person='person:bob',
                is_person_session=True, is_coordinator_or_owner=False,
            )
        self.assertEqual(rs.refusal_draft_decide_structural(draft_id), str(cm17.exception))

        # Reject without reason refused (#22).
        with self.assertRaises(rs.StandardsRefusal) as cm22a:
            self.admin_std('draft-decide', '--draft', draft_id, '--decision', 'reject', '--expected-version', '2', '--reason', '   ', '--actor', 'cora-op')
        self.assertEqual(rs.refusal_draft_reject_reason(draft_id), str(cm22a.exception))

        # Coordinator accepts draft -> v3!
        acc = self.admin_std('draft-decide', '--draft', draft_id, '--decision', 'accept', '--expected-version', '2', '--reason', 'Good catch', '--actor', 'cora-op')
        self.assertEqual(3, acc['register']['version'])

        # Deciding again is refused (#22).
        with self.assertRaises(rs.StandardsRefusal) as cm22b:
            self.admin_std('draft-decide', '--draft', draft_id, '--decision', 'reject', '--expected-version', '3', '--reason', 'Too late', '--actor', 'cora-op')
        self.assertEqual(rs.refusal_draft_already_decided(draft_id, 'accepted'), str(cm22b.exception))

    def test_applicability_bundle_and_branch_uses_merge_base_no_renames_never_author_list(self):
        bare = Path(self.tmp.name) / 'serving.git'
        _git(Path(self.tmp.name), 'init', '--bare', str(bare))
        clone = Path(self.tmp.name) / 'work-clone'
        _git(Path(self.tmp.name), 'clone', str(bare), str(clone))
        (clone / 'auth').mkdir()
        (clone / 'docs').mkdir()
        (clone / 'auth' / 'secret.py').write_text('TOKEN = 1\n', encoding='utf-8')
        (clone / 'docs' / 'readme.md').write_text('# Docs\n', encoding='utf-8')
        _git(clone, 'add', '.')
        _git(clone, 'commit', '-m', 'main base')
        _git(clone, 'push', 'origin', 'HEAD:refs/heads/main')
        main_sha = _git(clone, 'rev-parse', 'HEAD')

        self.serving_repos[self.pid] = str(bare)
        self._write_deployment()

        f = Path(self.tmp.name) / 'reg-app.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Guard auth/**.',
            'rules': [_sample_rule('AUTH-1', owner='person:alice', severity='must', applicability={'paths': ['auth/**']})],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')
        self.admin_std('set-gate', '--mode', 'block', '--actor', 'cora-op', '--reason', 'Enforce AUTH-1')

        # 1) Kills M1 (Author diff trust): contribute payload carrying author-supplied changed_files is rejected!
        tid1 = self.trackers[self.pid].create_task('Attempt author file list')
        ans_m1, _, _ = self.contribute_bundle(
            tid1, clone, base_commit=main_sha, op_id='op-m1',
            extra_fields={'changed_files': ['docs/readme.md']},
        )
        self.assertNotEqual(0, ans_m1['returncode'])

        # 2) Kills M2 (base_commit trim): 2-commit branch where C1 modifies auth/secret.py and C2 modifies docs/readme.md,
        # delivered with base_commit = C1. Serving repo merge-base(main, C2)..C2 catches auth/secret.py!
        _git(clone, 'checkout', '-b', 'feat-two-step')
        (clone / 'auth' / 'secret.py').write_text('TOKEN = 2\n', encoding='utf-8')
        _git(clone, 'add', 'auth/secret.py')
        _git(clone, 'commit', '-m', 'C1 touches guarded auth file')
        c1 = _git(clone, 'rev-parse', 'HEAD')

        (clone / 'docs' / 'readme.md').write_text('# Updated docs\n', encoding='utf-8')
        _git(clone, 'add', 'docs/readme.md')
        _git(clone, 'commit', '-m', 'C2 touches only docs')

        tid2 = self.trackers[self.pid].create_task('Two-commit trim attempt')
        ans_c2, _, _ = self.contribute_bundle(tid2, clone, base_commit=c1, op_id='op-m2')
        self.assertEqual(0, ans_c2['returncode'], ans_c2['stderr'])
        cid2 = json.loads(ans_c2['stdout'])['comment_id']

        brief2 = json.loads(self.endpoint_call('brief', ['--json', tid2], actor='cora-op')['stdout'])
        self.assertEqual('verified', brief2['standards']['diff_status'])
        self.assertIn('AUTH-1', [r['id'] for r in brief2['standards']['applicable_rules']])

        # Approving without answering AUTH-1 is refused in block mode!
        ref_m2 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-app-m2',
            'task': tid2, 'target': cid2, 'summary': 'Try skipping AUTH-1.', 'checks': [],
        }, actor='cora-op')
        self.assertNotEqual(0, ref_m2['returncode'])
        self.assertIn('AUTH-1', ref_m2['stderr'])

        # 3) Kills M3 (Rename evasion) + tests remote branch delivery via serving_repo:
        _git(clone, 'checkout', '-b', 'feat-rename', main_sha)
        _git(clone, 'mv', 'auth/secret.py', 'docs/moved_secret.txt')
        _git(clone, 'commit', '-m', 'Pure rename of guarded file')
        _git(clone, 'push', 'origin', 'HEAD:refs/heads/feat-rename')
        ren_sha = _git(clone, 'rev-parse', 'HEAD')

        tid3 = self.trackers[self.pid].create_task('Rename evasion attempt')
        ans_ren = self.review_op({
            'schema_version': 1, 'operation': 'contribute', 'operation_id': 'op-m3',
            'task': tid3, 'previous': None, 'supersedes': None,
            'repository': str(bare), 'commit': ren_sha, 'base_commit': main_sha,
            'delivery': {'kind': 'remote', 'remote': str(bare), 'branch': 'feat-rename'},
            'summary': 'Renamed file.',
        }, actor='worker-bob')
        self.assertEqual(0, ans_ren['returncode'], ans_ren['stderr'])
        ren_obs = rs.inspect_delivery_git(
            {'commit': ren_sha, 'delivery': {'kind': 'remote', 'remote': str(bare), 'branch': 'feat-rename'}},
            serving_repo=str(bare),
        )
        self.assertIn('auth/secret.py', ren_obs['changed_files'])
        self.assertIn('docs/moved_secret.txt', ren_obs['changed_files'])
        brief3 = json.loads(self.endpoint_call('brief', ['--json', tid3], actor='cora-op')['stdout'])
        self.assertEqual('verified', brief3['standards']['diff_status'])
        self.assertIn('AUTH-1', [r['id'] for r in brief3['standards']['applicable_rules']])

    def test_two_commit_bundle_advancing_author_base_commit_is_diff_unverified_never_trimmed(self):
        # Plan 3 Item 1 — Kills M21: NO serving_repos and NO verifier commit observation!
        repo = Path(self.tmp.name) / 'repo-m21'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'auth').mkdir()
        (repo / 'docs').mkdir()
        (repo / 'auth' / 'guard.py').write_text('ALLOW = False\n', encoding='utf-8')
        (repo / 'docs' / 'note.md').write_text('hello\n', encoding='utf-8')
        _git(repo, 'add', '.')
        _git(repo, 'commit', '-m', 'M')

        (repo / 'auth' / 'guard.py').write_text('ALLOW = True\n# line 2\n', encoding='utf-8')
        _git(repo, 'add', 'auth/guard.py')
        _git(repo, 'commit', '-m', 'C1 touches guarded path')
        c1 = _git(repo, 'rev-parse', 'HEAD')

        (repo / 'docs' / 'note.md').write_text('hello world\n', encoding='utf-8')
        _git(repo, 'add', 'docs/note.md')
        _git(repo, 'commit', '-m', 'C2 touches unguarded path')
        c2, sha = _make_bundle(repo, self.delivery_root / 'm21.bundle')

        inspected = rs.inspect_delivery_git(
            {
                'commit': c2,
                'base_commit': c1,
                'delivery': {'kind': 'bundle', 'path': _bundle_delivery_path(self.delivery_root / 'm21.bundle'), 'sha256': sha},
            },
            delivery_roots=[str(self.delivery_root)],
            serving_repo=None,
            needed_paths=['auth/guard.py'],
            lock_dir=self.root,
        )
        self.assertEqual('diff_unverified', inspected['diff_status'])
        self.assertIsNone(inspected['changed_files'])
        self.assertTrue(inspected['tree_available'])
        self.assertEqual(2, inspected['tree_files']['auth/guard.py']['line_count'])

    def test_bundle_fetch_with_protocol_file_allow_succeeds_and_blocks_remote_transports(self):
        repo = Path(self.tmp.name) / 'repo-proto'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'hello.py').write_text('print("hi")\n', encoding='utf-8')
        _git(repo, 'add', 'hello.py')
        _git(repo, 'commit', '-m', 'init')
        commit, sha = _make_bundle(repo, self.delivery_root / 'proto.bundle')

        bare_dst = Path(self.tmp.name) / 'dst.git'
        rs.run_confined_git(['init', '--bare', '--quiet', str(bare_dst)])
        rs.run_confined_git(['fetch', '--no-tags', '--', str(self.delivery_root / 'proto.bundle'), commit], cwd=bare_dst)
        self.assertEqual(commit, rs.run_confined_git(['rev-parse', commit], cwd=bare_dst).strip())

        for remote_url in (
            'https://127.0.0.1:1/repo.git',
            'ssh://127.0.0.1/repo.git',
            'git://127.0.0.1/repo.git',
            'ext::sh -c echo%20pwned',
        ):
            with self.subTest(remote_url=remote_url):
                with self.assertRaises(ValueError) as cm:
                    rs.run_confined_git(['ls-remote', remote_url], cwd=bare_dst, timeout=5)
                self.assertIn('not allowed', str(cm.exception).lower())

        lock_file = self.root / rs.BUNDLE_FETCH_LOCK_NAME
        victim = Path(self.tmp.name) / 'victim.txt'
        victim.write_text('keep', encoding='utf-8')
        os.symlink(str(victim), str(lock_file))
        try:
            res_sym = rs.inspect_delivery_git(
                {'commit': commit, 'delivery': {'kind': 'bundle', 'path': _bundle_delivery_path(self.delivery_root / 'proto.bundle'), 'sha256': sha}},
                delivery_roots=[str(self.delivery_root)],
                lock_dir=self.root,
                needed_paths=['hello.py'],
            )
            self.assertFalse(res_sym['tree_available'])
            self.assertEqual('diff_unverified', res_sym['diff_status'])
        finally:
            os.unlink(str(lock_file))

        rs.BUNDLE_INSPECT_SEMAPHORE.acquire()
        try:
            res_to = rs.inspect_delivery_git(
                {'commit': commit, 'delivery': {'kind': 'bundle', 'path': _bundle_delivery_path(self.delivery_root / 'proto.bundle'), 'sha256': sha}},
                delivery_roots=[str(self.delivery_root)],
                lock_dir=self.root,
                needed_paths=['hello.py'],
                lock_wait_seconds=0.05,
            )
            self.assertTrue(res_to['lock_timed_out'])
            self.assertFalse(res_to['tree_available'])
            self.assertEqual('diff_unverified', res_to['diff_status'])
        finally:
            rs.BUNDLE_INSPECT_SEMAPHORE.release()

    def test_citation_validation_resolves_tree_lines_and_refuses_missing_out_of_range_symlink_or_binary(self):
        # Kills M4 (off-by-one line 0 or line_count+1) and M5 (symlink git mode 120000).
        repo = Path(self.tmp.name) / 'repo-cite'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'mod.py').write_text('line1\nline2\nline3\n', encoding='utf-8')
        (repo / 'asset.bin').write_bytes(b'\x89PNG\r\n\x00\x01\x02')
        os.symlink('mod.py', str(repo / 'link.py'))
        _git(repo, 'add', '.')
        _git(repo, 'commit', '-m', 'tree fixtures')
        commit, sha = _make_bundle(repo, self.delivery_root / 'cite.bundle')

        info = rs.inspect_delivery_git(
            {'commit': commit, 'delivery': {'kind': 'bundle', 'path': _bundle_delivery_path(self.delivery_root / 'cite.bundle'), 'sha256': sha}},
            delivery_roots=[str(self.delivery_root)],
            needed_paths=['mod.py', 'missing.py', 'link.py', 'asset.bin'],
            lock_dir=self.root,
        )
        self.assertTrue(info['tree_available'])
        self.assertEqual(3, info['tree_files']['mod.py']['line_count'])

        for valid_cit in ('mod.py:1', 'mod.py:3', 'mod.py:1-3'):
            parsed = rs.validate_citation_against_tree(valid_cit, 'STD-1', commit, True, info['tree_files'])
            self.assertEqual('mod.py', parsed['path'])

        for bad_cit in ('mod.py:0', 'mod.py:4', 'mod.py:1-4', 'mod.py:3-2', 'missing.py:1', 'link.py:1', 'asset.bin:1'):
            with self.subTest(bad_cit=bad_cit):
                with self.assertRaises(rs.StandardsRefusal) as cm:
                    rs.validate_citation_against_tree(bad_cit, 'STD-1', commit, True, info['tree_files'])
                self.assertIn(commit[:12], str(cm.exception))

    def test_mechanical_rule_reads_ci_for_commit_refuses_reviewer_pass_and_marks_unverified_without_ci(self):
        # Kills M6 (reviewer asserting pass on mechanical rule) & M15 (author party recording CI).
        f = Path(self.tmp.name) / 'reg-mech.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Mechanical pytest rule.',
            'rules': [_sample_rule(
                'MECH-1', owner='person:alice', severity='must',
                check={'kind': 'mechanical', 'tool': 'pytest', 'command': 'pytest -q'},
            )],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')

        reg_sess = sessions.execute(
            self.project_path, self.pid,
            ['register', '--name', 'Bob Worker', '--request-id', '00000000-0000-4000-8000-000000000001'],
            lambda: '', principal='person:bob',
        )
        bob_actor = reg_sess['session']['actor']

        tid = self.trackers[self.pid].create_task('Mechanical task', assignee=bob_actor)
        repo = Path(self.tmp.name) / 'repo-mech'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'calc.py').write_text('def add(a, b):\n    return a + b\n', encoding='utf-8')
        _git(repo, 'add', 'calc.py')
        _git(repo, 'commit', '-m', 'add calc')
        ans_c, commit, _ = self.contribute_bundle(tid, repo, actor=bob_actor, op_id='op-mech-c1', extra_fields=None)
        self.assertEqual(0, ans_c['returncode'], ans_c['stderr'])
        cid = json.loads(ans_c['stdout'])['comment_id']

        # Kills M6: reviewer asserting result='pass' on mechanical rule is refused with exact sentence #8!
        ans_m6 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m6',
            'task': tid, 'target': cid, 'summary': 'Reviewer claims pytest passed.',
            'checks': [{'rule_id': 'MECH-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['calc.py:1']}}],
        }, actor='cora-op')
        self.assertNotEqual(0, ans_m6['returncode'])
        self.assertIn(
            rs.refusal_mechanical_reviewer_pass('MECH-1', 'pytest', 'pytest -q', commit),
            ans_m6['stderr'],
        )

        # Without CI observation, warn mode approves and marks MECH-1 'unverified'!
        ans_unv = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-unv',
            'task': tid, 'target': cid, 'summary': 'Approve in warn mode without CI.',
        }, actor='cora-op')
        self.assertEqual(0, ans_unv['returncode'], ans_unv['stderr'])
        so_unv = json.loads(self.endpoint_call('standards', ['sign-off', tid])['stdout'])
        self.assertEqual('unverified', so_unv['checks'][0]['result'])
        self.assertIn('MECH-1', ' '.join(so_unv['would_have_refused']))

        # Kills M15: delivery author OR owning person (person:bob) OR approver (person:cora) attempting record-commit is refused!
        ci_ok = Path(self.tmp.name) / 'ci-ok.json'
        ci_ok.write_text(json.dumps({
            'changed_files': ['calc.py'],
            'ci_checks': [{'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}],
        }), encoding='utf-8')
        for bad_recorder in (bob_actor, 'person:bob', 'cora-op'):
            with self.subTest(bad_recorder=bad_recorder):
                with self.assertRaises(rs.StandardsRefusal) as cm15:
                    rs.record_commit_observation(
                        self.store(), commit,
                        {'changed_files': ['calc.py'], 'ci_checks': [{'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}]},
                        actor=bad_recorder,
                        is_authorized_recorder=True,
                        same_party_checker=lambda other, who=bad_recorder: (
                            who == other or (who == 'person:bob' and other == bob_actor)
                        ),
                        rows_or_tasks=list(self.trackers[self.pid].rows.values()),
                    )
                self.assertEqual(rs.refusal_commit_observation_authority(commit), str(cm15.exception))

        # Independent verifier records exit_code=0 on a second delivery -> mechanical rule becomes 'pass'!
        (repo / 'calc.py').write_text('def add(a, b):\n    return a + b + 0\n', encoding='utf-8')
        _git(repo, 'add', 'calc.py')
        _git(repo, 'commit', '-m', 'step 2')
        ans_c2, commit2, _ = self.contribute_bundle(tid, repo, actor=bob_actor, op_id='op-mech-c2')
        cid2 = json.loads(ans_c2['stdout'])['comment_id']
        self.admin_std('record-commit', '--commit', commit2, '--file', str(ci_ok), '--actor', 'ci-verifier')
        self.admin_std('set-gate', '--mode', 'block', '--actor', 'olive-op', '--reason', 'Turn on block')

        ans_pass = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-mech-pass',
            'task': tid, 'target': cid2, 'summary': 'Verified by CI.',
        }, actor='olive-op')
        self.assertEqual(0, ans_pass['returncode'], ans_pass['stderr'])
        so_pass = json.loads(self.endpoint_call('standards', ['sign-off', tid])['stdout'])
        self.assertEqual('pass', so_pass['checks'][0]['result'])
        self.assertEqual('verifier-recorded', so_pass['checks'][0]['source'])

    def test_author_summary_claim_never_prefills_checks_and_must_na_counted_per_reviewer(self):
        f = Path(self.tmp.name) / 'reg-na.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Two must rules.',
            'rules': [
                _sample_rule('CITE-1', owner='person:alice', severity='must'),
                _sample_rule('SQL-1', owner='person:alice', severity='must', applicability={'paths': ['db/**']}),
            ],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')
        self.admin_std('set-gate', '--mode', 'block', '--actor', 'cora-op', '--reason', 'Block gate')

        tid = self.trackers[self.pid].create_task('Author claim task')
        repo = Path(self.tmp.name) / 'repo-na'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'main.py').write_text('x = 42\n', encoding='utf-8')
        _git(repo, 'add', 'main.py')
        _git(repo, 'commit', '-m', 'init')
        ans_c, _, _ = self.contribute_bundle(
            tid, repo, op_id='op-claim',
            summary='CITE-1: pass (main.py:1); SQL-1: not_applicable; all standards satisfied!',
        )
        cid = json.loads(ans_c['stdout'])['comment_id']

        # Empty checks in block mode MUST fail (author summary never pre-fills any check record!).
        ref = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-empty-chk',
            'task': tid, 'target': cid, 'summary': 'Approve relying on author summary.', 'checks': [],
        }, actor='cora-op')
        self.assertNotEqual(0, ref['returncode'])
        self.assertIn('CITE-1', ref['stderr'])

        # Under diff_unverified (no serving_repo), SQL-1 is possibly-applicable; marking it not_applicable
        # with a reason succeeds and increments person:cora's derived must not_applicable tally!
        ok = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-na-ok',
            'task': tid, 'target': cid, 'summary': 'Checked manually.',
            'checks': [
                {'rule_id': 'CITE-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['main.py:1']}},
                {'rule_id': 'SQL-1', 'result': 'not_applicable', 'reason': 'Only main.py changed, no SQL.'},
            ],
        }, actor='cora-op')
        self.assertEqual(0, ok['returncode'], ok['stderr'])
        so = json.loads(self.endpoint_call('standards', ['sign-off', tid])['stdout'])
        self.assertEqual(1, so['reviewer_must_not_applicable_total'])
        self.assertEqual(0, rs.derive_reviewer_must_na_counts(self.store(), 'olive-op'))

    def test_gate_warn_default_marks_would_have_refused_and_block_refuses_naming_rule_ids(self):
        # Kills M7 (unverified treated as pass in block mode) & checks should-failure non-blocking behavior.
        f = Path(self.tmp.name) / 'reg-gate.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Must judgement + must mechanical + should judgement.',
            'rules': [
                _sample_rule('MUST-1', owner='person:alice', severity='must'),
                _sample_rule('MUST-2', owner='person:alice', severity='must',
                             check={'kind': 'mechanical', 'tool': 'ruff', 'command': 'ruff check .'}),
                _sample_rule('SHLD-1', owner='person:alice', severity='should'),
            ],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')

        tid1 = self.trackers[self.pid].create_task('Warn mode task')
        repo = Path(self.tmp.name) / 'repo-gate'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'f.py').write_text('a = 1\n', encoding='utf-8')
        _git(repo, 'add', 'f.py')
        _git(repo, 'commit', '-m', 'init')
        ans1, _, _ = self.contribute_bundle(tid1, repo, op_id='op-gw1')
        cid1 = json.loads(ans1['stdout'])['comment_id']

        w_app = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-gw-app1',
            'task': tid1, 'target': cid1, 'summary': 'Warn mode approve.',
        }, actor='cora-op')
        self.assertEqual(0, w_app['returncode'], w_app['stderr'])
        so1 = json.loads(self.endpoint_call('standards', ['sign-off', tid1])['stdout'])
        self.assertEqual('warn', so1['gate_mode'])
        joined = ' '.join(so1['would_have_refused'])
        self.assertIn('MUST-1', joined)
        self.assertIn('MUST-2', joined)

        # Switch to 'block' mode: unverified MUST-2 MUST refuse approval (kills M7)!
        self.admin_std('set-gate', '--mode', 'block', '--actor', 'cora-op', '--reason', 'Flip to block')
        tid2 = self.trackers[self.pid].create_task('Block mode task')
        ans2, commit2, _ = self.contribute_bundle(tid2, repo, op_id='op-gw2')
        cid2 = json.loads(ans2['stdout'])['comment_id']

        b_ref = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-gw-app2',
            'task': tid2, 'target': cid2, 'summary': 'Try passing with unverified MUST-2.',
            'checks': [{'rule_id': 'MUST-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['f.py:1']}}],
        }, actor='cora-op')
        self.assertNotEqual(0, b_ref['returncode'])
        self.assertIn('MUST-2', b_ref['stderr'])

        # Record CI pass for MUST-2, and record a standing recommendation with a 'note' item so SHLD-1 can 'fail'
        # citing that item_id — in block mode, a 'should' failure does NOT block approval and shows on sign-off!
        ci_f = Path(self.tmp.name) / 'ci-ruff.json'
        ci_f.write_text(json.dumps({
            'changed_files': ['f.py'],
            'ci_checks': [{'tool': 'ruff', 'command': 'ruff check .', 'exit_code': 0}],
        }), encoding='utf-8')
        self.admin_std('record-commit', '--commit', commit2, '--file', str(ci_f), '--actor', 'ci-verifier')

        rec_ans = self.review_op({
            'schema_version': 1, 'operation': 'recommend', 'operation_id': 'op-rec-should',
            'task': tid2, 'contribution': cid2, 'commit': commit2, 'verdict': 'approve',
            'summary': 'Musts pass; minor SHLD-1 note.',
            'items': [{'id': 'note-1', 'text': 'Style nit at f.py:1 (optional cleanup).'}],
            'checks': [
                {'rule_id': 'MUST-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['f.py:1']}},
                {'rule_id': 'SHLD-1', 'result': 'fail', 'evidence': {'kind': 'review_item', 'item_id': 'note-1'}},
            ],
        }, actor='reviewer-r1')
        self.assertEqual(0, rec_ans['returncode'], rec_ans['stderr'])
        rec_id = json.loads(rec_ans['stdout'])['comment_id']

        b_ok = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-gw-app3',
            'task': tid2, 'target': cid2, 'summary': 'Approve adopting recommendation.',
            'adopt_recommendation': rec_id,
        }, actor='cora-op')
        self.assertEqual(0, b_ok['returncode'], b_ok['stderr'])
        so2 = json.loads(self.endpoint_call('standards', ['sign-off', tid2])['stdout'])
        by_rule = {c['rule_id']: c['result'] for c in so2['checks']}
        self.assertEqual({'MUST-1': 'pass', 'MUST-2': 'pass', 'SHLD-1': 'fail'}, by_rule)

    def test_damaged_gate_file_fails_closed_to_block_and_agent_cannot_flip_gate(self):
        # Kills M9 (agent gate flip) and M10 (corrupt gate.json failing open to warn).
        f = Path(self.tmp.name) / 'reg-dg.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'One must rule.',
            'rules': [_sample_rule('DG-1', owner='person:alice', severity='must')],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')

        # Kills M9: SSH endpoint AND non-person-session coordinator credential refused on set-gate!
        flip = self.endpoint_call('standards', ['set-gate', '--mode', 'warn', '--reason', 'agent flip'], actor='cora-op')
        self.assertNotEqual(0, flip['returncode'])
        self.assertIn(rs.refusal_ssh_person_only('set-gate'), flip['stderr'])
        with self.assertRaises(rs.StandardsRefusal) as m9_ctx:
            rs.set_gate_mode(
                self.store(), 'warn', 'Coordinator agent flip attempt',
                actor='agent:cora:bot', author_person='person:cora',
                is_person_session=False, is_coordinator_or_owner=True,
            )
        self.assertEqual(rs.refusal_gate_switch_authority(), str(m9_ctx.exception))

        # Corrupt gate.json -> read_gate_raw / get_gate_view must fail closed to 'block' (kills M10)!
        (self.project_path / '.standards' / 'gate.json').write_text('{corrupt json', encoding='utf-8')
        raw_status, gate_obj, _ = self.store().read_gate_raw()
        self.assertEqual('damaged', raw_status)
        self.assertIsNone(gate_obj)
        self.assertEqual('block', rs.get_gate_view(self.store())['mode'])

        tid = self.trackers[self.pid].create_task('Damaged gate task')
        repo = Path(self.tmp.name) / 'repo-dg'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'x.py').write_text('x = 1\n', encoding='utf-8')
        _git(repo, 'add', 'x.py')
        _git(repo, 'commit', '-m', 'init')
        ans_c, _, _ = self.contribute_bundle(tid, repo, op_id='op-dg-c')
        cid = json.loads(ans_c['stdout'])['comment_id']

        ref = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-dg-app',
            'task': tid, 'target': cid, 'summary': 'Try approving under corrupt gate.json.', 'checks': [],
        }, actor='cora-op')
        self.assertNotEqual(0, ref['returncode'])
        self.assertIn('DG-1', ref['stderr'])

    def test_damaged_head_json_blocks_in_block_mode_and_warns_in_warn_mode_and_reads_never_write(self):
        f = Path(self.tmp.name) / 'reg-dh.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Initial register.',
            'rules': [_sample_rule('DH-1', owner='person:alice', severity='must')],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')

        tid = self.trackers[self.pid].create_task('Damaged head task')
        repo = Path(self.tmp.name) / 'repo-dh'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'd.py').write_text('d = 1\n', encoding='utf-8')
        _git(repo, 'add', 'd.py')
        _git(repo, 'commit', '-m', 'init')
        ans_c, _, _ = self.contribute_bundle(tid, repo, op_id='op-dh-c1')
        cid1 = json.loads(ans_c['stdout'])['comment_id']

        # Corrupt head.json!
        (self.project_path / '.standards' / 'head.json').write_text('not valid json', encoding='utf-8')
        pins_dir = self.project_path / '.standards' / 'pins'
        if pins_dir.exists():
            for p in pins_dir.iterdir():
                p.unlink()
            pins_dir.rmdir()

        before_tree = sorted(str(p.relative_to(self.project_path)) for p in (self.project_path / '.standards').rglob('*'))
        show_ans = json.loads(self.endpoint_call('standards', ['show'])['stdout'])
        brief_ans = json.loads(self.endpoint_call('brief', ['--json', tid], actor='cora-op')['stdout'])
        after_tree = sorted(str(p.relative_to(self.project_path)) for p in (self.project_path / '.standards').rglob('*'))
        self.assertEqual(before_tree, after_tree)
        self.assertEqual('damaged', show_ans['status'])
        self.assertEqual('damaged', brief_ans['standards']['status'])

        # In warn mode (gate.json absent): approve succeeds and records damaged_register in would_have_refused!
        app_warn = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-dh-warn',
            'task': tid, 'target': cid1, 'summary': 'Emergency warn-mode approve.',
        }, actor='cora-op')
        self.assertEqual(0, app_warn['returncode'], app_warn['stderr'])
        so_warn = json.loads(self.endpoint_call('standards', ['sign-off', tid])['stdout'])
        self.assertEqual('damaged', so_warn['register_status'])
        self.assertTrue(any('damaged_register' in w for w in so_warn['would_have_refused']))

        # Switch gate to block -> damaged head.json MUST fail closed with sentence #19!
        self.admin_std('set-gate', '--mode', 'block', '--actor', 'cora-op', '--reason', 'Block on damaged')
        (repo / 'd.py').write_text('d = 2\n', encoding='utf-8')
        _git(repo, 'add', 'd.py')
        _git(repo, 'commit', '-m', 'c2')
        ans_c2, _, _ = self.contribute_bundle(tid, repo, op_id='op-dh-c2')
        cid2 = json.loads(ans_c2['stdout'])['comment_id']

        app_blk = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-dh-blk',
            'task': tid, 'target': cid2, 'summary': 'Should fail closed.',
        }, actor='cora-op')
        self.assertNotEqual(0, app_blk['returncode'])
        self.assertIn('Approval refused: standards register in project proj is damaged', app_blk['stderr'])

    def test_signoff_pins_context_keeps_open_review_version_and_does_not_carry_over_on_new_commit(self):
        # Kills M11 (sign-off carryover across commits) & M12 (unpinned register version mid-review).
        f1 = Path(self.tmp.name) / 'reg-pin1.json'
        f1.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'v1 has only PIN-1.',
            'rules': [_sample_rule('PIN-1', owner='person:alice', severity='must')],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f1), '--actor', 'cora-op')
        self.admin_std('set-gate', '--mode', 'block', '--actor', 'cora-op', '--reason', 'Block gate')

        tid = self.trackers[self.pid].create_task('Pinning task')
        repo = Path(self.tmp.name) / 'repo-pin'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'p.py').write_text('v = 1\n', encoding='utf-8')
        _git(repo, 'add', 'p.py')
        _git(repo, 'commit', '-m', 'c1')
        ans1, commit1, _ = self.contribute_bundle(tid, repo, op_id='op-pin-c1')
        cid1 = json.loads(ans1['stdout'])['comment_id']

        # Mid-review: v2 adds a second must rule 'PIN-2'!
        f2 = Path(self.tmp.name) / 'reg-pin2.json'
        f2.write_text(json.dumps({
            'expected_version': 1,
            'reason': 'v2 adds PIN-2.',
            'rules': [
                _sample_rule('PIN-1', owner='person:alice', severity='must'),
                _sample_rule('PIN-2', owner='person:alice', severity='must'),
            ],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f2), '--actor', 'cora-op')

        # Kills M12: open review on cid1 stays pinned to v1, so answering ONLY PIN-1 succeeds!
        app1 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-pin-app1',
            'task': tid, 'target': cid1, 'summary': 'Approved against pinned v1.',
            'checks': [{'rule_id': 'PIN-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['p.py:1']}}],
        }, actor='cora-op')
        self.assertEqual(0, app1['returncode'], app1['stderr'])

        so1_md = self.endpoint_call('standards', ['sign-off', tid, '--format', 'markdown'])['stdout']
        self.assertIn('Checked against standards register v1', so1_md)
        self.assertNotIn('certified', so1_md.lower())
        self.assertNotIn('guaranteed', so1_md.lower())

        # Kills M11: new contribution c2 at commit2 lands on the same task -> c1's sign-off does NOT carry over,
        # and c2 pins v2!
        (repo / 'p.py').write_text('v = 2\n', encoding='utf-8')
        _git(repo, 'add', 'p.py')
        _git(repo, 'commit', '-m', 'c2')
        ans2, commit2, _ = self.contribute_bundle(tid, repo, op_id='op-pin-c2')
        self.assertNotEqual(commit1, commit2)
        cid2 = json.loads(ans2['stdout'])['comment_id']

        so_latest = self.endpoint_call('standards', ['sign-off', tid])
        self.assertEqual(2, so_latest['returncode'])
        self.assertEqual(404, so_latest.get('standards_status'))

        so_hist = json.loads(self.endpoint_call('standards', ['sign-off', tid, '--contribution', cid1])['stdout'])
        self.assertEqual(cid1, so_hist['contribution_id'])

        ref2 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-pin-app2',
            'task': tid, 'target': cid2, 'summary': 'Missing PIN-2.',
            'checks': [{'rule_id': 'PIN-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['p.py:1']}}],
        }, actor='cora-op')
        self.assertNotEqual(0, ref2['returncode'])
        self.assertIn('PIN-2', ref2['stderr'])

    def test_waiver_authority_author_party_refusal_and_delivery_vs_date_expiry(self):
        # Kills M13 (same-party waiver) & M14 (expired date waiver).
        f = Path(self.tmp.name) / 'reg-waiv.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Waivable must rule owned by Alice.',
            'rules': [_sample_rule('WAIV-1', owner='person:alice', severity='must')],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')
        self.admin_std('set-gate', '--mode', 'block', '--actor', 'cora-op', '--reason', 'Block gate')

        reg_sess = sessions.execute(
            self.project_path, self.pid,
            ['register', '--name', 'Alice Agent', '--request-id', '00000000-0000-4000-8000-000000000002'],
            lambda: '', principal='person:alice',
        )
        alice_agent = reg_sess['session']['actor']

        tid = self.trackers[self.pid].create_task('Waiver task', assignee=alice_agent)
        repo = Path(self.tmp.name) / 'repo-waiv'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'w.py').write_text('w = 1\n', encoding='utf-8')
        _git(repo, 'add', 'w.py')
        _git(repo, 'commit', '-m', 'c1')
        ans_c1, commit1, _ = self.contribute_bundle(tid, repo, actor=alice_agent, op_id='op-waiv-c1')
        cid1 = json.loads(ans_c1['stdout'])['comment_id']
        contrib_info = {'comment_id': cid1, 'commit': commit1, 'author': alice_agent}

        # Kills M13: rule owner person:alice CANNOT waive on her own agent's delivery!
        with self.assertRaises(rs.StandardsRefusal) as cm13:
            rs.grant_waiver(
                self.store(),
                {'rule_id': 'WAIV-1', 'contribution_id': cid1, 'commit': commit1, 'reason': 'Self waive', 'expiry': {'kind': 'delivery'}},
                task_id=tid, contribution=contrib_info, task_assignee=alice_agent,
                actor='person:alice', author_person='person:alice',
                is_person_session=True, is_coordinator_or_owner=False,
                same_party_checker=lambda other: other in ('person:alice', alice_agent),
            )
        self.assertIn("author's or assignee's party", str(cm13.exception))

        # Kills M14: Coordinator grants a date-bounded waiver that expires yesterday -> refused at approval with #20!
        self.admin_std(
            'waive', '--task', tid, '--rule', 'WAIV-1', '--contribution', cid1, '--commit', commit1,
            '--until', '2020-01-01', '--reason', 'Short hotfix window', '--actor', 'cora-op',
        )
        ref_exp = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-waiv-exp',
            'task': tid, 'target': cid1, 'summary': 'Try approving with expired waiver.', 'checks': [],
        }, actor='cora-op')
        self.assertNotEqual(0, ref_exp['returncode'])
        self.assertIn('expired on 2020-01-01', ref_exp['stderr'])

        # Grant valid future-bounded waiver on cid1 -> approval succeeds!
        self.admin_std(
            'waive', '--task', tid, '--rule', 'WAIV-1', '--contribution', cid1, '--commit', commit1,
            '--until', '2099-12-31', '--reason', 'Approved migration waiver', '--actor', 'cora-op',
        )
        ok_w = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-waiv-ok',
            'task': tid, 'target': cid1, 'summary': 'Approved with active waiver.', 'checks': [],
        }, actor='cora-op')
        self.assertEqual(0, ok_w['returncode'], ok_w['stderr'])

        # Single-delivery rule: a new commit c2 on the same task immediately supersedes cid1's waiver!
        (repo / 'w.py').write_text('w = 2\n', encoding='utf-8')
        _git(repo, 'add', 'w.py')
        _git(repo, 'commit', '-m', 'c2')
        ans_c2, _, _ = self.contribute_bundle(tid, repo, actor=alice_agent, op_id='op-waiv-c2')
        cid2 = json.loads(ans_c2['stdout'])['comment_id']
        w_list = json.loads(self.endpoint_call('standards', ['waivers', tid])['stdout'])
        self.assertTrue(all(w['status'] == 'superseded_delivery' for w in w_list['items']))

        ref_c2 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-waiv-c2-app',
            'task': tid, 'target': cid2, 'summary': 'Prior waiver must not carry over.', 'checks': [],
        }, actor='cora-op')
        self.assertNotEqual(0, ref_c2['returncode'])
        self.assertIn('WAIV-1', ref_c2['stderr'])

    def test_worker_and_reviewer_briefs_list_applicable_rules_and_required_checks(self):
        f = Path(self.tmp.name) / 'reg-brief.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Rules for brief.',
            'rules': [
                _sample_rule('BRF-1', owner='person:alice', severity='must'),
                _sample_rule('BRF-2', owner='person:alice', severity='should', applicability={'task_labels': ['security']}),
            ],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')

        tid = self.trackers[self.pid].create_task('Brief task', labels=['security'])
        wb = json.loads(self.endpoint_call('brief', ['--json', tid], actor='worker-bob')['stdout'])['standards']
        self.assertEqual('active', wb['status'])
        self.assertEqual(1, wb['register_version'])
        self.assertEqual({'BRF-1', 'BRF-2'}, {r['id'] for r in wb['applicable_rules']})

        repo = Path(self.tmp.name) / 'repo-brief'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 's.py').write_text('s = 1\n', encoding='utf-8')
        _git(repo, 'add', 's.py')
        _git(repo, 'commit', '-m', 'init')
        self.contribute_bundle(tid, repo, op_id='op-brief-c1')

        rb = json.loads(self.endpoint_call('brief', ['--json', tid], actor='cora-op')['stdout'])['standards']
        self.assertEqual(1, rb['register_version'])
        self.assertIn('BRF-1', [c['rule_id'] for c in rb['required_checks'] if c['severity'] == 'must'])
        self.assertEqual({'BRF-1', 'BRF-2'}, {r['id'] for r in rb['applicable_rules']})

    def test_ssh_agent_lane_bound_to_person_and_operator_principal_refused_on_all_person_actions(self):
        # Plan 2 Item 3 — Kills M16: SSH lane bound to BOTH person:cora AND listed in operators
        # is unconditionally refused on every person-only standards subcommand and raw web-standards!
        reg_sess = sessions.execute(
            self.project_path, self.pid,
            ['register', '--name', 'Cora Lane', '--request-id', '00000000-0000-4000-8000-000000000003'],
            lambda: '', principal='person:cora',
        )
        cora_lane = reg_sess['session']['actor']
        self.operators.append(cora_lane)
        self._write_deployment()
        for subcmd in ('put', 'rule-edit', 'set-gate', 'waive', 'draft-decide', 'owner-departed', 'record-commit'):
            with self.subTest(subcmd=subcmd):
                ans = self.endpoint_call(
                    'standards', [subcmd, '--help'],
                    actor=cora_lane, key_principal='person:cora',
                )
                self.assertNotEqual(0, ans['returncode'])
                self.assertIn(rs.refusal_ssh_person_only(subcmd), ans['stderr'])

        with self.assertRaises(ValueError):
            self.endpoint_call(
                'web-standards', ['set-gate'],
                actor=cora_lane, key_principal='person:cora',
                extra={'operation': 'set-gate', 'payload': {'mode': 'block', 'reason': 'forged'}},
                raw_raise=True,
            )
        self.assertFalse((self.project_path / '.standards').exists())

    def test_expected_version_conflict_on_put_rule_edit_and_draft_decide(self):
        # Plan 2 Item 8 — Kills M19!
        f = Path(self.tmp.name) / 'reg-ev.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Create v1.',
            'rules': [_sample_rule('EV-1', owner='person:alice', severity='should')],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')

        with self.assertRaises(rs.StandardsRefusal) as cm_put:
            self.admin_std('put', '--file', str(f), '--actor', 'cora-op')
        self.assertEqual(409, cm_put.exception.status)
        self.assertEqual(rs.refusal_version_conflict(self.pid, 0, 1), str(cm_put.exception))

        ef = Path(self.tmp.name) / 'edit-ev.json'
        ef.write_text(json.dumps({
            'expected_version': 9,
            'reason': 'Stale edit.',
            'rule': _sample_rule('EV-1', owner='person:alice', severity='must'),
        }), encoding='utf-8')
        with self.assertRaises(rs.StandardsRefusal) as cm_edit:
            self.admin_std('rule-edit', '--rule', 'EV-1', '--file', str(ef), '--actor', 'cora-op')
        self.assertEqual(409, cm_edit.exception.status)
        self.assertEqual(rs.refusal_version_conflict(self.pid, 9, 1), str(cm_edit.exception))

        d_ans = self.endpoint_call(
            'standards', ['draft-propose', '@attachment:0'],
            actor='worker-bob',
            attachments=[json.dumps({'action': 'upsert', 'reason': 'Add EV-2.', 'rule': _sample_rule('EV-2')})],
        )
        did = json.loads(d_ans['stdout'])['id']
        with self.assertRaises(rs.StandardsRefusal) as cm_decide:
            self.admin_std('draft-decide', '--draft', did, '--decision', 'accept', '--expected-version', '0', '--reason', 'Stale', '--actor', 'cora-op')
        self.assertEqual(409, cm_decide.exception.status)
        self.assertEqual(rs.refusal_version_conflict(self.pid, 0, 1), str(cm_decide.exception))

    def test_staged_signoff_crash_recovery_and_derived_na_counter_no_double_count_on_replay(self):
        # Plan 2 Item 11 + Plan 3 Item 6 — Kills M20!
        f = Path(self.tmp.name) / 'reg-crash.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Must rule for crash & replay test.',
            'rules': [_sample_rule('CR-1', owner='person:alice', severity='must')],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')
        self.admin_std('set-gate', '--mode', 'block', '--actor', 'cora-op', '--reason', 'Block gate')

        tid = self.trackers[self.pid].create_task('Crash recovery task')
        repo = Path(self.tmp.name) / 'repo-crash'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'c.py').write_text('c = 1\n', encoding='utf-8')
        _git(repo, 'add', 'c.py')
        _git(repo, 'commit', '-m', 'init')
        ans_c, _, _ = self.contribute_bundle(tid, repo, op_id='op-cr-c1')
        cid = json.loads(ans_c['stdout'])['comment_id']

        approve_payload = {
            'schema_version': 1,
            'operation': 'approve',
            'operation_id': 'op-crash-approve-1',
            'task': tid,
            'previous': cid,
            'contribution': cid,
            'summary': 'Approve with NA check.',
            'checks': [{'rule_id': 'CR-1', 'result': 'not_applicable', 'reason': 'Config-only change.'}],
        }

        with mock.patch.object(rs.HostStandardsStore, 'commit_signoff', side_effect=RuntimeError('simulated crash after bd write')):
            crash_ans = self.review_op(approve_payload, actor='cora-op')
        self.assertEqual(124, crash_ans['returncode'])
        self.assertIn('simulated crash after bd write', crash_ans['stderr'])

        so_dir = self.project_path / '.standards' / 'signoffs'
        staged_file = so_dir / ('%s.staged.op-crash-approve-1.json' % cid)
        committed_file = so_dir / ('%s.committed.op-crash-approve-1.json' % cid)
        self.assertTrue(staged_file.is_file())
        self.assertFalse(committed_file.exists())

        orphan_file = so_dir / ('%s.staged.op-unreferenced-orphan.json' % cid)
        orphan_file.write_text(staged_file.read_text(encoding='utf-8'), encoding='utf-8')
        old_ts = time.time() - (rs.STAGED_TTL_SECONDS + 3600)
        os.utime(str(staged_file), (old_ts, old_ts))
        os.utime(str(orphan_file), (old_ts, old_ts))

        immediate = json.loads(self.endpoint_call('standards', ['sign-off', tid])['stdout'])
        self.assertIsNotNone(immediate)
        self.assertEqual(cid, immediate['contribution_id'])

        self.store().reconcile_or_clean_staged(list(self.trackers[self.pid].rows.values()), now_epoch=time.time())
        self.assertTrue(committed_file.is_file())
        self.assertFalse(staged_file.exists())
        self.assertFalse(orphan_file.exists())

        replay = self.review_op(approve_payload, actor='cora-op')
        self.assertEqual(0, replay['returncode'], replay['stderr'])
        self.assertTrue(json.loads(replay['stdout']).get('reconciled'))
        tally = rs.derive_reviewer_must_na_counts(self.store(), 'cora-op')
        self.assertEqual(1, tally)

    def test_r2_m1_closed_task_and_second_approve_gated_on_host(self):
        f = Path(self.tmp.name) / 'reg-m1.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Gate every approve.',
            'rules': [_sample_rule('MUST-1', owner='person:alice', severity='must')],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')
        self.admin_std('set-gate', '--mode', 'block', '--actor', 'cora-op', '--reason', 'Block gate')

        tid = self.trackers[self.pid].create_task('Closed and double approve task')
        repo = Path(self.tmp.name) / 'repo-m1'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'app.py').write_text('val = 1\n', encoding='utf-8')
        _git(repo, 'add', 'app.py')
        _git(repo, 'commit', '-m', 'c1')
        ans_c, _, _ = self.contribute_bundle(tid, repo, op_id='op-m1-c1')
        cid = json.loads(ans_c['stdout'])['comment_id']

        # 1) Close task, attempt ungated approve -> MUST be refused (rc 2), reopen -> still awaiting-review!
        self.trackers[self.pid].run_argv(['update', tid, '--status', 'closed'])
        closed_app = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m1-closed-bypass',
            'task': tid, 'previous': cid, 'contribution': cid, 'summary': 'Bypass while closed.',
        }, actor='cora-op')
        self.assertEqual(2, closed_app['returncode'], closed_app)
        self.assertIn('MUST-1', closed_app['stderr'])

        self.trackers[self.pid].run_argv(['update', tid, '--status', 'open'])
        r_after_reopen = json.loads(self.endpoint_call('review', [tid, '--json'], actor='cora-op')['stdout'])
        self.assertEqual('awaiting-review', r_after_reopen['review_state'])
        self.assertEqual(404, self.endpoint_call('standards', ['sign-off', tid]).get('standards_status'))

        # 2) Valid first approve succeeds and writes sign-off.
        first_payload = {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m1-first-ok',
            'task': tid, 'previous': cid, 'contribution': cid, 'summary': 'First valid approve.',
            'checks': [{'rule_id': 'MUST-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['app.py:1']}}],
        }
        app1 = self.review_op(first_payload, actor='cora-op')
        self.assertEqual(0, app1['returncode'], app1['stderr'])
        app1_cid = json.loads(app1['stdout'])['comment_id']
        so1 = json.loads(self.endpoint_call('standards', ['sign-off', tid])['stdout'])
        self.assertEqual('op-m1-first-ok', so1['operation_id'])

        # 3) Second approve without checks on already-approved contribution MUST be refused!
        second_bad = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m1-second-bad',
            'task': tid, 'previous': app1_cid, 'contribution': cid, 'summary': 'Second ungated approve.',
        }, actor='olive-op')
        self.assertEqual(2, second_bad['returncode'], second_bad)
        self.assertIn('MUST-1', second_bad['stderr'])
        so_still_1 = json.loads(self.endpoint_call('standards', ['sign-off', tid])['stdout'])
        self.assertEqual('op-m1-first-ok', so_still_1['operation_id'])

        # 4) Second approve WITH valid checks succeeds and updates committed sign-off; retry of first still reconciles!
        app2 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m1-second-ok',
            'task': tid, 'previous': app1_cid, 'contribution': cid, 'summary': 'Second checked approve.',
            'checks': [{'rule_id': 'MUST-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['app.py:1']}}],
        }, actor='olive-op')
        self.assertEqual(0, app2['returncode'], app2['stderr'])
        so2 = json.loads(self.endpoint_call('standards', ['sign-off', tid])['stdout'])
        self.assertEqual('op-m1-second-ok', so2['operation_id'])
        self.assertEqual('olive-op', so2['reviewer']['actor'])

        retry1 = self.review_op(first_payload, actor='cora-op')
        self.assertEqual(0, retry1['returncode'], retry1['stderr'])
        self.assertTrue(json.loads(retry1['stdout']).get('reconciled'))

    def test_r2_m2_partial_rerecord_and_predelivery_author_provenance_on_host(self):
        f = Path(self.tmp.name) / 'reg-m2.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Provenance per part.',
            'rules': [
                _sample_rule('STD-50', owner='person:alice', severity='must',
                             check={'kind': 'mechanical', 'tool': 'pytest', 'command': 'pytest -q'}),
                _sample_rule('PATH-1', owner='person:alice', severity='must',
                             applicability={'paths': ['secret/**']}),
            ],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')
        self.admin_std('set-gate', '--mode', 'block', '--actor', 'cora-op', '--reason', 'Block gate')

        # (a) Opus M2: cora-op records CI pass; independent ci-verifier records ONLY changed_files.
        tid1 = self.trackers[self.pid].create_task('Partial rerecord task')
        repo = Path(self.tmp.name) / 'repo-m2'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'README.md').write_text('hello\n', encoding='utf-8')
        _git(repo, 'add', 'README.md')
        _git(repo, 'commit', '-m', 'c1')
        ans1, commit1, _ = self.contribute_bundle(tid1, repo, op_id='op-m2-c1')
        cid1 = json.loads(ans1['stdout'])['comment_id']

        ci_part = Path(self.tmp.name) / 'm2-ci.json'
        ci_part.write_text(json.dumps({
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}],
        }), encoding='utf-8')
        diff_part = Path(self.tmp.name) / 'm2-diff.json'
        diff_part.write_text(json.dumps({'changed_files': ['README.md']}), encoding='utf-8')

        self.admin_std('record-commit', '--commit', commit1, '--file', str(ci_part), '--actor', 'cora-op')
        self.admin_std('record-commit', '--commit', commit1, '--file', str(diff_part), '--actor', 'ci-verifier')

        # cora-op cannot approve relying on her own CI pass even after ci-verifier recorded changed_files!
        self_app = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m2-cora-self',
            'task': tid1, 'previous': cid1, 'contribution': cid1, 'summary': 'Laundered CI attempt.',
        }, actor='cora-op')
        self.assertEqual(2, self_app['returncode'], self_app)
        self.assertIn('STD-50 (unverified: no CI result recorded', self_app['stderr'])

        # Independent olive-op CAN approve; sign-off credits STD-50 to cora-op and diff to ci-verifier!
        indep_app = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m2-olive-ok',
            'task': tid1, 'previous': cid1, 'contribution': cid1, 'summary': 'Independent approve.',
        }, actor='olive-op')
        self.assertEqual(0, indep_app['returncode'], indep_app['stderr'])
        so1 = json.loads(self.endpoint_call('standards', ['sign-off', tid1])['stdout'])
        self.assertEqual('ci-verifier', so1['diff_recorded_by'])
        std50_row = [r for r in so1['checks'] if r['rule_id'] == 'STD-50'][0]
        self.assertEqual('cora-op', std50_row['evidence']['recorded_by'])

        # (b) Fable MUST-2: author's party records fake changed_files + CI pass BEFORE delivering!
        s_bob_worker = sessions.execute(
            self.project_path, self.pid,
            ['register', '--name', 'Bob Worker', '--request-id', '00000000-0000-4000-8000-000000000011'],
            lambda: '', principal='person:bob',
        )['session']['actor']
        s_bob_ver = sessions.execute(
            self.project_path, self.pid,
            ['register', '--name', 'Bob Verifier', '--request-id', '00000000-0000-4000-8000-000000000012'],
            lambda: '', principal='person:bob',
        )['session']['actor']
        self.verifiers.append(s_bob_ver)
        self._write_deployment()

        (repo / 'secret').mkdir()
        (repo / 'secret' / 'k.py').write_text('secret = 1\n', encoding='utf-8')
        _git(repo, 'add', 'secret/k.py')
        _git(repo, 'commit', '-m', 'touch secret')
        commit2 = _git(repo, 'rev-parse', 'HEAD')

        tid2 = self.trackers[self.pid].create_task('Pre-delivery launder task', assignee=s_bob_worker)
        forged = Path(self.tmp.name) / 'm2-forged.json'
        forged.write_text(json.dumps({
            'changed_files': ['README.md'],
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}],
        }), encoding='utf-8')

        # Pre-delivery record-commit naming --task tid2 is refused because assignee s_bob_worker is same-party!
        with self.assertRaises(rs.StandardsRefusal) as cm_task:
            self.admin_std('record-commit', '--commit', commit2, '--task', tid2, '--file', str(forged), '--actor', s_bob_ver)
        self.assertEqual(403, cm_task.exception.status)

        # Even if recorded WITHOUT --task before any delivery mentions commit2, evaluation excludes author's party!
        self.admin_std('record-commit', '--commit', commit2, '--file', str(forged), '--actor', s_bob_ver)
        bpath2 = self.delivery_root / 'm2-c2.bundle'
        _, bsha2 = _make_bundle(repo, bpath2)
        ans_c2 = self.review_op({
            'schema_version': 1, 'operation': 'contribute', 'operation_id': 'op-m2-c2',
            'task': tid2, 'previous': None, 'supersedes': None, 'repository': str(repo),
            'commit': commit2, 'base_commit': commit1,
            'delivery': {'kind': 'bundle', 'path': str(bpath2), 'sha256': bsha2},
            'summary': 'Deliver secret change.',
        }, actor=s_bob_worker)
        self.assertEqual(0, ans_c2['returncode'], ans_c2['stderr'])
        cid2 = json.loads(ans_c2['stdout'])['comment_id']

        br2 = json.loads(self.endpoint_call('brief', ['--json', tid2], actor='olive-op')['stdout'])['standards']
        self.assertEqual('diff_unverified', br2['diff_status'])
        self.assertIn('PATH-1', [r['id'] for r in br2['conditional_rules']])

        ref2 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-m2-c2-app',
            'task': tid2, 'previous': cid2, 'contribution': cid2, 'summary': 'Try approving forged pre-delivery CI.',
        }, actor='olive-op')
        self.assertEqual(2, ref2['returncode'], ref2)
        self.assertIn('STD-50 (unverified: no CI result recorded', ref2['stderr'])
        self.assertIn('PATH-1', ref2['stderr'])

    def test_r2_m3_s1_s2_detach_inherits_empty_put_authority_and_na_over_ci_fail(self):
        # s1: non-coordinator/non-editor PUT refused even with empty rules at v0 and v1!
        with self.assertRaises(rs.StandardsRefusal) as cm_s1_v0:
            rs.put_register(
                self.store(),
                {'expected_version': 0, 'reason': 'Viewer empty v1', 'rules': []},
                actor='person:viewer', author_person='person:viewer',
                is_person_session=True, is_coordinator_or_owner=False,
            )
        self.assertEqual(403, cm_s1_v0.exception.status)
        self.assertEqual(rs.refusal_put_register_authority(), str(cm_s1_v0.exception))
        self.assertEqual('none', self.store().inspect_register_raw()[0])

        # m3: child cannot clear or switch inherits while current parent defines any must rule!
        other_shared_path = self.root / 'projects' / 'shared2'
        (other_shared_path / '.beads').mkdir(parents=True, exist_ok=True)
        (other_shared_path / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        self.trackers['shared2'] = HostTracker('shared2')

        sh1 = Path(self.tmp.name) / 'sh1.json'
        sh1.write_text(json.dumps({
            'expected_version': 0, 'shared': True, 'reason': 'Shared must',
            'rules': [_sample_rule('TEAM-1', owner='person:alice', severity='must')],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(sh1), '--actor', 'cora-op', project=self.shared_pid)

        sh2 = Path(self.tmp.name) / 'sh2.json'
        sh2.write_text(json.dumps({
            'expected_version': 0, 'shared': True, 'reason': 'Shared should only',
            'rules': [_sample_rule('OTHER-1', owner='person:alice', severity='should')],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(sh2), '--actor', 'cora-op', project='shared2')

        ch1 = Path(self.tmp.name) / 'ch1.json'
        ch1.write_text(json.dumps({
            'expected_version': 0, 'inherits': self.shared_pid, 'reason': 'Inherit shared', 'rules': [],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(ch1), '--actor', 'cora-op')

        for bad_inh in (None, 'shared2'):
            with self.subTest(new_inherits=bad_inh):
                bad_f = Path(self.tmp.name) / ('ch-detach-%s.json' % (bad_inh or 'null'))
                bad_f.write_text(json.dumps({
                    'expected_version': 1, 'inherits': bad_inh, 'reason': 'Try detaching TEAM-1', 'rules': [],
                }), encoding='utf-8')
                with self.assertRaises(rs.StandardsRefusal) as cm_m3:
                    self.admin_std('put', '--file', str(bad_f), '--actor', 'cora-op')
                self.assertEqual(422, cm_m3.exception.status)
                self.assertEqual(
                    rs.refusal_detach_inherited_must(self.pid, self.shared_pid, ['TEAM-1']),
                    str(cm_m3.exception),
                )
        self.assertEqual(1, self.store().inspect_register_raw()[1]['current_version'])

        # s2: not_applicable refused over recorded CI fail on mechanical must rule; waiver keeps ci_evidence!
        ch_mech = Path(self.tmp.name) / 'ch-mech.json'
        ch_mech.write_text(json.dumps({
            'expected_version': 1, 'inherits': self.shared_pid, 'reason': 'Add MECH-2',
            'rules': [_sample_rule('MECH-2', owner='person:alice', severity='must',
                                   check={'kind': 'mechanical', 'tool': 'ruff', 'command': 'ruff check'})],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(ch_mech), '--actor', 'cora-op')
        self.admin_std('set-gate', '--mode', 'block', '--actor', 'cora-op', '--reason', 'Block gate')

        tid = self.trackers[self.pid].create_task('S2 mechanical fail task')
        repo = Path(self.tmp.name) / 'repo-s2'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 's2.py').write_text('x = 1\n', encoding='utf-8')
        _git(repo, 'add', 's2.py')
        _git(repo, 'commit', '-m', 'c1')
        ans_c, commit, _ = self.contribute_bundle(tid, repo, op_id='op-s2-c1')
        cid = json.loads(ans_c['stdout'])['comment_id']

        ci_fail = Path(self.tmp.name) / 's2-fail.json'
        ci_fail.write_text(json.dumps({
            'changed_files': ['s2.py'],
            'ci_checks': [{'rule_id': 'MECH-2', 'tool': 'ruff', 'command': 'ruff check', 'exit_code': 1}],
        }), encoding='utf-8')
        self.admin_std('record-commit', '--commit', commit, '--file', str(ci_fail), '--actor', 'ci-verifier')

        na_ref = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-s2-na-ref',
            'task': tid, 'previous': cid, 'contribution': cid, 'summary': 'Try NA over CI fail.',
            'checks': [
                {'rule_id': 'TEAM-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['s2.py:1']}},
                {'rule_id': 'MECH-2', 'result': 'not_applicable', 'reason': 'Linter flaky today.'},
            ],
        }, actor='olive-op')
        self.assertEqual(2, na_ref['returncode'], na_ref)
        self.assertIn(rs.refusal_na_over_mechanical_fail('MECH-2', commit), na_ref['stderr'])

        self.admin_std(
            'waive', '--task', tid, '--rule', 'MECH-2', '--contribution', cid, '--commit', commit,
            '--reason', 'Known upstream ruff false positive', '--actor', 'olive-op',
        )
        ok_app = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-s2-waived-ok',
            'task': tid, 'previous': cid, 'contribution': cid, 'summary': 'Approved with waiver.',
            'checks': [{'rule_id': 'TEAM-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['s2.py:1']}}],
        }, actor='olive-op')
        self.assertEqual(0, ok_app['returncode'], ok_app['stderr'])
        so = json.loads(self.endpoint_call('standards', ['sign-off', tid])['stdout'])
        mech_row = [r for r in so['checks'] if r['rule_id'] == 'MECH-2'][0]
        self.assertEqual('fail', mech_row['result'])
        self.assertEqual(1, mech_row['ci_evidence']['exit_code'])
        self.assertEqual('ci-verifier', mech_row['ci_evidence']['recorded_by'])

    def test_r2_blob_cap_m15_admin_cli_ops_and_symlink_signoffs_resilience(self):
        # 1) Kills M15 (MAX_BLOB_BYTES cap): > 10 MiB blob in delivered bundle is refused when cited!
        f = Path(self.tmp.name) / 'reg-blob.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Citation cap rule.',
            'rules': [_sample_rule('BLOB-1', owner='person:alice', severity='must')],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')
        self.admin_std('set-gate', '--mode', 'block', '--actor', 'cora-op', '--reason', 'Block gate')

        tid = self.trackers[self.pid].create_task('Oversized blob citation task')
        repo = Path(self.tmp.name) / 'repo-blob'
        repo.mkdir()
        _git(repo, 'init')
        # 2,120,000 * 5 bytes = 10,600,000 bytes (> 10 MiB MAX_BLOB_BYTES), highly compressible in git pack!
        (repo / 'huge.txt').write_text('line\n' * 2120000, encoding='utf-8')
        (repo / 'small.py').write_text('ok = 1\n', encoding='utf-8')
        _git(repo, 'add', 'huge.txt', 'small.py')
        _git(repo, 'commit', '-m', 'add huge and small')
        ans_c, commit, _ = self.contribute_bundle(tid, repo, op_id='op-blob-c1')
        cid = json.loads(ans_c['stdout'])['comment_id']

        huge_ref = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-blob-huge',
            'task': tid, 'previous': cid, 'contribution': cid, 'summary': 'Cite >10MiB blob.',
            'checks': [{'rule_id': 'BLOB-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['huge.txt:1']}}],
        }, actor='cora-op')
        self.assertEqual(2, huge_ref['returncode'], huge_ref)
        self.assertIn(
            rs.refusal_citation_unresolved('huge.txt:1', 'BLOB-1', commit, 'path is not a regular text file'),
            huge_ref['stderr'],
        )

        # Citing small.py:1 succeeds, then verify admin.py standards read ops (history, draft-list, waivers, sign-off)!
        small_ok = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-blob-small',
            'task': tid, 'previous': cid, 'contribution': cid, 'summary': 'Cite small.py.',
            'checks': [{'rule_id': 'BLOB-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['small.py:1']}}],
        }, actor='cora-op')
        self.assertEqual(0, small_ok['returncode'], small_ok['stderr'])

        hist = self.admin_std('history')
        self.assertEqual([1], [v['version'] for v in hist['items']])
        dlist = self.admin_std('draft-list', '--status', 'pending')
        self.assertEqual([], dlist['items'])
        wlist = self.admin_std('waivers', '--task', tid)
        self.assertEqual([], wlist['items'])
        so_md = self.admin_std('sign-off', '--task', tid, '--format', 'markdown')
        self.assertIn('Checked against standards register v1', so_md)

        # 2) Opus N2/N3: symlinked .standards/signoffs never stops contribute/request-changes/respond,
        #    and approve fails closed with 409 StandardsRefusal!
        so_dir = self.project_path / '.standards' / 'signoffs'
        backup_so = self.project_path / '.standards_so_bak'
        so_dir.rename(backup_so)
        so_dir.symlink_to(backup_so)

        tid2 = self.trackers[self.pid].create_task('Symlink signoffs task', assignee='worker-bob')
        (repo / 'small.py').write_text('ok = 2\n', encoding='utf-8')
        _git(repo, 'add', 'small.py')
        _git(repo, 'commit', '-m', 'c2')
        ans_c2, _, _ = self.contribute_bundle(tid2, repo, actor='worker-bob', op_id='op-sym-c2')
        self.assertEqual(0, ans_c2['returncode'], ans_c2['stderr'])
        cid2 = json.loads(ans_c2['stdout'])['comment_id']

        rc_ans = self.review_op({
            'schema_version': 1, 'operation': 'request-changes', 'operation_id': 'op-sym-rc',
            'task': tid2, 'previous': cid2, 'contribution': cid2, 'summary': 'Fix small.py.',
            'items': [{'id': 'fix-1', 'text': 'Update value.'}],
        }, actor='cora-op')
        self.assertEqual(0, rc_ans['returncode'], rc_ans['stderr'])
        rc_cid = json.loads(rc_ans['stdout'])['comment_id']

        resp_ans = self.review_op({
            'schema_version': 1, 'operation': 'respond', 'operation_id': 'op-sym-resp',
            'task': tid2, 'previous': rc_cid, 'contribution': cid2,
            'resolutions': [{'request': rc_cid, 'item': 'fix-1', 'reason': 'Updated.', 'evidence': 'small.py:1'}],
        }, actor='worker-bob')
        self.assertEqual(0, resp_ans['returncode'], resp_ans['stderr'])
        resp_cid = json.loads(resp_ans['stdout'])['comment_id']

        sym_app = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-sym-app',
            'task': tid2, 'previous': resp_cid, 'contribution': cid2, 'summary': 'Approve with symlinked signoffs.',
            'checks': [{'rule_id': 'BLOB-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['small.py:1']}}],
        }, actor='cora-op')
        self.assertEqual(2, sym_app['returncode'], sym_app)
        self.assertEqual(409, sym_app.get('standards_status'), sym_app)
        self.assertEqual(409, sym_app.get('standards_status'))

    def test_r3_m1_no_eviction_per_party_and_256_flood_caps_on_host(self):
        # m1-no-eviction: per-recorder-party cap (32) + total cap (256), 409 refused, zero eviction,
        # and an independent verifier's recorded FAIL + N/A refusal survive the full 256-entry flood!
        f = Path(self.tmp.name) / 'reg-r3-m1.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Mechanical rule for flood test.',
            'rules': [_sample_rule(
                'STD-50', owner='person:alice', severity='must',
                check={'kind': 'mechanical', 'tool': 'pytest', 'command': 'pytest -q'},
            )],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')
        self.admin_std('set-gate', '--mode', 'block', '--actor', 'cora-op', '--reason', 'Block gate')

        tid = self.trackers[self.pid].create_task('Flood resilience task')
        repo = Path(self.tmp.name) / 'repo-r3-m1'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'app.py').write_text('x = 1\n', encoding='utf-8')
        _git(repo, 'add', 'app.py')
        _git(repo, 'commit', '-m', 'c1')
        ans_c, commit, _ = self.contribute_bundle(tid, repo, op_id='op-r3-m1-c1')
        cid = json.loads(ans_c['stdout'])['comment_id']

        # Entry #1: independent ci-verifier records STD-50 FAIL (exit 1).
        fail_file = Path(self.tmp.name) / 'r3-m1-fail.json'
        fail_file.write_text(json.dumps({
            'changed_files': ['app.py'],
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}],
        }), encoding='utf-8')
        self.admin_std('record-commit', '--commit', commit, '--file', str(fail_file), '--actor', 'ci-verifier')

        # Register two verifier sessions owned by the same person ('person:flood1') to verify per-PARTY cap (32)!
        s_f1a = sessions.execute(
            self.project_path, self.pid,
            ['register', '--name', 'Flood 1A', '--request-id', '00000000-0000-4000-8000-000000000101'],
            lambda: '', principal='person:flood1',
        )['session']['actor']
        s_f1b = sessions.execute(
            self.project_path, self.pid,
            ['register', '--name', 'Flood 1B', '--request-id', '00000000-0000-4000-8000-000000000102'],
            lambda: '', principal='person:flood1',
        )['session']['actor']
        extra_verifiers = [s_f1a, s_f1b]
        for idx in range(2, 10):
            v_actor = sessions.execute(
                self.project_path, self.pid,
                ['register', '--name', 'Flood %d' % idx, '--request-id', '00000000-0000-4000-8000-%012d' % (200 + idx)],
                lambda: '', principal='person:flood%d' % idx,
            )['session']['actor']
            extra_verifiers.append(v_actor)
        self.verifiers.extend(extra_verifiers)
        self._write_deployment()

        benign_file = Path(self.tmp.name) / 'r3-m1-benign.json'
        benign_file.write_text(json.dumps({'changed_files': ['app.py']}), encoding='utf-8')

        # person:flood1 records 20 via s_f1a and 12 via s_f1b (= 32 total for person:flood1); 33rd is refused 409!
        for _ in range(20):
            self.admin_std('record-commit', '--commit', commit, '--file', str(benign_file), '--actor', s_f1a)
        for _ in range(12):
            self.admin_std('record-commit', '--commit', commit, '--file', str(benign_file), '--actor', s_f1b)
        with self.assertRaises(rs.StandardsRefusal) as cm_party:
            self.admin_std('record-commit', '--commit', commit, '--file', str(benign_file), '--actor', s_f1b)
        self.assertEqual(409, cm_party.exception.status)
        self.assertEqual(
            rs.refusal_commit_observation_recorder_cap(commit, rs.COMMIT_OBSERVATIONS_PER_RECORDER_MAX),
            str(cm_party.exception),
        )

        # Fill remaining slots up to 256 total (currently 1 + 32 = 33; add 7 * 31 + 6 = 223 -> 256 total)!
        other_flooders = extra_verifiers[2:]  # 8 distinct principals: flood2..flood9
        for v_act in other_flooders[:7]:
            for _ in range(31):
                self.admin_std('record-commit', '--commit', commit, '--file', str(benign_file), '--actor', v_act)
        for _ in range(6):
            self.admin_std('record-commit', '--commit', commit, '--file', str(benign_file), '--actor', other_flooders[7])

        stored = self.store().read_commit_observation(commit)
        self.assertEqual(256, len(stored['observations']))
        self.assertEqual('ci-verifier', stored['observations'][0]['recorded_by'])
        self.assertEqual(1, stored['observations'][0]['ci_checks'][0]['exit_code'])

        # 257th observation from an unexhausted party (other_flooders[7] only has 6 < 32) is refused 409!
        with self.assertRaises(rs.StandardsRefusal) as cm_total:
            self.admin_std('record-commit', '--commit', commit, '--file', str(benign_file), '--actor', other_flooders[7])
        self.assertEqual(409, cm_total.exception.status)
        self.assertEqual(
            rs.refusal_commit_observation_cap(commit, rs.COMMIT_OBSERVATIONS_MAX),
            str(cm_total.exception),
        )
        # Nothing evicted; ci-verifier's FAIL is still entry #0 and N/A is still refused 422!
        stored_after = self.store().read_commit_observation(commit)
        self.assertEqual(256, len(stored_after['observations']))
        self.assertEqual('ci-verifier', stored_after['observations'][0]['recorded_by'])

        na_attempt = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-m1-na-after-flood',
            'task': tid, 'previous': cid, 'contribution': cid, 'summary': 'Try N/A after 256 flood.',
            'checks': [{'rule_id': 'STD-50', 'result': 'not_applicable', 'reason': 'Claim not applicable.'}],
        }, actor='olive-op')
        self.assertEqual(2, na_attempt['returncode'], na_attempt)
        self.assertIn(rs.refusal_na_over_mechanical_fail('STD-50', commit), na_attempt['stderr'])

    def test_r3_m2_fails_always_count_for_own_party_and_recommender_on_host(self):
        # m2-fails-always-count: Opus S2P cases (1) and (2) + Fable W8 on the host.
        f = Path(self.tmp.name) / 'reg-r3-m2.json'
        f.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Mechanical fail retention rule.',
            'rules': [_sample_rule(
                'STD-50', owner='person:alice', severity='must',
                applicability={'paths': ['src/**']},
                check={'kind': 'mechanical', 'tool': 'pytest', 'command': 'pytest -q'},
            )],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(f), '--actor', 'cora-op')
        self.admin_std('set-gate', '--mode', 'block', '--actor', 'cora-op', '--reason', 'Block gate')

        repo = Path(self.tmp.name) / 'repo-r3-m2'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'src').mkdir()
        (repo / 'src' / 'mod.py').write_text('v = 1\n', encoding='utf-8')
        _git(repo, 'add', 'src/mod.py')
        _git(repo, 'commit', '-m', 'c1')

        # Case (1) / Fable W8: approver olive-op's own party records CI FAIL + narrowing changed_files=['README.md'].
        # changed_files is excluded (cannot help olive-op skip STD-50), while CI FAIL counts!
        tid1 = self.trackers[self.pid].create_task('S2P case 1 own-party fail')
        ans1, commit1, _ = self.contribute_bundle(tid1, repo, op_id='op-r3-m2-c1')
        cid1 = json.loads(ans1['stdout'])['comment_id']

        own_fail_f = Path(self.tmp.name) / 'r3-m2-ownfail.json'
        own_fail_f.write_text(json.dumps({
            'changed_files': ['README.md'],
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}],
        }), encoding='utf-8')
        self.admin_std('record-commit', '--commit', commit1, '--file', str(own_fail_f), '--actor', 'olive-op')

        # Also olive-op recording a subsequent PASS cannot clear the FAIL for olive-op!
        own_pass_f = Path(self.tmp.name) / 'r3-m2-ownpass.json'
        own_pass_f.write_text(json.dumps({
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}],
        }), encoding='utf-8')
        self.admin_std('record-commit', '--commit', commit1, '--file', str(own_pass_f), '--actor', 'olive-op')

        na1 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-m2-case1-na',
            'task': tid1, 'previous': cid1, 'contribution': cid1, 'summary': 'Own-party fail NA attempt.',
            'checks': [{'rule_id': 'STD-50', 'result': 'not_applicable', 'reason': 'Try skipping.'}],
        }, actor='olive-op')
        self.assertEqual(2, na1['returncode'], na1)
        self.assertIn(rs.refusal_na_over_mechanical_fail('STD-50', commit1), na1['stderr'])

        plain1 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-m2-case1-plain',
            'task': tid1, 'previous': cid1, 'contribution': cid1, 'summary': 'Own-party fail plain attempt.',
        }, actor='olive-op')
        self.assertEqual(2, plain1['returncode'], plain1)
        self.assertIn('fail without waiver', plain1['stderr'])

        # Waiver from independent coordinator cora-op lets olive-op approve, and sign-off shows olive-op's FAIL!
        self.admin_std(
            'waive', '--task', tid1, '--rule', 'STD-50', '--contribution', cid1, '--commit', commit1,
            '--reason', 'Documented CI flake', '--actor', 'cora-op',
        )
        ok1 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-m2-case1-ok',
            'task': tid1, 'previous': cid1, 'contribution': cid1, 'summary': 'Approved with waiver.',
        }, actor='olive-op')
        self.assertEqual(0, ok1['returncode'], ok1['stderr'])
        so1 = json.loads(self.endpoint_call('standards', ['sign-off', tid1])['stdout'])
        self.assertEqual('diff_unverified', so1['diff_status'])
        row1 = [r for r in so1['checks'] if r['rule_id'] == 'STD-50'][0]
        self.assertEqual('fail', row1['result'])
        self.assertEqual(1, row1['ci_evidence']['exit_code'])
        self.assertEqual('olive-op', row1['ci_evidence']['recorded_by'])

        # Case (2): independent verifier records FAIL, then same person posts recommend, then cora-op approves!
        s_olga_rev = sessions.execute(
            self.project_path, self.pid,
            ['register', '--name', 'Olga Reviewer', '--request-id', '00000000-0000-4000-8000-000000000301'],
            lambda: '', principal='person:olga',
        )['session']['actor']
        s_olga_ver = sessions.execute(
            self.project_path, self.pid,
            ['register', '--name', 'Olga Verifier', '--request-id', '00000000-0000-4000-8000-000000000302'],
            lambda: '', principal='person:olga',
        )['session']['actor']
        self.verifiers.append(s_olga_ver)
        self._write_deployment()

        (repo / 'src' / 'mod.py').write_text('v = 2\n', encoding='utf-8')
        _git(repo, 'add', 'src/mod.py')
        _git(repo, 'commit', '-m', 'c2')
        tid2 = self.trackers[self.pid].create_task('S2P case 2 recommender fail')
        ans2, commit2, _ = self.contribute_bundle(tid2, repo, op_id='op-r3-m2-c2')
        cid2 = json.loads(ans2['stdout'])['comment_id']

        olga_fail_f = Path(self.tmp.name) / 'r3-m2-olgafail.json'
        olga_fail_f.write_text(json.dumps({
            'changed_files': ['src/mod.py'],
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}],
        }), encoding='utf-8')
        self.admin_std('record-commit', '--commit', commit2, '--file', str(olga_fail_f), '--actor', s_olga_ver)

        rec_ans = self.review_op({
            'schema_version': 1, 'operation': 'recommend', 'operation_id': 'op-r3-m2-olga-rec',
            'task': tid2, 'contribution': cid2, 'commit': commit2, 'verdict': 'approve',
            'summary': 'Code looks fine despite CI fail.', 'items': [],
        }, actor=s_olga_rev)
        self.assertEqual(0, rec_ans['returncode'], rec_ans['stderr'])

        na2 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-m2-case2-na',
            'task': tid2, 'previous': cid2, 'contribution': cid2, 'summary': 'Cora tries NA over Olga fail.',
            'checks': [{'rule_id': 'STD-50', 'result': 'not_applicable', 'reason': 'Ignore Olga CI fail.'}],
        }, actor='cora-op')
        self.assertEqual(2, na2['returncode'], na2)
        self.assertIn(rs.refusal_na_over_mechanical_fail('STD-50', commit2), na2['stderr'])

        plain2 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-m2-case2-plain',
            'task': tid2, 'previous': cid2, 'contribution': cid2, 'summary': 'Cora tries plain approve.',
        }, actor='cora-op')
        self.assertEqual(2, plain2['returncode'], plain2)
        self.assertIn('fail without waiver', plain2['stderr'])

        self.admin_std(
            'waive', '--task', tid2, '--rule', 'STD-50', '--contribution', cid2, '--commit', commit2,
            '--reason', 'Waived by olive-op', '--actor', 'olive-op',
        )
        ok2 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-m2-case2-ok',
            'task': tid2, 'previous': cid2, 'contribution': cid2, 'summary': 'Cora approves with waiver.',
        }, actor='cora-op')
        self.assertEqual(0, ok2['returncode'], ok2['stderr'])
        so2 = json.loads(self.endpoint_call('standards', ['sign-off', tid2])['stdout'])
        row2 = [r for r in so2['checks'] if r['rule_id'] == 'STD-50'][0]
        self.assertEqual('fail', row2['result'])
        self.assertEqual(s_olga_ver, row2['ci_evidence']['recorded_by'])

    def test_r3_s1_append_only_signoff_history_tally_retry_and_crash_parity_plus_n1c_on_host(self):
        # s1-signoff-history + n1(c): append-only sign-offs, preserved must-N/A tally, exact retry
        # returning its own sign-off, NOTE-1 crash-window parity, MR9 diff-first recorder, NOTE-5 deleted parent!
        # 1) NOTE-5: child can detach from a deleted shared parent project, but NOT a symlinked one!
        sh_file = Path(self.tmp.name) / 'r3-sh.json'
        sh_file.write_text(json.dumps({
            'expected_version': 0, 'shared': True, 'reason': 'Shared must rule',
            'rules': [_sample_rule('SHM-1', owner='person:alice', severity='must')],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(sh_file), '--actor', 'cora-op', project=self.shared_pid)

        ch_inh = Path(self.tmp.name) / 'r3-ch-inh.json'
        ch_inh.write_text(json.dumps({
            'expected_version': 0, 'inherits': self.shared_pid, 'reason': 'Inherit shared',
            'rules': [_sample_rule('STD-1', owner='person:alice', severity='must')],
        }), encoding='utf-8')
        self.admin_std('put', '--file', str(ch_inh), '--actor', 'cora-op')

        # Replace shared_path with a symlink -> detaching must be refused 409 (damaged, NOT treated as deleted)!
        sh_bak = self.root / 'projects' / 'shared_bak_dir'
        self.shared_path.rename(sh_bak)
        self.shared_path.symlink_to(sh_bak)
        ch_detach = Path(self.tmp.name) / 'r3-ch-detach.json'
        ch_detach.write_text(json.dumps({
            'expected_version': 1, 'inherits': None, 'reason': 'Detach parent',
            'rules': [_sample_rule('STD-1', owner='person:alice', severity='must')],
        }), encoding='utf-8')
        with self.assertRaises(rs.StandardsRefusal) as cm_sym_parent:
            self.admin_std('put', '--file', str(ch_detach), '--actor', 'cora-op')
        self.assertEqual(409, cm_sym_parent.exception.status)

        # Now genuinely delete shared_path -> child CAN leave the non-existent shared parent project!
        self.shared_path.unlink()
        shutil.rmtree(sh_bak)
        detached_res = self.admin_std('put', '--file', str(ch_detach), '--actor', 'cora-op')
        self.assertEqual(2, detached_res['version'])
        self.assertIsNone(self.admin_std('show')['inherits'])

        # 2) MR9 / R2e: diff recorded FIRST by verifier A, CI recorded SECOND by verifier B -> diff_recorded_by == verifier A!
        self.verifiers.append('diff-verifier-a')
        self._write_deployment()
        self.admin_std('set-gate', '--mode', 'block', '--actor', 'cora-op', '--reason', 'Block gate')

        repo = Path(self.tmp.name) / 'repo-r3-s1'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'f.py').write_text('a = 1\n', encoding='utf-8')
        _git(repo, 'add', 'f.py')
        _git(repo, 'commit', '-m', 'c1')
        tid1 = self.trackers[self.pid].create_task('Multi-approver signoff task')
        ans1, commit1, _ = self.contribute_bundle(tid1, repo, op_id='op-r3-s1-c1')
        cid1 = json.loads(ans1['stdout'])['comment_id']

        diff_first = Path(self.tmp.name) / 'r3-diff-first.json'
        diff_first.write_text(json.dumps({'changed_files': ['f.py']}), encoding='utf-8')
        ci_second = Path(self.tmp.name) / 'r3-ci-second.json'
        ci_second.write_text(json.dumps({
            'ci_checks': [{'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}],
        }), encoding='utf-8')
        self.admin_std('record-commit', '--commit', commit1, '--file', str(diff_first), '--actor', 'diff-verifier-a')
        self.admin_std('record-commit', '--commit', commit1, '--file', str(ci_second), '--actor', 'ci-verifier')

        # 3) s1-signoff-history: cora-op approves first with STD-1: not_applicable (tally = 1).
        app1_payload = {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-s1-app1',
            'task': tid1, 'previous': cid1, 'contribution': cid1, 'summary': 'First approve with N/A.',
            'checks': [{'rule_id': 'STD-1', 'result': 'not_applicable', 'reason': 'Not applicable here.'}],
        }
        app1 = self.review_op(app1_payload, actor='cora-op')
        self.assertEqual(0, app1['returncode'], app1['stderr'])
        app1_body = json.loads(app1['stdout'])
        app1_cid = app1_body['comment_id']
        self.assertEqual('diff-verifier-a', app1_body['sign_off']['diff_recorded_by'])
        self.assertEqual(1, app1_body['sign_off']['reviewer_must_not_applicable_total'])

        # olive-op approves second on the SAME contribution with STD-1: pass (citations)!
        app2_payload = {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-s1-app2',
            'task': tid1, 'previous': app1_cid, 'contribution': cid1, 'summary': 'Second approve with citation.',
            'checks': [{'rule_id': 'STD-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['f.py:1']}}],
        }
        app2 = self.review_op(app2_payload, actor='olive-op')
        self.assertEqual(0, app2['returncode'], app2['stderr'])
        app2_so = json.loads(app2['stdout'])['sign_off']
        self.assertEqual('olive-op', app2_so['reviewer']['actor'])
        self.assertEqual(0, app2_so['reviewer_must_not_applicable_total'])

        # Both committed files exist in .standards/signoffs/!
        so_dir = self.project_path / '.standards' / 'signoffs'
        self.assertTrue((so_dir / ('%s.committed.op-r3-s1-app1.json' % cid1)).is_file())
        self.assertTrue((so_dir / ('%s.committed.op-r3-s1-app2.json' % cid1)).is_file())

        # Exact retry of cora-op's first approve returns cora-op's OWN sign-off, NOT olive-op's!
        retry1 = self.review_op(app1_payload, actor='cora-op')
        self.assertEqual(0, retry1['returncode'], retry1['stderr'])
        retry1_body = json.loads(retry1['stdout'])
        self.assertTrue(retry1_body['reconciled'])
        self.assertEqual('op-r3-s1-app1', retry1_body['sign_off']['operation_id'])
        self.assertEqual('cora-op', retry1_body['sign_off']['reviewer']['actor'])
        self.assertEqual('not_applicable', retry1_body['sign_off']['checks'][0]['result'])
        self.assertEqual(1, retry1_body['sign_off']['reviewer_must_not_applicable_total'])

        # Reading sign-off shows latest (olive-op) + earlier_sign_offs ([cora-op])!
        so_latest = json.loads(self.endpoint_call('standards', ['sign-off', tid1])['stdout'])
        self.assertEqual('op-r3-s1-app2', so_latest['operation_id'])
        self.assertEqual('olive-op', so_latest['reviewer']['actor'])
        self.assertEqual(1, len(so_latest['earlier_sign_offs']))
        self.assertEqual('op-r3-s1-app1', so_latest['earlier_sign_offs'][0]['operation_id'])
        self.assertEqual('cora-op', so_latest['earlier_sign_offs'][0]['reviewer']['actor'])
        self.assertEqual(1, so_latest['earlier_sign_offs'][0]['reviewer_must_not_applicable_total'])

        md_out = self.admin_std('sign-off', '--task', tid1, '--format', 'markdown')
        self.assertIn('Earlier Approvals on This Delivery', md_out)
        self.assertIn('cora-op', md_out)
        txt_out = self.admin_std('sign-off', '--task', tid1, '--format', 'text')
        self.assertIn('earlier_sign_off: cora-op', txt_out)

        # cora-op's must-N/A tally survived olive-op's second approve: next N/A approval gives total == 2!
        tid2 = self.trackers[self.pid].create_task('Second task for cora tally')
        ans2, _, _ = self.contribute_bundle(tid2, repo, op_id='op-r3-s1-c2')
        cid2 = json.loads(ans2['stdout'])['comment_id']
        app_t2 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r3-s1-t2-app',
            'task': tid2, 'previous': cid2, 'contribution': cid2, 'summary': 'Second task N/A.',
            'checks': [{'rule_id': 'STD-1', 'result': 'not_applicable', 'reason': 'Also N/A.'}],
        }, actor='cora-op')
        self.assertEqual(0, app_t2['returncode'], app_t2['stderr'])
        self.assertEqual(2, json.loads(app_t2['stdout'])['sign_off']['reviewer_must_not_applicable_total'])

        # 4) NOTE-1: staged-vs-committed crash-window precedence parity between HostStandardsStore and DictStandardsStore!
        dict_root = {}
        d_store = rs.DictStandardsStore(dict_root, self.pid)
        h_store = self.store()
        for st in (h_store, d_store):
            st.commit_signoff('c-parity', 'op-a1', {
                'schema_version': 1, 'status': 'committed', 'contribution_id': 'c-parity',
                'commit': commit1, 'operation_id': 'op-a1', 'reviewer': {'actor': 'cora-op'}, 'checks': [],
            })
            st.stage_signoff('c-parity', 'op-a2', {
                'schema_version': 1, 'status': 'staged', 'contribution_id': 'c-parity',
                'commit': commit1, 'operation_id': 'op-a2', 'reviewer': {'actor': 'olive-op'}, 'checks': [],
            })
        # When op-a2 landed in comments but commit_signoff hasn't run yet, BOTH stores return op-a2's staged sign-off!
        self.assertEqual('op-a2', h_store.read_signoff('c-parity', approve_operation_id='op-a2')['operation_id'])
        self.assertEqual('op-a2', d_store.read_signoff('c-parity', approve_operation_id='op-a2')['operation_id'])
        self.assertEqual('staged', h_store.read_signoff('c-parity', approve_operation_id='op-a2')['status'])
        self.assertEqual('staged', d_store.read_signoff('c-parity', approve_operation_id='op-a2')['status'])
        # And when op-a2 is ALSO committed alongside a leftover staged file, BOTH return committed!
        for st in (h_store, d_store):
            st.commit_signoff('c-parity', 'op-a2', {
                'schema_version': 1, 'status': 'committed', 'contribution_id': 'c-parity',
                'commit': commit1, 'operation_id': 'op-a2', 'reviewer': {'actor': 'olive-op'}, 'checks': [],
            })
            st.stage_signoff('c-parity', 'op-a2', {
                'schema_version': 1, 'status': 'staged', 'contribution_id': 'c-parity',
                'commit': commit1, 'operation_id': 'op-a2', 'reviewer': {'actor': 'stale'}, 'checks': [],
            })
        self.assertEqual('committed', h_store.read_signoff('c-parity', approve_operation_id='op-a2')['status'])
        self.assertEqual('committed', d_store.read_signoff('c-parity', approve_operation_id='op-a2')['status'])

    def test_r4_m1b_party_byte_budget_document_byte_cap_and_fail_reserve(self):
        # m1b-byte-budget + Fable NOTE-3 (X-M3) + Fable NOTE-6 + Opus Q1e/Q1f + PM RULE!
        extra_verifiers = ['ver-b%d' % i for i in range(10)]
        self.verifiers.extend(extra_verifiers)
        self._write_deployment()

        self.admin_std('put', data={
            'expected_version': 0, 'reason': 'Init r4 m1b',
            'rules': [_sample_rule(
                'STD-50', owner='person:cora', severity='must',
                check={'kind': 'mechanical', 'tool': 'pytest', 'command': 'pytest -q'},
            )],
        })
        self.admin_std('set-gate', '--mode', 'block', '--reason', 'Block mode')

        repo = Path(self.tmp.name) / 'repo_r4_m1b'
        self.init_git_repo(repo, {'app.py': 'x = 1\n'})
        tid1 = self.trackers[self.pid].create_task('R4 m1b single-party flood')
        ans1, commit1, _ = self.contribute_bundle(tid1, repo, op_id='op-r4-m1b-c1')
        cid1 = json.loads(ans1['stdout'])['comment_id']

        # 1) Opus RH1: one party (olive-op before approving) posts large diff-only observations (~140 KB each).
        #    Capped at 512 KiB (COMMIT_OBSERVATIONS_PER_PARTY_MAX_BYTES) on record #4; independent FAIL lands;
        #    olive-op's N/A in block mode is refused 422!
        big_paths = ['pkg/mod_%04d_%s.py' % (i, 'x' * 22) for i in range(4096)]
        accepted_single = 0
        for idx in range(6):
            try:
                self.admin_std(
                    'record-commit', '--commit', commit1, actor='olive-op',
                    data={'changed_files': big_paths + ['run_%d.py' % idx]},
                )
                accepted_single += 1
            except rs.StandardsRefusal as exc:
                self.assertEqual(409, exc.status)
                self.assertIn(
                    rs.refusal_commit_observation_party_bytes_cap(
                        commit1, rs.COMMIT_OBSERVATIONS_PER_PARTY_MAX_BYTES
                    ),
                    str(exc),
                )
                break
        self.assertEqual(3, accepted_single)

        # Independent verifier records FAIL -> succeeds cleanly!
        self.admin_std('record-commit', '--commit', commit1, actor='ci-verifier', data={
            'changed_files': ['app.py'],
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}],
        })
        na1 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r4-m1b-na1',
            'task': tid1, 'previous': cid1, 'contribution': cid1, 'summary': 'Try N/A after flood.',
            'checks': [{'rule_id': 'STD-50', 'result': 'not_applicable', 'reason': 'No tests.'}],
        }, actor='olive-op')
        self.assertEqual(2, na1['returncode'])
        self.assertIn(rs.refusal_na_over_mechanical_fail('STD-50', commit1), na1['stderr'])

        # 2) Multi-party 4 MiB ordinary byte flood (kills X-M3 & Q1e/Q1f) + compact fail-only reserve!
        _git(repo, 'commit', '--allow-empty', '-m', 'second commit for 4 MiB flood')
        tid2 = self.trackers[self.pid].create_task('R4 m1b 4 MiB flood')
        ans2, commit2, _ = self.contribute_bundle(tid2, repo, op_id='op-r4-m1b-c2')
        cid2 = json.loads(ans2['stdout'])['comment_id']

        chunk_paths = ['pkg/heavy_%04d_%s.py' % (i, 'z' * 20) for i in range(4096)]
        hit_doc_cap = False
        for p_idx in range(9):
            actor_name = 'ver-b%d' % p_idx
            for sub in range(3):
                try:
                    self.admin_std(
                        'record-commit', '--commit', commit2, actor=actor_name,
                        data={'changed_files': chunk_paths + ['p%d_%d.py' % (p_idx, sub)]},
                    )
                except rs.StandardsRefusal as exc:
                    if rs.refusal_commit_observation_size_cap(commit2, rs.MAX_COMMIT_OBSERVATION_BYTES) in str(exc):
                        self.assertEqual(409, exc.status)
                        hit_doc_cap = True
                        break
                    raise
            if hit_doc_cap:
                break
        self.assertTrue(hit_doc_cap, 'Expected 4 MiB ordinary byte cap to refuse')
        # Top off within ~60 bytes of MAX_COMMIT_OBSERVATION_BYTES (like Opus's 4,194,255-byte doc)
        # so even a 250-byte ordinary pass entry exceeds 4 MiB!
        # Top off in ~18 KB steps so < 18 KB of ordinary room remains before MAX_COMMIT_OBSERVATION_BYTES!
        step_ci = [
            {'tool': 'pad', 'command': 'pad-%d' % i, 'exit_code': 0, 'summary': 'p' * 350}
            for i in range(18)
        ]
        for step_idx in range(25):
            try:
                self.admin_std('record-commit', '--commit', commit2, actor='ver-b8', data={
                    'ci_checks': [dict(it, run_id='s%d' % step_idx) for it in step_ci],
                })
            except rs.StandardsRefusal:
                break

        # Ordinary pass entry OR fail+file-list entry from a fresh party ver-b9 is refused 409 (ordinary cap full)!
        with self.assertRaises(rs.StandardsRefusal) as cm_pass_full:
            self.admin_std('record-commit', '--commit', commit2, actor='ver-b9', data={
                'ci_checks': [dict(it, run_id='v9') for it in step_ci],
            })
        self.assertEqual(409, cm_pass_full.exception.status)
        self.assertIn(
            rs.refusal_commit_observation_size_cap(commit2, rs.MAX_COMMIT_OBSERVATION_BYTES),
            str(cm_pass_full.exception),
        )
        # Ordinary pass+tree_files entry OR >16 KiB fail entry from ver-b9 is refused 409 (ordinary cap full)!
        with self.assertRaises(rs.StandardsRefusal) as cm_pass_files:
            self.admin_std('record-commit', '--commit', commit2, actor='ver-b9', data={
                'tree_files': {'app.py': {'exists': True, 'regular_text': True, 'line_count': 5}},
                'ci_checks': [dict(it, run_id='v9p') for it in step_ci],
            })
        self.assertEqual(409, cm_pass_files.exception.status)
        self.assertIn(
            rs.refusal_commit_observation_size_cap(commit2, rs.MAX_COMMIT_OBSERVATION_BYTES),
            str(cm_pass_files.exception),
        )
        with self.assertRaises(rs.StandardsRefusal) as cm_huge_fail:
            self.admin_std('record-commit', '--commit', commit2, actor='ver-b9', data={
                'ci_checks': [
                    {'tool': 'pytest', 'command': 'pytest -q %d' % i, 'exit_code': 1, 'summary': 'x' * 380}
                    for i in range(45)
                ],
            })
        self.assertEqual(409, cm_huge_fail.exception.status)
        self.assertIn(str(rs.COMMIT_FAIL_ONLY_ENTRY_MAX_BYTES), str(cm_huge_fail.exception))

        # r5 s1c-fail-with-lists: compact failing CI accompanied by tree_files/changed_files stores
        # the failing items in the reserve and reports dropped=['tree_files']!
        wf_res = self.admin_std('record-commit', '--commit', commit2, actor='ver-b9', data={
            'tree_files': {'app.py': {'exists': True, 'regular_text': True, 'line_count': 5}},
            'ci_checks': [dict(it, exit_code=1, run_id='wf') for it in step_ci[:8]],
        })
        self.assertTrue(wf_res.get('stored_in_fail_reserve'))
        self.assertEqual(['tree_files'], wf_res.get('dropped'))
        self.assertEqual({'failing_ci_checks': 8, 'rule_ids': []}, wf_res.get('stored'))
        self.assertIsNone(wf_res['observations'][-1]['tree_files'])

        # PM RULE: compact fail-only entries (failing CI items, no file lists) succeed OUTSIDE the byte cap
        # both for ver-b0 (whose own party byte budget is full) and for ver-b9 (up to 8 per party total)!
        self.admin_std('record-commit', '--commit', commit2, actor='ver-b0', data={
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}],
        })
        for r_idx in range(rs.COMMIT_FAIL_ALLOWANCE_PER_PARTY_MAX - 1):
            self.admin_std('record-commit', '--commit', commit2, actor='ver-b9', data={
                'ci_checks': [{
                    'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q',
                    'exit_code': 1, 'run_id': 'fail-reserve-%d' % r_idx, 'summary': 'f' * 350,
                } for _ in range(8)],
            })
        # 9th compact fail-only entry from ver-b9 on the full commit hits the per-party fail reserve cap!
        with self.assertRaises(rs.StandardsRefusal) as cm_reserve_cap:
            self.admin_std('record-commit', '--commit', commit2, actor='ver-b9', data={
                'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}],
            })
        self.assertEqual(409, cm_reserve_cap.exception.status)
        self.assertIn(
            rs.refusal_commit_fail_allowance_cap(commit2, rs.COMMIT_FAIL_ALLOWANCE_PER_PARTY_MAX),
            str(cm_reserve_cap.exception),
        )

        # On-disk file is > 4 MiB and HostStandardsStore.read_commit_observation reads it cleanly (kills Q1f)!
        commit2_file = self.project_path / '.standards' / 'commits' / ('%s.json' % commit2)
        self.assertGreater(commit2_file.stat().st_size, rs.MAX_COMMIT_OBSERVATION_BYTES)
        read_back = self.store().read_commit_observation(commit2)
        self.assertEqual(1, read_back['ci_checks'][-1]['exit_code'])

        # Approver N/A in block mode is refused 422 because the compact fail landed!
        na2 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r4-m1b-na2',
            'task': tid2, 'previous': cid2, 'contribution': cid2, 'summary': 'Try N/A over reserve fail.',
            'checks': [{'rule_id': 'STD-50', 'result': 'not_applicable', 'reason': 'No tests.'}],
        }, actor='olive-op')
        self.assertEqual(2, na2['returncode'])
        self.assertIn(rs.refusal_na_over_mechanical_fail('STD-50', commit2), na2['stderr'])

        # 3) Fable NOTE-6: 8 parties fill all 256 ordinary slots; 9th party's ordinary pass is refused 409,
        #    while its compact fail-only entry STILL lands and blocks N/A approval!
        _git(repo, 'commit', '--allow-empty', '-m', 'third commit for 256-slot fail reserve')
        tid3 = self.trackers[self.pid].create_task('R4 m1b 256-slot flood')
        ans3, commit3, _ = self.contribute_bundle(tid3, repo, op_id='op-r4-m1b-c3')
        cid3 = json.loads(ans3['stdout'])['comment_id']
        for p_idx in range(8):
            for _ in range(rs.COMMIT_OBSERVATIONS_PER_RECORDER_MAX):
                self.admin_std('record-commit', '--commit', commit3, actor='ver-b%d' % p_idx, data={
                    'changed_files': ['app.py'],
                })
        with self.assertRaises(rs.StandardsRefusal) as cm_slot_cap:
            self.admin_std('record-commit', '--commit', commit3, actor='ver-b8', data={
                'changed_files': ['app.py'],
            })
        self.assertEqual(409, cm_slot_cap.exception.status)
        self.assertIn(
            rs.refusal_commit_observation_cap(commit3, rs.COMMIT_OBSERVATIONS_MAX),
            str(cm_slot_cap.exception),
        )
        # Compact fail-only entry from ver-b8 succeeds despite 256 ordinary slots being full!
        self.admin_std('record-commit', '--commit', commit3, actor='ver-b8', data={
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 2}],
        })
        na3 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r4-m1b-na3',
            'task': tid3, 'previous': cid3, 'contribution': cid3, 'summary': 'Try N/A after 256 slots.',
            'checks': [{'rule_id': 'STD-50', 'result': 'not_applicable', 'reason': 'No tests.'}],
        }, actor='olive-op')
        self.assertEqual(2, na3['returncode'])
        self.assertIn(rs.refusal_na_over_mechanical_fail('STD-50', commit3), na3['stderr'])

    def test_r4_s1b_nonregular_committed_signoff_503_and_never_overwrite_guard(self):
        # s1b-signoff-store + Opus NOTE-2 Q3b (host) & Q3c (in-process) never-overwrite guards!
        self.admin_std('put', data={
            'expected_version': 0, 'reason': 'Init r4 s1b',
            'rules': [_sample_rule('STD-1', owner='person:cora', severity='must')],
        })
        repo = Path(self.tmp.name) / 'repo_r4_s1b'
        self.init_git_repo(repo, {'f.py': 'a = 1\n'})

        # 1) Directory sitting at committed sign-off filename -> rc=124 (503), staged copy KEPT,
        #    and retry after removing directory promotes staged to committed!
        tid1 = self.trackers[self.pid].create_task('R4 s1b dir collision')
        ans1, commit1, _ = self.contribute_bundle(tid1, repo, op_id='op-r4-s1b-c1')
        cid1 = json.loads(ans1['stdout'])['comment_id']

        so_dir = self.project_path / '.standards' / 'signoffs'
        so_dir.mkdir(parents=True, exist_ok=True)
        op_dir = 'op-r4-s1b-dir'
        committed_dir_path = so_dir / rs._signoff_committed_filename(cid1, op_dir)
        staged_dir_path = so_dir / rs._signoff_staged_filename(cid1, op_dir)
        committed_dir_path.mkdir()

        app_payload = {
            'schema_version': 1, 'operation': 'approve', 'operation_id': op_dir,
            'task': tid1, 'previous': cid1, 'contribution': cid1, 'summary': 'Approve blocked by dir.',
            'checks': [{'rule_id': 'STD-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['f.py:1']}}],
        }
        res_blocked = self.review_op(app_payload, actor='cora-op')
        self.assertEqual(124, res_blocked['returncode'], res_blocked)
        self.assertTrue(committed_dir_path.is_dir())
        self.assertTrue(staged_dir_path.is_file(), 'Staged sign-off must be preserved on commit_signoff failure')

        # Staged sign-off remains readable while committed_name is a directory!
        so_while_blocked = json.loads(self.endpoint_call('standards', ['sign-off', tid1])['stdout'])
        self.assertEqual('staged', so_while_blocked['status'])
        self.assertEqual(op_dir, so_while_blocked['operation_id'])

        # Remove the rogue directory and clear the unknown journal reservation -> retry promotes staged to committed!
        committed_dir_path.rmdir()
        for jf in self.project_path.glob('.http-operations*'):
            jf.unlink()
        res_recovered = self.review_op(app_payload, actor='cora-op')
        self.assertEqual(0, res_recovered['returncode'], res_recovered['stderr'])
        rec_body = json.loads(res_recovered['stdout'])
        self.assertTrue(rec_body['reconciled'])
        self.assertEqual('committed', rec_body['sign_off']['status'])
        self.assertTrue(committed_dir_path.is_file())
        self.assertFalse(staged_dir_path.exists())

        # 2) Symlink sitting at committed sign-off filename -> rc=124, symlink target untouched, staged kept!
        tid2 = self.trackers[self.pid].create_task('R4 s1b symlink collision')
        ans2, _, _ = self.contribute_bundle(tid2, repo, op_id='op-r4-s1b-c2')
        cid2 = json.loads(ans2['stdout'])['comment_id']
        op_sym = 'op-r4-s1b-sym'
        decoy = Path(self.tmp.name) / 'decoy_target.json'
        decoy.write_text('{"untouched": true}\n', encoding='utf-8')
        committed_sym_path = so_dir / rs._signoff_committed_filename(cid2, op_sym)
        staged_sym_path = so_dir / rs._signoff_staged_filename(cid2, op_sym)
        os.symlink(str(decoy), str(committed_sym_path))

        sym_payload = {
            'schema_version': 1, 'operation': 'approve', 'operation_id': op_sym,
            'task': tid2, 'previous': cid2, 'contribution': cid2, 'summary': 'Approve blocked by symlink.',
            'checks': [{'rule_id': 'STD-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['f.py:1']}}],
        }
        res_sym = self.review_op(sym_payload, actor='cora-op')
        self.assertEqual(124, res_sym['returncode'], res_sym)
        self.assertEqual('{"untouched": true}\n', decoy.read_text(encoding='utf-8'))
        self.assertTrue(staged_sym_path.is_file())
        os.unlink(str(committed_sym_path))
        for jf in self.project_path.glob('.http-operations*'):
            jf.unlink()
        res_sym_ok = self.review_op(sym_payload, actor='cora-op')
        self.assertEqual(0, res_sym_ok['returncode'], res_sym_ok['stderr'])
        self.assertTrue(committed_sym_path.is_file())
        self.assertFalse(staged_sym_path.exists())

        # 3) Pin 'never overwrite' guard in HostStandardsStore.commit_signoff (Q3b) and DictStandardsStore.commit_signoff (Q3c)!
        h_store = self.store()
        d_store = rs.DictStandardsStore({}, self.pid)
        orig_obj = {
            'schema_version': 1, 'status': 'committed', 'contribution_id': 'c-immut',
            'commit': commit1, 'operation_id': 'op-immut',
            'created_at': '2026-11-12T01:00:00Z', 'approval_comment_id': 'cmt-original',
            'reviewer': {'actor': 'cora-op'}, 'checks': [],
        }
        overwrite_attempt = dict(
            orig_obj,
            created_at='2099-12-31T23:59:59Z',
            approval_comment_id='cmt-OVERWRITTEN',
            reviewer={'actor': 'attacker'},
        )
        for st in (h_store, d_store):
            st.stage_signoff('c-immut', 'op-immut', dict(orig_obj, status='staged'))
            st.commit_signoff('c-immut', 'op-immut', orig_obj)
            st.stage_signoff('c-immut', 'op-immut', dict(overwrite_attempt, status='staged'))
            st.commit_signoff('c-immut', 'op-immut', overwrite_attempt)
            stored = st.read_signoff('c-immut', approve_operation_id='op-immut')
            self.assertEqual('2026-11-12T01:00:00Z', stored['created_at'])
            self.assertEqual('cmt-original', stored['approval_comment_id'])
            self.assertEqual('cora-op', stored['reviewer']['actor'])

    def test_r4_s2b_signoff_filename_codec_roundtrips_and_24h_reconcile_edge_lengths(self):
        # s2b-reconcile-names: single producer+parser codec round-trips at 151, 152..161, >200, and literal 'sha256-'!
        uuid_cid = '01a1cfb5-7594-750b-b717-a0fd0aa170f8'
        op_151 = 'a' * 151
        staged_151 = rs._signoff_staged_filename(uuid_cid, op_151)
        self.assertEqual(200, len(staged_151))
        self.assertNotIn('_sha256-', staged_151)
        self.assertEqual(('ok', uuid_cid, op_151), rs._signoff_filename(stage='staged', parse_name=staged_151))

        for length in range(152, 162):
            op_edge = 'b' * length
            staged_edge = rs._signoff_staged_filename(uuid_cid, op_edge)
            committed_edge = rs._signoff_committed_filename(uuid_cid, op_edge)
            self.assertIn('._sha256-', staged_edge)
            self.assertIn('._sha256-', committed_edge)
            self.assertLessEqual(len(staged_edge), 200)
            self.assertLessEqual(len(committed_edge), 200)
            self.assertIsNotNone(rs._SAFE_BACKUP_FILENAME_RE.fullmatch(staged_edge))
            self.assertIsNotNone(rs._SAFE_BACKUP_FILENAME_RE.fullmatch(committed_edge))
            self.assertEqual(
                ('ok', uuid_cid, op_edge),
                rs._signoff_filename(
                    stage='staged',
                    parse_name=staged_edge,
                    staged_payload={'contribution_id': uuid_cid, 'operation_id': op_edge},
                ),
            )

        long_cid = 'c' * 64
        long_op = 'd' * 161
        staged_long = rs._signoff_staged_filename(long_cid, long_op)
        self.assertLessEqual(len(staged_long), 200)
        self.assertEqual(
            ('ok', long_cid, long_op),
            rs._signoff_filename(
                stage='staged',
                parse_name=staged_long,
                staged_payload={'contribution_id': long_cid, 'operation_id': long_op},
            ),
        )

        # Literal op_id starting with 'sha256-' never collides with '._sha256-'!
        literal_op = 'sha256-' + ('e' * 24)
        staged_lit = rs._signoff_staged_filename(uuid_cid, literal_op)
        self.assertNotIn('._sha256-', staged_lit)
        self.assertEqual(('ok', uuid_cid, literal_op), rs._signoff_filename(stage='staged', parse_name=staged_lit))

        # End-to-end 25h reconcile test with real 36-char UUID comment IDs and 152-char & 161-char op_ids!
        self.trackers[self.pid].uuid_comments = True
        self.admin_std('put', data={
            'expected_version': 0, 'reason': 'Init r4 s2b',
            'rules': [_sample_rule('STD-1', owner='person:cora', severity='must')],
        })
        repo = Path(self.tmp.name) / 'repo_r4_s2b'
        self.init_git_repo(repo, {'m.py': 'v = 1\n'})

        op_152 = 'r' * 152
        op_161 = 's' * 161
        crashed = []
        for op_long in (op_152, op_161):
            tid = self.trackers[self.pid].create_task('Reconcile %d' % len(op_long))
            ans, commit_sha, _ = self.contribute_bundle(tid, repo, op_id='contrib-%d' % len(op_long))
            cid = json.loads(ans['stdout'])['comment_id']
            self.assertEqual(36, len(cid))
            with mock.patch.object(rs.HostStandardsStore, 'commit_signoff', side_effect=rs.SignoffCommitError('crash')):
                res = self.review_op({
                    'schema_version': 1, 'operation': 'approve', 'operation_id': op_long,
                    'task': tid, 'previous': cid, 'contribution': cid, 'summary': 'Long op approve.',
                    'checks': [{'rule_id': 'STD-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['m.py:1']}}],
                }, actor='cora-op')
            self.assertEqual(124, res['returncode'])
            crashed.append((tid, cid, commit_sha, op_long))

        # Also stage an unreferenced 160-char hashed orphan!
        orphan_op = 'z' * 160
        self.store().stage_signoff(uuid_cid, orphan_op, {
            'schema_version': 1, 'status': 'staged', 'contribution_id': uuid_cid,
            'commit': crashed[0][2], 'operation_id': orphan_op, 'checks': [],
        })

        so_dir = self.project_path / '.standards' / 'signoffs'
        past_mtime = time.time() - (25 * 3600)
        for p in so_dir.glob('*.staged.*'):
            os.utime(str(p), (past_mtime, past_mtime))

        # Trigger reconcile_or_clean_staged via a real review write on the project after 25 hours!
        tid_trigger = self.trackers[self.pid].create_task('Trigger reconcile')
        self.contribute_bundle(tid_trigger, repo, op_id='contrib-trigger-reconcile')

        # Both landed 152-char and 161-char approvals MUST be promoted to committed (NEVER deleted!),
        # while the unreferenced 160-char orphan IS deleted!
        for tid, cid, _, op_long in crashed:
            staged_p = so_dir / rs._signoff_staged_filename(cid, op_long)
            committed_p = so_dir / rs._signoff_committed_filename(cid, op_long)
            self.assertFalse(staged_p.exists(), 'Landed staged file must be promoted & unlinked')
            self.assertTrue(committed_p.is_file(), 'Landed >200-char approval must be promoted, not deleted!')
            so_out = json.loads(self.endpoint_call('standards', ['sign-off', tid])['stdout'])
            self.assertEqual('committed', so_out['status'])
            self.assertEqual(op_long, so_out['operation_id'])

        self.assertFalse((so_dir / rs._signoff_staged_filename(uuid_cid, orphan_op)).exists())

    def test_r4_n1b_newest_wins_ci_ordering_tree_exclusion_and_extra_committed_pins(self):
        # Fable NOTE-4 (X-M2 newest-wins CI ordering), Opus Q2d (tree_files exclusion),
        # Opus Q3e (extra_committed sign-offs), and Opus NOTE-1 (bounded citation path scan)!
        self.verifiers.append('ver-ci-2')
        self._write_deployment()

        self.admin_std('put', data={
            'expected_version': 0, 'reason': 'Init r4 n1b',
            'rules': [
                _sample_rule('STD-50', owner='person:cora', severity='must',
                      check={'kind': 'mechanical', 'tool': 'pytest', 'command': 'pytest -q'}),
                _sample_rule('STD-10', owner='person:cora', severity='must', check={'kind': 'judgement'}),
            ],
        })
        self.admin_std('set-gate', '--mode', 'block', '--reason', 'Block')

        repo = Path(self.tmp.name) / 'repo_r4_n1b'
        self.init_git_repo(repo, {'app.py': 'line1 = 1\nline2 = 2\n'})

        # 1) X-M2a: independent verifier records PASS first, then independent verifier records FAIL second -> FAIL wins!
        tid_pf = self.trackers[self.pid].create_task('CI pass then fail')
        ans_pf, commit_pf, _ = self.contribute_bundle(tid_pf, repo, op_id='op-r4-pf')
        cid_pf = json.loads(ans_pf['stdout'])['comment_id']
        self.admin_std('record-commit', '--commit', commit_pf, actor='ci-verifier', data={
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}],
        })
        self.admin_std('record-commit', '--commit', commit_pf, actor='ver-ci-2', data={
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}],
        })
        res_pf = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r4-pf-app',
            'task': tid_pf, 'previous': cid_pf, 'contribution': cid_pf, 'summary': 'Approve after pass->fail.',
            'checks': [{'rule_id': 'STD-10', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['app.py:1']}}],
        }, actor='olive-op')
        self.assertEqual(2, res_pf['returncode'])
        self.assertIn('fail without waiver', res_pf['stderr'])

        # 2) X-M2b: independent verifier records FAIL first, then independent verifier re-runs and records PASS second -> PASS wins!
        _git(repo, 'commit', '--allow-empty', '-m', 'fail then pass commit')
        tid_fp = self.trackers[self.pid].create_task('CI fail then pass')
        ans_fp, commit_fp, _ = self.contribute_bundle(tid_fp, repo, op_id='op-r4-fp')
        cid_fp = json.loads(ans_fp['stdout'])['comment_id']
        self.admin_std('record-commit', '--commit', commit_fp, actor='ci-verifier', data={
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}],
        })
        self.admin_std('record-commit', '--commit', commit_fp, actor='ver-ci-2', data={
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}],
        })
        res_fp = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r4-fp-app',
            'task': tid_fp, 'previous': cid_fp, 'contribution': cid_fp, 'summary': 'Approve after fail->pass.',
            'checks': [{'rule_id': 'STD-10', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['app.py:1']}}],
        }, actor='olive-op')
        self.assertEqual(0, res_fp['returncode'], res_fp['stderr'])
        fp_so = json.loads(res_fp['stdout'])['sign_off']
        std50_row = [c for c in fp_so['checks'] if c['rule_id'] == 'STD-50'][0]
        self.assertEqual('pass', std50_row['result'])
        self.assertEqual(0, std50_row['ci_evidence']['exit_code'])

        # Direct pin on _resolve_mechanical_rule_from_ci newest-first scan (kills Fable X-M2 on both loops):
        mech_rule = {'id': 'STD-50', 'check': {'kind': 'mechanical', 'tool': 'pytest', 'command': 'pytest -q'}}
        direct_by_rule = rs._resolve_mechanical_rule_from_ci(mech_rule, commit_fp, {'ci_checks': [
            {'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0, 'status': 'pass'},
            {'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1, 'status': 'fail'},
        ]})
        self.assertEqual('fail', direct_by_rule['result'])
        direct_by_cmd = rs._resolve_mechanical_rule_from_ci(mech_rule, commit_fp, {'ci_checks': [
            {'rule_id': None, 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0, 'status': 'pass'},
            {'rule_id': None, 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1, 'status': 'fail'},
        ]})
        self.assertEqual('fail', direct_by_cmd['result'])

        # 3) Opus Q2d: excluded participant party's tree_files is ignored when validating file:line citations!
        _git(repo, 'commit', '--allow-empty', '-m', 'remote tree exclusion commit')
        tid_tree = self.trackers[self.pid].create_task('Remote tree exclusion')
        contrib_remote, remote_commit, remote_bpath = self.contribute_bundle(tid_tree, repo, op_id='op-r4-remote-c')
        self.assertEqual(0, contrib_remote['returncode'], contrib_remote['stderr'])
        cid_tree = json.loads(contrib_remote['stdout'])['comment_id']
        # Remove bundle file so local git tree is unavailable; only commit_obs['tree_files'] can satisfy citations!
        remote_bpath.unlink()

        # olive-op records tree_files BEFORE becoming the approver on remote_commit:
        self.admin_std('record-commit', '--commit', remote_commit, actor='olive-op', data={
            'tree_files': {'remote_only.py': {'exists': True, 'regular_text': True, 'line_count': 20}},
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}],
        })
        # Independent verifier records PASS for STD-50, NOT tree_files:
        self.admin_std('record-commit', '--commit', remote_commit, actor='ci-verifier', data={
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}],
        })

        # olive-op tries to approve citing remote_only.py:5 -> refused because olive-op's own tree_files is excluded!
        res_self_tree = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r4-self-tree',
            'task': tid_tree, 'previous': cid_tree, 'contribution': cid_tree, 'summary': 'Self tree cite.',
            'checks': [{'rule_id': 'STD-10', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['remote_only.py:5']}}],
        }, actor='olive-op')
        self.assertEqual(2, res_self_tree['returncode'])
        self.assertIn('delivered commit tree is not available', res_self_tree['stderr'])

        # Once independent ci-verifier records tree_files, olive-op's citation succeeds!
        self.admin_std('record-commit', '--commit', remote_commit, actor='ci-verifier', data={
            'tree_files': {'remote_only.py': {'exists': True, 'regular_text': True, 'line_count': 20}},
        })
        res_indep_tree = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-r4-indep-tree',
            'task': tid_tree, 'previous': cid_tree, 'contribution': cid_tree, 'summary': 'Indep tree cite.',
            'checks': [{'rule_id': 'STD-10', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['remote_only.py:5']}}],
        }, actor='olive-op')
        self.assertEqual(0, res_indep_tree['returncode'], res_indep_tree['stderr'])

        # 4) Opus Q3e: read_signoff_for_contribution returns extra_committed sign-offs when issue=None!
        for st in (self.store(), rs.DictStandardsStore({}, self.pid)):
            st.commit_signoff('cid-q3e', 'op-q3e-1', {
                'schema_version': 1, 'status': 'committed', 'contribution_id': 'cid-q3e',
                'commit': commit_fp, 'operation_id': 'op-q3e-1',
                'created_at': '2026-11-12T01:00:00Z', 'approval_comment_id': 'c1',
                'reviewer': {'actor': 'cora-op'}, 'checks': [],
            })
            st.commit_signoff('cid-q3e', 'op-q3e-2', {
                'schema_version': 1, 'status': 'committed', 'contribution_id': 'cid-q3e',
                'commit': commit_fp, 'operation_id': 'op-q3e-2',
                'created_at': '2026-11-12T02:00:00Z', 'approval_comment_id': 'c2',
                'reviewer': {'actor': 'olive-op'}, 'checks': [],
            })
            so_q3e = rs.read_signoff_for_contribution(st, 'cid-q3e', expected_commit=commit_fp, issue=None)
            self.assertEqual('op-q3e-2', so_q3e['operation_id'])
            self.assertEqual(1, len(so_q3e['earlier_sign_offs']))
            self.assertEqual('op-q3e-1', so_q3e['earlier_sign_offs'][0]['operation_id'])

        # 5) Opus NOTE-1: collect_citation_paths_from_checks is O(n) and bounded by CHECKS_MAX * CITATIONS_MAX!
        many_checks = [
            {'evidence': {'citations': ['pkg/f_%d_%d.py:1' % (i, j) for j in range(60)]}}
            for i in range(300)
        ]
        collected = rs.collect_citation_paths_from_checks(many_checks)
        self.assertEqual(rs.CHECKS_MAX * rs.CITATIONS_MAX, len(collected))

    def test_r5_host_dotted_ids_crash_window_fail_with_lists_prelock_bound_and_nonregular_signoffs(self):
        """Pin r5 m1c-dotted-ids, s1c-fail-with-lists, and s2c-prelock-bound on the host stack."""
        self.admin_std('put', data={
            'expected_version': 0,
            'reason': 'Create r5 host register.',
            'rules': [
                _sample_rule('JUDG-1', owner='person:alice', severity='must'),
                _sample_rule(
                    'STD-50',
                    owner='person:alice',
                    severity='must',
                    check={'kind': 'mechanical', 'tool': 'pytest', 'command': 'pytest -q'},
                ),
            ],
        })
        self.admin_std('set-gate', '--mode', 'block', '--reason', 'Enforce block.')

        # 1) m1c-dotted-ids: approvals whose operation_id / contribution_id contain '.staged.',
        #    '.committed.', and trailing dots survive 25h reconcile_or_clean_staged and count in N/A tally!
        tracker = self.trackers[self.pid]
        tid1 = tracker.create_task('Dotted IDs task', assignee='worker-bob')
        repo1 = Path(self.tmp.name) / 'repo-r5-d1'
        self.init_git_repo(repo1, {'app.py': 'x = 1\ny = 2\nz = 3\n'})
        _, commit1, _ = self.contribute_bundle(tid1, repo1, op_id='op.staged.contrib1.')
        cid1 = self.chain_info(tid1)['contribution']['comment_id']
        self.admin_std('record-commit', '--commit', commit1, actor='ci-verifier', data={
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}],
        })

        dotted_op1 = 'op.staged.alpha.committed.beta.'
        app1 = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': dotted_op1,
            'task': tid1, 'previous': cid1, 'contribution': cid1,
            'summary': 'Approve with .staged. and .committed. and trailing dot.',
            'checks': [{'rule_id': 'JUDG-1', 'result': 'not_applicable', 'reason': 'No slice ops.'}],
        }, actor='cora-op')
        self.assertEqual(0, app1['returncode'], app1['stderr'])

        # Second task whose contribution_id ITSELF contains '.staged.' and crashes between bd write and commit_signoff!
        tid2 = tracker.create_task('Dotted contribution_id crash task', assignee='worker-bob')
        repo2 = Path(self.tmp.name) / 'repo-r5-d2'
        self.init_git_repo(repo2, {'app.py': 'a = 10\nb = 20\n'})
        bpath2 = self.delivery_root / 'r5-d2.bundle'
        commit2, sha2 = _make_bundle(repo2, bpath2)
        dotted_cid2 = '%s.staged.c1.' % tid2
        tracker.rows[tid2]['comments'].append({
            'id': dotted_cid2,
            'author': 'worker-bob',
            'created_at': '2026-11-12T01:00:00Z',
            'text': rw.PREFIX + json.dumps({
                'schema_version': 1,
                'operation': 'contribute',
                'operation_id': 'op-d2-c1',
                'task': tid2,
                'previous': None,
                'supersedes': None,
                'repository': 'https://example.invalid/repo.git',
                'commit': commit2,
                'base_commit': commit2,
                'delivery': {'kind': 'bundle', 'path': _bundle_delivery_path(bpath2), 'sha256': sha2},
                'summary': 'Delivered with dotted comment id.',
            }),
        })
        self.admin_std('record-commit', '--commit', commit2, actor='ci-verifier', data={
            'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 0}],
        })

        dotted_op2 = 'op.committed.crash.staged.two.'
        orig_commit_so = rs.HostStandardsStore.commit_signoff
        with mock.patch.object(
            rs.HostStandardsStore,
            'commit_signoff',
            side_effect=rs.SignoffCommitError('simulated disk full after bd approve'),
        ):
            crash_ans = self.review_op({
                'schema_version': 1, 'operation': 'approve', 'operation_id': dotted_op2,
                'task': tid2, 'previous': dotted_cid2, 'contribution': dotted_cid2,
                'summary': 'Approve dotted cid before simulated crash.',
                'checks': [{'rule_id': 'JUDG-1', 'result': 'not_applicable', 'reason': 'Config only.'}],
            }, actor='cora-op')
        self.assertEqual(124, crash_ans['returncode'])
        self.assertIn('SignoffCommitError', crash_ans['stderr'])
        assert orig_commit_so is not None

        # Advance 25 hours: reconcile_or_clean_staged promotes dotted_op2 AND keeps dotted_op1 committed!
        store = self.store()
        rows_now = list(tracker.rows.values())
        store.reconcile_or_clean_staged(rows_now, now_epoch=time.time() + 90000)
        store.reconcile_or_clean_staged(rows_now, now_epoch=time.time() + 180000)

        so1 = self.admin_std('sign-off', '--task', tid1)
        so2 = self.admin_std('sign-off', '--task', tid2)
        self.assertEqual(dotted_op1, so1['operation_id'])
        self.assertEqual(dotted_op2, so2['operation_id'])
        self.assertEqual(2, so1['reviewer_must_not_applicable_total'])
        self.assertEqual(2, so2['reviewer_must_not_applicable_total'])

        # 2) s2c-prelock-bound (b): non-regular entry in signoffs/ does NOT wipe out list_committed_signoffs
        #    or N/A tallies, and surfaces a clear warning in lint and sign-off!
        rogue_dir = self.project_path / '.standards' / 'signoffs' / 'rogue-subdir'
        rogue_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.assertEqual(2, len(store.list_committed_signoffs()))
            so1_warn = self.admin_std('sign-off', '--task', tid1)
            self.assertEqual(2, so1_warn['reviewer_must_not_applicable_total'])
            self.assertTrue(
                any('signoffs/rogue-subdir' in w for w in (so1_warn.get('store_warnings') or [])),
                so1_warn,
            )
            lint_out = self.admin_std('lint')
            self.assertTrue(
                any('signoffs/rogue-subdir' in w.get('message', '') for w in lint_out.get('warnings', [])),
                lint_out,
            )
        finally:
            rogue_dir.rmdir()

        # 3) s1c-fail-with-lists: documented inspect-commit -> add CI checks -> record-commit flow
        #    when ordinary storage is full stores failing CI items in reserve and reports stored/dropped!
        tid3 = tracker.create_task('Documented inspect-commit reserve flow', assignee='worker-bob')
        repo3 = Path(self.tmp.name) / 'repo-r5-s1c'
        self.init_git_repo(repo3, {'main.py': 'def run():\n    return 1\n'})
        _git(repo3, 'checkout', '-b', 'feat')
        (repo3 / 'added.py').write_text('val = 42\n', encoding='utf-8')
        _git(repo3, 'add', 'added.py')
        _git(repo3, 'commit', '-m', 'add file')
        _, commit3, _ = self.contribute_bundle(tid3, repo3, op_id='op-s1c-deliv')
        cid3 = self.chain_info(tid3)['contribution']['comment_id']

        inspected_doc = rs.inspect_local_repo_commit(repo3, commit3, target_ref='refs/heads/main')
        # Exhaust ci-verifier's 32 ordinary observation slots on commit3 first:
        for idx in range(rs.COMMIT_OBSERVATIONS_PER_RECORDER_MAX):
            self.admin_std('record-commit', '--commit', commit3, actor='ci-verifier', data={
                'ci_checks': [{'tool': 'lint', 'command': 'lint-%d' % idx, 'exit_code': 0}],
            })
        # Now ci-verifier runs the documented single-observation flow with changed_files + tree_files + mixed CI!
        combined_payload = dict(inspected_doc)
        combined_payload['ci_checks'] = [
            {'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1, 'summary': '1 failed'},
            {'tool': 'ruff', 'command': 'ruff check', 'exit_code': 0},
        ]
        res_s1c = self.admin_std('record-commit', '--commit', commit3, actor='ci-verifier', data=combined_payload)
        self.assertTrue(res_s1c.get('stored_in_fail_reserve'))
        self.assertEqual({'failing_ci_checks': 1, 'rule_ids': ['STD-50']}, res_s1c.get('stored'))
        self.assertEqual(['changed_files', 'tree_files', 'passing_ci_checks'], res_s1c.get('dropped'))
        self.assertEqual(
            {'changed_files': 1, 'tree_files': 1, 'passing_ci_checks': 1},
            res_s1c.get('dropped_counts'),
        )
        # Approve trying to mark STD-50 not_applicable is refused 422 because the failing CI item landed!
        bad_na = self.review_op({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-s1c-na',
            'task': tid3, 'previous': cid3, 'contribution': cid3,
            'summary': 'Attempt N/A over reserve fail from inspect-commit flow.',
            'checks': [
                {'rule_id': 'JUDG-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['added.py:1']}},
                {'rule_id': 'STD-50', 'result': 'not_applicable', 'reason': 'Claim not applicable.'},
            ],
        }, actor='cora-op')
        self.assertEqual(2, bad_na['returncode'])
        self.assertIn(rs.refusal_na_over_mechanical_fail('STD-50', commit3), bad_na['stderr'])

        # 4) s2c-prelock-bound (a):
        #    - request-changes without citations & unauthorized SSH approve skip inspect_delivery_git (0 calls)!
        with mock.patch.object(rs, 'inspect_delivery_git', wraps=rs.inspect_delivery_git) as spy_git:
            rc_nocite = self.review_op({
                'schema_version': 1, 'operation': 'request-changes', 'operation_id': 'op-nocite-rc',
                'task': tid1, 'previous': app1 and self.chain_info(tid1)['latest_comment_id'],
                'contribution': cid1, 'summary': 'Plain review item without standards citations.',
                'items': [{'id': 'i-1', 'title': 'Fix x', 'Blocking': True, 'detail': 'Details.'}],
            }, actor='cora-op')
            # Note: 'Blocking' capitalized is invalid schema -> rejected later without pre-lock git;
            # also test valid recommend without citations AND unauthorized approve with coordinator_writes!
            rec_nocite = self.review_op({
                'schema_version': 1, 'operation': 'recommend', 'operation_id': 'op-nocite-rec',
                'task': tid3, 'contribution': cid3, 'commit': commit3, 'verdict': 'approve',
                'summary': 'No citations here.', 'items': [],
            }, actor='reviewer-sam')
            self.assertEqual(0, rec_nocite['returncode'], rec_nocite['stderr'])
            self.assertEqual(0, spy_git.call_count)
            _ = rc_nocite

        #    - Slow git fake is bounded strictly below BUNDLE_LOCK_WAIT_SECONDS and refuses cleanly with 503!
        real_confined = rs.run_confined_git

        def _slow_git(args, **kwargs):
            time.sleep(0.12)
            return real_confined(args, **kwargs)

        t0 = time.monotonic()
        with mock.patch.object(rs, 'BUNDLE_INSPECT_MAX_SECONDS', 0.25), \
             mock.patch.object(rs, 'run_confined_git', side_effect=_slow_git):
            # Re- call inspect_delivery_git through endpoint review with citation:
            slow_ans = self.review_op({
                'schema_version': 1, 'operation': 'recommend', 'operation_id': 'op-slow-rec',
                'task': tid3, 'contribution': cid3, 'commit': commit3, 'verdict': 'approve',
                'summary': 'Check citation under slow git.', 'items': [],
                'checks': [{'rule_id': 'JUDG-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['added.py:1']}}],
            }, actor='olive-op')
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2.0)
        self.assertEqual(2, slow_ans['returncode'])
        self.assertEqual(503, slow_ans.get('standards_status'))
        self.assertIn(rs.refusal_git_inspection_timeout(commit3), slow_ans['stderr'])


class PortableReviewStandardsUnitTests(unittest.TestCase):
    """Cross-platform unit & git-bundle tests (run on both Linux and Windows CI)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        rs._cleanup_temp_git_dir(self.base)
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def test_portable_bundle_delivery_git_inspection_and_readonly_pack_cleanup(self):
        """Pin ci-windows: bundle unbundle, alternates path, batched tree read, and read-only .pack cleanup."""
        repo = self.base / 'repo'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'src').mkdir()
        (repo / 'src' / 'app.py').write_text('alpha = 1\nbeta = 2\ngamma = 3\n', encoding='utf-8')
        (repo / 'README.md').write_text('# Base\n', encoding='utf-8')
        _git(repo, 'add', '.')
        _git(repo, 'commit', '-m', 'base commit')

        serving = self.base / 'serving.git'
        _git(self.base, 'clone', '--bare', str(repo), str(serving))

        (repo / 'src' / 'app.py').write_text('alpha = 1\nbeta = 2\ngamma = 3\ndelta = 4\n', encoding='utf-8')
        (repo / 'bin.dat').write_bytes(b'\x00\x01\x02binary')
        _git(repo, 'add', '.')
        _git(repo, 'commit', '-m', 'feature commit')

        bundles = self.base / 'bundles'
        bpath = bundles / 'feat.bundle'
        commit, sha = _make_bundle(repo, bpath)

        obs = rs.inspect_delivery_git(
            {
                'commit': commit,
                'delivery': {
                    'kind': 'bundle',
                    'path': _bundle_delivery_path(bpath),
                    'sha256': sha,
                },
            },
            delivery_roots=[str(bundles)],
            serving_repo=str(serving),
            target_ref='refs/heads/main',
            needed_paths=['src/app.py', 'bin.dat', 'missing.py', 'src'],
            lock_dir=self.base,
        )
        self.assertTrue(obs['tree_available'], obs)
        self.assertEqual('bundle', obs['tree_source'])
        self.assertEqual('verified', obs['diff_status'])
        self.assertEqual(['bin.dat', 'src/app.py'], obs['changed_files'])
        self.assertEqual(4, obs['tree_files']['src/app.py']['line_count'])
        self.assertFalse(obs['tree_files']['bin.dat']['regular_text'])
        self.assertFalse(obs['tree_files']['missing.py']['exists'])
        self.assertTrue(obs['tree_files']['src']['exists'])
        self.assertFalse(obs['tree_files']['src']['regular_text'])

        cit = rs.validate_citation_against_tree('src/app.py:1-4', 'STD-1', commit, obs['tree_available'], obs['tree_files'])
        self.assertEqual((1, 4), (cit['start_line'], cit['end_line']))

        # Read-only .pack cleanup helper clears read-only bits cleanly on Windows & POSIX:
        ro_dir = self.base / 'ro-pack-dir'
        (ro_dir / 'objects' / 'pack').mkdir(parents=True)
        ro_pack = ro_dir / 'objects' / 'pack' / 'pack-demo.pack'
        ro_pack.write_bytes(b'PACK')
        os.chmod(str(ro_pack), 0o444)
        orig_unlink = os.unlink

        def _win_like_unlink(path, *args, **kwargs):
            dir_fd = kwargs.get('dir_fd')
            st = os.stat(path, dir_fd=dir_fd, follow_symlinks=False) if dir_fd is not None else os.lstat(path)
            if not (st.st_mode & stat.S_IWRITE):
                raise PermissionError('read-only: %s' % path)
            return orig_unlink(path, *args, **kwargs)

        with mock.patch('os.unlink', side_effect=_win_like_unlink):
            rs._cleanup_temp_git_dir(ro_dir)
        self.assertFalse(ro_dir.exists())

    def test_portable_m1c_dotted_and_boundary_signoff_filenames_and_nonregular_tolerance(self):
        """Pin m1c-dotted-ids + non-regular signoffs/ tolerance + O24/O31/O32/O34."""
        proj_dir = self.base / 'proj-m1c'
        proj_dir.mkdir()
        hstore = rs.HostStandardsStore(proj_dir, 'proj-m1c')
        dstore = rs.DictStandardsStore({}, 'proj-m1c')
        commit = 'a' * 40

        tricky_pairs = [
            ('cid-plain', 'op.staged.one'),
            ('cid-plain', 'op.committed.two'),
            ('cid-plain', 'op.staged.mid.committed.end.'),
            ('cid-plain', 'op-trailing-dot.'),
            ('cid-plain', 'op-ends-with.json'),
            ('abc.staged.def', 'op1'),
            ('abc', 'def.staged.op1'),
            ('cid.committed.x.', 'op.staged.y.'),
            ('c' * 95, 'o' * 96),
            ('c' * 161, 'o' * 161),
        ]
        for idx, (cid, op_id) in enumerate(tricky_pairs):
            staged_name = rs._signoff_staged_filename(cid, op_id)
            committed_name = rs._signoff_committed_filename(cid, op_id)
            self.assertNotEqual(staged_name, committed_name)
            self.assertEqual(4, len(staged_name.split('.')), staged_name)
            self.assertEqual(4, len(committed_name.split('.')), committed_name)
            payload = {
                'schema_version': 1,
                'status': 'committed',
                'contribution_id': cid,
                'commit': commit,
                'operation_id': op_id,
                'created_at': '2026-11-12T00:%02d:00Z' % idx,
                'approval_comment_id': 'cmt-%d' % idx,
                'reviewer': {'actor': 'cora-op'},
                'checks': [{'rule_id': 'STD-1', 'severity': 'must', 'result': 'not_applicable'}],
            }
            for st in (hstore, dstore):
                st.stage_signoff(cid, op_id, dict(payload, status='staged'))
                self.assertEqual(op_id, st.read_signoff(cid, approve_operation_id=op_id)['operation_id'])
                st.commit_signoff(cid, op_id, payload)
                self.assertEqual(op_id, st.read_signoff(cid, approve_operation_id=op_id)['operation_id'])

        self.assertEqual(len(tricky_pairs), len(hstore.list_committed_signoffs()))
        self.assertEqual(len(tricky_pairs), len(dstore.list_committed_signoffs()))

        # O24: commit_signoff refuses SignoffCommitError if existing committed file has mismatched (cid, op_id)!
        tampered_cid, tampered_op = tricky_pairs[0]
        tampered_file = proj_dir / '.standards' / 'signoffs' / rs._signoff_committed_filename(tampered_cid, tampered_op)
        orig_bytes = tampered_file.read_bytes()
        tampered_file.write_text(json.dumps({
            'schema_version': 1, 'status': 'committed', 'contribution_id': 'other-cid', 'operation_id': tampered_op,
        }), encoding='utf-8')
        with self.assertRaises(rs.SignoffCommitError):
            hstore.commit_signoff(tampered_cid, tampered_op, {'contribution_id': tampered_cid, 'operation_id': tampered_op})
        tampered_file.write_bytes(orig_bytes)

        # O31/O32/O34: hashed staged file with damaged JSON whose approve landed in rows is KEPT past 24h,
        # while an orphan damaged hashed staged file is kept <24h and cleaned >=24h!
        so_dir = proj_dir / '.standards' / 'signoffs'
        landed_cid, landed_op = 'cid.staged.landed', 'op.staged.landed.'
        landed_staged = so_dir / rs._signoff_staged_filename(landed_cid, landed_op)
        landed_staged.write_text('{corrupt', encoding='utf-8')
        orphan_staged = so_dir / rs._signoff_staged_filename('cid.staged.orphan', 'op.staged.orphan.')
        orphan_staged.write_text('{corrupt', encoding='utf-8')

        fake_rows = [{
            'id': 't-1',
            'issue_type': 'task',
            'comments': [{
                'id': 'app-cmt-1',
                'author': 'cora-op',
                'text': rw.PREFIX + json.dumps({
                    'schema_version': 1, 'operation': 'approve', 'operation_id': landed_op, 'contribution': landed_cid,
                }),
            }],
        }]
        now_ep = time.time()
        hstore.reconcile_or_clean_staged(fake_rows, now_epoch=now_ep + 60)
        self.assertTrue(landed_staged.exists())
        self.assertTrue(orphan_staged.exists())  # <24h grace preserved (kills X-R4-10)

        hstore.reconcile_or_clean_staged(fake_rows, now_epoch=now_ep + 90000)
        self.assertTrue(landed_staged.exists())   # landed approve is NEVER deleted at 25h!
        self.assertFalse(orphan_staged.exists())  # orphan >24h is cleaned!
        landed_staged.unlink()
        # All 10 committed dotted/long sign-offs remain intact after 25h reconcile_or_clean_staged!
        self.assertEqual(len(tricky_pairs), len(hstore.list_committed_signoffs()))

        # Non-regular directory in signoffs/ does NOT hide valid committed sign-offs and reports warning!
        bad_sub = so_dir / 'unexpected-dir'
        bad_sub.mkdir()
        try:
            self.assertEqual(len(tricky_pairs), len(hstore.list_committed_signoffs()))
            self.assertTrue(any('signoffs/unexpected-dir' in w for w in hstore.list_signoff_warnings()))
        finally:
            bad_sub.rmdir()

    def test_portable_s1c_fail_with_lists_reserve_and_total_and_file_caps(self):
        """Pin s1c-fail-with-lists + O5 (9th overflow when room), O8/X-R4-5 (256 reserve cap), O9/X-R4-6 (8 MiB cap)."""
        proj_dir = self.base / 'proj-s1c'
        proj_dir.mkdir()
        store = rs.HostStandardsStore(proj_dir, 'proj-s1c')
        commit_a = 'b' * 40

        # O5: when ordinary room IS available, a party's 9th compact fail overflows cleanly into ordinary slots!
        for i in range(9):
            rs.record_commit_observation(
                store,
                commit_a,
                {'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1, 'run_id': 'r%d' % i}]},
                actor='ver-o5',
                is_authorized_recorder=True,
                same_party_checker=lambda other: other == 'ver-o5',
            )
        obs_a = store.read_commit_observation(commit_a)
        self.assertEqual(9, len(obs_a['observations']))
        self.assertEqual(8, sum(1 for e in obs_a['observations'] if e.get('reserved_fail')))

        # O8 / X-R4-5: 32 parties * 8 compact fails = 256 total reserve cap; when ordinary slots are ALSO full,
        # a 33rd party's compact fail or fail+lists is refused 409 with refusal_commit_fail_allowance_total_cap!
        commit_b = 'c' * 40
        seed_entries = []
        for idx in range(rs.COMMIT_OBSERVATIONS_MAX):
            p = 'ord-%d' % (idx // 32)
            seed_entries.append({
                'changed_files': None, 'tree_files': None,
                'ci_checks': [{'rule_id': None, 'tool': 't', 'command': 'c', 'exit_code': 0, 'status': 'pass'}],
                'recorded_by': p, 'recorded_by_user_id': p, 'recorded_by_principal': p,
                'recorded_at': '2026-11-12T00:00:00Z',
            })
        for r_idx in range(rs.COMMIT_FAIL_ALLOWANCE_TOTAL_MAX):
            rp = 'res-%d' % (r_idx // 8)
            seed_entries.append({
                'changed_files': None, 'tree_files': None,
                'ci_checks': [{'rule_id': 'STD-50', 'tool': 't', 'command': 'c', 'exit_code': 1, 'status': 'fail'}],
                'recorded_by': rp, 'recorded_by_user_id': rp, 'recorded_by_principal': rp,
                'recorded_at': '2026-11-12T00:00:00Z',
                'reserved_fail': True,
            })
        store.write_commit_observation(commit_b, rs._fold_commit_observations(commit_b, seed_entries))
        for extra_payload in (
            {'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}]},
            {
                'changed_files': ['a.py'],
                'tree_files': {'a.py': {'exists': True, 'regular_text': True, 'line_count': 2}},
                'ci_checks': [{'rule_id': 'STD-50', 'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}],
            },
        ):
            with self.assertRaises(rs.StandardsRefusal) as cm_tot:
                rs.record_commit_observation(
                    store,
                    commit_b,
                    extra_payload,
                    actor='ver-33',
                    is_authorized_recorder=True,
                    same_party_checker=lambda other: other == 'ver-33',
                )
            self.assertEqual(409, cm_tot.exception.status)
            self.assertIn(
                rs.refusal_commit_fail_allowance_total_cap(commit_b, rs.COMMIT_FAIL_ALLOWANCE_TOTAL_MAX),
                str(cm_tot.exception),
            )

        # O9 / X-R4-6: MAX_COMMIT_OBSERVATION_FILE_BYTES ceiling on reserve path refuses 409 cleanly!
        commit_c = 'd' * 40
        with mock.patch.object(rs, 'MAX_COMMIT_OBSERVATION_BYTES', 1600), \
             mock.patch.object(rs, 'MAX_COMMIT_OBSERVATION_FILE_BYTES', 2000):
            rs.record_commit_observation(
                store,
                commit_c,
                {'ci_checks': [{'tool': 'pad', 'command': 'pad', 'exit_code': 0, 'summary': 'p' * 300}]},
                actor='ver-c1',
                is_authorized_recorder=True,
                same_party_checker=lambda other: other == 'ver-c1',
            )
            with self.assertRaises(rs.StandardsRefusal) as cm_file_cap:
                rs.record_commit_observation(
                    store,
                    commit_c,
                    {
                        'changed_files': ['x.py'],
                        'ci_checks': [{'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1, 'summary': 'f' * 380}],
                    },
                    actor='ver-c2',
                    is_authorized_recorder=True,
                    same_party_checker=lambda other: other == 'ver-c2',
                )
            self.assertEqual(409, cm_file_cap.exception.status)
            self.assertIn(rs.refusal_commit_observation_size_cap(commit_c, 2000), str(cm_file_cap.exception))

    def test_portable_s2c_batched_tree_paths_and_slow_git_bound(self):
        """Pin batched _inspect_repo_tree_paths (<= 4 git calls for 250 paths) and slow-git 503 timeout."""
        repo = self.base / 'repo-batch'
        repo.mkdir()
        _git(repo, 'init')
        paths = []
        for idx in range(240):
            rel = 'pkg/mod_%03d.py' % idx
            full = repo / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text('line1 = %d\nline2 = %d\n' % (idx, idx + 1), encoding='utf-8')
            paths.append(rel)
        _git(repo, 'add', '.')
        _git(repo, 'commit', '-m', '240 files')
        bpath = self.base / 'bundles' / 'batch.bundle'
        commit, sha = _make_bundle(repo, bpath)

        real_git = rs.run_confined_git
        calls = []

        def _counting_git(args, **kw):
            calls.append(list(args))
            return real_git(args, **kw)

        with mock.patch.object(rs, 'run_confined_git', side_effect=_counting_git):
            tree = rs._inspect_repo_tree_paths(repo, commit, paths + ['pkg/missing_%d.py' % i for i in range(10)])
        self.assertEqual(250, len(tree))
        self.assertEqual(2, tree['pkg/mod_000.py']['line_count'])
        self.assertFalse(tree['pkg/missing_0.py']['exists'])
        # 250 paths in 2 ls-tree chunks + 1 cat-file --batch-check + 1 cat-file --batch = 4 git calls (never 750!)
        self.assertLessEqual(len(calls), 5, calls)

        def _slow_git(args, **kw):
            time.sleep(0.12)
            return real_git(args, **kw)

        t0 = time.monotonic()
        with mock.patch.object(rs, 'run_confined_git', side_effect=_slow_git):
            slow_obs = rs.inspect_delivery_git(
                {'commit': commit, 'delivery': {'kind': 'bundle', 'path': _bundle_delivery_path(bpath), 'sha256': sha}},
                delivery_roots=[str(bpath.parent)],
                needed_paths=['pkg/mod_000.py'],
                lock_dir=self.base,
                lock_wait_seconds=1.5,
                max_inspect_seconds=0.25,
            )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)
        self.assertTrue(slow_obs['inspect_timed_out'])
        self.assertFalse(slow_obs['tree_available'])

        with self.assertRaises(rs.StandardsRefusal) as cm_503:
            rs.validate_citation_against_tree(
                'pkg/mod_000.py:1', 'STD-1', commit,
                slow_obs['tree_available'], slow_obs['tree_files'],
                git_timed_out=slow_obs['inspect_timed_out'],
            )
        self.assertEqual(503, cm_503.exception.status)
        self.assertIn(rs.refusal_git_inspection_timeout(commit), str(cm_503.exception))

        # Concurrent waiter succeeds because slow caller's lock hold is bounded strictly below lock_wait_seconds!
        entered = threading.Event()
        slow_holder_ids = set()
        results = {}

        def _selective_slow_git(args, **kw):
            if threading.get_ident() in slow_holder_ids:
                entered.set()
                time.sleep(0.15)
            return real_git(args, **kw)

        def _run_slow():
            slow_holder_ids.add(threading.get_ident())
            results['slow'] = rs.inspect_delivery_git(
                {'commit': commit, 'delivery': {'kind': 'bundle', 'path': _bundle_delivery_path(bpath), 'sha256': sha}},
                delivery_roots=[str(bpath.parent)],
                needed_paths=['pkg/mod_000.py'],
                lock_dir=self.base,
                lock_wait_seconds=2.0,
                max_inspect_seconds=0.25,
            )

        def _run_fast_waiter():
            entered.wait(timeout=1.5)
            results['waiter'] = rs.inspect_delivery_git(
                {'commit': commit, 'delivery': {'kind': 'bundle', 'path': _bundle_delivery_path(bpath), 'sha256': sha}},
                delivery_roots=[str(bpath.parent)],
                needed_paths=['pkg/mod_000.py'],
                lock_dir=self.base,
                lock_wait_seconds=2.0,
                max_inspect_seconds=5.0,
            )

        with mock.patch.object(rs, 'run_confined_git', side_effect=_selective_slow_git):
            t_slow = threading.Thread(target=_run_slow)
            t_wait = threading.Thread(target=_run_fast_waiter)
            t_slow.start()
            t_wait.start()
            t_slow.join(timeout=5.0)
            t_wait.join(timeout=5.0)
        self.assertTrue(results['slow']['inspect_timed_out'])
        self.assertTrue(results['waiter']['tree_available'])
        self.assertFalse(results['waiter']['lock_timed_out'])


if __name__ == '__main__':
    unittest.main()
