"""Project-locked, durable actor allocation. Attribution, not authentication."""
import argparse
import json
import re
import uuid
from datetime import datetime, timezone
from coordination import atomic
from requirements import content_hash

UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
#: An actor name as the endpoint accepts one (endpoint.execute's own rule), so every name a
#: request may carry can be an entry of the registry's owners map.
ACTOR = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}')
#: A principal (kittrial-5bb.194, rule 2): a lane, spelled ``lane:NAME`` or ``person:NAME``
#: (the coordinator decision of 2026-10-08; ``person:NAME`` stays for compatibility and the
#: two prefixes are different principals). One token with no space or control character,
#: because the value is an argument of the ``authorized_keys`` command line and that line is
#: split on spaces. The name rules are the same under both prefixes and case is kept.
PRINCIPAL = re.compile(r"(?:lane|person):[A-Za-z0-9][A-Za-z0-9_.-]{0,94}")

def valid_principal(value, label='--principal'):
    """Return ``value`` if it is a principal name, else raise naming the form."""
    if not isinstance(value, str) or not PRINCIPAL.fullmatch(value):
        raise ValueError('%s must be a principal name of the form lane:NAME or person:NAME (one token: '
                         'letters, digits, dot, underscore, dash; no space). Refused %r' % (label, value))
    return value


def valid_actor(value, label='--actor'):
    """Return ``value`` if it is an actor name the endpoint may carry, else raise."""
    if not isinstance(value, str) or not ACTOR.fullmatch(value):
        raise ValueError('%s must be a short actor name (letters, digits, dot, underscore, at, slash, '
                         'dash, up to 96 characters). Refused %r' % (label, value))
    return value


def read_registry(path):
    """The project's session registry, validated; an empty one when no file exists.

    A registry that exists but cannot be read as one raises ``ValueError``: whoever asks a
    question of it must not be told "none" by a damaged file (kittrial-5bb.184).
    """
    from pathlib import Path
    file = Path(path) / '.sessions.json'
    if file.is_symlink():
        raise ValueError('Session registry paths must not be symlinks')
    if not file.exists():
        return {'schema_version': 1, 'records': {}}
    try:
        return validate(json.loads(file.read_text(encoding='utf-8')))
    except (OSError, UnicodeDecodeError, RecursionError, json.JSONDecodeError) as error:
        raise ValueError('The session registry cannot be read: it is damaged, unreadable or not JSON; '
                         'nothing was changed') from None


def registered_actors(path):
    """The session actors registered in the project at ``path``. Reads the registry; writes nothing."""
    return sorted(record['actor'] for record in read_registry(path)['records'].values())


def owner_map(data):
    """The registry's actor -> principal map; ``{}`` when it has none."""
    value = data.get('owners', {}) if isinstance(data, dict) else {}
    return value if isinstance(value, dict) else {}


def owners(path):
    """The project's actor -> principal map; ``{}`` when the registry or the map is absent."""
    return dict(owner_map(read_registry(path)))


