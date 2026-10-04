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
        # `self.locks` replaces fcntl.flock to count holds of the coordination lock. The
        # lookup-miss log (kittrial-5bb.98) takes its own lock file through the same
        # function, so it is switched off here; tests/test_reference_lookup.py covers it.
        import capability_misses
        quiet = patch.object(capability_misses, 'fcntl', None)
        quiet.start()
        self.addCleanup(quiet.stop)

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

    def test_reference_apply_with_items_is_a_batch_and_takes_the_lock_per_item(self):
        # kittrial-5bb.98: one decision, several reviewed drafts, one of them attested.
        self.native.seed('decision-42', issue_type='decision', status='closed')
        self.native.actor = 'alice'
        rr.apply_native(entry(operation_id='attested', key='office.server.check', authority={
            'type': 'attestation', 'basis': 'host-check', 'by': 'person:james', 'observed': '2026-09-20',
            'how': 'check script run on the office server'}), 'alice', self.native, self.project)
        single = self.accept_payload()
        items = [{'key': key, 'revision': 1,
                  'record_sha256': rr.existing_revisions(rr.anchor_for(rr.read_key_rows(self.native, key), key)[0])[1]['sha256']}
                 for key in ('calendar.trading', 'office.server.check')]
        items.append({'key': 'calendar.missing', 'revision': 1, 'record_sha256': 'f' * 64})
        batch = {'schema_version': 1, 'operation_id': 'batch-1', 'items': items, 'acceptance_state': 'accepted',
                 'acceptance': single['acceptance']}
        with self.assertRaisesRegex(ValueError, 'not a server-side configured operator'):
            self.apply(batch, actor='rogue')
        flock = sys.modules['fcntl'].flock
        flock.reset_mock()
        result = self.apply(batch)
        self.assertEqual([(item['key'], item['result']) for item in result['items']],
                         [('calendar.trading', 'accepted'), ('office.server.check', 'accepted'),
                          ('calendar.missing', 'refused')])
        self.assertIn('Unknown reference key', result['items'][2]['reason'])
        self.assertTrue(result['complete'])
        # The batch receipt, three items, the batch receipt again: five separate holds.
        self.assertEqual(flock.call_count, 5)
        for key in ('calendar.trading', 'office.server.check'):
            view = rr.get(rr.read_key_rows(self.native, key), key, [OPERATOR])
            self.assertEqual((view['state'], view['acceptance']['operator']), ('accepted', OPERATOR))
        self.assertEqual(self.apply(batch)['items'][0]['result'], 'already-accepted')

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


try:
    from test_http_review_fixes import STUB, EndpointCase, Harness
    from http_service import EndpointBackend
except Exception:  # pragma: no cover - harness import problems surface in their own module
    EndpointCase = Harness = None


def accepted_anchor(task, operator, review_by):
    """An anchor exactly as the slice-1 writer leaves it after propose and accept."""
    import datetime
    payload = dict(entry(review_by=review_by), operation='propose')
    draft = rr.entry_record(payload, 1, 'draft')
    accepted = {name: value for name, value in draft.items() if name != 'sha256'}
    accepted.update(revision=2, acceptance_state='accepted')
    accepted['sha256'] = rr.content_hash(accepted)
    bound = rr.core.bind_acceptance(acceptance(), accepted)
    _, evidence = rr.acceptance_evidence(bound, task, 2, accepted, operator, at='2026-10-01T12:00:00Z')
    comment = lambda number, text, author: {'id': str(number), 'text': text, 'author': author,
                                             'created_at': '2026-10-01T12:00:0%dZ' % number}
    return {'id': task, 'title': 'Reference calendar.trading', 'description': '', 'status': 'closed',
            'assignee': None, 'issue_type': 'task', 'dependencies': [], 'created_at': '2026-10-01T12:00:00Z',
            'labels': ['reference', 'reference:accepted', 'reference-key:calendar-trading'],
            'comments': [comment(1, rr.entry_comment(draft), 'alice'), comment(2, evidence, operator),
                         comment(3, rr.entry_comment(accepted), operator)]}


if EndpointCase is not None:
    class CountingBackend(EndpointBackend):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.actions = []

        def _endpoint(self, action, project, actor, args, *rest, **kwargs):
            self.actions.append(action)
            return super()._endpoint(action, project, actor, args, *rest, **kwargs)


