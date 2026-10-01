"""The `ref` client action, its endpoint dispatch and the operator commands (kittrial-5bb.66).

Reads (`ref get`, `ref list`) go through the endpoint with no coordination lock and no
operation journal; `ref propose|revise` are writes under the lock and run_guarded.
`admin.py reference-apply` checks the deployment operator allowlist before any write;
`reference-reconcile` and `record-reconcile --kind` resolve stuck receipts.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import admin
import client
import reference_records as rr
from test_reference_records import OPERATOR, TODAY, RefNative, acceptance, entry


def _endpoint_module(case):
    """endpoint.py, importing it over a scoped fcntl stub where fcntl is missing (Windows)."""
    try:
        import fcntl  # noqa: F401
    except ImportError:
        stub = types.ModuleType('fcntl')
        stub.LOCK_EX = 1
        stub.flock = lambda *a, **k: None
        scoped = patch.dict(sys.modules, {'fcntl': stub})
        scoped.start()
        case.addCleanup(scoped.stop)   # also drops the stub-imported endpoint module
    import endpoint
    return endpoint


class ClientRoutingTests(unittest.TestCase):
    def test_the_client_routes_the_ref_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'client.json'
            config.write_text(json.dumps({'transport': 'ssh', 'host': 'h', 'endpoint': '/e.py', 'root': '/r'}),
                              encoding='utf-8')
            for argv_tail, expected in ((['ref', 'get', 'calendar.trading'], ['get', 'calendar.trading']),
                                        (['ref', 'propose', '--file', 'entry.json'],
                                         ['propose', '--file', 'entry.json'])):
                captured = {}

                def fake_request(client_config, project, actor, args, action='bd', path=None):
                    captured.update(action=action, args=args)
                    return {'stdout': '{}', 'stderr': '', 'returncode': 0}

                argv = ['client.py', '--config', str(config), '--project', 'p', '--actor', 'alice', '--',
                        *argv_tail]
                with patch.object(client, 'request', side_effect=fake_request), patch.object(sys, 'argv', argv):
                    self.assertEqual(client.main(), 0)
                self.assertEqual((captured['action'], captured['args']), ('ref', expected))


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
        self.native = RefNative()
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

        request = {'project': 'p', 'actor': actor, 'action': 'ref', 'args': args,
                   'attachments': attachments or {}}
        with patch.object(self.endpoint, 'project_dir', return_value=self.project), \
                patch.object(self.endpoint, 'environment', return_value={}), \
                patch.object(self.endpoint.native, 'run', side_effect=self.fake_run), \
                patch.object(self.endpoint.fcntl, 'flock', self.locks), \
                patch.object(self.endpoint, 'run_guarded', side_effect=guarded):
            return self.endpoint.execute(self.root, request)

    def test_propose_is_a_locked_guarded_write_and_reads_are_neither(self):
        attachments = {'0': {'flag': '--file', 'text': json.dumps(
            {k: v for k, v in entry().items() if k != 'operation'})}}
        written = self.execute(['propose', '@attachment:0'], attachments)
        self.assertEqual(written['returncode'], 0, written)
        self.assertEqual(json.loads(written['stdout'])['state'], 'draft')
        self.assertEqual((self.locks.call_count, self.guarded), (1, ['ref']))
        self.locks.reset_mock()
        self.guarded.clear()
        got = self.execute(['get', 'calendar.trading', '--json'])
        self.assertEqual(json.loads(got['stdout'])['state'], 'draft-only')
        listed = self.execute(['list', '--state', 'draft-only'])
        self.assertEqual(json.loads(listed['stdout'])['total'], 1)
        helped = self.execute(['--help'])
        self.assertEqual(json.loads(helped['stdout'])['action'], 'ref')
        self.assertEqual((self.locks.call_count, self.guarded), (0, []))

    def test_the_payload_must_come_from_the_file_and_must_not_set_its_operation(self):
        for args, attachments, message in (
                (['propose'], {}, 'takes --file'),
                (['propose', 'raw-json'], {}, 'takes --file'),
                (['propose', '@attachment:0'], {'0': {'flag': '--file', 'text': json.dumps(entry())}},
                 'must not set operation')):
            with self.assertRaisesRegex(ValueError, message):
                self.execute(args, attachments)
        self.assertEqual(self.native.writes(), [])

    def test_an_unknown_key_is_a_named_refusal(self):
        with self.assertRaisesRegex(ValueError, 'Unknown reference key calendar.nothing; use ref list'):
            self.execute(['get', 'calendar.nothing'])


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
        self.native = RefNative()
        for patcher in (patch.dict(os.environ, {'ORCHESTRA_OPERATORS': ''}),
                        patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=Mock(), LOCK_EX=2)}),
                        patch('time.gmtime', return_value=TODAY)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.native.actor = 'alice'
        rr.apply_native(entry(), 'alice', self.native, self.project)

    def run_native(self, root, name, args):
        if args[:1] == ['--actor']:
            self.native.actor = args[1]
            args = args[2:]
        return self.native(list(args))

    def admin(self, *argv):
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), *argv]), \
                patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'run_bd', side_effect=self.run_native), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        return json.loads(out.getvalue())

    def apply(self, payload, actor=OPERATOR):
        path = self.root / 'apply.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        return self.admin('reference-apply', 'trial', '--actor', actor, '--file', str(path))

    def accept_payload(self):
        row, _ = rr.anchor_for(rr.read_rows(self.native), 'calendar.trading')
        digest = rr.existing_revisions(row)[1]['sha256']
        return {'schema_version': 1, 'operation_id': 'owner-apply-1', 'key': 'calendar.trading',
                'revision': 1, 'record_sha256': digest, 'acceptance_state': 'accepted',
                'acceptance': acceptance()}

    def test_reference_apply_refuses_an_unlisted_actor_before_any_write(self):
        before = len(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.apply(self.accept_payload(), actor='rogue')
        self.assertEqual(len(self.native.writes()), before)

    def test_reference_apply_accepts_and_the_evidence_author_is_the_operator(self):
        result = self.apply(self.accept_payload())
        self.assertEqual((result['revision'], result['state']), (2, 'accepted'))
        view = rr.get(rr.read_rows(self.native), 'calendar.trading', [OPERATOR])
        self.assertEqual((view['state'], view['acceptance']['operator']), ('accepted', OPERATOR))

    def test_reference_and_record_reconcile_dispatch_to_the_kind(self):
        self.native.create_outcome = 'not-written'
        self.native.actor = 'alice'
        with self.assertRaises(RuntimeError):
            rr.apply_native(entry(operation_id='lost-1', key='calendar.lost'), 'alice', self.native, self.project)
        released = self.admin('reference-reconcile', 'trial', '--operation-id', 'lost-1', '--actor', OPERATOR,
                              '--reason', 'nothing was created', '--disposition', 'released')
        self.assertEqual(released['status'], 'released')
        again = self.admin('record-reconcile', 'trial', '--kind', 'reference', '--operation-id', 'lost-1',
                           '--actor', OPERATOR, '--reason', 'nothing was created', '--disposition', 'released')
        self.assertTrue(again['already'])
        with self.assertRaisesRegex(ValueError, 'No requirement operation receipt'):
            self.admin('record-reconcile', 'trial', '--kind', 'requirement', '--operation-id', 'lost-1',
                       '--actor', OPERATOR, '--reason', 'r', '--disposition', 'released')


if __name__ == '__main__':
    unittest.main()
