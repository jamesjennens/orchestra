"""Serialized native child creation and project merge-slot coordination."""
import argparse
import json
import os
import re
import time
from pathlib import Path
from requirements import content_hash, load_json


# Native `bd create --validate` enforces the type templates, and the identical
# `bd create --dry-run` asks that same validator before anything is written.
# That preflight is the single source of truth for validation: this module never
# decides whether a description is valid. The header list below is only a
# documentation hint, appended to an error when native validation itself reports
# missing sections; it is not a gate and must not refuse a text bd accepts.
REQUIRED_SECTIONS = {'decision': ('Decision', 'Rationale', 'Alternatives Considered')}
# A receipt in one of these states may be replaced under the same request ID by
# its original actor, or by any actor after an explicit operator release.
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
    """Documentation hint naming the section headers native validation expects."""
    required=required_sections(child_type)
    if not required:return ''
    return 'Required %s section headers: %s.' % (child_type, ', '.join('## '+name for name in required))


def validation_hint(child_type, message):
    """Add the header hint only when native validation itself names sections.

    bd accepts many heading spellings (case-insensitive substring match), so the
    gate is always bd's own preflight; this helper never refuses or rewrites a
    native decision. Unrelated errors such as a missing parent are returned
    unchanged.
    """
    text=str(message).strip() or 'Native create refused the request'
    requirement=template_requirement(child_type)
    if not requirement or requirement in text:return text
    if 'section' not in text.lower():return text
    return text.rstrip('. ')+'. '+requirement


def resubmittable(prior, actor):
    """Whether a recorded reservation may be replaced under the same request ID.

    Only a failed or operator-released receipt is reusable, and it stays bound to
    its original actor unless the operator recorded an explicit `--any-actor`
    release. A pending reservation never falls through, so an uncertain native
    outcome cannot be overwritten by retrying or by another actor.
    """
    if not isinstance(prior,dict) or prior.get('status') not in RELEASABLE:return False
    if prior.get('actor')==actor:return True
    audit=prior.get('reconciliation') or {}
    return prior.get('status')=='released' and audit.get('any_actor') is True


def receipt_record(prior, digest, status, **extra):
    """Receipt with bounded history so a release stays auditable after resubmission."""
    record={'sha256':digest,'status':status}
    record.update(extra)
    history=list(prior.get('history') or []) if isinstance(prior,dict) else []
    if isinstance(prior,dict):
        history.append({key:value for key,value in prior.items() if key!='history'})
    if history:record['history']=history[-5:]
    return record


