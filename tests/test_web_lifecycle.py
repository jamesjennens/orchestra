"""Adapter effects over native event and review shapes, before HTTP wiring."""
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


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.project = Path(self.scratch.name) / 'p'
        self.project.mkdir()
        self.chain = Chain()
        self.chain.add(approve(), 'approval', REVIEWER)
        self.rows = self.chain.rows
        self.scope = dict(source_commit='a' * 40, integration_commit='b' * 40,
                          release_id='', environment='synthetic')
        self.writes = 0
        self.holder = 'op'
        self.slot_task = TASK
        self.allowed = True
        self.parties = lambda name: {name} if name else set()
        self.project.joinpath('.merge-context.json').write_text(json.dumps(
            dict(holder='op', task=TASK, target='main')), encoding='utf-8')

    def run(self, argv):
        if argv == ['export', '--all']:
            return '\n'.join(json.dumps(row) for row in self.rows)
        if argv == ['merge-slot', 'check', '--json']:
            return json.dumps(dict(available=self.holder is None, holder=self.holder))
        if argv == ['show', 'p-merge-slot', '--json']:
            return json.dumps(dict(id='p-merge-slot', status='in_progress' if self.holder else 'open',
                                   labels=['gt:slot'], assignee=None))
        self.assertEqual(argv[:2], ['set-state', TASK])
        self.writes += 1
        dimension, value = argv[2].split('=', 1)
        event = lifecycle._native_event_row(TASK + '.' + str(self.writes), TASK, dimension,
                                             value, argv[4], 'op')
        event['created_at'] = '2026-10-01T00:00:%02dZ' % self.writes
        self.rows.append(event)
        task = self.rows[0]
        task['labels'] = [x for x in task['labels'] if not x.startswith(dimension + ':')] + [dimension + ':' + value]
        return json.dumps(dict(event_id=event['id']))

    # unittest's run method is the harness entry point; keep the native callable separate.
    native_run = run
    del run

    def body(self, dimension='tested', value='passed'):
        return dict(operation='fact', task=TASK, dimension=dimension, value=value,
                    scope=copy.deepcopy(self.scope), evidence=['synthetic evidence'], provenance='performed')

    def scope_body(self, scope=None, expected=None):
        return dict(operation='scope', task=TASK, scope=copy.deepcopy(scope or self.scope),
                    expected_scope_token=expected, evidence=['synthetic scope'], provenance='performed')

    def apply(self, body, key='write', **options):
        return web.apply(body, 'op', self.native_run, self.project, 'p', key,
                         authority=lambda: self.allowed, parties=self.parties, **options)

    def scope_first(self):
        self.apply(self.scope_body(), 'scope')

    def refusal(self, body, status, **options):
        before = copy.deepcopy(self.rows)
        writes = self.writes
        with self.assertRaises(web.LifecycleError) as raised:
            self.apply(body, **options)
        self.assertEqual(raised.exception.status, status)
        self.assertEqual(self.rows, before)
        self.assertEqual(self.writes, writes)

    def test_cas_scope_retry_does_not_rewrite_and_native_payload_has_no_control_fields(self):
        first = self.apply(self.scope_body(), 'scope')
        repeated = self.apply(self.scope_body(), 'scope')
        self.assertTrue(repeated['reconciled'])
        self.assertEqual(first['event_id'], repeated['event_id'])
        self.assertEqual(self.writes, 1)
        payload = lifecycle.native_event(self.rows[-1])['payload']
        self.assertEqual(set(payload), lifecycle.PAYLOAD_FIELDS)
        self.refusal(self.scope_body(dict(self.scope, environment='next')), 409)

    def test_fact_matches_current_scope_and_exact_key_refuses_changed_content(self):
        self.scope_first()
        bad = self.body();bad['scope']['environment'] = 'stale'
        self.refusal(bad, 409)
        self.apply(self.body())
        self.refusal(self.body(value='failed'), 409)

    def test_all_reviewed_and_integrated_values_require_current_approval_and_slot(self):
        self.scope_first()
        self.rows[0]['comments'].pop()
        for dimension in ('reviewed', 'integrated'):
            for value in lifecycle.VALUES:
                with self.subTest(dimension=dimension, value=value):
                    self.refusal(self.body(dimension, value), 409)

    def test_current_assignee_own_party_is_refused_even_when_native_names_differ(self):
        self.scope_first()
        self.rows[0]['assignee'] = 'different-current-assignee'
        self.parties = lambda name: {'account'} if name in (REVIEWER, self.rows[0]['assignee']) else {name}
        self.refusal(self.body('reviewed'), 409)

    def test_read_note_and_pending_trigger_belong_only_to_the_current_trusted_fact(self):
        self.scope_first()
        body = self.body(value='pending');body.update(trigger='Awaiting native evidence', note='Synthetic note')
        self.apply(body, 'pending-details')
        result = web.read(TASK, 'op', self.native_run, self.project, parties=self.parties)
        self.assertEqual(result['facts']['tested']['trigger'], body['trigger'])
        self.assertEqual(result['facts']['tested']['note'], body['note'])
        self.scope['environment'] = 'next-scope'
        self.apply(self.scope_body(expected=result['scope_token']), 'next-scope')
        result = web.read(TASK, 'op', self.native_run, self.project, parties=self.parties)
        self.assertNotIn('trigger', result['facts']['tested']);self.assertNotIn('note', result['facts']['tested'])

    def test_historical_worker_issuer_matches_approver_party(self):
        self.scope_first()
        author = self.rows[0]['comments'][0]['author']
        self.parties = lambda name: {'a', 'b'} if name == author else {'b'} if name == REVIEWER else {name}
        self.refusal(self.body('integrated'), 409)

    def test_full_current_commit_and_current_slot_task_holder_are_required(self):
        self.scope_first()
        self.scope['source_commit'] = 'c' * 40
        self.apply(self.scope_body(expected=content_hash(dict(self.scope, source_commit='a' * 40))), 'new-scope')
        self.refusal(self.body('reviewed'), 409)
        self.scope['source_commit'] = 'a' * 40
        self.apply(self.scope_body(expected=content_hash(dict(self.scope, source_commit='c' * 40))), 'back-scope')
        self.holder = 'other'
        self.refusal(self.body('reviewed'), 409)
        self.holder = 'op'
        self.project.joinpath('.merge-context.json').write_text(json.dumps(dict(holder='op', task='other', target='main')))
        self.refusal(self.body('reviewed'), 409)

    def test_default_off_refuses_only_passed_integration_before_native_effect(self):
        self.scope_first()
        self.refusal(self.body('integrated'), 503)
        self.apply(self.body('reviewed'), 'reviewed')
        self.apply(self.body('integrated', 'pending'), 'pending')
        self.assertFalse(self.project.joinpath(marks.DIRECTORY).exists())

    def test_closed_task_is_writable_and_mark_binds_actual_export_not_fake_timestamp(self):
        self.rows[0]['status'] = 'closed'
        self.scope_first()
        result = self.apply(self.body('integrated'), mark_writes=True)
        journal = marks.read_journal(self.project)
        self.assertFalse(journal['invalid'])
        entry = journal['entries'][result['event_id']]
        self.assertTrue(marks.matches(entry, 'p', self.rows[-1]))
        self.assertEqual(entry['record']['created_at'], self.rows[-1]['created_at'])
        before = self.writes
        retry = self.apply(self.body('integrated'), mark_writes=False)
        self.assertEqual(result['mark_sha256'], retry['mark_sha256'])
        self.assertEqual(self.writes, before)

    def test_publication_failure_is_uncertain_and_missing_mark_with_off_retry_stays_uncertain(self):
        self.scope_first()
        with patch.object(marks, 'issue', side_effect=OSError('synthetic publication cut')):
            with self.assertRaises(web.LifecycleUncertain):
                self.apply(self.body('integrated'), mark_writes=True)
        before = self.writes
        with self.assertRaises(web.LifecycleUncertain):
            self.apply(self.body('integrated'), mark_writes=False)
        self.assertEqual(self.writes, before)
        repaired = self.apply(self.body('integrated'), mark_writes=True)
        self.assertTrue(repaired['reconciled'])
        self.assertEqual(self.writes, before)

    def test_damaged_family_or_restore_marker_refuses_before_native_write(self):
        self.scope_first()
        self.project.joinpath(marks.RESTORE_MARKER).write_text('{}')
        self.refusal(self.body('integrated'), 503, mark_writes=True)

    def test_existing_native_integration_without_mark_retains_guarded_uncertainty(self):
        from http_authority import NativeRunner, OperationJournal, run_guarded
        self.scope_first()
        with patch.object(marks, 'issue', side_effect=OSError('synthetic mark cut')):
            with self.assertRaises(web.LifecycleUncertain):
                self.apply(self.body('integrated'), mark_writes=True)
        runner = NativeRunner(self.native_run)
        request = dict(project='p', actor='op', action='web-lifecycle', operation_id='write',
                       args=[json.dumps(self.body('integrated'))])
        path = self.project / '.synthetic-operation-journal'
        answer = run_guarded(request, path,
            lambda: web.apply(self.body('integrated'), 'op', runner, self.project, 'p', 'write',
                              authority=lambda: True, parties=self.parties), runner=runner)
        self.assertEqual(answer['returncode'], 124)
        self.assertIn('outcome unknown', answer['stderr'])
        self.assertFalse(runner.attempted_write)
        self.assertEqual(OperationJournal(path).lookup('write')['state'], 'unknown')

    def test_shared_native_refusal_keeps_its_complete_sentence_before_any_write(self):
        from http_authority import NativeRunner
        self.scope_first()
        message = 'HELD contribution: ' + ('native reason to preserve in full; ' * 12)
        runner = NativeRunner(self.native_run)
        with patch.object(lifecycle, 'apply_native', side_effect=ValueError(message)):
            with self.assertRaises(web.LifecycleError) as raised:
                web.apply(self.body('tested'), 'op', runner, self.project, 'p', 'held-native',
                          authority=lambda: True, parties=self.parties)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(str(raised.exception), message)
        self.assertFalse(runner.attempted_write)

    def test_authority_is_checked_again_immediately_before_effect(self):
        self.scope_first()
        calls = []
        def authority():
            calls.append(1)
            return len(calls) == 1
        before = self.writes
        with self.assertRaises(web.LifecycleError) as raised:
            web.apply(self.body(), 'op', self.native_run, self.project, 'p', 'racing-authority',
                      authority=authority, parties=self.parties)
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(len(calls), 2)
        self.assertEqual(before, self.writes)

    def test_client_context_and_non_six_dimensions_are_closed_before_effect(self):
        for extra in ('authority', 'verified', 'actor', 'route', 'capability', 'defect_task'):
            with self.subTest(extra=extra):
                self.refusal(dict(self.body(), **{extra: True}), 422)
        for dimension in ('live', 'enabled', 'release-deploy'):
            self.refusal(self.body(dimension), 422)
        self.refusal(dict(self.body(), trigger='because'), 422)
        self.assertEqual(self.writes, 0)

    def test_read_is_useful_without_permission_or_slot_and_creates_no_family(self):
        self.scope_first()
        self.holder = None
        result = web.read(TASK, 'viewer', self.native_run, self.project, parties=self.parties)
        self.assertEqual(result['scope_token'], content_hash(self.scope))
        self.assertEqual(set(result['facts']), set(lifecycle.DIMENSIONS))
        self.assertFalse(result['scope_write']['allowed'])
        self.assertFalse(any(a['allowed'] for a in result['affordances'].values()))
        self.assertFalse(self.project.joinpath(marks.DIRECTORY).exists())


if __name__ == '__main__':
    unittest.main()
