"""Compact task state, explicit checkpoints and lossless snapshot history pages."""
import argparse
import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from activity import build_entries, parse_moment, utc_text
from lifecycle import project_facts
from requirements import canonical_bytes, content_hash

PREFIX='Kind: task-checkpoint-v1\n'
KINDS={'blocker','question','decision','correction','dependency'}

def token(data):return base64.urlsafe_b64encode(canonical_bytes(data)).decode().rstrip('=')

def untoken(value):
    try:
        if not isinstance(value,str) or len(value)>4096 or not re.fullmatch(r'[A-Za-z0-9_-]+',value):raise ValueError()
        return json.loads(base64.urlsafe_b64decode(value+'='*(-len(value)%4)))
    except (ValueError,TypeError,UnicodeError):raise ValueError('Invalid cursor') from None

def identity(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}',value):raise ValueError('Invalid task/item ID')
    return value

def task_row(rows,task):
    identity(task)
    found=[r for r in rows if r.get('id')==task]
    if len(found)!=1:raise ValueError('Task missing or duplicated')
    return found[0]

def snapshot(rows,project,task,exclude=None):
    issue=task_row(rows,task)
    selected=[dict(issue,comments=[c for c in (issue.get('comments') or []) if str(c['id'])!=exclude])]
    selected.extend(r for r in rows if r.get('issue_type')=='event' and r['id']!=task and any(d.get('type')=='parent-child' and d.get('depends_on_id')==task for d in (r.get('dependencies') or [])))
    entries=build_entries(selected)
    # Titles live in task state; history carries immutable entry content, not a
    # repeated unbounded task description/title on every comment.
    for entry in entries:entry.pop('issue_title',None)
    entries.sort(key=lambda e:(parse_moment(e['timestamp']),e['entry_id']))
    if len({e['entry_id'] for e in entries})!=len(entries):raise ValueError('Duplicate history entry IDs')
    state={k:issue.get(k) for k in ('title','description','acceptance_criteria','design','notes','status','assignee','labels','dependencies')}
    for key in ('labels','dependencies'):state[key]=sorted(state[key] or [],key=canonical_bytes)
    return {'project':project,'task':task,'state_sha256':content_hash(state),'entries':entries}

def activity_cursor(data):return token({'v':1,'kind':'activity','project':data['project'],'task':data['task'],'sha256':content_hash(data)})

def text(value,label,limit,empty=False):
    if not isinstance(value,str) or len(value)>limit or (not empty and not value.strip()):raise ValueError(f'{label}: expected text up to {limit} characters')

def validate_checkpoint(p,task):
    fields={'schema_version','task','previous','activity_cursor','source_commit','branch','intent','acceptance','summary','next_action','open_items','resolved'}
    if not isinstance(p,dict) or set(p)!=fields or type(p['schema_version']) is not int or p['schema_version']!=1:raise ValueError('Invalid checkpoint fields/version')
    if p['task']!=task:raise ValueError('Checkpoint task mismatch')
    if p['previous'] is not None:identity(p['previous'])
    for key,limit in [('source_commit',128),('branch',200),('intent',600),('acceptance',1000),('summary',1000),('next_action',600)]:text(p[key],key,limit,empty=key in ('source_commit','branch'))
    cursor=untoken(p['activity_cursor'])
    if not isinstance(cursor,dict) or cursor.get('kind')!='activity' or cursor.get('task')!=task:raise ValueError('Expected task activity cursor from brief/history')
    for field in ('open_items','resolved'):
        items=p[field]
        if not isinstance(items,list) or len(items)>100:raise ValueError('Checkpoint item lists limited to 100')
        ids=[]
        for item in items:
            keys={'id','kind','text','source'} if field=='open_items' else {'id','reason','evidence'}
            if not isinstance(item,dict) or set(item)!=keys:raise ValueError('Invalid '+field+' item')
            ids.append(identity(item['id']))
            if field=='open_items':
                if item['kind'] not in KINDS:raise ValueError('Invalid unresolved kind')
                text(item['text'],'item text',400);text(item['source'],'source',240)
            else:text(item['reason'],'resolution reason',400);text(item['evidence'],'resolution evidence',240)
        if len(set(ids))!=len(ids):raise ValueError('Duplicate item IDs')
    if len(canonical_bytes(p))>80000:raise ValueError('Checkpoint exceeds 80 KB')

