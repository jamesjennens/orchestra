"""Evidence-scoped lifecycle facts carried by native Beads state events."""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from requirements import canonical_bytes, content_hash, load_json

DIMENSIONS = ('implemented','tested','reviewed','integrated','deployed','live-verified')
VALUES = ('unknown','pending','passed','failed','not-applicable')
SCOPE_KEYS = {'source_commit','integration_commit','release_id','environment'}
PREFIX = 'Kind: lifecycle-v1\n'
# Operational effect state, deliberately outside the six lifecycle facts. A
# release can be deployed and switched on while a known defect stops it working;
# recording that here keeps `deployed=passed` from reading as fully done, and the
# defect value names the task that will fix it.
ENABLED = 'enabled'
ENABLED_VALUES = ('enabled','disabled','enabled-with-known-defect')
DEFECT_VALUE = 'enabled-with-known-defect'
OPTIONAL_FIELDS = ('note','trigger','defect_task')
PAYLOAD_FIELDS = {'schema_version','operation_id','task','dimension','value','scope','evidence','provenance','actor'}
TEXT_MAX = 500
# One release-level write covers many tasks: one operation for the caller, one
# payload on the wire, one native event per recorded fact. Bounded in the number
# of targets and in every text field.
RELEASE = 'release-deploy'
RELEASE_FIELDS = (PAYLOAD_FIELDS - {'task'}) | {'targets','live_verified'}
RELEASE_TARGETS_MAX = 200
RELEASE_OPERATION_MAX = 110
# The dimensions a release itself still has to earn. Other dimensions count as
# owed only when the current scope explicitly records pending or failed.
DEPLOYMENT_DIMENSIONS = ('deployed','live-verified')
COMMIT = re.compile(r'[0-9a-f]{40}')
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,160}')


def validate_scope(scope):
    if not isinstance(scope,dict) or set(scope)!=SCOPE_KEYS or any(not isinstance(v,str) for v in scope.values()):
        raise ValueError('scope requires source_commit, integration_commit, release_id and environment strings')
    if not any(v.strip() for v in scope.values()):raise ValueError('scope must identify some work')


def one_line(value,name,maxlen=TEXT_MAX):
    if not isinstance(value,str) or not value.strip():raise ValueError(name+' must be a nonempty string')
    if len(value)>maxlen or any(ord(c)<32 for c in value):
        raise ValueError(name+' must be up to %d characters on one line'%maxlen)
    return value


def validate_payload(p):
    if not isinstance(p,dict) or set(p)-set(OPTIONAL_FIELDS)!=PAYLOAD_FIELDS or type(p.get('schema_version')) is not int or p['schema_version']!=1:raise ValueError('invalid lifecycle payload')
    for key in ('operation_id','task','actor'):
        if not isinstance(p[key],str) or not LABEL.fullmatch(p[key]):raise ValueError('invalid '+key)
    validate_scope(p['scope'])
    if not isinstance(p['evidence'],list) or any(not isinstance(x,str) or not x.strip() for x in p['evidence']):raise ValueError('evidence must be a list of pointers')
    if p['provenance'] not in ('performed','reported','imported'):raise ValueError('invalid provenance')
    if 'note' in p:one_line(p['note'],'note')
    if 'trigger' in p:
        if p['value']!='pending':raise ValueError('a trigger belongs to a pending fact')
        one_line(p['trigger'],'trigger')
    if 'defect_task' in p:
        if p['dimension']!=ENABLED or p['value']!=DEFECT_VALUE:raise ValueError('defect_task belongs to an enabled-with-known-defect fact')
        if not isinstance(p['defect_task'],str) or not LABEL.fullmatch(p['defect_task']):raise ValueError('invalid defect_task')
    if p['dimension']==ENABLED:
        if p['value'] not in ENABLED_VALUES:raise ValueError('invalid lifecycle dimension/value')
        if not p['evidence']:raise ValueError('asserted facts need evidence')
        if p['value']==DEFECT_VALUE and 'defect_task' not in p:raise ValueError('enabled-with-known-defect needs the fixing task')
    elif p['dimension']=='lifecycle-scope':
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


