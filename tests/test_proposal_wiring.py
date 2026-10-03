"""The proposal client action, endpoint dispatch and host commands (kittrial-5bb.68).

client.py sends `proposal ...` to the endpoint. The endpoint's proposal reads take no
lock and no journal; `submit` and `revise` are locked and run_guarded; `review`,
`decide` and `settings` are not reachable there at all. The host commands
(`admin.py proposal-review|proposal-decide|proposal-settings|proposal-reconcile`)
read the operator allowlist strictly and are the only route to a disposition.
"""
import contextlib
import io
import json
import os
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
import client
import proposal_records as pr
from test_proposal_records import COORD, OWNER, ProposalNative, proposal
from test_reference_records import OPERATOR
from test_reference_wiring import _endpoint_module


class ClientRoutingTests(unittest.TestCase):
    def test_proposal_is_an_endpoint_action_and_needs_a_config(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, root, ignore_errors=True)
        config = root / 'client.json'
        config.write_text(json.dumps({'transport': 'ssh', 'host': 'h', 'endpoint': '/e.py', 'root': '/r'}),
                          encoding='utf-8')
        calls = []

        def fake_request(client_config, project, actor, args, action='bd', path=None):
            calls.append((action, list(args)))
            return {'stdout': '{}', 'stderr': '', 'returncode': 0}

        def main(*argv, configured=True):
            prefix = ['client.py'] + (['--config', str(config), '--project', 'p', '--actor', 'alice']
                                      if configured else []) + ['--']
            with patch.object(client, 'request', side_effect=fake_request), \
                    patch.object(sys, 'argv', prefix + list(argv)), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    code = client.main()
                except SystemExit as stop:
                    code = stop.code
            return code, err.getvalue()

        for argv, expected in ((('proposal', 'list', '--state', 'submitted'), ['list', '--state', 'submitted']),
                               (('proposal', 'get', 'p-000000000000'), ['get', 'p-000000000000']),
                               (('proposal', 'submit', '--file', 'p.json'), ['submit', '--file', 'p.json']),
                               (('proposal', 'mine', '--submitter', 'person:alex'),
                                ['mine', '--submitter', 'person:alex'])):
            with self.subTest(argv=argv):
                del calls[:]
                self.assertEqual(main(*argv)[0], 0)
                self.assertEqual(calls, [('proposal', expected)])
        del calls[:]
        code, err = main('proposal', 'list', configured=False)
        self.assertEqual((code, calls), (2, []))
        self.assertIn('--config', err)


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
        self.native = ProposalNative()
        self.locks = Mock()
        self.guarded = []

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

    def execute(self, args, attachments=None, actor='alice', ok=True):
        original = self.endpoint.run_guarded

        def guarded(request, *rest, **kwargs):
            self.guarded.append(request['action'])
            return original(request, *rest, **kwargs)

        request = {'project': 'p', 'actor': actor, 'action': 'proposal', 'args': args,
                   'attachments': attachments or {}}
        with patch.object(self.endpoint, 'project_dir', return_value=self.project), \
                patch.object(self.endpoint, 'environment', return_value={}), \
                patch.object(self.endpoint.native, 'run', side_effect=self.fake_run), \
                patch.object(self.endpoint.fcntl, 'flock', self.locks), \
                patch.object(self.endpoint, 'run_guarded', side_effect=guarded):
            reply = self.endpoint.execute(self.root, request)
        self.assertEqual(reply['returncode'], 0, reply)
        return json.loads(reply['stdout'])

    def test_writes_are_locked_and_guarded_reads_are_neither_and_authority_is_not_reachable(self):
        payload = {name: value for name, value in proposal().items() if name != 'operation'}
        made = self.execute(['submit', '@attachment:0'], {'0': {'flag': '--file', 'text': json.dumps(payload)}})
        self.assertEqual((made['state'], made['revision']), ('submitted', 1))
        self.assertEqual((self.locks.call_count, self.guarded), (1, ['proposal']))
        self.locks.reset_mock()
        self.guarded.clear()
        view = self.execute(['get', made['key']])
        self.assertEqual((view['state'], view['identity'], view['text']['trust']),
                         ('submitted', 'unverified', 'unreviewed'))
        self.assertEqual(self.execute(['list'])['total'], 1)
        # Over SSH `mine` is the declared query of slice 1a: it lists by the named submitter.
        mine = self.execute(['mine', '--submitter', 'person:alex'])
        self.assertEqual((mine['total'], mine['items'][0]['identity'], 'unverified_omitted' in mine),
                         (1, 'unverified', False))
        # Help is a command, never an option value (kittrial-5bb.70 review 01a10262).
        self.assertEqual(self.execute(['list', '--help'])['action'], 'proposal')
        for args, message in ((['list', '--state', '--help'], '--state must be one of'),
                              (['list', '--target', '-h'], '--target is a requirement key'),
                              (['list', '--limit', '--help'], '--limit must be a number'),
                              (['get', made['key'], '--history', '-h'], '--history must be a number'),
                              (['mine', '--submitter', '--help'], 'durable identity|submitter')):
            with self.subTest(args=args), self.assertRaisesRegex(Exception, message):
                self.execute(args)
        self.assertEqual(self.execute(['--help'])['action'], 'proposal')
        self.assertEqual((self.locks.call_count, self.guarded), (0, []))
        # A caller who names the allowlisted operator still cannot triage through the endpoint.
        writes = len(self.native.writes())
        for command in ('review', 'decide', 'settings'):
            with self.assertRaisesRegex(ValueError, 'is a host command.*admin.py proposal-%s' % command):
                self.execute([command, made['key'], '@attachment:0'],
                             {'0': {'flag': '--file', 'text': '{}'}}, actor=OPERATOR)
        self.assertEqual((len(self.native.writes()), self.locks.call_count, self.guarded), (writes, 0, []))


class HostCommandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / 'projects' / 'trial'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            json.dumps({'password': 'x', 'unit': 'none', 'operators': [COORD, OWNER]}), encoding='utf-8')
        self.native = ProposalNative()
        self.native.seed('dec-1', issue_type='decision')
        self.flock = Mock()
        for patcher in (patch.dict(os.environ, {'ORCHESTRA_OPERATORS': ''}),
                        patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=self.flock, LOCK_EX=2)})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.native.actor = 'alice'
        self.made = pr.apply_native(proposal(), 'alice', self.native, self.project, [COORD, OWNER])

    def run_native(self, root, name, args):
        if args[:1] == ['--actor']:
            self.native.actor = args[1]
            args = args[2:]
        return self.native(list(args))

    def admin(self, command, *argv, actor=COORD, payload=None):
        extra = []
        if payload is not None:
            path = self.root / 'payload.json'
            path.write_text(json.dumps(payload), encoding='utf-8')
            extra = ['--file', str(path)]
        with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), command, 'trial', '--actor', actor,
                                        *argv, *extra]), \
                patch.object(admin, 'root_path', return_value=self.root), \
                patch.object(admin, 'run_bd', side_effect=self.run_native), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            admin.main()
        return json.loads(out.getvalue())

    def test_the_http_records_scan_is_a_read_only_host_command(self):
        # kittrial-5bb.70 review 01a10262: list proposal records with HTTP-shaped authors.
        def scan(*argv):
            with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'proposal-http-records', 'trial',
                                            *argv]),                     patch.object(admin, 'root_path', return_value=self.root),                     patch.object(admin, 'run_bd', side_effect=self.run_native),                     contextlib.redirect_stdout(io.StringIO()) as out:
                admin.main()
            return json.loads(out.getvalue())
        self.native.calls = []
        self.assertEqual((scan()['total'], scan()['records']), (0, []))
        row = self.native.rows[-1]
        planted = pr.revision_record({'target': {'kind': 'requirement-new'}, 'text': 'Planted.', 'rationale': None,
                                      'evidence': [], 'attachments': []}, row['id'], 2, 'person:alex',
                                     self.made['key'], None)
        row['comments'].append({'id': 'c-planted', 'author': 'usr_0123456789abcdef',
                                'created_at': '2026-09-30T08:00:00Z', 'text': pr.revision_comment(planted)})
        found = scan()
        self.assertEqual([(item['proposal'], item['author'], item['native_created_at'], item['revision'])
                          for item in found['records']],
                         [(self.made['key'], 'usr_0123456789abcdef', '2026-09-30T08:00:00Z', 2)])
        self.assertEqual(scan('--before', '2026-09-30T00:00:00Z')['total'], 0)
        self.assertEqual(scan('--before', '2026-10-01T00:00:00Z')['total'], 1)
        with self.assertRaisesRegex(ValueError, '--before is a UTC stamp'):
            scan('--before', 'yesterday')
        self.assertEqual(scan('--after', '2026-09-30T00:00:00Z', '--before', '2026-10-01T00:00:00Z')['total'], 1)
        self.assertEqual(scan('--after', '2026-10-01T00:00:00Z')['total'], 0)
        with self.assertRaisesRegex(ValueError, '--after is a UTC stamp'):
            scan('--after', 'last week')
        with self.assertRaisesRegex(ValueError, '--after must be earlier than --before'):
            scan('--after', '2026-10-02T00:00:00Z', '--before', '2026-10-01T00:00:00Z')
        self.assertFalse([call for call in self.native.calls if call[0] in ('create', 'update', 'close')
                          or call[:2] == ['comments', 'add']])
        self.flock.assert_not_called()

    def test_an_http_id_is_never_added_to_the_operator_allowlist(self):
        # Review 01a10308: an older kit advises `operators add ACTOR` for an inert web disposition.
        def operators(*argv):
            with patch.object(sys, 'argv', ['admin.py', '--root', str(self.root), 'operators', *argv]), \
                    patch.object(admin, 'root_path', return_value=self.root), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                admin.main()
            return json.loads(out.getvalue())['operators']
        before = operators('list')
        for actor in ('usr_0123456789abcdef', 'agent_0123456789abcdef'):
            with self.subTest(actor=actor), self.assertRaisesRegex(ValueError, 'never added to the operator allowlist'):
                operators('add', actor)
        self.assertEqual(operators('list'), before)
        self.assertIn('usr_short', operators('add', 'usr_short'))             # not the reserved shape

    def payload(self, to_state, **fields):
        view = pr.read(['get', self.made['key']], self.native, COORD, [COORD, OWNER], self.project)
        self.count = getattr(self, 'count', 0) + 1
        return dict({'schema_version': 1, 'operation_id': 'host-%d' % self.count, 'key': self.made['key'],
                     'previous': view['disposition_comment_id'], 'proposal_sha256': view['sha256'],
                     'to_state': to_state}, **fields)

    def test_settings_review_decide_and_reconcile_through_admin(self):
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.admin('proposal-settings', '--map-actor', COORD, '--to', 'person:coord', actor='mallory')
        self.assertEqual(self.admin('proposal-settings')['revision'], 0)           # reading writes nothing
        self.flock.reset_mock()
        mapped = self.admin('proposal-settings', '--map-actor', COORD, '--to', 'person:coord')
        self.assertEqual((mapped['revision'], mapped['changed'], self.flock.call_count), (1, True, 1))
        self.admin('proposal-settings', '--map-actor', OWNER, '--to', 'person:owner')
        self.assertEqual(self.admin('proposal-settings', '--add-decider', 'person:owner')['deciders'],
                         ['person:owner'])
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.admin('proposal-review', actor='mallory', payload=self.payload('under-review'))
        claimed = self.admin('proposal-review', payload=self.payload('under-review'))
        self.assertEqual((claimed['state'], claimed['role']), ('under-review', 'coordinator'))
        escalated = self.admin('proposal-review', payload=self.payload('escalated-to-owner', escalation={
            'question': 'Supersede the baseline?', 'owner_identity': 'person:owner', 'due_by': None}))
        self.assertEqual(escalated['next_actor'], 'owner')
        with self.assertRaisesRegex(ValueError, 'to_state must be one of approved, rejected'):
            self.admin('proposal-decide', actor=OWNER, payload=self.payload('incorporated'))
        with self.assertRaisesRegex(ValueError, 'different person than the one who escalated'):
            self.admin('proposal-decide', payload=self.payload('approved', decision={'decision_id': 'dec-1'}))
        decided = self.admin('proposal-decide', actor=OWNER,
                             payload=self.payload('approved', decision={'decision_id': 'dec-1'}))
        self.assertEqual((decided['state'], decided['role']), ('approved', 'owner'))
        # A shell value that disagrees with the file is refused: the file is the authority.
        with patch.dict(os.environ, {'ORCHESTRA_OPERATORS': 'mallory'}), \
                self.assertRaisesRegex(ValueError, 'ORCHESTRA_OPERATORS is set in this shell'):
            self.admin('proposal-review', actor='mallory', payload=self.payload('incorporated'))
        for command, extra in (('proposal-reconcile', []), ('record-reconcile', ['--kind', 'proposal'])):
            with self.assertRaisesRegex(ValueError, 'No proposal operation receipt'):
                self.admin(command, '--operation-id', 'nope', '--reason', 'r', *extra)
            # The allowlist is checked first, strictly (review 01a10180, reconcile-unchecked):
            # an unlisted actor learns nothing about the receipt and changes nothing.
            before = {path.name: path.read_text(encoding='utf-8')
                      for path in (self.project / '.proposal-requests').glob('*.json')}
            with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
                self.admin(command, '--operation-id', 'alex-prop-1', '--reason', 'r', '--disposition', 'failed',
                           *extra, actor='mallory')
            self.assertEqual(before, {path.name: path.read_text(encoding='utf-8')
                                      for path in (self.project / '.proposal-requests').glob('*.json')})
        # `operators remove` names what a revocation does to proposals and to the settings.
        with patch.object(admin, 'run_bd', side_effect=self.run_native), \
                patch.object(admin, 'initialized_projects', return_value=['trial']):
            warning = admin.revoked_proposal_records(self.root, COORD)
        self.assertIn('proposals whose state changes: trial/%s approved -> submitted' % self.made['key'], warning)
        self.assertIn('contribution settings that change: trial: settings revision 3 -> 0', warning)
        self.assertIn('no repair command exists for proposal records yet', warning)
        receipts = {'.proposal-requests/' + path.name: json.loads(path.read_text(encoding='utf-8'))
                    for path in (self.project / '.proposal-requests').glob('*.json')}
        self.assertEqual(sorted(receipt['operation'] for receipt in receipts.values()),
                         ['decide', 'review', 'review', 'submit'])
        admin.validate_coordination_files(receipts)


if __name__ == '__main__':
    unittest.main()
