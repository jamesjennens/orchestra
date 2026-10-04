"""Evidence-scoped lifecycle facts carried by native Beads state events."""
import argparse
import hashlib
import json
import record_json
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
# Liveness, deliberately outside the six facts AND separate from the evidence
# scope (kittrial-5bb.107 rev2 item 4). A `live` fact names the recorded release
# scope that is - or is no longer - the live release for the task's environment,
# so an explicit rollback can say what is live without rewriting where evidence
# for an older release was recorded. `live` says the named scope is live;
# `superseded` says it is not live any more.
LIVE = 'live'
LIVE_VALUES = ('live','superseded')
LIVE_SUPERSEDED = 'superseded'
OPTIONAL_FIELDS = ('note','trigger','defect_task')
PAYLOAD_FIELDS = {'schema_version','operation_id','task','dimension','value','scope','evidence','provenance','actor'}
TEXT_MAX = 500
# One release-level write covers many tasks: one operation for the caller, one
# payload on the wire, one native event per recorded fact. Bounded in the number
# of targets and in every text field.
RELEASE = 'release-deploy'
# A read-only release pre-flight: the client asks the endpoint which integrations
# are reverted by a host-issued operator record (item 5), so the default release
# command no longer needs a caller-side journal or ORCHESTRA_OPERATORS. It writes
# nothing.
RELEASE_QUERY = 'release-query'
RELEASE_FIELDS = (PAYLOAD_FIELDS - {'task'}) | {'targets','live_verified'}
# Additive, optional release fields. `rollback` marks an explicit rollback and
# `supersede` lists tasks whose live release is being moved back past them; both
# are absent from every payload written before this revision, so an older kit
# and an exact older payload still validate.
RELEASE_OPTIONAL_FIELDS = ('rollback','supersede')
RELEASE_TARGETS_MAX = 200
RELEASE_OPERATION_MAX = 110
# A release is sent as one request per group so the project lock is released
# between groups and no single request can approach the client's 150 s timeout.
# A smaller default group keeps one request well inside that timeout: a measured
# real project paid 1.6 to 1.7 s per target, so the old default of 50 could reach
# ~86 s and a group of 200 timed out at 150 s. 25 targets is ~45 s at that rate.
RELEASE_CHUNK_DEFAULT = 25
# Conservative per-target budget quoted by `release --dry-run`. The measured
# in-memory harness on the authoritative Linux host is far below it, but a real
# `bd` binary on a large project paid 1.6 to 1.7 s per target (201 targets took
# 335 s; groups of 50 took 79 to 86 s), so the estimate is a true upper bound only
# at 2.0 s per target with per-request overhead, and groups are sized so one
# request stays well inside the client's 150 s timeout.
DRY_RUN_SECONDS_PER_TARGET = 2.0
DRY_RUN_FIXED_SECONDS = 10.0
# The export-visible marker an operator revert leaves on the task (reserved_comments
# forbids raw contributors writing it; the host journal stays the review reader's
# trust anchor, review_workflow.REVERT_PREFIX is the same wire string).
REVERT_PREFIX = 'Kind: integration-revert-v1\n'
# The dimensions a release itself still has to earn. Other dimensions count as
# owed only when the current scope explicitly records pending or failed.
DEPLOYMENT_DIMENSIONS = ('deployed','live-verified')
COMMIT = re.compile(r'[0-9a-f]{40}')
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,160}')
SCOPE_ROLL_NOTE = ('The release scope becomes each target\'s current scope, so the six-fact readers '
                   '(brief, work, lifecycle.py list) show integrated=unknown (and any earlier-scope fact '
                   'unknown) until it is recorded for the release scope; per-scope integration evidence '
                   'and the review reads are unaffected.')
