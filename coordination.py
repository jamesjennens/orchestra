"""Serialized native child creation and project merge-slot coordination."""
import argparse
import json
import os
import re
import time
from pathlib import Path
from requirements import content_hash, load_json


# Native `bd create --validate` enforces the type templates. A decision without
# these section headers is refused before any issue exists. Keep the list here so
# the create-child error message and the published templates name the same
# requirement instead of letting a worker discover it through a stuck receipt.
REQUIRED_SECTIONS = {'decision': ('Decision', 'Rationale', 'Alternatives Considered')}
# Receipt statuses whose reservation may be replaced by corrected content under
# the same request ID (a failed native validation or an operator release).
RELEASABLE = ('failed', 'released')


def atomic(path, data):
    tmp=path.with_suffix('.tmp')
    with tmp.open('w',encoding='utf-8') as stream:
        json.dump(data,stream,ensure_ascii=False,sort_keys=True)
        stream.flush();os.fsync(stream.fileno())
    os.replace(tmp,path)


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,160}',value):
        raise ValueError('Invalid identifier')
    return value


def required_sections(child_type):
    return REQUIRED_SECTIONS.get(child_type, ())


def template_requirement(child_type):
    """Human-readable statement of the section headers native validation needs."""
    required=required_sections(child_type)
    if not required:return ''
    return 'Required %s section headers: %s.' % (child_type, ', '.join('## '+name for name in required))


def missing_sections(child_type, description):
    """Section headers absent from a description; tolerant of heading level/case."""
    required=required_sections(child_type)
    if not required or not isinstance(description,str):return ()
    headings=set()
    for line in description.splitlines():
        stripped=line.strip()
        if not stripped.startswith('#'):continue
        headings.add(re.sub(r'^#+\s*','',stripped).strip().lower())
    return tuple(name for name in required if name.lower() not in headings)


def receipt_record(prior, digest, status, **extra):
    """Receipt with bounded history so a release stays auditable after resubmission."""
    record={'sha256':digest,'status':status}
    record.update(extra)
    history=list(prior.get('history') or []) if isinstance(prior,dict) else []
    if isinstance(prior,dict):
        history.append({key:value for key,value in prior.items() if key!='history'})
    if history:record['history']=history[-5:]
    return record


def refused_create(prior, receipt, digest, label, run, refusal, child_type):
    """Resolve a nonzero native create without assuming whether the issue exists.

    A nonzero exit is a definitive refusal, but the label read is what proves no
    issue was created; anything unconfirmable keeps the reservation pending.
    """
    message=str(refusal).strip() or 'Native create refused the request'
    requirement=template_requirement(child_type)
    if requirement and requirement not in message:message=message.rstrip('. ')+'. '+requirement
    try:
        found=json.loads(run(['list','--all','--limit','0','--label',label,'--json'])) or []
    except Exception:
        raise ValueError(message+' Native outcome is uncertain; inspect native state before retrying the same request ID.')
    if len(found)>1:raise ValueError('Duplicate native request records; operator reconciliation required')
    if found:
        if 'request-content:'+digest not in (found[0].get('labels') or []):raise ValueError('Native request content mismatch after refused create; operator reconciliation required')
        atomic(receipt,receipt_record(prior,digest,'complete',id=found[0]['id']))
        return {'id':found[0]['id'],'reconciled':True}
    atomic(receipt,receipt_record(prior,digest,'failed',error=message))
    raise ValueError(message)


def reconcile_request(project, request_id, actor, reason, disposition, run, at=None):
    """Operator-only: confirm no native issue exists, then release a stuck request."""
    identifier(request_id);identifier(actor)
    if disposition not in ('failed','released'):raise ValueError('Disposition must be failed or released')
    if not isinstance(reason,str) or not reason.strip():raise ValueError('A reconciliation reason is required')
    identity=content_hash({'request_id':request_id})
    receipt=project/'.coordination-requests'/(identity+'.json')
    if not receipt.exists():raise ValueError('No coordination request reservation exists for that request ID')
    prior=load_json(receipt)
    if not isinstance(prior,dict) or not isinstance(prior.get('status'),str):
        raise ValueError('Malformed coordination request receipt; inspect before reconciling')
    if prior['status'] in RELEASABLE:
        return {'request_id':request_id,'status':prior['status'],'reconciled':False,'already':True,
                'reconciliation':prior.get('reconciliation')}
    if prior['status']!='pending':raise ValueError('Coordination request is not pending; nothing to reconcile')
    label='request:'+identity
    found=json.loads(run(['list','--all','--limit','0','--label',label,'--json'])) or []
    if len(found)>1:raise ValueError('Duplicate native request records; manual operator reconciliation required')
    if found:raise ValueError('A native issue already exists for this request; complete the reservation instead of releasing it')
    audit={'actor':actor,'reason':reason,'disposition':disposition,'at':at or time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
    atomic(receipt,receipt_record(prior,prior.get('sha256'),disposition,
                                  error=prior.get('error') or 'Released by the operator after confirming no native issue was created.',
                                  reconciliation=audit))
    return {'request_id':request_id,'status':disposition,'reconciled':True,'reconciliation':audit}


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
        released=isinstance(prior,dict) and prior.get('status') in RELEASABLE
        if prior and prior['sha256']!=digest and not released:raise ValueError('Request ID already reserved for different content or actor')
        label='request:'+identity
        found=json.loads(run(['list','--all','--limit','0','--label',label,'--json'])) or []
        if len(found)>1:raise ValueError('Duplicate native request records; operator reconciliation required')
        if found:
            if 'request-content:'+digest not in (found[0].get('labels') or []):raise ValueError('Native request content mismatch')
            result={'id':found[0]['id'],'reconciled':True}
            atomic(receipt,receipt_record(prior,digest,'complete',id=result['id']))
            return result
        if prior and not released:
            raise ValueError('Reserved request has no visible issue; outcome uncertain. Operator must reconcile before any new request; do not allocate another ID.')
        missing=missing_sections(p['type'],p['description'])
        if missing:
            # Validate the type template before any pending reservation exists, but
            # keep the attempt auditable and releasable under the same request ID.
            message='Missing required %s section(s): %s. %s' % (p['type'],', '.join('## '+name for name in missing),template_requirement(p['type']))
            atomic(receipt,receipt_record(prior,digest,'failed',error=message))
            raise ValueError(message)
        pending=receipt_record(prior,digest,'pending')
        atomic(receipt,pending)
        args=['create','--title',p['title'],'--parent',p['parent'],'--description',p['description'],'--type',p['type'],'--no-inherit-labels','--labels',label+',request-content:'+digest,'--json']
        if p['type']=='decision':args.append('--validate')
        try:
            raw=run(args)
        except ValueError as refusal:
            # The runtime raises ValueError for a nonzero native exit. Confirm the
            # absence of the issue before releasing; uncertain outcomes stay pending.
            return refused_create(pending,receipt,digest,label,run,refusal,p['type'])
        try:
            issue=json.loads(raw)
        except (TypeError,ValueError):
            issue=None
        if not isinstance(issue,dict) or not issue.get('id'):raise ValueError('Create response uncertain; reconcile same request ID')
        atomic(receipt,receipt_record(pending,digest,'complete',id=issue['id']))
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
