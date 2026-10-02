"""The capability client split, endpoint dispatch and operator commands (kittrial-5bb.67/.69).

client.py: `capability lookup|resolve|index` stay local and need no config (the .61
behaviour, pinned by tests/test_capabilities.py); `capability find|get|list|propose|
revise|propose-alias` go to the endpoint; `capability lookup` with --config/--project
adds the endpoint's records with live pointer resolution. The endpoint's capability
reads take no lock and no journal; its writes are locked and run_guarded.
`capability check` (slice 1b) runs in the client: it pages the records from the
endpoint, resolves their pointers in the caller's checkout, and writes nothing
without `--record`.
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


class ClientCase(unittest.TestCase):
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


class ClientSplitTests(ClientCase):
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


def list_item(key, state='accepted', code=('review_workflow.py::execute',), tests=(), anchors=(), revision=1):
    return {'key': key, 'name': key, 'state': state, 'trust': 'accepted' if state == 'accepted' else 'draft',
            'owner': 'person:james', 'tags': [], 'revision': revision, 'native_id': 'cap-' + key,
            'aliases_pending': 0, 'acceptance_inert': False, 'verification': 'unverified',
            'record_sha256': 'c' * 64, 'code': list(code), 'tests': list(tests), 'anchors': list(anchors)}


class ClientCheckTests(ClientCase):
    """`capability check --repo PATH [--key KEY]... [--record | --payloads FILE]` (.60 section 5.1)."""

    ITEMS = [list_item('review.flow', code=['review_workflow.py::execute'], anchors=['README.md#usage']),
             list_item('review.gone', state='draft-only', code=['review_workflow.py::missing']),
             list_item('other.lang', code=['src/thing.rs::run']),
             list_item('old.retired', state='superseded')]

    def setUp(self):
        super().setUp()
        (self.repo / 'README.md').write_text('# Usage\n\nText.\n', encoding='utf-8')
        # A pointer into a language the local resolver cannot parse reads `unknown` (no graph here).
        (self.repo / 'src').mkdir()
        (self.repo / 'src' / 'thing.rs').write_text('fn run() {}\n', encoding='utf-8')
        self.verified = []

    def endpoint(self, items=None, refuse=()):
        items = self.ITEMS if items is None else items

        def reply(action, args):
            if args[0] == 'list':
                offset = int(args[args.index('--offset') + 1])
                page = items[offset:offset + 2]          # two per page: the client must follow next_offset
                return {'returncode': 0, 'stderr': '', 'stdout': json.dumps(
                    {'items': page, 'total': len(items),
                     'next_offset': offset + 2 if offset + 2 < len(items) else None})}
            payload = json.loads(Path(args[args.index('--file') + 1]).read_text(encoding='utf-8'))
            self.verified.append(payload)
            if payload['key'] in refuse:
                return {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: the pool is full\n'}
            return {'returncode': 0, 'stderr': '', 'stdout': json.dumps(
                {'key': payload['key'], 'reconciled': False, 'identity': 'unverified'})}
        return reply

    def git(self, *argv):
        done = subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                               '-c', 'commit.gpgsign=false', *argv], capture_output=True, text=True)
        if done.returncode:
            self.skipTest('git is not usable here: ' + done.stderr[-200:])
        return done.stdout.strip()

    def commit(self):
        try:
            self.git('init', '-q')
        except OSError as error:
            self.skipTest('git is not installed: ' + str(error))
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def check(self, *argv, **options):
        return self.main('capability', 'check', '--repo', str(self.repo), *argv,
                         request=options.pop('request', None) or self.endpoint(**options))

    def test_check_needs_a_config_and_help_does_not(self):
        code, _, err, calls = self.main('capability', 'check', '--repo', str(self.repo), config=False)
        self.assertEqual((code, calls), (2, []))
        self.assertIn('--config', err)
        code, out, _, calls = self.main('capability', 'check', '--help', config=False)
        self.assertEqual((code, calls, json.loads(out)['output']['schema']), (0, [], 'capability-check-v1'))

    def test_check_resolves_every_pointer_and_writes_nothing(self):
        code, out, _, calls = self.check()
        self.assertEqual(code, 0)                         # a missing pointer is a result, not an error
        self.assertEqual([args[0] for _, args in calls], ['list', 'list'])
        self.assertTrue(all('--pointers' in args for _, args in calls))
        payload = json.loads(out)
        self.assertEqual((payload['schema'], payload['contract'], payload['recording']),
                         ('capability-check-v1', 'cli-contract-v1', None))
        rows = {row['key']: row for row in payload['capabilities']}
        self.assertEqual(sorted(rows), ['other.lang', 'review.flow', 'review.gone'])   # the retired key is skipped
        self.assertEqual([(r['pointer'], r['resolved'], r['basis'], r['reason']) for r in rows['review.flow']['results']],
                         [('review_workflow.py::execute', True, 'ast', None),
                          ('README.md#usage', True, 'markdown', None)])
        self.assertEqual((rows['review.flow']['passed'], rows['review.gone']['passed'], rows['other.lang']['passed']),
                         (True, False, None))
        self.assertEqual(rows['review.gone']['results'][0]['reason'], 'symbol-missing')
        self.assertEqual(rows['other.lang']['results'][0]['reason'], 'unsupported-file-type')
        self.assertEqual(payload['summary'], {'capabilities': 3, 'passed': 1, 'failed': 1, 'unknown': 1,
                                              'pointers': {'resolved': 2, 'missing': 1, 'unknown': 1}})
        self.assertNotIn('recorded', rows['review.flow'])
        self.assertEqual(self.verified, [])

    def test_key_filters_and_refusals(self):
        code, out, _, _ = self.check('--key', 'review.flow')
        self.assertEqual([row['key'] for row in json.loads(out)['capabilities']], ['review.flow'])
        code, out, err, _ = self.check('--key', 'no.such')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('unknown capability key', err)
        code, _, err, _ = self.check('--record', '--payloads', 'x.json')
        self.assertEqual(code, 2)
        self.assertIn('not both', err)
        stale = [{name: value for name, value in item.items() if name != 'record_sha256'} for item in self.ITEMS]
        code, _, err, _ = self.check(items=stale)
        self.assertEqual(code, 2)
        self.assertIn('does not support capability check', err)

        def refuse(action, args):
            return {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: unknown command list\n'}
        code, _, err, _ = self.check(request=refuse)
        self.assertEqual(code, 2)
        self.assertIn('capability records unavailable', err)

    def test_record_refuses_a_checkout_that_is_not_a_clean_full_commit(self):
        code, _, err, calls = self.check('--record')
        self.assertEqual((code, calls), (2, []))
        self.assertIn('git checkout at a full commit', err)
        self.commit()
        (self.repo / 'review_workflow.py').write_text('def execute():\n    return 2\n', encoding='utf-8')
        code, _, err, calls = self.check('--record')
        self.assertEqual((code, calls), (2, []))
        self.assertIn('clean checkout', err)
        self.git('checkout', '-q', '--', 'review_workflow.py')
        (self.repo / 'scratch.py').write_text('def missing():\n    return 1\n', encoding='utf-8')
        code, _, err, calls = self.check('--payloads', str(self.root / 'p.json'))
        self.assertEqual((code, calls), (2, []))          # an untracked file could make a pointer resolve
        self.assertIn('untracked', err)
        self.assertEqual(self.verified, [])

    def test_record_posts_one_verification_per_capability_at_head(self):
        head = self.commit()
        code, out, _, calls = self.check('--record', refuse=('review.gone',))
        self.assertEqual(code, 0)
        self.assertEqual([args[0] for _, args in calls], ['list', 'list', 'verify', 'verify'])
        payload = json.loads(out)
        rows = {row['key']: row for row in payload['capabilities']}
        self.assertEqual((rows['review.flow']['recorded'], rows['review.flow']['identity']),
                         ('recorded', 'unverified'))
        self.assertEqual((rows['review.gone']['recorded'], rows['review.gone']['refusal']),
                         ('refused', 'ValueError: the pool is full'))
        self.assertEqual(rows['other.lang']['recorded'], 'not-recordable')
        self.assertEqual(payload['summary']['recorded'], {'not-recordable': 1, 'recorded': 1, 'refused': 1})
        sent = {item['key']: item for item in self.verified}
        self.assertEqual(sorted(sent), ['review.flow', 'review.gone'])
        flow = sent['review.flow']
        self.assertEqual(set(flow), {'schema_version', 'key', 'revision', 'record_sha256', 'commit', 'checked_at',
                                     'source', 'graph_built_at_commit', 'tool', 'results', 'passed'})
        self.assertEqual((flow['commit'], flow['passed'], flow['source'], flow['record_sha256']),
                         (head, True, 'ast', 'c' * 64))
        self.assertEqual(flow['results'], [{'pointer': 'review_workflow.py::execute', 'resolved': True, 'reason': None},
                                           {'pointer': 'README.md#usage', 'resolved': True, 'reason': None}])
        self.assertEqual((sent['review.gone']['passed'], sent['review.gone']['results'][0]['reason']),
                         (False, 'symbol-missing'))
        # The payload the client builds is exactly what the endpoint's validator accepts.
        import capability_verification
        capability_verification.validate_payload(dict(flow, key='review.flow'))

    def test_payloads_writes_a_file_for_the_host_command_and_posts_nothing(self):
        self.commit()
        target = self.root / 'payloads.json'
        code, out, _, calls = self.check('--payloads', str(target))
        self.assertEqual(code, 0)
        self.assertEqual([args[0] for _, args in calls], ['list', 'list'])
        written = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual((written['schema_version'], [item['key'] for item in written['items']]),
                         (1, ['review.flow', 'review.gone']))
        import capability_verification
        self.assertEqual(len(capability_verification.validate_batch(written)), 2)
        self.assertEqual(json.loads(out)['recording'], 'payloads')

    def test_the_payloads_file_is_private_and_replaced_whole(self):
        """Review 01a0fe9e `smaller` (a)."""
        self.commit()
        target = self.root / 'payloads.json'
        target.write_text('an older file\n', encoding='utf-8')
        code, _, _, _ = self.check('--payloads', str(target))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['schema_version'], 1)
        self.assertEqual([path.name for path in self.root.glob('.payloads.json.*')], [])   # no temporary left
        if os.name == 'posix':
            import stat
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        code, _, err, calls = self.check('--payloads', str(self.repo))
        self.assertEqual((code, calls), (2, []))
        self.assertIn('symbolic link or onto a directory', err)

    def test_the_payloads_file_is_never_written_through_a_symbolic_link(self):
        self.commit()
        victim = self.root / 'victim.txt'
        victim.write_text('keep me\n', encoding='utf-8')
        link = self.root / 'link.json'
        try:
            os.symlink(victim, link)
        except (OSError, NotImplementedError):
            self.skipTest('cannot create symlinks here')
        code, _, err, calls = self.check('--payloads', str(link))
        self.assertEqual((code, calls), (2, []))          # refused before any endpoint call
        self.assertIn('symbolic link', err)
        self.assertEqual(victim.read_text(encoding='utf-8'), 'keep me\n')
        self.assertTrue(link.is_symlink())

    def test_a_transport_failure_stops_recording_and_says_how_to_resume(self):
        self.commit()
        listing = self.endpoint()

        def flaky(action, args):
            if args[0] == 'verify':
                raise RuntimeError('SSH failed (255); outcome may be uncertain.')
            return listing(action, args)
        code, out, err, _ = self.check('--record', request=flaky)
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual([row.get('recorded') for row in payload['capabilities']],
                         ['uncertain', 'not-run', 'not-recordable'])
        self.assertIn('re-run the same command', err)


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
        flocked = []
        self.locks.side_effect = lambda handle, flags: flocked.append(
            os.fstat(handle) if isinstance(handle, int) else None)
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
        if telemetry:   # the descriptor flocked is the miss-log lock file itself
            self.assertTrue(os.path.samestat(flocked[-1], os.stat(self.project / '.capability-misses.lock')))

    def test_verify_is_a_locked_guarded_write_that_is_never_verified(self):
        payload = {k: v for k, v in entry().items() if k != 'operation'}
        self.execute(['propose', '@attachment:0'], {'0': {'flag': '--file', 'text': json.dumps(payload)}})
        item = self.execute(['list', '--pointers'])['items'][0]
        check = {'schema_version': 1, 'key': item['key'], 'revision': item['revision'],
                 'record_sha256': item['record_sha256'], 'commit': '1' * 40, 'checked_at': '2026-10-01T12:00:00Z',
                 'source': 'ast', 'graph_built_at_commit': None,
                 'tool': {'name': 'orchestra-capability-check', 'version': '0.1.0'},
                 'results': [{'pointer': pointer, 'resolved': True, 'reason': None}
                             for pointer in item['code'] + item['tests'] + item['anchors']], 'passed': True}
        self.locks.reset_mock()
        self.guarded.clear()
        # The caller names the allowlisted operator as its actor: still an unverified report.
        done = self.execute(['verify', '@attachment:0'], {'0': {'flag': '--file', 'text': json.dumps(check)}},
                            actor=OPERATOR)
        self.assertEqual((done['identity'], done['passed'], done['reconciled']), ('unverified', True, False))
        self.assertEqual((self.locks.call_count, self.guarded), (1, ['capability']))
        self.locks.reset_mock()
        self.guarded.clear()
        view = self.execute(['get', item['key']])
        self.assertEqual((view['verification']['state'], view['verification']['verified_at']), ('reported', None))
        self.assertEqual(self.execute(['list'])['items'][0]['verification'], 'reported')
        self.assertEqual((self.locks.call_count, self.guarded), (0, []))


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
