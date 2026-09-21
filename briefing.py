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
    # Per-entry provenance: content digests keyed by stable entry ID let a later
    # checkpoint/snapshot distinguish edited entries and late backdated arrivals
    # that a whole-snapshot hash or timestamp filter cannot separate.
    digests={e['entry_id']:content_hash(e) for e in entries}
    return {'project':project,'task':task,'state_sha256':content_hash(state),'entries':entries,'entry_digests':digests}

def entry_digests(data):
    """Digests for snapshots predating the embedded entry_digests field."""
    found=data.get('entry_digests')
    return found if isinstance(found,dict) else {e['entry_id']:content_hash(e) for e in data['entries']}

def activity_cursor(data):return token({'v':1,'kind':'activity','project':data['project'],'task':data['task'],'sha256':content_hash(data)})

def text(value,label,limit,empty=False):
    if not isinstance(value,str) or len(value)>limit or (not empty and not value.strip()):raise ValueError(f'{label}: expected text up to {limit} characters')

def validate_checkpoint(p,task,require_digests=True):
    fields={'schema_version','task','previous','activity_cursor','source_commit','branch','intent','acceptance','summary','next_action','open_items','resolved','incorporated_digests','provenance','directions'}
    if not isinstance(p,dict) or type(p.get('schema_version')) is not int or p['schema_version']!=1:raise ValueError('Invalid checkpoint fields/version')
    # incorporated_digests, provenance and directions are always optional on the
    # wire: the server computes authoritative provenance under lock, and legacy
    # payloads without them stay valid (schema_version remains 1).
    optional={'incorporated_digests','provenance','directions'}
    if not set(p)<=fields or not set(fields)-optional<=set(p):raise ValueError('Invalid checkpoint fields/version')
    if p['task']!=task:raise ValueError('Checkpoint task mismatch')
    if p['previous'] is not None:identity(p['previous'])
    for key,limit in [('source_commit',128),('branch',200),('intent',600),('acceptance',1000),('summary',1000),('next_action',600)]:text(p[key],key,limit,empty=key in ('source_commit','branch'))
    cursor=untoken(p['activity_cursor'])
    if not isinstance(cursor,dict) or cursor.get('kind')!='activity' or cursor.get('task')!=task:raise ValueError('Expected task activity cursor from brief/history')
    # Legacy checkpoints predate per-entry digests; they stay readable with
    # unknown coverage. Digests are validated when present but never required —
    # the server computes authoritative provenance at write time.
    if 'incorporated_digests' in p:
        digests=p['incorporated_digests']
        if not isinstance(digests,dict) or len(digests)>DIGEST_WINDOW or not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,200}',str(k)) and re.fullmatch(r'[a-f0-9]{64}',str(v)) for k,v in digests.items()):raise ValueError('Invalid incorporated_digests')
    if 'provenance' in p:
        prov=p['provenance']
        if not isinstance(prov,dict) or not set(prov)<={'digests','chain','covered'} or not isinstance(prov.get('digests'),dict):raise ValueError('Invalid provenance')
        if not re.fullmatch(r'[a-f0-9]{64}',str(prov.get('chain',''))):raise ValueError('Invalid provenance chain')
        if type(prov.get('covered')) is not int or prov['covered']<0:raise ValueError('Invalid provenance covered count')
    if 'directions' in p:
        dirs=p['directions']
        if not isinstance(dirs,list) or len(dirs)>100:raise ValueError('Invalid directions')
        ids=[]
        for d in dirs:
            if not isinstance(d,dict) or not set(d)<={'id','state','digest','note','evidence'} or not {'id','state','digest'}<=set(d):raise ValueError('Invalid direction record')
            ids.append(identity(d['id']))
            if d['state'] not in DIRECTION_STATES:raise ValueError('Invalid direction state')
            if not re.fullmatch(r'[a-f0-9]{64}',str(d['digest'])):raise ValueError('Invalid direction digest')
            if d['state'] in RESOLUTION_STATES:
                text(d.get('note',''),'direction note',400);text(d.get('evidence',''),'direction evidence',240)
        if len(set(ids))!=len(ids):raise ValueError('Duplicate direction IDs')
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
            p=json.loads(body[len(PREFIX):]);validate_checkpoint(p,issue['id'],require_digests=False)
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

