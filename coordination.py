"""Serialized native child creation and project merge-slot coordination."""
import argparse
import json
import os
import re
from pathlib import Path
from requirements import content_hash, load_json


def atomic(path, data):
    tmp=path.with_suffix('.tmp')
    with tmp.open('w',encoding='utf-8') as stream:
        # Escape non-ASCII in coordination JSON so the sidecar remains
        # readable even when a Windows caller uses its legacy default codec.
        json.dump(data,stream,ensure_ascii=True,sort_keys=True)
        stream.flush();os.fsync(stream.fileno())
    os.replace(tmp,path)


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,160}',value):
        raise ValueError('Invalid identifier')
    return value


def apply_native(p, actor, run, project):
    """Caller holds canonical project lock. Journals belong to runtime, never Git."""
    if not isinstance(p,dict):raise ValueError('Expected object')
    op=p.get('operation')
    if op=='create-child':
        if set(p)!={'operation','request_id','parent','title','description','type'}:raise ValueError('Invalid child request fields')
        identifier(p['request_id']);identifier(p['parent'])
        if not isinstance(p['title'],str) or not p['title'].strip() or not isinstance(p['description'],str):raise ValueError('Child needs title and description')
        if p['type'] not in ('task','bug','feature','chore','decision'):raise ValueError('Invalid child type')
        identity=content_hash({'request_id':p['request_id']})
        digest=content_hash({'actor':actor,'payload':p})
        journal=project/'.coordination-requests';journal.mkdir(exist_ok=True)
        receipt=journal/(identity+'.json')
        prior=load_json(receipt) if receipt.exists() else None
        if prior and prior['sha256']!=digest:raise ValueError('Request ID already reserved for different content or actor')
        label='request:'+identity
        found=json.loads(run(['list','--all','--limit','0','--label',label,'--json'])) or []
        if len(found)>1:raise ValueError('Duplicate native request records; operator reconciliation required')
        if found:
            if 'request-content:'+digest not in (found[0].get('labels') or []):raise ValueError('Native request content mismatch')
            result={'id':found[0]['id'],'reconciled':True}
            atomic(receipt,{'sha256':digest,'status':'complete','id':result['id']})
            return result
        if prior:raise ValueError('Reserved request has no visible issue; outcome uncertain. Operator must reconcile before any new request; do not allocate another ID.')
        atomic(receipt,{'sha256':digest,'status':'pending'})
        args=['create','--title',p['title'],'--parent',p['parent'],'--description',p['description'],'--type',p['type'],'--no-inherit-labels','--labels',label+',request-content:'+digest,'--json']
        if p['type']=='decision':args.append('--validate')
        issue=json.loads(run(args))
        if not isinstance(issue,dict) or not issue.get('id'):raise ValueError('Create response uncertain; reconcile same request ID')
        atomic(receipt,{'sha256':digest,'status':'complete','id':issue['id']})
        return {'id':issue['id'],'reconciled':False}
    if op not in ('merge-create','merge-check','merge-acquire','merge-release'):raise ValueError('Unknown coordination operation')
    expected={'operation','task','target'} if op=='merge-acquire' else {'operation'}
    if set(p)!=expected:raise ValueError('Invalid merge request fields; holder is always the request actor')
    context_path=project/'.merge-context.json'
    if op=='merge-create':return json.loads(run(['merge-slot','create','--json']))
    state=json.loads(run(['merge-slot','check','--json']))
    context=load_json(context_path) if context_path.exists() else None
    if op=='merge-check':
        state['context']=context if context and context['holder']==state.get('holder') else None
        return state
    if op=='merge-release':
        if state.get('available'):return {'released':False,'available':True}
        if state.get('holder')!=actor:raise ValueError('Only the current holder may release the merge slot')
        result=json.loads(run(['merge-slot','release','--holder',actor,'--json']))
        # Keep context for recovery/audit; native holder remains authoritative.
        return result
    identifier(p['task'])
    if not isinstance(p['target'],str) or not p['target'].strip():raise ValueError('Integration target is required')
    desired={'holder':actor,'task':p['task'],'target':p['target']}
    if not state.get('available'):
        if state.get('holder')==actor:
            if context!=desired:raise ValueError('Already held with different or missing context; inspect before handoff')
            return {'acquired':True,'reconciled':True,'context':context}
        return {'acquired':False,'holder':state.get('holder')}
    # Validate task exists before reserving; check native state on retry.
    run(['show',p['task'],'--json'])
    atomic(context_path,desired)
    result=json.loads(run(['merge-slot','acquire','--holder',actor,'--json']))
    result['context']=desired if result.get('acquired') else None
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('config','project','actor','file'):p.add_argument('--'+name,required=True)
    a=p.parse_args()
    from client import request
    try:
        result=request(load_json(a.config),a.project,a.actor,[json.dumps(load_json(a.file))],action='coordinate')
        print(result['stdout'],end='')
        if result['stderr']:print(result['stderr'],file=__import__('sys').stderr,end='')
        raise SystemExit(result['returncode'])
    except (ValueError,OSError,RuntimeError) as exc:raise SystemExit(str(exc))


if __name__=='__main__':main()
