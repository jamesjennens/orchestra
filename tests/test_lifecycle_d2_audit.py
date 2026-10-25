"""Read-only D2 audit evidence; synthetic generic rows, never production history."""
import copy
import contextlib
import io
import sys
import tempfile
from pathlib import Path
import json
import unittest
from unittest import mock
import lifecycle_audit as audit
import task_assignment

import lifecycle
import lifecycle_voids as voids
import recovery
import review_workflow as reviews
from requirements import canonical_bytes, content_hash
import test_lifecycle_release as release

PROJECT='p'
TASK='p-task'
RETRY='Lifecycle update in progress; retry the same operation if interrupted.'


def scope():
    return dict(source_commit='a'*40,integration_commit='b'*40,release_id='',environment='test')


def event(number,dimension='tested',value='passed',actor='worker',reason=None,at=None):
    if reason is None:
        payload=dict(schema_version=1,operation_id='operation-'+str(number),task=TASK,
                     dimension=dimension,value=value,scope=scope(),actor=actor,
                     evidence=['synthetic evidence'],provenance='performed')
        reason=lifecycle.PREFIX+canonical_bytes(payload).decode()
    return dict(id=TASK+'.'+str(number),issue_type='event',
                title='State change: '+dimension+' → '+value,
                description='Set '+dimension+' to '+value+'\n\nReason: '+reason,
                created_by=actor,created_at=at or '2026-10-01T00:00:%02dZ'%number,
                dependencies=[dict(type='parent-child',depends_on_id=TASK)])


def rows(intermediate=False):
    task=dict(id=TASK,project_id=PROJECT,issue_type='task',title='Synthetic task',
              status='in_progress',assignee='worker',comments=[],
              created_at='2026-10-01T00:00:00Z',
              labels=['lifecycle-scope:'+content_hash(scope()),'tested:passed'])
    result=[task,event(1,'lifecycle-scope',content_hash(scope()))]
    if intermediate:result.append(event(2,value='pending',reason=RETRY))
    result.append(event(3))
    return result


class AuditRegressions(unittest.TestCase):
    def test_complete_intermediate_pair_is_separate_and_only_intermediate_is_excluded(self):
        data=rows(True);before=copy.deepcopy(data)
        report=voids.audit(data,operators=[],owners={})
        self.assertEqual(report.get('kit_intermediate_count',0),1)
        pair=report['kit_intermediates'][0]
        self.assertEqual((pair['event_id'],pair['following_event_id'],pair['operation_id']),
                         (TASK+'.2',TASK+'.3','operation-3'))
        final=next(item for item in report['items'] if item['event_id']==TASK+'.3')
        self.assertTrue(final['questionable'],'following fact still has unknown historical assignment')
        self.assertEqual(report['original_counts']['unclassifiable_fact_count']-report['unclassifiable_fact_count'],1)
        self.assertEqual(data,before)

    def test_todays_holder_does_not_prove_the_holder_when_a_fact_was_recorded(self):
        data=rows();report=voids.audit(data,operators=[],owners={})
        item=next(item for item in report['items'] if item['event_id']==TASK+'.3')
        self.assertTrue(item['recorder_is_current_assignee'])
        self.assertTrue(item['questionable'],'a current holder is not historical assignment evidence')
        self.assertEqual(item.get('historical_assignment',{}).get('state'),'unknown')
        self.assertEqual(item['current_assignee'],'worker')


class ReleaseRegressions(unittest.TestCase):
    def held_with_void(self,task='trial-a'):
        store=release.NativeStore(tasks=(task,)).seed(task)
        release.add_review_chain(store,task,hold=True)
        issue=store.rows[0];target=issue['comments'][-1]
        # A linked hold is protected and cannot be voided. A real repair VOID
        # reconciles a fork; removing its issuer makes that chain unreadable.
        contribution=json.loads(issue['comments'][0]['text'][len(reviews.PREFIX):])
        fork=dict(contribution,operation_id='forked-revision',previous='2',
                  supersedes='1',commit='9'*40,summary='Synthetic repair revision')
        issue['comments'].append(dict(id='repair-fork',author=release.ACTOR,
            created_at='2026-10-01T00:00:30Z',text=reviews.PREFIX+canonical_bytes(fork).decode()))
        payload=dict(schema_version=1,operation=recovery.OPERATION,operation_id='void-hold',
                     task=issue['id'],target=target['id'],target_kind='contribution-review',
                     target_sha256=recovery.digest(target['text']),original=target['text'],
                     reason='Synthetic operator review',disposition='void',operator=release.OPERATOR)
        recovery.validate(payload,issue['id'])
        issue['comments'].append(dict(id='void-comment',author=release.OPERATOR,
            created_at='2026-10-01T00:01:00Z',text=recovery.PREFIX+canonical_bytes(payload).decode()))
        return store

    def test_inactive_decisive_void_warns_but_cannot_clear_current_refusal(self):
        store=self.held_with_void();before=copy.deepcopy(store.rows)
        report=lifecycle.release_selection(store.rows,release.scope(integration=release.RELEASE_COMMIT),
                                           lambda a,b:True,operators=[])
        self.assertFalse(report['targets'])
        self.assertIn('unreadable hold chain',report['skipped'][0]['reason'])
        self.assertTrue(report.get('review_void_diagnostics'),'decisive inactive VOID must be visible')
        item=report['review_void_diagnostics'][0]
        self.assertEqual(item['void_id'],'void-comment')
        self.assertEqual(item['issuer'],release.OPERATOR)
        self.assertEqual(item['current_result'],'unreadable')
        self.assertEqual(item['counterfactual_result'],'unheld')
        self.assertEqual(item['trust'],'data')
        self.assertEqual(store.rows,before)


def version(commit,at,issue):
    return dict(CommitHash=commit*32,CommitDate=at,Committer='service-account',Issue=copy.deepcopy(issue))


