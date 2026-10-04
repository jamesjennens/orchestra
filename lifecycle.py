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
# Additive, optional release fields. `rollback` marks an explicit rollback,
# `supersede` lists tasks whose live release is being moved back past them, and
# `supersede_scopes` names the exact recorded scopes the same rollback moves past
# (a task that is a TARGET of the rollback keeps its newer deployment scope as the
# live one unless that scope is named here, which is what made a roll forward lose
# the task's second delivery); all three are absent from every payload written
# before this revision, so an older kit and an exact older payload still validate.
RELEASE_OPTIONAL_FIELDS = ('rollback','supersede','supersede_scopes')
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
# `bd` binary on a large project paid 1.6 to 1.7 s per target when each target cost
# ONE write. Rev2 made each target cost THREE writes (scope, deployed, live) and
# the reviewer then measured 438 s for 201 targets (2.18 s per target), against the
# old 2.0 s/target figure, so that figure was no longer an upper bound (rev3 item
# 3.3). The budget is now 3.0 s per target - about 1.0 s per native write - plus
# 15 s of per-request overhead, so 201 targets estimate 618 s and a 25-target group
# (the default) 90 s, well inside the client's 150 s timeout.
DRY_RUN_SECONDS_PER_TARGET = 3.0
DRY_RUN_FIXED_SECONDS = 15.0
# The writing command asks the endpoint which integrations are reverted; the
# offline dry run cannot, so it says plainly that its local revert view may list a
# target the writing run skips (rev3 item 3.4).
DRY_RUN_REVERT_NOTE = ('the dry run is offline: it reads reverts from the local journal view, so '
                       'without --journal PROJECT it may list a target that the writing run skips, '
                       'because the writing run asks the endpoint for the host-issued revert set'
                       ' (kittrial-5bb.107 rev3 item 3.4)')
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


def scoped_event(events,task,dim,scope):
    """Newest event of one task/dimension recorded under ``scope``, or None.

    Ordering is validated across ALL of the dimension's events exactly as
    ``latest_event`` does: a missing or duplicated order is ambiguous and returns
    None, so an ambiguous history is never guessed. Among the events whose payload
    scope IS ``scope`` the newest wins. This is what lets a fact recorded later
    under an OLDER scope leave the current scope's fact readable (rev3 item 2).
    """
    found=events.get((task,dim),[])
    if not found or any(e['order'] is None for e in found):return None
    orders=[e['order'] for e in found]
    if len(set(orders))!=len(orders):return None
    matching=[e for e in found if e['payload'] is not None and e['payload']['scope']==scope]
    if not matching:return None
    return max(matching,key=lambda e:e['order'])


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
    with its native label, event ordering is unambiguous, and the payload's scope
    is that trusted scope. ``project_facts``, ``enabled_states`` and
    ``evidence_owed`` all read this, so they cannot disagree about the same events.

    The value reported for a dimension is the newest event recorded for the task's
    CURRENT scope (rev3 item 2), not simply the newest event of that dimension. The
    native ``dim:`` label tracks the newest event of the dimension, so the tamper
    check compares the label with THAT event; a later fact recorded under an older,
    already-recorded scope therefore never hides the current scope's fact.
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
            newest=latest_event(events,task,dim)
            labelled=(newest is not None and newest['payload'] is not None
                      and label_agrees(labels,newest,dim))
            e=scoped_event(events,task,dim,scope) if scope is not None else None
            valid=labelled and e is not None
            # The event id names the event the value came from when the value is
            # trusted; when it is not, it still names the newest event of the
            # dimension, so a caller can see WHICH event made it unknown.
            dimensions[dim]={'scope':scope,'value':e['value'] if valid else 'unknown',
                             'event_id':e['id'] if valid else (newest['id'] if newest is not None else None),
                             'payload':e['payload'] if valid else None}
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
                # The recording order of that scope's fact, so a per-environment
                # liveness reader can order two scopes by the newest LIVE event
                # rather than by when each scope token first appeared (rev3 item 1).
                if got:entry[dim]['order']=got[0][0]
            entries.append(entry)
        result.append({'id':task,'scopes':entries,'untrusted':untrusted})
    return result


def scope_liveness(candidate):
    """``(value, order_key)`` for ONE recorded scope, or ``(None, None)``.

    The single-scope half of the per-environment liveness rule: an explicit ``live``
    fact names the answer; without one a ``deployed=passed`` scope is a deployment
    written before the liveness dimension existed and reads ``deployed``, so an
    upgraded kit keeps the pre-liveness meaning. ``order_key`` is the recording order
    of the event that decided it, so two scopes can be compared by the NEWEST live
    event rather than by which scope token appeared first (rev4 item 3 (4)).
    """
    fact=candidate.get(LIVE)
    if fact and fact.get('value'):
        return fact['value'],(fact.get('order'),fact.get('event_id') or '')
    deployed=candidate.get('deployed') or {}
    if deployed.get('value')=='passed':
        return 'deployed',(deployed.get('order'),deployed.get('event_id') or '')
    return None,None


def scope_is_superseded(candidate):
    """Does THIS scope's own newest liveness event say the task was moved off it?

    The single-scope half of the rollback rule: an explicit ``live=superseded`` fact
    written under this scope says the task is no longer live HERE. Any other scope
    that has been re-lived after it wins, which is what makes a rollback and a roll
    forward agree on exactly one live scope.
    """
    fact=candidate.get(LIVE) or {}
    return fact.get('value')==LIVE_SUPERSEDED