def transition(previous,current):
    old={x['id'] for x in previous['open_items']} if previous else set()
    now={x['id'] for x in current['open_items']}
    resolved={x['id'] for x in current['resolved']}
    if old-now!=resolved:raise ValueError('Carry every unresolved item forward or explicitly resolve/supersede it with reason and evidence')
    old_items={x['id']:x for x in previous['open_items']} if previous else {}
    if any(item['id'] in old_items and item!=old_items[item['id']] for item in current['open_items']):raise ValueError('Carry unresolved items unchanged; explicitly resolve/supersede changed items with new IDs')

def checkpoints(issue):
    records={};invalid=[]
    for comment in issue.get('comments') or []:
        body=comment.get('text','')
        if not body.startswith(PREFIX):continue
        try:
            p=json.loads(body[len(PREFIX):]);validate_checkpoint(p,issue['id'])
            cid=str(comment['id'])
            if cid in records:raise ValueError('Duplicate checkpoint comment')
            records[cid]=(p,comment)
        except (ValueError,TypeError,KeyError):invalid.append(str(comment.get('id')))
    current=None;parent=None;visited=set()
    while True:
        children=[(cid,p,c) for cid,(p,c) in records.items() if p['previous']==parent]
        if not children:break
        if len(children)!=1:raise ValueError('Conflicting checkpoint branches; reconcile history before briefing')
        cid,p,c=children[0]
        if cid in visited:raise ValueError('Checkpoint cycle')
        transition(current[0] if current else None,p)
        current=(p,c);parent=cid;visited.add(cid)
    if len(visited)!=len(records):raise ValueError('Unlinked checkpoint history; reconcile missing/conflicting revisions')
    return current,invalid

def clip(value,limit):
    value=str(value or '')
    return {'text':value[:limit],'omitted_chars':max(0,len(value)-limit)}

def brief(rows,project,task,offset=0,limit=5):
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=10:raise ValueError('Invalid unresolved-item page')
    issue=task_row(rows,task);current,invalid=checkpoints(issue)
    if issue.get('issue_type')=='event':raise ValueError('Use history/show for an event; brief requires a task or job')
    data=snapshot(rows,project,task);p,c=current if current else (None,None)
    facts=next(r for r in project_facts(rows) if r['id']==task)
    items=p['open_items'] if p else []
    if offset>len(items):raise ValueError('Unresolved-item offset exceeds total')
    deps=[d for d in (issue.get('dependencies') or []) if d.get('type')!='parent-child']
    return {'task':task,'title':clip(issue.get('title'),200),'owner':clip(issue.get('assignee') or 'unassigned',96),'status':issue.get('status'),
            'activity_cursor':activity_cursor(data),'checkpoint':None if p is None else {'comment_id':str(c['id']),'author':clip(c.get('author'),96),'timestamp':c.get('created_at'),'source_commit':p['source_commit'],'branch':p['branch'],'incorporated_activity_cursor':p['activity_cursor'],
                'newer_activity':p['activity_cursor']!=activity_cursor(snapshot(rows,project,task,str(c['id'])))},
            'intent':clip(p['intent'] if p else issue.get('description'),600),'acceptance':clip(p['acceptance'] if p else issue.get('acceptance_criteria'),1000),
            'current_position':p['summary'] if p else 'Unknown: no structured checkpoint; legacy prose has not been reconciled.',
            'next_action':p['next_action'] if p else 'Read relevant history, then register a checkpoint carrying unresolved items.',
            'unresolved':{'coverage':'explicit checkpoint items only; legacy prose is not classified','total':len(items) if p else None,'items':items[offset:offset+limit],'next_offset':offset+limit if offset+limit<len(items) else None},
            'dependencies':{'total':len(deps),'items':[{k:clip(d.get(k),160) for k in ('depends_on_id','type')} for d in deps[:8]],'omitted':max(0,len(deps)-8)},
            'lifecycle':{dim:dict(value=f['value'],event_id=f['event_id']) for dim,f in facts['facts'].items()},
            'lifecycle_scope':{k:clip(v,160) for k,v in (facts['scope'] or {}).items()},
            'warnings':(['Malformed checkpoint comments ignored: '+', '.join(invalid[:5])] if invalid else [])+['Newer activity also includes edits, deletions or changed task fields. Prose resolutions never silently clear explicit items.'],
            'evidence':{'issue':'show '+task,'history':'history '+task,'checkpoint_entry':task+'-c'+str(c['id']) if c else None}}

