"""Explicit owner/operator transfer, with a durable recoverable operation journal."""
import json
import re
from datetime import datetime, timezone
from coordination import atomic
from requirements import canonical_bytes, content_hash

ACTOR=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}')
ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}')
UUID=re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')

def validate_request(p):
    keys={'schema_version','operation','request_id','task','from_actor','to_actor','reason'}
    if not isinstance(p,dict) or set(p)!=keys or p.get('schema_version')!=1 or p.get('operation')!='request':
        raise ValueError('Invalid handoff request payload')
    if not UUID.fullmatch(p['request_id']) or not ID.fullmatch(p['task']):
        raise ValueError('Invalid handoff request identity')
    for key in ('from_actor','to_actor'):
        if not isinstance(p[key],str) or not ACTOR.fullmatch(p[key]):raise ValueError('Invalid '+key)
    if p['from_actor']==p['to_actor']:raise ValueError('Requester must be distinct from current owner')
    if not isinstance(p['reason'],str) or not p['reason'].strip() or len(p['reason'])>1000:
        raise ValueError('Handoff request requires a bounded reason')

def request(path, actor, p):
    validate_request(p)
    if actor!=p['to_actor']:raise ValueError('Only the requested destination actor may submit a handoff request')
    folder=path/'.handoff-requests'
    if folder.is_symlink():raise ValueError('Handoff request journal must not be a symlink')
    folder.mkdir(exist_ok=True)
    file=folder/(content_hash({'request_id':p['request_id']})+'.json')
    if file.is_symlink():raise ValueError('Invalid handoff request path')
    digest=content_hash(p)
    record=json.loads(file.read_text(encoding='utf-8')) if file.exists() else None
    if record:
        if record.get('digest')!=digest or record.get('requester')!=actor:
            raise ValueError('Handoff request ID reused with different content')
        return dict(record,reconciled=True)
    record={'schema_version':1,'request_id':p['request_id'],'task':p['task'],'from_actor':p['from_actor'],
            'to_actor':p['to_actor'],'reason':p['reason'],'requester':actor,'status':'pending',
            'created_at':datetime.now(timezone.utc).isoformat(),'digest':digest}
    atomic(file,record)
    return dict(record,reconciled=False)

def validate(p):
    keys={'schema_version','operation_id','task','from_actor','to_actor','reason','approval'}
    if not isinstance(p,dict) or set(p)!=keys or type(p['schema_version']) is not int or p['schema_version']!=1:raise ValueError('Invalid handoff payload')
    for key in ('operation_id','task'):
        if not isinstance(p[key],str) or not ID.fullmatch(p[key]):raise ValueError('Invalid '+key)
    for key in ('from_actor','to_actor'):
        if not isinstance(p[key],str) or not ACTOR.fullmatch(p[key]):raise ValueError('Invalid '+key)
    if p['from_actor']==p['to_actor']:raise ValueError('Use session resume to keep the same owner')
    for key in ('reason','approval'):
        if not isinstance(p[key],str) or not p[key].strip() or len(p[key])>1000:raise ValueError('Handoff requires bounded reason and approval evidence')

def validate_receipt(record):
    if not isinstance(record,dict) or set(record)!={'digest','status','identity'} or record['status'] not in ('pending','complete'):raise ValueError('Invalid handoff receipt')
    identity=record['identity']
    if not isinstance(identity,dict) or set(identity)!={'payload','initiator','operator'} or type(identity['operator']) is not bool:raise ValueError('Invalid handoff identity')
    validate(identity['payload'])
    if not isinstance(identity['initiator'],str) or not ACTOR.fullmatch(identity['initiator']):raise ValueError('Invalid handoff initiator')
    if not identity['operator'] and identity['initiator']!=identity['payload']['from_actor']:raise ValueError('Invalid handoff authority')
    if record['digest']!=content_hash(identity):raise ValueError('Handoff receipt integrity mismatch')

def execute(path, actor, p, run, operator=False):
    """Caller holds project lock. operator is supplied only by admin CLI."""
    validate(p)
    if not ACTOR.fullmatch(actor):raise ValueError('Invalid initiating actor')
    if not operator and actor!=p['from_actor']:raise ValueError('Only the current owner may hand off; authorized coordinator recovery uses admin handoff with approval evidence')
    folder=path/'.handoffs'
    if folder.is_symlink():raise ValueError('Handoff journal must not be a symlink')
    folder.mkdir(exist_ok=True)
    file=folder/(content_hash({'operation_id':p['operation_id']})+'.json')
    if file.is_symlink() or file.with_suffix('.tmp').is_symlink():raise ValueError('Invalid journal path')
    identity={'payload':p,'initiator':actor,'operator':operator};digest=content_hash(identity)
    record=json.loads(file.read_text(encoding='utf-8')) if file.exists() else None
    if file.exists():validate_receipt(record)
    if record and record.get('digest')!=digest:raise ValueError('Handoff operation ID reused with different content or authority')
    def issue():
        rows=json.loads(run(['show',p['task'],'--json']))
        if isinstance(rows,dict):rows=[rows]
        if not isinstance(rows,list) or len(rows)!=1 or rows[0].get('id')!=p['task']:raise ValueError('Expected exact task')
        return rows[0]
    current=issue()
    if current.get('issue_type') not in ('task','bug','feature','chore','epic','decision'):raise ValueError('Handoff is only for work issues, not gates/events')
    if record and record.get('status')=='complete':return {'task':p['task'],'from_actor':p['from_actor'],'to_actor':p['to_actor'],'current_owner':current.get('assignee'),'reconciled':True,'operation_id':p['operation_id']}
    if current.get('assignee') not in ((p['from_actor'],p['to_actor']) if record else (p['from_actor'],)):
        raise ValueError('Owner changed; reconcile before issuing a new authorized handoff')
    if record is None:
        record={'digest':digest,'status':'pending','identity':identity};atomic(file,record)
    intent='Kind: task-handoff-v1\n'+canonical_bytes(identity).decode()
    def comment(text):
        # Fetch comments directly: native show may not include them in every version.
        comments=json.loads(run(['comments',p['task'],'--json'])) or []
        if not any(c.get('text')==text and c.get('author')==actor for c in comments):
            run(['comments','add',p['task'],text,'--json'])
    comment(intent)
    current=issue()
    if current.get('assignee')==p['from_actor']:
        run(['update',p['task'],'--assignee',p['to_actor'],'--json'])
    elif current.get('assignee')!=p['to_actor']:raise ValueError('Owner changed during handoff')
    if issue().get('assignee')!=p['to_actor']:raise ValueError('Handoff outcome uncertain; retry same operation after inspection')
    comment('Kind: task-handoff-complete-v1\n'+canonical_bytes(identity).decode())
    record['status']='complete';atomic(file,record)
    return {'task':p['task'],'from_actor':p['from_actor'],'to_actor':p['to_actor'],'current_owner':p['to_actor'],'operation_id':p['operation_id'],'reconciled':False}