def validate(data):
    if not isinstance(data,dict) or not {'schema_version','records'}.issubset(data) or set(data)-{'schema_version','records','resumes','runs','owners'} or type(data['schema_version']) is not int or data['schema_version']!=1 or not isinstance(data['records'],dict):
        raise ValueError('Invalid session registry')
    actors=set()
    for key,r in data['records'].items():
        if not isinstance(key,str) or not UUID.fullmatch(key) or not isinstance(r,dict) or set(r)!={'request_id','actor','name','created_at'}:
            raise ValueError('Invalid session record')
        if r['request_id']!=key or not isinstance(r['actor'],str) or not r['actor'].startswith('session-') or not UUID.fullmatch(r['actor'][8:]) or r['actor'] in actors:
            raise ValueError('Duplicate or invalid session actor')
        check_name(r['name'])
        if not isinstance(r['created_at'],str):raise ValueError('Invalid session timestamp')
        moment=datetime.fromisoformat(r['created_at'].replace('Z','+00:00'))
        if moment.tzinfo is None:raise ValueError('Session timestamp needs timezone')
        actors.add(r['actor'])
    # The owners map (kittrial-5bb.194, rule 2 of the coordinators design). An actor with an
    # entry belongs to that principal; an actor with none has no principal and is its own
    # party, compared by name (the coordinator note of 2026-10-07 on kittrial-5bb.178). A key
    # must be a name the endpoint may carry, not necessarily a registered session: the whole
    # point of the adoption command is to name actors that already exist (legacy actors).
    owner_records=data.get('owners',{})
    if not isinstance(owner_records,dict):raise ValueError('Invalid session owner map')
    for actor,principal in owner_records.items():
        if (not isinstance(actor,str) or not ACTOR.fullmatch(actor)
                or not isinstance(principal,str) or not PRINCIPAL.fullmatch(principal)):
            raise ValueError('Invalid session owner map entry')
    resumes=data.get('resumes',{})
    if not isinstance(resumes,dict):raise ValueError('Invalid session resume records')
    for key,event in resumes.items():
        if not isinstance(key,str) or not UUID.fullmatch(key) or not isinstance(event,dict) or set(event)!={'request_id','actor','timestamp'}:
            raise ValueError('Invalid session resume record')
        if event['request_id']!=key or not isinstance(event['actor'],str) or event['actor'] not in actors:
            raise ValueError('Resume actor must have a session registration')
        if not isinstance(event['timestamp'],str):raise ValueError('Invalid resume timestamp')
        moment=datetime.fromisoformat(event['timestamp'].replace('Z','+00:00'))
        if moment.tzinfo is None:raise ValueError('Resume timestamp needs timezone')
    runs=data.get('runs',{})
    if not isinstance(runs,dict):raise ValueError('Invalid session run records')
    for key,run in runs.items():
        if not isinstance(key,str) or not UUID.fullmatch(key) or not isinstance(run,dict) or set(run)!={'run_id','actor','task','status','started_at','last_heartbeat','ended_at','events'}:
            raise ValueError('Invalid session run record')
        if run['run_id']!=key or run['actor'] not in actors or not isinstance(run['task'],str) or not run['task'] or run['status'] not in ('running','succeeded','failed','cancelled'):
            raise ValueError('Invalid session run identity or status')
        for field in ('started_at','last_heartbeat'):
            if not isinstance(run[field],str):raise ValueError('Invalid run timestamp')
            moment=datetime.fromisoformat(run[field].replace('Z','+00:00'))
            if moment.tzinfo is None:raise ValueError('Run timestamp needs timezone')
        if run['ended_at'] is not None:
            if not isinstance(run['ended_at'],str):raise ValueError('Invalid run end timestamp')
            moment=datetime.fromisoformat(run['ended_at'].replace('Z','+00:00'))
            if moment.tzinfo is None:raise ValueError('Run end timestamp needs timezone')
        if not isinstance(run['events'],dict):raise ValueError('Invalid run events')
        for event_id,event in run['events'].items():
            if not isinstance(event_id,str) or not UUID.fullmatch(event_id) or not isinstance(event,dict) or set(event)!={'event_id','kind','actor','timestamp','payload_hash'}:
                raise ValueError('Invalid run event')
            if event['event_id']!=event_id or event['kind'] not in ('start','heartbeat','end') or event['actor']!=run['actor'] or not isinstance(event['timestamp'],str) or not isinstance(event['payload_hash'],str):
                raise ValueError('Invalid run event identity')
            moment=datetime.fromisoformat(event['timestamp'].replace('Z','+00:00'))
            if moment.tzinfo is None:raise ValueError('Run event timestamp needs timezone')
    return data

def check_name(name):
    if not isinstance(name,str) or not name.strip() or len(name)>80 or not name.isprintable():
        raise ValueError('Use a nonempty readable session name up to 80 printable characters')

