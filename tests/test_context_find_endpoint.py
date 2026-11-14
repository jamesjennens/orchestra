"""The real endpoint entry boundary; its tracker snapshot alone is modeled here."""
import copy
import json
import os
import sys
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
    def test_dispatch_and_help_remain_self_contained(self):
        import client
        self.assertIn('find', client.CLIENT_ACTIONS)
        for phrase in ('--author', '--since', '--until', '--task-prefix', '--item-kind', '--cursor'):
            self.assertIn(phrase, client.contributor_command_help('find'))
        self.assertIn('find', client.contributor_command_names())


if __name__ == '__main__':
    unittest.main()