def history_fixture():
    data=rows();task=data[0];native=data[-1]
    native['updated_at']=native['created_at']
    task.update(assignee='new-holder',updated_at='2026-10-01T00:00:10Z')
    old=dict(task,assignee='worker',updated_at='2026-10-01T00:00:03Z')
    histories=audit.AssignmentHistory({TASK:dict(versions=[
        version('c','2026-10-01T00:00:10.5Z',task),
        version('a','2026-10-01T00:00:03.5Z',old)]),TASK+'.3':dict(versions=[
        version('a','2026-10-01T00:00:03.5Z',native)])})
    return data,histories


class IntermediateTests(unittest.TestCase):
    def count(self,data):return voids.audit(data,operators=[],owners={}).get('kit_intermediate_count')
    def test_only_exact_complete_ordered_pairs_qualify(self):
        mutations={
            'author':lambda d:d.__setitem__(-1,event(3,actor='other')),
            'dimension':lambda d:d.__setitem__(-1,event(3,dimension='implemented')),
            'nonadjacent':lambda d:d[-1].update(id=TASK+'.4'),
            'duplicate':lambda d:d.append(copy.deepcopy(d[-2])),
            'unknown-order':lambda d:d.append(dict(d[-1],id=TASK+'.unknown')),
            'wrong-value':lambda d:d.__setitem__(-2,event(2,value='failed',reason=RETRY)),
            'damaged-following':lambda d:d[-1].update(description=d[-1]['description']+' damaged'),
            'almost-reason':lambda d:d[-2].update(description=d[-2]['description']+' '),
            'bad-time':lambda d:d[-1].update(created_at='not-a-time'),
            'backwards-time':lambda d:d[-1].update(created_at='2026-09-30T00:00:00Z'),
        }
        for name,mutate in mutations.items():
            with self.subTest(case=name):
                data=rows(True);mutate(data);self.assertEqual(self.count(data),0)
    def test_second_precision_does_not_erase_proved_native_child_order(self):
        data=rows(True);data[-1]['created_at']=data[-2]['created_at']
        self.assertEqual(self.count(data),1)
        data.append(copy.deepcopy(data[-1]));self.assertEqual(self.count(data),0)
    def test_unordered_retry_row_is_explicitly_unpaired_not_silently_omitted(self):
        data=rows(True);data[-2]['id']=TASK+'.raw'
        report=voids.audit(data,operators=[],owners={})
        self.assertEqual(report['kit_intermediate_count'],0)
        self.assertEqual(report['unpaired_intermediate_count'],1)
        self.assertEqual(report['unpaired_intermediates'][0]['reason'],'native-order-unclassifiable')
    def test_previous_native_value_must_agree_when_available(self):
        data=rows(True);data.insert(1,event(0,value='failed'))
        self.assertEqual(self.count(data),0)
        data[1]=event(0);self.assertEqual(self.count(data),1)
    def test_repeating_pending_uses_unknown_only(self):
        data=rows(True);data[-2]=event(2,value='unknown',reason=RETRY);data[-1]=event(3,value='pending')
        self.assertEqual(self.count(data),1)
        data[-2]=event(2,value='pending',reason=RETRY);self.assertEqual(self.count(data),0)
    def test_ordinary_events_do_not_inflate_revised_unknown_native_counts(self):
        data=rows();data.append(event(4,dimension='custom',value='done',reason='Ordinary native state'))
        report=voids.audit(data,operators=[],owners={})
        self.assertEqual(report['ordinary_native_event_count'],1)
        self.assertEqual(report['unclassifiable_native_event_count'],0)
        self.assertEqual(report['original_counts']['unclassifiable_native_event_count'],1)
    def test_every_stream_and_global_total_reconcile_across_small_pages(self):
        data=rows(True);data.append(event(4,dimension='custom',reason='Ordinary event'))
        data.append(dict(id=TASK+'.5',issue_type='event',title='Unusable event',description='bad'))
        full=voids.audit(data,operators=[],owners={});offset=0;collected={k:[] for k in
            ('items','kit_intermediates','unpaired_intermediates','ordinary_native_events','unknown_native_events','corrections','unreadable_hold_chains')}
        while offset is not None:
            page=voids.audit(data,operators=[],owners={},limit=1,offset=offset)
            for key in collected:collected[key]+=page[key]
            for key in ('questionable_fact_count','unclassifiable_fact_count','kit_intermediate_count','original_counts','questionable_reason_counts'):
                self.assertEqual(page[key],full[key])
            offset=page['next_offset']
        for key,value in collected.items():self.assertEqual(value,full[key])


