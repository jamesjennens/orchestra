"""Release-level lifecycle writes, the evidence-owed read and the enabled state.

These use synthetic, generic fixtures only: no project, host or actor details from
any real deployment.
"""
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lifecycle
from lifecycle import (DEFECT_VALUE, ENABLED, RELEASE, RELEASE_TARGETS_MAX, apply_native,
                       derived_id, enabled_states, evidence_owed, git_ancestry, project_facts,
                       release_targets, validate_payload, validate_release_payload)
from requirements import content_hash

ACTOR = 'alice/session'
SOURCE_A = '1' * 40
SOURCE_B = '2' * 40
MERGE_A = '3' * 40
MERGE_B = '4' * 40
RELEASE_COMMIT = '5' * 40


@contextlib.contextmanager
def scratch():
    """A disposable writable directory, or a skip when this host has none.

    A confined test sandbox can allow the workspace but deny writes in the
    platform temporary area; those tests are skipped there rather than failing
    the suite for an environment reason, and they run wherever a scratch
    directory is writable.
    """
    root = tempfile.mkdtemp()
    try:
        probe = Path(root) / '.probe'
        probe.write_text('probe', encoding='utf-8')
        probe.unlink()
    except OSError as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise unittest.SkipTest('no writable scratch directory: %s' % exc)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def scope(source=SOURCE_A, integration=MERGE_A, release='release-a', environment='staging'):
    return {'source_commit': source, 'integration_commit': integration,
            'release_id': release, 'environment': environment}


def payload(task, dimension, value='passed', operation='operation-1', scope_value=None, **changes):
    result = dict(schema_version=1, operation_id=operation, task=task, dimension=dimension,
                  value=value, scope=scope_value or scope(), evidence=['commit:' + SOURCE_A],
                  provenance='performed', actor=ACTOR)
    result.update(changes)
    if dimension == 'lifecycle-scope':
        result['value'] = content_hash(result['scope'])
    return result


def target(task, source=SOURCE_A, integration=MERGE_A, **changes):
    result = {'task': task, 'source_commit': source, 'integration_commit': integration}
    result.update(changes)
    return result


