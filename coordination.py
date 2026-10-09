"""Serialized native child creation and project merge-slot coordination."""
import sys
if sys.version_info < (3, 10):
    # Before every other import, and in syntax Python 3.6 reads: an older interpreter failed in
    # an import further down, with a traceback that hid the cause (kittrial-5bb.191).
    sys.stderr.write('coordination.py needs Python 3.10 or newer and was started with Python %d.%d.%d (%s). '
                     'Nothing was carried out. Run it with Python 3.10 or newer.\n'
                     % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
    sys.exit(2)
import argparse
import json
import os
import record_json
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
    return bool(audit.get('any_actor'))


def receipt_record(prior, digest, status, **extra):
    """Receipt with bounded history so a release stays auditable after resubmission."""
    record={'sha256':digest,'status':status}
    record.update(extra)
    history=list(prior.get('history') or []) if isinstance(prior,dict) else []
    if isinstance(prior,dict):
        history.append({key:value for key,value in prior.items() if key!='history'})
    if history:record['history']=history[-5:]
    return record


def issue_confirmation(issue_id, run):
    """Read back the exact native issue a `complete` disposition will attach.

    The confirmation names the parent, creator and title so the operator sees
    *which* issue is being accepted instead of silently trusting whatever
    carried a guessable `request:` label. A read that cannot confirm the exact
    issue fails closed: the receipt is not completed on an unverified issue.
    """
    try:
        rows=json.loads(run(['show',issue_id,'--json']))
    except (TypeError,ValueError,RecursionError) as exc:
        raise ValueError('Could not read native issue %s to confirm its parent, creator and title: %s' % (issue_id,exc))
    if isinstance(rows,dict):rows=[rows]
    if (not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict)
            or rows[0].get('id')!=issue_id):
        raise ValueError('Could not confirm the exact native issue %s; refusing to complete an unconfirmed receipt' % issue_id)
    row=rows[0]
    return {'id':issue_id,
            'title':row.get('title'),
            'creator':row.get('created_by') or row.get('creator') or row.get('author') or row.get('assignee'),
            'parent':row.get('parent') or row.get('parent_id')}