NEWER_MAX=5
# Bounded provenance window: a checkpoint records exact digests for the most
# recent incorporated entries and a rolling chain hash covering all older ones,
# so the record stays within the checkpoint size cap on long-lived tasks while
# remaining tamper-evident over the full history.
DIGEST_WINDOW=200
DIRECTION_STATES=('acknowledged','resolved','superseded')
RESOLUTION_STATES=('resolved','superseded')
ZERO_HASH='0'*64

def provenance_chain(digests):
    """Rolling hash over entry_id:digest pairs in sorted order, tamper-evident."""
    h=ZERO_HASH
    for k in sorted(digests):
        h=content_hash({'chain':h,'entry_id':k,'digest':digests[k]})
    return h

def bounded_digests(digests):
    """Split an exact digest map into a bounded recent window plus a chain hash
    over the remainder. Window keeps the most recent entries by recency of the
    snapshot order is unavailable here, so keep the lexicographically-largest
    (most recent ULID/native) entry IDs bounded and chain the rest."""
    keys=sorted(digests)
    window={k:digests[k] for k in keys[-DIGEST_WINDOW:]}
    rest={k:digests[k] for k in keys[:-DIGEST_WINDOW]}
    return {'digests':window,'chain':provenance_chain(rest),'covered':len(digests)}

def parse_provenance(p):
    """Normalize a checkpoint's provenance into a full digest map, or None when
    unknown (legacy checkpoint with neither field)."""
    if 'incorporated_digests' in p:
        return dict(p['incorporated_digests'])
    prov=p.get('provenance')
    if isinstance(prov,dict) and isinstance(prov.get('digests'),dict):
        return dict(prov['digests'])
    return None

def direction_index(checkpoint_payload):
    """entry_id -> direction state record from a checkpoint's directions list."""
    return {d['id']:d for d in (checkpoint_payload.get('directions') or [])}

def unincorporated(data,known):
    """Split current snapshot entries into fresh/changed vs incorporated, by digest.

    `known` is the checkpoint's incorporated digest map (None = unknown coverage).
    Returns (entries, fresh, changed_late) preserving snapshot order.
    """
    if known is None:return data['entries'],[],[]
    fresh=[];changed_late=[]
    for e in data['entries']:
        digest=content_hash(e)
        if e['entry_id'] not in known:fresh.append(e)
        elif known[e['entry_id']]!=digest:changed_late.append(e)
    return fresh+changed_late,fresh,changed_late

def unresolved_directions(entries,direction_idx,owner):
    """Directions (other-actor comments) that are not yet resolved/superseded.

    A direction is outstanding when it is absent from the checkpoint's
    directions, only acknowledged, or its content changed since the recorded
    disposition (edit invalidates the earlier acknowledgement). Digest/cursor
    incorporation alone never resolves a direction; acknowledgement is distinct
    from resolution and completion.
    """
    outstanding=[]
    for e in entries:
        if e['kind']!='comment' or e['author']==owner:continue
        if str(e.get('body','')).startswith(('Kind: task-checkpoint-v1','Kind: contribution-review-v1')):continue
        d=direction_idx.get(e['entry_id'])
        if d is None or d['state'] not in RESOLUTION_STATES or d.get('digest')!=content_hash(e):
            outstanding.append(e)
    return outstanding