def release_payload(targets, operation='release-1', release='release-1', environment='production',
                    live_verified=False, **changes):
    result = dict(schema_version=1, operation_id=operation, dimension=RELEASE, value='passed',
                  scope={'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                         'release_id': release, 'environment': environment},
                  evidence=['release:build-17'], provenance='performed', actor=ACTOR,
                  live_verified=live_verified, targets=targets)
    result.update(changes)
    return result


class NativeStore:
    """Many tasks; simulates export, native label/event writes, and lost responses."""
    def __init__(self, tasks=('trial-a', 'trial-b', 'trial-c')):
        self.rows = [dict(_type='issue', id=task, title='Synthetic ' + task, issue_type='task',
                          status='open', labels=[], assignee=ACTOR) for task in tasks]
        self.calls = []
        self.orders = {}

    def run(self, args):
        self.calls.append(args[:])
        if args == ['export', '--all']:
            return ''.join(json.dumps(row) + '\n' for row in self.rows)
        assert args[0] == 'set-state', args
        task = args[1]
        dimension, value = args[2].split('=', 1)
        row = next(item for item in self.rows if item['id'] == task)
        labels = row['labels']
        if dimension + ':' + value in labels:
            return json.dumps({'changed': False})
        old = next((x.split(':', 1)[1] for x in labels if x.startswith(dimension + ':')), None)
        row['labels'] = ([x for x in labels if not x.startswith(dimension + ':')]
                         + [dimension + ':' + value])
        self.orders[task] = self.orders.get(task, 0) + 1
        event_id = task + '.' + str(self.orders[task])
        reason = args[args.index('--reason') + 1]
        description = ('Set ' + dimension + ' to ' + value if old is None else
                       'Changed ' + dimension + ' from ' + old + ' to ' + value)
        self.rows.append(dict(_type='issue', id=event_id, issue_type='event',
                              title='State change: ' + dimension + ' â†’ ' + value,
                              description=description + '\n\nReason: ' + reason,
                              status='closed', created_by=ACTOR,
                              created_at='2026-10-01T10:00:00Z',
                              dependencies=[dict(issue_id=event_id, depends_on_id=task,
                                                 type='parent-child')]))
        return json.dumps(dict(changed=True, dimension=dimension, event_id=event_id, new_value=value))

    def record(self, p):
        return apply_native(p, ACTOR, self.run)

    def seed(self, task, integration=MERGE_A, source=SOURCE_A):
        seeded = scope(source=source, integration=integration)
        self.record(payload(task, 'lifecycle-scope', operation='scope-' + task, scope_value=seeded))
        self.record(payload(task, 'integrated', operation='integrated-' + task, scope_value=seeded))
        return self

    def facts(self, task):
        return next(state for state in project_facts(self.rows) if state['id'] == task)


class ReleaseWriteTests(unittest.TestCase):
    def test_release_records_deployed_and_live_verified_for_every_target(self):
        store = NativeStore().seed('trial-a').seed('trial-b', integration=MERGE_B, source=SOURCE_B)
        result = store.record(release_payload([target('trial-a'),
                                               target('trial-b', source=SOURCE_B, integration=MERGE_B)],
                                              live_verified=True))
        self.assertEqual([item['task'] for item in result['targets']], ['trial-a', 'trial-b'])
        for task, source, merge in (('trial-a', SOURCE_A, MERGE_A),
                                    ('trial-b', SOURCE_B, MERGE_B)):
            facts = store.facts(task)
            self.assertEqual(facts['facts']['deployed']['value'], 'passed')
            self.assertEqual(facts['facts']['live-verified']['value'], 'passed')
            self.assertEqual(facts['facts']['deployed']['evidence'], ['release:build-17'])
            self.assertEqual(facts['scope'], {'source_commit': source, 'integration_commit': merge,
                                              'release_id': 'release-1', 'environment': 'production'})

    def test_release_carries_one_evidence_block_and_per_task_notes(self):
        store = (NativeStore().seed('trial-a')
                 .seed('trial-b', integration=MERGE_B, source=SOURCE_B))
        store.record(release_payload([target('trial-a', note='first task note'),
                                      target('trial-b', source=SOURCE_B, integration=MERGE_B)]))
        events = [row for row in store.rows if row.get('issue_type') == 'event']
        payloads = [lifecycle.native_event(row)['payload'] for row in events]
        deployed = [p for p in payloads if p and p['dimension'] == 'deployed']
        self.assertEqual({p['task']: p['evidence'] for p in deployed},
                         {'trial-a': ['release:build-17'], 'trial-b': ['release:build-17']})
        self.assertEqual({p['task']: p.get('note') for p in deployed},
                         {'trial-a': 'first task note', 'trial-b': None})

    def test_release_is_idempotent_on_exact_retry(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        release = release_payload([target('trial-a')], live_verified=True)
        store.record(release)
        count = len(store.rows)
        repeated = store.record(release)
        self.assertTrue(all(item['reconciled'] for item in repeated['targets']))
        self.assertEqual(len(store.rows), count)
        self.assertEqual(store.facts('trial-a')['facts']['deployed']['value'], 'passed')

    def test_release_labels_do_not_grow_and_scope_is_recorded_once(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        release = release_payload([target('trial-a')])
        store.record(release)
        store.record(release)
        labels = store.rows[0]['labels']
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual([x for x in labels if x.startswith('deployed:')], ['deployed:passed'])
        self.assertEqual([x for x in labels if x.startswith('lifecycle-scope:')],
                         ['lifecycle-scope:' + content_hash(
                             scope(release='release-1', environment='production'))])

    def test_release_refuses_the_whole_write_when_a_target_is_stale(self):
        store = NativeStore(tasks=('trial-a', 'trial-b')).seed('trial-a')
        before = json.dumps(store.rows)
        seeded = len(store.calls)
        with self.assertRaisesRegex(ValueError, 'not integrated at that commit'):
            store.record(release_payload([target('trial-a'),
                                          target('trial-b', source=SOURCE_A, integration=MERGE_A)]))
        self.assertEqual(json.dumps(store.rows), before)
        self.assertEqual(store.calls[seeded:], [['export', '--all']])

    def test_release_refuses_unknown_duplicate_and_unbounded_targets(self):
        store = NativeStore().seed('trial-a')
        with self.assertRaisesRegex(ValueError, 'unknown release target'):
            store.record(release_payload([target('missing-task')]))
        for targets, message in (([], 'at least one integrated target'),
                                 ([target('trial-a'), target('trial-a')], 'duplicate release target'),
                                 ([target('trial-a')] * (RELEASE_TARGETS_MAX + 1), 'at most')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate_release_payload(release_payload(targets))

    def test_release_payload_requires_shared_evidence_and_release_identity(self):
        base = release_payload([target('trial-a')])
        for changes, message in ((dict(evidence=[]), 'asserted facts need evidence'),
                                 (dict(live_verified='yes'), 'live_verified must be true or false'),
                                 (dict(value='pending'), 'invalid lifecycle dimension/value'),
                                 (dict(scope=scope(source='', integration='merge-a')),
                                  'full release commit'),
                                 (dict(scope=scope(source=SOURCE_A)),
                                  'no single source commit'),
                                 (dict(scope=scope(source='', release='',
                                                   environment='production')),
                                  'release_id and environment'),
                                 (dict(targets=[target('trial-a', integration='merge-a')]),
                                  'full lowercase integration_commit')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate_release_payload(dict(base, **changes))

    def test_release_request_actor_must_match_the_payload_actor(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        with self.assertRaisesRegex(ValueError, 'actor'):
            apply_native(release_payload([target('trial-a')]), 'someone-else', store.run)

    def test_long_operation_ids_still_produce_bounded_unique_target_ids(self):
        self.assertEqual(derived_id('op', 'deployed', 'trial-a'), 'op/deployed/trial-a')
        long_operation = 'o' * 110
        first = derived_id(long_operation, 'deployed', 'trial-a' * 20)
        second = derived_id(long_operation, 'deployed', 'trial-b' * 20)
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(first), 161)


class ReleaseSelectionTests(unittest.TestCase):
    def test_selection_keeps_ancestors_and_reports_the_rest_as_skipped(self):
        store = (NativeStore().seed('trial-a')
                 .seed('trial-b', integration=MERGE_B, source=SOURCE_B)
                 .seed('trial-c', integration='merge-c'))
        targets, skipped = release_targets(
            store.rows, {'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                         'release_id': 'release-1', 'environment': 'production'},
            lambda commit, release: commit == MERGE_A)
        self.assertEqual(targets, [{'task': 'trial-a', 'source_commit': SOURCE_A,
                                    'integration_commit': MERGE_A}])
        self.assertEqual([item['task'] for item in skipped], ['trial-b', 'trial-c'])

    def test_selection_reports_undecidable_ancestry_instead_of_guessing(self):
        store = NativeStore().seed('trial-a')
        def undecidable(commit, release):
            raise ValueError('cannot decide Git ancestry: synthetic failure')
        targets, skipped = release_targets(
            store.rows, {'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                         'release_id': 'release-1', 'environment': 'production'}, undecidable)
        self.assertEqual(targets, [])
        self.assertEqual(skipped[0]['reason'], 'cannot decide Git ancestry: synthetic failure')

    def test_selection_ignores_tasks_with_no_passing_integration(self):
        store = NativeStore(tasks=('trial-a', 'trial-b'))
        store.record(payload('trial-a', 'lifecycle-scope', operation='scope-a'))
        store.record(payload('trial-a', 'integrated', value='failed', operation='integrated-a',
                             evidence=['test:failed']))
        targets, skipped = release_targets(
            store.rows, {'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                         'release_id': 'release-1', 'environment': 'production'},
            lambda commit, release: True)
        self.assertEqual((targets, skipped), ([], []))


@unittest.skipUnless(shutil.which('git'), 'git is not installed')
class GitAncestryTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        if subprocess.run(['git', '-C', self.root, 'init', '-q'],
                          capture_output=True).returncode:
            shutil.rmtree(self.root, ignore_errors=True)
            self.skipTest('git cannot create a repository in this environment')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def git(self, *args):
        done = subprocess.run(['git', '-C', self.root, *args], capture_output=True, text=True)
        if done.returncode:
            raise AssertionError(done.stderr)
        return done.stdout.strip()

    def test_git_ancestry_decides_both_answers(self):
        git = self.git
        git('config', 'user.email', 'synthetic@example.invalid')
        git('config', 'user.name', 'Synthetic Test')
        (Path(self.root) / 'file.txt').write_text('one\n', encoding='utf-8')
        git('add', 'file.txt')
        git('commit', '-q', '-m', 'first')
        first = git('rev-parse', 'HEAD')
        (Path(self.root) / 'file.txt').write_text('two\n', encoding='utf-8')
        git('add', 'file.txt')
        git('commit', '-q', '-m', 'second')
        second = git('rev-parse', 'HEAD')
        is_ancestor = git_ancestry(self.root)
        self.assertTrue(is_ancestor(first, second))
        self.assertFalse(is_ancestor(second, first))

    def test_git_ancestry_refuses_an_undecidable_commit(self):
        missing = str(Path(__file__).resolve().parents[1] / 'no-such-checkout-for-tests')
        with self.assertRaisesRegex(ValueError, 'cannot decide Git ancestry'):
            git_ancestry(missing)('a' * 40, 'b' * 40)


class EnabledStateTests(unittest.TestCase):
    def test_enabled_with_known_defect_points_at_the_fixing_task(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.record(payload('trial-a', ENABLED, value=DEFECT_VALUE, operation='enabled-1',
                             defect_task='fixing-task'))
        state = enabled_states(store.rows)[0]
        self.assertEqual(state['value'], DEFECT_VALUE)
        self.assertEqual(state['defect_task'], 'fixing-task')
        self.assertEqual(state['scope'], scope())
        self.assertNotIn(ENABLED, store.facts('trial-a')['facts'])

    def test_enabled_state_shares_the_fact_trust_rule(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.record(payload('trial-a', ENABLED, value='enabled', operation='enabled-1'))
        self.assertEqual(enabled_states(store.rows)[0]['value'], 'enabled')
        store.rows[0]['labels'] = [x for x in store.rows[0]['labels']
                                   if not x.startswith(ENABLED + ':')] + [ENABLED + ':disabled']
        tampered = enabled_states(store.rows)[0]
        self.assertEqual(tampered['value'], 'unknown')
        self.assertIsNone(tampered['defect_task'])
        store.rows[0]['labels'] = [x for x in store.rows[0]['labels']
                                   if not x.startswith(ENABLED + ':')] + [ENABLED + ':enabled']
        store.record(payload('trial-a', 'lifecycle-scope', operation='scope-2',
                             scope_value=scope(source=SOURCE_B, integration=MERGE_B)))
        self.assertEqual(enabled_states(store.rows)[0]['value'], 'unknown')

    def test_enabled_payload_validation(self):
        validate_payload(payload('trial-a', ENABLED, value=DEFECT_VALUE, defect_task='fixing-task'))
        for changes, message in ((dict(value=DEFECT_VALUE), 'needs the fixing task'),
                                 (dict(value='enabled', defect_task='fixing-task'),
                                  'defect_task belongs'),
                                 (dict(value=DEFECT_VALUE, defect_task='has space'),
                                  'invalid defect_task'),
                                 (dict(value='works'), 'invalid lifecycle dimension/value'),
                                 (dict(value=DEFECT_VALUE, defect_task='fixing-task', evidence=[]),
                                  'asserted facts need evidence')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate_payload(payload('trial-a', ENABLED, **changes))
        with self.assertRaisesRegex(ValueError, 'defect_task belongs'):
            validate_payload(payload('trial-a', 'deployed', defect_task='fixing-task'))


class EvidenceOwedTests(unittest.TestCase):
    def seeded(self):
        store = NativeStore().seed('trial-a').seed('trial-b', integration=MERGE_B, source=SOURCE_B)
        production = scope(release='release-1', environment='production')
        store.record(payload('trial-a', 'lifecycle-scope', operation='deploy-scope-a',
                             scope_value=production))
        store.record(payload('trial-a', 'deployed', operation='deploy-a', scope_value=production))
        store.record(payload('trial-a', 'live-verified', value='pending', operation='verify-a',
                             scope_value=production, evidence=['plan:pending'],
                             trigger='after the nightly job'))
        staging = scope(source=SOURCE_B, integration=MERGE_B, release='release-2',
                        environment='staging')
        store.record(payload('trial-b', 'lifecycle-scope', operation='deploy-scope-b',
                             scope_value=staging))
        store.record(payload('trial-b', 'deployed', operation='deploy-b', scope_value=staging))
        store.record(payload('trial-b', 'live-verified', operation='verify-b', scope_value=staging))
        store.record(payload('trial-b', ENABLED, value=DEFECT_VALUE, operation='enabled-b',
                             scope_value=staging, defect_task='fixing-task'))
        return store

    def test_evidence_owed_groups_by_environment_and_release(self):
        rows = evidence_owed(self.seeded().rows)
        self.assertEqual([(group['environment'], group['release_id']) for group in rows],
                         [('production', 'release-1'), ('staging', 'release-2')])
        pending = rows[0]['tasks'][0]
        self.assertEqual(pending['task'], 'trial-a')
        self.assertEqual(pending['deployed'], 'passed')
        self.assertEqual(pending['enabled'], 'unknown')
        self.assertEqual(pending['remaining_evidence'], ['live-verified'])
        self.assertEqual(pending['responsible'], ACTOR)
        self.assertEqual(pending['next_trigger'], 'after the nightly job')
        verified = rows[1]['tasks'][0]
        self.assertEqual(verified['deployed'], 'passed')
        self.assertEqual(verified['enabled'], DEFECT_VALUE)
        self.assertEqual(verified['defect_task'], 'fixing-task')
        self.assertEqual(verified['remaining_evidence'], [])
        self.assertIsNone(verified['next_trigger'])

    def test_evidence_owed_ignores_undeployed_and_untrusted_tasks(self):
        store = NativeStore()
        store.seed('trial-a')
        production = scope(release='release-1', environment='production')
        store.record(payload('trial-a', 'lifecycle-scope', operation='deploy-scope-a',
                             scope_value=production))
        store.record(payload('trial-a', 'deployed', value='pending', operation='deploy-a',
                             scope_value=production, evidence=['rollout:pending']))
        rows = evidence_owed(store.rows)
        self.assertEqual([item['task'] for item in rows[0]['tasks']], ['trial-a'])
        self.assertEqual(rows[0]['tasks'][0]['remaining_evidence'], ['deployed', 'live-verified'])
        store.rows[0]['labels'] = [x for x in store.rows[0]['labels']
                                   if not x.startswith('deployed:')] + ['deployed:failed']
        self.assertEqual(evidence_owed(store.rows), [])


class ReleaseCommandTests(unittest.TestCase):
    """The release command resolves targets locally, then sends one payload."""
    def run_cli(self, argv, client=None):
        previous = sys.modules.get('client')
        if client is not None:
            sys.modules['client'] = client
        original_argv, original_ancestry = sys.argv, lifecycle.git_ancestry
        lifecycle.git_ancestry = lambda repo: (lambda commit, release: commit == MERGE_A)
        sys.argv = argv
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                lifecycle.main()
        finally:
            sys.argv, lifecycle.git_ancestry = original_argv, original_ancestry
            if client is not None:
                if previous is None:
                    sys.modules.pop('client', None)
                else:
                    sys.modules['client'] = previous
        return out.getvalue()

    def fixture(self, root, store, live_verified=False):
        export = Path(root) / 'issues.jsonl'
        export.write_text(''.join(json.dumps(row) + '\n' for row in store.rows), encoding='utf-8')
        request = Path(root) / 'release.json'
        request.write_text(json.dumps(release_payload([], live_verified=live_verified)),
                           encoding='utf-8')
        config = Path(root) / 'client.json'
        config.write_text('{}', encoding='utf-8')
        return ['lifecycle.py', 'release', '--config', str(config), '--project', 'example',
                '--actor', ACTOR, '--file', str(request), '--export', str(export), '--repo', str(root)]

    def test_dry_run_prints_the_selection_and_never_contacts_the_endpoint(self):
        store = NativeStore().seed('trial-a').seed('trial-b', integration=MERGE_B, source=SOURCE_B)
        calls = []
        client = types.ModuleType('client')
        client.request = lambda *args, **kwargs: calls.append(args) or {
            'returncode': 0, 'stdout': '{}', 'stderr': ''}
        with scratch() as root:
            argv = self.fixture(root, store) + ['--dry-run']
            report = json.loads(self.run_cli(argv, client))
            self.assertTrue(report['dry_run'])
            self.assertEqual([item['task'] for item in report['targets']], ['trial-a'])
            self.assertEqual([item['task'] for item in report['skipped']], ['trial-b'])
            self.assertNotIn('result', report)
            self.assertEqual(calls, [])

    def test_release_sends_one_payload_with_the_resolved_targets(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        calls = []
        client = types.ModuleType('client')

        def fake_request(config, project, actor, args, action=None):
            calls.append({'project': project, 'actor': actor, 'args': args, 'action': action})
            return {'returncode': 0, 'stderr': '',
                    'stdout': json.dumps({'operation_id': 'release-1',
                                          'targets': [{'task': 'trial-a'}]})}

        client.request = fake_request
        with scratch() as root:
            argv = self.fixture(root, store) + ['--live-verified']
            report = json.loads(self.run_cli(argv, client))
            self.assertEqual(len(calls), 1)
            self.assertEqual((calls[0]['project'], calls[0]['actor'], calls[0]['action']),
                             ('example', ACTOR, 'lifecycle'))
            sent = json.loads(calls[0]['args'][0])
            self.assertEqual(sent['dimension'], RELEASE)
            self.assertTrue(sent['live_verified'])
            self.assertEqual(sent['targets'], [{'task': 'trial-a', 'source_commit': SOURCE_A,
                                                'integration_commit': MERGE_A}])
            self.assertEqual(report['result']['targets'], [{'task': 'trial-a'}])

    def test_evidence_owed_command_prints_the_grouped_read(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        production = scope(release='release-1', environment='production')
        store.record(payload('trial-a', 'lifecycle-scope', operation='deploy-scope',
                             scope_value=production))
        store.record(payload('trial-a', 'deployed', operation='deploy', scope_value=production))
        with scratch() as root:
            export = Path(root) / 'issues.jsonl'
            export.write_text(''.join(json.dumps(row) + '\n' for row in store.rows),
                              encoding='utf-8')
            rows = json.loads(self.run_cli(['lifecycle.py', 'evidence-owed',
                                            '--export', str(export)]))
            self.assertEqual(rows[0]['environment'], 'production')
            self.assertEqual(rows[0]['release_id'], 'release-1')
            self.assertEqual(rows[0]['tasks'][0]['task'], 'trial-a')
            self.assertEqual(rows[0]['tasks'][0]['deployed'], 'passed')


class AdditivePayloadTests(unittest.TestCase):
    OLD_FIELDS = {'schema_version', 'operation_id', 'task', 'dimension', 'value', 'scope',
                  'evidence', 'provenance', 'actor'}

    def old_validate(self, p):
        if not isinstance(p, dict) or set(p) != self.OLD_FIELDS:
            raise ValueError('invalid lifecycle payload')

    def test_notes_and_triggers_are_additive_and_unknown_fields_are_refused(self):
        base = payload('trial-a', 'implemented')
        validate_payload(base)
        validate_payload(dict(base, note='kept for the release note'))
        validate_payload(dict(base, value='pending', trigger='after the nightly job'))
        for changes, message in ((dict(trigger='after the nightly job'), 'trigger'),
                                 (dict(note='x' * 600), 'note'),
                                 (dict(unexpected='x'), 'invalid lifecycle payload')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate_payload(dict(base, **changes))

    def test_an_older_kit_reads_new_payloads_as_unknown_not_passed(self):
        store = NativeStore().seed('trial-a').seed('trial-b')
        store.record(payload('trial-a', 'implemented', operation='plain'))
        store.record(payload('trial-b', 'implemented', operation='annotated',
                             note='recorded with a release note'))
        self.assertEqual(store.facts('trial-a')['facts']['implemented']['value'], 'passed')
        original = lifecycle.validate_payload
        lifecycle.validate_payload = self.old_validate
        try:
            self.assertEqual(store.facts('trial-a')['facts']['implemented']['value'], 'passed')
            self.assertEqual(store.facts('trial-b')['facts']['implemented']['value'], 'unknown')
        finally:
            lifecycle.validate_payload = original

    def test_unknown_payload_fields_never_become_evidence(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.record(payload('trial-a', 'implemented', operation='plain'))
        store.rows[-1]['description'] += 'x'
        self.assertEqual(store.facts('trial-a')['facts']['implemented']['value'], 'unknown')


if __name__ == '__main__':
    unittest.main()
