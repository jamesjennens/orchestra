"""Standing guidance channel (kittrial-5bb.99 slice 1): record, version, read, ack."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin
import briefing
import guidance
from work import queue

TASK = 'trial-task'
PROJECT = 'trial'


def rows():
    return [dict(id=TASK, title='A task', issue_type='task', status='in_progress',
                 assignee='worker-1', description='Intent', acceptance_criteria='Acceptance',
                 labels=[], comments=[])]


class GuidanceRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.project = self.root / 'project'; self.project.mkdir()

    def test_absent_guidance_reads_as_absent_not_an_error(self):
        self.assertEqual(guidance.read_text(self.project), None)
        state = guidance.state(self.project, 'worker-1')
        self.assertFalse(state['present']); self.assertIsNone(state['version'])
        self.assertFalse(state['attention'])
        result = guidance.read(self.project, [])
        self.assertFalse(result['present']); self.assertIsNone(result['text'])

    def test_write_read_version_and_audit(self):
        first = guidance.write_guidance(self.project, 'Read the design note first.', 'operator-1')
        self.assertTrue(first['changed'])
        self.assertEqual(first['version'], guidance.version_of('Read the design note first.'))
        self.assertIsNone(first['previous_version'])
        state = guidance.state(self.project, 'worker-1')
        self.assertTrue(state['present']); self.assertEqual(state['set_by'], 'operator-1')
        self.assertEqual(state['version'], first['version']); self.assertTrue(state['attention'])
        self.assertFalse(state['acknowledged'])
        self.assertEqual(guidance.read(self.project, ['get'])['text'], 'Read the design note first.')

    def test_second_write_keeps_previous_version_text_and_history(self):
        guidance.write_guidance(self.project, 'First guidance', 'operator-1')
        second = guidance.write_guidance(self.project, 'Second guidance', 'operator-2')
        meta = guidance.read_meta(self.project)
        self.assertEqual(second['previous_version'], guidance.version_of('First guidance'))
        self.assertEqual(meta['previous_text'], 'First guidance')
        self.assertEqual(len(meta['history']), 1)
        self.assertEqual(meta['history'][0]['version'], guidance.version_of('First guidance'))
        # What changed since the version this caller still had.
        result = guidance.read(self.project, ['get', '--since', second['previous_version']], 'worker-1')
        self.assertTrue(result['changed'])
        self.assertEqual(result['previous_text'], 'First guidance')
        # Since the current version, nothing changed.
        self.assertFalse(guidance.read(self.project, ['get', '--since', second['version']])['changed'])

    def test_unchanged_write_is_idempotent_and_keeps_audit(self):
        first = guidance.write_guidance(self.project, 'Same text', 'operator-1')
        again = guidance.write_guidance(self.project, 'Same text', 'operator-2')
        self.assertFalse(again['changed'])
        self.assertEqual(guidance.read_meta(self.project)['set_by'], 'operator-1')
        self.assertEqual(again['version'], first['version'])

    def test_size_plain_text_and_actor_are_bounded(self):
        for bad in ['x' * 8001, '  ', '', 5]:
            with self.subTest(bad=str(bad)[:20]), self.assertRaises(ValueError):
                guidance.write_guidance(self.project, bad, 'operator-1')
        with self.assertRaises(ValueError):
            guidance.write_guidance(self.project, 'ok', 'not a valid actor!')
        with self.assertRaises(ValueError):
            guidance.write_guidance(self.project, 'binary\x00text', 'operator-1')
        with self.assertRaises(ValueError):
            guidance.validate_text('escape\x1b[31m')

    def test_acknowledge_records_actor_and_clears_attention(self):
        guidance.write_guidance(self.project, 'Current guidance', 'operator-1')
        self.assertTrue(guidance.state(self.project, 'worker-1')['attention'])
        first = guidance.acknowledge(self.project, 'worker-1')
        self.assertFalse(first['reconciled']); self.assertEqual(first['version'], guidance.version_of('Current guidance'))
        self.assertTrue(first['acknowledged'])
        self.assertFalse(guidance.state(self.project, 'worker-1')['attention'])
        self.assertTrue(guidance.state(self.project, 'worker-1')['acknowledged'])
        again = guidance.acknowledge(self.project, 'worker-1')
        self.assertTrue(again['reconciled'])
        self.assertEqual(again['acknowledged_at'], first['acknowledged_at'])

    def test_new_guidance_returns_attention_to_an_acknowledged_actor(self):
        guidance.write_guidance(self.project, 'First', 'operator-1')
        guidance.acknowledge(self.project, 'worker-1')
        self.assertFalse(guidance.state(self.project, 'worker-1')['attention'])
        guidance.write_guidance(self.project, 'Second', 'operator-1')
        state = guidance.state(self.project, 'worker-1')
        self.assertTrue(state['attention'])
        self.assertFalse(guidance.state(self.project, 'worker-2')['acknowledged'])

    def test_stale_acknowledgement_and_missing_audit_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'No guidance'):
            guidance.acknowledge(self.project, 'worker-1')
        guidance.write_guidance(self.project, 'Text', 'operator-1')
        with self.assertRaisesRegex(ValueError, 'no longer current'):
            guidance.acknowledge(self.project, 'worker-1', 'a' * 64)
        # A text without audit metadata is still readable, but an acknowledgement is
        # refused rather than fabricating the audit record.
        (self.project / '.guidance.json').unlink()
        self.assertEqual(guidance.read_text(self.project), 'Text')
        self.assertIsNone(guidance.read_meta(self.project))
        with self.assertRaisesRegex(ValueError, 'audit metadata'):
            guidance.acknowledge(self.project, 'worker-1')

    def test_reader_tolerates_unknown_metadata_keys(self):
        guidance.write_guidance(self.project, 'Text', 'operator-1')
        meta = json.loads((self.project / '.guidance.json').read_text(encoding='utf-8'))
        meta['future_field'] = {'anything': True}
        (self.project / '.guidance.json').write_text(json.dumps(meta), encoding='utf-8')
        self.assertEqual(guidance.read_text(self.project), 'Text')
        self.assertEqual(guidance.state(self.project, 'worker-1')['set_by'], 'operator-1')

    def test_status_is_operator_only_and_lists_acks(self):
        guidance.write_guidance(self.project, 'Text', 'operator-1')
        guidance.acknowledge(self.project, 'worker-1')
        with self.assertRaisesRegex(ValueError, 'configured operator'):
            guidance.status(self.project, 'worker-1', ['operator-1'])
        with self.assertRaisesRegex(ValueError, 'No operator allowlist'):
            guidance.status(self.project, 'operator-1', [])
        report = guidance.status(self.project, 'operator-1', ['operator-1'])
        self.assertEqual(report['version'], guidance.version_of('Text'))
        self.assertEqual([row['actor'] for row in report['acknowledged']], ['worker-1'])
        self.assertTrue(report['acknowledged'][0]['current'])
        self.assertEqual(report['behind'], [])

    def test_brief_block_never_raises_and_marks_unreadable_guidance(self):
        self.assertIsNone(guidance.brief_block(None))
        block = guidance.brief_block(self.project, 'worker-1')
        self.assertFalse(block['present'])
        (self.project / 'GUIDANCE.md').write_text('x' * 9000, encoding='utf-8')
        broken = guidance.brief_block(self.project, 'worker-1')
        self.assertIsNone(broken['present']); self.assertIn('warning', broken)

    def test_symlinked_guidance_is_refused(self):
        secret = self.root / 'secret'; secret.write_text('secret', encoding='utf-8')
        try:
            (self.project / 'GUIDANCE.md').symlink_to(secret)
        except OSError:
            self.skipTest('No symlink privilege')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            guidance.read_text(self.project)

    def test_brief_and_work_and_resume_carry_the_version_block(self):
        guidance.write_guidance(self.project, 'Standing instruction', 'operator-1')
        result = briefing.brief(rows(), PROJECT, TASK, journal=self.project, actor='worker-1')
        self.assertTrue(result['guidance']['present'])
        self.assertEqual(result['guidance']['version'], guidance.version_of('Standing instruction'))
        self.assertTrue(result['guidance']['attention'])
        self.assertIn('Guidance: version', briefing.format_brief(result))
        page = queue(rows(), 'worker-1', [], journal=self.project)
        self.assertEqual(page['guidance']['version'], guidance.version_of('Standing instruction'))
        from sessions import execute as sessions_execute
        # The resume response (what worker.py resume prints) carries the same block.
        import json as json_module
        registry = self.project / '.sessions.json'
        actor = 'session-11111111-1111-1111-1111-111111111111'
        registry.write_text(json_module.dumps({'schema_version': 1, 'records': {
            '11111111-1111-1111-1111-111111111111': {
                'request_id': '11111111-1111-1111-1111-111111111111', 'actor': actor,
                'name': 'worker', 'created_at': '2026-10-03T00:00:00+00:00'}}}), encoding='utf-8')
        answer = sessions_execute(self.project, PROJECT, ['resume', '--request-id', '22222222-2222-2222-2222-222222222222'],
                                  lambda: '', actor=actor)
        self.assertEqual(answer['guidance']['version'], guidance.version_of('Standing instruction'))


class GuidanceBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.project = self.root / 'projects' / 'example'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        guidance.write_guidance(self.project, 'Be careful with X', 'operator-1')
        guidance.acknowledge(self.project, 'worker-1')

    def files(self):
        return {'GUIDANCE.md': {'text': guidance.read_text(self.project)},
                '.guidance.json': guidance.read_meta(self.project)}

    def test_valid_pair_passes_validation_and_round_trips_through_restore(self):
        files = self.files()
        admin.validate_coordination_files(files)
        dest = self.root / 'projects' / 'dest'; dest.mkdir(parents=True)
        with patch.object(admin, 'coordination_backup', return_value=files):
            admin.restore_coordination(self.root, 'source', 'dest')
        self.assertEqual(guidance.read_text(dest), 'Be careful with X')
        self.assertEqual(guidance.state(dest, 'worker-1')['version'], guidance.version_of('Be careful with X'))
        self.assertTrue(guidance.state(dest, 'worker-1')['acknowledged'])

    def test_malformed_guidance_records_are_refused(self):
        for bad in [{'GUIDANCE.md': {'text': ''}}, {'GUIDANCE.md': {'text': 'x', 'other': 1}},
                    {'.guidance.json': {'schema_version': 2}},
                    {'.guidance.json': dict(self.files()['.guidance.json'], unknown=1)},
                    {'.guidance.json': {}}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                admin.validate_coordination_files(bad)


class GuidanceAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.project = self.root / 'projects' / 'example'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        self.document = self.root / 'guidance.md'
        self.document.write_text('Operator guidance\n', encoding='utf-8')

    def run_admin(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            admin.main()
        return out.getvalue(), err.getvalue()

    @unittest.skipIf(sys.platform == 'win32', 'admin.py host commands take the POSIX lock')
    def test_only_a_configured_operator_may_set_guidance(self):
        with self.assertRaisesRegex(ValueError, 'No operator allowlist'):
            self.run_admin(['set-guidance', 'example', '--actor', 'operator-1', '--file', str(self.document)])
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'operators': ['operator-1']}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'configured operator'):
            self.run_admin(['set-guidance', 'example', '--actor', 'worker-1', '--file', str(self.document)])
        stdout, _ = self.run_admin(['set-guidance', 'example', '--actor', 'operator-1', '--file', str(self.document)])
        self.assertIn('installed', stdout)
        self.assertEqual(guidance.read_text(self.project), 'Operator guidance\n')
        self.assertEqual(guidance.read_meta(self.project)['set_by'], 'operator-1')

    @unittest.skipIf(sys.platform == 'win32', 'admin.py host commands take the POSIX lock')
    def test_guidance_status_cli_is_operator_only(self):
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'operators': ['operator-1']}), encoding='utf-8')
        guidance.write_guidance(self.project, 'Guidance', 'operator-1')
        guidance.acknowledge(self.project, 'worker-1')
        with self.assertRaisesRegex(ValueError, 'configured operator'):
            self.run_admin(['guidance-status', 'example', '--actor', 'worker-1'])
        stdout, _ = self.run_admin(['guidance-status', 'example', '--actor', 'operator-1'])
        report = json.loads(stdout)
        self.assertEqual(report['up_to_date'], ['worker-1'])


class GuidanceEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.project = self.root / 'projects' / 'example'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'operators': ['operator-1']}), encoding='utf-8')

    def call(self, action, args, actor='worker-1'):
        import endpoint
        return endpoint.execute(self.root, {'project': 'example', 'actor': actor, 'action': action, 'args': args})

    @unittest.skipIf(sys.platform == 'win32', 'endpoint.py uses the POSIX coordination lock')
    def test_get_ack_version_and_status_route_through_the_endpoint(self):
        import endpoint  # noqa: F401  (import here: endpoint imports fcntl)
        import guidance
        guidance.write_guidance(self.project, 'Endpoint guidance', 'operator-1')
        answer = json.loads(self.call('guidance', [])['stdout'])
        self.assertEqual(answer['text'], 'Endpoint guidance')
        self.assertTrue(answer['attention'])
        version = answer['version']
        acked = json.loads(self.call('guidance', ['ack'])['stdout'])
        self.assertTrue(acked['acknowledged'])
        again = json.loads(self.call('guidance', ['ack'])['stdout'])
        self.assertTrue(again['reconciled'])
        self.assertFalse(json.loads(self.call('guidance', ['version'])['stdout'])['attention'])
        self.assertEqual(json.loads(self.call('guidance', ['get', '--since', version])['stdout'])['changed'], False)
        with self.assertRaisesRegex(ValueError, 'configured operator'):
            self.call('guidance', ['status'], actor='worker-1')
        report = json.loads(self.call('guidance', ['status'], actor='operator-1')['stdout'])
        self.assertEqual(report['up_to_date'], ['worker-1'])

    @unittest.skipIf(sys.platform == 'win32', 'endpoint.py uses the POSIX coordination lock')
    def test_bad_guidance_arguments_are_refused(self):
        import endpoint  # noqa: F401
        for args in [['ack', 'extra'], ['status', 'extra'], ['--since'], ['--since', 'nope'],
                     ['get', '--since', 'a' * 64, '--since', 'b' * 64], ['frobnicate']]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.call('guidance', args)

    @unittest.skipIf(sys.platform == 'win32', 'endpoint.py uses the POSIX coordination lock')
    def test_client_routes_guidance_as_an_endpoint_action(self):
        import client
        config = self.root / 'config.json'; config.write_text('{}', encoding='utf-8')
        with patch.object(sys, 'argv', ['client.py', '--config', str(config), '--project', 'example',
                                        '--actor', 'worker-1', '--', 'guidance', 'get']), \
                patch.object(client, 'request', return_value={'stdout': '', 'stderr': '', 'returncode': 0}) as request:
            self.assertEqual(client.main(), 0)
            self.assertEqual(request.call_args.args[3:5], (['get'], 'guidance'))


if __name__ == '__main__':
    unittest.main()
