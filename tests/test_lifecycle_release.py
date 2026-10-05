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
from lifecycle import (DEFECT_VALUE, ENABLED, LIVE, RELEASE, RELEASE_QUERY, RELEASE_TARGETS_MAX,
                       apply_native, derived_id, enabled_states, evidence_owed, git_ancestry,
                       integration_evidence, project_facts, release_query, release_selection,
                       release_targets, reverted_integrations, rollback_selection, scoped_evidence,
                       validate_payload, validate_query_payload, validate_release_payload)
from requirements import canonical_bytes, content_hash

ACTOR = 'alice/session'
OPERATOR = 'coordinator-1'
SOURCE_A = '1' * 40
SOURCE_B = '2' * 40
MERGE_A = '3' * 40
MERGE_B = '4' * 40
MERGE_C = '9' * 40
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


def live_releases_in(rows, environment, task):
    """The release `evidence-owed` shows as LIVE for one task in one environment.

    The reader-level statement of "one live row per task" (kittrial-5bb.107 rev4
    item 2): a rollback must leave exactly one and a roll forward must move it. The
    liveness rewrite's own rule is applied - the newest `live` event for the task's
    CURRENT scope wins - so a stale row that still carries an old `live=live` fact
    is not reported as live.
    """
    state = next((entry for entry in project_facts(rows) if entry['id'] == task), None)
    if state is None or state['live'] != 'live' or state['scope'] is None:
        return []
    return [state['scope'].get('release_id') or '']