def latest_event(events,task,dim):
    """Newest trusted event for one task/dimension, or None when ambiguous.

    Native set-state allocates monotonically increasing hierarchical child IDs, so
    a numeric suffix orders events. A missing or duplicated order is ambiguous and
    is never trusted, matching the conservative view the whole kit documents.
    """
    found=events.get((task,dim),[])
    if not found or any(e['order'] is None for e in found):return None
    orders=[e['order'] for e in found]
    if len(set(orders))!=len(orders):return None
    return max(found,key=lambda e:e['order'])


def label_agrees(labels,event,dim):
    """The native ``dim:`` label must name exactly the event value."""
    return event is not None and [v for v in labels if v.startswith(dim+':')]==[dim+':'+event['value']]


def events_by_dimension(rows):
    """Every exported lifecycle-shaped state event, grouped by (task, dimension)."""
    events={}
    for row in rows:
        event=native_event(row)
        if event and event['dimension'] in (*DIMENSIONS,'lifecycle-scope',ENABLED):
            events.setdefault((event['task'],event['dimension']),[]).append(event)
    return events


def trusted_payloads(rows,events=None):
    """The one trust decision for scoped facts, shared by every reader.

    Returns ``{task: {dimension: {'scope','value','event_id','payload'}}}``. A
    value is trusted only when the task's newest ``lifecycle-scope`` event agrees
    with its native label, the dimension's newest event agrees with its native
    label, event ordering is unambiguous, and the payload's scope is that trusted
    scope. ``project_facts``, ``enabled_states`` and ``evidence_owed`` all read
    this, so they cannot disagree about the same events.
    """
    if events is None:events=events_by_dimension(rows)
    trusted={}
    for row in rows:
        if row.get('issue_type')=='event':continue
        task=row['id'];labels=row.get('labels') or []
        scope_event=latest_event(events,task,'lifecycle-scope')
        scope=scope_event['payload']['scope'] if label_agrees(labels,scope_event,'lifecycle-scope') and scope_event['payload'] else None
        dimensions={}
        for dim in (*DIMENSIONS,ENABLED):
            e=latest_event(events,task,dim)
            valid=scope is not None and label_agrees(labels,e,dim) and e['payload'] is not None and e['payload']['scope']==scope
            dimensions[dim]={'scope':scope,'value':e['value'] if valid else 'unknown',
                             'event_id':e['id'] if e else None,'payload':e['payload'] if valid else None}
        trusted[task]=dimensions
    return trusted


def project_facts(rows):
    events=events_by_dimension(rows)
    trusted=trusted_payloads(rows,events)
    result=[]
    for row in rows:
        if row.get('issue_type')=='event':continue
        dimensions=trusted[row['id']]
        facts={}
        for dim in DIMENSIONS:
            state=dimensions[dim];payload=state['payload']
            facts[dim]={'value':state['value'],'event_id':state['event_id'],
                        'evidence':payload['evidence'] if payload else [],
                        'provenance':payload['provenance'] if payload else None}
        result.append({'id':row['id'],'title':row.get('title',''),'status':row.get('status'),
                       'scope':dimensions[DIMENSIONS[0]]['scope'],'facts':facts,
                       'has_lifecycle':any((row['id'],dim) in events for dim in (*DIMENSIONS,'lifecycle-scope'))})
    return result


def enabled_states(rows):
    """Newest trusted ``enabled`` state per task: enabled, disabled or a known defect.

    The same trust rule as the six facts applies, so an unattributed, unscoped,
    ambiguous or tampered event reads ``unknown`` instead of being guessed. The
    ``enabled`` dimension is deliberately outside ``DIMENSIONS``: it says whether
    a deployed flag actually works, which is not one of the six completion facts.
    """
    trusted=trusted_payloads(rows)
    result=[]
    for row in rows:
        if row.get('issue_type')=='event':continue
        state=trusted[row['id']][ENABLED];payload=state['payload']
        result.append({'id':row['id'],'value':state['value'],'event_id':state['event_id'],
                       'evidence':payload['evidence'] if payload else [],
                       'defect_task':payload.get('defect_task') if payload else None,
                       'actor':payload['actor'] if payload else None,'scope':state['scope']})
    return result


