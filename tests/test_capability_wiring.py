"""The capability client split, endpoint dispatch and operator commands (kittrial-5bb.67).

client.py: `capability lookup|resolve|index` stay local and need no config (the .61
behaviour, pinned by tests/test_capabilities.py); `capability find|get|list|propose|
revise|propose-alias` go to the endpoint; `capability lookup` with --config/--project
adds the endpoint's records with live pointer resolution. The endpoint's capability
reads take no lock and no journal; its writes are locked and run_guarded.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import admin
import capability_records as cr
import client
from test_capability_records import CapabilityNative, entry
from test_reference_records import OPERATOR, TODAY, acceptance
from test_reference_wiring import _endpoint_module


class ClientSplitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / 'client.json'
        self.config.write_text(json.dumps({'transport': 'ssh', 'host': 'h', 'endpoint': '/e.py', 'root': '/r'}),
                               encoding='utf-8')
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'review_workflow.py').write_text('def execute():\n    return 1\n', encoding='utf-8')

    def main(self, *argv, config=True, request=None):
        calls = []

        def fake_request(client_config, project, actor, args, action='bd', path=None):
            calls.append((action, list(args)))
            if request is not None:
                return request(action, args)
            return {'stdout': '{}', 'stderr': '', 'returncode': 0}
        prefix = ['client.py'] + (['--config', str(self.config), '--project', 'p', '--actor', 'alice']
                                  if config else []) + ['--']
        with patch.object(client, 'request', side_effect=fake_request), patch.object(sys, 'argv', prefix + list(argv)), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                code = client.main()
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue(), calls

    def test_lookup_resolve_and_index_stay_local(self):
        for argv in (('capability', 'lookup', 'execute', '--repo', str(self.repo)),
                     ('capability', 'resolve', 'review_workflow.py::execute', '--repo', str(self.repo)),
                     ('capability', 'index', '--repo', str(self.repo))):
            with self.subTest(argv=argv):
                code, out, _, calls = self.main(*argv, config=False)
                self.assertEqual((code, calls), (0, []))
                self.assertIn('"contract": "cli-contract-v1"', out)
                if argv[1] != 'lookup':
                    self.assertEqual(self.main(*argv)[3], [])   # still local with a config

    def test_endpoint_subcommands_need_a_config_and_reach_the_endpoint(self):
        code, _, err, calls = self.main('capability', 'find', 'merge slot', config=False)
        self.assertEqual((code, calls), (2, []))
        self.assertIn('--config', err)
        for argv, expected in ((('capability', 'find', 'merge slot'), ['find', 'merge slot']),
                               (('capability', 'get', 'merge.slot'), ['get', 'merge.slot']),
                               (('capability', 'list', '--state', 'accepted'), ['list', '--state', 'accepted']),
                               (('capability', 'propose', '--file', 'e.json'), ['propose', '--file', 'e.json']),
                               (('capability', 'propose-alias', 'merge.slot', 'lock'),
                                ['propose-alias', 'merge.slot', 'lock'])):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv)[3], [('capability', expected)])

    def test_lookup_with_a_config_merges_records_resolved_live(self):
        found = {'found': True, 'hint': None, 'coverage': 'records only',
                 'records': [{'key': 'review.flow', 'trust': 'accepted', 'name': {'text': 'Review', 'omitted_chars': 0},
                              'code': ['review_workflow.py::execute', 'review_workflow.py::missing'],
                              'tests': [], 'anchors': ['README.md#nothing']}],
                 'candidates': [{'key': 'merge.slot', 'trust': 'draft', 'code': [], 'tests': [], 'anchors': []}]}
        reply = lambda action, args: {'returncode': 0, 'stdout': json.dumps(found), 'stderr': ''}
        code, out, _, calls = self.main('capability', 'lookup', 'execute', '--repo', str(self.repo), request=reply)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [('capability', ['find', 'execute', '--limit', '5'])])
        payload = json.loads(out)
        self.assertEqual(payload['schema'], 'capability-lookup-v1')
        self.assertTrue(payload['found'])   # the local code lookup is unchanged
        exact = payload['records'][0]
        self.assertEqual((exact['match'], exact['trust']), ('exact', 'accepted'))
        self.assertEqual([row['live'] for row in exact['code']], ['resolved', 'missing'])
        self.assertEqual(exact['anchors'][0]['live'], 'missing')
        self.assertEqual(payload['records'][1]['match'], 'candidate')
        self.assertTrue(payload['records_found'])

    def test_an_unreachable_endpoint_keeps_the_local_lookup(self):
        def fail(action, args):
            raise RuntimeError('SSH failed (255); outcome may be uncertain.')
        code, out, _, _ = self.main('capability', 'lookup', 'execute', '--repo', str(self.repo), request=fail)
        payload = json.loads(out)
        self.assertEqual((code, payload['records']), (0, []))
        self.assertIn('records unavailable', payload['records_warning'])
        self.assertTrue(payload['found'])


class EndpointDispatchTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = _endpoint_module(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / 'projects' / 'p'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'unit': 'none', 'operators': [OPERATOR]}), encoding='utf-8')
        self.native = CapabilityNative()
        self.locks = Mock()
        self.guarded = []
        clock = patch('time.gmtime', return_value=TODAY)
        clock.start()
        self.addCleanup(clock.stop)

    def fake_run(self, argv, env, timeout=None):
        command = list(map(str, argv))
        command = command[command.index('--sandbox') + 1:]
        if command[:1] == ['--actor']:
            self.native.actor = command[1]
            command = command[2:]
        try:
            stdout = self.native(command)
        except (ValueError, RuntimeError) as error:
            return types.SimpleNamespace(returncode=1, stdout='', stderr=str(error))
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr='')

    def execute(self, args, attachments=None, actor='alice'):
        original = self.endpoint.run_guarded

        def guarded(request, *rest, **kwargs):
            self.guarded.append(request['action'])
            return original(request, *rest, **kwargs)

        request = {'project': 'p', 'actor': actor, 'action': 'capability', 'args': args,
                   'attachments': attachments or {}}
        with patch.object(self.endpoint, 'project_dir', return_value=self.project), \
                patch.object(self.endpoint, 'environment', return_value={}), \
                patch.object(self.endpoint.native, 'run', side_effect=self.fake_run), \
                patch.object(self.endpoint.fcntl, 'flock', self.locks), \
                patch.object(self.endpoint, 'run_guarded', side_effect=guarded):
            reply = self.endpoint.execute(self.root, request)
        self.assertEqual(reply['returncode'], 0, reply)
        return json.loads(reply['stdout'])

    def test_writes_are_locked_and_guarded_and_reads_are_neither(self):
        payload = {k: v for k, v in entry().items() if k != 'operation'}
        written = self.execute(['propose', '@attachment:0'], {'0': {'flag': '--file', 'text': json.dumps(payload)}})
        self.assertEqual(written['state'], 'draft')
        # The endpoint never writes a verified alias, even for an actor named like an operator.
        aliased = self.execute(['propose-alias', 'review.structured-contribution', 'reserved label guard'],
                               actor=OPERATOR)
        self.assertEqual((aliased['state'], aliased['identity']), ('proposed', 'unverified'))
        self.assertEqual((self.locks.call_count, self.guarded), (2, ['capability', 'capability']))
        self.locks.reset_mock()
        self.guarded.clear()
        self.assertEqual(self.execute(['get', 'review.structured-contribution'])['state'], 'draft-only')
        self.assertEqual(self.execute(['list'])['total'], 1)
        self.assertEqual(self.execute(['find', 'reserved label guard'])['candidates'][0]['key'],
                         'review.structured-contribution')
        self.assertEqual(self.execute(['find', '--help'])['action'], 'capability')
        # No read takes the coordination lock (a file object here) or writes a journal row.
        # The one flock a find may make is the lookup-miss log's own (kittrial-5bb.77): a
        # descriptor of .capability-misses.lock, never blocking, where the platform has flock.
        coordination = [call for call in self.locks.call_args_list if not isinstance(call.args[0], int)]
        telemetry = [call for call in self.locks.call_args_list if isinstance(call.args[0], int)]
        self.assertEqual((coordination, self.guarded), ([], []))
        self.assertLessEqual(len(telemetry), 1)
        self.assertTrue(all(call.args[1] & self.endpoint.fcntl.LOCK_NB for call in telemetry))
        self.assertEqual((self.project / '.capability-misses.lock').exists(), bool(telemetry))


class OperatorCommandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / 'projects' / 'trial'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'unit': 'none', 'operators': [OPERATOR]}), encoding='utf-8')
        self.native = CapabilityNative()
        self.flock = Mock()
        for patcher in (patch.dict(os.environ, {'ORCHESTRA_OPERATORS': ''}),
                        patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=self.flock, LOCK_EX=2)}),
                        patch('time.gmtime', return_value=TODAY)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.native.actor = 'alice'
        cr.apply_native(entry(), 'alice', self.native, self.project)
        cr.apply_native(entry(operation_id='p2', key='merge.slot', name='Merge slot', aliases=[]),
                        'alice', self.native, self.project)

    def run_native(self, root, name, args):
        if args[:1] == ['--actor']:
            self.native.actor = args[1]
            args = args[2:]
        return self.native(list(args))

    def admin(self, command, payload, actor=OPERATOR):
        path = self.root / 'payload.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), command, 'trial', '--actor', actor,
                                        '--file', str(path)]), \
                patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'run_bd', side_effect=self.run_native), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        return json.loads(out.getvalue())

    def sha(self, key, revision=1):
        row, _ = cr.anchor_for(cr.read_rows(self.native), key)
        return cr.existing_revisions(row)[revision]['sha256']

    def test_apply_retire_and_alias_reject_through_admin(self):
        batch = {'schema_version': 1, 'operation_id': 'batch-1', 'acceptance_state': 'accepted',
                 'acceptance': acceptance(),
                 'items': [{'key': key, 'revision': 1, 'record_sha256': self.sha(key)}
                           for key in ('review.structured-contribution', 'merge.slot')]}
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.admin('capability-apply', batch, actor='mallory')
        self.flock.reset_mock()
        applied = self.admin('capability-apply', batch)
        self.assertEqual([item['result'] for item in applied['items']], ['accepted', 'accepted'])
        # The batch takes the coordination lock per item (receipt, two items, final receipt)
        # on a file it closes each time, never once around the whole run.
        self.assertEqual(self.flock.call_count, 4)
        self.assertTrue(all(call.args[0].closed for call in self.flock.call_args_list))
        self.assertEqual(len({id(call.args[0]) for call in self.flock.call_args_list}), 4)
        retired = self.admin('capability-retire', {
            'schema_version': 1, 'operation_id': 'retire-1', 'key': 'merge.slot', 'revision': 2,
            'record_sha256': self.sha('merge.slot', 2), 'successor': 'review.structured-contribution',
            'acceptance_state': 'superseded', 'acceptance': acceptance()})
        self.assertEqual(retired['state'], 'superseded')
        self.native.actor = 'alice'
        cr.propose_alias('review.structured-contribution', 'review loop', 'alice', self.native, [OPERATOR])
        rejected = self.admin('capability-alias-reject', {'schema_version': 1,
                                                          'key': 'review.structured-contribution',
                                                          'alias': 'review loop', 'reason': 'too vague'})
        self.assertEqual(rejected['state'], 'rejected')
        # The operator shell route is the only one that writes a verified alias.
        alias = {'schema_version': 1, 'key': 'review.structured-contribution', 'alias': 'operator phrase'}
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.admin('capability-alias-propose', alias, actor='mallory')
        with self.assertRaisesRegex(ValueError, 'alias payload must be'):
            self.admin('capability-alias-propose', dict(alias, identity='verified'))
        self.flock.reset_mock()
        proposed = self.admin('capability-alias-propose', alias)
        self.assertEqual((proposed['state'], proposed['identity']), ('proposed', 'verified'))
        self.assertEqual(self.flock.call_count, 1)
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'capability-reconcile', 'trial',
                                        '--operation-id', 'nope', '--actor', OPERATOR, '--reason', 'r']), \
                patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'run_bd', side_effect=self.run_native):
            with self.assertRaisesRegex(ValueError, 'No capability operation receipt'):
                admin.main()


if __name__ == '__main__':
    unittest.main()