def newer_activity_summary(data,incorporated_digests,checkpoint_timestamp,owner,direction_idx=None):
    """Bounded, per-entry view of activity the current checkpoint did not incorporate.

    `incorporated_digests` is the checkpoint's recorded entry_id -> content digest
    map, so edited entries (same ID, new content) and late arrivals backdated
    before the checkpoint (new ID, old timestamp) are identified exactly; a
    timestamp filter alone could not. Counts distinguish fresh entries from
    changed/late ones and split own vs other actors. Directions from other actors
    are tracked to an explicit acknowledged/resolved/superseded disposition;
    every collection is bounded with an explicit omitted count. Reading changes
    nothing.
    """
    unknown_coverage=incorporated_digests is None
    entries,fresh,changed_late=unincorporated(data,None if unknown_coverage else incorporated_digests)
    own=[e for e in entries if e['author']==owner]
    others=[e for e in entries if e['author']!=owner]
    authors=sorted({e['author'] for e in others})
    refs=[{'entry_id':e['entry_id'],'kind':e['kind'],'timestamp':e['timestamp'],'author':clip(e['author'],96),
           'changed':any(c is e for c in changed_late)} for e in entries[:NEWER_MAX]]
    history='history '+data['task']
    result={'own_count':len(own),'other_count':len(others),
            'fresh_count':len(fresh),'changed_or_late_count':len(changed_late),
            'coverage':'unknown' if unknown_coverage else 'snapshot',
            'other_authors':{'items':[clip(a,96) for a in authors[:NEWER_MAX]],'omitted':max(0,len(authors)-NEWER_MAX)},
            'entries':refs,'omitted':max(0,len(entries)-NEWER_MAX),
            'history':history,
            'history_new':'history '+data['task']+' --since '+utc_text(parse_moment(checkpoint_timestamp,'checkpoint timestamp')) if checkpoint_timestamp else history,
            'note':'Activity exists that the saved checkpoint did not incorporate; its next_action below may be stale. Entry refs are the oldest unincorporated ones with per-entry authors; counts cover the full snapshot, including edited and late backdated entries the excerpt omits. Use the history paths (the --since path lists fresh activity; page full history for edited/backdated entries). Reading clears nothing: acknowledging a direction, reconciling it into a new checkpoint and completing it stay separate explicit acts.'+(' Coverage is UNKNOWN: this checkpoint predates per-entry digests, so reconcile with history before trusting the counts.' if unknown_coverage else '')}
    if direction_idx is not None:
        outstanding=unresolved_directions(data['entries'],direction_idx,owner)
        result['unresolved_directions']={'total':len(outstanding),
            'items':[{'entry_id':e['entry_id'],'timestamp':e['timestamp'],'author':clip(e['author'],96),
                      'state':(direction_idx.get(e['entry_id']) or {}).get('state','unacknowledged')} for e in outstanding[:NEWER_MAX]],
            'omitted':max(0,len(outstanding)-NEWER_MAX)}
    return result