def reconcile_request(project, request_id, actor, reason, disposition, run, at=None, any_actor=False):
    """Operator-only: resolve a stuck reservation without guessing native state.

    * `complete` completes the receipt from the single labelled native issue when
      the original content is unknown, recording the operator in the audit.
    * `failed`/`released` confirm natively that no issue exists first. `released`
      with `any_actor=True` explicitly opens the ID to any actor; otherwise the
      original actor stays bound to it.
    * Repeating the identical reconciliation is idempotent, while a differing
      retry is refused with the recorded audit instead of silently returning
      `already: true` and keeping a different record.
    """
    identifier(request_id);identifier(actor)
    if disposition not in ('failed','released','complete'):
        raise ValueError('Disposition must be failed, released or complete')
    if any_actor and disposition!='released':raise ValueError('Only a released disposition may be opened to any actor')
    if not isinstance(reason,str) or not reason.strip():raise ValueError('A reconciliation reason is required')
    identity=content_hash({'request_id':request_id})
    receipt=project/'.coordination-requests'/(identity+'.json')
    if not receipt.exists():raise ValueError('No coordination request reservation exists for that request ID')
    prior=load_json(receipt)
    if not isinstance(prior,dict) or not isinstance(prior.get('status'),str):
        raise ValueError('Malformed coordination request receipt; inspect before reconciling')
    audit=prior.get('reconciliation') or {}
    if prior['status'] in RELEASABLE:
        same=(audit.get('actor')==actor and audit.get('reason')==reason
              and audit.get('disposition')==disposition and bool(audit.get('any_actor'))==any_actor)
        if same:
            return {'request_id':request_id,'status':prior['status'],'reconciled':False,'already':True,
                    'reconciliation':audit}
        raise ValueError('Reconciliation conflict: request %s is already %s by actor %r (disposition %r, reason %r); the recorded audit is kept and a differing retry is refused.'
                         % (request_id,prior['status'],audit.get('actor'),audit.get('disposition'),audit.get('reason')))
    if prior['status']!='pending':raise ValueError('Coordination request is not pending; nothing to reconcile')
    label='request:'+identity
    found=json.loads(run(['list','--all','--limit','0','--label',label,'--json'])) or []
    if len(found)>1:raise ValueError('Duplicate native request records; manual operator reconciliation required')
    at=at or time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    if found:
        if disposition!='complete':
            raise ValueError('A native issue already exists for this request; complete the reservation with --disposition complete instead of releasing it')
        audit={'actor':actor,'reason':reason,'disposition':'complete','at':at,'completed_from':'native'}
        updated=receipt_record(prior,prior.get('sha256'),'complete',id=found[0]['id'],
                               actor=prior.get('actor') or actor,reconciliation=audit)
        if prior.get('error'):updated['error']=prior['error']
        atomic(receipt,updated)
        return {'request_id':request_id,'status':'complete','reconciled':True,'id':found[0]['id'],'reconciliation':audit}
    if disposition=='complete':
        raise ValueError('No labelled native issue exists for this request; completion needs one, so use --disposition failed or released')
    audit={'actor':actor,'reason':reason,'disposition':disposition,'at':at}
    if any_actor:audit['any_actor']=True
    default_error=('Released by the operator after confirming no native issue was created.'
                   if disposition=='released' else
                   'Marked failed by the operator after confirming no native issue was created.')
    atomic(receipt,receipt_record(prior,prior.get('sha256'),disposition,actor=prior.get('actor'),
                                  error=prior.get('error') or default_error,reconciliation=audit))
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
        reusable=resubmittable(prior,actor)
        if prior and prior['sha256']!=digest and not reusable:raise ValueError('Request ID already reserved for different content or actor')
        label='request:'+identity
        found=json.loads(run(['list','--all','--limit','0','--label',label,'--json'])) or []
        if len(found)>1:raise ValueError('Duplicate native request records; operator reconciliation required')
        if found:
            if 'request-content:'+digest not in (found[0].get('labels') or []):
                # A receipt completed by the operator from the native issue has no
                # recoverable original content; accept its recorded id.
                if isinstance(prior,dict) and prior.get('status')=='complete' and prior.get('id')==found[0]['id']:
                    return {'id':found[0]['id'],'reconciled':True}
                raise ValueError('Native request content mismatch')
            result={'id':found[0]['id'],'reconciled':True}
            atomic(receipt,receipt_record(prior,digest,'complete',id=result['id'],actor=actor))
            return result
        if prior and not reusable:
            raise ValueError('Reserved request has no visible issue; outcome uncertain. Operator must reconcile before any new request; do not allocate another ID.')
        args=['create','--title',p['title'],'--parent',p['parent'],'--description',p['description'],'--type',p['type'],'--no-inherit-labels','--labels',label+',request-content:'+digest,'--json']
        if p['type']=='decision':args.append('--validate')
        # Preflight the identical create natively BEFORE any reservation exists.
        # `--dry-run` writes nothing: native validation and a missing parent are
        # refused here, so a refused create leaves the request ID free and no
        # pending receipt can strand it. bd stays the single source of truth for
        # what a valid description is.
        try:
            run(args+['--dry-run'])
        except ValueError as refusal:
            raise ValueError(validation_hint(p['type'],refusal))
        pending=receipt_record(prior,digest,'pending',actor=actor)
        atomic(receipt,pending)
        try:
            raw=run(args)
        except ValueError as refusal:
            # The preflight passed, so a nonzero exit is NOT a definitive refusal:
            # bd can commit the issue and then fail (for example while adding the
            # request label), and the commit may not be visible to an immediate
            # label read. Fail closed: keep the reservation pending rather than
            # releasing it to a retry that could duplicate the issue.
            raise ValueError(validation_hint(p['type'],refusal)
                             +' The native create did not confirm an issue; the outcome is uncertain, so the request stays pending. Inspect native state and reconcile this request ID (admin.py reconcile-request) before retrying.')
        try:
            issue=json.loads(raw)
        except (TypeError,ValueError):
            issue=None
        if not isinstance(issue,dict) or not issue.get('id'):raise ValueError('Create response uncertain; reconcile same request ID')
        atomic(receipt,receipt_record(pending,digest,'complete',id=issue['id'],actor=actor))
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