class HistoricalTests(unittest.TestCase):
    def read(self,data,history):
        return next(i for i in voids.audit(data,operators=[],owners={},assignment_history=history,project=PROJECT)['items'] if i['event_id']==TASK+'.3')
    def test_exact_creation_commit_proves_only_the_field_at_that_commit(self):
        data,history=history_fixture();before=copy.deepcopy(data)
        item=self.read(data,history);h=item['historical_assignment']
        self.assertEqual(h['state'],'known');self.assertEqual(h['assignee'],'worker')
        self.assertEqual(h['basis'],'same-native-event-creation-commit')
        self.assertEqual(h['native_versions'],['a'*32]);self.assertIsNone(h['assigned_by'])
        self.assertFalse(h['historical_principal_ownership_verified'])
        self.assertFalse(item['questionable']);self.assertEqual(item['current_assignee'],'new-holder')
        self.assertEqual(data,before)
    def test_all_missing_damaged_tied_gapped_or_mismatched_history_stays_unknown(self):
        mutations={
            'absent':lambda d,h:h.entries[TASK].update(versions=[]),
            'damaged':lambda d,h:h.entries[TASK].update(versions=[{'bad':True}]),
            'tie':lambda d,h:h.entries[TASK]['versions'][0].update(CommitDate=h.entries[TASK]['versions'][1]['CommitDate']),
            'duplicate':lambda d,h:h.entries[TASK]['versions'].append(copy.deepcopy(h.entries[TASK]['versions'][0])),
            'head':lambda d,h:d[0].update(assignee='unrecorded-holder'),
            'raw-gap':lambda d,h:h.entries[TASK]['versions'][1].update(CommitHash='d'*32),
            'commit-mismatch':lambda d,h:h.entries[TASK]['versions'][1].update(CommitDate='2026-10-01T00:00:03.6Z'),
            'backdated':lambda d,h:d[-1].update(created_at='2026-09-01T00:00:00Z'),
            'cap':lambda d,h:h.entries[TASK].update(reason='native-history-version-cap'),
            'event-absent':lambda d,h:h.entries.pop(TASK+'.3'),
            'wrong-task':lambda d,h:h.entries[TASK]['versions'][0]['Issue'].update(id='foreign'),
        }
        for name,mutate in mutations.items():
            with self.subTest(case=name):
                data,history=history_fixture();mutate(data,history)
                item=self.read(data,history);self.assertEqual(item['historical_assignment']['state'],'unknown')
                self.assertIsNone(item['recorder_is_historical_assignee']);self.assertTrue(item['questionable'])
    def test_row_json_cannot_manufacture_host_history(self):
        data,history=history_fixture();data[0]['assignment_history']=history.entries
        item=self.read(data,None);self.assertEqual(item['historical_assignment']['state'],'unknown')
    def notice_fixture(self):
        data,history=history_fixture();task=data[0];native=data[-1]
        task.update(assignee='after',updated_at='2026-10-01T00:00:30Z');native.update(created_at='2026-10-01T00:00:30Z',updated_at='2026-10-01T00:00:30Z')
        notice=task_assignment.make_notice(dict(schema_version=1,operation_id='assignment-proof',task=TASK,
            expected_assignee='before',assignee='after',reason='Synthetic assignment',allow_pending_review=False,allow_held_slot=False),
            project=PROJECT,assigned_by='coordinator',at='2026-10-01T00:00:14Z')
        task['comments']=[dict(id='notice',author='coordinator',created_at='2026-10-01T00:00:16Z',text=task_assignment.body(notice))]
        history.entries[TASK]['versions']=[version('a','2026-10-01T00:00:30.5Z',task),
            version('b','2026-10-01T00:00:15Z',dict(task,updated_at='2026-10-01T00:00:15Z')),
            version('c','2026-10-01T00:00:10Z',dict(task,assignee='before',updated_at='2026-10-01T00:00:10Z'))]
        history.entries[TASK+'.3']['versions']=[version('a','2026-10-01T00:00:30.5Z',native)]
        return data,history
    def test_who_assigned_needs_valid_anchored_notice_and_native_transition(self):
        data,history=self.notice_fixture();h=self.read(data,history)['historical_assignment']
        self.assertEqual(h['assignee'],'after');self.assertEqual(h['assigned_by'],'coordinator')
        self.assertEqual(h['notice_ids'],['notice'])
        self.assertNotEqual(h['assigned_by'],'service-account')
        for mutate in (lambda d:d[0]['comments'][0].update(author='forged'),
                       lambda d:d[0]['comments'][0].update(created_at='2026-10-01T00:00:30Z')):
            data,history=self.notice_fixture();mutate(data)
            self.assertIsNone(self.read(data,history)['historical_assignment']['assigned_by'])


class HistoryBoundsTests(unittest.TestCase):
    def test_only_host_adapter_loads_history(self):
        data=rows(True)
        with mock.patch.object(audit,'load_assignment_history',side_effect=AssertionError('not an ordinary reader')):
            lifecycle.project_facts(data);voids.audit(data,operators=[],owners={})
            lifecycle.release_selection(data,scope(),lambda a,b:True,operators=[])
    def test_per_issue_and_whole_audit_version_caps_are_explicit(self):
        data=rows();read=lambda identity,limit,byte_limit,timeout:json.dumps([{}]*limit)
        history=audit.load_assignment_history(data,read)
        self.assertEqual(history.entries[TASK]['reason'],'native-history-version-cap')
        with mock.patch.object(audit,'HISTORY_TOTAL_VERSION_CAP',2):
            history=audit.load_assignment_history(data,read)
        self.assertEqual(history.coverage['returned_versions'],2)
        self.assertEqual(history.coverage['read_calls'],1)
        self.assertEqual(history.coverage['stopped_reason'],'whole-audit-history-version-cap')
    def test_whole_audit_call_cap_and_missing_ids_never_guess(self):
        with mock.patch.object(audit,'HISTORY_CALL_CAP',1):
            history=audit.load_assignment_history(rows(),lambda *args:'[]')
        self.assertEqual(history.coverage['read_calls'],1)
        self.assertEqual(history.coverage['stopped_reason'],'whole-audit-history-call-cap')
        self.assertGreater(history.coverage['unread_ids'],0)
    def test_deadline_applies_to_the_whole_enrichment(self):
        now=[0.0];calls=[]
        def read(identity,limit,byte_limit,timeout):
            calls.append(timeout);now[0]+=11;return '[]'
        history=audit.load_assignment_history(rows(),read,clock=lambda:now[0])
        self.assertEqual(len(calls),1);self.assertLessEqual(calls[0],3)
        self.assertEqual(history.entries[TASK]['reason'],'whole-audit-history-time-budget')
        self.assertGreater(history.coverage['unread_ids'],0)
    def test_byte_cap_charges_failed_reads_to_the_whole_budget(self):
        data=rows()+[event(n) for n in range(4,10)];calls=[]
        def read(identity,limit,byte_limit,timeout):
            calls.append(byte_limit);raise audit.HistoryReadError('native-history-byte-cap',byte_limit)
        with mock.patch.multiple(audit,HISTORY_BYTES_CAP=100,HISTORY_TOTAL_BYTES_CAP=200):
            history=audit.load_assignment_history(data,read)
        self.assertEqual(len(calls),2);self.assertEqual(history.coverage['charged_bytes'],200)
        self.assertEqual(history.coverage['stopped_reason'],'whole-audit-history-byte-cap')
    def test_malformed_json_and_read_failure_are_unavailable(self):
        for value in ('bad JSON','{}','null'):
            history=audit.load_assignment_history(rows(),lambda *args:value)
            self.assertEqual(history.entries[TASK]['reason'],'native-history-unavailable-or-damaged')


