"""A release plans from a fresh endpoint read, and its refusals say what was stale (kittrial-5bb.313).

POSIX + real bd, like ``test_web_release_native.py``: the endpoint imports fcntl, so the
module skips where it cannot load, and without a pinned bd.
"""
import contextlib
import io
import json
import re
import sys
import types
import unittest
import uuid
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import test_bd_label_aliases as fixture
from test_coordinator_rules_endpoint import RulesHelpers, SCOPE
import lifecycle

TARGETS = 15


@unittest.skipIf(fixture.endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no pinned native bd (set ORCHESTRA_BD_BIN)')
class FreshReadReleaseTests(RulesHelpers, unittest.TestCase):
    def integrated(self):
        task = self.task('fresh read ' + uuid.uuid4().hex[:8], assignee='w1')
        self.assertEqual(self.lifecycle('w1', 'lifecycle-scope', task)['returncode'], 0)
        self.assertEqual(self.lifecycle('ops', 'integrated', task)['returncode'], 0)
        return task

    def payload(self, tasks, operation='rel-1', **changes):
        body = dict(schema_version=1, operation_id=operation, dimension=lifecycle.RELEASE,
                    value='passed',
                    scope=dict(source_commit='', integration_commit='c' * 40,
                               release_id='r-' + operation, environment='prod'),
                    evidence=['synthetic release evidence'], provenance='performed',
                    actor='ops', live_verified=False,
                    targets=[dict(task=t, source_commit=SCOPE['source_commit'],
                                  integration_commit=SCOPE['integration_commit']) for t in tasks])
        body.update(changes)
        return body

    def send(self, payload, actor='ops'):
        return self.endpoint('lifecycle', actor, [json.dumps(payload)])

    def facts(self, task):
        return next(s for s in lifecycle.project_facts(self.rows()) if s['id'] == task)

    # -- the command, through a stand-in client that reaches the real endpoint ----------
    def client(self):
        calls = []
        test = self

        def request(config, project, actor, args, action='bd', path=None):
            calls.append((action, list(args)))
            try:
                return fixture.endpoint.execute(test.root, {'project': 'pp', 'actor': actor,
                                                            'action': action, 'args': list(args)})
            except ValueError as error:
                return {'returncode': 1, 'stdout': '', 'stderr': str(error)}
        module = types.ModuleType('client')
        module.request = request
        module.calls = calls
        return module

    def run_release(self, extra=(), data=None, catch=False):
        stand_in = self.client()
        previous = sys.modules.get('client')
        sys.modules['client'] = stand_in
        original_argv, original_ancestry = sys.argv, lifecycle.git_ancestry
        lifecycle.git_ancestry = lambda repo: (lambda commit, release: True)
        scratch = self.project / 'release-request.json'
        scratch.write_text(json.dumps(data or self.payload([])), encoding='utf-8')
        config = self.project / 'client.json'
        config.write_text('{}', encoding='utf-8')
        sys.argv = ['lifecycle.py', 'release', '--config', str(config), '--project', 'pp',
                    '--actor', 'ops', '--file', str(scratch), '--repo', str(self.project), *extra]
        out, error = io.StringIO(), None
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                lifecycle.main()
        except SystemExit as exc:
            if not catch:
                raise
            error = str(exc)
        finally:
            sys.argv, lifecycle.git_ancestry = original_argv, original_ancestry
            if previous is None:
                sys.modules.pop('client', None)
            else:
                sys.modules['client'] = previous
        return out.getvalue(), error, stand_in.calls

    def export_file(self):
        path = self.project / 'stale-export.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in self.rows()), encoding='utf-8')
        return str(path)

    # -- B: one command from a fresh read ------------------------------------------------
    def test_one_command_releases_fifteen_targets_after_other_writes_without_a_retry(self):
        first = [self.integrated() for _ in range(TARGETS - 1)]
        last = [self.integrated() for _ in range(3)]
        # Other writes land first: another release carries two targets and a plain
        # fact lands on a third.
        self.assertEqual(self.send(self.payload(first[2:4], 'rel-0'))['returncode'], 0)
        self.assertEqual(self.lifecycle('ops', 'tested', first[5])['returncode'], 0)
        out, error, calls = self.run_release(data=self.payload(first + last))
        report = json.loads(out)
        self.assertIsNone(error)
        self.assertEqual((report['groups_completed'], report['groups_total']), (1, 1), report)
        carried = first[2:4]
        wanted = [t for t in first + last if t not in carried]
        self.assertEqual(len(wanted), TARGETS)
        self.assertEqual(sorted(item['task'] for item in report['targets']), sorted(wanted))
        for task in wanted:
            state = self.facts(task)
            self.assertEqual(state['scope']['release_id'], 'r-rel-1')
            self.assertEqual(state['live'], 'live')

    def test_the_export_file_path_keeps_its_old_meaning(self):
        first = [self.integrated() for _ in range(TARGETS - 3)]
        stale = self.export_file()
        last = [self.integrated() for _ in range(3)]
        out, error, calls = self.run_release(['--export', stale], data=self.payload(first + last))
        report = json.loads(out)
        # The file knows only the first twelve; the last three are skipped, as before.
        self.assertEqual(sorted(item['task'] for item in report['targets']), sorted(first))
        self.assertEqual(sorted(item['task'] for item in report['skipped']), sorted(last))
        self.assertEqual([c for c in calls if c[0] == 'bd'], [], 'no read is made when a file is given')

    def test_the_live_read_uses_only_the_callers_existing_bd_verbs(self):
        task = self.integrated()
        self.assertEqual(self.contribute(task, 'w1')['returncode'], 0)
        out, error, calls = self.run_release(['--dry-run'], data=self.payload([task]))
        reads = [args for action, args in calls if action == 'bd']
        self.assertEqual({args[0] for args in reads}, {'list', 'show'})
        self.assertEqual({action for action, args in calls}, {'bd'})
        self.assertFalse([args for args in reads if 'export' in args])

    def test_the_live_rows_plan_exactly_as_the_export_does(self):
        tasks = [self.integrated() for _ in range(4)]
        made = self.contribute(tasks[0], 'w1')
        self.assertEqual(made['returncode'], 0, made)
        stand_in = self.client()
        rows = lifecycle.read_live_rows(
            lambda argv: stand_in.request({}, 'pp', 'ops', argv, action='bd')['stdout'])
        export = self.rows()
        self.assertEqual({r['id'] for r in rows}, {r['id'] for r in export})
        self.assertEqual(lifecycle.project_facts(rows), lifecycle.project_facts(export))
        self.assertEqual(lifecycle.current_contribution_commits(rows),
                         lifecycle.current_contribution_commits(export))
        self.assertEqual(lifecycle.scoped_evidence(rows, ('integrated',)),
                         lifecycle.scoped_evidence(export, ('integrated',)))

    def test_a_row_that_vanishes_between_the_two_reads_refuses(self):
        listing = json.dumps([{'id': 'pp-1', 'comment_count': 1}, {'id': 'pp-2', 'comment_count': 1}])

        def call(argv):
            return listing if argv[0] == 'list' else json.dumps([{'id': 'pp-1', 'comments': []}])
        with self.assertRaisesRegex(ValueError, 'changed while it was read.*pp-2'):
            lifecycle.read_live_rows(call)

    def test_a_failed_read_refuses_instead_of_planning_from_a_part(self):
        def call(argv):
            raise ValueError('endpoint down')
        with self.assertRaisesRegex(ValueError, 'the project task list cannot be read'):
            lifecycle.read_live_rows(call)

    # -- A: the refusals name what was stale --------------------------------------------
    def test_a_moved_verification_binding_names_the_task_both_scopes_and_the_view_time(self):
        task = self.integrated()
        self.assertEqual(self.send(self.payload([task], 'rel-9'))['returncode'], 0)
        bound = dict(source_commit=SCOPE['source_commit'], integration_commit=SCOPE['integration_commit'],
                     release_id='r-rel-9', environment='prod')
        self.assertEqual(self.send(self.payload([task], 'rel-10'))['returncode'], 0)
        before = self.rows()
        body = self.payload([task], 'rel-v', live_verified=True)
        body['targets'][0]['verify_scope'] = bound
        with self.assertRaises(ValueError) as caught:
            self.send(body)
        text = str(caught.exception)
        self.assertIn('no longer live', text)
        self.assertIn(task, text)
        self.assertIn('r-rel-9', text, 'the scope the request bound')
        self.assertIn('r-rel-10', text, 'the scope that is live now')
        self.assertRegex(text, r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')
        self.assertEqual(self.rows(), before, 'a refusal writes nothing')

    def test_a_target_that_stopped_being_integrated_still_refuses_and_names_it(self):
        tasks = [self.integrated() for _ in range(TARGETS)]
        self.assertEqual(self.lifecycle('ops', 'integrated', tasks[7], value='failed')['returncode'], 0)
        before = self.rows()
        with self.assertRaises(ValueError) as caught:
            self.send(self.payload(tasks))
        text = str(caught.exception)
        self.assertIn('not integrated at that commit', text)
        self.assertIn(tasks[7], text)
        self.assertIn(SCOPE['integration_commit'], text, 'the commit the request named')
        self.assertRegex(text, r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')
        self.assertEqual(self.rows(), before, 'a refusal writes nothing, the other 14 included')

    def test_a_caller_without_release_authority_learns_no_scope_from_the_refusal(self):
        self.deployment(operators=['ops'], lifecycle_authors=True)
        task = self.integrated()
        self.assertEqual(self.send(self.payload([task], 'rel-9'))['returncode'], 0)
        body = self.payload([task], 'rel-v', live_verified=True, actor='w1')
        body['targets'][0]['verify_scope'] = dict(
            source_commit=SCOPE['source_commit'], integration_commit=SCOPE['integration_commit'],
            release_id='r-other', environment='prod')
        with self.assertRaises(Exception) as caught:
            self.send(body, actor='w1')
        text = str(caught.exception)
        self.assertNotIn('r-rel-9', text)
        self.assertNotIn('r-other', text)
        self.assertNotIn('no longer live', text)

if __name__ == '__main__':
    unittest.main()