def evidence_owed(rows):
    """Deployed releases and the evidence they still owe, grouped by release.

    Groups tasks by the environment and release_id of their trusted current
    scope and reports what the coordinator still has to record:
    ``remaining_evidence``, the ``enabled`` flag and fixing task for a known
    defect, the responsible actor, and the free-text ``next_trigger`` recorded on
    a pending fact. ``remaining_evidence`` is the deployment dimensions
    (``deployed``, ``live-verified``) that are not passed or not-applicable, plus
    any dimension explicitly recorded pending or failed in the current scope: a
    dimension that reads unknown only because a later release scope rolled the
    task's scope over is not re-owed. Reading changes nothing; a deployed release
    with no trusted scope is never invented.
    """
    trusted=trusted_payloads(rows)
    groups={}
    for state in project_facts(rows):
        task=state['id'];scope=state['scope'];deployed=state['facts']['deployed']
        if scope is None or deployed['value'] in ('unknown','not-applicable'):continue
        remaining=[];trigger=None
        for dim in DIMENSIONS:
            fact=state['facts'][dim]
            if fact['value'] in ('passed','not-applicable'):continue
            if dim not in DEPLOYMENT_DIMENSIONS and fact['value'] not in ('pending','failed'):continue
            remaining.append(dim)
            payload=trusted[task][dim]['payload']
            if trigger is None and fact['value']=='pending' and payload:trigger=payload.get('trigger')
        enabled=trusted[task][ENABLED]
        issue=next(row for row in rows if row.get('id')==task)
        item={'task':task,'deployed':deployed['value'],'enabled':enabled['value'],
              'defect_task':enabled['payload'].get('defect_task') if enabled['payload'] else None,
              'remaining_evidence':remaining,
              'responsible':issue.get('assignee') or (trusted[task]['deployed']['payload'] or {}).get('actor'),
              'next_trigger':trigger}
        groups.setdefault((scope.get('environment',''),scope.get('release_id','')),[]).append(item)
    return [{'environment':environment,'release_id':release_id,
             'tasks':sorted(items,key=lambda item:item['task'])}
            for (environment,release_id),items in sorted(groups.items())]


def integration_evidence(rows):
    """Per-scope ``integrated`` evidence, newest recorded scope first.

    ``project_facts`` reports only the newest scope and treats older evidence as
    unknown once a later scope is recorded. Integration is decided per scope: a
    contribution counts as integrated when ANY scope whose ``source_commit``
    equals its full commit records ``integrated=passed``, regardless of scope
    order. This reader exposes those values without changing ``project_facts``.

    Trust is decided here once with the same rule ``project_facts`` applies: the
    newest ``lifecycle-scope`` event must agree with the native
    ``lifecycle-scope:`` label, the newest ``integrated`` event must agree with
    the native ``integrated:`` label, and event ordering must be unambiguous.
    Otherwise no scoped value is trusted. A newest unstructured (manual)
    assertion or a tampered lifecycle-scope label therefore yields no trusted
    value for any scope, never a guess, and `project_facts` and this reader can no
    longer disagree about the same events.

    Cost is linear in rows: each row is parsed once, each scoped integrated
    payload is hashed once, and the NEWEST recording of each scope token is kept
    in a single pass, so a recurring token keeps its newest position instead of an
    older one.
    """
    events={}
    for row in rows:
        event=native_event(row)
        if event:events.setdefault((event['task'],event['dimension']),[]).append(event)
    result=[]
    for row in rows:
        if row.get('issue_type')=='event':continue
        task=row['id'];labels=row.get('labels') or []
        scope_event=latest_event(events,task,'lifecycle-scope')
        scope_trusted=(scope_event is not None and scope_event['payload'] is not None
                       and label_agrees(labels,scope_event,'lifecycle-scope'))
        trusted=None
        found=events.get((task,'integrated'),[])
        orders=[e['order'] for e in found]
        if (found and scope_trusted and all(o is not None for o in orders)
                and len(set(orders))==len(orders)):
            newest=max(found,key=lambda e:e['order'])
            if newest['payload'] is not None and label_agrees(labels,newest,'integrated'):
                trusted=found
        # One pass over the trusted integrated events: hash each payload once.
        trusted_facts={}
        for event in (trusted or []):
            payload=event['payload']
            if not payload:continue
            key=(event['order'],event['id']);token=content_hash(payload['scope'])
            if token not in trusted_facts or key>trusted_facts[token][0]:
                trusted_facts[token]=(key,{'value':event['value'],'event_id':event['id'],
                                           'evidence':payload['evidence'],'provenance':payload['provenance']})
        # One pass over the recorded scopes: the newest recording of a token wins.
        scopes={}
        for event in events.get((task,'lifecycle-scope'),[]):
            payload=event['payload']
            if not payload:continue
            key=(event['order'] if event['order'] is not None else -1,event['id'])
            if event['value'] not in scopes or key>scopes[event['value']][0]:
                scopes[event['value']]=(key,event,payload['scope'])
        entries=[]
        for token,(key,event,scope_value) in sorted(scopes.items(),key=lambda kv:(kv[1][0],kv[0]),reverse=True):
            entries.append({'scope_token':token,'scope':scope_value,'order':event['order'],
                            'integrated':dict(trusted_facts[token][1]) if token in trusted_facts else None})
        result.append({'id':task,'scopes':entries})
    return result