def released(store, task, release, environment, integration, source=SOURCE_A, live=True):
    """Record a deployment scope for a task, optionally with its `live=live` fact."""
    scope_value = scope(source=source, integration=integration, release=release,
                        environment=environment)
    store.record(payload(task, 'lifecycle-scope', operation='scope-%s-%s-%s' % (task, release, environment),
                         scope_value=scope_value))
    store.record(payload(task, 'deployed', operation='deployed-%s-%s-%s' % (task, release, environment),
                         scope_value=scope_value))
    if live:
        store.record(payload(task, 'live', value='live', operation='live-%s-%s-%s' % (task, release, environment),
                             scope_value=scope_value))
    return scope_value


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

    def record_honest(self, p):
        """``record`` that also keeps the native events it writes (reconciliation).

        ``record`` reuses the same 2026-10-01 timestamp for every event, so two events
        written in the same second are indistinguishable and the reconciliation-by-
        operation-id path cannot be simulated. A real runtime names each event with a
        unique id and reads it back on the next export; this does the same, so a test
        can assert that an exact retry writes nothing at all.
        """
        added = []
        real = self.run
        def run(args):
            before = len(self.rows)
            answer = real(args)
            if args and args[0] == 'set-state':
                for offset, row in enumerate(self.rows[before:]):
                    seconds = before + offset
                    row['created_at'] = '2026-10-01T%02d:%02d:%02dZ' % (seconds // 3600,
                                                                        seconds // 60 % 60,
                                                                        seconds % 60)
                    added.append(row)
            return answer
        self.run = run
        try:
            return self.record(p)
        finally:
            self.run = real
            self.honest_events = getattr(self, 'honest_events', []) + added

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

    def fixture_for(self, root, store, data):
        export = Path(root) / 'issues.jsonl'
        export.write_text(''.join(json.dumps(row) + '\n' for row in store.rows), encoding='utf-8')
        request = Path(root) / 'release.json'
        request.write_text(json.dumps(data), encoding='utf-8')
        config = Path(root) / 'client.json'
        config.write_text('{}', encoding='utf-8')
        return ['lifecycle.py', 'release', '--config', str(config), '--project', 'example',
                '--actor', ACTOR, '--file', str(request), '--export', str(export), '--repo', str(root)]

    def test_a_plain_release_round_trips_through_the_endpoint(self):
        # The CLI's chunk payload must be exactly a valid release payload: a plain
        # release carries NO supersede field (that field belongs to a rollback only).
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        sent = []
        client = types.ModuleType('client')
        def fake_request(config, project, actor, args, action=None):
            payload = json.loads(args[0]); sent.append(payload)
            return {'returncode': 0, 'stderr': '',
                    'stdout': json.dumps(apply_native(payload, ACTOR, store.run))}
        client.request = fake_request
        with scratch() as root:
            argv = self.fixture(root, store)
            report = json.loads(self.run_cli(argv, client))
        self.assertEqual(report['groups_completed'], 1)
        write = next(payload for payload in sent if payload['dimension'] == RELEASE)
        self.assertNotIn('supersede', write)
        self.assertEqual(store.facts('trial-a')['facts']['deployed']['value'], 'passed')
        self.assertEqual(store.facts('trial-a')['live'], 'live')

    def test_a_rollback_round_trips_through_the_endpoint(self):
        store = (NativeStore(tasks=('trial-a', 'trial-b'))
                 .seed('trial-a')
                 .seed('trial-b', integration=MERGE_B, source=SOURCE_B))
        store.record(release_payload([target('trial-a')], operation='release-r1', release='r-1',
                                     scope={'source_commit': '', 'integration_commit': RELEASE_1,
                                            'release_id': 'r-1', 'environment': 'production'}))
        store.record(release_payload([target('trial-a'),
                                      target('trial-b', integration=MERGE_B, source=SOURCE_B)],
                                     operation='release-r3', release='r-3',
                                     scope={'source_commit': '', 'integration_commit': RELEASE_2,
                                            'release_id': 'r-3', 'environment': 'production'}))
        sent = []
        client = types.ModuleType('client')
        def fake_request(config, project, actor, args, action=None):
            payload = json.loads(args[0]); sent.append(payload)
            return {'returncode': 0, 'stderr': '',
                    'stdout': json.dumps(apply_native(payload, ACTOR, store.run))}
        client.request = fake_request
        rb = release_payload([], operation='rollback-r1', release='r-1',
                             scope={'source_commit': '', 'integration_commit': RELEASE_1,
                                    'release_id': 'r-1', 'environment': 'production'},
                             rollback=True)
        with scratch() as root:
            report = json.loads(self.run_cli(self.fixture_for(root, store, rb) + ['--rollback'], client))
        self.assertTrue(report['rollback'])
        self.assertEqual(report['superseded'], ['trial-b'])
        self.assertEqual(store.facts('trial-a')['scope']['release_id'], 'r-1')
        self.assertEqual(store.facts('trial-b')['live'], 'superseded')
        self.assertEqual(store.facts('trial-b')['facts']['deployed']['value'], 'unknown')

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

    def test_release_asks_the_endpoint_about_reverts_then_writes_the_targets(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        calls = []
        client = types.ModuleType('client')

        def fake_request(config, project, actor, args, action=None):
            payload = json.loads(args[0])
            calls.append({'project': project, 'actor': actor, 'payload': payload, 'action': action})
            if payload['dimension'] == RELEASE_QUERY:
                return {'returncode': 0, 'stderr': '',
                        'stdout': json.dumps({'reverted': [], 'live_releases': []})}
            return {'returncode': 0, 'stderr': '',
                    'stdout': json.dumps({'operation_id': 'release-1',
                                          'targets': [{'task': 'trial-a'}]})}

        client.request = fake_request
        with scratch() as root:
            argv = self.fixture(root, store) + ['--live-verified']
            report = json.loads(self.run_cli(argv, client))
            self.assertEqual(len(calls), 2)
            self.assertEqual((calls[0]['project'], calls[0]['actor'], calls[0]['action']),
                             ('example', ACTOR, 'lifecycle'))
            self.assertEqual(calls[0]['payload']['dimension'], RELEASE_QUERY)
            sent = calls[1]['payload']
            self.assertEqual(sent['dimension'], RELEASE)
            self.assertTrue(sent['live_verified'])
            self.assertEqual(sent['targets'], [{'task': 'trial-a', 'source_commit': SOURCE_A,
                                                'integration_commit': MERGE_A}])
            self.assertEqual(report['result']['targets'], [{'task': 'trial-a'}])

    def test_the_client_uses_the_endpoints_reverted_set(self):
        # rev3 item 4.1b: the writing command must use the ENDPOINT's reverted set,
        # not the local journal view. The endpoint reverts the only task, so no group
        # may be sent and the task must be reported skipped as reverted.
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        sent = []
        client = types.ModuleType('client')

        def fake_request(config, project, actor, args, action=None):
            payload = json.loads(args[0]); sent.append(payload)
            if payload['dimension'] == RELEASE_QUERY:
                return {'returncode': 0, 'stderr': '', 'stdout': json.dumps(
                    {'reverted': [{'task': 'trial-a', 'integration_commit': MERGE_A}],
                     'live_releases': []})}
            return {'returncode': 0, 'stderr': '',
                    'stdout': json.dumps(apply_native(payload, ACTOR, store.run))}

        client.request = fake_request
        with scratch() as root:
            report = json.loads(self.run_cli(self.fixture(root, store), client))
        self.assertEqual([payload for payload in sent if payload['dimension'] == RELEASE], [],
                         'the endpoint reverted the only task, so no group may be sent')
        self.assertIn('reverted', report['skipped'][0]['reason'])

    def test_the_dry_run_says_its_revert_view_is_local(self):
        # rev3 item 3.4: the dry run is offline, so it must say that it can list a
        # target the writing run skips.
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        client = types.ModuleType('client')

        def unexpected(*args, **kwargs):
            raise AssertionError('the dry run must stay offline')

        client.request = unexpected
        with scratch() as root:
            report = json.loads(self.run_cli(self.fixture(root, store) + ['--dry-run'], client))
        self.assertEqual(report['reverts_source'], 'local')
        self.assertIn('offline', report['dry_run_revert_note'])
        self.assertTrue(any('offline' in warning for warning in report['warnings']))

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

    def test_a_plain_second_release_is_incremental_by_default(self):
        # rev2 item 1: a plain deploy of R2 after R1 selects only what is NEW in R2.
        # No --previous-release-commit is needed; the containment rule makes the
        # default incremental again (rev1's per-release-id rule reselected everything).
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        self.deployed(store, 'trial-a', 'r-1', 'production')
        selection = release_selection(
            store.rows, self.release_scope(integration=RELEASE_2, release='r-2'),
            self.ancestry({(MERGE_A, RELEASE_1), (MERGE_A, RELEASE_2), (RELEASE_1, RELEASE_2)}))
        self.assertEqual(selection['targets'], [],
                         'a task already deployed in a release contained in R2 is not reselected')
        self.assertIn('already deployed', selection['skipped'][0]['reason'])

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

    def live_deployed(self, task_store, task, release, environment, integration, source=SOURCE_A):
        """Record a release scope plus deployed=passed AND live=live for a task."""
        return released(task_store, task, release, environment, integration, source=source)

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


class OlderReleaseRunTests(unittest.TestCase):
    """A run WITHOUT --rollback never supersedes when R is an ancestor of what is live.

    kittrial-5bb.107 rev4 item 1, found new in revision 3: a release run for an OLDER
    release marked the newer live release's tasks `live=superseded`, so they read
    `deployed=unknown` with `deployed_delivery: null` while the newer release was the
    live one. Deploying, redeploying or verifying an older release - with any
    operation id, including the original one - must write no live fact for another
    release's tasks, and the hotfix-on-a-branch drop must keep working.
    """

    def two_releases(self):
        """t1 delivered at r-1; t3 deployed at the newer r-2, which moves t1 to r-2.

        trial-a is recorded at r-1 WITHOUT a live fact (the state just before its
        first release run), so a test can record the r-1 deployment itself and still
        have a release to run afterwards. trial-b has no passing integration of its
        own, which is what makes it the task a later hotfix can drop.
        """
        store = NativeStore(tasks=('trial-a', 'trial-b')).seed('trial-a')
        released(store, 'trial-a', 'r-1', 'production', MERGE_A, live=False)
        released(store, 'trial-b', 'r-2', 'production', RELEASE_2, source=SOURCE_B)
        return store

    def scope_of(self, release, integration, environment='production'):
        return {'source_commit': '', 'integration_commit': integration,
                'release_id': release, 'environment': environment}

    def newer_release_ancestry(self):
        """r-1 is an ancestor of r-2; neither contains the other way round."""
        def is_ancestor(commit, release):
            return commit == release or (commit, release) in {(MERGE_A, RELEASE_1),
                                                              (MERGE_A, RELEASE_2),
                                                              (MERGE_B, RELEASE_2),
                                                              (RELEASE_1, RELEASE_2)}
        return is_ancestor

    def test_liveness_picks_the_newest_live_event_not_the_first_scope(self):
        # rev4 item 3 (4): order the two live events so that "first scope" and
        # "newest live event" disagree. The r-2 scope is recorded FIRST and r-1
        # second, so a reader that took the first scope would answer r-2 while the
        # newest live event says r-1. Only the newest-live-event rule answers r-1.
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        released(store, 'trial-a', 'r-2', 'production', MERGE_B, source=SOURCE_B)
        released(store, 'trial-a', 'r-1', 'production', MERGE_A)
        scope_entry, value = lifecycle.environment_liveness(
            next(entry for entry in scoped_evidence(store.rows, ('deployed', 'live'))
                 if entry['id'] == 'trial-a')['scopes'], 'production')
        self.assertEqual(value, 'live')
        self.assertEqual(scope_entry['scope']['release_id'], 'r-1',
                         'the newest live EVENT wins, not the first scope recorded')
        self.assertEqual(live_releases_in(store.rows, 'production', 'trial-a'), ['r-1'])


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
            payload = json.loads(args[0])
            sent.append(payload)
            return {'returncode': 0, 'stderr': '',
                    'stdout': json.dumps({'targets': [{'task': item['task']}
                                                       for item in payload['targets']]})}
        client.request = fake_request
        with scratch() as root:
            argv = self.fixture(root, store) + ['--chunk-size', '2']
            report = json.loads(self.run_cli(argv, client))
        writes = [payload for payload in sent if payload['dimension'] == RELEASE]
        self.assertEqual(report['chunks'], [2, 1])
        self.assertEqual(len(writes), 2)
        self.assertTrue(all(len(payload['targets']) <= 2 for payload in writes))


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
        # deployed_delivery values are PLAIN STRINGS clipped to 160 characters - the
        # shape the field shipped with a version ago (rev2 item 6.1), not excerpt
        # objects.
        return value[:160]

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
        self.assertTrue(all(isinstance(value, str)
                            for value in brief['deployed_delivery'].values()))
        self.assertIsNone(brief['deployed_delivery_is_current_contribution'])
        self.assertEqual(brief['deployed_live'], 'live')
        queue = work.queue(store.rows, ACTOR, ['--mine'])
        item = next(entry for entry in queue['items'] if entry['task'] == 'trial-a')
        self.assertEqual(item['deployed_delivery']['release_id'], self.clipped('release-1'))
        self.assertEqual(item['deployed_delivery']['environment'], self.clipped('production'))
        self.assertEqual(item['deployed_live'], 'live')

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
        self.assertIsInstance(release, str)
        self.assertEqual(len(release), 160)
        self.assertEqual(release, 'r' * 160)


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
        self.assertTrue(recorded['verify_only'])
        self.assertTrue(recorded['already_deployed'])
        self.assertIsNone(recorded['deployed'])
        self.assertIsNotNone(recorded['live_verified'])
        self.assertEqual(store.facts('trial-a')['facts']['live-verified']['value'], 'passed')
        deployed_events = [row for row in store.rows if row.get('issue_type') == 'event'
                           and 'deployed' in row.get('title', '')]
        self.assertEqual(len(deployed_events), 1)
        # Once it is passed, a further --live-verified is a no-op: the endpoint's
        # plan is empty (reconciled) and the client's selection skips it.
        again = store.record(release_payload([target('trial-a')], release='r-1', live_verified=True))
        self.assertIsNone(again['targets'][0]['live_verified'])
        self.assertTrue(again['targets'][0]['reconciled'])
        selection = release_selection(store.rows, self.release_scope(),
                                      lambda commit, release: True, live_verified=True)
        self.assertEqual(selection['targets'], [])
        self.assertIn('already deployed', selection['skipped'][0]['reason'])

    def test_verifying_an_older_release_never_moves_the_current_scope(self):
        # rev2 item 3: deploy R1, deploy R2, then --live-verified for R1. The task's
        # current scope stays R2 whichever operation id the operator uses, and the
        # live-verification is recorded as evidence for R1.
        for operation in ('r1-verify-new', 'release-r1'):
            with self.subTest(operation=operation):
                store = NativeStore(tasks=('trial-a',)).seed('trial-a')
                store.record(release_payload([target('trial-a')], operation='release-r1', release='r-1'))
                store.record(release_payload([target('trial-a')], operation='release-r2', release='r-2'))
                result = store.record(release_payload([target('trial-a')], operation=operation,
                                                      release='r-1', live_verified=True))
                recorded = result['targets'][0]
                self.assertTrue(recorded['verify_only'])
                self.assertFalse(recorded['scope_recorded'])
                self.assertIsNone(recorded['deployed'])
                self.assertIsNotNone(recorded['live_verified'])
                state = store.facts('trial-a')
                self.assertEqual(state['scope']['release_id'], 'r-2',
                                 'verifying R1 must not move the current scope back')
                self.assertEqual(state['facts']['deployed']['value'], 'passed')
                # The evidence is readable per scope: R1 no longer owes live-verified.
                owed = {(group['release_id'], item['task']): item['remaining_evidence']
                        for group in evidence_owed(store.rows) for item in group['tasks']}
                self.assertNotIn('live-verified', owed[('r-1', 'trial-a')])
                self.assertEqual(owed[('r-2', 'trial-a')], ['live-verified'])

    def test_a_late_verify_under_an_older_scope_never_hides_the_current_scope_fact(self):
        # rev3 item 2: deploy R1 to staging; deploy R1 to production with
        # --live-verified; then --live-verified for staging. The current scope stays
        # production and production must keep reading live-verified=passed even
        # though the newest live-verified event is now the staging one.
        import briefing
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        for environment, operation, verified in (('staging', 'deploy-staging', False),
                                                 ('production', 'deploy-production', True),
                                                 ('staging', 'verify-staging', True)):
            store.record(release_payload([target('trial-a')], operation=operation,
                                         release='r-1', live_verified=verified,
                                         scope={'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                                                'release_id': 'r-1', 'environment': environment}))
        state = store.facts('trial-a')
        self.assertEqual(state['scope']['environment'], 'production')
        self.assertEqual(state['facts']['live-verified']['value'], 'passed',
                         'a later fact under the older staging scope must not hide production')
        brief = briefing.brief(store.rows, 'trial', 'trial-a')
        self.assertEqual(brief['lifecycle']['live-verified']['value'], 'passed')

    def test_a_verified_newer_release_does_not_mask_an_unverified_older_one(self):
        # The verification owed for R1 must not be read as done because a LATER
        # release of the same integration was verified (rev2 item 3).
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.record(release_payload([target('trial-a')], operation='release-r1', release='r-1'))
        store.record(release_payload([target('trial-a')], operation='release-r2', release='r-2',
                                     live_verified=True))
        result = store.record(release_payload([target('trial-a')], operation='verify-r1',
                                              release='r-1', live_verified=True))
        self.assertTrue(result['targets'][0]['verify_only'])
        self.assertIsNotNone(result['targets'][0]['live_verified'])
        self.assertEqual(store.facts('trial-a')['scope']['release_id'], 'r-2')

    def test_a_late_verify_of_a_rolled_back_release_still_does_not_move_the_scope(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.record(release_payload([target('trial-a')], operation='release-r1', release='r-1'))
        store.record(release_payload([target('trial-a')], operation='release-r3', release='r-3'))
        store.record(release_payload([target('trial-a')], operation='rollback-r1', release='r-1',
                                     rollback=True))
        result = store.record(release_payload([target('trial-a')], operation='verify-r1',
                                              release='r-1', live_verified=True))
        self.assertTrue(result['targets'][0]['verify_only'])
        self.assertEqual(store.facts('trial-a')['scope']['release_id'], 'r-1')


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
        def fake_request(config, project, actor, args, action=None):
            payload = json.loads(args[0])
            sent.append(payload)
            if payload['dimension'] == RELEASE_QUERY:
                return {'returncode': 0, 'stdout': json.dumps({'reverted': []}), 'stderr': ''}
            return {'returncode': 0, 'stdout': '{}', 'stderr': ''}
        client.request = fake_request
        with scratch() as root:
            argv = self.fixture(root, store) + ['--chunk-size', '1']
            out, error = self.run_cli(argv, client, catch=True)
        writes = [payload for payload in sent if payload['dimension'] == RELEASE]
        self.assertEqual(writes, [], 'no group may be sent after the whole-release check fails')
        self.assertIn('operation ID already used for different content', error or '')
        # The planted-id refusal prints the SAME JSON failure report as a group
        # failure (rev2 item 6.2), not only the plain error line.
        report = json.loads(out)
        self.assertEqual(report['groups_completed'], 0)
        self.assertIn('operation ID already used for different content', report['failure'])

    def test_a_group_failure_reports_which_groups_completed(self):
        store = self.store()
        writes = []
        client = types.ModuleType('client')
        def fake_request(config, project, actor, args, action=None):
            payload = json.loads(args[0])
            if payload['dimension'] == RELEASE_QUERY:
                return {'returncode': 0, 'stderr': '',
                        'stdout': json.dumps({'reverted': []})}
            writes.append(payload)
            if len(writes) == 2:
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
        # A between-group failure needs a fresh export before a retry (rev2 item 6.3).
        self.assertTrue(report['fresh_export_required'])
        self.assertIn('FRESH export', report['fresh_export_note'])


class ReleaseCostEstimateTests(unittest.TestCase):
    """expected_seconds is an upper bound and the default group is small (.107 item 6)."""

    def test_the_estimate_covers_the_measured_cost(self):
        self.assertLessEqual(lifecycle.RELEASE_CHUNK_DEFAULT, 25)
        per_target = lifecycle.DRY_RUN_SECONDS_PER_TARGET
        fixed = lifecycle.DRY_RUN_FIXED_SECONDS
        # Each target now costs three writes, and the reviewer measured 438 s for
        # 201 targets (rev3 item 3.3), so the stated upper bound must clear that.
        self.assertGreaterEqual(round(fixed + per_target * 201, 1), 438.0)
        self.assertGreaterEqual(round(fixed + per_target * 50, 1), 86.0)
        self.assertLessEqual(round(fixed + per_target * lifecycle.RELEASE_CHUNK_DEFAULT, 1), 150.0,
                             'one default group must stay inside the 150 s client timeout')

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


class RollbackSemanticsTests(unittest.TestCase):
    """An explicit rollback leaves every reader saying what is live (rev2 item 2)."""

    def ancestry(self, pairs):
        def is_ancestor(commit, release):
            return commit == release or (commit, release) in pairs
        return is_ancestor

    def rolled_back(self):
        store = (NativeStore(tasks=('trial-a', 'trial-b'))
                 .seed('trial-a')
                 .seed('trial-b', integration=MERGE_B, source=SOURCE_B))
        store.record(release_payload([target('trial-a')], operation='release-r1', release='r-1'))
        store.record(release_payload([target('trial-a'),
                                      target('trial-b', integration=MERGE_B, source=SOURCE_B)],
                                     operation='release-r3', release='r-3'))
        result = store.record(release_payload([target('trial-a')], operation='rollback-r1',
                                              release='r-1', rollback=True, supersede=['trial-b']))
        return store, result

    def test_a_rollback_moves_the_live_release_and_supersedes_newer_tasks(self):
        store, result = self.rolled_back()
        self.assertEqual([entry['task'] for entry in result['targets']], ['trial-a'])
        self.assertFalse(result['targets'][0]['verify_only'])
        self.assertTrue(result['rollback'])
        self.assertEqual([entry['task'] for entry in result['superseded']], ['trial-b'])
        first = store.facts('trial-a')
        self.assertEqual(first['facts']['deployed']['value'], 'passed')
        self.assertEqual(first['scope']['release_id'], 'r-1')
        self.assertEqual(first['live'], 'live')
        second = store.facts('trial-b')
        self.assertEqual(second['facts']['deployed']['value'], 'unknown',
                         'a task only in the newer release reads not live after a rollback')
        self.assertEqual(second['live'], 'superseded')

    def test_brief_and_work_agree_with_the_rollback(self):
        import briefing
        import work
        store, _result = self.rolled_back()
        brief = briefing.brief(store.rows, 'trial', 'trial-b')
        self.assertIsNone(brief['deployed_delivery'])
        self.assertEqual(brief['deployed_live'], 'superseded')
        self.assertEqual(brief['lifecycle']['deployed']['value'], 'unknown')
        item = next(entry for entry in work.queue(store.rows, ACTOR, ['--mine'])['items']
                    if entry['task'] == 'trial-b')
        self.assertIsNone(item['deployed_delivery'])
        self.assertEqual(item['deployed_live'], 'superseded')
        kept = briefing.brief(store.rows, 'trial', 'trial-a')
        self.assertEqual(kept['deployed_delivery']['release_id'], 'r-1')
        self.assertEqual(kept['deployed_live'], 'live')

    def test_evidence_owed_names_the_superseded_release(self):
        store, _result = self.rolled_back()
        rows = {(group['environment'], group['release_id'], item['task']): item['live']
                for group in evidence_owed(store.rows) for item in group['tasks']}
        self.assertEqual(rows[('production', 'r-1', 'trial-a')], 'live')
        self.assertEqual(rows[('production', 'r-3', 'trial-b')], 'superseded')

    def test_a_plain_roll_forward_re_lives_a_superseded_task(self):
        import briefing
        store, _result = self.rolled_back()
        forward = release_payload([target('trial-b', integration=MERGE_B, source=SOURCE_B)],
                                  operation='forward-r3', release='r-3')
        store.record(forward)
        state = store.facts('trial-b')
        self.assertEqual(state['live'], 'live')
        self.assertEqual(state['facts']['deployed']['value'], 'passed')
        self.assertEqual(briefing.brief(store.rows, 'trial', 'trial-b')['deployed_live'], 'live')

    def test_an_older_kit_still_reads_the_release_as_it_did_before(self):
        # An additive dimension: a kit that predates `live` has no reader for it and
        # drops those events entirely, so it reads the superseded task exactly as it
        # did before (rev2 item 4). Dropping the live events here simulates that.
        store, _result = self.rolled_back()

        def is_live_event(row):
            event = lifecycle.native_event(row)
            return event is not None and event['dimension'] == 'live'

        older = [row for row in store.rows if not is_live_event(row)]
        state = next(row for row in project_facts(older) if row['id'] == 'trial-b')
        self.assertEqual(state['facts']['deployed']['value'], 'passed')
        self.assertEqual(state['live'], 'unknown')

    def test_supersede_is_allowed_on_a_plain_deploy_but_never_overlaps_a_target(self):
        # rev3 item 1: a plain deploy of a hotfix release that does not carry a live
        # task supersedes it, so the field is no longer rollback-only. rev3 item 3.1:
        # a hand-built payload may not name one task as both target and superseded.
        validate_release_payload(release_payload([target('trial-a')], supersede=['trial-b']))
        with self.assertRaisesRegex(ValueError, 'both a release target and superseded'):
            validate_release_payload(release_payload([target('trial-a')], supersede=['trial-a']))

    def live_in(self, store, task, release, environment, integration, source=SOURCE_A):
        scope_value = scope(source=source, integration=integration, release=release,
                            environment=environment)
        store.record(payload(task, 'lifecycle-scope',
                             operation='scope-%s-%s-%s' % (task, release, environment),
                             scope_value=scope_value))
        store.record(payload(task, 'deployed',
                             operation='deployed-%s-%s-%s' % (task, release, environment),
                             scope_value=scope_value))
        store.record(payload(task, 'live', value='live',
                             operation='live-%s-%s-%s' % (task, release, environment),
                             scope_value=scope_value))

    def test_a_rollback_uses_the_environments_history_not_the_current_scope(self):
        # rev3 item 1: two tasks deployed to production at r-1 and then to staging at
        # r-1 have staging as their CURRENT scope. A production rollback must still
        # find and supersede the task in production.
        store = (NativeStore(tasks=('trial-a', 'trial-b'))
                 .seed('trial-a')
                 .seed('trial-b', integration=MERGE_B, source=SOURCE_B))
        production = {'source_commit': '', 'integration_commit': RELEASE_1,
                      'release_id': 'r-1', 'environment': 'production'}
        staging = {'source_commit': '', 'integration_commit': RELEASE_1,
                   'release_id': 'r-1', 'environment': 'staging'}
        for environment, operation in ((production, 'release-prod'), (staging, 'release-staging')):
            store.record(release_payload([target('trial-a'),
                                          target('trial-b', integration=MERGE_B, source=SOURCE_B)],
                                         operation=operation, release='r-1', scope=environment))
        self.assertEqual(store.facts('trial-b')['scope']['environment'], 'staging')
        result = store.record(release_payload([], operation='rollback-prod', release='r-1',
                                              scope=production, rollback=True, supersede=['trial-b']))
        self.assertEqual([item['task'] for item in result['superseded']], ['trial-b'])
        self.assertEqual(result['superseded'][0]['scope']['environment'], 'production',
                         'the supersede fact is written under the environment live scope')

    def test_a_rollback_does_not_supersede_a_task_live_in_another_environment(self):
        # Kills the mutation that drops the environment check from the supersede
        # decision (rev3 item 4.1c): trial-b is live only in staging, so a production
        # rollback must not touch it.
        store = (NativeStore(tasks=('trial-a', 'trial-b'))
                 .seed('trial-a')
                 .seed('trial-b', integration=MERGE_B, source=SOURCE_B))
        self.live_in(store, 'trial-a', 'r-1', 'production', RELEASE_1)
        self.live_in(store, 'trial-b', 'r-3', 'staging', MERGE_B, source=SOURCE_B)
        selection = rollback_selection(
            store.rows, {'source_commit': '', 'integration_commit': RELEASE_1,
                         'release_id': 'r-1', 'environment': 'production'},
            lambda commit, release: (commit, release) == (MERGE_A, RELEASE_1))
        self.assertEqual([item['task'] for item in selection['targets']], ['trial-a'])
        self.assertEqual(selection['supersede'], [],
                         'a task live only in staging is not superseded by a production rollback')

    def test_a_rollback_to_a_never_deployed_release_is_refused(self):
        # rev3 item 3.1: rolling back to a release that was never deployed in the
        # environment is refused, on the client and at the endpoint.
        store = (NativeStore(tasks=('trial-a',)).seed('trial-a'))
        store.record(release_payload([target('trial-a')], operation='release-r3', release='r-3'))
        scope_value = {'source_commit': '', 'integration_commit': RELEASE_1,
                       'release_id': 'r-1', 'environment': 'production'}
        with self.assertRaisesRegex(ValueError, 'was never deployed'):
            rollback_selection(store.rows, scope_value, lambda commit, release: True)
        with self.assertRaisesRegex(ValueError, 'was never deployed'):
            store.record(release_payload([], release='r-1', scope=scope_value, rollback=True))

    def test_a_rollback_in_an_environment_with_nothing_deployed_is_refused(self):
        # rev3 item 3.1: with nothing deployed the "rollback" would act as a full
        # deploy, so it is refused before any write.
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        scope_value = {'source_commit': '', 'integration_commit': RELEASE_1,
                       'release_id': 'r-1', 'environment': 'production'}
        with self.assertRaisesRegex(ValueError, 'nothing is deployed in that environment'):
            rollback_selection(store.rows, scope_value, lambda commit, release: True)
        with self.assertRaisesRegex(ValueError, 'nothing is deployed in that environment'):
            store.record(release_payload([], release='r-1', scope=scope_value, rollback=True))

    def test_a_hand_built_rollback_cannot_name_a_task_twice(self):
        store = (NativeStore(tasks=('trial-a',)).seed('trial-a'))
        store.record(release_payload([target('trial-a')], operation='release-r1', release='r-1'))
        scope_value = {'source_commit': '', 'integration_commit': RELEASE_1,
                       'release_id': 'r-1', 'environment': 'production'}
        with self.assertRaisesRegex(ValueError, 'both a release target and superseded'):
            store.record(release_payload([target('trial-a')], release='r-1', scope=scope_value,
                                         rollback=True, supersede=['trial-a']))


class ReleaseQueryTests(unittest.TestCase):
    """The default command learns reverts from the endpoint (rev2 item 5)."""

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

    def revert(self, store, task, commit):
        payload = dict(schema_version=1, operation='revert-record', operation_id='revert-1',
                       task=task, contribution='1', integration_commit=commit,
                       revert_commit='b' * 40, reason='dropped', operator=OPERATOR)
        row = next(item for item in store.rows if item['id'] == task)
        cid = str(len(row.get('comments') or []) + 1)
        row.setdefault('comments', []).append(
            {'id': cid, 'author': OPERATOR, 'created_at': '2026-01-01T00:00:00Z',
             'text': lifecycle.REVERT_PREFIX + canonical_bytes(payload).decode()})
        entry = rw.revert_journal_entry(rw.JOURNAL_REVERT, payload, cid, OPERATOR, task=task,
                                        contribution='1', integration_commit=commit,
                                        revert_commit='b' * 40)
        rw.publish_revert_journal(self.journal, entry)

    def test_the_query_returns_the_host_reverted_set(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        self.revert(store, 'trial-a', MERGE_A)
        query = dict(release_payload([]), dimension=RELEASE_QUERY)
        validate_query_payload(query)
        answer = release_query(query, ACTOR, store.run, operators=[OPERATOR], journal=self.journal)
        self.assertEqual(answer['dimension'], RELEASE_QUERY)
        self.assertEqual(answer['reverted'],
                         [{'task': 'trial-a', 'integration_commit': MERGE_A}])

    def test_the_query_writes_nothing(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        before = json.dumps(store.rows)
        calls = len(store.calls)
        query = dict(release_payload([]), dimension=RELEASE_QUERY)
        release_query(query, ACTOR, store.run, operators=[OPERATOR], journal=self.journal)
        self.assertEqual(json.dumps(store.rows), before)
        self.assertEqual(store.calls[calls:], [['export', '--all']])

    def test_a_reverted_target_is_not_selected_for_a_second_release(self):
        store = (NativeStore(tasks=('trial-a', 'trial-b'))
                 .seed('trial-a').seed('trial-b', integration=MERGE_B, source=SOURCE_B))
        self.revert(store, 'trial-b', MERGE_B)
        answer = release_query(dict(release_payload([]), dimension=RELEASE_QUERY),
                               ACTOR, store.run, operators=[OPERATOR], journal=self.journal)
        reverted = {(item['task'], item['integration_commit']) for item in answer['reverted']}
        selection = release_selection(
            store.rows, {'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                         'release_id': 'r-2', 'environment': 'production'},
            lambda commit, release: True, reverted=reverted)
        self.assertEqual([item['task'] for item in selection['targets']], ['trial-a'])
        self.assertIn('reverted', [item['reason'] for item in selection['skipped']][0])


    def test_a_pre_liveness_deployment_is_reported_as_deployed(self):
        # kittrial-5bb.107 rev4 item 3 (1): the docs say a pre-liveness deployment is
        # reported as deployed, but the query asked for the live dimension alone, so
        # the legacy branch of environment_liveness could never fire and the mutation
        # removing it survived. The scope below has deployed=passed and NO live fact.
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        released(store, 'trial-a', 'r-p', 'production', MERGE_A, live=False)
        answer = release_query(dict(release_payload([]), dimension=RELEASE_QUERY),
                               ACTOR, store.run, operators=[OPERATOR], journal=self.journal)
        self.assertEqual([(item['task'], item['release_id'], item['liveness'])
                          for item in answer['live_releases']],
                         [('trial-a', 'r-p', 'deployed')],
                         'a deployment with no live fact reads as the pre-liveness value')

    def test_live_releases_cover_an_environment_that_has_live_tasks(self):
        # rev3 item 3.5: the query's environment may not be the task's newest live
        # scope; it must still report the live releases that environment has.
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.record(release_payload([target('trial-a')], operation='deploy-prod', release='r-p',
                                     scope={'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                                            'release_id': 'r-p', 'environment': 'production'}))
        store.record(release_payload([target('trial-a')], operation='deploy-staging', release='r-s',
                                     scope={'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                                            'release_id': 'r-s', 'environment': 'staging'}))
        answer = release_query(dict(release_payload([]), dimension=RELEASE_QUERY),
                               ACTOR, store.run, operators=[OPERATOR], journal=self.journal)
        self.assertEqual(answer['environment'], 'production')
        self.assertEqual({item['release_id'] for item in answer['live_releases']}, {'r-p'})


class SingleFactScopeTests(unittest.TestCase):
    """A single recorded fact must name the task's CURRENT scope (rev3 item 2)."""

    def test_a_single_fact_under_an_older_scope_is_refused(self):
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        older = scope(source=SOURCE_A, integration=MERGE_A, release='release-a', environment='staging')
        newer = scope(source=SOURCE_B, integration=MERGE_B, release='release-b', environment='production')
        store.record(payload('trial-a', 'lifecycle-scope', operation='scope-newer', scope_value=newer))
        store.record(payload('trial-a', 'integrated', operation='integrated-newer', scope_value=newer))
        with self.assertRaisesRegex(ValueError, 'set matching lifecycle scope'):
            store.record(payload('trial-a', 'tested', operation='tested-older', scope_value=older))

    def test_the_release_operation_may_still_write_under_an_older_scope(self):
        # The documented relaxation: only a release operation (a verify-only target
        # or a per-environment supersede) may write under an already-recorded scope.
        store = NativeStore(tasks=('trial-a',)).seed('trial-a')
        store.record(release_payload([target('trial-a')], operation='deploy-staging', release='r-1',
                                     scope={'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                                            'release_id': 'r-1', 'environment': 'staging'}))
        store.record(release_payload([target('trial-a')], operation='deploy-production', release='r-1',
                                     scope={'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                                            'release_id': 'r-1', 'environment': 'production'}))
        result = store.record(release_payload([target('trial-a')], operation='verify-staging',
                                              release='r-1', live_verified=True,
                                              scope={'source_commit': '', 'integration_commit': RELEASE_COMMIT,
                                                     'release_id': 'r-1', 'environment': 'staging'}))
        self.assertTrue(result['targets'][0]['verify_only'])
        self.assertIsNotNone(result['targets'][0]['live_verified'])

    def test_the_scope_token_is_memoised_across_events(self):
        import unittest.mock
        store = NativeStore(tasks=('trial-a',))
        seeded = scope()
        store.record(payload('trial-a', 'lifecycle-scope', operation='scope-a', scope_value=seeded))
        for index in range(25):
            store.record(payload('trial-a', 'deployed', operation='deploy-%d' % index,
                                 scope_value=seeded))
        real = lifecycle.hashlib.sha256
        calls = {'n': 0}
        def counting(*args, **kwargs):
            calls['n'] += 1
            return real(*args, **kwargs)
        with unittest.mock.patch.object(lifecycle.hashlib, 'sha256', counting):
            lifecycle.evidence_owed(store.rows)
        self.assertLessEqual(calls['n'], 2,
                             'the same distinct scope must not be hashed once per event')


class RollbackCommandTests(ReleaseCommandTests):
    """`release --rollback` sends the resolved targets plus the supersede list."""

    def rollback_fixture(self, root, store):
        export = Path(root) / 'issues.jsonl'
        export.write_text(''.join(json.dumps(row) + '\n' for row in store.rows), encoding='utf-8')
        request = Path(root) / 'release.json'
        request.write_text(json.dumps(release_payload([], release='r-1', rollback=True)),
                           encoding='utf-8')
        config = Path(root) / 'client.json'
        config.write_text('{}', encoding='utf-8')
        return ['lifecycle.py', 'release', '--config', str(config), '--project', 'example',
                '--actor', ACTOR, '--file', str(request), '--export', str(export), '--repo', str(root)]

    def test_the_rollback_flag_sends_targets_and_supersede(self):
        store = (NativeStore(tasks=('trial-a', 'trial-b'))
                 .seed('trial-a')
                 .seed('trial-b', integration=MERGE_B, source=SOURCE_B))
        # r-1 must already have been deployed in production: a rollback to a release
        # that was never deployed is refused (rev3 item 3.1).
        store.record(release_payload([target('trial-a')], operation='release-r1', release='r-1'))
        store.record(release_payload([target('trial-a'),
                                      target('trial-b', integration=MERGE_B, source=SOURCE_B)],
                                     operation='release-r3', release='r-3'))
        sent = []
        client = types.ModuleType('client')
        def fake_request(config, project, actor, args, action=None):
            payload = json.loads(args[0]); sent.append(payload)
            if payload['dimension'] == RELEASE_QUERY:
                return {'returncode': 0, 'stderr': '', 'stdout': json.dumps({'reverted': []})}
            return {'returncode': 0, 'stderr': '', 'stdout': json.dumps({'targets': []})}
        client.request = fake_request
        with scratch() as root:
            argv = self.rollback_fixture(root, store) + ['--rollback']
            report = json.loads(self.run_cli(argv, client))
        write = next(payload for payload in sent if payload['dimension'] == RELEASE)
        self.assertTrue(write['rollback'])
        self.assertEqual([item['task'] for item in write['targets']], ['trial-a'])
        self.assertEqual(write['supersede'], ['trial-b'])
        self.assertTrue(report['rollback'])
        self.assertEqual(report['superseded'], ['trial-b'])