def brief(rows,project,task,offset=0,limit=5):
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=10:raise ValueError('Invalid unresolved-item page')
    issue=task_row(rows,task);current,invalid=checkpoints(issue)
    if issue.get('issue_type')=='event':raise ValueError('Use history/show for an event; brief requires a task or job')
    data=snapshot(rows,project,task);p,c=current if current else (None,None)
    facts=next(r for r in project_facts(rows) if r['id']==task)
    items=p['open_items'] if p else []
    if offset>len(items):raise ValueError('Unresolved-item offset exceeds total')
    deps=[d for d in (issue.get('dependencies') or []) if d.get('type')!='parent-child']
    from work import workflow
    review=workflow(issue)
    matches_contribution=None if not review.get('contribution') else (facts['scope'] or {}).get('source_commit','').lower()==review['contribution']['commit'].lower()
    if matches_contribution and review['review_state']=='awaiting-integration' and facts['facts']['integrated']['value']=='passed':
        review=dict(review,review_state='integrated')
    pending=review.get('pending_requests',[])
    review_next={'changes-requested':'Address the outstanding review requests for the current contribution; read review '+task+'.',
                 'awaiting-review':'Reviewer: retrieve and verify the current contribution, then record review feedback or approval.',
                 'awaiting-integration':'Authorized integrator: integrate the approved contribution and record scoped integration evidence.',
                 'integrated':'Integration is recorded for this contribution; follow the project release/deployment workflow and scoped lifecycle evidence.'}
    newer=None;next_action=review_next.get(review['review_state'],p['next_action'] if p else 'Read the task description, acceptance criteria and any history, then publish a checkpoint.')
    if p is not None:
        excluded=snapshot(rows,project,task,str(c['id']))
        if p['activity_cursor']!=activity_cursor(excluded):
            newer=newer_activity_summary(excluded,parse_provenance(p),c.get('created_at'),issue.get('assignee'),direction_index(p))
            if review['review_state'] not in review_next:
                dirs=newer.get('unresolved_directions')
                dir_note='' if not dirs else '; '+str(dirs['total'])+' outstanding direction(s) not yet resolved/superseded'
                next_action=('STALE CHECKPOINT: activity the checkpoint did not incorporate exists ('+str(newer['other_count'])
                             +' by other actors, '+str(newer['own_count'])+' own'+dir_note
                             +('' if newer['coverage']!='unknown' else ', coverage UNKNOWN — reconcile with history before trusting counts')
                             +'). Read newer activity first: '+newer['history']
                             +'. The recorded checkpoint next action was: '+p['next_action'])
    # Outstanding directions persist independently of cursor freshness: an
    # acknowledged-but-unresolved direction stays visible on a current checkpoint.
    dir_out=None
    if p is not None:
        outstanding=unresolved_directions(data['entries'],direction_index(p),issue.get('assignee'))
        dir_out={'total':len(outstanding),
                 'items':[{'entry_id':e['entry_id'],'timestamp':e['timestamp'],'author':clip(e['author'],96),
                           'state':(direction_index(p).get(e['entry_id']) or {}).get('state','unacknowledged')} for e in outstanding[:NEWER_MAX]],
                 'omitted':max(0,len(outstanding)-NEWER_MAX)}
    return {'task':task,'title':clip(issue.get('title'),200),'owner':clip(issue.get('assignee') or 'unassigned',96),'status':issue.get('status'),
            'activity_cursor':activity_cursor(data),'checkpoint':None if p is None else {'comment_id':str(c['id']),'author':clip(c.get('author'),96),'timestamp':c.get('created_at'),'source_commit':p['source_commit'],'branch':p['branch'],'incorporated_activity_cursor':p['activity_cursor'],
                'newer_activity':p['activity_cursor']!=activity_cursor(snapshot(rows,project,task,str(c['id'])))},
            'newer':newer,
            'directions':dir_out,
            'intent':clip(p['intent'] if p else issue.get('description'),600),'acceptance':clip(p['acceptance'] if p else issue.get('acceptance_criteria'),1000),
            'current_position':p['summary'] if p else 'No checkpoint yet; current position and unresolved items have not been summarized.',
            'next_action':next_action,
            'directions':dir_out,
            'unresolved':{'coverage':'explicit checkpoint items only; unsummarized prose is not classified','total':len(items) if p else None,'items':items[offset:offset+limit],'next_offset':offset+limit if offset+limit<len(items) else None},
            'review':dict(review,pending_requests=pending[:5],pending_total=len(pending),more='review '+task if len(pending)>5 else None),
            'lifecycle_matches_contribution':matches_contribution,
            'dependencies':{'total':len(deps),'items':[{k:clip(d.get(k),160) for k in ('depends_on_id','type')} for d in deps[:8]],'omitted':max(0,len(deps)-8)},
            'lifecycle':{dim:dict(value=f['value'],event_id=f['event_id']) for dim,f in facts['facts'].items()},
            'lifecycle_scope':{k:clip(v,160) for k,v in (facts['scope'] or {}).items()},
            'warnings':(['Malformed checkpoint comments ignored: '+', '.join(invalid[:5])] if invalid else [])+['Newer activity also includes edits, deletions or changed task fields. Prose resolutions never silently clear explicit items.'],
            'evidence':{'issue':'show '+task,'history':'history '+task,'checkpoint_entry':task+'-c'+str(c['id']) if c else None}}

