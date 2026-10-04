"""Independent DAG/delivery oracle: expected state never reads lifecycle events."""
import copy
import random
import unittest
import lifecycle
import briefing
import work
from test_lifecycle_release import NativeStore, ACTOR, payload, scope, release_payload

def commit(n):return ('%02x'%n)*20

class Scenario:
    ENVIRONMENTS=('production','staging')
    RELEASES={'R1':4,'R2':6,'R3':8,'H':9}
    DELIVERIES={'ta':[2],'tx':[5,7],'tc':[3],'th':[9]}
    def __init__(self):
        self.store=NativeStore(tasks=tuple(self.DELIVERIES))
        self.expected={e:{t:None for t in self.DELIVERIES} for e in self.ENVIRONMENTS}
        self.serial=0;self.commands=[]
        for task,ds in self.DELIVERIES.items():
            for n in ds:
                sc=scope(source=commit(n+20),integration=commit(n))
                self.write(payload(task,'lifecycle-scope',operation='seed-sc-'+task+str(n),scope_value=sc))
                self.write(payload(task,'integrated',operation='seed-in-'+task+str(n),scope_value=sc))
    @staticmethod
    def ancestor(a,b):
        a=int(a[:2],16);b=int(b[:2],16)
        return a==b or (b==9 and a<=4) or (b<=8 and a<=b)
    def write(self,p):
        # Common apply_native API, available on pre-fix rev4. No new fixture API.
        native=self.store.run
        def run(args):
            before=len(self.store.rows);answer=native(args)
            for row in self.store.rows[before:]:
                row['created_at']='2026-10-01T%02d:%02d:%02dZ'%(before//3600,before//60%60,before%60)
            return answer
        return lifecycle.apply_native(p,ACTOR,run)
    def chosen(self,t,r):
        return next((n for n in reversed(self.DELIVERIES[t]) if self.ancestor(commit(n),commit(self.RELEASES[r]))),None)
    def run(self,r,e='production',verified=False,rollback=False,op=None):
        self.serial+=1;op=op or 'run-%d'%self.serial
        sc=dict(source_commit='',integration_commit=commit(self.RELEASES[r]),release_id=r,environment=e)
        before=copy.deepcopy(self.store.rows)
        try:
            selection=(lifecycle.rollback_selection(self.store.rows,sc,self.ancestor) if rollback else
                       lifecycle.release_selection(self.store.rows,sc,self.ancestor,live_verified=verified))
            p=release_payload(selection['targets'],operation=op,release=r,environment=e,scope=sc,live_verified=verified)
            if rollback:p.update(rollback=True,supersede=selection['supersede'],supersede_scopes=selection['supersede_scopes'])
            if p['targets'] or rollback:self.write(p)
        except ValueError as exc:
            assert self.store.rows==before,'refused operation wrote native state'
            self.commands.append((r,e,verified,rollback,op,str(exc)))
            return str(exc)
        for t in self.DELIVERIES:
            chosen=self.chosen(t,r);cur=self.expected[e][t]
            if rollback:self.expected[e][t]=(chosen,r) if chosen is not None else None
            elif chosen is not None and (cur is None or (chosen!=cur[0] and self.ancestor(commit(cur[0]),commit(self.RELEASES[r])))):
                self.expected[e][t]=(chosen,r)
        self.commands.append((r,e,verified,rollback,op,None));return None
    def check(self,test):
        rows=self.store.rows;where='commands='+repr(self.commands)
        owed=lifecycle.evidence_owed(rows)
        queue=work.queue(rows,ACTOR,['--limit','100'])['items']
        queue={x['task']:x for x in queue}
        for e in self.ENVIRONMENTS:
            q=release_payload([],operation='read',environment=e,dimension=lifecycle.RELEASE_QUERY)
            result=lifecycle.release_query(q,ACTOR,self.store.run)
            actual={x['task']:(int(x['integration_commit'][:2],16),x['release_id']) for x in result['live_releases'] if x['liveness'] in ('live','deployed')}
            test.assertEqual(actual,{t:v for t,v in self.expected[e].items() if v is not None},where)
            for t,expected in self.expected[e].items():
                live=[g['release_id'] for g in owed if g['environment']==e for x in g['tasks'] if x['task']==t and x['live'] in ('live','deployed')]
                test.assertEqual(live,[] if expected is None else [expected[1]],where+' evidence-owed '+t)
        for t in self.DELIVERIES:
            b=briefing.brief(rows,'trial',t);w=queue[t]
            test.assertEqual((b['deployed_delivery'],b['deployed_live']),(w['deployed_delivery'],w['deployed_live']),where)
            sc=b['lifecycle_scope'];e=sc['environment']['text'];r=sc['release_id']['text']
            if e in self.expected:
                expected=self.expected[e][t]
                if b['deployed_live']=='live':
                    test.assertIsNotNone(expected,where)
                    test.assertEqual((int(b['deployed_delivery']['integration_commit'][:2],16),r),expected,where)
                if expected is not None and expected[1]==r:test.assertEqual(b['deployed_live'],'live',where)

class RuleTests(unittest.TestCase):
    def step(self,s,*args,**kw):
        error=s.run(*args,**kw);s.check(self);return error
    def test_dropped_twice_delivered_task_does_not_resurrect(self):
        s=Scenario()
        for r in ('R1','R2','R3'):self.step(s,r)
        self.step(s,'R1',rollback=True);self.assertIsNone(s.expected['production']['tx'])
    def test_stale_deploy_and_rollback_ids_refuse_before_writes(self):
        s=Scenario();self.step(s,'R1');self.step(s,'R3',op='A');self.step(s,'R1',rollback=True,op='B')
        self.assertIn('new operation ID',self.step(s,'R3',op='A'))
        self.step(s,'R3',op='C');self.assertIn('new operation ID',self.step(s,'R1',rollback=True,op='B'))
    def test_first_verified_roll_forward_and_repeated_rollbacks(self):
        s=Scenario();self.step(s,'R1');self.step(s,'R3');self.step(s,'R1',rollback=True)
        self.step(s,'R3',verified=True);self.step(s,'R1',rollback=True);self.step(s,'R3',verified=True)
        self.assertEqual(s.expected['production']['tx'],(7,'R3'))
    def test_hotfix_two_step_and_mainline_return(self):
        s=Scenario();self.step(s,'R1');self.step(s,'R2')
        self.assertIn('never deployed',self.step(s,'H',rollback=True))
        self.step(s,'H');self.assertEqual(s.expected['production']['tx'],(5,'R2'))
        self.step(s,'H',rollback=True);self.assertIsNone(s.expected['production']['tx'])
        self.step(s,'R3');self.assertEqual(s.expected['production']['th'],(9,'H'))
        self.step(s,'R3',rollback=True);self.assertIsNone(s.expected['production']['th'])
    def test_random_sequences_use_an_independent_oracle(self):
        for seed in range(8):
            rng=random.Random(seed);s=Scenario()
            for r in ('R1','R2','R3'):self.step(s,r)
            for _ in range(55):
                if s.commands and rng.randrange(4)==0:
                    r,e,v,rb,op,_err=rng.choice(s.commands)
                    self.step(s,r,e,verified=v,rollback=rb,op=op)
                else:self.step(s,rng.choice(tuple(s.RELEASES)),rng.choice(s.ENVIRONMENTS),verified=bool(rng.randrange(2)),rollback=rng.randrange(3)==0)
    def test_newest_negative_event_beats_any_older_positive(self):
        s=Scenario()
        for r in ('R1','R2','R3'):self.step(s,r)
        entry=next(x for x in lifecycle.scoped_evidence(s.store.rows,('deployed',lifecycle.LIVE)) if x['id']=='tx')
        latest=max(entry['scopes'],key=lambda c:(c.get(lifecycle.LIVE) or {}).get('order',-1))
        s.write(payload('tx',lifecycle.LIVE,'superseded',operation='negative',scope_value=latest['scope']))
        s.expected['production']['tx']=None;s.check(self)

if __name__=='__main__':unittest.main()