def save_checkpoint(rows,project,task,p,actor,run):
    validate_checkpoint(p,task);issue=task_row(rows,task)
    for c in issue.get('comments') or []:
        if c.get('text')==PREFIX+canonical_bytes(p).decode() and c.get('author')==actor:
            return {'comment_id':str(c['id']),'reconciled':True}
    current,invalid=checkpoints(issue)
    if invalid:raise ValueError('Malformed checkpoint entries require correction before publishing another checkpoint')
    previous=str(current[1]['id']) if current else None
    if p['previous']!=previous:raise ValueError('Stale previous checkpoint; read brief again')
    if p['activity_cursor']!=activity_cursor(snapshot(rows,project,task)):raise ValueError('Activity changed; read/reconcile history and obtain a fresh activity cursor')
    transition(current[0] if current else None,p)
    result=json.loads(run(['comments','add',task,PREFIX+canonical_bytes(p).decode(),'--json']))
    return {'comment_id':str(result['id']),'reconciled':False}

def history_page(data,project,task,limit=5,since=None,cursor=None,body_budget=4000):
    if type(limit) is not int or not 1<=limit<=20:raise ValueError('History limit must be 1..20')
    if type(body_budget) is not int or not 256<=body_budget<=8000:raise ValueError('History body budget must be 256..8000')
    if data.get('project')!=project or data.get('task')!=task:raise ValueError('History scope mismatch')
    digest=content_hash(data);index=0;start=0
    since=utc_text(parse_moment(since,'--since')) if since is not None else None
    if cursor:
        c=untoken(cursor)
        if not isinstance(c,dict) or set(c)!={'v','kind','project','task','snapshot','since','index','offset'} or type(c['v']) is not int or c['v']!=1 or c['kind']!='history':raise ValueError('Invalid history cursor')
        if (c['project'],c['task'],c['snapshot'])!=(project,task,digest):raise ValueError('Cursor snapshot/scope mismatch')
        if since is not None and since!=c['since']:raise ValueError('Cursor --since mismatch')
        since=c['since'];index=c['index'];start=c['offset']
        if type(index) is not int or type(start) is not int or index<0 or start<0:raise ValueError('Invalid cursor position')
    entries=[e for e in data['entries'] if not since or parse_moment(e['timestamp'])>=parse_moment(since)]
    if index>len(entries) or (index==len(entries) and start) or (index<len(entries) and start>len(entries[index]['body'])):raise ValueError('Invalid cursor position')
    page=[];remaining=body_budget
    while index<len(entries) and len(page)<limit and remaining>0:
        entry=entries[index];body=entry['body'];end=min(len(body),start+remaining)
        # Budget JSON-encoded body bytes, not just characters: Unicode/control
        # characters must not unexpectedly inflate the transport response.
        lo,hi=start,end
        while lo<hi:
            mid=(lo+hi+1)//2
            if len(json.dumps(body[start:mid],ensure_ascii=False).encode())<=remaining+2:lo=mid
            else:hi=mid-1
        end=lo
        if end==start and start<len(body):break
        fragment=body[start:end]
        page.append({'entry_id':entry['entry_id'],'kind':entry['kind'],'timestamp':entry['timestamp'],'author':clip(entry['author'],96),
                     'body':fragment,'body_offset':start,'body_total_chars':len(body),'continued':end<len(body)})
        remaining-=max(1,len(json.dumps(fragment,ensure_ascii=False).encode())-2)
        if end<len(body):start=end;break
        index+=1;start=0
    next_value=None if index==len(entries) else token({'v':1,'kind':'history','project':project,'task':task,'snapshot':digest,'since':since,'index':index,'offset':start})
    return {'task':task,'snapshot':digest,'since':since,'since_inclusive':True,'total_entries':len(entries),'entries':page,'next_cursor':next_value,'activity_cursor':activity_cursor(data),
            'coverage':'Snapshot of exported comments and native task events, not every database mutation. Continue this snapshot or restart for new activity.'}

class Parser(argparse.ArgumentParser):
    def error(self,message):raise ValueError(message)

def parse_args(action,args):
    parser=Parser(add_help=False);parser.add_argument('task');parser.add_argument('--json',action='store_true')
    if action=='brief':parser.add_argument('--items-offset',type=int,default=0);parser.add_argument('--items-limit',type=int,default=5)
    elif action=='history':
        parser.add_argument('--limit',type=int,default=5);parser.add_argument('--since');parser.add_argument('--cursor');parser.add_argument('--body-budget',type=int,default=4000)
    else:raise ValueError('Unknown briefing action')
    parsed=parser.parse_args(args);identity(parsed.task);return parsed

