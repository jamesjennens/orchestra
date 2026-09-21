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

def validate_request_record(record):
    if not isinstance(record,dict) or not {'schema_version','request_id','task','from_actor','to_actor','reason','requester','status','created_at','digest'}.issubset(record):
        raise ValueError('Invalid handoff request record')
    request_payload={key:record[key] for key in ('schema_version','request_id','task','from_actor','to_actor','reason')}
    request_payload['operation']='request'
    validate_request(request_payload)
    if record['requester']!=record['to_actor'] or record['status'] not in ('pending','accepted','declined','withdrawn','superseded'):
        raise ValueError('Invalid handoff request disposition')
    if not isinstance(record['created_at'],str) or datetime.fromisoformat(record['created_at'].replace('Z','+00:00')).tzinfo is None:
        raise ValueError('Invalid handoff request timestamp')
    if record['digest']!=content_hash(request_payload):
        raise ValueError('Handoff request integrity mismatch')
    disposition=record.get('disposition')
    if record['status']=='pending' and disposition is not None:
        raise ValueError('Pending handoff request cannot have a disposition')
    if record['status']!='pending' and disposition is None:
        raise ValueError('Disposed handoff request requires a disposition')
    if disposition is not None:
        if not isinstance(disposition,dict) or set(disposition)!={'kind','actor','reason','operation_id'} or disposition['kind'] not in ('accept','decline','withdraw','supersede') or not ACTOR.fullmatch(disposition['actor']) or not ID.fullmatch(disposition['operation_id']) or not isinstance(disposition['reason'],str) or not disposition['reason'].strip():
            raise ValueError('Invalid handoff disposition')
        expected={'accepted':'accept','declined':'decline','withdrawn':'withdraw','superseded':'supersede'}[record['status']]
        if disposition['kind']!=expected:
            raise ValueError('Handoff disposition does not match request status')

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

def request(path, actor, p):
    validate_request(p)
    if actor!=p['to_actor']:raise ValueError('Only the requested destination actor may submit a handoff request')
    folder=path/'.handoff-requests'
    if folder.is_symlink():raise ValueError('Handoff request journal must not be a symlink')
    folder.mkdir(exist_ok=True)
    file=folder/(content_hash({'request_id':p['request_id']})+'.json')
    if file.is_symlink() or file.with_suffix('.tmp').is_symlink():raise ValueError('Invalid handoff request path')
    digest=content_hash(p)
    record=json.loads(file.read_text(encoding='utf-8')) if file.exists() else None
    if record:
        validate_request_record(record)
        if record['digest']!=digest or record['requester']!=actor:raise ValueError('Handoff request ID reused with different content')
        return dict(record,reconciled=True)
    record={'schema_version':1,'request_id':p['request_id'],'task':p['task'],'from_actor':p['from_actor'],
            'to_actor':p['to_actor'],'reason':p['reason'],'requester':actor,'status':'pending',
            'created_at':datetime.now(timezone.utc).isoformat(),'digest':digest}
    validate_request_record(record);atomic(file,record)
    return dict(record,reconciled=False)

def disposition(path, actor, p, run, operator=False):
    keys={'schema_version','operation','operation_id','request_id','task','disposition','reason','supersedes'}
    if not isinstance(p,dict) or set(p)!=keys or p.get('schema_version')!=1 or p.get('operation')!='disposition':
        raise ValueError('Invalid handoff disposition payload')
    if not ID.fullmatch(p['operation_id']) or not UUID.fullmatch(p['request_id']) or not ID.fullmatch(p['task']):
        raise ValueError('Invalid handoff disposition identity')
    if p['disposition'] not in ('accept','decline','withdraw','supersede') or not isinstance(p['reason'],str) or not p['reason'].strip() or len(p['reason'])>1000:
        raise ValueError('Invalid handoff disposition')
    if p['supersedes'] is not None and not UUID.fullmatch(p['supersedes']):raise ValueError('Invalid superseded request')
    folder=path/'.handoff-requests';file=folder/(content_hash({'request_id':p['request_id']})+'.json')
    if not file.exists():raise ValueError('Unknown handoff request')
    record=json.loads(file.read_text(encoding='utf-8'));validate_request_record(record)
    if p['task']!=record['task']:raise ValueError('Handoff disposition task does not match stored request')
    if record.get('disposition'):
        old=record['disposition']
        if old['operation_id']==p['operation_id'] and old['kind']==p['disposition'] and old['actor']==actor and old['reason']==p['reason']:
            return dict(record,reconciled=True)
        raise ValueError('Handoff request already has a disposition')
    if p['disposition']=='accept' and _reconcile_completed_request(path, record, actor, p, run):
        return dict(json.loads(file.read_text(encoding='utf-8')), reconciled=True)
    rows=json.loads(run(['show',p['task'],'--json']))
    if not isinstance(rows,list) or len(rows)!=1 or rows[0].get('id')!=p['task']:raise ValueError('Expected exact task')
    current=rows[0].get('assignee')
    if current!=record['from_actor'] and not operator:
        if p['disposition']=='accept' and actor==record['requester'] and current==record['to_actor']:
            raise ValueError('Requester cannot accept their own handoff request')
        raise ValueError('Handoff disposition requires the current owner')
    if not operator and p['disposition'] in ('accept','decline') and actor!=current:
        raise ValueError('Handoff disposition requires the current owner')
    if p['disposition']=='accept' and actor==record['requester'] and not operator:
        raise ValueError('Requester cannot accept their own handoff request')
    allowed = {record['from_actor']} if p['disposition'] in ('accept','decline') else {record['to_actor']}
    if not operator and actor not in allowed:raise ValueError('Actor is not authorized; disposition requires the current owner or requested destination')
    if p['disposition']=='accept':
        handoff_payload={'schema_version':1,'operation_id':'handoff-'+p['request_id'],'task':record['task'],
                         'from_actor':record['from_actor'],'to_actor':record['to_actor'],
                         'reason':record['reason'],'approval':p['reason']}
        execute(path,actor,handoff_payload,run,operator=operator)
    record['status']={'accept':'accepted','decline':'declined','withdraw':'withdrawn','supersede':'superseded'}[p['disposition']]
    record['disposition']={'kind':p['disposition'],'actor':actor,'reason':p['reason'],'operation_id':p['operation_id']}
    validate_request_record(record);atomic(file,record)
    return dict(record,reconciled=False)

