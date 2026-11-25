"""The real endpoint entry boundary; its tracker snapshot alone is modeled here."""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import context_find
from test_context_find import row, ACTOR


@unittest.skipUnless(os.name == 'posix', 'canonical endpoint is a POSIX server')
class EndpointFindTests(unittest.TestCase):
    def setUp(self):
        # Deferred: importing endpoint at module level would break Windows readers.
        import endpoint
        from test_endpoint_authority_block import EndpointHarness, PROJECT
        self.endpoint, self.project = endpoint, PROJECT
        self.case = EndpointHarness('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.rows = [row('pp-1'), row('pp-2')]
        self.snapshot = mock.patch('read_cache.cached_export_snapshot',
                                   side_effect=lambda *a, **kw: {'rows': copy.deepcopy(self.rows), 'generation': 'g1'})
        self.cache = self.snapshot.start(); self.addCleanup(self.snapshot.stop)
        p = mock.patch('read_cache.project_generation', return_value='g1')
        p.start(); self.addCleanup(p.stop)

    def request(self, args=None, **extra):
        return dict({'project': self.project, 'actor': ACTOR, 'action': 'find',
                     'args': args or ['orthogonal', '--json'], 'attachments': {}}, **extra)

    def execute(self, request=None, **kwargs):
        return self.endpoint.execute(self.case.root, request or self.request(), **kwargs)

    def test_endpoint_one_loaded_snapshot_and_json_envelope(self):
        answer = self.execute()
        self.assertEqual(0, answer['returncode'], answer)
        body = json.loads(answer['stdout'])
        self.assertEqual(2, body['total'])
        self.assertEqual('current', body['stale'])
        self.assertEqual(1, self.cache.call_count)
        self.assertLessEqual(len(answer['stdout'].encode()), 16384)
        self.assertEqual([], self.case.tracker.writes)

    def test_default_find_reads_decisions_without_web_authority_state(self):
        self.rows.append(row('pp-3', issue_type='decision'))
        path = self.case.root / 'http-state.json'
        for contents in (None, b'{"schema_version": 999}', b'\xff'):
            with self.subTest(contents=contents):
                if contents is not None:
                    path.write_bytes(contents)
                body = json.loads(self.execute()['stdout'])
                self.assertEqual({'task:pp-1', 'task:pp-2', 'task:pp-3'},
                                 {item['pointer'] for item in body['items']})
                decision = next(item for item in body['items'] if item['kind'] == 'decision')
                self.assertEqual('not-attested', decision['state'])
                self.assertEqual('current', body['stale'])

    def test_web_authority_state_is_read_twice_for_multiple_decisions(self):
        from http_authority import read_state, SCHEMA_VERSION
        self.rows = [row('pp-%d' % n, issue_type='decision') for n in range(6)]
        path = self.case.root / 'http-state.json'
        path.write_text(json.dumps({'schema_version': SCHEMA_VERSION}))
        with mock.patch('http_authority.read_state', wraps=read_state) as read:
            body = json.loads(self.execute()['stdout'])
        self.assertEqual(6, body['total'])
        self.assertEqual([mock.call(path), mock.call(path)], read.call_args_list)

    def test_decision_grant_is_read_again_before_returning_a_cursor(self):
        import test_decision_attest as attest
        from http_authority import SCHEMA_VERSION
        path = self.case.root / 'http-state.json'
        snapshot = attest.web_state()
        for field in ('memberships', 'coordinator_grants'):
            snapshot[field][self.project] = snapshot[field].pop(attest.PROJECT)
        snapshot['agents'][attest.AGENT]['projects'] = [self.project]
        snapshot['schema_version'] = SCHEMA_VERSION
        path.write_text(json.dumps(snapshot))
        decision = row('pp-3', issue_type='decision')
        journal = attest.Case('runTest')
        journal.journal = self.case.root / 'projects' / self.project
        journal.seq = 0
        journal.attest(decision, attest.AGENT, project=self.project)
        self.rows.append(decision)

        args = self.request(['orthogonal', '--limit', '1', '--json'])
        current = json.loads(self.execute(args)['stdout'])
        self.assertIsNotNone(current['cursor'])
        self.assertEqual('counts', current['items'][0]['state'])

        search = context_find.search
        def revoke_after_search(*args, **kwargs):
            answer = search(*args, **kwargs)
            snapshot['coordinator_grants'][self.project].clear()
            path.write_text(json.dumps(snapshot))
            return answer
        with mock.patch.object(context_find, 'search', side_effect=revoke_after_search):
            checked = json.loads(self.execute(args)['stdout'])
        self.assertEqual('lagging', checked['stale'])
        self.assertIsNone(checked['cursor'])
        revoked = json.loads(self.execute(args)['stdout'])
        self.assertNotEqual('counts', revoked['items'][0]['state'])

    def test_cursor_binds_actor_but_omits_operator_authority(self):
        args = ['orthogonal', '--limit', '1', '--json']
        def page():
            return json.loads(self.execute(self.request(args), key_projects={self.project})['stdout'])
        with mock.patch.object(self.endpoint, 'configured_operators', return_value=['coordinator']):
            first = page()
        with mock.patch.object(self.endpoint, 'configured_operators', return_value=['another-coordinator']):
            second = page()
        self.assertIsNotNone(first['cursor'])
        self.assertEqual(first['cursor'], second['cursor'])
        with self.assertRaisesRegex(ValueError, 'start the search again'):
            self.execute(self.request(args + ['--cursor', first['cursor']], actor='alice/session2'),
                         key_projects={self.project})

    def test_configured_help_is_json_without_a_native_read(self):
        answer = self.execute(self.request(['--help']))
        self.assertEqual(0, answer['returncode'])
        body = json.loads(answer['stdout'])
        self.assertEqual('cli-contract-v1', body['contract'])
        self.assertEqual('find', body['command'])
        self.assertEqual(5, body['default_limit'])
        self.cache.assert_not_called()
        self.assertEqual([], self.case.tracker.calls)

    def test_find_help_respects_bound_project_scope(self):
        with self.assertRaisesRegex(ValueError, 'project'):
            self.execute(self.request(['--help']), key_projects={'different'})
        self.cache.assert_not_called()

    def test_denied_project_before_native_read_missing_and_existing_identical(self):
        messages = []
        for project in (self.project, 'missing'):
            with self.subTest(project=project), self.assertRaises(ValueError) as refused:
                self.execute(self.request(project=project), key_projects={'different'})
            messages.append(str(refused.exception))
        self.assertEqual(messages[0], messages[1])
        self.cache.assert_not_called()
        self.assertEqual([], self.case.tracker.calls)

    def test_caller_visibility_override_and_operation_identity_refused(self):
        for fields in ({'scope': {'kinds': list(context_find.KINDS)}}, {'operation_id': 'fake-id'},
                       {'documents': self.rows}, {'attachments': {'0': {'text': 'override'}}}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.execute(self.request(**fields))
        self.cache.assert_not_called()
        self.assertEqual([], self.case.tracker.writes)

    def test_final_live_principal_check_cannot_be_skipped(self):
        original = self.endpoint.key_principal_refusal
        calls = []
        def live(request, path, principal):
            calls.append(True)
            if len(calls) >= 3:
                raise ValueError('Key principal no longer owns this actor')
            return original(request, path, principal)
        with mock.patch.object(self.endpoint, 'key_principal_refusal', side_effect=live):
            with self.assertRaisesRegex(ValueError, 'no longer owns'):
                self.execute()
        self.assertGreaterEqual(len(calls), 3)

    def test_live_operator_scope_change_refuses_the_answer(self):
        original = context_find.search
        current = []
        def search(*args, **kwargs):
            answer = original(*args, **kwargs)
            current.append('new-operator')
            return answer
        with mock.patch.object(self.endpoint, 'configured_operators', side_effect=lambda root: list(current)), \
                mock.patch.object(context_find, 'search', side_effect=search):
            with self.assertRaisesRegex(ValueError, 'start the search again'):
                self.execute()

    def test_reader_acceptance_uses_the_same_operator_view_as_bound_scope(self):
        from test_reference_records import ReferenceCase, OPERATOR
        fixture = ReferenceCase('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.propose(statement='orthogonal accepted reference')
        fixture.accept(1, fixture.sha(1))
        self.rows = fixture.native.rows
        observations = []
        def live_operators(root):
            result = [OPERATOR] if not observations else []
            observations.append(result)
            return result
        with mock.patch.object(self.endpoint, 'configured_operators', side_effect=live_operators):
            try:
                result = self.execute(self.request(['orthogonal', '--kind', 'ref', '--json']))
            except ValueError as error:
                self.assertIn('start the search again', str(error))
                return
        answer = json.loads(result['stdout'])
        self.assertEqual(1, answer['total'])
        self.assertEqual('draft', answer['items'][0]['trust_class'])

    def test_unknown_generation_has_no_cursor(self):
        self.snapshot.stop()
        with mock.patch('read_cache.cached_export_snapshot', return_value={'rows': self.rows, 'generation': None}):
            result = self.execute(self.request(['orthogonal', '--limit', '1', '--json']))
        answer = json.loads(result['stdout'])
        self.assertEqual('unknown', answer['stale'])
        self.assertIsNone(answer['cursor'])

    def test_native_source_failure_cannot_become_a_current_empty_answer(self):
        self.cache.side_effect = ValueError('Native snapshot unavailable')
        with self.assertRaisesRegex(ValueError, 'Native snapshot unavailable'):
            self.execute()
        self.assertEqual([], self.case.tracker.writes)

    def test_visible_write_during_search_is_lagging_and_cannot_paginate(self):
        calls = []
        def snapshot(*args, **kwargs):
            calls.append(True)
            data = copy.deepcopy(self.rows)
            if len(calls) > 1:
                data[0]['description'] += ' changed'
            return {'rows': data, 'generation': 'g1' if len(calls) == 1 else 'g2'}
        with mock.patch('read_cache.cached_export_snapshot', side_effect=snapshot), \
                mock.patch('read_cache.project_generation', return_value='g2'):
            body = json.loads(self.execute(self.request(['orthogonal', '--limit', '1', '--json']))['stdout'])
        self.assertEqual('lagging', body['stale'])
        self.assertIsNone(body['cursor'])

    def test_hidden_machine_write_does_not_change_visible_generation_or_page(self):
        before = json.loads(self.execute()['stdout'])
        calls = []
        def snapshot(*args, **kwargs):
            calls.append(True)
            data = copy.deepcopy(self.rows)
            if len(calls) > 1:
                data.append(row('pp-event', text='orthogonal hidden event', issue_type='event'))
            return {'rows': data, 'generation': 'g1' if len(calls) == 1 else 'g2'}
        with mock.patch('read_cache.cached_export_snapshot', side_effect=snapshot), \
                mock.patch('read_cache.project_generation', return_value='g2'):
            after = json.loads(self.execute()['stdout'])
        for field in ('total', 'items', 'coverage', 'stale', 'cursor'):
            self.assertEqual(before[field], after[field], field)
        self.assertEqual(before['watermark']['generation'], after['watermark']['generation'])

    def test_http_descriptor_has_no_search_surface_and_no_native_read(self):
        # Even a server launch with the private authority config has no find route.
        with self.assertRaises(ValueError):
            self.execute(authority_config=self.case.config)
        self.cache.assert_not_called()
        self.assertEqual([], self.case.tracker.writes)

    def test_read_classification_is_shared_and_has_no_identity_reservation(self):
        request = self.request()
        self.assertTrue(self.endpoint.coordination_lock_shared(request))
        self.assertFalse(self.endpoint.coordination_lock_shared(dict(request, operation_id='not-a-read')))


class ClientFindTests(unittest.TestCase):
    def test_copied_client_has_find_help_without_application_modules(self):
        with tempfile.TemporaryDirectory() as root:
            copied = Path(root) / 'client.py'
            shutil.copyfile(ROOT / 'client.py', copied)
            answer = subprocess.run([sys.executable, '-I', str(copied), 'find', '--help'],
                                    cwd=root, text=True, capture_output=True, timeout=15)
        self.assertEqual(0, answer.returncode, answer.stderr)
        for option in ('--author', '--task-prefix', '--cursor', '--item-kind'):
            self.assertIn(option, answer.stdout)

    @unittest.skipUnless(os.name == 'posix', 'Local endpoint transport requires POSIX paths')
    def test_copied_client_local_transport_keeps_find_arguments_literal(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            copied, endpoint = root / 'client.py', root / 'endpoint.py'
            shutil.copyfile(ROOT / 'client.py', copied)
            endpoint.write_text('import json,sys\np=json.load(sys.stdin)\n'
                                'print(json.dumps(dict(returncode=0,stdout=json.dumps(p),stderr="")))\n')
            config = root / 'config.json'
            config.write_text(json.dumps(dict(transport='local', python=sys.executable,
                                              endpoint=str(endpoint), root=str(root))))
            local_file = root / 'private.txt'
            local_file.write_text('This stays local and is never attached.')
            args = ['orthogonal', '--file', str(local_file)]
            answer = subprocess.run([sys.executable, '-I', str(copied), '--config', str(config),
                                     '--project', 'sample', '--actor', ACTOR, '--', 'find', *args],
                                    cwd=root, text=True, capture_output=True, timeout=15)
        self.assertEqual(0, answer.returncode, answer.stderr)
        payload = json.loads(answer.stdout)
        self.assertEqual('find', payload['action'])
        self.assertEqual(args, payload['args'])
        self.assertEqual({}, payload['attachments'])

    def test_find_does_not_open_a_local_file_from_an_unsupported_option(self):
        import client
        arguments = ['orthogonal', '--file', '/not-a-file-that-find-may-read']
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('find opened a file')):
            payload = json.loads(client._wire('sample', ACTOR, arguments, 'find', None))
        self.assertEqual(arguments, payload['args'])
        self.assertEqual({}, payload['attachments'])

    def test_dispatch_and_help_remain_self_contained(self):
        import client
        self.assertIn('find', client.CLIENT_ACTIONS)
        for phrase in ('--author', '--since', '--until', '--task-prefix', '--item-kind', '--cursor'):
            self.assertIn(phrase, client.contributor_command_help('find'))
        self.assertIn('find', client.contributor_command_names())


if __name__ == '__main__':
    unittest.main()
