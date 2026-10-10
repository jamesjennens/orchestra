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


class MergeSlotTests(unittest.TestCase):
    """Slice 7 also covers taking the merge slot (coordination.apply_native)."""

    def merge(self,root,run,actor,**settings):
        return coordination.apply_native({'operation':'merge-acquire','task':TASK,'target':'a'*40},
                                         actor,run,root,**settings)

    def slot_run(self,calls):
        def run(argv):
            calls.append(list(argv))
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
                              coordinator_writes=True)
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


if __name__=='__main__':
    unittest.main()
