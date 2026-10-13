"""Host correction boundary, durable reading and native-first cut recovery."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import lifecycle
import lifecycle_voids as voids
from requirements import canonical_bytes, content_hash


def rows():
    scope = dict(source_commit='a' * 40, integration_commit='b' * 40,
                 release_id='', environment='synthetic')
    task = dict(id='p-task', issue_type='task', title='synthetic', status='closed',
                assignee='worker', comments=[], labels=['lifecycle-scope:' + content_hash(scope),
                                                       'integrated:passed'])
    events = []
    for number, dimension, value in [(1, 'lifecycle-scope', content_hash(scope)), (2, 'integrated', 'passed')]:
        payload = dict(schema_version=1, operation_id='fact-' + str(number), task=task['id'],
                       dimension=dimension, value=value, scope=scope, actor='op',
                       evidence=['synthetic source'], provenance='performed')
        lifecycle.validate_payload(payload)
        events.append(dict(id=task['id'] + '.' + str(number), issue_type='event',
                           title='State change: ' + dimension + ' → ' + value,
                           description='Set ' + dimension + ' to ' + value + '\n\nReason: '
                           + lifecycle.PREFIX + canonical_bytes(payload).decode(), created_by='op',
                           created_at='2026-10-01T00:00:0' + str(number) + 'Z',
                           dependencies=[dict(type='parent-child', depends_on_id=task['id'])]))
    return [task] + events


class HostCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.project = Path(self.scratch.name)
        self.rows = rows()
        self.native_writes = 0

    def payload(self, operation='void', key='void-one', target=None, actor='op'):
        return dict(schema_version=1, operation=operation, operation_id=key, actor=actor,
                    reason='Operator reviewed the source evidence.', evidence=['synthetic review'],
                    binding=voids.event_binding(self.rows[2]), target_void=target)

    def native_run(self, argv):
        if argv == ['comments', 'p-task', '--json']:
            return json.dumps(self.rows[0]['comments'])
        self.assertEqual(argv[:3], ['comments', 'add', 'p-task'])
        self.native_writes += 1
        comment = dict(id=str(self.native_writes), author=json.loads(argv[3][len(voids.PREFIX):])['actor'],
                       text=argv[3], created_at='2026-10-02T00:00:0' + str(self.native_writes) + 'Z')
        self.rows[0]['comments'].append(comment)
        return json.dumps(comment)

    def apply(self, payload, **options):
        return voids.apply(self.rows, payload['actor'], payload, self.native_run, self.project,
                           operators=options.pop('operators', ['op']), enabled=options.pop('enabled', True),
                           **options)

    def reading(self):
        return voids.context(self.rows, voids.read_journal(self.project))

    def test_read_and_off_refusal_create_no_family_and_raw_comment_is_not_authority(self):
        before = copy.deepcopy(self.rows)
        self.assertEqual(voids.read_journal(self.project)['entries'], {})
        self.assertIs(voids.snapshot(self.rows, self.project), self.rows)
        with self.assertRaisesRegex(ValueError, 'lifecycle_void_writes is off'):
            self.apply(self.payload(), enabled=False)
        self.assertEqual(self.rows, before)
        self.assertEqual(list(self.project.iterdir()), [])
        self.native_run(['comments', 'add', 'p-task', voids.PREFIX + canonical_bytes(self.payload()).decode(), '--json'])
        self.assertEqual(self.reading()['reasons'], {})
        self.assertEqual(list(self.project.iterdir()), [])

    def test_authority_before_effect_including_bound_principal_and_unreadable_registry(self):
        payload = self.payload(actor='session-one')
        with self.assertRaisesRegex(ValueError, 'currently listed operator'):
            self.apply(payload, owners={'op': 'person:owner', 'session-one': 'person:other'})
        def unreadable():
            raise ValueError('session registry cannot be read')
        with self.assertRaisesRegex(ValueError, 'registry cannot be read'):
            self.apply(payload, owners=unreadable)
        self.assertEqual(self.native_writes, 0)
        self.assertEqual(list(self.project.iterdir()), [])
        self.apply(payload, owners={'op': 'person:owner', 'session-one': 'person:owner'})
        self.assertEqual(self.reading()['reasons'], {'p-task.2': 'voided'})

    def test_void_survives_operator_replacement_and_current_operator_can_retract(self):
        self.apply(self.payload())
        # No operator/owner argument exists on the reader: authority was checked at write.
        self.assertEqual(self.reading()['reasons'], {'p-task.2': 'voided'})
        retraction = self.payload('retract', 'retract-one', 'void-one', 'replacement')
        with self.assertRaisesRegex(ValueError, 'currently listed operator'):
            self.apply(retraction, operators=['another'])
        self.apply(retraction, operators=['replacement'])
        self.assertEqual(self.reading()['reasons'], {})
        self.assertEqual(len(list((self.project / voids.DIRECTORY).glob('*.json'))), 2)
        self.assertEqual(self.native_writes, 2)

    def test_native_cut_exact_retry_publishes_missing_record_without_duplicate_native_write(self):
        payload = self.payload()
        with mock.patch.object(voids, 'publish', side_effect=OSError('synthetic publication cut')):
            with self.assertRaisesRegex(ValueError, 'uncertain for operation void-one'):
                self.apply(payload)
        self.assertEqual(self.native_writes, 1)
        self.assertEqual(self.reading()['reasons'], {})
        answer = self.apply(payload)
        self.assertTrue(answer['reconciled'])
        self.assertEqual(self.native_writes, 1)
        self.assertEqual(self.reading()['reasons'], {'p-task.2': 'voided'})

    def test_conflicting_native_identity_refuses_before_creating_family(self):
        payload = self.payload()
        self.native_run(['comments', 'add', 'p-task', voids.PREFIX + canonical_bytes(payload).decode(), '--json'])
        conflicting = dict(payload, reason='Changed content for the same identity.')
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.apply(conflicting)
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertEqual(self.native_writes, 1)

    def test_target_or_native_comment_change_stays_unknown_and_cannot_be_retracted(self):
        self.apply(self.payload())
        original = copy.deepcopy(self.rows)
        self.rows[2]['description'] += 'changed'
        self.assertEqual(self.reading()['reasons'], {'p-task.2': 'lifecycle-void-target-changed'})
        with self.assertRaisesRegex(ValueError, 'exact host-issued void'):
            self.apply(self.payload('retract', 'retract-one', 'void-one'))
        self.rows = original
        self.rows[0]['comments'][0]['text'] += 'changed'
        self.assertEqual(self.reading()['reasons'], {'p-task.2': 'lifecycle-void-journal-mismatch'})
        with self.assertRaisesRegex(ValueError, 'binding is damaged'):
            self.apply(self.payload('retract', 'retract-two', 'void-one'))
        self.assertEqual(self.native_writes, 1)

    def test_damaged_or_renamed_journal_never_silently_restores_fact(self):
        self.apply(self.payload())
        file = next((self.project / voids.DIRECTORY).glob('*.json'))
        raw = file.read_bytes()
        file.write_text('{')
        self.assertEqual(self.reading()['global_reason'], voids.UNKNOWN_JOURNAL)
        file.write_bytes(raw)
        file.rename(file.with_name('0' * 64 + '.json'))
        self.assertEqual(self.reading()['global_reason'], voids.UNKNOWN_JOURNAL)
        with self.assertRaisesRegex(ValueError, 'journal cannot be read'):
            self.apply(self.payload('retract', 'retract-one', 'void-one'))
        self.assertEqual(self.native_writes, 1)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO boundary')
    def test_fifo_journal_read_is_bounded_and_does_not_block(self):
        directory = self.project / voids.DIRECTORY
        directory.mkdir()
        os.mkfifo(directory / ('0' * 64 + '.json'))
        self.assertTrue(voids.read_journal(self.project)['invalid'])

    def test_closed_payload_and_journal_shapes_refuse_extra_authority_fields(self):
        payload = self.payload()
        for key in ('operators', 'store', 'principal', 'authority', 'attestation'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'fields'):
                self.apply(dict(payload, **{key: 'untrusted'}))
        self.assertEqual(self.native_writes, 0)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_current_historical_and_capability_readers_agree_without_mutating_native_facts(self):
        original_event = copy.deepcopy(self.rows[2])
        self.apply(self.payload())
        captured = voids.snapshot(self.rows, self.project)
        current = lifecycle.project_facts(captured)[0]['facts']['integrated']
        self.assertEqual((current['value'], current['reason']), ('unknown', 'voided'))
        historical = lifecycle.integration_evidence(captured)[0]['scopes'][0]['integrated']
        self.assertEqual((historical['value'], historical['reason']), ('unknown', 'voided'))
        from capability_verification import integrated_commits
        self.assertEqual(integrated_commits(captured), set())
        from review_state import integration
        review = integration({'commit': 'a' * 40}, lifecycle.integration_evidence(captured)[0]['scopes'])
        self.assertEqual((review['fact'], review['reason']), ('unknown', 'voided'))
        self.assertEqual(self.rows[2], original_event)
        self.assertEqual(self.rows[0]['labels'], rows()[0]['labels'])

    def test_deleted_target_does_not_revive_an_older_pass_or_retry_identity(self):
        older = copy.deepcopy(self.rows[2])
        older['id'] = 'p-task.0'
        older['description'] = older['description'].replace('fact-2', 'fact-old')
        self.rows.append(older)
        self.apply(self.payload())
        target = self.rows.pop(2)
        captured = voids.snapshot(self.rows, self.project)
        current = lifecycle.project_facts(captured)[0]['facts']['integrated']
        self.assertEqual((current['value'], current['reason']), ('unknown', 'lifecycle-void-target-changed'))
        historical = lifecycle.integration_evidence(captured)[0]['scopes'][0]['integrated']
        self.assertEqual(historical['value'], 'unknown')
        index = lifecycle._operation_index(captured)
        self.assertNotIn('fact-2', index)
        self.assertEqual(index.blocked['fact-2'], 'lifecycle-void-target-changed')
        self.assertEqual(lifecycle.native_event(target)['value'], 'passed')

    def test_voided_historical_scope_fact_does_not_change_newer_scope_fact(self):
        original_scope = lifecycle.native_event(self.rows[1])['payload']['scope']
        new_scope = dict(original_scope, source_commit='c' * 40, integration_commit='d' * 40)
        for number, dimension, value in [(3, 'lifecycle-scope', content_hash(new_scope)), (4, 'integrated', 'passed')]:
            template = copy.deepcopy(self.rows[1 if dimension == 'lifecycle-scope' else 2])
            payload = dict(lifecycle.native_event(template)['payload'], operation_id='fact-' + str(number),
                           scope=new_scope, value=value)
            template.update(id='p-task.' + str(number), title='State change: ' + dimension + ' → ' + value,
                            description='Set ' + dimension + ' to ' + value + '\n\nReason: '
                            + lifecycle.PREFIX + canonical_bytes(payload).decode())
            self.rows.append(template)
        self.rows[0]['labels'] = ['lifecycle-scope:' + content_hash(new_scope), 'integrated:passed']
        self.apply(self.payload())
        captured = voids.snapshot(self.rows, self.project)
        self.assertEqual(lifecycle.project_facts(captured)[0]['facts']['integrated']['value'], 'passed')
        history = lifecycle.integration_evidence(captured)[0]['scopes']
        self.assertEqual([(s['scope']['source_commit'], s['integrated']['value']) for s in history],
                         [('c' * 40, 'passed'), ('a' * 40, 'unknown')])
        self.assertEqual(history[1]['integrated']['reason'], 'voided')

    def test_native_json_cannot_supply_host_correction_context(self):
        self.rows[0]['lifecycle_corrections'] = dict(reasons={'p-task.2': 'voided'})
        self.assertEqual(lifecycle.project_facts(self.rows)[0]['facts']['integrated']['value'], 'passed')
        self.assertNotIn('reason', lifecycle.project_facts(self.rows)[0]['facts']['integrated'])

    def test_request_reads_family_once_and_voided_fact_retry_is_pre_effect_refused(self):
        self.apply(self.payload())
        from http_authority import NativeRunner
        def native(argv):
            self.assertEqual(argv, ['export', '--all'], 'a voided operation must not attempt a native write')
            return '\n'.join(json.dumps(row) for row in self.rows)
        runner = NativeRunner(native, project=self.project)
        with mock.patch.object(voids, 'read_journal', wraps=voids.read_journal) as read:
            first = lifecycle.read_event_rows(runner)
            lifecycle.project_facts(first)
            lifecycle.integration_evidence(first)
            lifecycle.read_event_rows(runner)
            self.assertEqual(read.call_count, 1)
        payload = lifecycle.native_event(self.rows[2])['payload']
        with self.assertRaisesRegex(ValueError, 'event that reads unknown.*voided'):
            lifecycle.apply_native(payload, 'op', runner)
        self.assertFalse(runner.attempted_write)

    def test_audit_is_read_only_paged_and_names_unclassifiable_native_rows(self):
        self.rows.append(dict(id='p-unreadable', issue_type='event', malformed=True))
        before = copy.deepcopy(self.rows)
        page = voids.audit(self.rows, operators=['op'], limit=1)
        self.assertEqual((page['total'], len(page['items']), page['omitted_after']), (2, 1, 1))
        self.assertEqual(page['unclassifiable_native_event_count'], 1)
        self.assertEqual(page['next_offset'], 1)
        self.assertEqual(voids.audit(self.rows, operators=['op'], limit=1, offset=1)['next_offset'], None)
        self.assertEqual(self.rows, before)
        self.assertEqual(list(self.project.iterdir()), [])
        with self.assertRaisesRegex(ValueError, 'limit'):
            voids.audit(self.rows, limit=101)

    def test_audit_approval_uses_recording_time_and_stored_assignee_snapshot(self):
        import review_workflow as review
        contribution = dict(schema_version=1, operation='contribute', operation_id='contribute-one',
                            task='p-task', previous=None, supersedes=None, follows=None,
                            repository='https://example.test/repository.git', commit='a' * 40,
                            base_commit='f' * 40, delivery=dict(kind='remote', remote='git@example.test:repository.git',
                            branch='worker/change'), summary='Synthetic contribution.')
        approval = dict(schema_version=1, operation='approve', operation_id='approve-one',
                        task='p-task', previous='contribution', contribution='contribution',
                        summary='Synthetic independent approval.', assignee_at_approval='worker')
        review.validate(contribution, 'p-task'); review.validate(approval, 'p-task')
        self.rows[0]['comments'] = [dict(id='contribution', author='worker', created_at='2026-09-30T23:00:00Z',
            text=review.PREFIX + canonical_bytes(contribution).decode()),
            dict(id='approval', author='reviewer', created_at='2026-09-30T23:01:00Z',
            text=review.PREFIX + canonical_bytes(approval).decode())]
        page = voids.audit(self.rows, operators=['op'])
        integrated = next(x for x in page['items'] if x['dimension'] == 'integrated')
        self.assertTrue(integrated['independent_approval_at_recording'])
        self.assertFalse(integrated['historical_principal_ownership_verified'])
        self.rows[0]['comments'][1]['created_at'] = self.rows[2]['created_at']
        integrated = next(x for x in voids.audit(self.rows)['items'] if x['dimension'] == 'integrated')
        self.assertIsNone(integrated['independent_approval_at_recording'])
        self.assertEqual(integrated['classification_reason'], 'review-record-time-unclassifiable')

    def test_admin_audit_reads_one_export_and_creates_no_files(self):
        import admin
        exported = '\n'.join(json.dumps(row) for row in self.rows)
        with mock.patch.object(admin, 'project_dir', return_value=self.project), \
                mock.patch.object(admin, 'operators', return_value=['op']), \
                mock.patch.object(admin, 'run_bd', return_value=exported) as native:
            report = admin.lifecycle_audit_command(self.project, 'pp', limit=1)
        native.assert_called_once_with(self.project, 'pp', ['export', '--all'])
        self.assertEqual(report['project'], 'pp')
        self.assertEqual(report['omitted_after'], 1)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_capacity_refusal_keeps_room_for_retracting_existing_void(self):
        with mock.patch.object(voids, 'MAX_RECORDS', 2):
            self.apply(self.payload())
            new_void = dict(self.payload(key='void-scope'), binding=voids.event_binding(self.rows[1]))
            before = copy.deepcopy(self.rows)
            with self.assertRaisesRegex(ValueError, 'keeps space for outstanding retractions'):
                self.apply(new_void)
            self.assertEqual(self.rows, before)
            self.assertEqual(self.native_writes, 1)
            self.apply(self.payload('retract', 'retract-one', 'void-one'))
            self.assertEqual(self.reading()['reasons'], {})

    def test_raw_correction_prefix_and_future_versions_are_reserved(self):
        from reserved_comments import check_raw_request
        for prefix in (voids.PREFIX, 'Kind: lifecycle-event-correction-v99\n'):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, 'lifecycle correction'):
                check_raw_request(['comments', 'add', 'p-task', prefix + '{}'], {})

    def test_release_dryrun_reports_void_reason_without_mutating_release_or_native_records(self):
        # A recorded deployment/rollback/capability verification is independent
        # native evidence; correction only changes the derived lifecycle view.
        immutable = dict(id='p-release-record', issue_type='task', comments=[dict(
            id='release-comment', author='op', text='Synthetic immutable release and rollback evidence',
            created_at='2026-10-01T01:00:00Z')])
        self.rows.append(immutable)
        self.apply(self.payload())
        before = copy.deepcopy(self.rows)
        release = dict(source_commit='a' * 40, integration_commit='b' * 40,
                       release_id='synthetic-release', environment='synthetic')
        selection = lifecycle.release_selection(voids.snapshot(self.rows, self.project), release,
                                                lambda a,b: a == b)
        self.assertEqual(selection['targets'], [])
        self.assertEqual(selection['skipped'], [{'task':'p-task','reason':'integrated evidence is unknown: voided'}])
        self.assertEqual(self.rows, before)

    def test_completed_release_batch_void_changes_derived_deployment_and_blocks_batch_retry(self):
        import test_lifecycle_release as release
        store = release.NativeStore(tasks=('p-task',))
        scope = release.scope()
        store.record_honest(release.payload('p-task', 'lifecycle-scope', operation='release-test-scope', scope_value=scope))
        store.record_honest(release.payload('p-task', 'integrated', operation='release-test-integration', scope_value=scope))
        batch = release.release_payload([release.target('p-task')], live_verified=True)
        store.record_honest(batch)
        event = next(row for row in store.rows if (lifecycle.native_event(row) or {}).get('dimension') == 'deployed')
        release_events = copy.deepcopy([row for row in store.rows if row['issue_type'] == 'event'])
        def native(argv):
            task = store.rows[0]
            if argv == ['comments', 'p-task', '--json']:
                return json.dumps(task.get('comments', []))
            if argv[:3] == ['comments', 'add', 'p-task']:
                comment = dict(id='release-void-comment', author='op', text=argv[3], created_at='2026-10-02T00:00:00Z')
                task.setdefault('comments', []).append(comment)
                return json.dumps(comment)
            return store.run(argv)
        correction = dict(self.payload(key='void-release-deployment'), binding=voids.event_binding(event))
        voids.apply(store.rows, 'op', correction, native, self.project, operators=['op'], enabled=True)
        snapshot = voids.snapshot(store.rows, self.project)
        facts = next(item for item in lifecycle.project_facts(snapshot) if item['id'] == 'p-task')
        self.assertEqual(facts['facts']['deployed']['value'], 'unknown')
        self.assertEqual(facts['facts']['deployed']['reason'], 'voided')
        before = copy.deepcopy(store.rows)
        native.lifecycle_project = self.project
        with self.assertRaisesRegex(ValueError, 'voided or mismatched lifecycle event'):
            lifecycle.apply_native(batch, release.ACTOR, native)
        self.assertEqual(store.rows, before)
        self.assertEqual([row for row in store.rows if row['issue_type'] == 'event'], release_events)

    def test_raw_fact_void_keeps_an_explicit_unknown_reason(self):
        self.rows[2]['description'] = 'Set integrated to passed\n\nReason: raw assertion'
        self.apply(self.payload())
        facts = next(row for row in lifecycle.project_facts(voids.snapshot(self.rows, self.project))
                     if row['id'] == 'p-task')['facts']['integrated']
        self.assertEqual((facts['value'], facts['reason']), ('unknown', 'voided'))

    def test_restore_uncertainty_keeps_reason_even_when_native_events_are_absent(self):
        (self.project / voids.RESTORE_MARKER).write_text('{}')
        facts = lifecycle.project_facts(voids.snapshot(self.rows[:1], self.project))[0]['facts']
        self.assertEqual(facts['integrated']['value'], 'unknown')
        self.assertEqual(facts['integrated']['reason'], voids.UNKNOWN_JOURNAL)


if __name__ == '__main__':
    unittest.main()