def reconcile_request(project, request_id, actor, reason, disposition, run, at=None, any_actor=False, issue_id=None):
    """Operator-only: resolve a stuck reservation without guessing native state.

    * `complete` completes the receipt from the single labelled native issue when
      the original content is unknown, recording the operator in the audit. It
      requires an explicit `issue_id` and prints that issue's parent, creator and
      title in the confirmation.
    * `failed`/`released` confirm natively that no issue exists first. A receipt
      that records its original actor keeps that binding; `released --any-actor`
      deliberately opens the ID to any actor. A receipt with **no recorded
      actor** (an older stuck reservation) refuses `failed`/`released` unless
      `--any-actor` is supplied on that same first call, because otherwise the
      ID would be bound to nobody and could never be resubmitted.
    * Repeating the identical reconciliation is idempotent, while a differing
      retry is refused with the recorded audit instead of silently returning
      `already: true` and keeping a different record.
    """
    identifier(request_id);identifier(actor)
    if disposition not in ('failed','released','complete'):
        raise ValueError('Disposition must be failed, released or complete')
    if issue_id is not None and (not isinstance(issue_id,str) or not issue_id.strip()):
        raise ValueError('Invalid issue ID')
    if issue_id is not None and disposition!='complete':
        raise ValueError('An issue ID applies only to a complete disposition')
    if not isinstance(reason,str) or not reason.strip():raise ValueError('A reconciliation reason is required')
    if disposition=='complete' and not issue_id:
        raise ValueError('A complete disposition requires an explicit --issue-id naming the labelled native issue; confirm its parent, creator and title before completing the receipt')
    identity=content_hash({'request_id':request_id})
    receipt=project/'.coordination-requests'/(identity+'.json')
    if not receipt.exists():raise ValueError('No coordination request reservation exists for that request ID')
    prior=load_json(receipt)
    if not isinstance(prior,dict) or not isinstance(prior.get('status'),str):
        raise ValueError('Malformed coordination request receipt; inspect before reconciling')
    audit=prior.get('reconciliation') or {}
    actorless=not prior.get('actor')
    if any_actor and disposition=='complete':
        raise ValueError('Only a failed or released disposition may be opened to any actor')
    if any_actor and disposition=='failed' and not actorless:
        raise ValueError('A failed receipt stays bound to its original actor; only a released disposition may be opened to any actor')
    at=at or time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    if prior['status'] in RELEASABLE:
        same=(audit.get('actor')==actor and audit.get('reason')==reason
              and audit.get('disposition')==disposition and bool(audit.get('any_actor'))==any_actor)
        if same:
            return {'request_id':request_id,'status':prior['status'],'reconciled':False,'already':True,
                    'reconciliation':audit}
        # A release recorded by an older kit build bound the ID to nobody because
        # the receipt had no actor. Let the operator upgrade that recorded
        # release to any_actor instead of leaving it permanently locked.
        if actorless and any_actor and disposition in ('failed','released'):
            upgraded=dict(audit)
            upgraded.update({'any_actor':True,'upgraded_by':actor,'upgraded_at':at,'upgrade_reason':reason})
            updated=receipt_record(prior,prior.get('sha256'),prior['status'],actor=prior.get('actor'),
                                   reconciliation=upgraded)
            if prior.get('error'):updated['error']=prior['error']
            atomic(receipt,updated)
            return {'request_id':request_id,'status':prior['status'],'reconciled':True,'upgraded':True,
                    'reconciliation':upgraded}
        raise ValueError('Reconciliation conflict: request %s is already %s by actor %r (disposition %r, reason %r); the recorded audit is kept and a differing retry is refused.'
                         % (request_id,prior['status'],audit.get('actor'),audit.get('disposition'),audit.get('reason')))
    if prior['status']=='complete':
        same=(audit.get('actor')==actor and audit.get('reason')==reason
              and audit.get('disposition')=='complete' and prior.get('id')==issue_id and not any_actor)
        if same:
            return {'request_id':request_id,'status':'complete','reconciled':False,'already':True,
                    'id':prior.get('id'),'reconciliation':audit}
        raise ValueError('Reconciliation conflict: request %s is already complete by actor %r (reason %r, id %r); the recorded audit is kept and a differing retry is refused.'
                         % (request_id,audit.get('actor'),audit.get('reason'),prior.get('id')))
    if prior['status']!='pending':raise ValueError('Coordination request is not pending; nothing to reconcile')
    if actorless and disposition in ('failed','released') and not any_actor:
        raise ValueError('The reservation for request %s records no actor, so a %s disposition would bind it to nobody and permanently lock the request ID. Re-run with --any-actor to open it to any actor, or use --disposition complete --issue-id if a native issue exists. See docs/OPERATIONAL_WORKFLOW.md.'
                         % (request_id,disposition))
    label='request:'+identity
    found=json.loads(run(['list','--all','--limit','0','--label',label,'--json'])) or []
    if len(found)>1:raise ValueError('Duplicate native request records; manual operator reconciliation required')
    digest=prior.get('sha256')
    labels=(found[0].get('labels') or []) if found else []
    coordinated=(not digest) or ('request-content:'+digest in labels)
    if found and disposition!='complete':
        if coordinated:
            raise ValueError('A native issue already exists for this request; complete the reservation with --disposition complete --issue-id instead of releasing it')
        # The only issue carrying the guessable request: label lacks this
        # reservation's request-content: marker, so it is a planted or foreign
        # issue and must not block the operator from releasing the request.
    if found and disposition=='complete':
        if found[0].get('id')!=issue_id:
            raise ValueError('--issue-id %s does not match the labelled native request issue %s; refusing to complete a different issue'
                             % (issue_id,found[0].get('id')))
        if not coordinated:
            raise ValueError('The labelled native issue %s does not carry request-content:%s, so it was not created for this reserved content; a same-label planted or foreign issue is refused. Inspect its parent, creator and title before deciding.'
                             % (issue_id,digest))
        details=issue_confirmation(issue_id,run)
        audit={'actor':actor,'reason':reason,'disposition':'complete','at':at,'completed_from':'native','issue':details}
        updated=receipt_record(prior,prior.get('sha256'),'complete',id=found[0]['id'],
                               actor=prior.get('actor') or actor,reconciliation=audit)
        if prior.get('error'):updated['error']=prior['error']
        atomic(receipt,updated)
        return {'request_id':request_id,'status':'complete','reconciled':True,'id':found[0]['id'],
                'issue':details,'reconciliation':audit}
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


