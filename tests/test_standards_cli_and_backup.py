"""CLI (client.py, http_client.py, admin.py), sign-off file export, backup/restore, and rollback compatibility tests for Phase 1 Review Standards (kittrial-5bb.570)."""
import argparse
import hashlib
import io
import json
import secrets
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import admin
import briefing
import client
import http_auth
import http_client
import http_service
import review_recommendations as rr
import review_standards as rs
import review_workflow as rw
from test_http_review_fixes import Harness
from test_review_standards import HostTracker, _ADMIN_STD_PARSER, _git, _make_bundle, _sample_rule

try:
    import endpoint
except ImportError:
    endpoint = None

_ORIG_HASH_PASSWORD = http_auth.hash_password


def setUpModule():
    http_auth.hash_password = lambda pw, **kw: _ORIG_HASH_PASSWORD(
        pw, n=16, r=8, p=1, minimum=kw.get('minimum', http_auth.MIN_PASSWORD)
    )
    http_auth._DUMMY_VERIFIER = http_auth.hash_password('orchestra-dummy-password')


def tearDownModule():
    http_auth.hash_password = _ORIG_HASH_PASSWORD
    http_auth._DUMMY_VERIFIER = None


class StandardsCliAndBackupTests(Harness):
    """Named CLI, sign-off file export, backup/restore-new, and legacy rollback tests."""

    def make_backend(self):
        self.delivery_root = self.tmp / 'bundles'
        self.delivery_root.mkdir(parents=True, exist_ok=True)
        return http_service.InProcessBackend(self.service, delivery_roots=[str(self.delivery_root)])

    def _run_http_cli(self, argv):
        normalized = []
        idx = 0
        while idx < len(argv):
            if argv[idx] == '--token' and idx + 1 < len(argv):
                normalized.append('--token=' + str(argv[idx + 1]))
                idx += 2
            else:
                normalized.append(argv[idx])
                idx += 1
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = http_client.main([
                '--base-url', 'http://127.0.0.1:%d' % self.port,
                *normalized,
            ])
        return rc, out.getvalue(), err.getvalue()

    def test_cli_standards_commands_and_signoff_file_export(self):
        # 1) Local `client.py standards inspect-commit` & `http_client.py standards inspect-commit`
        #    using real git (merge-base + --no-renames) + --out file export!
        repo = self.tmp / 'inspect-repo'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'guarded.py').write_text('def check():\n    return 1\n', encoding='utf-8')
        _git(repo, 'add', '.')
        _git(repo, 'commit', '-m', 'base on main')
        _git(repo, 'branch', '-M', 'main')

        _git(repo, 'checkout', '-b', 'work/feature')
        _git(repo, 'mv', 'guarded.py', 'renamed_guarded.py')
        (repo / 'added.py').write_text('a = 10\nb = 20\nc = 30\n', encoding='utf-8')
        _git(repo, 'add', '.')
        _git(repo, 'commit', '-m', 'rename guarded and add added.py')
        tip_sha = _git(repo, 'rev-parse', 'HEAD')

        client_out_file = self.tmp / 'client_inspect.json'
        with mock.patch.object(sys, 'argv', [
            'client.py', 'standards', 'inspect-commit',
            '--repo', str(repo),
            '--commit', tip_sha,
            '--target-ref', 'refs/heads/main',
            '--out', str(client_out_file),
        ]):
            rc_c = client.main()
        self.assertEqual(0, rc_c)
        inspected = json.loads(client_out_file.read_text(encoding='utf-8'))
        self.assertEqual(tip_sha, inspected['commit'])
        # --no-renames ensures BOTH old guarded.py AND renamed_guarded.py + added.py appear!
        self.assertEqual(['added.py', 'guarded.py', 'renamed_guarded.py'], inspected['changed_files'])
        self.assertEqual({'regular_text': True, 'line_count': 3}, inspected['tree_files']['added.py'])

        http_inspect_file = self.tmp / 'http_inspect.json'
        rc_h_insp = http_client.main([
            'standards', 'anyproj', 'inspect-commit',
            '--repo', str(repo),
            '--commit', tip_sha,
            '--target-ref', 'refs/heads/main',
            '--out', str(http_inspect_file),
        ])
        self.assertEqual(0, rc_h_insp)
        self.assertEqual(inspected['changed_files'], json.loads(http_inspect_file.read_text(encoding='utf-8'))['changed_files'])

        # 2) Full `http_client.py standards ...` lifecycle + JSON/Markdown/text `--out` sign-off exports!
        admin_tok = self.admin_token()
        owner_uid = self.create_account(admin_tok, 'olive', 'olive-pw-123456')
        owner_tok, _, _ = self.login('olive', 'olive-pw-123456')
        alice_uid = self.create_account(admin_tok, 'alice', 'alice-pw-123456')
        alice_tok, _, _ = self.login('alice', 'alice-pw-123456')
        bob_uid = self.create_account(admin_tok, 'bob', 'bob-pw-123456')
        bob_tok, _, _ = self.login('bob', 'bob-pw-123456')
        cora_uid = self.create_account(admin_tok, 'cora', 'cora-pw-123456')
        cora_tok, _, _ = self.login('cora', 'cora-pw-123456')

        pid = self.create_project(owner_tok, 'CLI Standards Project')
        self.request('PUT', '/v1/projects/%s/members/%s' % (pid, alice_uid), {'role': 'contributor'}, token=owner_tok)
        self.request('PUT', '/v1/projects/%s/members/%s' % (pid, bob_uid), {'role': 'contributor'}, token=owner_tok)
        self.request('PUT', '/v1/projects/%s/members/%s' % (pid, cora_uid), {'role': 'coordinator'}, token=owner_tok)

        ver_resp = self.request('POST', '/v1/projects/%s/worker-credentials' % pid, {'scopes': ['verify'], 'label': 'CI'}, token=owner_tok)
        self.assertEqual(201, ver_resp.status)
        ver_secret = ver_resp.data['credential']['secret']

        put_file = self.tmp / 'put_v1.json'
        put_file.write_text(json.dumps({
            'expected_version': 0,
            'reason': 'Initial CLI register v1.',
            'rules': [
                _sample_rule('STD-1', owner=alice_uid, severity='should'),
                _sample_rule('MECH-1', owner=alice_uid, severity='must', check={'kind': 'mechanical', 'tool': 'pytest', 'command': 'pytest -q'}),
            ],
        }), encoding='utf-8')
        rc, out, err = self._run_http_cli(['--token', owner_tok, 'standards', pid, 'put', '--file', str(put_file)])
        self.assertEqual(0, rc, err)
        self.assertEqual(1, json.loads(out)['version'])

        # Rule-edit by rule owner Alice -> v2
        edit_file = self.tmp / 'rule_edit.json'
        edit_file.write_text(json.dumps({
            'rule': _sample_rule('STD-1', owner=alice_uid, severity='must', statement='Alice promoted STD-1 to must.'),
        }), encoding='utf-8')
        rc, out, err = self._run_http_cli([
            '--token', alice_tok, 'standards', pid, 'rule-edit', 'STD-1',
            '--file', str(edit_file), '--expected-version', '1', '--reason', 'Promote STD-1',
        ])
        self.assertEqual(0, rc, err)
        self.assertEqual(2, json.loads(out)['version'])

        # Draft propose by Bob -> draft-list -> draft-decide accept by Owner -> v3
        draft_file = self.tmp / 'draft_prop.json'
        draft_file.write_text(json.dumps({
            'action': 'upsert',
            'rule': _sample_rule('DOC-1', owner=alice_uid, severity='should'),
        }), encoding='utf-8')
        rc, out, err = self._run_http_cli([
            '--token', bob_tok, 'standards', pid, 'draft-propose', '--file', str(draft_file), '--reason', 'Add DOC-1',
        ])
        self.assertEqual(0, rc, err)
        draft_id = json.loads(out)['id']

        rc, out, err = self._run_http_cli(['--token', owner_tok, 'standards', pid, 'draft-list', '--status', 'pending'])
        self.assertEqual(0, rc, err)
        self.assertEqual([draft_id], [d['id'] for d in json.loads(out)['items']])

        rc, out, err = self._run_http_cli([
            '--token', owner_tok, 'standards', pid, 'draft-decide', draft_id,
            '--decision', 'accept', '--expected-version', '2', '--reason', 'Accept DOC-1',
        ])
        self.assertEqual(0, rc, err)
        self.assertEqual(3, json.loads(out)['register']['version'])

        # show, history, lint, set-gate
        rc, out, err = self._run_http_cli(['--token', bob_tok, 'standards', pid, 'show'])
        self.assertEqual(0, rc, err)
        self.assertEqual(3, json.loads(out)['version'])

        rc, out, err = self._run_http_cli(['--token', bob_tok, 'standards', pid, 'history'])
        self.assertEqual(0, rc, err)
        self.assertEqual([3, 2, 1], [v['version'] for v in json.loads(out)['items']])

        rc, out, err = self._run_http_cli(['--token', bob_tok, 'standards', pid, 'lint'])
        self.assertEqual(0, rc, err)
        self.assertEqual(3, json.loads(out)['version'])

        rc, out, err = self._run_http_cli(['--token', owner_tok, 'standards', pid, 'set-gate', 'block', '--reason', 'Enforce block'])
        self.assertEqual(0, rc, err)
        self.assertEqual('block', json.loads(out)['mode'])

        # Deliver bundle by Bob, record-commit with --credential=ver_secret, waive MECH-1 by Alice, approve by Owner!
        bpath = self.delivery_root / 'cli_deliv.bundle'
        commit_sha, bundle_sha = _make_bundle(repo, bpath, ref='HEAD')
        tid = self.create_task(owner_tok, pid, 'CLI Sign-off Task').data['id']
        self.request('POST', '/v1/projects/%s/tasks/%s/claim' % (pid, tid), {}, token=bob_tok)
        self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'contribute', 'operation_id': 'op-cli-c1',
            'previous': None, 'supersedes': None, 'repository': 'https://example.invalid/r.git',
            'commit': commit_sha, 'base_commit': commit_sha,
            'delivery': {'kind': 'bundle', 'path': 'server:' + str(bpath), 'sha256': bundle_sha},
            'summary': 'CLI bundle delivery.',
        }, token=bob_tok)
        brief = self.request('GET', '/v1/projects/%s/tasks/%s/brief' % (pid, tid), token=owner_tok).data
        cid = brief['review']['contribution']['id']
        prev = brief['review']['latest_id']

        obs_file = self.tmp / 'commit_obs.json'
        obs_file.write_text(json.dumps({
            'changed_files': inspected['changed_files'],
            'tree_files': inspected['tree_files'],
            'ci_checks': [{'tool': 'pytest', 'command': 'pytest -q', 'exit_code': 1}],
        }), encoding='utf-8')
        rc, out, err = self._run_http_cli([
            'standards', pid, 'record-commit', commit_sha, '--file', str(obs_file), '--credential=' + ver_secret,
        ])
        self.assertEqual(0, rc, err)

        rc, out, err = self._run_http_cli([
            '--token', alice_tok, 'standards', pid, 'waive', tid,
            '--contribution', cid, '--commit', commit_sha, '--rule', 'MECH-1',
            '--this-delivery', '--reason', 'Waived via CLI for this delivery.',
        ])
        self.assertEqual(0, rc, err)

        rc, out, err = self._run_http_cli(['--token', bob_tok, 'standards', pid, 'waivers', tid])
        self.assertEqual(0, rc, err)
        self.assertEqual(1, len(json.loads(out)['items']))

        app_resp = self.request('POST', '/v1/projects/%s/tasks/%s/reviews' % (pid, tid), {
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-cli-a1',
            'previous': prev, 'contribution': cid, 'summary': 'Approved after CLI waiver + citation.',
            'checks': [{'rule_id': 'STD-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['added.py:1-3']}}],
        }, token=cora_tok)
        self.assertEqual(201, app_resp.status, app_resp.data)

        # Export sign-off via `--out` in all 3 formats (json, markdown, text)!
        so_json_path = self.tmp / 'signoff.json'
        so_md_path = self.tmp / 'signoff.md'
        so_txt_path = self.tmp / 'signoff.txt'

        for fmt, out_path in (('json', so_json_path), ('markdown', so_md_path), ('text', so_txt_path)):
            rc, out, err = self._run_http_cli([
                '--token', bob_tok, 'standards', pid, 'sign-off', tid,
                '--format', fmt, '--out', str(out_path),
            ])
            self.assertEqual(0, rc, err)
            self.assertEqual('', out)
            exported = out_path.read_text(encoding='utf-8')
            self.assertIn('Checked against standards register v3', exported)
            self.assertNotIn('certified', exported.lower())
            self.assertNotIn('guaranteed', exported.lower())

        # 3) Also verify SSH `client.py standards` export & SSH confinement when endpoint is available!
        if endpoint is not None:
            host_root = self.tmp / 'ssh_host'
            pdir = host_root / 'projects' / 'sshproj'
            (pdir / '.beads').mkdir(parents=True)
            (pdir / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
            (host_root / 'deployment.private.json').write_text(json.dumps({
                'password': 'secret', 'unit': 'none', 'operators': ['cora-op'], 'verifiers': [],
                'review_workflow_writes': True, 'delivery_roots': [str(self.delivery_root)],
            }), encoding='utf-8')
            tracker = HostTracker('sshproj')
            put_ssh = self.tmp / 'ssh_put.json'
            put_ssh.write_text(json.dumps({
                'expected_version': 0,
                'rules': [_sample_rule('SSH-1', owner='person:alice', severity='should')],
            }), encoding='utf-8')
            rs.admin_standards_command(
                host_root, pdir, 'sshproj',
                _ADMIN_STD_PARSER.parse_args(['put', '--project', 'sshproj', '--actor', 'cora-op', '--file', str(put_ssh), '--reason', 'v1']),
                lambda _r, _p, argv: tracker.run_argv(argv), ['cora-op'], [],
            )
            cfg_path = self.tmp / 'client_cfg.json'
            cfg_path.write_text(json.dumps({'host': 'local', 'user': 'git'}), encoding='utf-8')

            def _fake_request(_cfg, proj, act, args, action='bd', path=None):
                with mock.patch.object(endpoint.native, 'run', side_effect=tracker.native_run):
                    try:
                        return endpoint.execute(host_root, {'project': proj, 'actor': act, 'action': action, 'args': list(args)})
                    except ValueError as exc:
                        return {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: %s\n' % exc}

            ssh_show_out = self.tmp / 'ssh_show.json'
            with mock.patch.object(client, 'request', side_effect=_fake_request):
                with mock.patch.object(sys, 'argv', [
                    'client.py', '--config', str(cfg_path), '--project', 'sshproj', '--actor', 'cora-op',
                    'standards', 'show', '--out', str(ssh_show_out),
                ]):
                    rc_s = client.main()
                self.assertEqual(0, rc_s)
                self.assertEqual(1, json.loads(ssh_show_out.read_text(encoding='utf-8'))['version'])

                err_buf = io.StringIO()
                with redirect_stderr(err_buf), mock.patch.object(sys, 'argv', [
                    'client.py', '--config', str(cfg_path), '--project', 'sshproj', '--actor', 'cora-op',
                    'standards', 'set-gate', 'block', '--reason', 'try over ssh',
                ]):
                    rc_refused = client.main()
                self.assertNotEqual(0, rc_refused)
                self.assertIn(rs.refusal_ssh_person_only('set-gate'), err_buf.getvalue())

    def test_backup_and_restore_new_preserves_standards_and_supports_without_standards(self):
        root = self.tmp / 'backup_runtime'
        (root / 'backups').mkdir(parents=True)
        project = root / 'projects' / 'alpha'
        (project / '.beads').mkdir(parents=True)
        (project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')

        store = rs.HostStandardsStore(project, 'alpha')
        rs.put_register(
            store,
            {'expected_version': 0, 'reason': 'v1', 'rules': [_sample_rule('BKP-1', owner='person:alice', severity='should')]},
            actor='cora-op', author_person='person:cora', is_person_session=True, is_coordinator_or_owner=True, via='admin',
        )
        rs.put_register(
            store,
            {'expected_version': 1, 'reason': 'v2', 'rules': [_sample_rule('BKP-1', owner='person:alice', severity='must')]},
            actor='cora-op', author_person='person:cora', is_person_session=True, is_coordinator_or_owner=True, via='admin',
        )
        rs.set_gate_mode(
            store, 'block', 'Ready for block',
            actor='cora-op', author_person='person:cora', is_person_session=True, is_coordinator_or_owner=True, via='admin',
        )
        rs.propose_draft(
            store,
            {'action': 'upsert', 'reason': 'Draft preserved across backup', 'rule': _sample_rule('BKP-2', owner='person:alice', severity='should')},
            actor='worker-1',
        )
        commit_sha = 'a' * 40
        rs.record_commit_observation(
            store, commit_sha,
            {'changed_files': ['f.py'], 'ci_checks': [{'tool': 'pytest', 'command': 'pytest', 'exit_code': 0}]},
            actor='ci-verifier', recorded_by_user_id=None, recorded_by_principal='verifier:ci-verifier',
            is_authorized_recorder=True, same_party_checker=lambda other: other == 'ci-verifier',
        )
        rs.record_contribution_pin(store, '14', commit_sha, declared_tool='codex', declared_model='gpt-5')
        rs.grant_waiver(
            store,
            {'contribution_id': '14', 'commit': commit_sha, 'rule_id': 'BKP-1', 'expiry': {'kind': 'delivery'}, 'reason': 'Backup waiver'},
            task_id='alpha-1',
            contribution={'comment_id': '14', 'commit': commit_sha, 'author': 'worker-bob'},
            task_assignee='worker-bob',
            actor='person:alice', author_person='person:alice',
            is_person_session=True, is_coordinator_or_owner=False,
            same_party_checker=lambda other: other == 'person:alice',
        )

        # 1) Standards backup rides outside .coordination.json so legacy rollback ignores it cleanly!
        admin.validate_coordination_files({})
        bkp_record = rs.backup_record(project)
        self.assertIsNotNone(bkp_record)
        self.assertIn('head.json', bkp_record['files'])
        self.assertIn('gate.json', bkp_record['files'])

        def fake_backup(rt, name):
            (rt / 'backups' / name).mkdir(parents=True, exist_ok=True)
            (rt / 'backups' / (name + '.coordination.json')).write_text(
                json.dumps({'schema_version': 1, 'status': 'complete', 'files': {}, 'operators': []}),
                encoding='utf-8',
            )
            rec = rs.backup_record(rt / 'projects' / name)
            if rec is not None:
                raw = json.dumps(rec, sort_keys=True)
                (rt / 'backups' / (name + admin.STANDARDS_SUFFIX)).write_text(raw, encoding='utf-8')
                admin.last_complete_standards_path(rt, name).write_text(raw, encoding='utf-8')
            return 'native output'

        with mock.patch.object(admin, 'backup_project', side_effect=fake_backup):
            admin.backup_projects(root, ['alpha'])

        status = admin.read_backup_status(root)
        admin.validate_backup_status(status)
        entry = status['projects'][0]
        self.assertEqual('complete', entry['status'])
        self.assertEqual('backups/alpha.standards.json', entry['standards']['file'])
        self.assertEqual('backups/alpha.standards.last-complete.json', entry['standards']['last_complete']['file'])

        # Failed project entry must not claim a standards backup!
        bad_status = dict(status)
        bad_status['status'] = 'incomplete'
        bad_status['projects'] = [{'name': 'alpha', 'status': 'failed', 'reason': 'disk full', 'standards': entry['standards']}]
        with self.assertRaises(ValueError):
            admin.validate_backup_status(bad_status)

        # 2) Full restore into `restoredfull` preserves register v2, gate=block, drafts, commits, and waivers!
        dest_full = root / 'projects' / 'restoredfull'
        dest_full.mkdir(parents=True)
        msg = admin.restore_standards(root, 'alpha', 'restoredfull')
        self.assertIn('Restored', msg)

        restored_store = rs.HostStandardsStore(dest_full, 'restoredfull')
        # Stored version records bind project_id == 'alpha':
        restored_alpha_store = rs.HostStandardsStore(dest_full, 'alpha')
        restored_alpha_view = rs.resolve_effective_register(restored_alpha_store)
        self.assertEqual('active', restored_alpha_view['status'])
        self.assertEqual(2, restored_alpha_view['version'])
        self.assertEqual('block', rs.get_gate_view(restored_store)['mode'])
        self.assertEqual(1, len(rs.list_drafts_view(restored_store)['items']))
        self.assertEqual(commit_sha, restored_store.read_commit_observation(commit_sha)['commit'])
        self.assertEqual(1, len(rs.list_waivers_view(restored_store, 'alpha-1')['items']))

        # Restoring again into an already-populated `.standards/` is refused, never overwritten!
        with self.assertRaises(ValueError):
            admin.restore_standards(root, 'alpha', 'restoredfull')

        # 3) Canonical missing -> falls back to `.standards.last-complete.json`!
        canonical_std = root / 'backups' / 'alpha.standards.json'
        canonical_std.unlink()
        outcome, chosen, _, problem = admin.standards_backup_outcome(root, 'alpha')
        self.assertEqual(('answer', admin.last_complete_standards_path(root, 'alpha'), None), (outcome, chosen, problem))

        # 4) Damaged standards backup refuses normal restore, and `--without-standards` skips cleanly!
        tampered = json.loads(admin.last_complete_standards_path(root, 'alpha').read_text(encoding='utf-8'))
        tampered['files']['head.json']['base64'] = 'AAAA'
        canonical_std.write_text(json.dumps(tampered), encoding='utf-8')
        admin.last_complete_standards_path(root, 'alpha').unlink()

        outcome_bad, _, _, problem_bad = admin.standards_backup_outcome(root, 'alpha')
        self.assertEqual('refuse', outcome_bad)
        self.assertIn('head.json', problem_bad)

        dest_skip = root / 'projects' / 'restoredskip'
        dest_skip.mkdir(parents=True)
        with self.assertRaises(ValueError):
            admin.restore_standards(root, 'alpha', 'restoredskip')

        skip_msg = admin.restore_standards(root, 'alpha', 'restoredskip', without_standards=True)
        self.assertIn('NOT restored (--without-standards)', skip_msg)
        self.assertFalse((dest_skip / '.standards').exists())

    @unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
    def test_stored_contribution_review_v1_comment_stays_legacy_compatible_for_rollback(self):
        host_root = self.tmp / 'rollback_host'
        pid = 'rbproj'
        pdir = host_root / 'projects' / pid
        (pdir / '.beads').mkdir(parents=True)
        (pdir / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (host_root / 'deployment.private.json').write_text(json.dumps({
            'password': 'secret', 'unit': 'none', 'operators': ['cora-op'], 'verifiers': [],
            'review_workflow_writes': True, 'checkpoint_provenance_writes': True,
            'delivery_roots': [str(self.delivery_root)],
        }), encoding='utf-8')
        tracker = HostTracker(pid)

        # Create active block register
        put_f = self.tmp / 'rb_put.json'
        put_f.write_text(json.dumps({
            'expected_version': 0,
            'rules': [_sample_rule('RB-1', owner='person:alice', severity='must')],
        }), encoding='utf-8')
        rs.admin_standards_command(
            host_root, pdir, pid,
            _ADMIN_STD_PARSER.parse_args(['put', '--project', pid, '--actor', 'cora-op', '--file', str(put_f), '--reason', 'v1']),
            lambda _r, _p, argv: tracker.run_argv(argv), ['cora-op'], [],
        )
        rs.admin_standards_command(
            host_root, pdir, pid,
            _ADMIN_STD_PARSER.parse_args(['set-gate', '--project', pid, '--actor', 'cora-op', '--mode', 'block', '--reason', 'block']),
            lambda _r, _p, argv: tracker.run_argv(argv), ['cora-op'], [],
        )

        # Build bundle and run contribute -> recommend -> approve through endpoint.execute!
        repo = self.tmp / 'rb-repo'
        repo.mkdir()
        _git(repo, 'init')
        (repo / 'app.py').write_text('def main():\n    return 0\n', encoding='utf-8')
        _git(repo, 'add', '.')
        _git(repo, 'commit', '-m', 'init')
        bpath = self.delivery_root / 'rb.bundle'
        commit_sha, bundle_sha = _make_bundle(repo, bpath)

        def ep(action, args, actor, attachments=None):
            req = {'project': pid, 'actor': actor, 'action': action, 'args': list(args)}
            if attachments:
                req['attachments'] = attachments
            real_sub = subprocess.run
            with mock.patch.object(endpoint.native, 'run', side_effect=tracker.native_run), \
                 mock.patch.object(endpoint.subprocess, 'run', side_effect=lambda cmd, *a, **kw: (
                     tracker.native_run(cmd) if isinstance(cmd, (list, tuple)) and cmd and Path(str(cmd[0])).name == 'bd'
                     else real_sub(cmd, *a, **kw)
                 )):
                return endpoint.execute(host_root, req)

        tid = tracker.create_task('Rollback Task', assignee='worker-bob')

        c_body = json.dumps({
            'schema_version': 1, 'operation': 'contribute', 'operation_id': 'op-rb-c1',
            'task': tid, 'previous': None, 'supersedes': None,
            'repository': 'https://example.invalid/rb.git',
            'commit': commit_sha, 'base_commit': commit_sha,
            'delivery': {'kind': 'bundle', 'path': 'server:' + str(bpath), 'sha256': bundle_sha},
            'summary': 'Delivered rollback test bundle.',
            'declared_tool': 'codex', 'declared_model': 'gpt-5',
        })
        self.assertEqual(0, ep('review', [tid, '@attachment:0'], 'worker-bob', {'0': {'flag': '--file', 'text': c_body}})['returncode'])

        cid = tracker.rows[tid]['comments'][-1]['id']
        rec_body = json.dumps({
            'schema_version': 1, 'operation': 'recommend', 'operation_id': 'op-rb-rec1',
            'task': tid, 'contribution': cid, 'commit': commit_sha, 'verdict': 'approve',
            'summary': 'Checked RB-1.', 'items': [],
            'declared_tool': 'claude-code', 'declared_model': 'claude-opus-4-6',
            'checks': [{'rule_id': 'RB-1', 'result': 'pass', 'evidence': {'kind': 'citations', 'citations': ['app.py:1-2']}}],
        })
        self.assertEqual(0, ep('review', [tid, '@attachment:0'], 'reviewer-sam', {'0': {'flag': '--file', 'text': rec_body}})['returncode'])

        rec_id = tracker.rows[tid]['comments'][-1]['id']
        app_body = json.dumps({
            'schema_version': 1, 'operation': 'approve', 'operation_id': 'op-rb-a1',
            'task': tid, 'previous': cid, 'contribution': cid,
            'summary': 'Approved adopting recommendation.',
            'adopt_recommendation': rec_id,
            'declared_tool': 'web', 'declared_model': 'human',
        })
        self.assertEqual(0, ep('review', [tid, '@attachment:0'], 'cora-op', {'0': {'flag': '--file', 'text': app_body}})['returncode'])

        # Exercise real admin.py CLI parser on standards history, draft-list, waivers, and sign-off (Opus N5)!
        def _run_admin_cli(*cli_args):
            buf = io.StringIO()
            with mock.patch.object(admin, 'run_bd', side_effect=lambda _r, _p, argv: tracker.run_argv(argv)), \
                 mock.patch.object(sys, 'argv', ['admin.py', '--root', str(host_root), *cli_args]), \
                 redirect_stdout(buf):
                admin.main()
            return buf.getvalue()

        adm_hist = json.loads(_run_admin_cli('standards', 'history', '--project', pid))
        self.assertEqual([1], [v['version'] for v in adm_hist['items']])
        adm_drafts = json.loads(_run_admin_cli('standards', 'draft-list', '--project', pid, '--status', 'pending'))
        self.assertEqual([], adm_drafts['items'])
        adm_waivers = json.loads(_run_admin_cli('standards', 'waivers', '--project', pid, '--task', tid))
        self.assertEqual([], adm_waivers['items'])
        adm_so_md = _run_admin_cli('standards', 'sign-off', '--project', pid, '--task', tid, '--format', 'markdown')
        self.assertIn('Checked against standards register v1', adm_so_md)

        # Inspect raw stored comments: Kind: contribution-review-v1 MUST NOT contain any standards keys!
        issue = tracker.rows[tid]
        cr_comments = [c for c in issue['comments'] if c['text'].startswith(rw.PREFIX)]
        self.assertEqual(2, len(cr_comments))
        for c in cr_comments:
            parsed = json.loads(c['text'][len(rw.PREFIX):])
            for forbidden_key in ('checks', 'declared_tool', 'declared_model', 'adopt_recommendation', 'sign_off', 'standards'):
                self.assertNotIn(forbidden_key, parsed, c['text'])

        # Validate chain with review_workflow.history + projection even if .standards/ is completely removed (rollback)!
        import shutil
        shutil.rmtree(pdir / '.standards')
        ordered, voids, invalid, refused, positions, reverts, invalid_reverts = rw.history(issue, ['cora-op'], pdir)
        self.assertEqual([], invalid)
        self.assertEqual([], refused)
        proj = rw.projection(ordered, voids, invalid, refused, positions, reverts, invalid_reverts, closed=False)
        self.assertEqual('awaiting-integration', proj['review_state'])

        # And brief reads cleanly without .standards/ and omits any 'standards' key!
        brief_res = json.loads(ep('brief', [tid, '--json'], 'cora-op')['stdout'])
        self.assertNotIn('standards', brief_res)
        self.assertEqual('awaiting-integration', brief_res['review']['review_state'])


if __name__ == '__main__':
    unittest.main()
