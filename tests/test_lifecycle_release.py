"""Release-level lifecycle writes, the evidence-owed read and the enabled state.

These use synthetic, generic fixtures only: no project, host or actor details from
any real deployment.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lifecycle
import review_workflow as rw
from lifecycle import (DEFECT_VALUE, ENABLED, RELEASE, RELEASE_TARGETS_MAX, apply_native,
                       derived_id, enabled_states, evidence_owed, git_ancestry,
                       integration_evidence, project_facts, release_selection, release_targets,
                       reverted_integrations, scoped_evidence, validate_payload,
                       validate_release_payload)
from requirements import canonical_bytes, content_hash

ACTOR = 'alice/session'
OPERATOR = 'coordinator-1'
SOURCE_A = '1' * 40
SOURCE_B = '2' * 40
MERGE_A = '3' * 40
MERGE_B = '4' * 40
RELEASE_COMMIT = '5' * 40
RELEASE_1 = '6' * 40
RELEASE_2 = '7' * 40
PREVIOUS_RELEASE = '8' * 40
OLD_FIELDS = {'schema_version', 'operation_id', 'task', 'dimension', 'value', 'scope',
              'evidence', 'provenance', 'actor'}


def old_validate(p):
    """A pre-note kit's validator: any extra field makes the payload unknown."""
    if not isinstance(p, dict) or set(p) != OLD_FIELDS:
        raise ValueError('invalid lifecycle payload')


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
        # The fixing task must exist: an enabled-with-known-defect fact that names
        # no real task is refused (kittrial-5bb.95 review item small-and-tests).
        store = NativeStore(tasks=('trial-a', 'fixing-task')).seed('trial-a')
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
        store = (NativeStore(tasks=('trial-a', 'trial-b', 'fixing-task'))
                 .seed('trial-a').seed('trial-b', integration=MERGE_B, source=SOURCE_B))
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
    def run_cli(self, argv, client=None, catch=False):
        previous = sys.modules.get('client')
        if client is not None:
            sys.modules['client'] = client
        original_argv, original_ancestry = sys.argv, lifecycle.git_ancestry
        lifecycle.git_ancestry = lambda repo: (lambda commit, release: commit == MERGE_A)
        sys.argv = argv
        out = io.StringIO()
        error = None
        try:
            with contextlib.redirect_stdout(out):
                lifecycle.main()
        except SystemExit as exc:
            if not catch:
                raise
            error = str(exc)
        finally:
            sys.argv, lifecycle.git_ancestry = original_argv, original_ancestry
            if client is not None:
                if previous is None:
                    sys.modules.pop('client', None)
                else:
                    sys.modules['client'] = previous
        return (out.getvalue(), error) if catch else out.getvalue()

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


