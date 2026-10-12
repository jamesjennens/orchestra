"""F0 review regressions: actual derived readers, retained corrections and cut refusal."""
import copy
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import lifecycle
import lifecycle_voids as voids
from requirements import canonical_bytes,content_hash
import test_lifecycle_voids as core


class VoidReviewTests(unittest.TestCase):
    setUp=core.HostCorrectionTests.setUp
    payload=core.HostCorrectionTests.payload
    native_run=core.HostCorrectionTests.native_run
    apply=core.HostCorrectionTests.apply
    reading=core.HostCorrectionTests.reading

    def test_binding_of_comment_author_and_time_and_event_title_parent_and_time(self):
        self.apply(self.payload())
        original=copy.deepcopy(self.rows)
        changes=[('comment-author',lambda r:r[0]['comments'][0].update(author='other')),
                 ('comment-time',lambda r:r[0]['comments'][0].update(created_at='2026-10-03T00:00:00Z')),
                 ('event-title',lambda r:r[2].update(title='different lifecycle title')),
                 ('event-parent',lambda r:r[2]['dependencies'].append(dict(type='parent-child',depends_on_id='p-task'))),
                 ('event-time',lambda r:r[2].update(created_at='2026-10-04T00:00:00Z'))]
        for name,change in changes:
            with self.subTest(binding=name):
                self.rows=copy.deepcopy(original);change(self.rows)
                value=lifecycle.project_facts(voids.snapshot(self.rows,self.project))[0]['facts']['integrated']
                self.assertEqual(value['value'],'unknown')
                self.assertEqual(value['reason'],'lifecycle-void-journal-mismatch' if name.startswith('comment')
                                 else 'lifecycle-void-target-changed')
        self.assertEqual(self.native_writes,1)

    def test_native_binding_hash_covers_title_parent_and_time_exactly(self):
        row=self.rows[2]
        bound={key:row.get(key) for key in ('id','issue_type','title','description','created_by','created_at')}
        bound['parents']=[{'depends_on_id':d.get('depends_on_id'),'type':d.get('type')}
                          for d in row['dependencies'] if d.get('type')=='parent-child']
        actual=voids.event_binding(row)
        self.assertEqual(actual['native_sha256'],content_hash(bound))
        self.assertEqual(actual['created_at'],row['created_at'])

    def test_render_preserves_void_truth_and_reason_and_review_projection(self):
        import render,review_workflow as reviews
        con=dict(schema_version=1,operation='contribute',operation_id='contribute',task='p-task',
                 previous=None,supersedes=None,follows=None,repository='https://example.test/repo.git',
                 commit='a'*40,base_commit='f'*40,delivery=dict(kind='remote',remote='git@example.test:repo.git',branch='own/change'),summary='Synthetic.')
        approve=dict(schema_version=1,operation='approve',operation_id='approve',task='p-task',
                     previous='con',contribution='con',summary='Synthetic independent approval.',assignee_at_approval='worker')
        self.rows[0]['comments']=[dict(id='con',author='worker',created_at='2026-09-30T23:00:00Z',text=reviews.PREFIX+canonical_bytes(con).decode()),
                                  dict(id='approve',author='reviewer',created_at='2026-09-30T23:01:00Z',text=reviews.PREFIX+canonical_bytes(approve).decode())]
        self.apply(self.payload())
        for row in self.rows:row.setdefault('status','closed')
        render.render(voids.snapshot(self.rows,self.project),self.project/'views',operators=['op'])
        page=(self.project/'views/CURRENT.md').read_text()
        self.assertIn('[unknown](jobs/p-task.2.md) (voided)',page)
        self.assertIn('awaiting-integration',page)
        self.assertNotIn('| p-task | worker | closed | integrated |',page)

    def cli(self,*args):
        exported=self.project/'export.jsonl';exported.write_text(''.join(json.dumps(r)+'\n' for r in self.rows))
        out=io.StringIO();err=io.StringIO()
        with mock.patch.object(sys,'argv',['lifecycle.py',*args,'--export',str(exported)]),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            lifecycle.main()
        return json.loads(out.getvalue()),err.getvalue()

    def test_offline_list_reads_named_family_and_does_not_claim_unconsulted_pass(self):
        self.apply(self.payload())
        facts,_=self.cli('list','--journal',str(self.project))
        self.assertEqual((facts[0]['facts']['integrated']['value'],facts[0]['facts']['integrated']['reason']),('unknown','voided'))
        facts,warning=self.cli('list')
        self.assertFalse(facts[0]['corrections_consulted'])
        self.assertEqual(facts[0]['facts']['integrated']['value'],'unknown')
        self.assertEqual(facts[0]['facts']['integrated']['reason'],'lifecycle-corrections-not-consulted')
        self.assertIn('not consulted',warning)

    def test_offline_dry_run_excludes_voided_and_unconsulted_targets(self):
        self.apply(self.payload())
        cfg=self.project/'client.json';cfg.write_text('{}')
        data=dict(schema_version=1,operation_id='release-own',dimension=lifecycle.RELEASE,value='passed',
                  actor='op',scope=dict(source_commit='',integration_commit='c'*40,release_id='synthetic',environment='synthetic'),
                  evidence=['synthetic'],provenance='performed',targets=[],live_verified=False)
        payload=self.project/'release.json';payload.write_text(json.dumps(data))
        args=['release','--dry-run','--file',str(payload),'--config',str(cfg),'--project','pp','--actor','op','--repo',str(self.project)]
        with mock.patch.object(lifecycle,'git_ancestry',return_value=lambda a,b:True):
            report,_=self.cli(*args,'--journal',str(self.project))
            self.assertEqual(report['targets'],[])
            self.assertTrue(report['corrections_consulted'])
            self.assertTrue(any('voided' in r['reason'] for r in report['skipped']))
            report,_=self.cli(*args)
            self.assertEqual(report['targets'],[])
            self.assertFalse(report['corrections_consulted'])
            self.assertIn('not consulted',report['correction_note'])

    def test_audit_retains_retracted_and_incomplete_corrections_with_global_counts(self):
        self.apply(self.payload());self.apply(self.payload('retract','retract-one','void-one'))
        pending=self.payload(key='cut-two')
        with mock.patch.object(voids,'publish',side_effect=OSError('synthetic cut')),self.assertRaisesRegex(ValueError,'uncertain'):
            self.apply(pending)
        captured=voids.snapshot(self.rows,self.project)
        page=voids.audit(captured,operators=['op'],limit=1)
        self.assertEqual(page['correction_count'],3)
        self.assertEqual(page['retracted_void_count'],1)
        self.assertEqual(page['incomplete_correction_count'],1)
        self.assertEqual(page['correction_status'],'incomplete')
        pages=[voids.audit(captured,operators=['op'],limit=1,offset=i) for i in range(3)]
        entries=[c for p in pages for c in p['corrections']]
        self.assertEqual({c['status'] for c in entries},{'retracted','retraction','incomplete'})
        for c in entries:
            self.assertEqual(c['operator'],'op');self.assertTrue(c['created_at']);self.assertTrue(c['reason']);self.assertTrue(c['evidence'])
        self.assertEqual(len({p['questionable_fact_count'] for p in pages}),1)
        self.assertEqual(pages[-1]['next_offset'],None)

    def test_cut_key_cannot_publish_second_void_after_other_key_succeeds(self):
        first=self.payload(key='first-cut')
        with mock.patch.object(voids,'publish',side_effect=OSError('synthetic cut')),self.assertRaisesRegex(ValueError,'uncertain'):
            self.apply(first)
        self.apply(self.payload(key='second-published'))
        before=copy.deepcopy(self.rows)
        with self.assertRaisesRegex(ValueError,'already voided under operation second-published'):
            self.apply(first)
        self.assertEqual(self.rows,before)
        self.assertEqual(set(voids.read_journal(self.project)['entries']),{'second-published'})
        self.apply(self.payload('retract','retract-second','second-published'))
        self.assertEqual(lifecycle.project_facts(voids.snapshot(self.rows,self.project))[0]['facts']['integrated']['value'],'passed')
        self.assertEqual(voids.audit(voids.snapshot(self.rows,self.project))['incomplete_correction_count'],1)

    def test_whole_family_omission_refuses_and_retains_unknown_restore(self):
        self.apply(self.payload())
        dest=self.project/'omitted';dest.mkdir()
        with self.assertRaisesRegex(ValueError,'backup carries no VOID'):
            voids.restore_files(dest,{},self.rows)
        self.assertTrue((dest/voids.RESTORE_MARKER).exists())
        fact=lifecycle.project_facts(voids.snapshot(self.rows,dest))[0]['facts']['integrated']
        self.assertEqual((fact['value'],fact['reason']),('unknown',voids.UNKNOWN_JOURNAL))
        with self.assertRaisesRegex(ValueError,'unreadable/incomplete'):
            voids.backup_files(dest,self.rows)
        audit=voids.audit(voids.snapshot(self.rows,dest))
        self.assertEqual(audit['incomplete_correction_count'],1)
        self.assertEqual(audit['correction_status'],'damaged')

    def test_wrong_task_host_binding_has_reason_without_duplicate_event_rows(self):
        self.apply(self.payload())
        file=next((self.project/voids.DIRECTORY).glob('*.json'));entry=json.loads(file.read_text())
        payload=copy.deepcopy(entry['payload']);payload['binding']['task']='p-other'
        payload['binding']['payload']['task']='p-other';payload['binding']['payload_sha256']=content_hash(payload['binding']['payload'])
        entry.update(payload=payload,sha256=content_hash(payload))
        file.unlink();(file.parent/(entry['sha256']+'.json')).write_bytes(voids.entry_bytes(entry))
        other=copy.deepcopy(self.rows[0]);other.update(id='p-other',comments=[]);self.rows.append(other)
        captured=voids.snapshot(self.rows,self.project);events=lifecycle.classified_events(captured)
        self.assertEqual(len([e for e in events if e['id']=='p-task.2']),1)
        fact=next(f for f in lifecycle.project_facts(captured) if f['id']=='p-other')['facts']['integrated']
        self.assertEqual((fact['value'],fact['reason']),('unknown','lifecycle-void-target-changed'))

    def test_work_and_capability_readers_do_not_reuse_voided_integration(self):
        import work
        from capability_verification import integrated_commits
        self.rows[0]['status']='open'
        self.apply(self.payload());captured=voids.snapshot(self.rows,self.project)
        for rows in (self.rows,captured):
            item=next(i for i in work.queue(rows,'worker',[],operators=['op'],journal=self.project)['items'] if i['task']=='p-task')
            self.assertEqual(item['lifecycle']['integrated'],'unknown')
            self.assertEqual(item['lifecycle_reasons']['integrated'],'voided')
        self.assertEqual(integrated_commits(self.rows,journal=self.project),set())
        self.assertEqual(integrated_commits(captured),set())

    def test_damaged_family_write_names_family_before_any_native_effect(self):
        self.apply(self.payload());next((self.project/voids.DIRECTORY).glob('*.json')).write_text('{')
        payload=dict(lifecycle.native_event(self.rows[2])['payload'],operation_id='new-fact')
        captured=voids.snapshot(self.rows,self.project)
        with self.assertRaisesRegex(ValueError,'correction family is damaged'),mock.patch.object(lifecycle,'_operation_index',return_value={}):
            lifecycle._apply_fact(payload,'op',captured,lambda args:self.fail('unexpected native effect'),None,
                                  {},{'p-task':self.rows[0]})

    def test_recorded_integrations_accepts_only_the_listed_principals_bound_actor(self):
        import review_workflow as review
        old_scope=lifecycle.native_event(self.rows[1])['payload']['scope']
        new_scope=dict(old_scope,source_commit='c'*40,integration_commit='d'*40)
        task=copy.deepcopy(self.rows[0]);task.update(id='p-later',comments=[],labels=['lifecycle-scope:'+content_hash(new_scope),'integrated:passed'])
        new_rows=[task]
        for old in self.rows[1:]:
            event=copy.deepcopy(old);p=copy.deepcopy(lifecycle.native_event(old)['payload'])
            p.update(task='p-later',actor='bound',scope=new_scope,operation_id='later-'+p['dimension'])
            if p['dimension']=='lifecycle-scope':p['value']=content_hash(new_scope)
            event.update(id='p-later.'+old['id'].rsplit('.',1)[1],created_by='bound',created_at='2026-10-02T00:00:00Z',
                         title='State change: '+p['dimension']+' → '+p['value'],
                         description='Set '+p['dimension']+' to '+p['value']+'\n\nReason: '+lifecycle.PREFIX+canonical_bytes(p).decode(),
                         dependencies=[dict(type='parent-child',depends_on_id='p-later')])
            new_rows.append(event)
        prior=dict(scope_token=content_hash(old_scope))
        recorded,newest=review.recorded_integrations(self.rows+new_rows,'p-task',prior,operators=['op'],
                         owners={'op':'person:owner','bound':'person:owner'})
        self.assertEqual((recorded['d'*40],newest),('later','d'*40))
        recorded,newest=review.recorded_integrations(self.rows+new_rows,'p-task',prior,operators=['op'],
                         owners={'op':'person:owner','bound':'person:other'})
        self.assertEqual(recorded['d'*40][0],'unlisted');self.assertIsNone(newest)


    def test_offline_evidence_owed_consults_named_family_and_warns_when_omitted(self):
        from test_lifecycle_release import NativeStore,payload,scope
        store=NativeStore(tasks=('trial-a',)).seed('trial-a')
        production=scope(release='synthetic',environment='synthetic')
        store.record(payload('trial-a','lifecycle-scope',operation='deploy-scope',scope_value=production))
        store.record(payload('trial-a','deployed',operation='deploy',scope_value=production))
        self.rows=store.rows
        data,_=self.cli('evidence-owed','--journal',str(self.project))
        self.assertEqual(data[0]['tasks'][0]['deployed'],'passed')
        data,warning=self.cli('evidence-owed')
        self.assertEqual(data,[]);self.assertIn('not consulted',warning)

    def test_web_task_projection_carries_reason_beside_unknown(self):
        import http_service,types
        self.apply(self.payload())
        canonical={'lifecycle':lifecycle.project_facts(voids.snapshot(self.rows,self.project))[0]['facts']}
        backend=http_service.EndpointBackend(sys.executable,'endpoint.py','/synthetic',
                    service=types.SimpleNamespace(store=types.SimpleNamespace(path='/synthetic/store')))
        with mock.patch.object(backend,'get_task',return_value={'id':'p-task'}),mock.patch.object(backend,'_run',return_value=canonical):
            view=backend.task_brief('p','p-task')
        self.assertEqual(view['lifecycle']['integrated'],{'value':'unknown','reason':'voided','note':'voided'})
        self.assertEqual(view['lifecycle']['implemented'],{'value':'unknown','note':None})

if __name__=='__main__':unittest.main()
