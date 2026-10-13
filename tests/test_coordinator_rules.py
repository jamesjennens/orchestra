"""Slices 6 and 7 (kittrial-5bb.197), parts (b) and (c), and the three settings.

(b) nobody passes their own work: `review approve` is refused for the contribution's
author and for the task's assignee, on the same name-based terms the follows gate
uses. (c) coordinator writes: approval and taking the merge slot only for a listed
actor of the project (or an actor of a listed operator's principal). Both are
per-installation settings read by `admin.coordinator_rules`; absent or false is OFF,
so an installation that configures nothing behaves exactly as before.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin
import coordination
import review_workflow as rw
from requirements import canonical_bytes

TASK='trial-task'
AUTHOR='alice/session'
REVIEWER='reviewer'
OPERATOR='ops'
COMMIT='a'*40


def contribute(commit=COMMIT,opid='send-1'):
    return dict(schema_version=1,operation='contribute',operation_id=opid,task=TASK,previous=None,
                repository='ssh://git/example',commit=commit,base_commit='b'*40,
                delivery=dict(kind='bundle',path='server:/rev.bundle',sha256='c'*64),
                summary='Contribution delivered',supersedes=None)


def approve(contribution='delivery',opid='approve-1'):
    return dict(schema_version=1,operation='approve',operation_id=opid,task=TASK,previous=contribution,
                contribution=contribution,summary='Reviewed and approved')


class Chain:
    """The least native state `review_workflow.execute` needs: one task and its comments."""

    def __init__(self,assignee=AUTHOR,contribution_author=AUTHOR):
        self.rows=[dict(id=TASK,title='Synthetic task',issue_type='task',status='open',assignee=assignee,
                        labels=[],comments=[],created_by='someone',created_at='2026-09-15T00:00:00Z')]
        self.add(contribute(),'delivery',contribution_author)

    def issue(self):return self.rows[0]

    def add(self,body,cid,author):
        self.issue().setdefault('comments',[]).append(
            dict(id=cid,author=author,created_at='2026-09-16T00:00:00Z',
                 text=rw.PREFIX+canonical_bytes(body).decode()))

    def run_as(self,actor):
        def run(args):
            if args[0]=='comments' and args[1]=='add':
                cid='comment-%d'%(len(self.issue()['comments'])+1)
                self.issue()['comments'].append(dict(id=cid,author=actor,created_at='2026-09-17T00:00:00Z',
                                                     text=args[3]))
                return json.dumps(dict(id=cid))
            raise AssertionError('unexpected native call %r'%(args,))
        return run

    def approve_as(self,actor,operators=(OPERATOR,),**settings):
        return rw.execute(self.rows,TASK,actor,approve(),self.run_as(actor),operators=list(operators),**settings)


class SelfApprovalTests(unittest.TestCase):
    def test_off_by_default_the_author_still_approves_its_own_contribution(self):
        chain=Chain()
        result=chain.approve_as(AUTHOR,independent_approval=False)
        self.assertFalse(result['reconciled'])
        self.assertEqual(len(chain.issue()['comments']),2)

    def test_with_the_rule_on_the_contribution_author_cannot_approve(self):
        chain=Chain()
        with self.assertRaises(ValueError) as caught:
            chain.approve_as(AUTHOR,independent_approval=True)
        message=str(caught.exception)
        self.assertIn('independent_approval',message)
        self.assertIn('author',message)
        self.assertEqual(len(chain.issue()['comments']),1)   # zero writes

    def test_with_the_rule_on_the_assignee_cannot_approve(self):
        chain=Chain(assignee=AUTHOR,contribution_author='somebody-else')
        with self.assertRaises(ValueError) as caught:
            chain.approve_as(AUTHOR,independent_approval=True)
        message=str(caught.exception)
        self.assertIn('independent_approval',message)
        self.assertIn('assignee',message)
        self.assertEqual(len(chain.issue()['comments']),1)

    def test_with_the_rule_on_another_party_may_approve(self):
        chain=Chain()
        result=chain.approve_as(REVIEWER,independent_approval=True)
        self.assertFalse(result['reconciled'])
        self.assertEqual(len(chain.issue()['comments']),2)

    def test_the_assignee_comparison_is_the_normalisation_rule_3_uses(self):
        chain=Chain(assignee='alice',contribution_author='somebody-else')
        with self.assertRaises(ValueError):
            chain.approve_as('alice/session',independent_approval=True)
        self.assertEqual(len(chain.issue()['comments']),1)

    def test_the_author_comparison_is_not_exact_name_only(self):
        # M07c: a case variant of the author's name is the same party and is refused.
        chain=Chain(assignee='somebody-else',contribution_author=AUTHOR)
        with self.assertRaises(ValueError) as caught:
            chain.approve_as(AUTHOR.upper(),independent_approval=True)
        self.assertIn('independent_approval',str(caught.exception))
        self.assertEqual(len(chain.issue()['comments']),1)

    def test_two_actors_of_one_principal_are_one_party(self):
        # Item 1: compare PRINCIPALS where the registry gives one (sessions.owners).
        chain=Chain(assignee='somebody-else',contribution_author='w2')
        owners={'w2':'lane:w2','w2b':'lane:w2'}
        with self.assertRaises(ValueError):
            chain.approve_as('w2b',operators=['ops'],independent_approval=True,owners=owners)
        self.assertEqual(len(chain.issue()['comments']),1)

    def test_a_name_only_actor_and_a_principal_actor_cannot_be_told_apart(self):
        # The documented limit: they are treated as distinct parties, so the approval
        # counts (the task says names only for an actor with no principal).
        chain=Chain(assignee='somebody-else',contribution_author='w2')
        owners={'w2b':'lane:other'}
        chain.approve_as('w2b',operators=['ops'],independent_approval=True,owners=owners)
        self.assertEqual(len(chain.issue()['comments']),2)


class CoordinatorWritesTests(unittest.TestCase):
    def test_off_by_default_an_unlisted_actor_still_approves(self):
        chain=Chain()
        chain.approve_as(REVIEWER,coordinator_writes=False)
        self.assertEqual(len(chain.issue()['comments']),2)

    def test_with_the_rule_on_an_unlisted_actor_cannot_approve(self):
        chain=Chain()
        with self.assertRaises(ValueError) as caught:
            chain.approve_as(REVIEWER,coordinator_writes=True)
        message=str(caught.exception)
        self.assertIn('coordinator_writes',message)
        self.assertIn('listed operator',message)
        self.assertEqual(len(chain.issue()['comments']),1)

    def test_with_the_rule_on_a_listed_operator_approves(self):
        chain=Chain()
        chain.approve_as(OPERATOR,coordinator_writes=True)
        self.assertEqual(len(chain.issue()['comments']),2)

    def test_an_actor_of_a_listed_operators_principal_approves(self):
        chain=Chain()
        owners={'ops':'lane:kit','ops/session':'lane:kit'}
        chain.approve_as('ops/session',operators=['ops'],coordinator_writes=True,owners=owners)
        self.assertEqual(len(chain.issue()['comments']),2)

    def test_an_actor_of_another_principal_cannot_approve(self):
        chain=Chain()
        owners={'ops':'lane:kit','other/session':'lane:other'}
        with self.assertRaises(ValueError):
            chain.approve_as('other/session',operators=['ops'],coordinator_writes=True,owners=owners)
        self.assertEqual(len(chain.issue()['comments']),1)

    def test_with_only_the_independent_rule_on_the_list_is_not_consulted(self):
        chain=Chain()
        chain.approve_as(REVIEWER,operators=[],independent_approval=True,coordinator_writes=False)
        self.assertEqual(len(chain.issue()['comments']),2)

    def test_both_rules_on_an_operator_that_is_the_assignee_is_still_refused(self):
        chain=Chain(assignee=OPERATOR,contribution_author='somebody-else')
        with self.assertRaises(ValueError) as caught:
            chain.approve_as(OPERATOR,independent_approval=True,coordinator_writes=True)
        self.assertIn('independent_approval',str(caught.exception))
        self.assertEqual(len(chain.issue()['comments']),1)


class ListedActorTests(unittest.TestCase):
    def test_the_allowlist_matches_the_whole_name_and_case(self):
        self.assertTrue(rw.listed_operator_actor('ops',['ops'],{}))
        self.assertFalse(rw.listed_operator_actor('Ops',['ops'],{}))
        self.assertFalse(rw.listed_operator_actor('ops-2',['ops'],{}))

    def test_with_no_owners_map_only_a_listed_name_counts(self):
        self.assertFalse(rw.listed_operator_actor('ops/session',['ops'],None))
        self.assertFalse(rw.listed_operator_actor('ops/session',['ops'],{}))
        # A principal of its own is not a LISTED principal: somebody on the allowlist
        # must belong to it.
        self.assertFalse(rw.listed_operator_actor('ops/session',['ops'],{'ops/session':'lane:kit'}))
        self.assertTrue(rw.listed_operator_actor('ops/session',['ops'],{'ops':'lane:kit','ops/session':'lane:kit'}))

    def test_a_principal_counts_only_when_a_listed_operator_shares_it(self):
        self.assertTrue(rw.listed_operator_actor('coord',['ops'],{'ops':'lane:kit','coord':'lane:kit'}))
        self.assertFalse(rw.listed_operator_actor('coord',['ops'],{'coord':'lane:kit'}))
        self.assertFalse(rw.listed_operator_actor('coord',['ops'],{'ops':'lane:other','coord':'lane:kit'}))

    def test_a_name_that_is_not_a_plain_actor_is_not_repeated_in_a_refusal(self):
        self.assertEqual(rw.named_rule_actor('bad name\x1b[31m'),'an actor')
        self.assertEqual(rw.named_rule_actor('ops'),'"ops"')

    def test_a_plain_listed_name_needs_no_registry_read(self):
        # Item 4c: a listed operator is decided without reading the registry, so a
        # damaged registry never stops their operation.
        def broken():raise ValueError('The session registry cannot be read')
        self.assertTrue(rw.listed_operator_actor('ops',['ops'],broken))

    def test_an_unlisted_name_reads_the_registry_lazily(self):
        reads=[]
        def loader():
            reads.append(1)
            return {'ops':'lane:kit','cp':'lane:kit'}
        self.assertTrue(rw.listed_operator_actor('cp',['ops'],loader))
        self.assertEqual(len(reads),1)
        reads.clear()
        self.assertFalse(rw.listed_operator_actor('nobody',['ops'],loader))
        self.assertEqual(len(reads),1)

    def test_party_key_compares_principals_then_names(self):
        owners={'ops':'lane:kit','ops/session':'lane:kit','other':'lane:other'}
        self.assertEqual(rw.party_key('ops',owners),('principal','lane:kit'))
        self.assertEqual(rw.party_key('ops/session',owners),('principal','lane:kit'))
        self.assertEqual(rw.party_key('other',owners),('principal','lane:other'))
        # A name-only actor falls back to the normalised name; a principal actor and a
        # name-only actor are never the same party.
        self.assertEqual(rw.party_key('OPS/x',{}),('name','ops'))
        self.assertNotEqual(rw.party_key('ops',owners),rw.party_key('OPS/x',{}))
        self.assertTrue(rw.same_party('ops','ops/session',owners))
        self.assertFalse(rw.same_party('ops','other',owners))

    def test_party_key_falls_back_to_names_when_the_registry_is_unreadable(self):
        def broken():raise ValueError('The session registry cannot be read')
        self.assertEqual(rw.party_key('ops',broken),('name','ops'))
        self.assertTrue(rw.same_party('OPS','ops',broken))


class MergeSlotTests(unittest.TestCase):
    """Slice 7 also covers taking the merge slot (coordination.apply_native)."""

    def merge(self,root,run,actor,**settings):
        return coordination.apply_native({'operation':'merge-acquire','task':TASK,'target':'a'*40},
                                         actor,run,root,**settings)

    def slot_run(self,calls):
        def run(argv):
            calls.append(list(argv))
            if argv==['export','--all']:return ''   # merge-acquire reads for a stored hold; none here
            if argv[:2]==['merge-slot','check']:return json.dumps({'available':True,'holder':None,'waiters':[]})
            if argv[:2]==['merge-slot','acquire']:return json.dumps({'acquired':True})
            if argv[:1]==['show']:return '[]'
            raise AssertionError('unexpected native call %r'%(argv,))
        return run

    def test_off_by_default_any_actor_still_takes_the_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            calls=[]
            result=self.merge(Path(temp),self.slot_run(calls),REVIEWER,operators=[OPERATOR])
            self.assertTrue(result.get('acquired'))
            self.assertTrue(calls)

    def test_with_the_rule_on_an_unlisted_actor_cannot_take_the_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            calls=[]
            with self.assertRaises(ValueError) as caught:
                self.merge(Path(temp),self.slot_run(calls),REVIEWER,operators=[OPERATOR],coordinator_writes=True)
            self.assertIn('coordinator_writes',str(caught.exception))
            self.assertEqual(calls,[])            # nothing was even read from the slot

    def test_with_the_rule_on_a_listed_operator_takes_the_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            calls=[]
            result=self.merge(Path(temp),self.slot_run(calls),OPERATOR,operators=[OPERATOR],
                              coordinator_writes=True)
            self.assertTrue(result.get('acquired'))

    def test_with_the_rule_on_a_listed_principals_actor_takes_the_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            registry=Path(temp)/'.sessions.json'
            registry.write_text(json.dumps({'schema_version':1,'records':{},
                                            'owners':{'ops':'lane:kit','ops/session':'lane:kit'}}),
                                encoding='utf-8')
            calls=[]
            result=self.merge(Path(temp),self.slot_run(calls),'ops/session',operators=[OPERATOR],
                              coordinator_writes=True,owners=lambda: {'ops':'lane:kit','ops/session':'lane:kit'})
            self.assertTrue(result.get('acquired'))


class SwitchTests(unittest.TestCase):
    """`admin.py coordinator-rules`: operator-gated, audited, and OFF by default."""

    def root(self,deployment=None):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        document={'operators':['ops'],'password':'x'}
        document.update(deployment or {})
        (root/'deployment.private.json').write_text(json.dumps(document),encoding='utf-8')
        return root

    def switch(self,root,action,rule=None,actor='ops'):
        with mock.patch.dict(os.environ,{'ORCHESTRA_OPERATORS':'ops'}):
            return admin.coordinator_rules_switch(root,action,rule,actor)

    def document(self,root):
        return json.loads((root/'deployment.private.json').read_text(encoding='utf-8'))

    def test_every_rule_is_off_on_a_fresh_install(self):
        root=self.root()
        status=self.switch(root,'status')
        self.assertEqual(status['coordinator_rules'],
                         {'lifecycle_authors':False,'independent_approval':False,'coordinator_writes':False})
        self.assertTrue(status['audit_readable'])
        self.assertEqual(status['audit_records'],0)
        self.assertFalse(status['changed'])

    def test_a_flip_is_recorded_and_reported(self):
        root=self.root()
        flipped=self.switch(root,'on','lifecycle_authors')
        self.assertTrue(flipped['changed'])
        self.assertTrue(flipped['coordinator_rules']['lifecycle_authors'])
        self.assertIs(self.document(root)['lifecycle_authors'],True)
        audit=self.document(root)['coordinator_rules_audit']
        self.assertEqual([(entry['actor'],entry['rule'],entry['action'],entry['previous']) for entry in audit],
                         [('ops','lifecycle_authors','on',False)])
        self.assertTrue(self.switch(root,'status','lifecycle_authors')['coordinator_rules']['lifecycle_authors'])
        off=self.switch(root,'off','lifecycle_authors')
        self.assertFalse(off['coordinator_rules']['lifecycle_authors'])
        self.assertNotIn('lifecycle_authors',self.document(root))
        self.assertEqual(self.document(root)['coordinator_rules_audit'][-1]['previous'],True)

    def test_a_flip_that_changes_nothing_writes_nothing(self):
        root=self.root()
        self.switch(root,'on','coordinator_writes')
        before=self.document(root)
        again=self.switch(root,'on','coordinator_writes')
        self.assertFalse(again['changed'])
        self.assertEqual(len(again['coordinator_rules']),3)
        self.assertEqual(self.document(root),before)

    def test_the_three_rules_are_independent(self):
        root=self.root()
        self.switch(root,'on','independent_approval')
        self.assertEqual(self.document(root)['independent_approval'],True)
        self.assertNotIn('lifecycle_authors',self.document(root))
        self.assertNotIn('coordinator_writes',self.document(root))

    def test_only_a_listed_operator_may_read_or_flip(self):
        root=self.root()
        with self.assertRaises(ValueError):
            self.switch(root,'status',actor='mallory')
        with self.assertRaises(ValueError):
            self.switch(root,'on','lifecycle_authors',actor='mallory')
        self.assertNotIn('lifecycle_authors',self.document(root))

    def test_on_and_off_require_a_rule_and_a_known_one(self):
        root=self.root()
        with self.assertRaises(ValueError):
            self.switch(root,'on',None)
        with self.assertRaises(ValueError):
            self.switch(root,'on','nonsense')

    def test_status_does_not_require_a_rule(self):
        self.assertIn('coordinator_rules',self.switch(self.root(),'status'))

    def test_a_malformed_value_reads_as_off_with_a_warning(self):
        root=self.root({'lifecycle_authors':'on'})
        warnings=[]
        values=admin.coordinator_rules(root,warnings=warnings)
        self.assertFalse(values['lifecycle_authors'])
        self.assertEqual(len(warnings),1)
        self.assertIn('lifecycle_authors',warnings[0])

    def test_a_huge_or_escaped_malformed_value_is_bounded_and_escaped(self):
        # Item 6: the warning must not repeat a 200 kB value whole or print ESC raw.
        root=self.root({'lifecycle_authors':'x'*200000+'\x1b[31m'})
        warnings=[]
        admin.coordinator_rules(root,warnings=warnings)
        self.assertEqual(len(warnings),1)
        self.assertLess(len(warnings[0]),400)
        self.assertNotIn('\x1b',warnings[0])
        self.assertNotIn('x'*1000,warnings[0])

    def test_off_on_a_non_bool_value_writes_false(self):
        # Item 6: `off` on a non-bool is a real flip, not a no-op that leaves the value.
        root=self.root({'lifecycle_authors':'on'})
        flipped=self.switch(root,'off','lifecycle_authors')
        self.assertTrue(flipped['changed'])
        self.assertNotIn('lifecycle_authors',self.document(root))
        self.assertFalse(admin.coordinator_rules(root)['lifecycle_authors'])

    def test_status_bounds_a_hand_edited_audit_entry(self):
        # Item 6: a hand-edited entry (huge actor) is printed bounded and escaped.
        root=self.root({'coordinator_rules_audit':[{'at':'2026-01-01T00:00:00Z','actor':'a'*200000,
                                                    'rule':'lifecycle_authors','action':'on','previous':False}]})
        status=self.switch(root,'status')
        self.assertLess(len(status['audit']['actor']),400)
        self.assertNotIn('a'*1000,status['audit']['actor'])

    def test_a_flip_takes_the_deployment_lock(self):
        # M19: a flip without the lock leaves two simultaneous flips able to lose each
        # other. The lock must be entered by a real flip.
        root=self.root()
        with mock.patch.object(admin,'review_writes_lock',wraps=admin.review_writes_lock) as lock:
            flipped=self.switch(root,'on','coordinator_writes')
        self.assertTrue(flipped['changed'])
        self.assertTrue(lock.called)

    def test_the_switch_gate_reads_the_operator_list_strictly(self):
        # M11b: a non-strict gate would authorize an actor from the environment while
        # the deployment list says otherwise. The strict gate refuses the mismatch.
        root=self.root()
        with mock.patch.dict(os.environ,{'ORCHESTRA_OPERATORS':'somebody-else'}):
            with self.assertRaises(ValueError):
                admin.coordinator_rules_switch(root,'on','lifecycle_authors','ops')
        self.assertNotIn('lifecycle_authors',self.document(root))

    def test_a_damaged_audit_is_kept_aside_and_the_flip_still_works(self):
        root=self.root({'coordinator_rules_audit':'hand edited'})
        status=self.switch(root,'status')
        self.assertFalse(status['audit_readable'])
        flipped=self.switch(root,'on','lifecycle_authors')
        self.assertTrue(flipped['changed'])
        document=self.document(root)
        kept=[key for key in document if key.startswith('coordinator_rules_audit_damaged_')]
        self.assertEqual(len(kept),1)
        self.assertEqual(document[kept[0]],'hand edited')
        self.assertEqual(len(document['coordinator_rules_audit']),1)

    def test_the_audit_list_is_bounded(self):
        root=self.root()
        for _ in range(admin.COORDINATOR_RULES_AUDIT_MAX+3):
            self.switch(root,'on','coordinator_writes')
            self.switch(root,'off','coordinator_writes')
        self.assertEqual(len(self.document(root)['coordinator_rules_audit']),admin.COORDINATOR_RULES_AUDIT_MAX)

    def test_the_rule_keys_and_the_reader_agree(self):
        root=self.root()
        self.assertEqual(sorted(admin.coordinator_rules(root)),sorted(admin.COORDINATOR_RULE_KEYS))
        with self.assertRaises(ValueError):
            admin.coordinator_rule(root,'nonsense')


class SetupStatusTests(unittest.TestCase):
    def test_setup_status_reports_the_three_rules(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        (root/'deployment.private.json').write_text(
            json.dumps({'operators':['ops'],'password':'x','independent_approval':True}),encoding='utf-8')
        (root/'projects'/'demo').mkdir(parents=True)
        status=admin.project_setup_status(root,'demo')
        self.assertEqual(status['coordinator_rules']['rules'],
                         {'lifecycle_authors':False,'independent_approval':True,'coordinator_writes':False})
        self.assertTrue(status['coordinator_rules']['enabled'])
        self.assertIn('independent_approval',status['coordinator_rules']['detail'])

    def test_setup_status_says_none_when_all_are_off(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        (root/'deployment.private.json').write_text(json.dumps({'operators':['ops'],'password':'x'}),encoding='utf-8')
        (root/'projects'/'demo').mkdir(parents=True)
        status=admin.project_setup_status(root,'demo')
        self.assertFalse(status['coordinator_rules']['enabled'])
        self.assertIn('none of the coordinator rules',status['coordinator_rules']['detail'])


class CommandLineTests(unittest.TestCase):
    def test_rule_given_twice_is_refused(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        (root/'deployment.private.json').write_text(json.dumps({'operators':['ops'],'password':'x'}),encoding='utf-8')
        argv=['admin.py','--root',str(root),'coordinator-rules','on','--rule','lifecycle_authors',
              '--rule','independent_approval','--actor','ops']
        with mock.patch.object(admin,'root_path',return_value=root), mock.patch.object(sys,'argv',argv):
            with self.assertRaises(SystemExit) as caught:
                admin.run_main()
        self.assertIn('--rule may be given only once',str(caught.exception))


if __name__=='__main__':
    unittest.main()