class ReselectionTests(unittest.TestCase):
    """A release selects only what is new in it (kittrial-5bb.95 item 1)."""

    def deployed(self, store, task, release, environment, integration=RELEASE_1,
                 source=SOURCE_A, live=False):
        scope_value = scope(source=source, integration=integration, release=release,
                            environment=environment)
        store.record(payload(task, 'lifecycle-scope',
                             operation='scope-%s-%s-%s' % (task, release, environment),
                             scope_value=scope_value))
        store.record(payload(task, 'deployed',
                             operation='deployed-%s-%s-%s' % (task, release, environment),
                             scope_value=scope_value))
        if live:
            store.record(payload(task, 'live-verified',
                                 operation='verified-%s-%s-%s' % (task, release, environment),
                                 scope_value=scope_value))

    def release_scope(self, integration=RELEASE_2, release='r-2', environment='production'):
        return {'source_commit': '', 'integration_commit': integration,
                'release_id': release, 'environment': environment}

    def ancestry(self, pairs):
        def is_ancestor(commit, release):
            return commit == release or (commit, release) in pairs
        return is_ancestor

    def test_a_same_release_is_not_recorded_twice(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        self.deployed(store, 'trial-a', 'r-1', 'production')
        selection = release_selection(
            store.rows, self.release_scope(integration=RELEASE_1, release='r-1'),
            self.ancestry({(MERGE_A, RELEASE_1)}))
        self.assertEqual(selection['targets'], [])
        self.assertIn('already deployed', selection['skipped'][0]['reason'])

    def test_a_different_release_id_reselects_a_task_deployed_earlier(self):
        # .107 item 4: deployed is per (environment, release id). A new release id
        # must record even a task already deployed at an earlier release, so a
        # rollback and a later roll-forward can both be recorded.
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        self.deployed(store, 'trial-a', 'r-1', 'production')
        selection = release_selection(
            store.rows, self.release_scope(integration=RELEASE_2, release='r-2'),
            self.ancestry({(MERGE_A, RELEASE_1), (MERGE_A, RELEASE_2),
                           (RELEASE_1, RELEASE_2)}))
        self.assertEqual([item['task'] for item in selection['targets']], ['trial-a'])

    def test_a_rollback_and_a_roll_forward_are_both_recordable(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        def always(commit, release):
            return True
        store.record(release_payload([target('trial-a')], operation='release-r1', release='r-1'))
        store.record(release_payload([target('trial-a')], operation='release-r3', release='r-3'))
        rollback = release_selection(store.rows,
                                     self.release_scope(release='r-1', integration=RELEASE_COMMIT),
                                     always)
        self.assertEqual([item['task'] for item in rollback['targets']], ['trial-a'],
                         'a rollback to R1 must be recordable after R3')
        store.record(release_payload([target('trial-a')], operation='release-r1-again', release='r-1'))
        again = release_selection(store.rows,
                                  self.release_scope(release='r-3', integration=RELEASE_COMMIT),
                                  always)
        self.assertEqual([item['task'] for item in again['targets']], ['trial-a'],
                         'rolling forward to R3 again must be recordable')

    def test_a_deployment_in_another_environment_is_not_already_deployed(self):
        # Kills the mutation that drops the environment from the already-deployed
        # decision (.107 item 8): staging is not production.
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        self.deployed(store, 'trial-a', 'r-1', 'staging')
        selection = release_selection(
            store.rows, self.release_scope(integration=RELEASE_1, release='r-1',
                                           environment='production'),
            self.ancestry({(MERGE_A, RELEASE_1)}))
        self.assertEqual([item['task'] for item in selection['targets']], ['trial-a'])

    def test_a_new_integration_is_selected_even_when_an_older_one_was_deployed(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        self.deployed(store, 'trial-a', 'r-1', 'production')
        # A NEW integration recorded after the first release must still ship.
        newer = scope(source=SOURCE_B, integration=MERGE_B)
        store.record(payload('trial-a', 'lifecycle-scope', operation='scope-b', scope_value=newer))
        store.record(payload('trial-a', 'integrated', operation='integrated-b', scope_value=newer))
        selection = release_selection(
            store.rows, self.release_scope(),
            self.ancestry({(MERGE_A, RELEASE_1), (MERGE_B, RELEASE_2), (RELEASE_1, RELEASE_2)}))
        self.assertEqual(selection['targets'],
                         [{'task': 'trial-a', 'source_commit': SOURCE_B,
                           'integration_commit': MERGE_B}])

    def test_a_caller_subset_selects_only_the_named_tasks(self):
        store = (NativeStore(tasks=('trial-a', 'trial-b')).seed('trial-a')
                 .seed('trial-b', integration=MERGE_B, source=SOURCE_B))
        selection = release_selection(store.rows, self.release_scope(),
                                      self.ancestry({(MERGE_A, RELEASE_2), (MERGE_B, RELEASE_2)}),
                                      subset={'trial-b'})
        self.assertEqual([item['task'] for item in selection['targets']], ['trial-b'])
        self.assertEqual(selection['skipped'], [])

    def test_a_previous_release_restricts_selection_to_the_range(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        second = scope(source=SOURCE_B, integration=MERGE_B)
        store.record(payload('trial-a', 'lifecycle-scope', operation='scope-b', scope_value=second))
        store.record(payload('trial-a', 'integrated', operation='integrated-b', scope_value=second))
        without = release_selection(store.rows, self.release_scope(),
                                    self.ancestry({(MERGE_A, RELEASE_2), (MERGE_B, RELEASE_2)}))
        self.assertEqual(without['targets'][0]['integration_commit'], MERGE_B)
        def ranged(commit, release):
            if release == RELEASE_2 and commit in (MERGE_A, MERGE_B):
                return True
            if commit == PREVIOUS_RELEASE and release == MERGE_A:
                return True
            return False
        with_previous = release_selection(store.rows, self.release_scope(), ranged,
                                          previous_commit=PREVIOUS_RELEASE)
        self.assertEqual(with_previous['targets'][0]['integration_commit'], MERGE_A)

    def test_chunking_pages_past_the_wire_bound(self):
        items = [target('trial-%03d' % index) for index in range(RELEASE_TARGETS_MAX + 5)]
        chunks = lifecycle._chunks(items, RELEASE_TARGETS_MAX)
        self.assertEqual([len(chunk) for chunk in chunks], [RELEASE_TARGETS_MAX, 5])
        with self.assertRaisesRegex(ValueError, 'at most'):
            validate_release_payload(release_payload(items))


class RevertAndDeliveryTests(unittest.TestCase):
    """Reverted integrations follow the REVIEW rule; a stale delivery is flagged."""

    def setUp(self):
        journal = None
        try:
            journal = Path(tempfile.mkdtemp())
            (journal / rw.JOURNAL_DIR).mkdir()
        except OSError as exc:
            if journal is not None:
                shutil.rmtree(journal, ignore_errors=True)
            self.skipTest('no writable host revert journal: ' + str(exc))
        self.journal = journal
        self.addCleanup(shutil.rmtree, journal, ignore_errors=True)

    def seeded(self, task='trial-a'):
        return NativeStore(tasks=(task,)).seed(task)

    def revert_payload(self, task, commit=MERGE_A, author=OPERATOR, operation_id='revert-1',
                       contribution='1'):
        return dict(schema_version=1, operation='revert-record', operation_id=operation_id,
                    task=task, contribution=contribution, integration_commit=commit,
                    revert_commit='b' * 40, reason='Re-merge dropped the change',
                    operator=author)

    def revert(self, store, task, commit=MERGE_A, author=OPERATOR, journaled=True,
               operation_id='revert-1'):
        """Append a native revert comment and, unless ``journaled`` is False, its host entry."""
        payload = self.revert_payload(task, commit, author, operation_id)
        row = next(item for item in store.rows if item['id'] == task)
        cid = str(len(row.get('comments') or []) + 1)
        row.setdefault('comments', []).append(
            {'id': cid, 'author': author, 'created_at': '2026-01-01T00:00:00Z',
             'text': lifecycle.REVERT_PREFIX + canonical_bytes(payload).decode()})
        if journaled:
            entry = rw.revert_journal_entry(rw.JOURNAL_REVERT, payload, cid, author, task=task,
                                            contribution='1', integration_commit=commit,
                                            revert_commit='b' * 40)
            rw.publish_revert_journal(self.journal, entry)
        return cid

    def read_reverts(self, store):
        return reverted_integrations(store.rows, [OPERATOR], self.journal)

    def test_a_host_issued_revert_is_read_by_the_review_rule(self):
        store = self.seeded()
        self.revert(store, 'trial-a', MERGE_A)
        self.assertEqual(self.read_reverts(store), {('trial-a', MERGE_A)})
        selection = release_selection(store.rows, {'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                                                   'release_id': 'release-1', 'environment': 'production'},
                                      lambda commit, release: True,
                                      operators=[OPERATOR], journal=self.journal)
        self.assertEqual(selection['targets'], [])
        self.assertIn('reverted', selection['skipped'][0]['reason'])

    def test_a_revert_without_a_host_journal_entry_is_ignored(self):
        # A comment written through host bd by a non-operator (or with no journal
        # entry) is not a revert to review, so it must not exclude the task (.107 item 2).
        store = self.seeded()
        self.revert(store, 'trial-a', MERGE_A, author='mallory', journaled=False)
        self.assertEqual(self.read_reverts(store), set())
        selection = release_selection(store.rows, {'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                                                   'release_id': 'release-1', 'environment': 'production'},
                                      lambda commit, release: True,
                                      operators=[OPERATOR], journal=self.journal)
        self.assertEqual([item['task'] for item in selection['targets']], ['trial-a'])

    def test_a_journaled_revert_by_a_non_operator_is_ignored(self):
        store = self.seeded()
        self.revert(store, 'trial-a', MERGE_A, author='mallory', journaled=True)
        self.assertEqual(self.read_reverts(store), set())

    def test_a_retracted_revert_is_read_as_passed_again(self):
        # A host-journaled operator void retracts the revert; review reads passed
        # again and the release must select the task (.107 item 2).
        store = self.seeded()
        cid = self.revert(store, 'trial-a', MERGE_A)
        row = store.rows[0]
        original = next(c['text'] for c in row['comments'] if str(c['id']) == cid)
        void = dict(schema_version=1, operation='void-record', operation_id='void-revert-1',
                    task='trial-a', target=cid, target_kind='integration-revert',
                    target_sha256=rw.recovery.digest(original), original=original,
                    reason='mistaken revert', disposition='void', operator=OPERATOR)
        def operator_run(args):
            comment_id = str(len(row.get('comments') or []) + 1)
            row.setdefault('comments', []).append(
                {'id': comment_id, 'author': OPERATOR, 'created_at': '2026-01-01T00:00:00Z',
                 'text': args[3]})
            return json.dumps({'id': comment_id})
        rw.apply_void(store.rows, 'trial-a', OPERATOR, void, operator_run, operator=True,
                      operators=[OPERATOR], journal=self.journal)
        self.assertEqual(self.read_reverts(store), set())
        selection = release_selection(store.rows, {'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                                                   'release_id': 'release-1', 'environment': 'production'},
                                      lambda commit, release: True,
                                      operators=[OPERATOR], journal=self.journal)
        self.assertEqual([item['task'] for item in selection['targets']], ['trial-a'])

    def test_a_reverted_integration_is_refused_by_the_endpoint(self):
        store = self.seeded()
        self.revert(store, 'trial-a', MERGE_A)
        with self.assertRaisesRegex(ValueError, 'reverted'):
            apply_native(release_payload([target('trial-a')]), ACTOR, store.run,
                         operators=[OPERATOR], journal=self.journal)

    def test_selection_flags_a_delivery_that_is_not_the_current_contribution(self):
        store = self.seeded()
        selection = release_selection(store.rows, {'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                                                   'release_id': 'release-1', 'environment': 'production'},
                                      lambda commit, release: True,
                                      current_commits={'trial-a': SOURCE_B})
        self.assertEqual([item['task'] for item in selection['targets']], ['trial-a'])
        flag = selection['flags'][0]
        self.assertEqual(flag['flag'], 'delivery-not-current')
        self.assertEqual(flag['current_contribution'], SOURCE_B)
        self.assertIn(SOURCE_A, flag['reason'])

    def test_the_release_result_flags_a_delivery_that_is_not_current(self):
        store = self.seeded()
        original = lifecycle.current_contribution_commits
        lifecycle.current_contribution_commits = lambda rows: {'trial-a': SOURCE_B}
        try:
            result = store.record(release_payload([target('trial-a')]))
        finally:
            lifecycle.current_contribution_commits = original
        recorded = result['targets'][0]
        self.assertEqual(recorded['flag'], 'delivery-not-current')
        self.assertEqual(recorded['current_contribution'], SOURCE_B)
        self.assertIn('reader_note', result)


class EvidenceOwedHistoryTests(unittest.TestCase):
    """Evidence owed survives later releases and other environments (item 4)."""

    def release(self, store, release, environment, integration, live=False):
        scope_value = scope(source=SOURCE_A, integration=integration, release=release,
                            environment=environment)
        store.record(payload('trial-a', 'lifecycle-scope',
                             operation='scope-%s-%s' % (release, environment), scope_value=scope_value))
        store.record(payload('trial-a', 'deployed',
                             operation='deployed-%s-%s' % (release, environment), scope_value=scope_value))
        if live:
            store.record(payload('trial-a', 'live-verified',
                                 operation='verified-%s-%s' % (release, environment),
                                 scope_value=scope_value))

    def test_evidence_owed_keeps_earlier_environment_and_release_debts(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        self.release(store, 'r-10', 'staging', RELEASE_1, live=True)
        self.release(store, 'r-10', 'production', RELEASE_1)
        self.release(store, 'r-11', 'production', RELEASE_2)
        owed = {(group['environment'], group['release_id']): group['tasks'][0]['remaining_evidence']
                for group in evidence_owed(store.rows)}
        self.assertEqual(owed, {('staging', 'r-10'): [],
                                ('production', 'r-10'): ['live-verified'],
                                ('production', 'r-11'): ['live-verified']})

    def test_scoped_evidence_keeps_every_recorded_scope(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        self.release(store, 'r-10', 'production', RELEASE_1)
        entry = next(item for item in scoped_evidence(store.rows, ('deployed',))
                     if item['id'] == 'trial-a')
        releases = [item['scope']['release_id'] for item in entry['scopes'] if item['deployed']]
        self.assertEqual(releases, ['r-10'])

    def test_evidence_owed_keeps_the_enabled_flag_per_scope(self):
        # .107 item 8: a historical row must not show the task's CURRENT enabled
        # flag; it shows the flag recorded for that row's scope.
        store = NativeStore(tasks=('trial-a', 'fixing-task')).seed('trial-a')
        self.release(store, 'r-10', 'production', RELEASE_1)
        self.release(store, 'r-11', 'production', RELEASE_2)
        later = scope(source=SOURCE_A, integration=RELEASE_2, release='r-11', environment='production')
        store.record(payload('trial-a', ENABLED, value=DEFECT_VALUE, operation='enabled-11',
                             scope_value=later, defect_task='fixing-task'))
        rows = {(group['environment'], group['release_id']): group['tasks'][0]
                for group in evidence_owed(store.rows)}
        self.assertEqual(rows[('production', 'r-10')]['enabled'], 'unknown')
        self.assertIsNone(rows[('production', 'r-10')]['defect_task'])
        self.assertEqual(rows[('production', 'r-11')]['enabled'], DEFECT_VALUE)
        self.assertEqual(rows[('production', 'r-11')]['defect_task'], 'fixing-task')


class NoteOnOlderKitTests(unittest.TestCase):
    """The per-target note rides the deployed fact only (item 5)."""

    def test_a_note_never_reaches_the_scope_event_and_an_old_reader_still_reads_it(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.record(release_payload([target('trial-a', note='note for the deployed fact')]))
        events = [lifecycle.native_event(row) for row in store.rows if row.get('issue_type') == 'event']
        payloads = [event['payload'] for event in events if event and event['payload']]
        scope_payloads = [p for p in payloads
                          if p['dimension'] == 'lifecycle-scope' and p['scope']['release_id'] == 'release-1']
        self.assertEqual(len(scope_payloads), 1)
        self.assertEqual(set(scope_payloads[0]), OLD_FIELDS)
        self.assertNotIn('note', scope_payloads[0])
        deployed = [p for p in payloads
                    if p['dimension'] == 'deployed' and p['scope']['release_id'] == 'release-1']
        self.assertEqual(deployed[0]['note'], 'note for the deployed fact')
        # A strict pre-note reader still reads the release scope; the annotated
        # deployed payload is unknown to it, never passed.
        original = lifecycle.validate_payload
        lifecycle.validate_payload = old_validate
        try:
            state = next(row for row in project_facts(store.rows) if row['id'] == 'trial-a')
        finally:
            lifecycle.validate_payload = original
        self.assertEqual(state['scope'], scope(source=SOURCE_A, integration=MERGE_A,
                                               release='release-1', environment='production'))
        self.assertEqual(state['facts']['deployed']['value'], 'unknown')


class SilentDropAndConflictTests(unittest.TestCase):
    """Nothing integrated is dropped silently; IDs are checked first (item 6)."""

    def release_scope(self):
        return {'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                'release_id': 'release-1', 'environment': 'production'}

    def test_a_raw_native_set_state_integration_is_reported_as_skipped(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.rows.append(dict(_type='issue', id='trial-a.99', issue_type='event',
                               title='State change: integrated → passed',
                               description='Set integrated to passed\n\nReason: manual operator note',
                               status='closed', created_by=ACTOR, created_at='',
                               dependencies=[dict(issue_id='trial-a.99', depends_on_id='trial-a',
                                                  type='parent-child')]))
        selection = release_selection(store.rows, self.release_scope(), lambda commit, release: True)
        self.assertEqual(selection['targets'], [])
        self.assertIn('raw native set-state', selection['skipped'][0]['reason'])

    def test_two_integrated_labels_are_reported_as_skipped(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.rows[0]['labels'] = store.rows[0]['labels'] + ['integrated:failed']
        selection = release_selection(store.rows, self.release_scope(), lambda commit, release: True)
        self.assertEqual(selection['targets'], [])
        self.assertIn('more than one native integrated: label', selection['skipped'][0]['reason'])

    def test_an_operation_id_conflict_is_refused_before_the_first_write(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        release = release_payload([target('trial-a')], operation='release-1')
        planted = derived_id('release-1', 'deployed', 'trial-a')
        store.record(payload('trial-a', 'tested', operation=planted))
        before = json.dumps(store.rows)
        calls = len(store.calls)
        with self.assertRaisesRegex(ValueError, 'operation ID already used for different content'):
            store.record(release)
        self.assertEqual(json.dumps(store.rows), before)
        self.assertEqual(store.calls[calls:], [['export', '--all']])


class SmallAndTestsTests(unittest.TestCase):
    """defect_task existence, shallow clones, the git guard and scope writes (item 7)."""

    def test_an_unknown_defect_task_is_refused(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        with self.assertRaisesRegex(ValueError, 'unknown defect_task'):
            store.record(payload('trial-a', ENABLED, value=DEFECT_VALUE, operation='enabled-x',
                                 defect_task='no-such-task'))

    def test_a_non_commit_integration_never_reaches_the_git_command_line(self):
        store = NativeStore(tasks=('trial-help',))
        odd = scope(integration='--help')
        store.record(payload('trial-help', 'lifecycle-scope', operation='scope-help', scope_value=odd))
        store.record(payload('trial-help', 'integrated', operation='integrated-help', scope_value=odd))
        def never(commit, release):
            raise AssertionError('the non-commit guard must keep %r off the git command line' % commit)
        selection = release_selection(store.rows,
                                      {'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                                       'release_id': 'release-1', 'environment': 'production'}, never)
        self.assertEqual(selection['targets'], [])
        self.assertIn('not a full lowercase commit', selection['skipped'][0]['reason'])

    def test_the_release_scope_is_written_only_when_it_differs(self):
        store = NativeStore(tasks=('trial-a',))
        already = scope(source=SOURCE_A, integration=MERGE_A, release='release-1',
                        environment='production')
        store.record(payload('trial-a', 'lifecycle-scope', operation='scope-equal', scope_value=already))
        store.record(payload('trial-a', 'integrated', operation='integrated-a', scope_value=already))
        result = store.record(release_payload([target('trial-a')]))
        self.assertIs(result['targets'][0]['scope_recorded'], False)
        scope_events = [row for row in store.rows if row.get('issue_type') == 'event'
                        and 'lifecycle-scope' in row.get('description', '')]
        self.assertEqual(len(scope_events), 1)

    def test_the_release_reads_one_export_for_the_whole_batch(self):
        store = (NativeStore(tasks=('trial-a', 'trial-b')).seed('trial-a')
                 .seed('trial-b', integration=MERGE_B, source=SOURCE_B))
        calls = len(store.calls)
        store.record(release_payload([target('trial-a'),
                                      target('trial-b', source=SOURCE_B, integration=MERGE_B)],
                                     live_verified=True))
        added = store.calls[calls:]
        self.assertEqual(added.count(['export', '--all']), 1)

    def test_integration_evidence_shape_is_unchanged(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        entry = next(item for item in integration_evidence(store.rows) if item['id'] == 'trial-a')
        self.assertLessEqual({'value', 'event_id', 'evidence', 'provenance'},
                             set(entry['scopes'][0]['integrated']))
        self.assertLessEqual({'scope_token', 'scope', 'order', 'integrated'},
                             set(entry['scopes'][0]))


@unittest.skipUnless(shutil.which('git'), 'git is not installed')
class ShallowCloneTests(unittest.TestCase):
    def test_a_commit_missing_from_the_checkout_is_named(self):
        root = tempfile.mkdtemp()
        try:
            if subprocess.run(['git', '-C', root, 'init', '-q'], capture_output=True).returncode:
                self.skipTest('git cannot create a repository in this environment')
            subprocess.run(['git', '-C', root, 'config', 'user.email', 'synthetic@example.invalid'],
                           capture_output=True)
            subprocess.run(['git', '-C', root, 'config', 'user.name', 'Synthetic Test'],
                           capture_output=True)
            (Path(root) / 'file.txt').write_text('one\n', encoding='utf-8')
            subprocess.run(['git', '-C', root, 'add', 'file.txt'], capture_output=True)
            subprocess.run(['git', '-C', root, 'commit', '-q', '-m', 'first'], capture_output=True)
            head = subprocess.run(['git', '-C', root, 'rev-parse', 'HEAD'], capture_output=True,
                                  text=True).stdout.strip()
            missing = 'a' * 40
            with self.assertRaises(ValueError) as caught:
                git_ancestry(root)(missing, head)
            self.assertIn('is not in this checkout', str(caught.exception))
            self.assertIn(missing, str(caught.exception))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class ReleaseCommandChunkTests(ReleaseCommandTests):
    """The release command sends one request per group (item 2, lock hold)."""

    def test_the_command_splits_the_write_into_groups(self):
        store = (NativeStore(tasks=('trial-a', 'trial-b', 'trial-c'))
                 .seed('trial-a').seed('trial-b').seed('trial-c'))
        sent = []
        client = types.ModuleType('client')
        def fake_request(config, project, actor, args, action=None):
            sent.append(json.loads(args[0]))
            return {'returncode': 0, 'stderr': '',
                    'stdout': json.dumps({'targets': [{'task': item['task']} for item in json.loads(args[0])['targets']]})}
        client.request = fake_request
        with scratch() as root:
            argv = self.fixture(root, store) + ['--chunk-size', '2']
            report = json.loads(self.run_cli(argv, client))
        self.assertEqual(report['chunks'], [2, 1])
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(len(payload['targets']) <= 2 for payload in sent))


class ReaderDeliveryTests(unittest.TestCase):
    """brief/work name the delivery a deployed fact belongs to (item 3, .107 item 7)."""

    def add_contribution(self, store, task, commit):
        payload = dict(schema_version=1, operation='contribute', operation_id='contribute-1',
                       task=task, previous=None, repository='ssh://git/example', commit=commit,
                       base_commit='b' * 40,
                       delivery=dict(kind='bundle', path='server:/rev.bundle', sha256='c' * 64),
                       summary='Contribution delivered', supersedes=None)
        row = next(item for item in store.rows if item['id'] == task)
        row.setdefault('comments', []).append(
            {'id': 'contribution-1', 'author': ACTOR, 'created_at': '2026-01-01T00:00:00Z',
             'text': rw.PREFIX + canonical_bytes(payload).decode()})

    def clipped(self, value):
        return {'text': value, 'omitted_chars': 0}

    def test_brief_and_work_name_the_deployed_delivery(self):
        import briefing
        import work
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.record(release_payload([target('trial-a')]))
        brief = briefing.brief(store.rows, 'trial', 'trial-a')
        self.assertEqual(brief['deployed_delivery'],
                         {'release_id': self.clipped('release-1'),
                          'environment': self.clipped('production'),
                          'source_commit': self.clipped(SOURCE_A),
                          'integration_commit': self.clipped(MERGE_A)})
        self.assertIsNone(brief['deployed_delivery_is_current_contribution'])
        queue = work.queue(store.rows, ACTOR, ['--mine'])
        item = next(entry for entry in queue['items'] if entry['task'] == 'trial-a')
        self.assertEqual(item['deployed_delivery']['release_id'], self.clipped('release-1'))
        self.assertEqual(item['deployed_delivery']['environment'], self.clipped('production'))

    def test_deployed_delivery_is_current_contribution_answers_both_ways(self):
        # Kills the mutation that inverts this answer in brief (.107 item 8).
        import briefing
        same = NativeStore(tasks=('trial-a',)).seed('trial-a')
        same.record(release_payload([target('trial-a')]))
        self.add_contribution(same, 'trial-a', SOURCE_A)
        self.assertIs(briefing.brief(same.rows, 'trial', 'trial-a')
                      ['deployed_delivery_is_current_contribution'], True)
        other = NativeStore(tasks=('trial-a',)).seed('trial-a')
        other.record(release_payload([target('trial-a')]))
        self.add_contribution(other, 'trial-a', SOURCE_B)
        self.assertIs(briefing.brief(other.rows, 'trial', 'trial-a')
                      ['deployed_delivery_is_current_contribution'], False)

    def test_work_omits_deployed_delivery_when_deployed_is_not_passed(self):
        # Kills the mutation that shows the delivery in work regardless of deployed (.107 item 8).
        import work
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        queue = work.queue(store.rows, ACTOR, ['--mine'])
        item = next(entry for entry in queue['items'] if entry['task'] == 'trial-a')
        self.assertIsNone(item['deployed_delivery'])
        self.assertIsNone(item['deployed_delivery_is_current_contribution'])

    def test_an_oversized_release_id_is_clipped(self):
        import briefing
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.record(release_payload([target('trial-a')], release='r' * 3657))
        brief = briefing.brief(store.rows, 'trial', 'trial-a')
        release = brief['deployed_delivery']['release_id']
        self.assertEqual(len(release['text']), 160)
        self.assertEqual(release['omitted_chars'], 3657 - 160)


class LaterLiveVerifiedTests(unittest.TestCase):
    """A later --live-verified selects tasks deployed at that release (.107 item 1)."""

    def release_scope(self, release='r-1', environment='production'):
        return {'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                'release_id': release, 'environment': environment}

    def test_a_plain_reselection_is_empty_but_live_verified_selects(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.record(release_payload([target('trial-a')], release='r-1'))
        plain = release_selection(store.rows, self.release_scope(), lambda commit, release: True)
        self.assertEqual(plain['targets'], [])
        later = release_selection(store.rows, self.release_scope(), lambda commit, release: True,
                                  live_verified=True)
        self.assertEqual([item['task'] for item in later['targets']], ['trial-a'])
        self.assertEqual(later['verify_only'], ['trial-a'])

    def test_the_later_write_records_live_verified_without_duplicating_deployed(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.record(release_payload([target('trial-a')], release='r-1'))
        result = store.record(release_payload([target('trial-a')], release='r-1', live_verified=True))
        recorded = result['targets'][0]
        self.assertTrue(recorded['already_deployed'])
        self.assertIsNone(recorded['deployed'])
        self.assertIsNotNone(recorded['live_verified'])
        self.assertEqual(store.facts('trial-a')['facts']['live-verified']['value'], 'passed')
        deployed_events = [row for row in store.rows if row.get('issue_type') == 'event'
                           and 'deployed' in row.get('title', '')]
        self.assertEqual(len(deployed_events), 1)
        # Once it is passed, a further --live-verified is a no-op.
        again = store.record(release_payload([target('trial-a')], release='r-1', live_verified=True))
        self.assertIsNone(again['targets'][0]['live_verified'])
        self.assertTrue(again['targets'][0]['reconciled'])


class PreviousReleaseValidationTests(ReleaseCommandTests):
    """--previous-release-commit is validated before git is used (.107 item 5)."""

    def test_a_non_commit_short_commit_or_uppercase_commit_is_refused(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        for value in ('HEAD', '6' * 7, 'A' * 40):
            with scratch() as root:
                argv = self.fixture(root, store) + ['--dry-run', '--previous-release-commit', value]
                _out, error = self.run_cli(argv, catch=True)
            self.assertIn('full lowercase 40-character commit', error or '')

    def test_a_commit_that_is_not_an_ancestor_of_the_release_is_refused(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        with scratch() as root:
            argv = self.fixture(root, store) + ['--dry-run', '--previous-release-commit', 'a' * 40]
            _out, error = self.run_cli(argv, catch=True)
        self.assertIn('must be an ancestor of the release commit', error or '')

    def test_the_release_commit_itself_is_refused(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        with scratch() as root:
            argv = self.fixture(root, store) + ['--dry-run', '--previous-release-commit', RELEASE_COMMIT]
            _out, error = self.run_cli(argv, catch=True)
        self.assertIn('must not be the release commit itself', error or '')


class ReleaseGroupReportingTests(ReleaseCommandTests):
    """A multi-group release is checked as one operation (.107 item 3)."""

    def store(self):
        return (NativeStore(tasks=('trial-a', 'trial-b', 'trial-c'))
                .seed('trial-a').seed('trial-b').seed('trial-c'))

    def test_a_conflict_in_the_last_group_writes_nothing(self):
        store = self.store()
        planted = derived_id('release-1', 'deployed', 'trial-c')
        store.record(payload('trial-c', 'tested', operation=planted))
        sent = []
        client = types.ModuleType('client')
        client.request = lambda *args, **kwargs: sent.append(args) or {
            'returncode': 0, 'stdout': '{}', 'stderr': ''}
        with scratch() as root:
            argv = self.fixture(root, store) + ['--chunk-size', '1']
            _out, error = self.run_cli(argv, client, catch=True)
        self.assertEqual(sent, [], 'no group may be sent after the whole-release check fails')
        self.assertIn('operation ID already used for different content', error or '')

    def test_a_group_failure_reports_which_groups_completed(self):
        store = self.store()
        sent = []
        client = types.ModuleType('client')
        def fake_request(config, project, actor, args, action=None):
            sent.append(args)
            if len(sent) == 2:
                return {'returncode': 2, 'stdout': '', 'stderr': 'endpoint refused group two'}
            return {'returncode': 0, 'stderr': '', 'stdout': json.dumps({'targets': []})}
        client.request = fake_request
        with scratch() as root:
            argv = self.fixture(root, store) + ['--chunk-size', '1']
            out, error = self.run_cli(argv, client, catch=True)
        report = json.loads(out)
        self.assertEqual(report['groups_completed'], 1)
        self.assertEqual(report['groups_total'], 3)
        self.assertEqual(report['chunks'], [1, 1, 1])
        self.assertIn('endpoint refused group two', report['failure'])
        self.assertIn('failed after 1 of 3 group(s)', error or '')


class ReleaseCostEstimateTests(unittest.TestCase):
    """expected_seconds is an upper bound and the default group is small (.107 item 6)."""

    def test_the_estimate_covers_the_measured_cost(self):
        self.assertLessEqual(lifecycle.RELEASE_CHUNK_DEFAULT, 25)
        self.assertGreaterEqual(
            round(lifecycle.DRY_RUN_FIXED_SECONDS + lifecycle.DRY_RUN_SECONDS_PER_TARGET * 201, 1),
            335.0)
        self.assertGreaterEqual(
            round(lifecycle.DRY_RUN_FIXED_SECONDS + lifecycle.DRY_RUN_SECONDS_PER_TARGET * 50, 1),
            86.0)

    def test_the_dry_run_uses_the_upper_bound_formula(self):
        store = (NativeStore(tasks=('trial-a', 'trial-b', 'trial-c'))
                 .seed('trial-a').seed('trial-b').seed('trial-c'))
        client = types.ModuleType('client')
        client.request = lambda *args, **kwargs: {'returncode': 0, 'stdout': '{}', 'stderr': ''}
        with scratch() as root:
            argv = ReleaseCommandTests.fixture(self, root, store) + ['--dry-run']
            report = json.loads(ReleaseCommandTests.run_cli(self, argv, client))
        self.assertEqual(report['total_chunks'], 1)
        self.assertEqual(report['expected_seconds'],
                         round(lifecycle.DRY_RUN_FIXED_SECONDS
                               + lifecycle.DRY_RUN_SECONDS_PER_TARGET * 3, 1))


if __name__ == '__main__':
    unittest.main()
