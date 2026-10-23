"""The read-only integration check (kittrial-5bb.374).

Every allowed/refused row is the writer's own decision function and sentence.
The three unchecked rows stay honest, the read writes nothing, and the next
rows name the operation identity a committed native write already recorded.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import lifecycle
import web_lifecycle as web
import web_lifecycle_marks as marks
from requirements import content_hash
from test_coordinator_rules import Chain, TASK, approve, REVIEWER

COMMIT = 'a' * 40
INTEGRATION = 'b' * 40


class IntegrationCheckTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.project = Path(self.scratch.name) / 'p'
        self.project.mkdir()
        self.chain = Chain()
        self.chain.add(approve(), 'approval', REVIEWER)
        self.rows = self.chain.rows
        self.scope = dict(source_commit=COMMIT, integration_commit=INTEGRATION,
                          release_id='', environment='synthetic')
        self.writes = 0
        self.holder = 'op'
        self.parties = lambda name: {name} if name else set()
        self.project.joinpath('.merge-context.json').write_text(json.dumps(
            dict(holder='op', task=TASK, target='main')), encoding='utf-8')

    def native_run(self, argv):
        if argv == ['export', '--all']:
            return '\n'.join(json.dumps(row) for row in self.rows)
        if argv == ['merge-slot', 'check', '--json']:
            return json.dumps(dict(available=self.holder is None, holder=self.holder))
        if argv == ['show', 'p-merge-slot', '--json']:
            return json.dumps(dict(id='p-merge-slot',
                                   status='in_progress' if self.holder else 'open',
                                   labels=['gt:slot'], assignee=None))
        self.assertEqual(argv[:2], ['set-state', TASK])
        self.writes += 1
        dimension, value = argv[2].split('=', 1)
        event = lifecycle._native_event_row(TASK + '.' + str(self.writes), TASK, dimension,
                                            value, argv[4], 'op')
        event['created_at'] = '2026-10-01T00:00:%02dZ' % self.writes
        self.rows.append(event)
        task = self.rows[0]
        task['labels'] = ([x for x in task['labels'] if not x.startswith(dimension + ':')]
                          + [dimension + ':' + value])
        return json.dumps(dict(event_id=event['id']))

    def scope_body(self, scope=None, expected=None):
        return dict(operation='scope', task=TASK, scope=copy.deepcopy(scope or self.scope),
                    expected_scope_token=expected, evidence=['synthetic scope'],
                    provenance='performed')

    def fact_body(self, dimension, value, scope=None):
        return dict(operation='fact', task=TASK, dimension=dimension, value=value,
                    scope=copy.deepcopy(scope or self.scope),
                    evidence=['synthetic evidence'], provenance='performed')

    def apply(self, body, key, **options):
        options.setdefault('parties', self.parties)
        return web.apply(body, 'op', self.native_run, self.project, 'p', key,
                         authority=lambda: True, **options)

    def check(self, **options):
        options.setdefault('parties', self.parties)
        return web.integration_check(TASK, 'op', self.native_run, self.project,
                                     project_name='p', **options)

    def scoped(self):
        return self.apply(self.scope_body(), 'scope-1')

    def test_before_merge_every_visible_check_is_allowed_and_nothing_is_written(self):
        scope_write = self.scoped()
        before = copy.deepcopy(self.rows)
        writes = self.writes
        result = self.check(target='main')
        self.assertEqual(self.rows, before)
        self.assertEqual(self.writes, writes)
        self.assertFalse(self.project.joinpath(marks.DIRECTORY).exists())
        self.assertEqual([row['check'] for row in result['checks']],
                         ['approval', 'source_commit', 'slot', 'hold', 'reverted'])
        self.assertTrue(all(row['allowed'] for row in result['checks']), result['checks'])
        self.assertTrue(result['merge_ready'])
        self.assertEqual(result['task']['id'], TASK)
        self.assertEqual(result['contribution']['commit'], COMMIT)
        self.assertEqual(result['review_state'], 'awaiting-integration')
        self.assertEqual([row['check'] for row in result['unchecked']],
                         ['base_membership', 'tests', 'pushed_result'])
        for row in result['unchecked']:
            self.assertFalse(row['known'])
            self.assertEqual(row['reason'], 'the server has no code-host access')
        steps = {step['step']: step for step in result['next']}
        self.assertEqual(steps['scope']['state'], 'committed')
        self.assertEqual(steps['scope']['operation_id'], scope_write['operation_id'])
        for name in ('reviewed', 'integrated', 'integrated-mark', 'slot-release'):
            self.assertEqual(steps[name]['state'], 'not-attempted', steps[name])
            self.assertIsNone(steps[name]['operation_id'])
        self.assertTrue(steps['integrated-mark']['automatic'])
        self.assertIn('Idempotency-Key', steps['slot-release']['operation_id_note'])

    def test_after_merge_the_committed_rows_name_their_reused_operation_identity(self):
        self.scoped()
        self.apply(self.fact_body('reviewed', 'passed'), 'reviewed-1')
        self.apply(self.fact_body('integrated', 'passed'), 'integrated-1', mark_writes=True)
        before = copy.deepcopy(self.rows)
        writes = self.writes
        result = self.check()
        self.assertEqual(self.rows, before)
        self.assertEqual(self.writes, writes)
        self.assertTrue(all(row['allowed'] for row in result['checks']), result['checks'])
        steps = {step['step']: step for step in result['next']}
        self.assertEqual(steps['scope']['state'], 'committed')
        self.assertEqual(steps['reviewed']['state'], 'committed')
        self.assertEqual(steps['reviewed']['operation_id'], 'reviewed-1')
        self.assertEqual(steps['integrated']['state'], 'committed')
        self.assertEqual(steps['integrated']['operation_id'], 'integrated-1')
        self.assertEqual(steps['integrated-mark']['state'], 'committed')
        self.assertEqual(steps['integrated-mark']['operation_id'], 'integrated-1')
        self.assertEqual(steps['slot-release']['state'], 'not-attempted')

    def test_refusals_are_answers_with_the_writers_sentence(self):
        self.scoped()
        next_scope = dict(self.scope, source_commit='c' * 40)
        self.apply(self.scope_body(next_scope, expected=content_hash(self.scope)), 'scope-2')
        result = self.check()
        rows = {row['check']: row for row in result['checks']}
        self.assertFalse(rows['source_commit']['allowed'])
        self.assertEqual(rows['source_commit']['reason'],
                         'The full source commit must match the current approved contribution')
        self.assertTrue(rows['approval']['allowed'])
        self.assertFalse(result['merge_ready'])

        self.rows[0]['comments'] = [c for c in self.rows[0]['comments'] if c['id'] != 'approval']
        rows = {row['check']: row for row in self.check()['checks']}
        self.assertFalse(rows['approval']['allowed'])
        self.assertIn('current independent approval', rows['approval']['reason'])
        self.assertFalse(self.check()['merge_ready'])

    def test_slot_refusal_reports_the_holder_and_does_not_raise(self):
        self.scoped()
        self.holder = 'other'
        row = {r['check']: r for r in self.check()['checks']}['slot']
        self.assertFalse(row['allowed'])
        self.assertEqual(row['reason'],
                         'Hold the merge slot for this task before recording reviewed or integrated facts')
        self.assertEqual(row['holder'], 'other')
        self.assertFalse(self.check()['merge_ready'])

    def test_hold_and_revert_rows_use_the_canonical_readers(self):
        self.scoped()
        with patch.object(lifecycle, 'require_unheld_fact',
                          side_effect=ValueError('HELD contribution: synthetic hold')):
            row = {r['check']: r for r in self.check()['checks']}['hold']
        self.assertFalse(row['allowed'])
        self.assertEqual(row['reason'], 'HELD contribution: synthetic hold')
        with patch.object(lifecycle, 'reverted_integrations',
                          return_value={(TASK, INTEGRATION)}):
            row = {r['check']: r for r in self.check()['checks']}['reverted']
        self.assertFalse(row['allowed'])
        self.assertEqual(row['reason'],
                         'integration commit was reverted by an operator revert record: ' + INTEGRATION)
        self.assertEqual(row['reverted_commits'], [INTEGRATION])

    def test_an_unknown_task_is_404_and_bad_fields_are_422(self):
        with self.assertRaises(web.LifecycleError) as raised:
            web.integration_check('no-such-task', 'op', self.native_run, self.project,
                                  project_name='p', parties=self.parties)
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(web.validate(dict(operation='check', task=TASK, target='main')),
                         ('check', None))
        for body in (dict(operation='check', task=TASK, target=''),
                     dict(operation='check', task=TASK, target='x' * 161),
                     dict(operation='check', task=TASK, target='main', authority=True),
                     dict(operation='check')):
            with self.subTest(body=body):
                with self.assertRaises(web.LifecycleError) as raised:
                    web.validate(body)
                self.assertEqual(raised.exception.status, 422)


if __name__ == '__main__':
    unittest.main()
