"""Unit, endpoint, admin, and git bundle/branch tests for Phase 1 Review Standards (kittrial-5bb.570)."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
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
            cid = '%s-c%04d' % (tid, self.cseq)
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
        'show', 'lint', 'notifications', 'put', 'rule-edit',
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

    def admin_std(self, op, *rest, project=None):
        target_pid = project or self.pid
        p_dir = self.root / 'projects' / target_pid
        tr = self.trackers[target_pid]
        parsed = _ADMIN_STD_PARSER.parse_args([op, '--project', target_pid] + [str(x) for x in rest])
        return rs.admin_standards_command(
            self.root,
            p_dir,
            target_pid,
            parsed,
            lambda _r, _p, argv: tr.run_argv(argv),
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
                'path': 'server:' + str(bpath),
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
                'delivery': {'kind': 'bundle', 'path': 'server:' + str(self.delivery_root / 'm21.bundle'), 'sha256': sha},
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
                {'commit': commit, 'delivery': {'kind': 'bundle', 'path': 'server:' + str(self.delivery_root / 'proto.bundle'), 'sha256': sha}},
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
                {'commit': commit, 'delivery': {'kind': 'bundle', 'path': 'server:' + str(self.delivery_root / 'proto.bundle'), 'sha256': sha}},
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
            {'commit': commit, 'delivery': {'kind': 'bundle', 'path': 'server:' + str(self.delivery_root / 'cite.bundle'), 'sha256': sha}},
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
        committed_file = so_dir / ('%s.json' % cid)
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


if __name__ == '__main__':
    unittest.main()