# A between-group failure leaves the earlier groups written, so the same file with
# the same export fails again at the same group; the retry needs fresh state.
FRESH_EXPORT_NOTE = ('the groups before this one were written; take a FRESH export and reconcile before '
                     'retrying, because the same file with the same export fails again at the same group')


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
    elif p['dimension']==LIVE:
        # A liveness fact names a recorded release scope; it carries no commit of
        # its own, so it needs the release identity (release_id + environment) and
        # evidence like any other asserted fact.
        if p['value'] not in LIVE_VALUES:raise ValueError('invalid lifecycle dimension/value')
        if not p['evidence']:raise ValueError('asserted facts need evidence')
        if not p['scope']['release_id'].strip() or not p['scope']['environment'].strip():
            raise ValueError('a live fact needs a release_id and environment')
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
            payload=record_json.loads(reason[len(PREFIX):]);validate_payload(payload)
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
        if event and event['dimension'] in (*DIMENSIONS,'lifecycle-scope',ENABLED,LIVE):
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
        for dim in (*DIMENSIONS,ENABLED,LIVE):
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
        live=dimensions[LIVE]['value']
        facts={}
        for dim in DIMENSIONS:
            state=dimensions[dim];payload=state['payload']
            value=state['value'];evidence=payload['evidence'] if payload else []
            # Liveness override (kittrial-5bb.107 rev2 item 2): a release the task
            # was rolled back out of is not live, so `deployed` reads unknown rather
            # than passed-at-an-old-release. Evidence is untouched in the history and
            # stays visible per scope through `scoped_evidence`/`evidence-owed`.
            if dim=='deployed' and live==LIVE_SUPERSEDED:
                value='unknown';evidence=[]
            facts[dim]={'value':value,'event_id':state['event_id'],
                        'evidence':evidence,'provenance':payload['provenance'] if payload else None}
        result.append({'id':row['id'],'title':row.get('title',''),'status':row.get('status'),
                       'scope':dimensions[DIMENSIONS[0]]['scope'],'facts':facts,'live':live,
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


def untrusted_reason(dim,labels,found,scope_trusted,orders):
    """Why a task's dimension history cannot be trusted, or None when it can.

    Mirrors exactly the rule ``trusted_payloads``/``integration_evidence`` apply,
    so a task that is silently dropped from a release selection can be reported
    with the concrete cause (raw native set-state, two native labels, ambiguous
    ordering, or an untrusted scope) instead of vanishing.
    """
    if not found:return None
    if not scope_trusted:
        return 'the task has no trusted lifecycle scope, so its %s events are not trusted'%dim
    if any(o is None for o in orders):
        return 'the %s event order is ambiguous (a native event id has no numeric suffix)'%dim
    if len(set(orders))!=len(orders):
        return 'the %s event order is ambiguous (duplicate native event id suffixes)'%dim
    if len([v for v in labels if v.startswith(dim+':')])>1:
        return 'the task has more than one native %s: label'%dim
    newest=max(found,key=lambda e:e['order'])
    if newest['payload'] is None:
        return 'the newest %s event has no structured lifecycle payload (raw native set-state)'%dim
    if not label_agrees(labels,newest,dim):
        return 'the native %s: label disagrees with the newest %s event'%(dim,dim)
    return None


def scoped_evidence(rows,dimensions=('integrated',)):
    """Per-scope trusted values for the named dimensions, newest scope first.

    ``project_facts`` reports only the newest scope and treats older evidence as
    unknown once a later scope is recorded. This reader keeps the whole per-scope
    history, so a release can see what an earlier release and environment still
    owe and can tell what a task has already been deployed for. Trust is decided
    once with the same rule ``project_facts`` applies: the newest
    ``lifecycle-scope`` event must agree with the native ``lifecycle-scope:``
    label, the newest event of each dimension must agree with its native label,
    payloads must carry the trusted scope and ordering must be unambiguous.
    Otherwise no scoped value is trusted and the cause is reported under
    ``untrusted``; a task is never guessed.

    Cost is linear in rows plus one pass per dimension: each row is parsed once,
    each scoped payload is hashed once, and the NEWEST recording of each scope
    token is kept, so a recurring token cannot resurrect an older position.
    """
    dims=tuple(dimensions)
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
        trusted={};untrusted={}
        for dim in dims:
            found=events.get((task,dim),[])
            orders=[e['order'] for e in found]
            reason=untrusted_reason(dim,labels,found,scope_trusted,orders)
            if reason is not None:untrusted[dim]=reason
            elif found:trusted[dim]=found
        # One pass per dimension over the trusted events: hash each payload once and
        # keep the newest event per (scope token, dimension). The token is memoised
        # by the scope's canonical bytes (a repeated scope is hashed once), which is
        # what makes the per-scope readers linear in DISTINCT scopes rather than in
        # events (kittrial-5bb.107 rev2 item 6.4).
        per_token={};token_cache={}
        for dim in dims:
            for event in trusted.get(dim,[]):
                payload=event['payload']
                if not payload:continue
                canon=canonical_bytes(payload['scope'])
                token=token_cache.get(canon)
                if token is None:
                    token=hashlib.sha256(canon).hexdigest()
                    token_cache[canon]=token
                key=(event['order'],event['id'])
                slot=per_token.setdefault(token,{})
                prior=slot.get(dim)
                if prior is None or key>prior[0]:
                    slot[dim]=(key,{'value':event['value'],'event_id':event['id'],
                                    'evidence':payload['evidence'],'provenance':payload['provenance'],
                                    'actor':payload['actor'],'trigger':payload.get('trigger'),
                                    'defect_task':payload.get('defect_task')})
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
            entry={'scope_token':token,'scope':scope_value,'order':event['order']}
            for dim in dims:
                got=per_token.get(token,{}).get(dim)
                entry[dim]=dict(got[1]) if got else None
            entries.append(entry)
        result.append({'id':task,'scopes':entries,'untrusted':untrusted})
    return result


def integration_evidence(rows):
    """Per-scope ``integrated`` evidence, newest recorded scope first.

    Thin wrapper over ``scoped_evidence`` so every existing reader keeps exactly
    its documented shape. Integration is decided per scope: a contribution counts
    as integrated when ANY scope whose ``source_commit`` equals its full commit
    records ``integrated=passed``, regardless of scope order.
    """
    return scoped_evidence(rows,('integrated',))


def evidence_owed(rows):
    """Deployed releases and the evidence they still owe, grouped by release.

    Derived from the whole trusted SCOPE HISTORY, not only the task's current
    scope: a task released to one environment and then to another still owes the
    live verification of the earlier release, and a later release never hides an
    earlier debt. Every recorded scope whose ``deployed`` is passed, pending or
    failed gets a row carrying ``remaining_evidence``, the ``enabled`` flag and
    fixing task for a known defect, the responsible actor, and the free-text
    ``next_trigger`` recorded on a pending fact. The enabled flag is the one
    recorded for THAT row's scope (kittrial-5bb.107 item 8), so a later enabled
    change never appears on an earlier release's row. ``remaining_evidence`` is the
    deployment dimensions (``deployed``, ``live-verified``) that are not passed or
    not-applicable, plus any dimension explicitly recorded pending or failed in
    that scope. Reading changes nothing; a deployed release with no trusted scope
    is never invented.
    """
    issues={row['id']:row for row in rows if row.get('issue_type')!='event'}
    groups={}
    for entry in scoped_evidence(rows,DIMENSIONS+(ENABLED,LIVE)):
        task=entry['id'];issue=issues.get(task,{})
        for facts in entry['scopes']:
            deployed_value=(facts.get('deployed') or {}).get('value','unknown')
            if deployed_value not in ('passed','pending','failed'):continue
            remaining=[];trigger=None
            for dim in DIMENSIONS:
                got=facts.get(dim)
                value=(got or {}).get('value','unknown')
                if value in ('passed','not-applicable'):continue
                if dim not in DEPLOYMENT_DIMENSIONS and value not in ('pending','failed'):continue
                remaining.append(dim)
                if trigger is None and value=='pending' and got and got.get('trigger'):
                    trigger=got['trigger']
            scope=facts['scope']
            # The enabled flag is per SCOPE, not retroactively the task's current one:
            # a historical release row shows the flag recorded for that row's scope
            # (kittrial-5bb.107 item 8), so a later enabled change never rewrites it.
            enabled=facts.get(ENABLED) or {'value':'unknown'}
            # The per-scope liveness fact (rev2 item 2): a row whose release the task
            # has been rolled back out of reads `superseded` here instead of passing
            # silently as deployed. A row written before this revision reads `unknown`.
            live=(facts.get(LIVE) or {}).get('value','unknown')
            item={'task':task,'deployed':deployed_value,'live':live,
                  'enabled':enabled.get('value','unknown'),
                  'defect_task':enabled.get('defect_task'),
                  'remaining_evidence':remaining,
                  'responsible':issue.get('assignee') or (facts.get('deployed') or {}).get('actor'),
                  'next_trigger':trigger}
            groups.setdefault((scope.get('environment',''),scope.get('release_id','')),[]).append(item)
    return [{'environment':environment,'release_id':release_id,
             'tasks':sorted(items,key=lambda item:(item['task'],item.get('deployed') or ''))}
            for (environment,release_id),items in sorted(groups.items())]


def validate_release_payload(p,require_targets=True):
    """A release covers many tasks: one shared evidence block, one scope, targets.

    ``rollback`` and ``supersede`` are additive optional fields: an older payload
    (or an older kit's validator) never carries them, and a rollback is the one
    release shape that may name no new target at all.
    """
    if not isinstance(p,dict) or set(p)-set(RELEASE_OPTIONAL_FIELDS)!=RELEASE_FIELDS:raise ValueError('invalid release payload')
    if type(p['schema_version']) is not int or p['schema_version']!=1:raise ValueError('invalid release payload')
    for key in ('operation_id','actor'):
        if not isinstance(p[key],str) or not LABEL.fullmatch(p[key]) or len(p[key])>RELEASE_OPERATION_MAX:raise ValueError('invalid '+key)
    if p['dimension']!=RELEASE or p['value']!='passed':raise ValueError('invalid lifecycle dimension/value')
    if type(p['live_verified']) is not bool:raise ValueError('live_verified must be true or false')
    if 'rollback' in p and type(p['rollback']) is not bool:raise ValueError('rollback must be true or false')
    validate_scope(p['scope'])
    if p['scope']['source_commit'].strip():raise ValueError('a release scope names no single source commit')
    if not COMMIT.fullmatch(p['scope']['integration_commit']):raise ValueError('a release needs its full release commit in integration_commit')
    if not p['scope']['release_id'].strip() or not p['scope']['environment'].strip():raise ValueError('a release needs a release_id and environment')
    if not isinstance(p['evidence'],list) or any(not isinstance(x,str) or not x.strip() for x in p['evidence']):raise ValueError('evidence must be a list of pointers')
    if not p['evidence']:raise ValueError('asserted facts need evidence')
    if p['provenance'] not in ('performed','reported','imported'):raise ValueError('invalid provenance')
    supersede=p.get('supersede')
    if supersede is not None:
        if not isinstance(supersede,list) or any(not isinstance(x,str) or not LABEL.fullmatch(x) for x in supersede):
            raise ValueError('invalid release supersede list')
        if len(set(supersede))!=len(supersede):raise ValueError('duplicate supersede task')
        if len(supersede)>RELEASE_TARGETS_MAX:raise ValueError('a release supersedes at most %d tasks'%RELEASE_TARGETS_MAX)
        if not p.get('rollback'):raise ValueError('supersede belongs to an explicit rollback')
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


def reverted_integrations(rows,operators=None,journal=None):
    """``(task, integration_commit_lower)`` named by a HOST-ISSUED operator revert.

    This is the SAME reader every ``review``/``brief``/``work`` answer uses
    (``review_state.reverts_by_task``), so all four agree: a revert comment is
    honoured only when the host journal carries its matching entry and its author
    is on the configured operator allowlist, and a host-journaled retraction
    removes it. A raw comment written through host ``bd`` by a non-operator, or a
    revert the operator has retracted, no longer excludes a task here.
    ``journal`` is the project directory holding ``.integration-reverts/``;
    without it no revert is trusted, exactly as in review.
    """
    try:
        from review_state import reverts_by_task
    except ImportError:
        return set()
    reverts,_invalid=reverts_by_task(rows,operators,journal)
    result=set()
    for task,records in reverts.items():
        for record in records:
            commit=str(record.get('integration_commit') or '').lower()
            if commit:result.add((task,commit))
    return result


def current_contribution_commits(rows):
    """Newest contribution commit per task, read from the native review chain.

    Best effort: a task with no review chain (or a chain this read cannot parse)
    simply has no entry, and the release result then carries no delivery flag.
    Nothing here changes what is recorded.
    """
    try:
        from review_workflow import records, projection
    except ImportError:return {}
    result={}
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('id'),str):continue
        if row.get('issue_type') in ('event','gate','merge-slot'):continue
        try:
            state=projection(records(row,None))
        except (ValueError,KeyError,TypeError):continue
        contribution=state.get('contribution')
        if isinstance(contribution,dict) and contribution.get('commit'):
            result[row['id']]=str(contribution['commit'])
    return result


def release_selection(rows,scope,is_ancestor,previous_commit=None,subset=None,reverted=None,current_commits=None,
                      live_verified=False,operators=None,journal=None):
    """What is NEW in this release, plus why everything else was left out.

    ``is_ancestor(commit,release_commit)`` decides Git ancestry in the caller's
    checkout and raises ValueError when ancestry cannot be decided. A task is a
    target only when its chosen (newest) passing integration commit is an ancestor
    of the release commit, is not named by a HOST-ISSUED operator revert record,
    and is not already live in this environment in a release that CONTAINS that
    integration commit and is itself CONTAINED in R. That containment rule is the
    incremental default (rev2 item 1): a plain deploy of R2 after R1 selects only
    the tasks new in R2, with no ``--previous-release-commit``. A task whose newest
    trusted ``live`` fact is ``superseded`` is NOT already deployed, so a
    roll-forward re-lives it.

    ``previous_commit`` (a previous release commit) additionally restricts the
    choice to integrations recorded in ``PREV..R``: the chosen commit must be a
    descendant of the previous release and an ancestor of this one. ``subset`` is
    an optional set of task IDs the caller asked for. ``reverted``/``current_commits``
    default to the values read from the export.

    ``live_verified`` asks for the live-verification write. A task already
    deployed=passed at this release and environment is then still selected when
    its live-verified fact is not yet passed, so the documented "verify later from
    a fresh export" step records instead of selecting nothing (item 1). Such a
    target is listed in ``verify_only`` and the endpoint records only the
    live-verified evidence, under that release's recorded scope, without moving the
    task's current scope (rev2 item 3).

    Returns ``{'targets','skipped','flags','verify_only'}``. ``targets`` are
    wire-shaped; every omission is in ``skipped`` with a cause, including a task
    whose integrated events cannot be trusted, a host-issued reverted integration,
    an already-deployed integration and an undecidable ancestry. ``flags`` names
    selected targets whose delivery is not the task's current contribution.
    """
    release=scope['integration_commit']
    environment=scope['environment']
    release_id=scope.get('release_id') or ''
    if reverted is None:reverted=reverted_integrations(rows,operators,journal)
    if current_commits is None:current_commits=current_contribution_commits(rows)
    wanted=set(subset) if subset else None
    targets=[];skipped=[];flags=[];verifying=[]
    for entry in scoped_evidence(rows,('integrated','deployed','live-verified',LIVE)):
        task=entry['id']
        if wanted is not None and task not in wanted:continue
        scopes=entry['scopes']
        passing=[c for c in scopes if (c.get('integrated') or {}).get('value')=='passed']
        if not passing:
            reason=entry['untrusted'].get('integrated')
            if reason:skipped.append({'task':task,'reason':reason})
            continue
        chosen=None;reason=None
        for candidate in passing:
            commit=candidate['scope']['integration_commit']
            if not COMMIT.fullmatch(commit):
                reason=reason or 'integration commit is not a full lowercase commit: '+str(commit)
                continue
            if (task,commit.lower()) in reverted:
                reason=reason or 'integration commit was reverted by an operator revert record: '+commit
                continue
            if previous_commit is not None:
                try:after_previous=is_ancestor(previous_commit,commit)
                except ValueError as exc:
                    reason=reason or str(exc);continue
                if not after_previous:
                    reason=reason or 'integration commit is not in the previous release..release range: '+commit
                    continue
            try:ancestor=is_ancestor(commit,release)
            except ValueError as exc:
                reason=reason or str(exc);continue
            if ancestor:chosen=candidate;break
            reason=reason or 'no passing integration commit is an ancestor of the release'
        if chosen is None:
            skipped.append({'task':task,'reason':reason or 'no passing integration commit is an ancestor of the release'})
            continue
        # Liveness as of now: the newest recorded scope that carries a `live` fact.
        # Scopes are newest-first, so the first match wins; no fact at all reads
        # `unknown` and is treated as live, exactly as a kit before this revision.
        live_value='unknown'
        for candidate in scopes:
            value=(candidate.get(LIVE) or {}).get('value')
            if value:live_value=value;break
        # "Already deployed": a passed deployed scope for this environment whose
        # release contains the chosen integration and is itself contained in this
        # release. That is the incremental default (rev2 item 1). A superseded task
        # is deliberately not "already deployed", so a roll-forward re-lives it.
        already=None
        for candidate in scopes:
            if (candidate.get('deployed') or {}).get('value')!='passed':continue
            if (candidate['scope'].get('environment') or '')!=environment:continue
            deployed_commit=candidate['scope']['integration_commit']
            if not COMMIT.fullmatch(deployed_commit):continue
            if (task,deployed_commit.lower()) in reverted:continue
            try:
                contains=is_ancestor(chosen['scope']['integration_commit'],deployed_commit)
                within=is_ancestor(deployed_commit,release)
            except ValueError:
                continue
            if contains and within:
                already=candidate;break
        if already is not None and live_value!=LIVE_SUPERSEDED:
            # Prefer the deployment of THIS release id when one exists, so a
            # verification owed for R1 is not read as already done because a LATER
            # release of the same integration was verified (rev2 item 3).
            target_scope=dict(chosen['scope'])
            target_scope['release_id']=release_id
            target_scope['environment']=environment
            exact=next((c for c in scopes if (c.get('deployed') or {}).get('value')=='passed'
                        and c['scope']==target_scope),None)
            if exact is not None:already=exact
            verified=(already.get('live-verified') or {}).get('value')
            if not (live_verified and verified!='passed'):
                skipped.append({'task':task,'reason':'already deployed for %s at release %s (integration commit %s); not reselected'
                                %(environment,already['scope'].get('release_id') or 'unknown',
                                  already['scope']['integration_commit'])})
                continue
            # Already deployed at this release: select only for the live-verified write.
            verifying.append(task)
        target={'task':task,'source_commit':chosen['scope']['source_commit'],
                'integration_commit':chosen['scope']['integration_commit']}
        targets.append(target)
        current=current_commits.get(task)
        if current and current.lower()!=target['source_commit'].lower():
            flags.append({'task':task,'flag':'delivery-not-current',
                          'reason':('the deployed fact would be for delivery %s but the task\'s current contribution is %s'
                                    %(target['source_commit'],current)),
                          'delivery':{'release_id':scope['release_id'],'environment':environment,
                                      'source_commit':target['source_commit'],
                                      'integration_commit':target['integration_commit']},
                          'current_contribution':current})
    targets.sort(key=lambda t:t['task']);skipped.sort(key=lambda s:s['task']);flags.sort(key=lambda f:f['task'])
    return {'targets':targets,'skipped':skipped,'flags':flags,'verify_only':sorted(verifying)}


def release_targets(rows,scope,is_ancestor,previous_commit=None):
    """Backward-compatible ``(targets, skipped)`` view of ``release_selection``."""
    selection=release_selection(rows,scope,is_ancestor,previous_commit=previous_commit)
    return selection['targets'],selection['skipped']


def rollback_selection(rows,scope,is_ancestor,reverted=None,current_commits=None,
                       operators=None,journal=None):
    """The explicit rollback plan: who R carries, and who it supersedes.

    A rollback to release R (rev2 item 2) is not a normal deploy: it must also say
    that tasks shipped only after R are no longer live. ``targets`` are the tasks
    in R's membership - their chosen trusted integrated commit is an ancestor of R
    and is not host-reverted - which the endpoint re-records live at R.
    ``supersede`` names tasks deployed in the environment that are NOT in R's
    membership, so they read not live instead of silently passing. Every other
    omission is in ``skipped`` with a cause.
    """
    release=scope['integration_commit'];environment=scope['environment']
    if reverted is None:reverted=reverted_integrations(rows,operators,journal)
    if current_commits is None:current_commits=current_contribution_commits(rows)
    targets=[];supersede=[];skipped=[]
    for entry in scoped_evidence(rows,('integrated','deployed','live-verified',LIVE)):
        task=entry['id'];scopes=entry['scopes']
        chosen=None;reason=None
        for candidate in scopes:
            if (candidate.get('integrated') or {}).get('value')!='passed':continue
            commit=candidate['scope']['integration_commit']
            if not COMMIT.fullmatch(commit):
                reason=reason or 'integration commit is not a full lowercase commit: '+str(commit)
                continue
            if (task,commit.lower()) in reverted:
                reason=reason or 'integration commit was reverted by an operator revert record: '+commit
                continue
            try:ancestor=is_ancestor(commit,release)
            except ValueError as exc:
                reason=reason or str(exc);continue
            if ancestor:chosen=candidate;break
            reason=reason or 'no passing integration commit is an ancestor of the rollback release'
        live_value='unknown';live_scope=None
        for candidate in scopes:
            value=(candidate.get(LIVE) or {}).get('value')
            if value:live_value=value;live_scope=candidate;break
        if chosen is not None:
            targets.append({'task':task,'source_commit':chosen['scope']['source_commit'],
                            'integration_commit':chosen['scope']['integration_commit']})
            continue
        if live_value==LIVE_SUPERSEDED:
            continue
        # Not part of R: supersede it only when it is actually deployed live in this
        # environment at a release R does not contain (rev2 item 2). A task with no
        # deployment here is left alone.
        deployed=None
        for candidate in scopes:
            if (candidate.get('deployed') or {}).get('value')!='passed':continue
            if (candidate['scope'].get('environment') or '')!=environment:continue
            deployed=candidate;break
        if deployed is None:
            if reason:skipped.append({'task':task,'reason':reason})
            continue
        dcommit=deployed['scope']['integration_commit']
        try:within=COMMIT.fullmatch(dcommit) and is_ancestor(dcommit,release)
        except ValueError:within=False
        if within:
            skipped.append({'task':task,'reason':'already live in %s at a release contained in the rollback target'%environment})
            continue
        supersede.append(task)
    targets.sort(key=lambda t:t['task']);supersede=sorted(set(supersede));skipped.sort(key=lambda s:s['task'])
    return {'targets':targets,'supersede':supersede,'skipped':skipped}


def git_ancestry(repo):
    """An ``is_ancestor(commit,release)`` predicate over the caller's Git checkout.

    Only exit 0 (ancestor) and exit 1 (not an ancestor) are answers; any other
    outcome is undecidable and raises ValueError instead of guessing. A commit the
    checkout does not contain (a shallow clone, or the wrong repository) is
    reported as the missing commit rather than as a misleading ancestry answer.
    """
    root=str(Path(repo))
    def repo_ok():
        try:return subprocess.run(['git','-C',root,'rev-parse','--git-dir'],
                                  capture_output=True,text=True).returncode==0
        except OSError:return False
    def is_ancestor(commit,release):
        try:
            done=subprocess.run(['git','-C',root,'merge-base','--is-ancestor',commit,release],
                                capture_output=True,text=True)
        except OSError as exc:raise ValueError('cannot run git: %s'%exc)
        if done.returncode==0:return True
        if done.returncode==1:return False
        stderr=(done.stderr or '').strip()
        if repo_ok():
            try:
                known=subprocess.run(['git','-C',root,'cat-file','-e',commit+'^{commit}'],
                                     capture_output=True,text=True)
            except OSError as exc:raise ValueError('cannot run git: %s'%exc)
            if known.returncode!=0:
                raise ValueError('commit is not in this checkout (shallow clone or wrong repository): '+commit)
        raise ValueError('cannot decide Git ancestry: %s'%(stderr or 'git exit %d'%done.returncode))
    return is_ancestor


def _native_event_row(event_id,task,dimension,value,reason,actor):
    """The export shape a native set-state event has, so a batch keeps one view.

    The in-memory view exists only to avoid a second full export inside one
    request; the authoritative rows always come from the next real export.
    """
    return {'_type':'issue','id':event_id,'issue_type':'event',
            'title':'State change: '+dimension+' → '+value,
            'description':'Set '+dimension+' to '+value+'\n\nReason: '+reason,
            'status':'closed','created_by':actor,'created_at':'',
            'dependencies':[{'issue_id':event_id,'depends_on_id':task,'type':'parent-child'}]}


def _operation_index(rows):
    """``{operation_id: (payload, event_id)}`` from one pass over the export."""
    index={}
    for row in rows:
        event=native_event(row)
        if event and event['payload'] and event['payload']['operation_id'] not in index:
            index[event['payload']['operation_id']]=(event['payload'],event['id'])
    return index


def _apply_fact(payload,actor,rows,run,current_scope,op_index,issues,recorded=None):
    """One native fact write against an already-read export (``rows``).

    Identical to the single-fact path: an exact operation-ID retry reconciles, a
    reused ID with different content is refused, the task must exist, a non-scope
    fact needs the matching current scope, and the task's native label is moved
    through an intermediate value when it already shows the target. The passed
    ``rows``/``op_index``/``issues`` view is updated after a committed write so a
    release batch never pays for a second full export.

    ``recorded`` is the set of scope tokens already recorded for the task. A fact
    whose scope is one of them is accepted even when it is not the task's CURRENT
    scope, so evidence for an older release can be recorded without moving what is
    live (rev2 item 3). A scope that was never recorded is still refused.
    """
    prior=op_index.get(payload['operation_id'])
    if prior is not None:
        if prior[0]!=payload:raise ValueError('operation ID already used for different content')
        return {'event_id':prior[1],'reconciled':True}
    issue=issues.get(payload['task'])
    if issue is None or issue.get('issue_type')=='event':raise ValueError('unknown lifecycle task or event is not a task')
    if payload['dimension']==ENABLED and payload.get('defect_task') is not None:
        fix=issues.get(payload['defect_task'])
        if fix is None or fix.get('issue_type')=='event':
            raise ValueError('unknown defect_task: '+payload['defect_task'])
    if payload['dimension']!='lifecycle-scope' and current_scope!=payload['scope']:
        if recorded is None or content_hash(payload['scope']) not in recorded:
            raise ValueError('set matching lifecycle scope before recording facts')
    dim,value=payload['dimension'],payload['value']
    if dim+':'+value in (issue.get('labels') or []):
        intermediate='pending' if value!='pending' else 'unknown'
        reason='Lifecycle update in progress; retry the same operation if interrupted.'
        run(['set-state',payload['task'],dim+'='+intermediate,'--reason',reason,'--json'])
        issue['labels']=[x for x in (issue.get('labels') or []) if not x.startswith(dim+':')]+[dim+':'+intermediate]
    reason=PREFIX+canonical_bytes(payload).decode()
    result=json.loads(run(['set-state',payload['task'],dim+'='+value,'--reason',reason,'--json']))
    event_id=result.get('event_id')
    if not event_id:raise ValueError('native write returned no evidence event; inspect and reconcile')
    issue['labels']=[x for x in (issue.get('labels') or []) if not x.startswith(dim+':')]+[dim+':'+value]
    rows.append(_native_event_row(event_id,payload['task'],dim,value,reason,actor))
    op_index[payload['operation_id']]=(payload,event_id)
    return result


def _release_plan(rows,payload,operators=None,journal=None):
    """Read-only: resolve every target and the exact per-task write plan.

    Shared by the endpoint write (``apply_release``) and the client's whole-release
    pre-check, so both decide targets, already-deployed suppression and derived
    operation IDs the same way, against the same export and with no native write.
    Raises on an unknown, stale or host-reverted target before returning, which is
    what lets the client check the WHOLE release before the first group request.

    Returns ``(selected,plans,issues)``. ``selected`` entries are dicts carrying
    ``target``, ``release_scope``, ``already``, ``verified`` and ``verify_only``;
    ``plans[task]`` is an ordered list of ``(fact_scope,planned_payload)`` where
    ``fact_scope`` is the scope the endpoint must have current (or, for a
    verify-only target, the task's current scope against which the release scope
    must already be recorded) before that fact; ``None`` for the scope write.

    A target already deployed at this release/environment whose live-verification
    is not yet passed is VERIFY-ONLY: its plan is the single ``live-verified`` fact
    under that release's recorded scope, so the task's current scope never moves
    (rev2 item 3). Every other target gets the scope (only when it differs), the
    ``deployed=passed`` fact (only when the current scope is not already deployed)
    and the ``live=live`` liveness fact (only when the current scope is not already
    live), so a plain deploy and a roll-forward both end live.
    """
    issues={row['id']:row for row in rows if row.get('issue_type')!='event'}
    facts={state['id']:state for state in project_facts(rows)}
    evidence={entry['id']:entry for entry in
              scoped_evidence(rows,('integrated','deployed','live-verified',LIVE))}
    reverted=reverted_integrations(rows,operators,journal)
    selected=[];plans={}
    for target in sorted(payload['targets'],key=lambda t:t['task']):
        task=target['task']
        if issues.get(task) is None:raise ValueError('unknown release target: '+task)
        scopes=evidence.get(task,{}).get('scopes',[])
        match=next((c for c in scopes
                    if (c.get('integrated') or {}).get('value')=='passed'
                    and c['scope']['integration_commit']==target['integration_commit']
                    and c['scope']['source_commit']==target['source_commit']),None)
        if match is None:raise ValueError('release target is not integrated at that commit: '+task)
        if (task,target['integration_commit'].lower()) in reverted:
            raise ValueError('release target integration commit was reverted by an operator revert record: '+task)
        release_scope=dict(match['scope'])
        release_scope['release_id']=payload['scope']['release_id']
        release_scope['environment']=payload['scope']['environment']
        def same_release(candidate):
            return ((candidate['scope'].get('environment') or '')==release_scope['environment']
                    and (candidate['scope'].get('release_id') or '')==release_scope['release_id']
                    and candidate['scope']['integration_commit']==target['integration_commit']
                    and candidate['scope']['source_commit']==target['source_commit'])
        current_scope=facts[task]['scope']
        at_current=current_scope==release_scope
        already=next((c for c in scopes if (c.get('deployed') or {}).get('value')=='passed'
                      and same_release(c)),None)
        verified=next((c for c in scopes if (c.get('live-verified') or {}).get('value')=='passed'
                       and same_release(c)),None)
        live_present=next((c for c in scopes if (c.get(LIVE) or {}).get('value')=='live'
                           and same_release(c)),None)
        common=dict(schema_version=1,task=task,scope=release_scope,evidence=list(payload['evidence']),
                    provenance=payload['provenance'],actor=payload['actor'])
        # Already deployed here and only the verification is owed: record that
        # evidence against the release's recorded scope and touch nothing else.
        verify_only=already is not None and payload['live_verified'] and verified is None
        plan=[]
        if verify_only:
            plan.append((current_scope,dict(common,operation_id=derived_id(payload['operation_id'],'verified',task),
                                            dimension='live-verified',value='passed')))
        else:
            if not at_current:
                plan.append((None,dict(common,operation_id=derived_id(payload['operation_id'],'scope',task),
                                       dimension='lifecycle-scope',value=content_hash(release_scope))))
            if not (at_current and already is not None):
                deployed=dict(common,operation_id=derived_id(payload['operation_id'],'deployed',task),
                              dimension='deployed',value='passed')
                if 'note' in target:deployed['note']=target['note']
                plan.append((release_scope,deployed))
            if not (at_current and live_present is not None):
                plan.append((release_scope,dict(common,operation_id=derived_id(payload['operation_id'],'live',task),
                                                dimension=LIVE,value='live')))
            if payload['live_verified'] and verified is None:
                plan.append((release_scope,dict(common,operation_id=derived_id(payload['operation_id'],'verified',task),
                                                dimension='live-verified',value='passed')))
        selected.append({'target':target,'release_scope':release_scope,'already':already,
                         'verified':verified,'verify_only':verify_only})
        plans[task]=plan
    return selected,plans,issues


def _supersede_plan(rows,payload,issues=None):
    """Read-only: the ``live=superseded`` write for every task a rollback drops.

    ``payload['supersede']`` names tasks the client resolved as no longer in the
    live release. Each must exist, must not be a target of the same rollback, and
    must currently be live (or legacy-deployed) in the payload's environment. A
    task already recorded ``superseded`` is a no-op, so an exact retry reconciles.
    Returns a list of ``(task,planned_payload)``; evidence events are untouched.
    """
    if issues is None:
        issues={row['id']:row for row in rows if row.get('issue_type')!='event'}
    facts={state['id']:state for state in project_facts(rows)}
    environment=payload['scope']['environment']
    planned=[]
    for task in sorted(payload.get('supersede') or []):
        issue=issues.get(task)
        if issue is None or issue.get('issue_type')=='event':
            raise ValueError('unknown release supersede task: '+task)
        state=facts[task]
        scope=state['scope']
        if state.get('live')==LIVE_SUPERSEDED:
            continue
        if scope is None or (scope.get('environment') or '')!=environment:
            raise ValueError('release supersede task is not deployed in %s: %s'%(environment,task))
        planned.append((task,dict(schema_version=1,task=task,scope=scope,
                                  evidence=list(payload['evidence']),provenance=payload['provenance'],
                                  actor=payload['actor'],
                                  operation_id=derived_id(payload['operation_id'],'superseded',task),
                                  dimension=LIVE,value=LIVE_SUPERSEDED)))
    return planned


def apply_release(payload,actor,run,operators=None,journal=None):
    """One release-level write: record deployed (and optionally live-verified).

    One export serves the whole request. Every target is verified against it
    before the first native write, and every derived operation ID is checked for a
    conflicting planted fact before the first write too, so a stale target or a
    used ID refuses the whole release and nothing is written. Each target then gets
    the release scope recorded only when it differs from the task's current scope,
    followed by ``deployed=passed`` carrying the shared evidence block and the
    target's optional note (ONLY on the deployed fact: the scope event keeps
    exactly the four scope fields, so an older strict reader still reads it) and
    ``live-verified=passed`` when the payload asks for it. A target already
    recorded deployed=passed at this release and environment is a verify-only
    target (rev2 item 3): only the live-verified evidence is recorded, under the
    release's already recorded scope, so the task's current scope never moves. A
    target whose current scope is already deployed and live needs neither a scope
    nor a deployed nor a live write. Deterministic per-task operation IDs make an
    exact retry reconcile; changed content needs a new ID.

    Reverted integrations are read with the SAME host-journal and operator rule as
    every ``review`` read (``operators``/``journal``), so an unjournaled comment or
    a retracted revert no longer refuses a target by itself.

    ``live=live`` is recorded for every non-verify-only target (rev2 item 4), so
    readers can tell a live release from one the environment has been rolled back
    out of; when ``rollback`` is set, every task named in ``supersede`` gets
    ``live=superseded`` under its current scope with its evidence untouched.

    Ancestry is NOT decided here. The endpoint re-verifies that each target is
    integrated at the commits named in its own export; whether that commit is an
    ancestor of the release commit is decided by git in the caller's checkout (see
    ``release_selection``), exactly like ``commit``/``base_commit`` in contribution
    review.
    """
    validate_release_payload(payload,require_targets=not payload.get('rollback'))
    if payload['actor']!=actor:raise ValueError('payload actor must match request actor')
    rows=[json.loads(x) for x in run(['export','--all']).splitlines() if x.strip()]
    selected,plans,issues=_release_plan(rows,payload,operators,journal)
    superseded=_supersede_plan(rows,payload,issues)
    current=current_contribution_commits(rows)
    op_index=_operation_index(rows)
    for plan in plans.values():
        for _scope,planned in plan:
            prior=op_index.get(planned['operation_id'])
            if prior is not None and prior[0]!=planned:
                raise ValueError('operation ID already used for different content: '+planned['operation_id'])
    for _task,planned in superseded:
        prior=op_index.get(planned['operation_id'])
        if prior is not None and prior[0]!=planned:
            raise ValueError('operation ID already used for different content: '+planned['operation_id'])
    # Scope tokens already recorded for a task, so a verify-only fact under the
    # release's older scope is accepted without moving the current scope (item 3).
    recorded={entry['id']:{content_hash(c['scope']) for c in entry['scopes']}
              for entry in scoped_evidence(rows,('integrated','deployed','live-verified',LIVE))}
    results=[]
    for entry in selected:
        target=entry['target'];release_scope=entry['release_scope']
        task=target['task'];plan=plans[task]
        recorded_row={'task':task,'scope_recorded':False,'deployed':None,'live':None,'live_verified':None,
                      'reconciled':not plan,'already_deployed':entry['already'] is not None,
                      'verify_only':entry['verify_only'],
                      'delivery':{'release_id':release_scope['release_id'],
                                  'environment':release_scope['environment'],
                                  'source_commit':release_scope['source_commit'],
                                  'integration_commit':release_scope['integration_commit']}}
        for fact_scope,planned in plan:
            result=_apply_fact(planned,actor,rows,run,fact_scope,op_index,issues,
                               recorded=recorded.get(task))
            if planned['dimension']=='lifecycle-scope':
                recorded_row['scope_recorded']=True
            elif planned['dimension']=='deployed':
                recorded_row['deployed']=result['event_id']
            elif planned['dimension']==LIVE:
                recorded_row['live']=result['event_id']
            elif planned['dimension']=='live-verified':
                recorded_row['live_verified']=result['event_id']
            recorded_row['reconciled']=recorded_row['reconciled'] or bool(result.get('reconciled'))
        current_commit=current.get(task)
        if current_commit and current_commit.lower()!=release_scope['source_commit'].lower():
            recorded_row['flag']='delivery-not-current'
            recorded_row['current_contribution']=current_commit
            recorded_row['flag_reason']=('the deployed fact is for delivery %s but the task\'s current contribution is %s'
                                         %(release_scope['source_commit'],current_commit))
        results.append(recorded_row)
    supersede_results=[]
    for task,planned in superseded:
        result=_apply_fact(planned,actor,rows,run,planned['scope'],op_index,issues,
                           recorded=recorded.get(task))
        supersede_results.append({'task':task,'live':result.get('event_id'),
                                  'scope':dict(planned['scope']),
                                  'reconciled':bool(result.get('reconciled'))})
    return {'operation_id':payload['operation_id'],'release_id':payload['scope']['release_id'],
            'environment':payload['scope']['environment'],'targets':results,
            'rollback':bool(payload.get('rollback')),
            'superseded':supersede_results,
            'reader_note':SCOPE_ROLL_NOTE,
            'ancestry_note':('Ancestry/membership in the release is checked by git in the caller\'s checkout '
                             '(lifecycle.py release --repo); this endpoint re-verifies only that each target is '
                             'integrated at the commits named, in its own export.')}


def validate_query_payload(p):
    """A read-only release pre-flight: the release shape, no writes.

    The client sends it so the endpoint, which always has the host journal and the
    operator allowlist, answers which integrations are reverted (item 5). Exactly
    the release field set, so it can never carry an unknown field into a write path.
    """
    if not isinstance(p,dict) or set(p)!=RELEASE_FIELDS:raise ValueError('invalid release query payload')
    if type(p['schema_version']) is not int or p['schema_version']!=1:raise ValueError('invalid release query payload')
    for key in ('operation_id','actor'):
        if not isinstance(p[key],str) or not LABEL.fullmatch(p[key]) or len(p[key])>RELEASE_OPERATION_MAX:raise ValueError('invalid '+key)
    if p['dimension']!=RELEASE_QUERY:raise ValueError('invalid lifecycle dimension')
    validate_scope(p['scope'])
    if not p['scope']['environment'].strip():raise ValueError('a release query needs an environment')
    return p


def release_query(payload,actor,run,operators=None,journal=None):
    """Read-only endpoint answer for the release pre-flight (rev2 item 5).

    Returns the host-issued reverted ``(task, integration_commit)`` set - the same
    reader ``review`` uses - and the recorded live releases for the environment.
    Writes nothing and touches no journal.
    """
    validate_query_payload(payload)
    if payload['actor']!=actor:raise ValueError('payload actor must match request actor')
    rows=[json.loads(x) for x in run(['export','--all']).splitlines() if x.strip()]
    environment=payload['scope']['environment']
    reverted=sorted(reverted_integrations(rows,operators,journal))
    live=[]
    seen=set()
    for entry in scoped_evidence(rows,(LIVE,)):
        task=entry['id']
        for candidate in entry['scopes']:
            fact=candidate.get(LIVE)
            if not fact or fact.get('value')!='live':continue
            scope=candidate['scope']
            if (scope.get('environment') or '')!=environment:break
            key=(scope.get('release_id') or '',scope.get('integration_commit') or '')
            if key not in seen:
                seen.add(key)
                live.append({'task':task,'release_id':key[0],'integration_commit':key[1]})
            break
    return {'schema_version':1,'dimension':RELEASE_QUERY,'query':True,
            'environment':environment,
            'reverted':[{'task':task,'integration_commit':commit} for task,commit in reverted],
            'live_releases':live}


def apply_native(payload, actor, run, operators=None, journal=None):
    """Caller holds project lock; run(argv) invokes pinned bd and returns stdout."""
    if isinstance(payload,dict) and payload.get('dimension')==RELEASE_QUERY:
        return release_query(payload,actor,run,operators,journal)
    if isinstance(payload,dict) and payload.get('dimension')==RELEASE:
        return apply_release(payload,actor,run,operators,journal)
    validate_payload(payload)
    if payload['actor']!=actor:raise ValueError('payload actor must match request actor')
    rows=[json.loads(x) for x in run(['export','--all']).splitlines() if x.strip()]
    issues={row['id']:row for row in rows if row.get('issue_type')!='event'}
    state=next((r for r in project_facts(rows) if r['id']==payload['task']),None)
    current_scope=state['scope'] if state else None
    recorded=None
    if payload['dimension']!='lifecycle-scope' and current_scope!=payload['scope']:
        recorded=recorded_scope_tokens(rows).get(payload['task'],set())
    return _apply_fact(payload,actor,rows,run,current_scope,
                       _operation_index(rows),issues,recorded=recorded)


def recorded_scope_tokens(rows):
    """``{task: {scope token, ...}}`` for every recorded trusted scope.

    One pass over the export, shared by the single-fact and release paths so a fact
    may be recorded against any scope a task already has, not only its newest.
    """
    return {entry['id']:{content_hash(c['scope']) for c in entry['scopes']}
            for entry in scoped_evidence(rows,())}


def _declared_targets(raw):
    """The caller's optional subset: task names, or full targets to check."""
    names=[];identity={}
    for item in raw:
        if isinstance(item,str):
            one_line(item,'target');names.append(item);continue
        if not isinstance(item,dict):raise ValueError('invalid release target')
        task=item.get('task')
        if not isinstance(task,str) or not LABEL.fullmatch(task):raise ValueError('invalid release target task')
        names.append(task)
        identity[task]=(item.get('source_commit'),item.get('integration_commit'))
    return sorted(set(names)),identity


def _chunks(items,size):
    size=max(1,min(int(size),RELEASE_TARGETS_MAX))
    return [items[index:index+size] for index in range(0,len(items),size)]


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    record=sub.add_parser('record')
    for name in ('config','project','actor','file'):record.add_argument('--'+name,required=True)
    release=sub.add_parser('release')
    for name in ('config','project','actor','file','export','repo'):release.add_argument('--'+name,required=True)
    release.add_argument('--live-verified',action='store_true')
    release.add_argument('--dry-run',action='store_true')
    # An explicit rollback (rev2 item 2): move the environment's live release back to
    # this release and mark the tasks it does not carry as no longer live.
    release.add_argument('--rollback',action='store_true')
    # A caller subset, a previous release to select PREV..R, paging past the bound,
    # and the request group size (the lock is released between groups).
    release.add_argument('--target',action='append',default=None)
    release.add_argument('--previous-release-commit')
    release.add_argument('--page',type=int,default=1)
    release.add_argument('--page-size',type=int)
    release.add_argument('--chunk-size',type=int,default=RELEASE_CHUNK_DEFAULT)
    # The project directory holding the host journal (.integration-reverts/). When
    # given, revert records are read with the SAME operator/journal rule as review;
    # without it (a remote worker usually cannot read the host journal) no revert
    # is trusted locally and the endpoint, which always has both, is authoritative.
    release.add_argument('--journal')
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
            data['live_verified']=bool(data.get('live_verified',False))
            if a.live_verified:data['live_verified']=True
            data['rollback']=bool(data.get('rollback',False)) or a.rollback
            declared,declared_identity=_declared_targets(data.get('targets') or [])
            subset=sorted(set(declared)|set(a.target or []))
            if a.page<1:raise ValueError('--page must be >= 1')
            if a.page_size is not None and a.page_size<1:raise ValueError('--page-size must be >= 1')
            check=dict(data,targets=[])
            validate_release_payload(check,require_targets=False)
            if data['rollback'] and subset:
                raise ValueError('--target/--rollback cannot be combined: a rollback resolves its own targets')
            rows=read_export(a.export)
            journal=a.journal or None
            is_ancestor=git_ancestry(a.repo)
            config=load_json(a.config)
            warnings=[]
            # The default (writing) command is endpoint-authoritative about
            # host-issued reverts (item 5): it asks the endpoint, which always has
            # the host journal and the operator allowlist, instead of trusting a
            # caller-side journal. A dry run stays offline and views reverts locally
            # (pass --journal PROJECT for a host-accurate dry run).
            endpoint_reverts=None
            if not a.dry_run:
                query=dict(data,dimension=RELEASE_QUERY,targets=[],live_verified=False,
                           operation_id=(data['operation_id'][:RELEASE_OPERATION_MAX-6]+'/query'))
                query.pop('rollback',None);query.pop('supersede',None)
                try:
                    answer=request(config,a.project,a.actor,[canonical_bytes(query).decode()],action='lifecycle')
                    if answer['returncode']:raise ValueError(answer['stderr'])
                    endpoint_reverts={(item['task'],str(item['integration_commit']).lower())
                                      for item in (json.loads(answer['stdout']).get('reverted') or [])}
                except (ValueError,OSError,RuntimeError) as exc:
                    warnings.append('the endpoint could not be asked which integrations are reverted (%s); '
                                    'using the local journal view instead'%exc)
            # --previous-release-commit must be a full lowercase commit that really is
            # an ancestor of R and is not R itself: HEAD, a short or uppercase commit,
            # a descendant and a missing commit are all refused (item 5).
            previous=a.previous_release_commit
            if previous is not None:
                if not COMMIT.fullmatch(previous):
                    raise ValueError('--previous-release-commit must be a full lowercase 40-character commit: '+previous)
                if previous==data['scope']['integration_commit']:
                    raise ValueError('--previous-release-commit must not be the release commit itself')
                if not is_ancestor(previous,data['scope']['integration_commit']):
                    raise ValueError('--previous-release-commit must be an ancestor of the release commit: '+previous)
            if data['rollback']:
                selection=rollback_selection(rows,data['scope'],is_ancestor,
                                             reverted=endpoint_reverts,journal=journal,
                                             current_commits=current_contribution_commits(rows))
                targets=selection['targets'];supersede=list(selection['supersede'])
                skipped=list(selection['skipped']);flags=[];verify_only=[]
            else:
                selection=release_selection(rows,data['scope'],is_ancestor,
                                            previous_commit=previous,
                                            subset=set(subset) if subset else None,
                                            current_commits=current_contribution_commits(rows),
                                            live_verified=data['live_verified'],journal=journal,
                                            reverted=endpoint_reverts)
                targets=selection['targets'];supersede=[]
                skipped=list(selection['skipped']);flags=selection['flags']
                verify_only=selection['verify_only']
            seen={item['task'] for item in skipped}
            # A caller-supplied full commit identity must agree with the resolution.
            resolved={item['task']:item for item in targets}
            for task in subset:
                identity=declared_identity.get(task)
                if identity and task in resolved:
                    source,integration=identity
                    if source is not None and source!=resolved[task]['source_commit'] or \
                       integration is not None and integration!=resolved[task]['integration_commit']:
                        skipped.append({'task':task,'reason':'the caller-specified commits do not match the resolved integration'})
                        targets=[item for item in targets if item['task']!=task];seen.add(task)
                elif task not in resolved and task not in seen:
                    skipped.append({'task':task,'reason':'the caller named this task but it is not a new release target (not integrated, already deployed, reverted, or not an ancestor of the release)'})
                    seen.add(task)
            skipped.sort(key=lambda s:s['task'])
            page=targets
            if a.page_size is not None:
                start=(a.page-1)*a.page_size
                page=targets[start:start+a.page_size]
            chunks=_chunks(page,a.chunk_size)
            # A rollback carries its supersede list in the first group payload, so the
            # whole rollback is one logical operation beside the rest of the release.
            # `supersede` is only present on a rollback: the field is refused on a
            # plain release, so a normal payload must not carry it at all.
            chunk_payloads=[]
            for index,chunk in enumerate(chunks):
                payload_chunk=dict(data,targets=chunk)
                if data['rollback']:payload_chunk['supersede']=supersede if index==0 else []
                chunk_payloads.append(payload_chunk)
            if not chunk_payloads and data['rollback'] and supersede:
                chunk_payloads=[dict(data,targets=[],supersede=supersede)]
            report={'operation_id':data['operation_id'],'scope':data['scope'],
                    'targets':page,'skipped':skipped,'flags':flags,
                    'verify_only':sorted(set(verify_only)&{item['task'] for item in page}),
                    'superseded':supersede if data['rollback'] else [],
                    'total_targets':len(targets),'page':a.page,'page_size':a.page_size,
                    'chunks':[len(chunk) for chunk in chunks],'total_chunks':len(chunks),
                    'expected_seconds':round(DRY_RUN_FIXED_SECONDS+DRY_RUN_SECONDS_PER_TARGET*len(page),1),
                    'reader_note':SCOPE_ROLL_NOTE,'dry_run':bool(a.dry_run),
                    'rollback':bool(data['rollback'])}
            if warnings:report['warnings']=warnings
            # Check every target and every derived operation ID for the WHOLE release
            # before the first group request, so a conflict or a stale target in group
            # 3 of 4 writes nothing instead of completing groups 1 and 2. A planted
            # operation id is reported with the SAME JSON failure report as a group
            # failure, not only the plain error line (item 6.2).
            if not a.dry_run:
                op_index=_operation_index(rows)
                try:
                    for chunk_payload in chunk_payloads:
                        _selected,plans,_issues=_release_plan(rows,chunk_payload,journal=journal)
                        for plan in plans.values():
                            for _scope,planned in plan:
                                prior=op_index.get(planned['operation_id'])
                                if prior is not None and prior[0]!=planned:
                                    raise ValueError('operation ID already used for different content: '+planned['operation_id'])
                        for _task,planned in _supersede_plan(rows,chunk_payload):
                            prior=op_index.get(planned['operation_id'])
                            if prior is not None and prior[0]!=planned:
                                raise ValueError('operation ID already used for different content: '+planned['operation_id'])
                except ValueError as exc:
                    report['results']=[]
                    report['groups_completed']=0
                    report['groups_total']=len(chunk_payloads)
                    report['failure']=str(exc)
                    report['fresh_export_required']=False
                    print(json.dumps(report,ensure_ascii=False,indent=2))
                    raise SystemExit('release %s failed before the first group: %s'
                                     %(data['scope']['release_id'],exc))
            if a.dry_run or not chunk_payloads:
                print(json.dumps(report,ensure_ascii=False,indent=2))
            else:
                results=[]
                try:
                    for index,chunk_payload in enumerate(chunk_payloads,1):
                        print('release %s: group %d/%d (%d target(s))'
                              %(data['scope']['release_id'],index,len(chunk_payloads),
                                len(chunk_payload['targets'])),file=sys.stderr,flush=True)
                        answer=request(config,a.project,a.actor,[canonical_bytes(chunk_payload).decode()],action='lifecycle')
                        if answer['returncode']:raise ValueError(answer['stderr'])
                        results.append(json.loads(answer['stdout']))
                except (ValueError,OSError,RuntimeError) as exc:
                    # Report what actually completed and say plainly that a fresh
                    # export is needed, because the written groups cannot be replayed
                    # from the same file with the same export (item 6.3).
                    report['results']=results
                    report['groups_completed']=len(results)
                    report['groups_total']=len(chunk_payloads)
                    report['failure']=str(exc)
                    report['fresh_export_required']=True
                    report['fresh_export_note']=FRESH_EXPORT_NOTE
                    print(json.dumps(report,ensure_ascii=False,indent=2))
                    raise SystemExit('release %s failed after %d of %d group(s): %s. %s'
                                     %(data['scope']['release_id'],len(results),len(chunk_payloads),exc,FRESH_EXPORT_NOTE))
                report['results']=results
                report['groups_completed']=len(results)
                report['groups_total']=len(chunk_payloads)
                report['result']=results[0] if len(results)==1 else None
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
