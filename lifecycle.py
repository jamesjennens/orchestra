"""Evidence-scoped lifecycle facts carried by native Beads state events."""
import argparse
import json
import re
import sys
from pathlib import Path
from requirements import canonical_bytes, content_hash, load_json

DIMENSIONS = ('implemented','tested','reviewed','integrated','deployed','live-verified')
VALUES = ('unknown','pending','passed','failed','not-applicable')
SCOPE_KEYS = {'source_commit','integration_commit','release_id','environment'}
PREFIX = 'Kind: lifecycle-v1\n'


def validate_scope(scope):
    if not isinstance(scope,dict) or set(scope)!=SCOPE_KEYS or any(not isinstance(v,str) for v in scope.values()):
        raise ValueError('scope requires source_commit, integration_commit, release_id and environment strings')
    if not any(v.strip() for v in scope.values()):raise ValueError('scope must identify some work')


def validate_payload(p):
    fields={'schema_version','operation_id','task','dimension','value','scope','evidence','provenance','actor'}
    if not isinstance(p,dict) or set(p)!=fields or type(p['schema_version']) is not int or p['schema_version']!=1:raise ValueError('invalid lifecycle payload')
    for key in ('operation_id','task','actor'):
        if not isinstance(p[key],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,160}',p[key]):raise ValueError('invalid '+key)
    validate_scope(p['scope'])
    if not isinstance(p['evidence'],list) or any(not isinstance(x,str) or not x.strip() for x in p['evidence']):raise ValueError('evidence must be a list of pointers')
    if p['provenance'] not in ('performed','reported','imported'):raise ValueError('invalid provenance')
    if p['dimension']=='lifecycle-scope':
        if p['value']!=content_hash(p['scope']):raise ValueError('scope token mismatch')
    elif p['dimension'] in DIMENSIONS and p['value'] in VALUES:
        if p['value'] in ('passed','failed','not-applicable') and not p['evidence']:raise ValueError('asserted facts need evidence')
        if p['value']=='passed':
            keys=('release_id','environment') if p['dimension'] in ('deployed','live-verified') else (('integration_commit',) if p['dimension']=='integrated' else ('source_commit',))
            if any(not p['scope'][key].strip() for key in keys):raise ValueError('passed fact needs relevant commit/release identity')
    else:raise ValueError('invalid lifecycle dimension/value')


def native_event(row):
    if row.get('issue_type')!='event':return None
    match=re.fullmatch(r'(?:Set |Changed )([A-Za-z0-9-]+)(?: from [^\n]+)? to ([^\n]+)(?:\n\nReason: ([\s\S]*))?',row.get('description',''))
    parents=[d.get('depends_on_id') for d in row.get('dependencies',[]) if d.get('type')=='parent-child']
    if len(parents)!=1:return None
    if match:dimension,value,reason=match.groups()
    else:
        title=re.fullmatch(r'State change: ([A-Za-z0-9-]+) → (.*)',row.get('title',''))
        if not title:return None
        dimension,value=title.groups();reason=None
    payload=None
    if reason and reason.startswith(PREFIX):
        try:
            payload=json.loads(reason[len(PREFIX):]);validate_payload(payload)
            if canonical_bytes(payload).decode()!=reason[len(PREFIX):] or payload['task']!=parents[0] or payload['dimension']!=dimension or payload['value']!=value or payload['actor']!=row.get('created_by'):payload=None
        except (ValueError,TypeError):payload=None
    suffix=row['id'].removeprefix(parents[0]+'.')
    # Native set-state allocates monotonically increasing hierarchical child IDs.
    # Unknown ordering is conservative: the view refuses ambiguous latest events.
    order=int(suffix) if suffix.isdigit() else None
    return {'task':parents[0],'dimension':dimension,'value':value,'payload':payload,'id':row['id'],'order':order,'created_at':row.get('created_at','')}


def project_facts(rows):
    events={}
    for row in rows:
        event=native_event(row)
        if event and event['dimension'] in (*DIMENSIONS,'lifecycle-scope'):
            events.setdefault((event['task'],event['dimension']),[]).append(event)
    def latest(task,dim):
        found=events.get((task,dim),[])
        if not found or any(e['order'] is None for e in found):return None
        orders=[e['order'] for e in found]
        if len(set(orders))!=len(orders):return None
        return max(found,key=lambda e:e['order'])
    result=[]
    for row in rows:
        if row.get('issue_type')=='event':continue
        labels=row.get('labels') or []
        def agrees(event,dim):return event is not None and [v for v in labels if v.startswith(dim+':')]==[dim+':'+event['value']]
        scope_event=latest(row['id'],'lifecycle-scope')
        scope=scope_event['payload']['scope'] if agrees(scope_event,'lifecycle-scope') and scope_event['payload'] else None
        facts={}
        for dim in DIMENSIONS:
            e=latest(row['id'],dim)
            valid=scope is not None and agrees(e,dim) and e['payload'] is not None and e['payload']['scope']==scope
            facts[dim]={'value':e['value'] if valid else 'unknown',
                        'event_id':e['id'] if e else None,
                        'evidence':e['payload']['evidence'] if valid else [],
                        'provenance':e['payload']['provenance'] if valid else None}
        result.append({'id':row['id'],'title':row.get('title',''),'status':row.get('status'), 'scope':scope,'facts':facts,
                       'has_lifecycle':any((row['id'],dim) in events for dim in (*DIMENSIONS,'lifecycle-scope'))})
    return result