def _request_file(path, request_id):
    return path/'.handoff-requests'/(content_hash({'request_id':request_id})+'.json')

def _reconcile_completed_request(path, request_record, actor, disposition, run=None, operator=False):
    operation_id='handoff-'+request_record['request_id']
    receipt_file=path/'.handoffs'/(content_hash({'operation_id':operation_id})+'.json')
    if not receipt_file.exists():
        return False
    receipt=json.loads(receipt_file.read_text(encoding='utf-8'))
    validate_receipt(receipt)
    payload=receipt['identity']['payload']
    receipt_identity=receipt['identity']
    if not operator and receipt_identity['operator']:
        raise ValueError('Recovery authority does not match original handoff authority')
    if actor!=receipt_identity['initiator'] and not (operator and receipt_identity['operator']):
        raise ValueError('Recovery actor does not match original handoff authority')
    if receipt['status']=='pending' and run is not None:
        rows=json.loads(run(['show',request_record['task'],'--json']))
        if isinstance(rows,list) and len(rows)==1 and rows[0].get('assignee')==request_record['to_actor']:
            receipt['status']='complete'
            atomic(receipt_file,receipt)
    if receipt['status']!='complete' or payload['operation_id']!=operation_id:
        return False
    expected={key:request_record[key] for key in ('task','from_actor','to_actor','reason')}
    if any(payload[key]!=value for key,value in expected.items()):
        raise ValueError('Completed handoff receipt conflicts with request')
    if isinstance(disposition,dict):
        request_record['status']='accepted'
        request_record['disposition']={'kind':'accept','actor':actor,'reason':disposition['reason'],
                                        'operation_id':disposition['operation_id']}
    else:
        request_record['status']='accepted'
        request_record['disposition']={'kind':'accept','actor':actor,'reason':disposition,
                                        'operation_id':operation_id}
    validate_request_record(request_record)
    atomic(_request_file(path,request_record['request_id']),request_record)
    return True

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
    if record and record.get('status')=='complete':
        request_id=p['operation_id'][8:] if p['operation_id'].startswith('handoff-') else ''
        request_file=_request_file(path,request_id) if request_id else None
        if request_file and request_file.exists():
            request_record=json.loads(request_file.read_text(encoding='utf-8'))
            validate_request_record(request_record)
            if request_record['status']=='pending':
                _reconcile_completed_request(path,request_record,actor,p['approval'],run,operator)
        return {'task':p['task'],'from_actor':p['from_actor'],'to_actor':p['to_actor'],'current_owner':current.get('assignee'),'reconciled':True,'operation_id':p['operation_id']}
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
    request_id=p['operation_id'][8:] if p['operation_id'].startswith('handoff-') else ''
    request_file=_request_file(path,request_id) if request_id else None
    if request_file and request_file.exists():
        request_record=json.loads(request_file.read_text(encoding='utf-8'))
        validate_request_record(request_record)
        if request_record['status']=='pending':
            _reconcile_completed_request(path,request_record,actor,p['approval'],run,operator)
    return {'task':p['task'],'from_actor':p['from_actor'],'to_actor':p['to_actor'],'current_owner':p['to_actor'],'operation_id':p['operation_id'],'reconciled':False}