def validate_release_payload(p,require_targets=True):
    """A release covers many tasks: one shared evidence block, one scope, targets."""
    if not isinstance(p,dict) or set(p)!=RELEASE_FIELDS:raise ValueError('invalid release payload')
    if type(p['schema_version']) is not int or p['schema_version']!=1:raise ValueError('invalid release payload')
    for key in ('operation_id','actor'):
        if not isinstance(p[key],str) or not LABEL.fullmatch(p[key]) or len(p[key])>RELEASE_OPERATION_MAX:raise ValueError('invalid '+key)
    if p['dimension']!=RELEASE or p['value']!='passed':raise ValueError('invalid lifecycle dimension/value')
    if type(p['live_verified']) is not bool:raise ValueError('live_verified must be true or false')
    validate_scope(p['scope'])
    if p['scope']['source_commit'].strip():raise ValueError('a release scope names no single source commit')
    if not COMMIT.fullmatch(p['scope']['integration_commit']):raise ValueError('a release needs its full release commit in integration_commit')
    if not p['scope']['release_id'].strip() or not p['scope']['environment'].strip():raise ValueError('a release needs a release_id and environment')
    if not isinstance(p['evidence'],list) or any(not isinstance(x,str) or not x.strip() for x in p['evidence']):raise ValueError('evidence must be a list of pointers')
    if not p['evidence']:raise ValueError('asserted facts need evidence')
    if p['provenance'] not in ('performed','reported','imported'):raise ValueError('invalid provenance')
    targets=p['targets']
    if not isinstance(targets,list):raise ValueError('invalid release targets')
    if require_targets and not targets:raise ValueError('a release needs at least one integrated target')
    if len(targets)>RELEASE_TARGETS_MAX:raise ValueError('a release covers at most %d targets'%RELEASE_TARGETS_MAX)
    seen=set()
    for target in targets:
        if not isinstance(target,dict) or set(target)-{'note'}!={'task','source_commit','integration_commit'}:raise ValueError('invalid release target')
        if not isinstance(target['task'],str) or not LABEL.fullmatch(target['task']):raise ValueError('invalid release target task')
        if target['task'] in seen:raise ValueError('duplicate release target')
        seen.add(target['task'])
        for key in ('source_commit','integration_commit'):
            if not isinstance(target[key],str) or not COMMIT.fullmatch(target[key]):raise ValueError('release target needs a full lowercase '+key)
        if 'note' in target:one_line(target['note'],'note')
    return p


def derived_id(operation_id,label,task):
    """A deterministic per-task operation ID, so an exact retry reconciles."""
    token=operation_id+'/'+label+'/'+task
    if len(token)<=161:return token
    return operation_id+'/'+label+'/'+hashlib.sha256(task.encode('utf-8')).hexdigest()[:24]