def integration_evidence(rows):
    """Per-scope ``integrated`` evidence, newest recorded scope first.

    ``project_facts`` reports only the newest scope and treats older evidence as
    unknown once a later scope is recorded. Integration is decided per scope: a
    contribution counts as integrated when ANY scope whose ``source_commit``
    equals its full commit records ``integrated=passed``, regardless of scope
    order. This reader exposes those values without changing ``project_facts``.

    Trust stays conservative. Ordering ambiguity, a newest unstructured (manual)
    assertion, or a newest value that disagrees with the native ``integrated:``
    label yields no trusted value, never a guess.
    """
    events={}
    for row in rows:
        event=native_event(row)
        if event:events.setdefault((event['task'],event['dimension']),[]).append(event)
    result=[]
    for row in rows:
        if row.get('issue_type')=='event':continue
        task=row['id'];found=events.get((task,'integrated'),[]);trusted=None
        if found:
            orders=[e['order'] for e in found]
            newest=max(found,key=lambda e:e['order'] if e['order'] is not None else -1)
            labels=[v for v in (row.get('labels') or []) if v.startswith('integrated:')]
            if (all(o is not None for o in orders) and len(set(orders))==len(orders)
                    and newest['payload'] is not None
                    and labels==['integrated:'+newest['value']]):
                trusted=found
        scopes=[];seen=set()
        for event in events.get((task,'lifecycle-scope'),[]):
            payload=event['payload']
            if not payload or event['value'] in seen:continue
            seen.add(event['value']);scopes.append((event,event['value'],payload['scope']))
        scopes.sort(key=lambda x:x[0]['order'] if x[0]['order'] is not None else -1,reverse=True)
        entries=[]
        for event,token,scope in scopes:
            fact=None
            if trusted:
                candidates=[x for x in trusted if x['payload'] and content_hash(x['payload']['scope'])==token]
                if candidates:
                    latest=max(candidates,key=lambda x:x['order'])
                    fact={'value':latest['value'],'event_id':latest['id'],
                          'evidence':latest['payload']['evidence'],'provenance':latest['payload']['provenance']}
            entries.append({'scope_token':token,'scope':scope,'order':event['order'],'integrated':fact})
        result.append({'id':task,'scopes':entries})
    return result


def apply_native(payload, actor, run):
    """Caller holds project lock; run(argv) invokes pinned bd and returns stdout."""
    validate_payload(payload)
    if payload['actor']!=actor:raise ValueError('payload actor must match request actor')
    rows=[json.loads(x) for x in run(['export','--all']).splitlines() if x.strip()]
    for row in rows:
        e=native_event(row)
        if e and e['payload'] and e['payload']['operation_id']==payload['operation_id']:
            if e['payload']!=payload:raise ValueError('operation ID already used for different content')
            return {'event_id':e['id'],'reconciled':True}
    issue=next((r for r in rows if r['id']==payload['task']),None)
    if issue is None or issue.get('issue_type')=='event':raise ValueError('unknown lifecycle task or event is not a task')
    if payload['dimension']!='lifecycle-scope':
        state=next(r for r in project_facts(rows) if r['id']==payload['task'])
        if state['scope']!=payload['scope']:raise ValueError('set matching lifecycle scope before recording facts')
    dim,value=payload['dimension'],payload['value']
    if dim+':'+value in (issue.get('labels') or []):
        intermediate='pending' if value!='pending' else 'unknown'
        run(['set-state',payload['task'],dim+'='+intermediate,'--reason','Lifecycle update in progress; retry the same operation if interrupted.','--json'])
    result=json.loads(run(['set-state',payload['task'],dim+'='+value,'--reason',PREFIX+canonical_bytes(payload).decode(),'--json']))
    if not result.get('event_id'):raise ValueError('native write returned no evidence event; inspect and reconcile')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    record=sub.add_parser('record')
    for name in ('config','project','actor','file'):record.add_argument('--'+name,required=True)
    view=sub.add_parser('list');view.add_argument('--export',required=True);view.add_argument('--implemented-not-deployed',action='store_true')
    a=parser.parse_args()
    try:
        if a.command=='record':
            from client import request
            data=load_json(a.file);validate_payload(data)
            answer=request(load_json(a.config),a.project,a.actor,[canonical_bytes(data).decode()],action='lifecycle')
            if answer['returncode']:raise ValueError(answer['stderr'])
            print(answer['stdout'],end='')
        else:
            from export_requirements import read_export
            facts=project_facts(read_export(a.export))
            if a.implemented_not_deployed:facts=[r for r in facts if r['facts']['implemented']['value']=='passed' and r['facts']['deployed']['value'] not in ('passed','not-applicable')]
            print(json.dumps(facts,ensure_ascii=False,indent=2))
    except (ValueError,OSError,RuntimeError) as exc:raise SystemExit(str(exc))


if __name__=='__main__':main()