class AdditionalEvidenceTests(unittest.TestCase):
    def test_duplicate_id_overrides_otherwise_proved_historical_assignment(self):
        data,history=history_fixture()
        before=voids.audit(data,operators=[],owners={},assignment_history=history)
        original=next(i for i in before['items'] if i['event_id']==TASK+'.3')
        self.assertEqual(original['historical_assignment']['state'],'known')
        data.append(copy.deepcopy(data[-1]))
        result=voids.audit(data,operators=[],owners={},assignment_history=history)
        repeated=[i for i in result['items'] if i['event_id']==TASK+'.3']
        self.assertEqual(len(repeated),2)
        self.assertTrue(all(i['historical_assignment']['state']=='unknown' for i in repeated))
        self.assertTrue(all(i['historical_assignment']['reason']=='duplicate-native-event-id' for i in repeated))
        self.assertTrue(all(i['historical_assignment']['assignee'] is None for i in repeated))

    def test_duplicate_native_ids_are_unknown_and_deduplicated_in_revised_totals(self):
        data=rows();data.append(copy.deepcopy(data[-1]))
        report=voids.audit(data,operators=[],owners={})
        ids={i['event_id'] for i in report['items'] if i['questionable']}
        self.assertEqual(report['questionable_fact_count'],len(ids))
        self.assertEqual(report['deduplicated_event_count'],2)
        self.assertEqual(report['duplicate_native_event_id_count'],1)
        repeated=[i for i in report['items'] if i['event_id']==TASK+'.3']
        self.assertTrue(all(i['historical_assignment']['state']=='unknown' for i in repeated))
        self.assertTrue(all('duplicate-native-event-id' in i['unclassifiable_causes'] for i in repeated))
        self.assertEqual(report['questionable_reason_counts']['duplicate-native-event-id'],1)
    def test_unknown_row_hash_preserves_every_source_field_including_sha256(self):
        data=rows();broken=dict(id=TASK+'.unknown',issue_type='event',title='Unclassifiable native row',description='Synthetic unknown',sha256='a'*64)
        data += [broken,dict(broken,sha256='b'*64)]
        report=voids.audit(data,operators=[],owners={})
        item=report['unknown_native_events'][0]
        self.assertEqual(item['raw_occurrences'],2)
        self.assertEqual(len(item['row_sha256s']),2)
        self.assertEqual(report['unclassifiable_native_event_count'],1)
    def test_naive_version_time_and_edited_event_history_are_unknown(self):
        harness=HistoricalTests()
        data,history=history_fixture();history.entries[TASK]['versions'][0]['CommitDate']='2026-10-01T00:00:10.5'
        self.assertEqual(harness.read(data,history)['historical_assignment']['state'],'unknown')
        data,history=history_fixture();old=copy.deepcopy(history.entries[TASK+'.3']['versions'][0]);old['Issue']['description']='Different original event'
        history.entries[TASK+'.3']['versions'].insert(0,version('d','2026-10-01T00:00:11Z',data[-1]))
        history.entries[TASK+'.3']['versions'][-1]=old
        self.assertEqual(harness.read(data,history)['historical_assignment']['reason'],'native-event-history-was-edited')
    def test_detached_or_duplicate_notices_cannot_prove_who_assigned(self):
        harness=HistoricalTests();data,history=harness.notice_fixture()
        duplicate=copy.deepcopy(data[0]['comments'][0]);duplicate['id']='duplicate';data[0]['comments'].append(duplicate)
        self.assertIsNone(harness.read(data,history)['historical_assignment']['assigned_by'])
        data,history=harness.notice_fixture();data[0].update(assignee='current-other',updated_at='2026-10-01T00:00:40Z')
        history.entries[TASK]['versions'].insert(0,version('d','2026-10-01T00:00:40.5Z',data[0]))
        result=harness.read(data,history)['historical_assignment']
        self.assertEqual(result['assignee'],'after');self.assertIsNone(result['assigned_by'])