def used_actors(value):
    result=set()
    if isinstance(value,dict):
        for k,v in value.items():
            if k in ('actor','author','assignee','created_by') and isinstance(v,str):result.add(v)
            result.update(used_actors(v))
    elif isinstance(value,list):
        for v in value:result.update(used_actors(v))
    return result

class Parser(argparse.ArgumentParser):
    def error(self,message):raise ValueError(message)

def execute(path, project, args, export, *, actor=None, principal=None):
    """Caller holds .coordination.lock, including native writes and backup.

    ``principal`` is the principal of the key that made the request, or None for a caller
    with no bound key (kittrial-5bb.194). It is used in one place only: registering a new
    session records the new actor under it. Nothing else about an operation changes.
    """
    parser=Parser(add_help=False)
    sub=parser.add_subparsers(dest='operation',required=True)
    p=sub.add_parser('register',add_help=False);p.add_argument('--name',required=True);p.add_argument('--request-id',required=True)
    p=sub.add_parser('show',add_help=False);p.add_argument('actor')
    p=sub.add_parser('resume',add_help=False);p.add_argument('--request-id',required=True)
    p=sub.add_parser('run',add_help=False)
    p.add_argument('kind',choices=('start','heartbeat','end','status'))
    p.add_argument('--run-id',required=True);p.add_argument('--event-id',default='')
    p.add_argument('--task',default='');p.add_argument('--status',choices=('succeeded','failed','cancelled'))
    p.add_argument('--limit',type=int,default=20);p.add_argument('--offset',type=int,default=0)
    a=parser.parse_args(args)
    file=path/'.sessions.json'
    if file.is_symlink() or file.with_suffix('.tmp').is_symlink():raise ValueError('Session registry paths must not be symlinks')
    data=read_registry(path)
    if a.operation=='show':
        found=[r for r in data['records'].values() if r['actor']==a.actor]
        if not found:raise ValueError('Actor not registered in this project; legacy actors have no registration record')
        # The principal the actor belongs to. The key is omitted when the registry has no
        # owners map at all (an installation that configures nothing): that answer is what
        # this kit gave before rule 2, so "unchanged without configuration" is literally true.
        answer=dict(project=project,session=found[0])
        if owner_map(data):answer['principal']=owner_map(data).get(a.actor)
        return answer
    if a.operation=='resume':
        if not UUID.fullmatch(a.request_id):raise ValueError('request-id must be a lowercase UUID')
        found=[r for r in data['records'].values() if r['actor']==actor]
        if not found:raise ValueError('Resume requires a registered actor in this project; legacy actors may use onboard, or request an explicit authorized handoff')
        # The standing guidance channel (kittrial-5bb.99): the resume output is the
        # worker's first server read each run, so it carries the current guidance
        # version and whether this actor has acknowledged it.
        from guidance import brief_block
        block=brief_block(path,actor)
        resumes=data.setdefault('resumes',{})
        old=resumes.get(a.request_id)
        if old:
            if old['actor']!=actor:raise ValueError('Resume request-id already used by a different actor')
            return dict(project=project,session=found[0],resume=old,reconciled=True,guidance=block)
        event=dict(request_id=a.request_id,actor=actor,timestamp=datetime.now(timezone.utc).isoformat())
        resumes[a.request_id]=event;validate(data);atomic(file,data)
        return dict(project=project,session=found[0],resume=event,reconciled=False,guidance=block)
    if a.operation=='run':
        if not UUID.fullmatch(a.run_id) or (a.kind!='status' and not UUID.fullmatch(a.event_id)):
            raise ValueError('run-id and event-id must be lowercase UUIDs')
        found=[r for r in data['records'].values() if r['actor']==actor]
        if not found:raise ValueError('Run requires a registered actor in this project')
        runs=data.setdefault('runs',{})
        run=runs.get(a.run_id)
        if a.kind=='status':
            if run is None:raise ValueError('Unknown run')
            if run['actor']!=actor:raise ValueError('Run belongs to a different actor')
            if not 1<=a.limit<=100 or a.offset<0:raise ValueError('Invalid run status page')
            events=sorted(run['events'].values(),key=lambda event:(event['timestamp'],event['event_id']))
            page=events[a.offset:a.offset+a.limit]
            bounded=dict(run,events={event['event_id']:event for event in page},
                         event_total=len(events),
                         next_offset=a.offset+a.limit if a.offset+a.limit<len(events) else None)
            return dict(project=project,run=bounded,reconciled=False)
        now=datetime.now(timezone.utc).isoformat()
        existing=runs.get(a.run_id)
        payload={'kind':a.kind,'run_id':a.run_id,'event_id':a.event_id,'task':a.task,'status':a.status or ''}
        digest=content_hash(payload)
        if existing is None:
            if a.kind!='start' or not a.task:raise ValueError('Run start requires a task')
            if a.status is not None:raise ValueError('Run start cannot set a terminal status')
            existing={'run_id':a.run_id,'actor':actor,'task':a.task,'status':'running',
                      'started_at':now,'last_heartbeat':now,'ended_at':None,'events':{}}
            runs[a.run_id]=existing
            event={'event_id':a.event_id,'kind':'start','actor':actor,'timestamp':now,'payload_hash':digest}
            existing['events'][a.event_id]=event
            validate(data);atomic(file,data)
            return dict(project=project,run=existing,event=event,reconciled=False)
        elif existing['actor']!=actor:
            raise ValueError('Run belongs to a different actor')
        old=existing['events'].get(a.event_id)
        if old:
            if old['payload_hash']!=digest:raise ValueError('Run event ID reused with different content')
            return dict(project=project,run=existing,event=old,reconciled=True)
        if a.kind=='start':
            raise ValueError('Run already exists; retry the exact start event')
        if existing['status']!='running':raise ValueError('Run is already ended')
        if a.kind=='heartbeat':
            if a.task:raise ValueError('Heartbeat cannot change the bound task')
            existing['last_heartbeat']=now
        else:
            if a.task:raise ValueError('End cannot change the bound task')
            if a.status is None:raise ValueError('Run end requires a status')
            existing['status']=a.status;existing['ended_at']=now
        event={'event_id':a.event_id,'kind':a.kind,'actor':actor,'timestamp':now,'payload_hash':digest}
        existing['events'][a.event_id]=event
        validate(data);atomic(file,data)
        return dict(project=project,run=existing,event=event,reconciled=False)
    check_name(a.name)
    if not UUID.fullmatch(a.request_id):raise ValueError('request-id must be a lowercase UUID')
    old=data['records'].get(a.request_id)
    if old:
        if old['name']!=a.name:raise ValueError('Registration request-id already used with a different name')
        if not owner_map(data):
            return dict(project=project,session=old,reconciled=True)
        return dict(project=project,session=old,reconciled=True,principal=owner_map(data).get(old['actor']))
    rows=[json.loads(line) for line in export().splitlines() if line.strip()]
    occupied=used_actors(rows)|{r['actor'] for r in data['records'].values()}
    for _ in range(20):
        actor='session-'+str(uuid.uuid4())
        if actor not in occupied:break
    else:raise ValueError('Could not allocate an unused actor; no registration written')
    record=dict(request_id=a.request_id,actor=actor,name=a.name,created_at=datetime.now(timezone.utc).isoformat())
    data['records'][a.request_id]=record
    # Rule 2: registering a new session under a bound key makes the new actor that
    # principal's. Without a principal (an unbound key, or a local caller) no owners map is
    # written at all, so an installation that configures nothing writes the registry exactly
    # as before and an older kit can still read it (the downgrade limit).
    if principal is not None:
        assigned=dict(owner_map(data));assigned[actor]=valid_principal(principal)
        data['owners']=assigned
    validate(data);atomic(file,data)
    answer=dict(project=project,session=record,reconciled=False)
    if owner_map(data):answer['principal']=owner_map(data).get(actor)
    return answer