@unittest.skipIf(EndpointCase is None, 'HTTP harness unavailable')
class HttpReferenceRouteTests(EndpointCase if EndpointCase else unittest.TestCase):
    """GET /references and /references/{key} at CAP_READ, over the strict canonical stub."""

    def make_backend(self):
        self.canonical_root = self.tmp / 'canonical'
        return CountingBackend(sys.executable, str(STUB), str(self.canonical_root), service=self.service)

    def seed_anchor(self):
        import datetime
        review_by = (datetime.date.today() + datetime.timedelta(days=200)).isoformat()
        path = self.canonical_root / 'canonical.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        state['rows'].append(accepted_anchor('kittrial-5bb.900', 'ops-stub', review_by))
        path.write_text(json.dumps(state), encoding='utf-8')
        (self.canonical_root / 'deployment.private.json').write_text(json.dumps({'operators': ['ops-stub']}),
                                                                     encoding='utf-8')

    def test_the_catalog_reads_and_404s(self):
        alex, project = self.setup_project()
        self.assertEqual(201, self.create_task(alex, project, 'ordinary').status)
        self.seed_anchor()
        listed = self.request('GET', '/v1/projects/%s/references' % project, token=alex)
        self.assertEqual(200, listed.status, listed.data)
        self.assertEqual([(i['key'], i['state'], i['due']) for i in listed.data['items']],
                         [('calendar.trading', 'accepted', 'ok')])
        # kittrial-5bb.98: every listed entry says what kind of authority it carries, and
        # the list filters on it.
        self.assertEqual([(i['authority_kind'], i['authority_accepted']) for i in listed.data['items']],
                         [('repository', True)])
        for kind, total in (('repository', 1), ('attested', 0)):
            filtered = self.request('GET', '/v1/projects/%s/references?authority=%s' % (project, kind), token=alex)
            self.assertEqual((200, total), (filtered.status, filtered.data['total']), filtered.data)
        self.assertIsNone(listed.data['next_cursor'])
        got = self.request('GET', '/v1/projects/%s/references/calendar.trading' % project, token=alex)
        self.assertEqual(200, got.status, got.data)
        self.assertEqual((got.data['state'], got.data['acceptance']['operator'], got.data['record']['revision']),
                         ('accepted', 'ops-stub', 2))
        missing = self.request('GET', '/v1/projects/%s/references/calendar.none' % project, token=alex)
        self.assertEqual(404, missing.status, missing.data)
        self.assertNotIn('Charts derive', json.dumps(missing.data))
        # The anchor is still hidden from the task list.
        tasks = self.request('GET', '/v1/projects/%s/tasks' % project, token=alex)
        self.assertNotIn('kittrial-5bb.900', [item['id'] for item in tasks.data['items']])

    def test_a_bad_filter_is_a_422_before_any_canonical_read(self):
        alex, project = self.setup_project()
        self.backend.actions = []
        for query in ('state=open', 'due=soon', 'owner=session-4e40fde3', 'tag=Bad%20Tag', 'limit=0',
                      'authority=attestation'):
            with self.subTest(query=query):
                bad = self.request('GET', '/v1/projects/%s/references?%s' % (project, query), token=alex)
                self.assertEqual(422, bad.status, bad.data)
        self.assertNotIn('ref', self.backend.actions)

    def test_reads_need_an_authenticated_member(self):
        alex, project = self.setup_project()
        self.assertEqual(401, self.request('GET', '/v1/projects/%s/references' % project).status)


@unittest.skipIf(Harness is None, 'HTTP harness unavailable')
class InProcessReferenceTests(Harness if Harness else unittest.TestCase):
    def test_the_in_process_backend_has_an_empty_catalog(self):
        admin_token = self.admin_token()
        self.create_account(admin_token, 'alex', 'alex-password-1')
        alex = self.login('alex', 'alex-password-1')[0]
        project = self.create_project(alex, 'Alpha')
        listed = self.request('GET', '/v1/projects/%s/references' % project, token=alex)
        self.assertEqual((listed.status, listed.data['total']), (200, 0))
        self.assertEqual(404, self.request('GET', '/v1/projects/%s/references/calendar.trading' % project,
                                           token=alex).status)


class OlderEndpointTests(unittest.TestCase):
    def test_an_endpoint_without_the_catalog_is_named(self):
        import http_service
        fake = types.SimpleNamespace(actor_namespace='http',
                                     _endpoint=lambda *a: {'returncode': 2, 'stdout': '',
                                                           'stderr': 'ValueError: Unknown action\n'})
        with self.assertRaises(http_service.HttpError) as raised:
            http_service.EndpointBackend._ref_read(fake, 'p', ['list'])
        self.assertEqual(raised.exception.status, 501)


if __name__ == '__main__':
    unittest.main()
