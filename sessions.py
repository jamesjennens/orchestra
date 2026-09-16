"""Project-locked, durable actor allocation. Attribution, not authentication."""
import argparse
import json
import re
import uuid
from datetime import datetime, timezone
from coordination import atomic

UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')

def validate(data):
    if not isinstance(data,dict) or set(data)!={'schema_version','records'} or type(data['schema_version']) is not int or data['schema_version']!=1 or not isinstance(data['records'],dict):
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

def execute(path, project, args, export):
    """Caller holds .coordination.lock, including native writes and backup."""
    parser=Parser(add_help=False)
    sub=parser.add_subparsers(dest='operation',required=True)
    p=sub.add_parser('register',add_help=False);p.add_argument('--name',required=True);p.add_argument('--request-id',required=True)
    p=sub.add_parser('show',add_help=False);p.add_argument('actor')
    a=parser.parse_args(args)
    file=path/'.sessions.json'
    if file.is_symlink() or file.with_suffix('.tmp').is_symlink():raise ValueError('Session registry paths must not be symlinks')
    data=validate(json.loads(file.read_text(encoding='utf-8'))) if file.exists() else {'schema_version':1,'records':{}}
    if a.operation=='show':
        found=[r for r in data['records'].values() if r['actor']==a.actor]
        if not found:raise ValueError('Actor not registered in this project; legacy actors have no registration record')
        return dict(project=project,session=found[0])
    check_name(a.name)
    if not UUID.fullmatch(a.request_id):raise ValueError('request-id must be a lowercase UUID')
    old=data['records'].get(a.request_id)
    if old:
        if old['name']!=a.name:raise ValueError('Registration request-id already used with a different name')
        return dict(project=project,session=old,reconciled=True)
    rows=[json.loads(line) for line in export().splitlines() if line.strip()]
    occupied=used_actors(rows)|{r['actor'] for r in data['records'].values()}
    for _ in range(20):
        actor='session-'+str(uuid.uuid4())
        if actor not in occupied:break
    else:raise ValueError('Could not allocate an unused actor; no registration written')
    record=dict(request_id=a.request_id,actor=actor,name=a.name,created_at=datetime.now(timezone.utc).isoformat())
    data['records'][a.request_id]=record;validate(data);atomic(file,data)
    return dict(project=project,session=record,reconciled=False)