#: The label bd puts on a project's merge slot, and the end of its id.
MERGE_SLOT_LABEL='gt:slot'
MERGE_SLOT_SUFFIX='-merge-slot'

def is_merge_slot(row):
    """True for a project's merge slot row: an internal record, never work.

    bd 1.2.2 creates the slot as an ordinary row of type ``task`` with the id
    ``<project>-merge-slot`` and the label ``gt:slot`` (measured on real bd,
    kittrial-5bb.113), so a check on the issue type alone never matched it and the
    slot was listed as open, unclaimed work.

    The rule is the exact id alone (review 01a10c0b). The label is what an accident or
    a host command removes, and a slot without it was listed and writable again; the
    id is what the kit provisions. A project name holds no hyphen, so the id has
    exactly the shape ``NAME-merge-slot``: a row whose id merely ends that way
    (``p-x-merge-slot``) is an ordinary task, and no contributor can make a row with
    the slot's shape (the endpoint refuses ``create --id`` for it). A row of type
    ``merge-slot`` (what earlier code expected) counts too.
    """
    if not isinstance(row,dict):return False
    if row.get('issue_type')=='merge-slot':return True
    from reserved_comments import is_merge_slot_id
    return is_merge_slot_id(row.get('id'))

def merge_slot_sentence(task):
    """What a write on the slot is told, on every path."""
    return '%s is the project\'s merge slot, an internal record, not a task'%task

def merge_slot_missing(state):
    """True when a native merge-slot check reports no slot for this project.

    bd 1.2.2 answers a missing slot with ``{"available": false, "error": "not
    found", "id": "<project>-merge-slot"}`` and exit code 0, so key absence is
    NOT a signal: ``available`` is present. A real state always carries
    ``holder`` and ``waiters`` (either may be null), including a held slot,
    which is ``available: false`` WITH a holder. A state is therefore missing
    when native reports an error, when it is an unavailable dict without those
    keys, or when it is not a recognizable check result at all (empty or
    unparseable), so callers can safely treat it as missing.
    """
    if not isinstance(state,dict):return True
    if state.get('error'):return True
    if 'available' not in state:return True
    return state.get('available') is False and 'holder' not in state and 'waiters' not in state


SLOT_DAMAGED=('The merge slot record is damaged: it is not available and names no holder. '
              'Run the merge-create operation, which repairs it, then try again.')

def merge_slot_damaged(state):
    """True when a native merge-slot check reports the damaged shape (kittrial-5bb.202).

    A real state carries ``holder`` and ``waiters`` (either may be null): a free slot is
    ``available: true`` with no holder, and a held slot is ``available: false`` WITH a
    holder. The damaged row is ``available: false`` and names no holder (``SLOT_DAMAGED``);
    a state that is absent, empty or unparseable is NOT damaged, it is missing
    (``merge_slot_missing``). The two never overlap, so callers can test missing first.
    """
    if not isinstance(state,dict) or state.get('error'):return False
    if 'available' not in state:return False
    return state.get('available') is False and 'holder' in state and not state.get('holder')

def repair_merge_slot(run,slot):
    """Put a damaged merge slot row back in order; returns what was changed (kittrial-5bb.113 review).

    bd keeps the holder in the row's metadata and the status beside it: in_progress
    while held, open when free, never an assignee, always the label. A write that went
    round `coordinate` (a claim after the label was removed, a close while held) leaves
    them disagreeing, and bd then refuses both acquire and release. The holder bd
    recorded decides: nothing here names a new holder or releases one.
    """
    rows=record_json.loads(run(['show',slot,'--json']))
    row=rows[0] if isinstance(rows,list) and rows else rows
    if not isinstance(row,dict) or row.get('id')!=slot:raise ValueError('Could not read the merge slot row %s to check it'%slot)
    metadata=row.get('metadata')
    if isinstance(metadata,str):
        try:metadata=record_json.loads(metadata)
        except ValueError:metadata=None
    holder=metadata.get('holder') if isinstance(metadata,dict) else None
    wanted='in_progress' if holder else 'open'
    changes=[];fixed=[]
    if MERGE_SLOT_LABEL not in (row.get('labels') or []):changes+=['--add-label',MERGE_SLOT_LABEL];fixed.append('label')
    if row.get('status')!=wanted:changes+=['--status',wanted];fixed.append('status')
    if row.get('assignee'):changes+=['--assignee',''];fixed.append('assignee')
    if changes:run(['update',slot,*changes,'--json'])
    return fixed,wanted