def save_checkpoint(rows,project,task,p,actor,run):
    validate_checkpoint(p,task,require_digests=False);issue=task_row(rows,task)
    for c in issue.get('comments') or []:
        if c.get('text')==PREFIX+canonical_bytes(p).decode() and c.get('author')==actor:
            return {'comment_id':str(c['id']),'reconciled':True}
    current,invalid=checkpoints(issue)
    if invalid:raise ValueError('Malformed checkpoint entries require correction before publishing another checkpoint')
    previous=str(current[1]['id']) if current else None
    if p['previous']!=previous:raise ValueError('Stale previous checkpoint; read brief again')
    snap=snapshot(rows,project,task)
    if p['activity_cursor']!=activity_cursor(snap):raise ValueError('Activity changed; read/reconcile history and obtain a fresh activity cursor')
    # Authoritative binding: the caller may not assert which entries the
    # checkpoint incorporated. The server computes the exact digest map from the
    # current snapshot under the project lock and rejects a caller-supplied map
    # that is missing, extra or incorrect — then stores the canonical bounded form.
    computed=entry_digests(snap)
    supplied=parse_provenance(p)
    if supplied is not None and supplied!=computed:
        raise ValueError('incorporated_digests/provenance does not match the current snapshot; recompute from a fresh brief; on the live server request provenance via "checkpoint TASK --provenance" and embed it')
    # Direction dispositions must reference the digests actually incorporated.
    if p.get('directions'):
        for d in p['directions']:
            if d['id'] in computed and d['digest']!=computed[d['id']]:
                raise ValueError('Direction '+d['id']+' digest does not match the incorporated entry; reconcile before resolving')
    # Acknowledgement is not resolution: a direction carried forward as merely
    # 'acknowledged' while it is incorporated by this checkpoint stays outstanding
    # in brief until explicitly resolved/superseded with evidence.
    transition(current[0] if current else None,p)
    p=dict(p);p.pop('incorporated_digests',None);p.pop('provenance',None)
    p['provenance']=bounded_digests(computed)
    result=json.loads(run(['comments','add',task,PREFIX+canonical_bytes(p).decode(),'--json']))
    return {'comment_id':str(result['id']),'reconciled':False,'covered':p['provenance']['covered']}

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
           'Review/contribution: '+json.dumps(result['review'],ensure_ascii=False),
           'Intent: '+excerpt(result['intent']),'Acceptance: '+excerpt(result['acceptance']),
           'Current position: '+result['current_position'],'Next: '+result['next_action']]
    cp=result['checkpoint']
    if cp:lines += [f'Checkpoint: {cp["comment_id"]} by {excerpt(cp["author"])} at {cp["timestamp"]}',f'Branch: {cp["branch"] or "unknown"} | Source commit: {cp["source_commit"] or "unknown"}',
                    'Newer/changed activity: '+str(cp['newer_activity']), 'Incorporated activity cursor: '+cp['incorporated_activity_cursor']]
    else:lines += ['No checkpoint yet; unresolved items are UNKNOWN, not zero.']
    newer=result['newer']
    if newer:
        authors=', '.join(a['text'] for a in newer['other_authors']['items']) or 'none'
        if newer['other_authors']['omitted']:authors+=f' (+{newer["other_authors"]["omitted"]} more)'
        coverage='' if newer['coverage']!='unknown' else ' Coverage UNKNOWN (checkpoint predates per-entry digests).'
        lines += [f'Newer activity not in checkpoint: {newer["other_count"]} by other actors ({authors}), {newer["own_count"]} own; {newer["fresh_count"]} fresh, {newer["changed_or_late_count"]} changed/late.'+coverage]
        lines += ['  '+e['entry_id']+' ['+e['kind']+(' changed' if e['changed'] else '')+'] '+e['timestamp']+' by '+e['author']['text'] for e in newer['entries']]
        if newer['omitted']:lines += [f'  ... {newer["omitted"]} more; fresh activity: '+newer['history_new']+'; full: '+newer['history']]
        lines += ['Read newer activity: '+newer['history_new'], newer['note']]
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
        if args and args[1:2]==['--provenance']:
            # Supported read path: return the exact current provenance (bounded
            # window + chain) and cursor for the caller to embed in a checkpoint.
            rows=[json.loads(x) for x in run(['export','--all']).splitlines() if x.strip()]
            snap=snapshot(rows,project,args[0])
            prov=bounded_digests(entry_digests(snap))
            return json.dumps({'task':args[0],'activity_cursor':activity_cursor(snap),
                               'incorporated_digests':prov['digests'],'provenance':prov},ensure_ascii=False,indent=2)+'\n'
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
