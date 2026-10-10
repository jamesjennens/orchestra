"""Slices 6 and 7 (kittrial-5bb.197), part (a): who may write which lifecycle fact.

The rule is the table recorded on kittrial-5bb.106: `implemented`, `tested` and the
task's `lifecycle-scope` by the task's assignee or a listed operator; `reviewed`,
`integrated`, `deployed`, `live-verified`, `enabled`, `live` and a release deploy by
a listed operator only. It sits behind the per-installation setting
`lifecycle_authors`, OFF by default, so these tests pin both halves: the refusal when
it is on, the same write succeeding with a warning when it is off, and the read-only
preview (`admin.py lifecycle-authors PROJECT`) that is meant to be read first.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin
import lifecycle
import record_json
import review_workflow

TASK='task-1'
ASSIGNEE='worker'
OPERATOR='ops'
OTHER='outsider'
SCOPE={'source_commit':'a'*40,'integration_commit':'b'*40,'release_id':'','environment':''}
RELEASE_SCOPE={'source_commit':'','integration_commit':'c'*40,'release_id':'release-1','environment':'prod'}


def task_row(task=TASK,assignee=ASSIGNEE,labels=(),title=None):
    return dict(id=task,title=title or 'Task '+task,issue_type='task',status='open',assignee=assignee,
                labels=list(labels),comments=[],created_by='someone',created_at='2026-10-01T00:00:00Z')


def event_row(task,dimension,value,actor,order,scope=None,operation_id=None,label_value=None):
    """One native `set-state` event, in the shape `lifecycle.native_event` reads."""
    body=dict(schema_version=1,operation_id=operation_id or ('op-%s-%d'%(task,order)),task=task,
              dimension=dimension,value=value,scope=dict(scope or SCOPE),
              evidence=['commit:'+('b'*40)],provenance='performed',actor=actor)
    reason=lifecycle.PREFIX+lifecycle.canonical_bytes(body).decode()
    event='%s.%d'%(task,order)
    return dict(id=event,issue_type='event',title='State change: %s \u2192 %s'%(dimension,value),
                description='Set %s to %s\n\nReason: %s'%(dimension,label_value if label_value is not None else value,reason),
                status='closed',created_by=actor,created_at='2026-10-01T00:00:00Z',labels=[],comments=[],
                dependencies=[dict(issue_id=event,depends_on_id=task,type='parent-child')])


class Tracker:
    """The least native state `apply_native` needs: one task and its state events."""

    def __init__(self,assignee=ASSIGNEE):
        self.rows=[task_row(assignee=assignee)]
        self.writes=[]

    def task(self):return self.rows[0]

    def export(self):return '\n'.join(json.dumps(row) for row in self.rows)

    def __call__(self,argv):
        if list(argv)==['export','--all']:return self.export()
        if argv[0]=='set-state':
            self.writes.append(list(argv))
            task,assignment=argv[1],argv[2]
            dimension,value=assignment.split('=',1)
            reason=argv[argv.index('--reason')+1]
            body=record_json.loads(reason[len(lifecycle.PREFIX):])
            row=self.task()
            row['labels']=[x for x in row['labels'] if not x.startswith(dimension+':')]+['%s:%s'%(dimension,value)]
            event='%s.%d'%(task,len(self.rows))
            self.rows.append(dict(id=event,issue_type='event',title='State change: %s \u2192 %s'%(dimension,value),
                                  description='Set %s to %s\n\nReason: %s'%(dimension,value,reason),
                                  status='closed',created_by=body['actor'],created_at='2026-10-01T00:00:01Z',
                                  labels=[],comments=[],
                                  dependencies=[dict(issue_id=event,depends_on_id=task,type='parent-child')]))
            return json.dumps(dict(changed=True,dimension=dimension,event_id=event,new_value=value))
        raise AssertionError('unexpected native call %r'%(argv,))


def payload(dimension,value,actor=ASSIGNEE,scope=None,operation_id=None,**extra):
    body=dict(schema_version=1,operation_id=operation_id or ('op-%s-%s'%(dimension,actor)),task=TASK,
              dimension=dimension,value=value,scope=dict(scope or SCOPE),evidence=['commit:'+('b'*40)],
              provenance='performed',actor=actor)
    if dimension=='lifecycle-scope':body['value']=lifecycle.content_hash(body['scope'])
    body.update(extra)
    return body


def record_scope(tracker,actor=ASSIGNEE,enforce=True,warnings=None):
    return lifecycle.apply_native(payload('lifecycle-scope',None,actor=actor),actor,tracker,
                                  operators=[OPERATOR],lifecycle_authors=enforce,warnings=warnings)


class AuthorityTests(unittest.TestCase):
    def test_a_listed_operator_may_record_every_dimension_the_rule_names(self):
        for dimension in lifecycle.AUTHORED_DIMENSIONS+(lifecycle.RELEASE,):
            allowed,reason=lifecycle.dimension_authority(dimension,OPERATOR,'somebody-else',[OPERATOR])
            self.assertTrue(allowed,dimension);self.assertIsNone(reason)

    def test_the_assignee_may_record_implemented_tested_and_the_scope(self):
        for dimension in lifecycle.ASSIGNEE_DIMENSIONS:
            self.assertTrue(lifecycle.dimension_authority(dimension,ASSIGNEE,ASSIGNEE,[])[0],dimension)

    def test_the_assignee_may_not_record_the_operator_only_dimensions(self):
        for dimension in lifecycle.OPERATOR_ONLY_DIMENSIONS:
            allowed,reason=lifecycle.dimension_authority(dimension,ASSIGNEE,ASSIGNEE,[])
            self.assertFalse(allowed,dimension)
            self.assertIn('listed operator',reason)

    def test_an_unassigned_task_allows_only_a_listed_operator(self):
        allowed,reason=lifecycle.dimension_authority('implemented',ASSIGNEE,None,[])
        self.assertFalse(allowed)
        self.assertIn('no assignee',reason)

    def test_the_operator_list_matches_the_whole_name_and_case(self):
        self.assertFalse(lifecycle.dimension_authority('reviewed','Ops',None,['ops'])[0])
        self.assertTrue(lifecycle.dimension_authority('reviewed','ops',None,['ops'])[0])

    def test_the_assignee_comparison_is_the_normalisation_rule_3_uses(self):
        for name in ('Worker','worker/sub',' WORKER ','team/worker'):
            self.assertEqual(lifecycle.attribution_key(name),review_workflow.author_key(name),name)
        self.assertTrue(lifecycle.dimension_authority('implemented','worker/sub','Worker',[])[0])

    def test_a_name_that_is_not_a_plain_actor_is_not_repeated_in_the_reason(self):
        _,reason=lifecycle.dimension_authority('reviewed','bad name\x1b[31m',None,[])
        self.assertIn('an actor',reason)
        self.assertNotIn('\x1b',reason)


class RuleTests(unittest.TestCase):
    def test_the_rule_refuses_naming_the_setting_and_the_preview(self):
        with self.assertRaises(ValueError) as caught:
            lifecycle.lifecycle_authors_rule('integrated',ASSIGNEE,'somebody',[OPERATOR],enforce=True,subject=TASK)
        message=str(caught.exception)
        self.assertIn('lifecycle_authors',message)
        self.assertIn('integrated',message)
        self.assertIn(TASK,message)
        self.assertIn('admin.py lifecycle-authors PROJECT',message)

    def test_while_off_a_write_that_would_be_refused_is_only_warned_about(self):
        warnings=[]
        self.assertFalse(lifecycle.lifecycle_authors_rule('integrated',ASSIGNEE,None,[],enforce=False,warnings=warnings))
        self.assertEqual(len(warnings),1)
        self.assertTrue(warnings[0].startswith('WARNING: '))
        self.assertIn('lifecycle_authors is off',warnings[0])

    def test_an_allowed_actor_is_never_warned_about(self):
        warnings=[]
        self.assertTrue(lifecycle.lifecycle_authors_rule('implemented',ASSIGNEE,ASSIGNEE,[],warnings=warnings))
        self.assertTrue(lifecycle.lifecycle_authors_rule('integrated',OPERATOR,None,[OPERATOR],warnings=warnings))
        self.assertEqual(warnings,[])

    def test_a_direct_caller_with_no_list_to_report_into_is_not_printed_at(self):
        self.assertFalse(lifecycle.lifecycle_authors_rule('integrated',ASSIGNEE,None,[]))


class EnforcedWriteTests(unittest.TestCase):
    def test_with_the_rule_on_an_unlisted_actor_cannot_record_the_scope(self):
        tracker=Tracker()
        with self.assertRaises(ValueError) as caught:
            record_scope(tracker,actor=OTHER)
        self.assertIn('lifecycle_authors',str(caught.exception))
        self.assertEqual(tracker.writes,[])                 # zero native writes
        self.assertEqual(len(tracker.rows),1)               # nothing was recorded

    def test_with_the_rule_on_the_assignee_records_the_scope_and_its_own_facts(self):
        tracker=Tracker()
        record_scope(tracker)
        lifecycle.apply_native(payload('implemented','passed'),ASSIGNEE,tracker,
                               operators=[OPERATOR],lifecycle_authors=True)
        self.assertEqual(len(tracker.writes),2)
        state=next(row for row in lifecycle.project_facts(tracker.rows) if row['id']==TASK)
        self.assertEqual(state['facts']['implemented']['value'],'passed')

    def test_with_the_rule_on_the_assignee_cannot_record_integrated(self):
        tracker=Tracker()
        record_scope(tracker)
        with self.assertRaises(ValueError) as caught:
            lifecycle.apply_native(payload('integrated','passed'),ASSIGNEE,tracker,
                                   operators=[OPERATOR],lifecycle_authors=True)
        self.assertIn('integrated',str(caught.exception))
        self.assertEqual(len(tracker.writes),1)             # only the scope was written

    def test_with_the_rule_off_the_same_write_succeeds_and_is_warned_about(self):
        tracker=Tracker();warnings=[]
        record_scope(tracker,warnings=warnings)
        self.assertEqual(warnings,[])                       # the assignee's own scope is allowed
        lifecycle.apply_native(payload('integrated','passed'),ASSIGNEE,tracker,operators=[OPERATOR],
                               lifecycle_authors=False,warnings=warnings)
        self.assertEqual(len(tracker.writes),2)             # the write happened as before
        self.assertEqual(len(warnings),1)
        self.assertIn('lifecycle_authors is off',warnings[0])

    def test_with_the_rule_on_an_unlisted_actor_cannot_start_a_release(self):
        tracker=Tracker()
        release=dict(schema_version=1,operation_id='release-1',dimension=lifecycle.RELEASE,value='passed',
                     scope=dict(RELEASE_SCOPE),evidence=['release:release-1'],provenance='performed',
                     actor=ASSIGNEE,targets=[],live_verified=False,supersede=[TASK])
        with self.assertRaises(ValueError) as caught:
            lifecycle.apply_native(release,ASSIGNEE,tracker,operators=[OPERATOR],lifecycle_authors=True)
        self.assertIn(lifecycle.RELEASE,str(caught.exception))
        self.assertEqual(tracker.writes,[])
        self.assertEqual(len(tracker.rows),1)


class ReportTests(unittest.TestCase):
    TOKEN=lifecycle.content_hash(SCOPE)

    def rows_for(self,recorded=(('integrated',OTHER),('implemented',ASSIGNEE)),assignee=ASSIGNEE,scope_actor=OPERATOR):
        labels=['lifecycle-scope:'+self.TOKEN]
        rows=[task_row(assignee=assignee)]
        order=1
        if scope_actor is not None:
            rows.append(event_row(TASK,'lifecycle-scope',self.TOKEN,scope_actor,order))
        order+=1
        for dimension,actor in recorded:
            rows.append(event_row(TASK,dimension,'passed',actor,order))
            labels.append('%s:passed'%dimension)
            order+=1
        rows[0]['labels']=labels
        return rows

    def facts(self,report):
        return {fact['dimension']:fact for fact in report['tasks'][0]['facts']}

    def test_the_report_names_the_actor_and_the_verdict_for_every_fact(self):
        report=lifecycle.lifecycle_authors_report(self.rows_for(),operators=[OPERATOR])
        self.assertTrue(report['read_only'])
        facts=self.facts(report)
        self.assertEqual(facts['integrated']['actor'],OTHER)
        self.assertTrue(facts['integrated']['would_be_refused'])
        self.assertEqual(facts['integrated']['authority'],'refused')
        self.assertIn('listed operator',facts['integrated']['reason'])
        self.assertEqual(facts['implemented']['actor'],ASSIGNEE)
        self.assertFalse(facts['implemented']['would_be_refused'])
        self.assertEqual(facts['implemented']['authority'],'assignee')
        self.assertEqual(facts['lifecycle-scope']['authority'],'operator')
        self.assertEqual(report['counts']['facts'],3)
        self.assertEqual(report['counts']['would_be_refused'],1)
        self.assertEqual(report['counts']['tasks_with_would_be_refused'],1)

    def test_with_no_operator_configured_the_assignee_is_still_allowed(self):
        report=lifecycle.lifecycle_authors_report(self.rows_for(),operators=[])
        self.assertEqual(report['operators'],[])
        self.assertFalse(self.facts(report)['implemented']['would_be_refused'])
        self.assertTrue(self.facts(report)['lifecycle-scope']['would_be_refused'])

    def test_a_task_with_no_trusted_fact_is_not_reported(self):
        report=lifecycle.lifecycle_authors_report([task_row()],operators=[OPERATOR])
        self.assertEqual(report['tasks'],[])
        self.assertEqual(report['counts']['tasks_with_facts'],0)

    def test_a_tagged_fact_whose_label_disagrees_is_not_trusted(self):
        rows=self.rows_for(recorded=(('integrated',OTHER),))
        rows[0]['labels']=[label for label in rows[0]['labels'] if not label.startswith('integrated:')]
        report=lifecycle.lifecycle_authors_report(rows,operators=[OPERATOR])
        self.assertNotIn('integrated',self.facts(report))
        self.assertEqual(report['counts']['would_be_refused'],0)

    def test_the_task_list_is_bounded_and_the_counts_cover_every_task(self):
        rows=self.rows_for()
        rows.append(task_row('task-2'))
        rows.append(event_row('task-2','lifecycle-scope',self.TOKEN,OPERATOR,10))
        rows.append(event_row('task-2','reviewed','passed',OTHER,11))
        rows[-3]['labels']=['lifecycle-scope:'+self.TOKEN,'reviewed:passed']
        report=lifecycle.lifecycle_authors_report(rows,operators=[OPERATOR],limit=1)
        self.assertTrue(report['truncated'])
        self.assertEqual(report['counts']['tasks_listed'],1)
        self.assertEqual(report['counts']['tasks_with_facts'],2)
        self.assertEqual(report['counts']['would_be_refused'],2)

    def test_the_report_uses_the_dimension_table_recorded_on_5bb106(self):
        report=lifecycle.lifecycle_authors_report([],operators=[])
        self.assertEqual(report['dimensions']['assignee_or_operator'],['implemented','tested','lifecycle-scope'])
        self.assertEqual(report['dimensions']['operator_only'],
                         ['reviewed','integrated','deployed','live-verified','enabled','live'])


class AdminReportCommandTests(unittest.TestCase):
    def root_with(self,deployment):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        (root/'deployment.private.json').write_text(json.dumps(deployment),encoding='utf-8')
        return root

    def test_the_admin_report_reads_the_export_and_reports_the_setting(self):
        root=self.root_with({'operators':['ops'],'password':'x'})
        rows=ReportTests().rows_for()
        with mock.patch.object(admin,'run_bd',return_value='\n'.join(json.dumps(row) for row in rows)):
            report=admin.lifecycle_authors_command(root,'demo')
        self.assertEqual(report['project'],'demo')
        self.assertEqual(report['setting'],{'lifecycle_authors':False})
        self.assertEqual(report['counts']['tasks_with_facts'],1)

    def test_the_admin_report_says_the_setting_is_on_once_it_is(self):
        root=self.root_with({'operators':['ops'],'password':'x','lifecycle_authors':True})
        with mock.patch.object(admin,'run_bd',return_value=json.dumps(task_row())):
            report=admin.lifecycle_authors_command(root,'demo')
        self.assertEqual(report['setting'],{'lifecycle_authors':True})

    def test_an_invalid_limit_is_refused(self):
        root=self.root_with({'operators':['ops'],'password':'x'})
        with self.assertRaises(ValueError):
            admin.lifecycle_authors_command(root,'demo',-1)


if __name__=='__main__':
    unittest.main()