def environment_liveness(scopes,environment):
    """What the scope history says about one task's liveness in ONE environment.

    Returns ``(scope_entry, value)`` where ``scope_entry`` is the
    ``scoped_evidence`` entry whose recorded scope carries the answer and ``value``
    is:

    * ``live`` - an explicit ``live=live`` fact in that environment;
    * ``superseded`` - the newest thing recorded about this task's liveness in that
      environment is a ``live=superseded`` fact, so it is deployed there but not live;
    * ``deployed`` - no ``live`` fact at all but a ``deployed=passed`` scope, i.e.
      data written before the liveness dimension existed. It is read as live so an
      upgraded kit keeps the pre-liveness meaning;
    * ``None`` - nothing is deployed there, with ``scope_entry`` None.

    THE RULE, applied once here for every reader: the scope carrying the NEWEST
    liveness event wins, where a liveness event is a ``live`` fact or, for a scope
    with no ``live`` fact, its legacy ``deployed=passed`` recording. A scope whose
    own newest liveness event is ``superseded`` is not re-lived and loses to any
    scope that was re-lived after it (a rollback re-lives an OLDER release, so its
    newer event must win), and when it is the newest event of all the answer is that
    the task is not live in this environment. A scope that is not ``live`` and has no
    legacy ``deployed=passed`` is not a liveness event at all.
    """
    newest=None
    for candidate in scopes:
        if (candidate['scope'].get('environment') or '')!=environment:continue
        value,key=scope_liveness(candidate)
        if value is not None and (newest is None or key>newest[0]):
            newest=(key,candidate,value)
    return (newest[1],newest[2]) if newest is not None else (None,None)


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
        # ONE live row per task and environment (rev4): the rule itself says whether
        # THIS row's scope is the live one, so the row is read with the same call the
        # other readers use. A scope the task is live on reads ``live``, and a scope it
        # has been moved off - superseded, or simply not the newest liveness event -
        # reads ``superseded``. A row written before this revision reads ``unknown``.
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
            recorded=(facts.get(LIVE) or {}).get('value','unknown')
            live=recorded
            if recorded in ('live','deployed'):
                that_scope,that_value=environment_liveness(entry['scopes'],
                                                           scope.get('environment',''))
                if that_value not in ('live','deployed') or that_scope is None \
                        or that_scope['scope']!=scope:
                    live=LIVE_SUPERSEDED
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

    ``rollback``, ``supersede`` and ``supersede_scopes`` are additive optional
    fields: an older payload (or an older kit's validator) never carries them. Only a
    stated MOVE may cover no new target at all (a rollback, or a release that names
    tasks or scopes to supersede - a hotfix that only drops work); every other
    release needs at least one target. ``supersede`` is allowed on a plain deploy too
    (rev3 item 1): a deploy of a hotfix release that does not carry a live task makes
    that task no longer live. A task may never be named as both a target and
    superseded (rev3 item 3.1), while a TARGET may carry ``supersede_scopes``
    (rev4 item 2), because a rollback re-lives a task at R and moves the same task
    past its own newer deployment.
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
    targets=p['targets']
    if not isinstance(targets,list):raise ValueError('invalid release targets')
    # A release that covers no new target is only valid when it is a STATED MOVE: a
    # rollback, a task-level supersede, or the scope-level list a rollback uses to move
    # past a task's own newer deployment (rev4 item 2). Nothing else may write a
    # release that covers and moves nothing at all.
    stated_move=bool(p.get('rollback') or p.get('supersede') or p.get('supersede_scopes'))
    if require_targets and not targets and not stated_move:
        raise ValueError('a release needs at least one integrated target')
    if len(targets)>RELEASE_TARGETS_MAX:raise ValueError('a release covers at most %d targets'%RELEASE_TARGETS_MAX)
    seen=set()
    for target in targets:
        if not isinstance(target,dict) or set(target)-{'note','verify_scope'}!={'task','source_commit','integration_commit'}:raise ValueError('invalid release target')
        if not isinstance(target['task'],str) or not LABEL.fullmatch(target['task']):raise ValueError('invalid release target task')
        if target['task'] in seen:raise ValueError('duplicate release target')
        seen.add(target['task'])
        for key in ('source_commit','integration_commit'):
            if not isinstance(target[key],str) or not COMMIT.fullmatch(target[key]):raise ValueError('release target needs a full lowercase '+key)
        if 'note' in target:one_line(target['note'],'note')
        if 'verify_scope' in target:
            validate_scope(target['verify_scope'])
            if (not p['live_verified'] or p.get('rollback')
                    or target['verify_scope']['environment']!=p['scope']['environment']
                    or any(target['verify_scope'][key]!=target[key] for key in ('source_commit','integration_commit'))):
                raise ValueError('invalid release verification scope')
    if supersede:
        # A hand-built payload could otherwise name one task as both carried by the
        # release and dropped by it, and the readers would then disagree about
        # whether it is live (rev3 item 3.1).
        overlap=sorted(set(supersede)&seen)
        if overlap:raise ValueError('a task cannot be both a release target and superseded: '+overlap[0])
    supersede_scopes=p.get('supersede_scopes')
    if supersede_scopes is not None:
        # The additive scope-level companion of `supersede` (rev4 item 2). It names
        # recorded SCOPES, not tasks, so a TARGET may carry one: a rollback moves an
        # environment past a task's own newer deployment while it re-records that
        # task live at R.
        if not isinstance(supersede_scopes,list):raise ValueError('invalid release supersede_scopes list')
        if len(supersede_scopes)>RELEASE_TARGETS_MAX:raise ValueError('a release supersedes scopes for at most %d entries'%RELEASE_TARGETS_MAX)
        filed=set()
        for item in supersede_scopes:
            if not isinstance(item,dict) or set(item)!={'task','scope'}:raise ValueError('invalid release supersede scope entry')
            if not isinstance(item['task'],str) or not LABEL.fullmatch(item['task']):raise ValueError('invalid release supersede scope task')
            validate_scope(item['scope'])
            if item['scope']['environment']!=p['scope']['environment']:
                raise ValueError('a release supersedes scopes in its own environment only')
            key=content_hash(item['scope'])
            if key in filed:raise ValueError('duplicate release supersede scope')
            filed.add(key)
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


def run_advances_past(live_scope,release,is_ancestor):
    """Does running ``release`` here ADVANCE the environment past this live scope?

    THE ONE GATE of the rev4 rule, used by both the supersede resolution and the
    already-deployed resolution so they can never disagree:

    * ``True`` exactly when the live deployment's own integration commit is an
      ANCESTOR of R and is not R itself - R strictly contains what is live, which is
      what makes deploying R a move forward that leaves the older deployment behind.
      That covers the hotfix/mainline release that drops a task it does not carry.
    * ``False`` when R is an ANCESTOR of the live commit (R is a release this
      environment has already moved past: a redeploy of an older release, an exact
      retry of its command, or a late ``--live-verified``), when R IS the live commit
      (its own redeploy), when the two DIVERGE (a hotfix branch taken before the live
      release does not replace it), and whenever ancestry cannot be decided.

    A ``False`` therefore means "this run writes no liveness for that task": it can
    neither supersede it nor move it back, which is exactly review item 1 ("a run
    without --rollback never writes live=superseded for a task it does not target when
    its release is an ancestor of, or equal to, a release live in that environment")
    without also silently dropping a mainline hotfix. Moving an environment backwards
    on purpose is what ``--rollback`` is for, with its own refusals.
    """
    if live_scope is None:return False
    commit=live_scope['scope'].get('integration_commit') or ''
    if not COMMIT.fullmatch(commit) or commit==release:return False
    try:return bool(is_ancestor(commit,release))
    except ValueError:return False


def live_scope_is_release(live_scope,scope,chosen,is_ancestor,deployed=None):
    """Is what is live here already the deployment this run would write for the task?

    THE decision both the client selection and the endpoint write plan use, so a
    release run can never write a fact the selection said it would not write. It is
    True when the live deployment is in the release id and environment being run and
    it is at or past the integration this run would write - the live scope carries
    either the chosen integration or the release commit being deployed, or it carries
    a deployment that already contains the chosen integration. Deploying such a task
    again changes no fact, so it is reported the pre-rev4 way ("already deployed ...;
    not reselected") instead of being passed over as a task this run moves backwards.
    Anything else - a newer release, a divergent branch, undecidable ancestry - is not
    this release and the caller must leave it alone.
    """
    if live_scope is None:return False
    live=live_scope['scope']
    if not scope.get('release_id'):return False
    if (live.get('release_id') or '')!=(scope.get('release_id') or ''):return False
    if (live.get('environment') or '')!=(scope.get('environment') or ''):return False
    live_commit=live.get('integration_commit') or ''
    chosen_commit=(deployed or chosen)['scope']['integration_commit']
    if not COMMIT.fullmatch(live_commit):return False
    if live_commit in (chosen_commit,scope.get('integration_commit')):return True
    # The reverse test: the live scope of the SAME release id that does NOT carry the
    # chosen integration is a DIFFERENT deployment of this release, so this run must
    # write under it rather than treat the task as already deployed. Ancestry is only
    # consulted when it can decide that; an undecidable answer reads as "write".
    if is_ancestor is not None:
        try:
            if not is_ancestor(live_commit,chosen_commit):return False
        except ValueError:return False
    return True


def release_selection(rows,scope,is_ancestor,previous_commit=None,subset=None,reverted=None,current_commits=None,
                      live_verified=False,operators=None,journal=None):
    """Incremental deploy, derived from the global task/environment live state.

    Choose the newest trusted integration contained in the release. A deployment
    already carrying that integration is left at its recorded release; verification
    may be appended there without moving it. Newer carried integrations advance,
    and a negative newest liveness event permits restoration. Older/divergent
    requests and tasks the release does not carry are left alone. Full membership
    changes require explicit rollback; plain deploy is not a hotfix rollback.
    """
    release=scope['integration_commit'];environment=scope['environment']
    if reverted is None:reverted=reverted_integrations(rows,operators,journal)
    if current_commits is None:current_commits=current_contribution_commits(rows)
    wanted=set(subset) if subset else None
    targets=[];skipped=[];flags=[];verifying=[];restoring=[]
    for entry in scoped_evidence(rows,('integrated','deployed','live-verified',LIVE)):
        task=entry['id'];scopes=entry['scopes']
        if wanted is not None and task not in wanted:continue
        chosen=None;reason=entry['untrusted'].get('integrated')
        for candidate in sorted(scopes,key=lambda c:((c.get('integrated') or {}).get('order') or -1,
                                                      (c.get('integrated') or {}).get('event_id') or ''),reverse=True):
            if (candidate.get('integrated') or {}).get('value')!='passed':continue
            commit=candidate['scope']['integration_commit']
            if not COMMIT.fullmatch(commit):
                reason='integration commit is not a full lowercase commit: '+str(commit);continue
            if (task,commit.lower()) in reverted:
                reason='integration commit was reverted by an operator revert record: '+commit;continue
            try:
                if previous_commit is not None and not is_ancestor(previous_commit,commit):
                    reason='integration commit is not in the previous release..release range: '+commit;continue
                if is_ancestor(commit,release):chosen=candidate;break
            except ValueError as exc:reason=str(exc)
        if chosen is None:
            if reason or any((c.get('integrated') or {}).get('value')=='passed' for c in scopes):skipped.append({'task':task,'reason':reason or 'no passing integration commit is an ancestor of the release'})
            continue
        live_scope,live_value=environment_liveness(scopes,environment)
        active=live_scope if live_value in ('live','deployed') else None
        if active is not None:
            deployed_commit=active['scope']['integration_commit']
            try:
                carried=is_ancestor(chosen['scope']['integration_commit'],deployed_commit) and is_ancestor(deployed_commit,release)
                forward=run_advances_past(active,release,is_ancestor)
            except ValueError as exc:
                skipped.append({'task':task,'reason':str(exc)});continue
            if carried:
                if live_verified and (active.get('live-verified') or {}).get('value')!='passed':
                    verifying.append(task)
                else:
                    skipped.append({'task':task,'reason':'already deployed for %s at release %s (integration commit %s); not reselected'
                                    %(environment,active['scope'].get('release_id') or 'unknown',deployed_commit)})
                    continue
            elif not forward:
                skipped.append({'task':task,'reason':'already live in %s at release %s, which this run does not advance past; nothing to write'
                                %(environment,active['scope'].get('release_id') or 'unknown')});continue
        elif live_value==LIVE_SUPERSEDED:restoring.append(task)
        target={'task':task,'source_commit':chosen['scope']['source_commit'],
                'integration_commit':chosen['scope']['integration_commit']}
        if task in verifying:
            target.update(source_commit=active['scope']['source_commit'],
                          integration_commit=active['scope']['integration_commit'],
                          verify_scope=dict(active['scope']))
        targets.append(target)
        current=current_commits.get(task)
        if current and current.lower()!=target['source_commit'].lower():
            flags.append({'task':task,'flag':'delivery-not-current',
                          'reason':'the deployed fact would be for delivery %s but the task\'s current contribution is %s'%(target['source_commit'],current),
                          'delivery':dict(scope,source_commit=target['source_commit'],integration_commit=target['integration_commit']),
                          'current_contribution':current})
    return {'targets':sorted(targets,key=lambda t:t['task']),'skipped':sorted(skipped,key=lambda t:t['task']),
            'flags':sorted(flags,key=lambda t:t['task']),'verify_only':sorted(verifying),
            'supersede':[],'restored':sorted(restoring)}


def release_targets(rows,scope,is_ancestor,previous_commit=None):
    """Backward-compatible ``(targets, skipped)`` view of ``release_selection``."""
    selection=release_selection(rows,scope,is_ancestor,previous_commit=previous_commit)
    return selection['targets'],selection['skipped']


def require_rollback_target_deployed(rows,scope):
    """Refuse a rollback whose environment or release has no deployment (rev3 3.1).

    A rollback is a move of an existing environment back to a release it once ran.
    A rollback in an environment with nothing deployed is not a rollback at all (it
    would act as a full deploy), and a rollback to a release that was never deployed
    in that environment would supersede everything from an unverifiable position.
    Both are refused before the first write, on the client and at the endpoint.
    """
    environment=scope['environment']
    release_id=scope.get('release_id') or ''
    any_deployed=False;release_deployed=False
    for entry in scoped_evidence(rows,('deployed',)):
        for candidate in entry['scopes']:
            if (candidate['scope'].get('environment') or '')!=environment:continue
            if (candidate.get('deployed') or {}).get('value')!='passed':continue
            any_deployed=True
            # A recorded release scope names the RELEASE by release_id (its
            # integration_commit is the task's own integration commit), so the
            # release identity is release_id + environment.
            if (candidate['scope'].get('release_id') or '')==release_id:
                release_deployed=True
    if not any_deployed:
        raise ValueError('refusing a rollback in %s: nothing is deployed in that environment'%environment)
    if not release_deployed:
        raise ValueError('refusing a rollback: release %s was never deployed in %s'%(release_id,environment))


def rollback_selection(rows,scope,is_ancestor,reverted=None,current_commits=None,
                       operators=None,journal=None):
    """The explicit rollback plan: who R carries, and who it supersedes.

    A rollback to release R (rev2 item 2) is not a normal deploy: it must also say
    that tasks shipped only after R are no longer live. ``targets`` are the tasks
    in R's membership - their chosen trusted integrated commit is an ancestor of R
    and is not host-reverted - which the endpoint re-records live at R.
    ``supersede`` names tasks live in THIS ENVIRONMENT (decided from the scope
    history, rev3 item 1) that are NOT in R's membership, so they read not live
    instead of silently passing. ``supersede_scopes`` names the exact recorded
    SCOPES the rollback moves past for the tasks it DOES carry (rev4 item 2): a task
    delivered twice and live at a newer R2 keeps R2's own ``live=live`` fact, so
    ``evidence-owed`` would show two live rows for it and a later deploy of R2 would
    read it as already deployed and lose the second delivery. Each named scope gets
    its own ``live=superseded`` fact, which is what makes the roll forward restore
    it. Every other omission is in ``skipped`` with a cause. The environment must
    already have a deployment of the rollback target, or the whole rollback is
    refused (rev3 item 3.1).
    """
    release=scope['integration_commit'];environment=scope['environment']
    if reverted is None:reverted=reverted_integrations(rows,operators,journal)
    if current_commits is None:current_commits=current_contribution_commits(rows)
    require_rollback_target_deployed(rows,scope)
    targets=[];supersede=[];skipped=[]
    # (task, scope token) -> the exact scope this rollback moves past. A rollback
    # keeps R's membership and everything R contains, and stops every OTHER recorded
    # deployment of the environment from reading live (rev4 item 2).
    beyond={}
    for entry in scoped_evidence(rows,('integrated','deployed','live-verified',LIVE)):
        task=entry['id'];scopes=entry['scopes']
        # Every recorded deployment of this environment that R does NOT contain is a
        # scope this rollback moves past, whether the task is carried back by R or
        # dropped by it (rev4 item 2). Collected in its own pass so a candidate that
        # ends the selection loop early cannot hide a later scope.
        for candidate in scopes:
            if (candidate.get('deployed') or {}).get('value')!='passed':continue
            if (candidate['scope'].get('environment') or '')!=environment:continue
            dcommit=candidate['scope']['integration_commit']
            try:within=bool(COMMIT.fullmatch(dcommit)) and is_ancestor(dcommit,release)
            except ValueError:within=False
            # A scope R contains is a deployment the rollback keeps; a scope R does
            # not contain is one it moves past, unless it is already superseded
            # (then there is nothing left to write).
            if not within and scope_liveness(candidate)[0]!=LIVE_SUPERSEDED:
                beyond[(task,content_hash(candidate['scope']))]={'task':task,'scope':dict(candidate['scope'])}
        chosen=None;reason=None
        for candidate in sorted(scopes,key=lambda c:((c.get('integrated') or {}).get('order') or -1,
                                                      (c.get('integrated') or {}).get('event_id') or ''),reverse=True):
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
        if chosen is not None:
            targets.append({'task':task,'source_commit':chosen['scope']['source_commit'],
                            'integration_commit':chosen['scope']['integration_commit']})
            continue
        # Not part of R. Supersede it only when it is live in THIS environment (its
        # own history, whichever scope is newest overall) at a release R does not
        # contain. A task with no deployment here is left alone, and a task live in
        # another environment is not touched by this rollback (rev3 item 1/4.1c).
        live_scope,live_value=environment_liveness(scopes,environment)
        if live_value==LIVE_SUPERSEDED:continue
        if live_scope is None:
            if reason:skipped.append({'task':task,'reason':reason})
            continue
        dcommit=live_scope['scope'].get('integration_commit') or ''
        try:within=COMMIT.fullmatch(dcommit) and is_ancestor(dcommit,release)
        except ValueError:within=False
        if within:
            skipped.append({'task':task,'reason':'already live in %s at a release contained in the rollback target'%environment})
            continue
        supersede.append(task)
    targets.sort(key=lambda t:t['task']);supersede=sorted(set(supersede));skipped.sort(key=lambda s:s['task'])
    # A task this rollback CARRIES is a target, so it is never named by the
    # task-level `supersede` (which would be refused as an overlap); the newer
    # deployment scopes the rollback moves past for it are named scope by scope
    # instead. That is what leaves exactly ONE live row for the task (rev4 item 2).
    carried={item['task'] for item in targets}
    supersede=sorted(set(supersede)-carried)
    superseded_scopes=sorted(beyond.values(),
                             key=lambda item:(item['task'],item['scope']['release_id']))
    return {'targets':targets,'supersede':supersede,'supersede_scopes':superseded_scopes,
            'skipped':skipped}


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

    Returns ``(selected,plans,issues,evidence)``. ``selected`` entries are dicts
    carrying ``target``, ``release_scope``, ``already``, ``verified`` and
    ``verify_only``; ``plans[task]`` is an ordered list of ``(fact_scope,planned_payload)``
    where ``fact_scope`` is the scope the endpoint must have current (or, for a
    verify-only target, the task's current scope against which the release scope
    must already be recorded) before that fact; ``None`` for the scope write.
    ``evidence`` is the per-scope read shared with ``_supersede_plan``.

    A target already deployed at this release/environment whose live-verification
    is not yet passed is VERIFY-ONLY: its plan is the single ``live-verified`` fact
    under the scope that really carries the deployment (the release's own scope when
    it is the one deployed there, otherwise the recorded deployment scope), so the
    task's current scope never moves (rev2 item 3, rev4 item 3 (3)). Every other
    target gets the scope (only when it differs), the ``deployed=passed`` fact (only
    when the current scope is not already deployed) and the ``live=live`` liveness
    fact (only when the current scope is not already live), so a plain deploy and a
    roll-forward both end live.
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
        live_scope,live_value=environment_liveness(scopes,release_scope['environment'])
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
        is_live=live_value in ('live','deployed') and live_scope['scope']==release_scope
        if any(item['task']==task for item in payload.get('supersede_scopes') or []):is_live=False
        common=dict(schema_version=1,task=task,scope=release_scope,evidence=list(payload['evidence']),
                    provenance=payload['provenance'],actor=payload['actor'])
        # Already deployed here and only the verification is owed: record that
        # evidence against the scope that really carries the deployment and touch
        # nothing else (rev4 item 3 (3)). The release scope is used only when no
        # recorded scope of this task carries this exact deployment, so a task
        # deployed at an earlier release and verified from a later one no longer has
        # its `live-verified` fact filed under the later release.
        verify_only=(already is not None and payload['live_verified'] and not payload.get('rollback')
                     and live_value in ('live','deployed') and not scope_is_superseded(already))
        # The client explicitly binds an incremental verification to the currently
        # live recorded scope. A direct target with no binding retains the historical
        # release contract; it is never silently redirected to a different release.
        if 'verify_scope' in target:
            if live_value not in ('live','deployed') or live_scope['scope']!=target['verify_scope']:
                raise ValueError('verification target is no longer live; get a fresh export')
            if (live_scope.get('deployed') or {}).get('value')!='passed':
                raise ValueError('verification target has no passed deployment')
            release_scope=dict(live_scope['scope']);common['scope']=release_scope
            already=live_scope
            verified=live_scope if (live_scope.get('live-verified') or {}).get('value')=='passed' else None
            is_live=True;verify_only=True
        fact_scope=release_scope
        if verify_only:
            exact_deployed=next((c for c in scopes if (c.get('deployed') or {}).get('value')=='passed'
                                 and c['scope']==release_scope),None)
            fact_scope=(exact_deployed or already or {}).get('scope') or release_scope
        plan=[]
        if verify_only:
            if verified is None:
                plan.append((fact_scope,dict(common,operation_id=derived_id(payload['operation_id'],'verified',task),
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
            if not is_live:
                plan.append((release_scope,dict(common,operation_id=derived_id(payload['operation_id'],'live',task),
                                                dimension=LIVE,value='live')))
            if payload['live_verified'] and verified is None:
                plan.append((release_scope,dict(common,operation_id=derived_id(payload['operation_id'],'verified',task),
                                                dimension='live-verified',value='passed')))
        selected.append({'target':target,'release_scope':release_scope,'already':already,
                         'verified':verified,'verify_only':verify_only})
        plans[task]=plan
    return selected,plans,issues,evidence


def _supersede_plan(rows,payload,issues=None,evidence=None):
    """Read-only: the ``live=superseded`` write for every task a release drops.

    ``payload['supersede']`` names tasks the client resolved as no longer live in
    the payload's environment. Each must exist, must not be a target of the same
    release, and must be live there according to THAT ENVIRONMENT'S scope history
    (``environment_liveness``, rev3 item 1) - not according to the task's single
    newest scope, so a task deployed to production and then to staging can still be
    superseded in production. The fact is written under the environment's live
    scope, which may be older than the task's current scope; a task already recorded
    ``superseded`` there is a no-op, so an exact retry reconciles. Returns a list of
    ``(task,planned_payload)``; evidence events are untouched.
    """
    if issues is None:
        issues={row['id']:row for row in rows if row.get('issue_type')!='event'}
    if evidence is None:
        evidence={entry['id']:entry for entry in
                  scoped_evidence(rows,('integrated','deployed','live-verified',LIVE))}
    environment=payload['scope']['environment']
    target_tasks={target['task'] for target in payload.get('targets') or []}
    planned=[]
    for task in sorted(payload.get('supersede') or []):
        issue=issues.get(task)
        if issue is None or issue.get('issue_type')=='event':
            raise ValueError('unknown release supersede task: '+task)
        if task in target_tasks:
            raise ValueError('a task cannot be both a release target and superseded: '+task)
        live_scope,live_value=environment_liveness(evidence.get(task,{}).get('scopes',[]),environment)
        if live_value==LIVE_SUPERSEDED:
            continue
        if live_scope is None:
            raise ValueError('release supersede task is not deployed in %s: %s'%(environment,task))
        planned.append((task,dict(schema_version=1,task=task,scope=live_scope['scope'],
                                  evidence=list(payload['evidence']),provenance=payload['provenance'],
                                  actor=payload['actor'],
                                  operation_id=derived_id(payload['operation_id'],'superseded',task),
                                  dimension=LIVE,value=LIVE_SUPERSEDED)))
    return planned


def derived_scope_id(operation_id,task,scope):
    """A deterministic per-(task, scope) operation ID, so an exact retry reconciles.

    A rollback may move the same task past more than one recorded deployment, so the
    scope-level supersede needs an ID that names the SCOPE as well as the task.
    """
    commit=scope.get('integration_commit') or ''
    label='superseded-scope'
    if COMMIT.fullmatch(commit):
        token=operation_id+'/'+label+'/'+task+'/'+commit[:12]
        if len(token)<=161:return token
    return derived_id(operation_id,label,task)+'/'+content_hash(scope)[:12]


def _supersede_scope_plan(rows,payload,issues=None,evidence=None):
    """Read-only: the ``live=superseded`` write for every scope a run moves past.

    ``payload['supersede_scopes']`` names recorded SCOPES (rev4 item 2). Each entry's
    task must exist, the scope must be a scope that task already has recorded IN THE
    PAYLOAD'S ENVIRONMENT (``_apply_fact`` accepts a fact under any recorded scope of
    the task), and it must be a real deployment there: ``deployed=passed``. The
    environment check runs twice - the payload shape, and the recorded scope - because
    a rollback in one environment must never touch another's row. A scope already
    recorded ``superseded`` is a no-op, so an exact retry reconciles. Unlike the
    task-level ``supersede``, a TARGET may be named here: the rollback re-lives the
    task at R while marking the newer deployment it moves past. Returns a list of
    ``(task,planned_payload)``; evidence events are untouched.
    """
    if issues is None:
        issues={row['id']:row for row in rows if row.get('issue_type')!='event'}
    if evidence is None:
        evidence={entry['id']:entry for entry in
                  scoped_evidence(rows,('integrated','deployed','live-verified',LIVE))}
    environment=payload['scope']['environment']
    planned=[]
    for item in payload.get('supersede_scopes') or []:
        task=item['task'];wanted=item['scope']
        issue=issues.get(task)
        if issue is None or issue.get('issue_type')=='event':
            raise ValueError('unknown release supersede scope task: '+task)
        recorded=next((c for c in evidence.get(task,{}).get('scopes',[]) if c['scope']==wanted),None)
        if recorded is None:
            raise ValueError('release supersede scope is not a recorded scope of %s: %s/%s'
                             %(task,wanted.get('release_id') or 'unknown',wanted.get('integration_commit') or ''))
        if (recorded['scope'].get('environment') or '')!=environment:
            raise ValueError('release supersede scope is not in environment %s: %s'%(environment,task))
        if (recorded.get('deployed') or {}).get('value')!='passed':
            raise ValueError('release supersede scope is not a deployment of %s: %s'%(task,environment))
        if (recorded.get(LIVE) or {}).get('value')==LIVE_SUPERSEDED:continue
        planned.append((task,dict(schema_version=1,task=task,scope=dict(recorded['scope']),
                                  evidence=list(payload['evidence']),provenance=payload['provenance'],
                                  actor=payload['actor'],
                                  operation_id=derived_scope_id(payload['operation_id'],task,recorded['scope']),
                                  dimension=LIVE,value=LIVE_SUPERSEDED)))
    return planned


def _check_release_identity(payload,op_index):
    """Bind reused release IDs to their original release/environment before writes."""
    prefix=payload['operation_id']+'/'
    for operation,(prior,_event) in op_index.items():
        if not operation.startswith(prefix):continue
        if any(prior['scope'][key]!=payload['scope'][key] for key in ('release_id','environment')):
            raise ValueError('operation ID already used for different release/environment: '+payload['operation_id'])


def _check_release_operations(plans,negative,op_index,evidence):
    """Check every receipt before writing, including historical liveness retries."""
    for planned in [p for plan in plans.values() for _,p in plan]+[p for _,p in negative]:
        prior=op_index.get(planned['operation_id'])
        if prior is None:continue
        if prior[0]!=planned:
            raise ValueError('operation ID already used for different content: '+planned['operation_id'])
        if planned['dimension']!=LIVE:continue
        current,value=environment_liveness(evidence.get(planned['task'],{}).get('scopes',[]),
                                           planned['scope']['environment'])
        effective=(current is not None and current['scope']==planned['scope'] and value==planned['value'])
        if not effective:
            raise ValueError('operation ID was already used for a state that has since changed; a new operation ID is needed: '+planned['operation_id'])


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
    out of; every task named in ``supersede`` gets ``live=superseded`` under its
    live scope IN THIS ENVIRONMENT (rev3 item 1) with its evidence untouched, on a
    plain deploy and on an explicit rollback alike, and every scope named in
    ``supersede_scopes`` gets the same fact under EXACTLY that scope (rev4 item 2).
    An explicit ``rollback`` is additionally refused unless the environment already
    has a deployment of the rollback target (rev3 item 3.1).

    Ancestry is NOT decided here. The endpoint re-verifies that each target is
    integrated at the commits named in its own export; whether that commit is an
    ancestor of the release commit is decided by git in the caller's checkout (see
    ``release_selection``), exactly like ``commit``/``base_commit`` in contribution
    review.
    """
    validate_release_payload(payload,require_targets=not (payload.get('rollback')
                                                          or payload.get('supersede')
                                                          or payload.get('supersede_scopes')))
    # A release that covers no new target must at least be a STATED move: a rollback,
    # a task-level supersede, or the scope-level list a rollback uses to move past a
    # task's own newer deployment (rev4 item 2). Nothing else may write a release that
    # covers and moves nothing at all.
    if not payload.get('targets') and not (payload.get('rollback') or payload.get('supersede')
                                           or payload.get('supersede_scopes')):
        raise ValueError('a release needs at least one integrated target')
    if payload['actor']!=actor:raise ValueError('payload actor must match request actor')
    rows=[json.loads(x) for x in run(['export','--all']).splitlines() if x.strip()]
    if payload.get('rollback'):require_rollback_target_deployed(rows,payload['scope'])
    selected,plans,issues,evidence=_release_plan(rows,payload,operators,journal)
    superseded=_supersede_plan(rows,payload,issues,evidence)
    superseded_scopes=_supersede_scope_plan(rows,payload,issues,evidence)
    current=current_contribution_commits(rows)
    op_index=_operation_index(rows)
    _check_release_identity(payload,op_index)
    _check_release_operations(plans,superseded+superseded_scopes,op_index,evidence)
    # Scope tokens already recorded for a task, so a verify-only fact under the
    # release's older scope, and a per-environment supersede under the environment's
    # live scope, are accepted without moving the task's current scope (item 3,
    # rev3 item 1). The SINGLE-fact path deliberately does not get this relaxation.
    recorded=recorded_scope_tokens(rows)
    supersede_results=[]
    for task,planned in superseded:
        result=_apply_fact(planned,actor,rows,run,planned['scope'],op_index,issues,
                           recorded=recorded.get(task))
        supersede_results.append({'task':task,'live':result.get('event_id'),
                                  'scope':dict(planned['scope']),
                                  'reconciled':bool(result.get('reconciled'))})
    superseded_scope_results=[]
    for task,planned in superseded_scopes:
        result=_apply_fact(planned,actor,rows,run,planned['scope'],op_index,issues,
                           recorded=recorded.get(task))
        superseded_scope_results.append({'task':task,'live':result.get('event_id'),
                                         'scope':dict(planned['scope']),
                                         'reconciled':bool(result.get('reconciled'))})
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
    return {'operation_id':payload['operation_id'],'release_id':payload['scope']['release_id'],
            'environment':payload['scope']['environment'],'targets':results,
            'rollback':bool(payload.get('rollback')),
            'superseded':supersede_results,
            'superseded_scopes':superseded_scope_results,
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

    ``live_releases`` is decided PER TASK PER ENVIRONMENT from the scope history
    (rev3 item 3.5): the previous reader looked only at each task's newest live
    scope and gave up when that scope was in another environment, so an environment
    that had live tasks reported none. A pre-liveness ``deployed=passed`` scope is
    reported too, under the value it really is, so an upgraded kit does not invent a
    ``live`` fact that was never written. That legacy branch is reachable only when
    the scope read asks for ``deployed`` as well as ``LIVE`` (rev4 item 3 (1)): the
    previous call asked for the live dimension alone, so the documented branch could
    never fire and no test could tell.
    """
    validate_query_payload(payload)
    if payload['actor']!=actor:raise ValueError('payload actor must match request actor')
    rows=[json.loads(x) for x in run(['export','--all']).splitlines() if x.strip()]
    environment=payload['scope']['environment']
    reverted=sorted(reverted_integrations(rows,operators,journal))
    live=[]
    seen=set()
    for entry in scoped_evidence(rows,(LIVE,'deployed')):
        task=entry['id']
        scope,value=environment_liveness(entry['scopes'],environment)
        if scope is None or value not in ('live','deployed'):continue
        # One row PER TASK and environment (rev4): two tasks live at the same release
        # and integration commit are two live rows, so the key names the task as well.
        key=(task,scope['scope'].get('release_id') or '',scope['scope'].get('integration_commit') or '')
        if key in seen:continue
        seen.add(key)
        live.append({'task':task,'release_id':key[1],'integration_commit':key[2],'liveness':value})
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
    # A SINGLE fact must name the task's CURRENT scope: a fact recorded under an
    # older, previously recorded scope is refused here with main's rule (rev3 item
    # 2). Only the release operation may write a fact under an already-recorded
    # older scope - a verify-only target or a per-environment supersede; see
    # apply_release and docs/OPERATIONAL_WORKFLOW.md.
    return _apply_fact(payload,actor,rows,run,current_scope,
                       _operation_index(rows),issues)


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
                query.pop('supersede_scopes',None)
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
                # The scope-level list of what the rollback moves past (rev4 item 2):
                # a task the rollback carries back may still have a newer deployment
                # scope of this environment reading live, and that is what made a
                # roll forward lose the task's second delivery.
                supersede_scopes=list(selection['supersede_scopes'])
                skipped=list(selection['skipped']);flags=[];verify_only=[]
            else:
                selection=release_selection(rows,data['scope'],is_ancestor,
                                            previous_commit=previous,
                                            subset=set(subset) if subset else None,
                                            current_commits=current_contribution_commits(rows),
                                            live_verified=data['live_verified'],journal=journal,
                                            reverted=endpoint_reverts)
                targets=selection['targets'];supersede=list(selection['supersede'])
                supersede_scopes=[]
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
            # The supersede list rides the first group payload, so a rollback or a
            # hotfix deploy that drops tasks is one logical operation beside the rest
            # of the release. The field is only present when it is non-empty, so a
            # plain incremental deploy still sends the exact pre-rev3 payload shape
            # (rev3 item 1). The scope-level list rides the first group the same way
            # (rev4 item 2), so an older client-side payload is unchanged.
            chunk_payloads=[]
            for index,chunk in enumerate(chunks):
                payload_chunk=dict(data,targets=chunk)
                if supersede:payload_chunk['supersede']=supersede if index==0 else []
                if supersede_scopes:payload_chunk['supersede_scopes']=supersede_scopes if index==0 else []
                chunk_payloads.append(payload_chunk)
            if not chunk_payloads and (supersede or supersede_scopes):
                payload_chunk=dict(data,targets=[])
                if supersede:payload_chunk['supersede']=supersede
                if supersede_scopes:payload_chunk['supersede_scopes']=supersede_scopes
                chunk_payloads=[payload_chunk]
            report={'operation_id':data['operation_id'],'scope':data['scope'],
                    'targets':page,'skipped':skipped,'flags':flags,
                    'verify_only':sorted(set(verify_only)&{item['task'] for item in page}),
                    'superseded':supersede,
                    'superseded_scopes':[{'task':item['task'],'scope':item['scope']}
                                         for item in supersede_scopes],
                    'total_targets':len(targets),'page':a.page,'page_size':a.page_size,
                    'chunks':[len(chunk) for chunk in chunks],'total_chunks':len(chunks),
                    'expected_seconds':round(DRY_RUN_FIXED_SECONDS+DRY_RUN_SECONDS_PER_TARGET*len(page),1),
                    'reader_note':SCOPE_ROLL_NOTE,'dry_run':bool(a.dry_run),
                    'rollback':bool(data['rollback'])}
            if a.dry_run:
                # The dry run is offline, so it cannot ask the endpoint which
                # integrations a host-issued revert record excludes; say so plainly
                # instead of letting the reader trust the target list (rev3 item 3.4).
                report['reverts_source']='local'
                report['dry_run_revert_note']=DRY_RUN_REVERT_NOTE
                warnings=warnings+[DRY_RUN_REVERT_NOTE]
            if warnings:report['warnings']=warnings
            # Check every target and every derived operation ID for the WHOLE release
            # before the first group request, so a conflict or a stale target in group
            # 3 of 4 writes nothing instead of completing groups 1 and 2. A planted
            # operation id is reported with the SAME JSON failure report as a group
            # failure, not only the plain error line (item 6.2).
            if not a.dry_run:
                op_index=_operation_index(rows)
                try:
                    _check_release_identity(data,op_index)
                    for chunk_payload in chunk_payloads:
                        _selected,plans,_issues,_evidence=_release_plan(rows,chunk_payload,journal=journal)
                        negative=_supersede_plan(rows,chunk_payload)+_supersede_scope_plan(rows,chunk_payload)
                        _check_release_operations(plans,negative,op_index,_evidence)
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