def format_brief(result):
    def excerpt(value):return value['text']+(f' [excerpt; {value["omitted_chars"]} characters omitted — use show/history]' if value['omitted_chars'] else '')
    lines=[f'{result["task"]}: {excerpt(result["title"])}',f'Owner: {excerpt(result["owner"])} | Status: {result["status"]}',
           'Intent: '+excerpt(result['intent']),'Acceptance: '+excerpt(result['acceptance']),
           'Current position: '+result['current_position'],'Next: '+result['next_action']]
    cp=result['checkpoint']
    if cp:lines += [f'Checkpoint: {cp["comment_id"]} by {excerpt(cp["author"])} at {cp["timestamp"]}',f'Branch: {cp["branch"] or "unknown"} | Source commit: {cp["source_commit"] or "unknown"}',
                    'Newer/changed activity: '+str(cp['newer_activity']), 'Incorporated activity cursor: '+cp['incorporated_activity_cursor']]
    else:lines += ['Checkpoint: absent; unresolved legacy items are UNKNOWN, not zero.']
    unresolved=result['unresolved'];lines += [f'Unresolved items: {unresolved["total"] if unresolved["total"] is not None else "unknown"}']
    lines += [f'- {item["id"]} [{item["kind"]}]: {item["text"]} (source: {item["source"]})' for item in unresolved['items']]
    if unresolved['next_offset'] is not None:lines += [f'More unresolved items: brief {result["task"]} --items-offset {unresolved["next_offset"]}']
    lines += ['Native dependencies: '+json.dumps(result['dependencies'],ensure_ascii=False), 'Lifecycle: '+', '.join(k+'='+v['value'] for k,v in result['lifecycle'].items()),
              'Lifecycle scope: '+json.dumps(result['lifecycle_scope'],ensure_ascii=False),'Current activity cursor: '+result['activity_cursor'],
              'Evidence: '+json.dumps(result['evidence'],ensure_ascii=False),*result['warnings']]
    return '\n'.join(lines)+'\n'

def execute(root,path,project,actor,action,args,attachments,run):
    """Endpoint holds project coordination lock. History caches are disposable."""
    if action=='checkpoint':
        if len(args)!=2 or not args[1].startswith('@attachment:'):raise ValueError('Use checkpoint TASK --file checkpoint.json')
        item=attachments.get(args[1].partition(':')[2],{})
        if item.get('flag') not in ('--file','-f') or not isinstance(item.get('text'),str):raise ValueError('Checkpoint needs a JSON file attachment')
        rows=[json.loads(x) for x in run(['export','--all']).splitlines() if x.strip()]
        return json.dumps(save_checkpoint(rows,project,args[0],json.loads(item['text']),actor,run))+'\n'
    a=parse_args(action,args)
    cache=path/'.history-snapshots'
    if cache.is_symlink():raise ValueError('History cache must not be a symlink')
    if action=='history' and a.cursor:
        c=untoken(a.cursor);digest=c.get('snapshot') if isinstance(c,dict) else None
        if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):raise ValueError('Invalid snapshot cursor')
        file=cache/(digest+'.json')
        if file.is_symlink():raise ValueError('Invalid snapshot path')
        if not file.exists():raise ValueError('History snapshot unavailable/expired; restart history without a cursor')
        saved=json.loads(file.read_text(encoding='utf-8'));data=saved['data']
        if content_hash(data)!=digest:raise ValueError('History cache integrity mismatch')
    else:
        rows=[json.loads(x) for x in run(['export','--all']).splitlines() if x.strip()]
        if action=='brief':
            result=brief(rows,project,a.task,a.items_offset,a.items_limit)
            return json.dumps(result,ensure_ascii=False,indent=2)+'\n' if a.json else format_brief(result)
        data=snapshot(rows,project,a.task);digest=content_hash(data);cache.mkdir(exist_ok=True)
        file=cache/(digest+'.json')
        if file.is_symlink():raise ValueError('Invalid snapshot path')
        if not file.exists():
            from coordination import atomic
            atomic(file,{'captured_at':datetime.now(timezone.utc).isoformat(),'data':data})
        saved=json.loads(file.read_text(encoding='utf-8'))
    result=history_page(data,project,a.task,a.limit,a.since,a.cursor,a.body_budget)
    result['captured_at']=saved['captured_at']
    # JSON for both modes keeps fragments and opaque continuation cursors exact.
    return json.dumps(result,ensure_ascii=False,indent=2)+'\n'
