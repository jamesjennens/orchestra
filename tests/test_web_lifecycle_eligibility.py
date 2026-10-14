"""Historical mark authority in the existing later-base and audit readers."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import lifecycle
import lifecycle_voids as voids
import review_workflow as review
import web_lifecycle_marks as marks
from http_authority import NativeRunner
from requirements import canonical_bytes, content_hash
from test_lifecycle_voids import rows as prior_rows


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'p';self.project.mkdir()
        self.rows = prior_rows()
        self.prior = dict(scope_token=content_hash(lifecycle.native_event(self.rows[1])['payload']['scope']))
        later = copy.deepcopy(prior_rows())
        self.commit = 'd' * 40
        scope = dict(source_commit='c' * 40, integration_commit=self.commit, release_id='', environment='synthetic')
        later[0].update(id='p-other', assignee='somebody-else', labels=['lifecycle-scope:' + content_hash(scope), 'integrated:passed'])
        for index,event in enumerate(later[1:],1):
            payload = lifecycle.native_event(event)['payload']
            payload.update(task='p-other', actor='web-coordinator', scope=scope,
                           operation_id='later-' + str(index))
            if payload['dimension'] == 'lifecycle-scope':payload['value'] = content_hash(scope)
            event.update(id='p-other.' + str(index), created_by='web-coordinator',
                         created_at='2026-10-03T00:00:0' + str(index) + 'Z',
                         title='State change: ' + payload['dimension'] + ' → ' + payload['value'],
                         description='Set ' + payload['dimension'] + ' to ' + payload['value'] + '\n\nReason: '
                                     + lifecycle.PREFIX + canonical_bytes(payload).decode(),
                         dependencies=[dict(type='parent-child', depends_on_id='p-other')])
        self.rows.extend(later)
        self.event = later[-1]

    def issue(self):
        return marks.issue(self.project, 'p', self.event,
                           dict(route=marks.route_for('p','p-other'), capability='coordinate', project='p'),
                           enabled=True)

    def bases(self, rows=None, operators=(), journal=True):
        return review.recorded_integrations(self.rows if rows is None else rows, 'p-task', self.prior,
                                            operators=list(operators), owners={}, journal=self.project if journal else None)

    def test_existing_operator_eligibility_and_legacy_native_trust_stay_intact(self):
        old = self.bases(operators=['web-coordinator'])
        self.assertEqual(old[0][self.commit], 'later');self.assertEqual(old[1], self.commit)
        denied = self.bases()
        self.assertEqual(denied[0][self.commit][0], 'unlisted');self.assertIsNone(denied[1])
        self.assertEqual(lifecycle.project_facts(self.rows)[-1]['facts']['integrated']['value'], 'passed')
        self.assertFalse(self.project.joinpath(marks.DIRECTORY).exists())

    def test_issued_mark_is_a_later_base_after_operator_replacement_without_web_store(self):
        self.issue()
        with patch('http_authority.read_state', side_effect=AssertionError('Historical reader asked for present grants')):
            found = self.bases(operators=['replacement'])
        self.assertEqual(found[0][self.commit], 'later');self.assertEqual(found[1], self.commit)

    def test_plain_export_and_untrusted_row_metadata_cannot_invent_mark_authority(self):
        entry = self.issue()
        raw = copy.deepcopy(self.rows)
        raw[-1]['web_lifecycle_mark_journal'] = dict(entries={self.event['id']: entry}, invalid=[])
        denied = self.bases(rows=raw,journal=False)
        self.assertEqual(denied[0][self.commit][0], 'unlisted');self.assertIsNone(denied[1])
        self.assertIsNone(self.bases(journal=False)[1])

    def test_host_snapshot_reads_one_family_per_snapshot_and_request(self):
        self.issue()
        runner = NativeRunner(lambda _: '\n'.join(json.dumps(row) for row in self.rows), project=self.project)
        actual = marks.read_journal
        with patch.object(marks,'read_journal',wraps=actual) as read:
            snap = lifecycle.read_event_rows(runner)
            self.bases(rows=snap);self.bases(rows=snap)
            lifecycle.read_event_rows(runner)
            self.assertEqual(read.call_count, 1)
        self.assertIsInstance(snap,voids.Snapshot)

    def test_changed_native_binding_and_cross_project_copy_lose_later_base(self):
        self.issue()
        for field,value in [('created_by','copied-author'),('created_at','2026-10-04T00:00:00Z'),
                            ('title','State change: integrated → changed'),
                            ('description',self.event['description']+' changed')]:
            changed = copy.deepcopy(self.rows);changed[-1][field] = value
            with self.subTest(field=field):self.assertIsNone(self.bases(rows=changed)[1])
        foreign = Path(self.temp.name) / 'foreign';foreign.mkdir()
        marks.restore_files(foreign,marks.backup_files(self.project,self.rows),self.rows)
        self.assertIsNone(review.recorded_integrations(self.rows,'p-task',self.prior,operators=[],owners={},journal=foreign)[1])

    def test_damaged_or_incomplete_family_confers_none_and_does_not_erase_legacy_facts(self):
        self.issue();self.project.joinpath(marks.RESTORE_MARKER).write_text('{}')
        self.assertIsNone(self.bases()[1])
        snap = lifecycle.host_snapshot(self.rows,project=self.project)
        self.assertEqual(lifecycle.project_facts(snap)[-1]['facts']['integrated']['value'],'passed')
        self.assertEqual(self.bases(operators=['web-coordinator'])[1],self.commit)

    def test_void_context_survives_mark_snapshot_and_never_resurrects_corrections(self):
        self.issue()
        corrected = voids.Snapshot(self.rows,dict(entries={},reasons={self.event['id']:'voided'},
            task_reasons={},invalid=[],retracted=set(),global_reason=None))
        snap = lifecycle.host_snapshot(corrected,project=self.project)
        self.assertEqual(lifecycle.project_facts(snap)[-1]['facts']['integrated']['value'],'unknown')
        self.assertIsNone(self.bases(rows=snap)[1])
        self.assertEqual(snap.lifecycle_corrections,corrected.lifecycle_corrections)

    def test_audit_names_issued_authority_without_relabelling_it_as_present_operator(self):
        self.issue();snap=lifecycle.host_snapshot(self.rows,project=self.project)
        item=next(i for i in voids.audit(snap,operators=[],owners={})['items'] if i['event_id']==self.event['id'])
        self.assertFalse(item['recorder_is_listed_operator'])
        self.assertTrue(item['recorder_has_web_coordinate_mark']);self.assertTrue(item['recorder_eligible'])
        self.assertFalse(item['historical_principal_ownership_verified'])


if __name__=='__main__':unittest.main()