def release_targets(rows,scope,is_ancestor):
    """Integrated tasks whose integration commit is an ancestor of the release.

    ``is_ancestor(commit,release_commit)`` decides Git ancestry in the caller's
    checkout: it returns True or False and raises ValueError when ancestry cannot
    be decided. Each target keeps its own newest passing integration scope, so the
    release adds release identity without losing the task's contribution and merge
    identity. A task with a passing integration that is not a full commit, or whose
    ancestry is undecidable or not an ancestor, is reported as skipped, never
    guessed, and a task with no passing integration is not part of the release.
    """
    release=scope['integration_commit']
    targets=[];skipped=[]
    for entry in integration_evidence(rows):
        passing=[c for c in entry['scopes'] if (c.get('integrated') or {}).get('value')=='passed']
        if not passing:continue
        chosen=None;reason=None
        for candidate in passing:
            commit=candidate['scope']['integration_commit']
            if not COMMIT.fullmatch(commit):
                reason=reason or 'integration commit is not a full lowercase commit'
                continue
            try:ancestor=is_ancestor(commit,release)
            except ValueError as exc:
                reason=reason or str(exc);continue
            if ancestor:chosen=candidate;break
            reason=reason or 'no passing integration commit is an ancestor of the release'
        if chosen is None:
            skipped.append({'task':entry['id'],'reason':reason or 'no passing integration commit is an ancestor of the release'})
            continue
        targets.append({'task':entry['id'],'source_commit':chosen['scope']['source_commit'],
                        'integration_commit':chosen['scope']['integration_commit']})
    return sorted(targets,key=lambda t:t['task']),sorted(skipped,key=lambda s:s['task'])


def git_ancestry(repo):
    """An ``is_ancestor(commit,release)`` predicate over the caller's Git checkout.

    Only exit 0 (ancestor) and exit 1 (not an ancestor) are answers; any other
    outcome is undecidable and raises ValueError instead of guessing.
    """
    root=str(Path(repo))
    def is_ancestor(commit,release):
        try:
            done=subprocess.run(['git','-C',root,'merge-base','--is-ancestor',commit,release],
                                capture_output=True,text=True)
        except OSError as exc:raise ValueError('cannot run git: %s'%exc)
        if done.returncode==0:return True
        if done.returncode==1:return False
        raise ValueError('cannot decide Git ancestry: %s'%(done.stderr.strip() or 'git exit %d'%done.returncode))
    return is_ancestor


def apply_release(payload,actor,run):
    """One release-level write: record deployed (and optionally live-verified).

    Every target is verified against one export before the first native write, so
    a stale or unknown target refuses the whole release and nothing is written.
    Each target then gets the release scope recorded only when it differs from the
    task's current scope, followed by ``deployed=passed`` with the shared evidence
    block and the target's optional note, and ``live-verified=passed`` when the
    payload asks for it. Deterministic per-task operation IDs make an exact retry
    reconcile instead of duplicating; changed content needs a new operation ID.
    """
    validate_release_payload(payload)
    if payload['actor']!=actor:raise ValueError('payload actor must match request actor')
    rows=[json.loads(x) for x in run(['export','--all']).splitlines() if x.strip()]
    facts={state['id']:state for state in project_facts(rows)}
    integrated={entry['id']:[c for c in entry['scopes'] if (c.get('integrated') or {}).get('value')=='passed']
                for entry in integration_evidence(rows)}
    selected=[]
    for target in sorted(payload['targets'],key=lambda t:t['task']):
        task=target['task']
        issue=next((row for row in rows if row.get('id')==task),None)
        if issue is None or issue.get('issue_type')=='event':raise ValueError('unknown release target: '+task)
        match=next((c for c in integrated.get(task,[])
                    if c['scope']['integration_commit']==target['integration_commit']
                    and c['scope']['source_commit']==target['source_commit']),None)
        if match is None:raise ValueError('release target is not integrated at that commit: '+task)
        scope=dict(match['scope'])
        scope['release_id']=payload['scope']['release_id']
        scope['environment']=payload['scope']['environment']
        selected.append((target,scope))
    results=[]
    for target,scope in selected:
        task=target['task']
        common=dict(schema_version=1,task=task,scope=scope,evidence=list(payload['evidence']),
                    provenance=payload['provenance'],actor=payload['actor'])
        if 'note' in target:common['note']=target['note']
        recorded={'task':task,'scope_recorded':False,'deployed':None,'live_verified':None,'reconciled':False}
        if facts[task]['scope']!=scope:
            scope_result=apply_native(dict(common,operation_id=derived_id(payload['operation_id'],'scope',task),
                                           dimension='lifecycle-scope',value=content_hash(scope)),actor,run)
            recorded['scope_recorded']=True
            recorded['reconciled']=recorded['reconciled'] or bool(scope_result.get('reconciled'))
        deployed=apply_native(dict(common,operation_id=derived_id(payload['operation_id'],'deployed',task),
                                   dimension='deployed',value='passed'),actor,run)
        recorded['deployed']=deployed['event_id']
        recorded['reconciled']=recorded['reconciled'] or bool(deployed.get('reconciled'))
        if payload['live_verified']:
            verified=apply_native(dict(common,operation_id=derived_id(payload['operation_id'],'verified',task),
                                       dimension='live-verified',value='passed'),actor,run)
            recorded['live_verified']=verified['event_id']
            recorded['reconciled']=recorded['reconciled'] or bool(verified.get('reconciled'))
        results.append(recorded)
    return {'operation_id':payload['operation_id'],'release_id':payload['scope']['release_id'],
            'environment':payload['scope']['environment'],'targets':results}


