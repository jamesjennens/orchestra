"""Synthetic operational checks. Use ONLY a disposable deployment."""
import argparse
import json
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from client import request
from lifecycle import project_facts
from requirements import content_hash


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('config','project','output'):parser.add_argument('--'+name,required=True)
    a=parser.parse_args();cfg=json.loads(Path(a.config).read_text());checks=[]
    def call(args,actor='alice',action='bd',ok=True,path=None):
        result=request(cfg,a.project,actor,args,action,path)
        if ok and result['returncode']:raise AssertionError(result)
        return result
    def coordinate(payload,actor='alice',ok=True):return call([json.dumps(payload)],actor,'coordinate',ok)
    def mark(name):checks.append({'check':name,'result':'PASS'});print('PASS',name,flush=True)
    token=uuid.uuid4().hex
    job=json.loads(call(['create','Operations '+token,'--type','epic','--json'])['stdout'])['id']
    def brief(n):return {'operation':'create-child','request_id':token+'-'+str(n),'parent':job,'title':'Concurrent child '+str(n),'description':'Synthetic operational test','type':'task'}
    with ThreadPoolExecutor(4) as pool:
        children=list(pool.map(lambda n:json.loads(coordinate(brief(n))['stdout'])['id'],range(4)))
    assert len(set(children))==4
    retry=json.loads(coordinate(brief(0))['stdout']);assert retry['id']==children[0] and retry['reconciled']
    changed=brief(0);changed['title']='different';assert coordinate(changed,ok=False)['returncode']!=0
    mark('concurrent native child allocation and durable retry')
    coordinate({'operation':'merge-create'})
    def acquire(actor):return json.loads(coordinate({'operation':'merge-acquire','task':children[0],'target':'refs/heads/main'},actor)['stdout'])
    with ThreadPoolExecutor(2) as pool:attempts=list(pool.map(acquire,['alice','bob']))
    assert sum(x['acquired'] for x in attempts)==1
    winner='alice' if attempts[0]['acquired'] else 'bob';loser='bob' if winner=='alice' else 'alice'
    assert coordinate({'operation':'merge-release'},loser,False)['returncode']!=0
    assert acquire(winner)['reconciled']
    coordinate({'operation':'merge-release'},winner)
    assert acquire(loser)['acquired'];coordinate({'operation':'merge-release'},loser)
    mark('merge exclusivity nonholder refusal and interrupted-holder reacquisition')
    task=children[0]
    scope={'source_commit':'a'*40,'integration_commit':'','release_id':'trial-1','environment':'test'}
    def lifecycle(dim,value,opid,scope=scope):
        p={'schema_version':1,'operation_id':token+opid,'actor':'alice','task':task,'dimension':dim,'value':value,'scope':scope,'evidence':['synthetic:'+token],'provenance':'performed'}
        return json.loads(call([json.dumps(p)],action='lifecycle')['stdout'])
    lifecycle('lifecycle-scope',content_hash(scope),'scope')
    event=lifecycle('implemented','passed','implemented')
    assert lifecycle('implemented','passed','implemented')['event_id']==event['event_id']
    call([],action='refresh')
    def facts():
        rows=[json.loads(x) for x in call([],action='view',path='issues.jsonl')['stdout'].splitlines()]
        return next(r for r in project_facts(rows) if r['id']==task)
    assert facts()['facts']['implemented']['value']=='passed'
    assert facts()['facts']['deployed']['value']=='unknown'
    assert 'Lifecycle evidence' in call([],action='view',path='CURRENT.md')['stdout']
    assert call([],action='view',path='CURRENT.md')['stdout']==call([],action='view',path='COORDINATION.md')['stdout']
    lifecycle('implemented','failed','correction');call([],action='refresh');assert facts()['facts']['implemented']['value']=='failed'
    lifecycle('implemented','passed','retest')
    revised=dict(scope,source_commit='b'*40)
    lifecycle('lifecycle-scope',content_hash(revised),'scope2',revised)
    call([],action='refresh');assert facts()['facts']['implemented']['value']=='unknown'
    mark('native lifecycle evidence correction and scope rollover')
    decision=call(['create','Synthetic decision','--type','decision','--description','## Decision\nUse a disposable test.\n## Rationale\nProtect active work.\n## Alternatives Considered\nTesting live is unsuitable.','--validate','--json'])
    decision_id=json.loads(decision['stdout'])['id'];call(['lint',decision_id,'--json'])
    mark('native decision validation and lint')
    Path(a.output).write_text(json.dumps({'suite':'operational native integration','checks':checks},indent=2)+'\n')


if __name__=='__main__':main()
