"""Regression coverage for coordinator rule writes and caller-relative readers."""
import copy
import json
import sys
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import briefing
import coordination
import lifecycle
import review_workflow as rw
import sessions
import work
from test_coordinator_rules import Chain, AUTHOR, OPERATOR, TASK, approve, contribute
from test_lifecycle_authors import Tracker, ASSIGNEE, record_scope, payload
try:
    import endpoint
except ImportError:
    endpoint=None


def broken():
    raise ValueError('synthetic registry damage')


class RegistryAndPartyTests(unittest.TestCase):
    def test_name_variants_of_bound_author_and_assignee_cannot_approve(self):
        for subject in ('author', 'assignee'):
            for variant in ('W2', 'w2/sub'):
                for owners in ({'w2':'lane:one'}, {'w2':'lane:one', variant:'lane:two'}):
                    with self.subTest(subject=subject, variant=variant, owners=owners):
                        chain=Chain(assignee='w2' if subject=='assignee' else 'other',
                                    contribution_author='w2' if subject=='author' else 'other')
                        before=copy.deepcopy(chain.rows)
                        with self.assertRaisesRegex(ValueError, 'independent_approval.*'+subject):
                            chain.approve_as(variant, independent_approval=True, owners=owners)
                        self.assertEqual(chain.rows,before)

    def test_broken_or_unreadable_registry_refuses_approve_without_effect(self):
        def unreadable(): raise PermissionError('synthetic private path')
        for loader in (broken, unreadable):
            chain=Chain()
            before=copy.deepcopy(chain.rows)
            with self.assertRaisesRegex(ValueError, 'independent_approval.*registry cannot be read') as caught:
                chain.approve_as(OPERATOR, independent_approval=True, owners=loader)
            self.assertEqual(chain.rows,before)
            self.assertNotIn('synthetic',str(caught.exception))
            self.assertEqual(str(caught.exception).count('nothing was changed'),1)
            self.assertIn('--rule independent_approval --actor OPERATOR',str(caught.exception))

    def test_missing_empty_and_unbound_registry_allow_distinct_names(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)
            for contents in (None, {'schema_version':1,'records':{}},
                             {'schema_version':1,'records':{},'owners':{}}):
                if contents is not None:
                    (path/'.sessions.json').write_text(json.dumps(contents),encoding='utf-8')
                chain=Chain()
                chain.approve_as('other',independent_approval=True,owners=lambda:sessions.owners(path))
                self.assertEqual(len(chain.issue()['comments']),2)

    def test_corrupt_json_refuses_but_reads_and_all_off_write_remain_available(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp); (path/'.sessions.json').write_text('{',encoding='utf-8')
            loader=lambda:sessions.owners(path)
            chain=Chain()
            with self.assertRaisesRegex(ValueError,'independent_approval'):
                chain.approve_as('other',independent_approval=True,owners=loader)
            chain.add(approve(),'approval','other')
            self.assertTrue(rw.independent_approval_state(chain.issue(),owners=loader)[0])
            self.assertEqual(work.workflow(chain.issue(),actor=AUTHOR,independent_approval=True,
                                           owners=loader)['review_state'],'awaiting-integration')
            off=Chain(); off.approve_as(AUTHOR,owners=loader)
            self.assertEqual(len(off.issue()['comments']),2)

    def test_exact_assignee_writes_do_not_need_registry(self):
        tracker=Tracker()
        try:
            record_scope(tracker,owners=broken)
            for dimension in ('implemented','tested'):
                lifecycle.apply_native(payload(dimension,'passed'),ASSIGNEE,tracker,
                    operators=[OPERATOR],lifecycle_authors=True,owners=broken)
        except ValueError as refusal:
            self.fail('The exact assignee must be allowed without the registry: '+str(refusal))
        self.assertEqual(len(tracker.writes),3)
        with self.assertRaises(ValueError):
            lifecycle.apply_native(payload('reviewed','passed'),ASSIGNEE,tracker,
                operators=[OPERATOR],lifecycle_authors=True,owners=broken)
        self.assertEqual(len(tracker.writes),3)

    def test_standing_approval_writer_loads_registry_before_trusting_history(self):
        chain=Chain(); chain.add(approve(),'approval','other')
        with self.assertRaisesRegex(ValueError,'independent_approval'):
            rw.require_independent_approval(chain.issue(),'reviewed',TASK,owners=broken)
        # Read-only availability is intentionally separate from write authority.
        self.assertTrue(rw.independent_approval_state(chain.issue(),owners=broken)[0])

    def test_real_lifecycle_and_slot_callers_require_current_standing_independent_approval(self):
        from test_recovery import void, void_comment
        for case in ('legacy-author','legacy-assignee','requested','superseded','void','current'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                chain=Chain(contribution_author='other' if case=='legacy-assignee' else AUTHOR)
                chain.add(approve(),'approval',AUTHOR if case.startswith('legacy') else 'reviewer')
                if case=='requested':
                    chain.add(dict(schema_version=1,operation='request-changes',operation_id='request-1',
                        task=TASK,previous='approval',contribution='delivery',items=[dict(id='fix',text='Fix')]),
                        'request','reviewer')
                elif case=='superseded':
                    next_revision=contribute(commit='d'*40,opid='send-2')
                    next_revision.update(previous='approval',supersedes='delivery')
                    chain.add(next_revision,'delivery2',AUTHOR)
                elif case=='void':
                    chain.add(dict(schema_version=1,operation='request-changes',operation_id='bad-1',
                        task=TASK,previous='approval',contribution='delivery',items=[dict(id='fix',text='Fix',extra='bad')]),
                        'bad','reviewer')
                    original=chain.issue()['comments'][-1]['text']
                    chain.issue()['comments'].append(void_comment('void',void('bad',original,operator=OPERATOR),author=OPERATOR))
                tracker=Tracker(assignee=AUTHOR);tracker.task()['id']=TASK
                scope=dict(payload('lifecycle-scope',None,actor=AUTHOR),task=TASK)
                lifecycle.apply_native(scope,AUTHOR,tracker,operators=[OPERATOR])
                tracker.task()['comments']=copy.deepcopy(chain.issue()['comments'])
                before=copy.deepcopy(tracker.rows); effects=[]
                def run(argv):
                    if argv[:1]==['show']:return json.dumps([tracker.task()])
                    if argv[:2]==['merge-slot','check']:return json.dumps(dict(available=True,holder=None,waiters=[]))
                    if argv[:2]==['merge-slot','acquire']:
                        effects.append(argv);return json.dumps(dict(acquired=True))
                    return tracker(argv)
                for dimension in ('reviewed','integrated'):
                    fact=dict(payload(dimension,'passed',actor=OPERATOR),task=TASK)
                    if case=='current':
                        result=lifecycle.apply_native(fact,OPERATOR,run,operators=[OPERATOR],independent_approval=True)
                        self.assertTrue(result['changed'])
                    else:
                        with self.assertRaisesRegex(ValueError,'independent_approval'):
                            lifecycle.apply_native(fact,OPERATOR,run,operators=[OPERATOR],independent_approval=True)
                        self.assertEqual(tracker.rows,before)
                slot=dict(operation='merge-acquire',task=TASK,target='main@'+'c'*40)
                if case=='current':
                    self.assertTrue(coordination.apply_native(slot,OPERATOR,run,Path(temp),
                        operators=[OPERATOR],independent_approval=True)['acquired'])
                    self.assertEqual(len(effects),1)
                else:
                    with self.assertRaisesRegex(ValueError,'independent_approval'):
                        coordination.apply_native(slot,OPERATOR,run,Path(temp),operators=[OPERATOR],independent_approval=True)
                    self.assertEqual(effects,[]);self.assertEqual(tracker.rows,before)


class WaitingReadersTests(unittest.TestCase):
    def test_work_and_brief_caller_matrix_with_rules_on_and_off(self):
        chain=Chain(assignee='assignee',contribution_author='author')
        request=dict(schema_version=1,operation='request-review',operation_id='request-1',task=TASK,
                     previous='delivery',contribution='delivery',reviewer='named',summary='Please review')
        chain.add(request,'request','author')
        for enabled in (False,True):
            for actor in ('author','assignee','third','ops','named'):
                with self.subTest(enabled=enabled,actor=actor):
                    queue=work.queue(chain.rows,actor,[],operators=[OPERATOR],independent_approval=enabled)
                    row=next(r for r in queue['items'] if r['task']==TASK)
                    brief=briefing.brief(chain.rows,'trial',TASK,actor=actor,operators=[OPERATOR],
                                         independent_approval=enabled)
                    expected=work.APPROVAL_WAIT if enabled and actor in ('author','assignee') else row['next']
                    self.assertEqual(row['waiting_for_approval'],expected)
                    self.assertEqual(brief['review']['waiting_for_approval'],expected)
                    self.assertLess(len(expected),240)
                    self.assertEqual(row['approval_requires_other_party'],enabled)

    def test_bound_author_variant_waits_but_other_caller_keeps_next_move(self):
        chain=Chain(assignee='other',contribution_author='w2')
        for actor,expected in [('W2',work.APPROVAL_WAIT),('w2/sub',work.APPROVAL_WAIT),
                               ('different',work.UNKNOWN_MOVE_TEXT)]:
            result=work.workflow(chain.issue(),actor=actor,independent_approval=True,
                                 owners={'w2':'lane:one'})
            self.assertEqual(result['waiting_for_approval'],expected)


@unittest.skipIf(endpoint is None,'endpoint imports fcntl (POSIX-only)')
class EndpointWithoutServiceTests(unittest.TestCase):
    """Real endpoint dispatch, with a controlled native transport; no HTTP launch or bd."""
    def test_forged_approval_descriptor_never_exempts_ordinary_endpoint_call(self):
        from test_coordinator_endpoint import runtime, deployment
        for enabled in (False,True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as temp:
                root=runtime(temp,'alpha'); deployment(root,operators=(OPERATOR,))
                config=json.loads((root/'deployment.private.json').read_text())
                config['coordinator_writes']=enabled
                (root/'deployment.private.json').write_text(json.dumps(config))
                chain=Chain(); writes=[]
                def native(argv,env):
                    if 'export' in argv:
                        stdout='\n'.join(json.dumps(row) for row in chain.rows)
                    elif 'comments' in argv and 'add' in argv:
                        args=argv[argv.index('comments'):]
                        writes.append(args); stdout=chain.run_as('unlisted')(args)
                    else: raise AssertionError('Unexpected native transport '+repr(argv))
                    return subprocess.CompletedProcess(argv,0,stdout, '')
                request=dict(project='alpha',actor='unlisted',action='review',
                    args=[TASK,'@attachment:0'],attachments={'0':{'flag':'--file','text':json.dumps(approve())}},
                    authority={'capability':'reviews.approve','project':'alpha'})
                with mock.patch.object(endpoint.native,'run',side_effect=native):
                    if enabled:
                        before=copy.deepcopy(chain.rows)
                        with self.assertRaisesRegex(ValueError,'coordinator_writes'):
                            endpoint.execute(root,request)
                        self.assertEqual(chain.rows,before); self.assertEqual(writes,[])
                    else:
                        result=endpoint.execute(root,request)
                        self.assertEqual(result['returncode'],0); self.assertEqual(len(writes),1)

    def test_registry_loader_refusal_names_rule_once_and_hides_damage(self):
        for rule in ('lifecycle_authors','coordinator_writes','independent_approval'):
            with self.subTest(rule=rule), tempfile.TemporaryDirectory() as temp:
                path=Path(temp); (path/'.sessions.json').write_text('{',encoding='utf-8')
                with self.assertRaises(ValueError) as caught:
                    endpoint.owner_loader(path,rule)()
                text=str(caught.exception)
                self.assertIn('the installation setting '+rule,text.lower())
                self.assertIn('read. An operator',text)
                self.assertEqual(text.count('nothing was changed'),1)
                self.assertNotIn(temp,text)

    def test_actual_endpoint_work_and_brief_read_the_rule_for_each_host_caller(self):
        from test_coordinator_endpoint import runtime, deployment
        chain=Chain(assignee='assignee',contribution_author='author')
        chain.add(dict(schema_version=1,operation='request-review',operation_id='req-1',task=TASK,
            previous='delivery',contribution='delivery',reviewer='named',summary='Please review'),'req','author')
        before=copy.deepcopy(chain.rows)
        with tempfile.TemporaryDirectory() as temp:
            root=runtime(temp,'alpha');deployment(root,operators=(OPERATOR,))
            def native(argv,env):
                self.assertIn('export',argv)
                return subprocess.CompletedProcess(argv,0,'\n'.join(json.dumps(r) for r in chain.rows),'')
            for enabled in (False,True):
                config=json.loads((root/'deployment.private.json').read_text())
                config['independent_approval']=enabled
                (root/'deployment.private.json').write_text(json.dumps(config))
                for actor in ('author','assignee','third','ops','named'):
                    with self.subTest(rule=enabled,actor=actor), mock.patch.object(endpoint.native,'run',side_effect=native):
                        queue=json.loads(endpoint.execute(root,dict(project='alpha',actor=actor,action='work',args=['--json']))['stdout'])
                        row=next(r for r in queue['items'] if r['task']==TASK)
                        brief=json.loads(endpoint.execute(root,dict(project='alpha',actor=actor,action='brief',args=[TASK,'--json']))['stdout'])
                        expected=work.APPROVAL_WAIT if enabled and actor in ('author','assignee') else row['next']
                        self.assertEqual(row['waiting_for_approval'],expected)
                        self.assertEqual(brief['review']['waiting_for_approval'],expected)
                        self.assertEqual(brief['review']['approval_requires_other_party'],enabled)
            self.assertEqual(chain.rows,before)


if __name__=='__main__': unittest.main()