def apply_native(payload, actor, run):
    """Caller holds project lock; run(argv) invokes pinned bd and returns stdout."""
    if isinstance(payload,dict) and payload.get('dimension')==RELEASE:return apply_release(payload,actor,run)
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
    release=sub.add_parser('release')
    for name in ('config','project','actor','file','export','repo'):release.add_argument('--'+name,required=True)
    release.add_argument('--live-verified',action='store_true')
    release.add_argument('--dry-run',action='store_true')
    view=sub.add_parser('list');view.add_argument('--export',required=True);view.add_argument('--implemented-not-deployed',action='store_true')
    owed=sub.add_parser('evidence-owed');owed.add_argument('--export',required=True)
    a=parser.parse_args()
    try:
        if a.command=='record':
            from client import request
            data=load_json(a.file);validate_payload(data)
            answer=request(load_json(a.config),a.project,a.actor,[canonical_bytes(data).decode()],action='lifecycle')
            if answer['returncode']:raise ValueError(answer['stderr'])
            print(answer['stdout'],end='')
        elif a.command=='release':
            from client import request
            from export_requirements import read_export
            data=load_json(a.file)
            if not isinstance(data,dict):raise ValueError('invalid release payload')
            if a.live_verified:data['live_verified']=True
            validate_release_payload(dict(data,targets=data.get('targets') or []),require_targets=False)
            targets,skipped=release_targets(read_export(a.export),data['scope'],git_ancestry(a.repo))
            data['targets']=targets
            report={'operation_id':data['operation_id'],'scope':data['scope'],'targets':targets,'skipped':skipped,'dry_run':bool(a.dry_run)}
            if a.dry_run:
                print(json.dumps(report,ensure_ascii=False,indent=2))
            else:
                validate_release_payload(data)
                answer=request(load_json(a.config),a.project,a.actor,[canonical_bytes(data).decode()],action='lifecycle')
                if answer['returncode']:raise ValueError(answer['stderr'])
                report['result']=json.loads(answer['stdout'])
                print(json.dumps(report,ensure_ascii=False,indent=2))
        elif a.command=='evidence-owed':
            from export_requirements import read_export
            print(json.dumps(evidence_owed(read_export(a.export)),ensure_ascii=False,indent=2))
        else:
            from export_requirements import read_export
            facts=project_facts(read_export(a.export))
            if a.implemented_not_deployed:facts=[r for r in facts if r['facts']['implemented']['value']=='passed' and r['facts']['deployed']['value'] not in ('passed','not-applicable')]
            print(json.dumps(facts,ensure_ascii=False,indent=2))
    except (ValueError,OSError,RuntimeError) as exc:raise SystemExit(str(exc))


if __name__=='__main__':main()
