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

def _task_owner(run, task, actor):
    if run is None:
        return
    rows=json.loads(run(['show',task,'--json']))
    if isinstance(rows,dict): rows=[rows]
    if not isinstance(rows,list) or len(rows)!=1 or rows[0].get('id')!=task:
        raise ValueError('Expected exact handoff task')
    if rows[0].get('assignee')!=actor:
        raise ValueError('Handoff request is stale; task is not owned by the requested source actor')

def request(path, actor, p, run=None):
    validate_request(p)
    if actor!=p['to_actor']:raise ValueError('Only the requested destination actor may submit a handoff request')
    _task_owner(run,p['task'],p['from_actor'])
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
            'created_at':datetime.now(timezone.utc).isoformat(),'digest':digest,'dispositions':[]}
    atomic(file,record)
    return dict(record,reconciled=False)

def validate_disposition(p):
    keys={'schema_version','operation','request_id','task','disposition','reason'}
    if not isinstance(p,dict) or set(p)!=keys or p.get('schema_version')!=1 or p.get('operation')!='disposition':
        raise ValueError('Invalid handoff disposition payload')
    if not UUID.fullmatch(p.get('request_id','')) or not ID.fullmatch(p.get('task','')):
        raise ValueError('Invalid handoff disposition identity')
    if p.get('disposition') not in ('accept','decline','withdraw','supersede'):
        raise ValueError('Invalid handoff disposition')
    if not isinstance(p.get('reason'),str) or not p['reason'].strip() or len(p['reason'])>1000:
        raise ValueError('Handoff disposition requires a bounded reason')

def validate_request_record(record):
    if not isinstance(record,dict) or set(record)!={
        'schema_version','request_id','task','from_actor','to_actor','reason',
        'requester','status','created_at','digest','dispositions'}:
        raise ValueError('Invalid handoff request record')
    validate_request({'schema_version':record['schema_version'],'operation':'request',
                      'request_id':record['request_id'],'task':record['task'],
                      'from_actor':record['from_actor'],'to_actor':record['to_actor'],
                      'reason':record['reason']})
    if record['requester']!=record['to_actor'] or not ACTOR.fullmatch(record['requester']):
        raise ValueError('Invalid handoff requester')
    if record['status'] not in ('pending','accepted','declined','withdrawn','superseded'):
        raise ValueError('Invalid handoff request status')
    if not isinstance(record['created_at'],str):
        raise ValueError('Invalid handoff request timestamp')
    try:
        moment=datetime.fromisoformat(record['created_at'].replace('Z','+00:00'))
    except ValueError:
        raise ValueError('Invalid handoff request timestamp') from None
    if moment.tzinfo is None: raise ValueError('Handoff request timestamp needs timezone')
    if record['digest']!=content_hash({'schema_version':1,'operation':'request',
                                       'request_id':record['request_id'],'task':record['task'],
                                       'from_actor':record['from_actor'],'to_actor':record['to_actor'],
                                       'reason':record['reason']}):
        raise ValueError('Handoff request integrity mismatch')
    if not isinstance(record['dispositions'],list):
        raise ValueError('Invalid handoff dispositions')
    for item in record['dispositions']:
        if not isinstance(item,dict) or set(item)!={'actor','disposition','reason','timestamp','digest'}:
            raise ValueError('Invalid handoff disposition record')
        if not ACTOR.fullmatch(item['actor']) or item['disposition'] not in ('accept','decline','withdraw','supersede') or not isinstance(item['reason'],str) or not item['reason'].strip() or not isinstance(item['timestamp'],str) or not re.fullmatch(r'[a-f0-9]{64}',item['digest']):
            raise ValueError('Invalid handoff disposition record')
        try:
            moment=datetime.fromisoformat(item['timestamp'].replace('Z','+00:00'))
        except ValueError:
            raise ValueError('Invalid handoff disposition timestamp') from None
        if moment.tzinfo is None: raise ValueError('Handoff disposition timestamp needs timezone')

def dispose(path, actor, p, run=None):
    validate_disposition(p)
    folder=path/'.handoff-requests'
    file=folder/(content_hash({'request_id':p['request_id']})+'.json')
    if not file.exists() or file.is_symlink(): raise ValueError('Unknown handoff request')
    record=json.loads(file.read_text(encoding='utf-8'))
    validate_request_record(record)
    _task_owner(run,p['task'],record['from_actor'])
    allowed={'accept':record['to_actor'],'decline':record['to_actor'],
             'withdraw':record['from_actor'],'supersede':record['from_actor']}
    if actor!=allowed[p['disposition']]:
        raise ValueError('Actor is not authorized for this handoff disposition')
    if p['disposition']=='accept' and actor==record['from_actor']:
        raise ValueError('Requester cannot self-approve a handoff')
    entry={'actor':actor,'disposition':p['disposition'],'reason':p['reason'],
           'timestamp':datetime.now(timezone.utc).isoformat(),
           'digest':content_hash({'actor':actor,'payload':p})}
    dispositions=record.setdefault('dispositions',[])
    if not isinstance(dispositions,list): raise ValueError('Invalid handoff dispositions')
    for old in dispositions:
        if old.get('digest')==entry['digest']: return dict(record,reconciled=True)
    if dispositions and p['disposition'] not in ('supersede',):
        raise ValueError('Handoff already has a disposition; supersede it explicitly')
    dispositions.append(entry)
    record['status']='withdrawn' if p['disposition']=='withdraw' else ('declined' if p['disposition']=='decline' else 'accepted' if p['disposition']=='accept' else 'superseded')
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