class OperatorEligibilityTests(unittest.TestCase):
    def history(self,issuer=release.OPERATOR):
        import admin
        baseline=admin.authority_change_baseline(dict(operators=[issuer],verifiers=[]),'ops','Synthetic baseline',
            at='2026-10-01T00:00:00Z')
        entry=dict(at='2026-10-01T00:02:00Z',operator='ops',list='operators',actor=issuer,change='remove',reason='Synthetic removal')
        return audit.OperatorHistory([entry],baseline,[])

    def test_only_proved_prior_eligibility_is_currently_ineligible(self):
        store=ReleaseRegressions().held_with_void();before=copy.deepcopy(store.rows)
        report=audit.review_void_report(store.rows,[],operator_history=self.history())
        notes=report.get('review_void_diagnostics',[]);self.assertEqual(len(notes),1,report)
        self.assertEqual(notes[0]['reason'],'currently-ineligible-review-void')
        self.assertEqual(notes[0]['historical_operator_eligibility'],'eligible')
        self.assertEqual(notes[0]['eligibility_basis'],'retained-authority-trail')
        self.assertEqual(before,store.rows)

    def test_never_eligible_commenter_cannot_plant_currently_ineligible_warning(self):
        store=ReleaseRegressions().held_with_void();comment=store.rows[0]['comments'][-1]
        payload=json.loads(comment['text'][len(recovery.PREFIX):]);payload['operator']='never-operator'
        comment.update(author='never-operator',text=recovery.PREFIX+canonical_bytes(payload).decode())
        before=copy.deepcopy(store.rows)
        self.assertEqual(audit.review_void_report(store.rows,[],operator_history=self.history()),{})
        self.assertEqual(before,store.rows)

    def test_verifier_add_and_remove_never_change_operator_eligibility(self):
        import admin
        issuer=release.OPERATOR
        for change,initial,state in [('add',False,'ineligible'),('remove',True,'eligible')]:
            with self.subTest(change=change),tempfile.TemporaryDirectory(prefix='d2-verifier-history-') as scratch:
                root=Path(scratch);path=root/admin.AUTHORITY_CHANGES_AUDIT
                baseline=admin.authority_change_baseline(
                    dict(operators=[issuer] if initial else [],verifiers=[issuer] if initial else []),
                    'ops','Synthetic separate lists',at='2026-10-01T00:00:00Z')
                entries=[dict(at='2026-10-01T00:00:10Z',operator='ops',list='verifiers',
                              actor=issuer,change=change,reason='Synthetic verifier change')]
                if initial:
                    entries.append(dict(at='2026-10-01T00:02:00Z',operator='ops',list='operators',
                                        actor=issuer,change='remove',reason='Synthetic later operator removal'))
                path.write_text(json.dumps(dict(schema_version=1,baseline=baseline,entries=entries)))
                saved=path.read_bytes();history=audit.read_operator_history(root,[])
                self.assertIsNone(history.reason)
                eligibility=audit.operator_eligibility(history,issuer,'2026-10-01T00:01:00Z',[])
                self.assertEqual(eligibility['state'],state,eligibility)
                store=ReleaseRegressions().held_with_void();before=copy.deepcopy(store.rows)
                notes=audit.review_void_report(store.rows,[],operator_history=history).get('review_void_diagnostics',[])
                if initial:
                    self.assertEqual(len(notes),1,notes)
                    self.assertEqual(notes[0]['historical_operator_eligibility'],'eligible')
                    self.assertEqual(notes[0]['reason'],'currently-ineligible-review-void')
                    self.assertEqual(notes[0]['eligibility_basis'],'retained-authority-trail')
                else:self.assertEqual(notes,[])
                self.assertEqual(store.rows,before);self.assertEqual(path.read_bytes(),saved)

    def test_mode_zero_label_uses_existing_stat_without_reading_or_relabeling_damage(self):
        import admin,stat
        with tempfile.TemporaryDirectory(prefix='d2-unreadable-label-') as scratch:
            root=Path(scratch);path=root/admin.AUTHORITY_CHANGES_AUDIT;history=self.history()
            path.write_text(json.dumps(dict(schema_version=1,entries=history.entries,baseline=history.baseline)))
            saved=path.read_bytes();fields=list(path.lstat());fields[0]=stat.S_IFREG
            unreadable_stat=type(path.lstat())(fields)
            with mock.patch.object(Path,'lstat',return_value=unreadable_stat),mock.patch.object(
                    admin,'read_authority_changes',wraps=admin.read_authority_changes) as reader:
                loaded=audit.read_operator_history(root,[])
                self.assertEqual(loaded.reason,'authority-audit-unreadable')
                self.assertEqual(audit.operator_eligibility(loaded,release.OPERATOR,
                    '2026-10-01T00:01:00Z',[])['state'],'unknown')
                reader.assert_not_called()
            self.assertEqual(path.read_bytes(),saved)
            path.write_text('{damaged')
            self.assertEqual(audit.read_operator_history(root,[]).reason,'authority-audit-damaged')

    def test_real_posix_mode_zero_is_unreadable_and_preserves_bytes(self):
        import admin,os
        if os.name!='posix':self.skipTest('POSIX mode-000 permission denial is not available')
        with tempfile.TemporaryDirectory(prefix='d2-mode-zero-') as scratch:
            root=Path(scratch);path=root/admin.AUTHORITY_CHANGES_AUDIT;history=self.history()
            path.write_text(json.dumps(dict(schema_version=1,entries=history.entries,baseline=history.baseline)))
            saved=path.read_bytes();mode=path.stat().st_mode
            try:
                path.chmod(0)
                try:path.read_bytes()
                except PermissionError:pass
                else:self.skipTest('this runner can read mode-000 files; actual permission denial unverified')
                loaded=audit.read_operator_history(root,[])
                self.assertEqual(loaded.reason,'authority-audit-unreadable')
                store=ReleaseRegressions().held_with_void()
                notes=audit.review_void_report(store.rows,[],operator_history=loaded)['review_void_diagnostics']
                self.assertEqual(notes[0]['historical_operator_eligibility'],'unknown')
                self.assertEqual(notes[0]['eligibility_reason'],'authority-audit-unreadable')
                self.assertEqual(path.stat().st_mode & 0o777,0)
            finally:path.chmod(mode)
            self.assertEqual(path.read_bytes(),saved)

    def test_missing_or_caller_supplied_history_is_explicitly_unknown(self):
        store=ReleaseRegressions().held_with_void()
        for history in (None,dict(state='eligible'),audit.OperatorHistory(reason='authority-audit-damaged')):
            with self.subTest(history=type(history).__name__):
                report=audit.review_void_report(store.rows,[],operator_history=history)
                notes=report.get('review_void_diagnostics',[]);self.assertEqual(len(notes),1,report)
                self.assertEqual(notes[0]['reason'],'review-void-eligibility-unknown')
                self.assertEqual(notes[0]['historical_operator_eligibility'],'unknown')
                self.assertNotEqual(notes[0]['eligibility_reason'],None)

    def test_time_gaps_ties_bad_order_null_baseline_and_mismatch_are_unknown(self):
        cases=[]
        for stamp in ('2026-09-30T23:59:59Z','2026-10-01T00:00:00Z','2026-10-01T00:02:00Z','2999-01-01T00:00:00Z','damaged'):
            cases.append((self.history(),stamp,[]))
        history=self.history();history.baseline['lists']['operators']=None;cases.append((history,'2026-10-01T00:01:00Z',[]))
        history=self.history();history.entries += [dict(history.entries[0],at='2026-10-01T00:01:59Z')];cases.append((history,'2026-10-01T00:01:00Z',[]))
        history=self.history();history.listed=['unexplained'];cases.append((history,'2026-10-01T00:01:00Z',['unexplained']))
        cases.append((self.history(),'2026-10-01T00:01:00Z',['different']))
        history=self.history();history.entries[0]['at']='2999-01-01T00:00:00Z';cases.append((history,'2026-10-01T00:01:00Z',[]))
        history=self.history();history.baseline['at']='2026-10-01T00:03:00Z';cases.append((history,'2026-10-01T00:04:00Z',[]))
        for history,stamp,operators in cases:
            with self.subTest(stamp=stamp,operators=operators):
                self.assertEqual(audit.operator_eligibility(history,release.OPERATOR,stamp,operators)['state'],'unknown')

    def test_repeated_add_remove_membership_is_evaluated_at_the_void_time(self):
        history=self.history();history.baseline['lists']['operators']=[]
        history.entries=[dict(history.entries[0],at='2026-10-01T00:'+stamp+'Z',change=change)
                         for stamp,change in [('00:10','add'),('00:40','remove'),('01:10','add'),('01:40','remove')]]
        for stamp,state in [('00:05','ineligible'),('00:20','eligible'),('00:50','ineligible'),('01:20','eligible'),('01:50','ineligible')]:
            with self.subTest(stamp=stamp):
                self.assertEqual(audit.operator_eligibility(history,release.OPERATOR,'2026-10-01T00:'+stamp+'Z',[])['state'],state)

    def test_existing_audit_reader_missing_damaged_symlink_caps_and_no_effects(self):
        import admin
        with tempfile.TemporaryDirectory(prefix='d2-authority-') as scratch:
            root=Path(scratch)/'runtime';root.mkdir();path=root/admin.AUTHORITY_CHANGES_AUDIT
            missing=audit.read_operator_history(root,[])
            self.assertEqual(missing.reason,'authority-audit-missing')
            history=self.history();record=dict(schema_version=1,entries=history.entries,baseline=history.baseline)
            text=json.dumps(record);path.write_text(text)
            loaded=audit.read_operator_history(root,[])
            self.assertEqual(audit.operator_eligibility(loaded,release.OPERATOR,'2026-10-01T00:01:00Z',[])['state'],'eligible')
            self.assertEqual(path.read_text(),text)
            path.write_text('{damaged')
            self.assertEqual(audit.read_operator_history(root,[]).reason,'authority-audit-damaged')
            path.unlink();target=Path(scratch)/'outside.json';target.write_text(text)
            try:path.symlink_to(target)
            except (OSError,NotImplementedError):pass
            else:
                with mock.patch.object(admin,'read_authority_changes') as reader:
                    self.assertEqual(audit.read_operator_history(root,[]).reason,'authority-audit-not-regular')
                    reader.assert_not_called()
                self.assertEqual(target.read_text(),text);path.unlink()
            path.write_text('x'*(audit.OPERATOR_AUDIT_BYTES+1))
            with mock.patch.object(admin,'read_authority_changes') as reader:
                self.assertEqual(audit.read_operator_history(root,[]).reason,'authority-audit-byte-cap');reader.assert_not_called()
            path.write_text(json.dumps(dict(record,entries=history.entries*201)))
            self.assertEqual(audit.read_operator_history(root,[]).reason,'authority-audit-entry-cap')
            path.write_text(text)
            with mock.patch.object(admin,'read_authority_changes',side_effect=OSError('Synthetic unreadable')):
                self.assertEqual(audit.read_operator_history(root,[]).reason,'authority-audit-unreadable')
            self.assertEqual(sorted(p.name for p in root.iterdir()),[admin.AUTHORITY_CHANGES_AUDIT])

    def test_existing_reader_enforces_byte_bound_and_refuses_open_time_symlink_replacement(self):
        import admin
        with tempfile.TemporaryDirectory(prefix='d2-authority-race-') as scratch:
            root=Path(scratch);path=root/admin.AUTHORITY_CHANGES_AUDIT
            history=self.history();text=json.dumps(dict(schema_version=1,entries=history.entries,baseline=history.baseline))
            path.write_text(text)
            self.assertIsNone(admin.read_authority_changes(root)[2])
            self.assertIsNotNone(admin.read_authority_changes(root,max_bytes=8)[2])
            self.assertIsNotNone(admin.read_authority_changes(root,max_bytes=len(text.encode())-1)[2])
            self.assertEqual(path.read_text(),text)
            target=root/'outside.json';target.write_text(text);real_open=admin.os.open
            def replaced(name,flags,*args,**kwargs):
                if str(name)==str(path):
                    path.unlink();path.symlink_to(target)
                return real_open(name,flags,*args,**kwargs)
            try:
                link=root/'probe-link';link.symlink_to(target);link.unlink()
            except (OSError,NotImplementedError):return
            with mock.patch.object(admin.os,'open',side_effect=replaced):
                self.assertIsNotNone(admin.read_authority_changes(root,max_bytes=65536)[2])
            self.assertEqual(target.read_text(),text)


