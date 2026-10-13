"""Second full review: legacy offline answers and truthful journal consultation."""
import copy
import json
import unittest
from unittest import mock

import lifecycle
import lifecycle_voids as voids
import test_lifecycle_voids_review as reviewed


class OfflineRoundTwoTests(unittest.TestCase):
    setUp=reviewed.VoidReviewTests.setUp
    payload=reviewed.VoidReviewTests.payload
    native_run=reviewed.VoidReviewTests.native_run
    apply=reviewed.VoidReviewTests.apply
    cli=reviewed.VoidReviewTests.cli

    def test_no_journal_no_correction_preserves_every_main_fact(self):
        expected=lifecycle.project_facts(self.rows)
        got,warning=self.cli('list')
        self.assertEqual(len(warning.splitlines()),1)
        for fact in got:
            self.assertFalse(fact.pop('corrections_consulted',True))
            self.assertIn('not consulted',fact.pop('correction_note',''))
        self.assertEqual(got,expected)

    def release(self,*journal):
        cfg=self.project/'client.json';cfg.write_text('{}')
        data=dict(schema_version=1,operation_id='release-own',dimension=lifecycle.RELEASE,value='passed',
                  actor='op',scope=dict(source_commit='',integration_commit='c'*40,release_id='synthetic',environment='synthetic'),
                  evidence=['synthetic'],provenance='performed',targets=[],live_verified=False)
        file=self.project/'release.json';file.write_text(json.dumps(data))
        with mock.patch.object(lifecycle,'git_ancestry',return_value=lambda a,b:True):
            return self.cli('release','--dry-run','--file',str(file),'--config',str(cfg),
                            '--project','pp','--actor','op','--repo',str(self.project),*journal)[0]

    def test_no_journal_no_correction_keeps_release_target(self):
        report=self.release()
        self.assertFalse(report.get('corrections_consulted',True))
        self.assertEqual([t['task'] for t in report['targets']],['p-task'])

    def test_only_the_event_named_by_exported_comment_becomes_unknown(self):
        self.apply(self.payload())
        got,_=self.cli('list')
        self.assertFalse(got[0].get('corrections_consulted',True))
        fact=got[0]['facts']['integrated']
        self.assertEqual(fact.get('reason'),'lifecycle-corrections-not-consulted')
        self.assertEqual(fact['value'],'unknown')
        context=lifecycle.offline_snapshot(self.rows).lifecycle_corrections
        self.assertIsNone(context['global_reason'])
        self.assertEqual(context['reasons'],{'p-task.2':'lifecycle-corrections-not-consulted'})
        self.assertEqual(self.release()['targets'],[])
        debt,warning=self.cli('evidence-owed')
        self.assertEqual(debt,[])  # This fixture never deployed anything.
        self.assertIn('not consulted',warning)

    def test_nonexistent_journal_is_an_error_for_list_release_and_owed(self):
        missing=self.project/'absent'
        for command in ('list','evidence-owed'):
            with self.subTest(command=command),self.assertRaisesRegex(SystemExit,'existing project directory'):
                self.cli(command,'--journal',str(missing))
        with self.assertRaisesRegex(SystemExit,'existing project directory'):
            self.release('--journal',str(missing))
        self.assertFalse(missing.exists())

    def test_another_projects_family_does_not_claim_consultation(self):
        self.apply(self.payload())
        other=self.project/'foreign';other.mkdir()
        entry=copy.deepcopy(next(iter(voids.read_journal(self.project)['entries'].values())))
        entry['comment_id']='a-comment-not-in-this-export'
        voids.publish(other,entry)
        got,_=self.cli('list','--journal',str(other))
        self.assertFalse(got[0].get('corrections_consulted',True))
        self.assertEqual(got[0]['facts']['integrated'].get('reason'),'lifecycle-corrections-not-consulted')
        report=self.release('--journal',str(other))
        self.assertFalse(report.get('corrections_consulted',True));self.assertEqual(report['targets'],[])
        self.assertEqual(self.cli('evidence-owed','--journal',str(other))[0],[])

    def test_folder_without_family_or_project_identity_does_not_claim_consultation(self):
        self.apply(self.payload())
        folder=self.project/'empty';folder.mkdir()
        got,_=self.cli('list','--journal',str(folder))
        self.assertFalse(got[0].get('corrections_consulted',True))
        self.assertEqual(got[0]['facts']['integrated'].get('reason'),'lifecycle-corrections-not-consulted')
        report=self.release('--journal',str(folder))
        self.assertFalse(report.get('corrections_consulted',True));self.assertEqual(report['targets'],[])

    def test_absence_established_at_the_named_native_project_is_consulted(self):
        metadata=self.project/'.beads';metadata.mkdir();(metadata/'metadata.json').write_text(json.dumps({'database':'p'}))
        got,_=self.cli('list','--journal',str(self.project))
        self.assertTrue(got[0].get('corrections_consulted',False))
        self.assertEqual(got[0]['facts']['integrated']['value'],'passed')
        (metadata/'metadata.json').write_text(json.dumps({'database':'another'}))
        got,_=self.cli('list','--journal',str(self.project))
        self.assertFalse(got[0].get('corrections_consulted',True))

    def test_correct_family_consultation_survives_target_change(self):
        self.apply(self.payload());self.rows[2]['title']='changed after void'
        got,_=self.cli('list','--journal',str(self.project))
        self.assertTrue(got[0].get('corrections_consulted',False))
        self.assertEqual(got[0]['facts']['integrated'].get('reason'),'lifecycle-void-target-changed')

    def test_zero_questionable_mutant_is_killed_by_exact_count(self):
        report=voids.audit(self.rows,operators=['op'])
        self.assertEqual(report.get('questionable_fact_count'),1)
        self.assertEqual(report.get('questionable_native_record_count'),1)
        self.assertEqual(sum(x['questionable'] for x in report['items']),1)

    def test_exported_correction_of_a_missing_target_does_not_revive_a_pass(self):
        self.apply(self.payload());self.rows.pop(2)
        fact=self.cli('list')[0][0]['facts']['integrated']
        self.assertEqual(fact['value'],'unknown')
        self.assertEqual(fact.get('reason'),'lifecycle-corrections-not-consulted')

    def deployed_with_comment(self,dimension):
        from test_lifecycle_release import NativeStore,payload,scope
        from requirements import canonical_bytes
        store=NativeStore(tasks=('trial-a',)).seed('trial-a')
        production=scope(release='synthetic',environment='synthetic')
        store.record(payload('trial-a','lifecycle-scope',operation='deploy-scope',scope_value=production))
        store.record(payload('trial-a','deployed',operation='deploy',scope_value=production))
        self.rows=store.rows
        event=next(r for r in self.rows if (lifecycle.native_event(r) or {}).get('dimension')==dimension)
        correction=dict(schema_version=1,operation='void',operation_id='synthetic-owed-void',actor='op',
                        reason='Synthetic offline debt correction.',evidence=['synthetic review'],
                        binding=voids.event_binding(event),target_void=None)
        voids.validate_payload(correction)
        task=next(r for r in self.rows if r['id']=='trial-a')
        task.setdefault('comments',[]).append(dict(id='synthetic-comment',author='op',
            created_at='2026-10-03T00:00:00Z',text=voids.PREFIX+canonical_bytes(correction).decode()))

    def test_unconsulted_unrelated_correction_keeps_known_evidence_owed(self):
        self.deployed_with_comment('integrated')
        try:
            report,warning=self.cli('evidence-owed')
        except SystemExit as failure:
            self.fail('An unrelated correction must not refuse known debt: '+str(failure))
        self.assertTrue(report)
        self.assertEqual(report[0]['tasks'][0]['deployed'],'passed')
        self.assertFalse(report[0].get('corrections_consulted',True))
        self.assertIn('not consulted',warning)

    def test_unconsulted_deployment_correction_cannot_hide_owed_release_as_empty(self):
        self.deployed_with_comment('deployed')
        with self.assertRaisesRegex(SystemExit,'evidence owed is unknown'):
            self.cli('evidence-owed')


if __name__=='__main__':unittest.main()