def apply_native(p, actor, run, project):
    """Caller holds canonical project lock. Journals belong to runtime, never Git."""
    if not isinstance(p,dict):raise ValueError('Expected object')
    op=p.get('operation')
    if op=='create-child':
        if set(p)!={'operation','request_id','parent','title','description','type'}:raise ValueError('Invalid child request fields')
        identifier(p['request_id']);identifier(p['parent'])
        # bd resolves a parent from any substring of an id, so the row is read, not guessed.
        parents=record_json.loads(run(['show',p['parent'],'--json']))
        for parent in parents if isinstance(parents,list) else [parents]:
            if is_merge_slot(parent):raise ValueError(merge_slot_sentence(parent.get('id'))+'; it takes no child')
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
        found=record_json.loads_array_rows(run(['list','--all','--limit','0','--label',label,'--json'])) or []
        if len(found)>1:raise ValueError('Duplicate native request records; operator reconciliation required')
        if found:
            if found[0].get('malformed'):
                raise ValueError('Cannot verify native request: child %s could not be parsed' % (found[0].get('id') or ''))
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
        except (TypeError,ValueError,RecursionError):
            issue=None
        if not isinstance(issue,dict) or not issue.get('id'):raise ValueError('Create response uncertain; reconcile same request ID')
        atomic(receipt,receipt_record(pending,digest,'complete',id=issue['id'],actor=actor))
        return {'id':issue['id'],'reconciled':False}
    if op not in ('merge-create','merge-check','merge-acquire','merge-release'):raise ValueError('Unknown coordination operation')
    expected={'operation','task','target'} if op=='merge-acquire' else {'operation'}
    if set(p)!=expected:
        # Name the fault (kittrial-5bb.97): which fields are unexpected or missing for THIS
        # operation. Caller-supplied names are bounded so an error cannot echo a large payload.
        def names(values):return ', '.join(str(value)[:40] for value in sorted(values, key=str)[:8])
        details=[]
        if set(p)-expected:details.append('unexpected field(s): '+names(set(p)-expected))
        if expected-set(p):details.append('missing field(s): '+names(expected-set(p)))
        raise ValueError('Invalid merge request fields for %s: %s. merge-acquire takes operation, task and target; '
                         'merge-create, merge-check and merge-release take only operation. The holder is always '
                         'the request actor.'%(op,'; '.join(details)))
    context_path=project/'.merge-context.json'
    if op=='merge-create':
        # Creates the slot, or says it exists; either way a damaged row is put right.
        result=json.loads(run(['merge-slot','create','--json']))
        if isinstance(result,dict) and isinstance(result.get('id'),str):
            result['repaired'],wanted=repair_merge_slot(run,result['id'])
            if result['repaired'] and 'status' in result:result['status']=wanted
        return result
    state=json.loads(run(['merge-slot','check','--json']))
    if merge_slot_missing(state):
        detail=''
        if isinstance(state,dict) and state.get('error'):detail=' (%s)' % state['error']
        raise ValueError('Merge slot does not exist for this project%s; run the merge-create operation to create it before checking, acquiring or releasing' % detail)
    if merge_slot_damaged(state):raise ValueError(SLOT_DAMAGED)
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
    task_raw=run(['show',p['task'],'--json'])
    task_rows=record_json.loads_array_rows(task_raw)
    if task_rows and task_rows[0].get('malformed'):
        raise ValueError('Task %s is malformed: %s' % (p['task'], task_rows[0].get('error') or 'cannot parse row'))
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