class ReleaseDiagnosticsTests(unittest.TestCase):
    held_with_void=ReleaseRegressions.held_with_void
    def select(self,store,operators):
        return lifecycle.release_selection(store.rows,release.scope(integration=release.RELEASE_COMMIT),lambda a,b:True,operators=operators)
    def test_no_candidate_does_not_repeat_canonical_projections(self):
        store=release.NativeStore(tasks=('trial-a',)).seed('trial-a')
        release.add_review_chain(store,'trial-a')
        with mock.patch.object(lifecycle,'unreadable_hold_chain',side_effect=AssertionError('unneeded projection')):
            self.assertEqual(audit.review_void_report(store.rows,[]),{})
    def test_malformed_comment_author_is_not_an_eligibility_claim(self):
        for author in ([],{},None):
            with self.subTest(author=author):
                store=self.held_with_void();store.rows[0]['comments'][-1]['author']=author
                self.assertEqual(audit.review_void_report(store.rows,[]),{})
    def failed_attestation_fork(self,target,task='trial-a'):
        store=release.NativeStore(tasks=(task,)).seed(task)
        release.add_review_chain(store,task)
        issue=store.rows[0]
        contribution=json.loads(issue['comments'][0]['text'][len(reviews.PREFIX):])
        failed=dict(schema_version=1,operation='attest',operation_id='failed-attest',task=task,
            previous='2',contribution='1',verdict='failed',commit=contribution['commit'],
            base_commit=contribution['base_commit'],method='Synthetic check',reason='Synthetic failure')
        fork=dict(contribution,operation_id='failed-repair-fork',previous='2',supersedes='1',commit='9'*40)
        for identity,author,payload in [('3',release.VERIFIER,failed),('4',release.ACTOR,fork)]:
            reviews.validate(payload,task)
            issue['comments'].append(dict(id=identity,author=author,created_at='2026-10-01T00:00:%02dZ'%int(identity),
                text=reviews.PREFIX+canonical_bytes(payload).decode()))
        original=next(c['text'] for c in issue['comments'] if c['id']==target)
        payload=dict(schema_version=1,operation=recovery.OPERATION,operation_id='failed-repair-void',task=task,
            target=target,target_kind='contribution-review',target_sha256=recovery.digest(original),original=original,
            reason='Synthetic repair',disposition='void',operator=release.OPERATOR)
        recovery.validate(payload,task)
        issue['comments'].append(dict(id='void-failed-repair',author=release.OPERATOR,
            created_at='2026-10-01T00:00:05Z',text=recovery.PREFIX+canonical_bytes(payload).decode()))
        return store

    def test_failed_attestation_and_repair_fork_voids_are_decisive(self):
        for target,operation,counter in [('3','attest','unheld'),('4','contribute','held')]:
            with self.subTest(target=target):
                store=self.failed_attestation_fork(target);before=copy.deepcopy(store.rows)
                live=self.select(store,[])
                self.assertFalse(live['targets'])
                self.assertIn('unreadable hold chain',live['skipped'][0]['reason'])
                notes=live.get('review_void_diagnostics',[])
                self.assertEqual(len(notes),1,live)
                note=notes[0]
                self.assertEqual((note['target_id'],note['target_operation']), (target,operation))
                self.assertEqual((note['current_result'],note['counterfactual_result']),('unreadable',counter))
                self.assertIsNone(note['hold_id']);self.assertIsNone(note['release_id'])
                restored=self.select(store,[release.OPERATOR])
                self.assertEqual(bool(restored['targets']),counter=='unheld')
                self.assertFalse(restored.get('review_void_diagnostics'))
                self.assertEqual(before,store.rows)

    def test_diagnostics_never_change_any_live_release_result(self):
        stores=[self.held_with_void(),self.failed_attestation_fork('3'),self.failed_attestation_fork('4')]
        for store in stores:
            with self.subTest(comments=len(store.rows[0]['comments'])):
                with mock.patch.object(audit,'review_void_diagnostics',return_value=[]):
                    live=self.select(store,[])
                reported=self.select(store,[])
                operative={k:v for k,v in reported.items() if not k.startswith('review_void_diagnostics')}
                self.assertEqual(operative,live)
                self.assertEqual(operative['targets'],[])
                self.assertEqual(len(operative['skipped']),1)
                self.assertIn('unreadable hold chain',operative['skipped'][0]['reason'])

    def test_operator_removal_readd_recomputes_without_ever_using_a_counterfactual_verdict(self):
        store=self.held_with_void();before=copy.deepcopy(store.rows)
        for operators,warn in (([release.OPERATOR],False),([],True),([release.OPERATOR],False)):
            with self.subTest(operators=operators):
                result=self.select(store,operators)
                self.assertEqual(bool(result.get('review_void_diagnostics')),warn)
                if warn:
                    self.assertFalse(result['targets'])
                    self.assertIn('unreadable hold chain',result['skipped'][0]['reason'])
                    with self.assertRaisesRegex(ValueError,'cannot be read'):
                        lifecycle.require_unheld_fact(store.rows[0],'integrated','trial-a',operators)
                else:self.assertTrue(result['targets'])
        self.assertEqual(store.rows,before)
    def test_inactive_void_of_a_release_reports_unreadable_not_an_invented_hold_result(self):
        store=self.held_with_void();issue=store.rows[0];issue['comments']=issue['comments'][:3]
        release_payload=dict(schema_version=1,operation='release-hold',operation_id='release-proof',task='trial-a',
            previous='3',contribution='1',hold='3',reason='Synthetic release')
        reviews.validate(release_payload,'trial-a')
        release_comment=dict(id='4',author=release.OPERATOR,created_at='2026-10-01T00:00:30Z',text=reviews.PREFIX+canonical_bytes(release_payload).decode())
        original=json.loads(issue['comments'][0]['text'][len(reviews.PREFIX):])
        fork=dict(original,operation_id='fork-release',previous='3',supersedes='1',commit='9'*40)
        issue['comments'] += [release_comment,dict(id='repair',author=release.ACTOR,created_at='2026-10-01T00:00:40Z',text=reviews.PREFIX+canonical_bytes(fork).decode())]
        payload=dict(schema_version=1,operation=recovery.OPERATION,operation_id='void-release',task='trial-a',target='4',
            target_kind='contribution-review',target_sha256=recovery.digest(release_comment['text']),original=release_comment['text'],
            reason='Synthetic release repair',disposition='void',operator=release.OPERATOR)
        issue['comments'].append(dict(id='void-release-comment',author=release.OPERATOR,created_at='2026-10-01T00:00:50Z',text=recovery.PREFIX+canonical_bytes(payload).decode()))
        result=self.select(store,[]);self.assertFalse(result['targets'])
        self.assertTrue(result.get('review_void_diagnostics'))
        diagnostic=result['review_void_diagnostics'][0]
        self.assertEqual(diagnostic['current_result'],'unreadable');self.assertEqual(diagnostic['counterfactual_result'],'held')
        self.assertEqual((diagnostic['hold_id'],diagnostic['release_id']),('3','4'))
        restored=self.select(store,[release.OPERATOR]);self.assertFalse(restored['targets'])
        self.assertIn('held from integration',restored['skipped'][0]['reason']);self.assertFalse(restored.get('review_void_diagnostics'))
    def test_protected_or_damaged_void_never_becomes_a_decisive_warning(self):
        store=self.held_with_void();store.rows[0]['comments']=[c for c in store.rows[0]['comments'] if c['id']!='repair-fork']
        result=self.select(store,[]);self.assertFalse(result.get('review_void_diagnostics'))
        self.assertIn('held from integration',result['skipped'][0]['reason'])
        store=self.held_with_void();store.rows[0]['comments'][-1]['text']+='damaged'
        self.assertFalse(self.select(store,[]).get('review_void_diagnostics'))
    def test_decisive_approval_void_names_its_operation_without_inventing_hold_ids(self):
        store=self.held_with_void();issue=store.rows[0];issue['comments']=issue['comments'][:2]
        target=issue['comments'][1];approve=json.loads(target['text'][len(reviews.PREFIX):]);approve.update(operation_id='approval-fork',previous='1')
        issue['comments'].append(dict(id='alternate',author='other-reviewer',created_at='2026-10-01T00:00:30Z',text=reviews.PREFIX+canonical_bytes(approve).decode()))
        payload=dict(schema_version=1,operation=recovery.OPERATION,operation_id='void-approval',task='trial-a',target='2',target_kind='contribution-review',
            target_sha256=recovery.digest(target['text']),original=target['text'],reason='Synthetic irrelevant repair',disposition='void',operator=release.OPERATOR)
        issue['comments'].append(dict(id='irrelevant-void',author=release.OPERATOR,created_at='2026-10-01T00:00:40Z',text=recovery.PREFIX+canonical_bytes(payload).decode()))
        self.assertTrue(reviews.applied_review_voids(issue,[release.OPERATOR])[0])
        result=self.select(store,[])
        notes=result.get('review_void_diagnostics',[])
        self.assertEqual(len(notes),1,result)
        self.assertEqual(notes[0]['target_operation'],'approve')
        self.assertEqual(notes[0]['current_result'],'unreadable')
        self.assertEqual(notes[0]['counterfactual_result'],'unheld')
        self.assertIsNone(notes[0]['hold_id']);self.assertIsNone(notes[0]['release_id'])
    def test_query_is_bounded_and_writes_no_native_or_host_state(self):
        store=self.held_with_void();before=copy.deepcopy(store.rows);calls=len(store.calls)
        query=dict(release.release_payload([]),dimension=lifecycle.RELEASE_QUERY)
        result=lifecycle.release_query(query,release.ACTOR,store.run,operators=[])
        self.assertTrue(result.get('review_void_diagnostics'))
        self.assertEqual(result['review_void_diagnostics'][0]['current_result'],'unreadable')
        self.assertEqual(store.calls[calls:],[['export','--all']]);self.assertEqual(store.rows,before)
    def test_diagnostics_have_a_whole_report_bound_and_omitted_count(self):
        all_rows=[]
        for number in range(51):all_rows+=self.held_with_void('trial-'+str(number)).rows
        result=audit.review_void_report(all_rows,[])
        self.assertEqual(len(result.get('review_void_diagnostics',[])),50)
        self.assertEqual(result['review_void_diagnostics_total'],51);self.assertEqual(result['review_void_diagnostics_omitted'],1)
    def test_cli_dry_run_displays_diagnostics_without_any_host_history_read(self):
        store=self.held_with_void()
        with tempfile.TemporaryDirectory() as scratch:
            root=Path(scratch);(root/'deployment.private.json').write_text(json.dumps(dict(operators=[])))
            (root/'client.json').write_text(json.dumps(dict(root=str(root))))
            (root/'export.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in store.rows))
            (root/'release.json').write_text(json.dumps(release.release_payload([])))
            args=['lifecycle.py','release','--config',str(root/'client.json'),'--project','p','--actor',release.ACTOR,
                  '--file',str(root/'release.json'),'--repo',str(root),'--export',str(root/'export.jsonl'),'--dry-run']
            out=io.StringIO()
            with mock.patch.object(sys,'argv',args),mock.patch.object(lifecycle,'git_ancestry',return_value=lambda a,b:True),mock.patch.object(audit,'load_assignment_history',side_effect=AssertionError('host audit only')),contextlib.redirect_stdout(out):
                self.assertIsNone(lifecycle.main())
            result=json.loads(out.getvalue());self.assertFalse(result['targets'])
            self.assertTrue(result.get('review_void_diagnostics'))
            self.assertEqual(result['review_void_diagnostics'][0]['current_result'],'unreadable')
            self.assertEqual(result['review_void_diagnostics_source'],'local')


if __name__=='__main__':unittest.main()
