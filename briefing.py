"""Compact task state, explicit checkpoints and lossless snapshot history pages."""
import argparse
import base64
import json
import record_json
import re
from datetime import datetime, timezone
from pathlib import Path
from activity import build_entries, parse_moment, utc_text
from field_limits import check_text
from lifecycle import project_facts
from requirements import canonical_bytes, content_hash

PREFIX='Kind: task-checkpoint-v1\n'
KINDS={'blocker','question','decision','correction','dependency'}
CHECKPOINT_ITEMS_MAX=100
CHECKPOINT_TEXT_LIMIT=400
#: The top-level text fields of a checkpoint and their limits; the validator and the
#: `checkpoint` help both read this list (kittrial-5bb.97).
CHECKPOINT_FIELD_LIMITS=(('source_commit',128),('branch',200),('intent',600),('acceptance',1000),('summary',1000),('next_action',600))
CHECKPOINT_SOURCE_LIMIT=240
CHECKPOINT_MAX_BYTES=80000
BRIEF_ITEM_OFFSET_MIN=0
BRIEF_ITEM_LIMIT_MIN,BRIEF_ITEM_LIMIT_MAX=1,10
HISTORY_LIMIT_MIN,HISTORY_LIMIT_MAX=1,20
HISTORY_BUDGET_MIN,HISTORY_BUDGET_MAX=256,8000
FIELD_NAME_LIMIT=60
FIELD_NAME_COUNT=8
BRIEF_MISTAKEN_FLAGS={
    '--limit':'use --items-limit for the unresolved-item page size',
    '--offset':'use --items-offset for the unresolved-item page offset',
}
# `brief` is a compact read: the complete prior-contribution chain stays available
# through `review TASK`, so the embedded view carries only a bounded recent slice
# (without summaries) plus a total and a pointer.
PRIOR_BRIEF_LIMIT=5
# The embedded host-issued revert list is bounded the same way (kittrial-5bb.52
# review item `smaller`): the compact read carries the newest slice plus a total
# and a pointer, never the unclipped list.
REVERT_BRIEF_LIMIT=5

def token(data):return base64.urlsafe_b64encode(canonical_bytes(data)).decode().rstrip('=')

def untoken(value):
    try:
        if not isinstance(value,str) or len(value)>4096 or not re.fullmatch(r'[A-Za-z0-9_-]+',value):raise ValueError()
        return record_json.loads(base64.urlsafe_b64decode(value+'='*(-len(value)%4)))
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
    from reserved_comments import is_record_comment
    issue=task_row(rows,task)
    # Reference/proposal/settings/capability record comments never reach a history,
    # brief or checkpoint read, on an anchor or on any other task (kittrial-5bb.64).
    selected=[dict(issue,comments=[c for c in (issue.get('comments') or [])
                                   if str(c['id'])!=exclude and not is_record_comment(c.get('text'))])]
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

def activity_cursor(data):
    # entry_digests is derived entirely from entries, not extra activity. Keep
    # the released hash shape so staged writes remain fresh on older readers.
    compatible={k:v for k,v in data.items() if k!='entry_digests'}
    return token({'v':1,'kind':'activity','project':data['project'],'task':data['task'],'sha256':content_hash(compatible)})

def cursor_matches(cursor,data,compatible=None):
    # Read receipts already written by the first provenance kit in their shape.
    if cursor==(compatible if compatible is not None else activity_cursor(data)):return True
    previous=token({'v':1,'kind':'activity','project':data['project'],'task':data['task'],'sha256':content_hash(data)})
    return cursor==previous

def text(value,label,limit,empty=False):
    # Names the field, the length it had and the limit (kittrial-5bb.97).
    check_text(value,label,limit,empty=empty,nul=False)

def field_names(values):
    """Bounded, sorted caller-supplied field names for an error message.

    Names are capped individually and in count so a caller cannot turn a
    validation error into an echo of an arbitrarily large payload.
    """
    names=sorted(values)
    shown=[name[:FIELD_NAME_LIMIT]+('...' if len(name)>FIELD_NAME_LIMIT else '') for name in names[:FIELD_NAME_COUNT]]
    if len(names)>FIELD_NAME_COUNT:shown.append('(+%d more)'%(len(names)-FIELD_NAME_COUNT))
    return ', '.join(shown)

#: At most this many problems are named in one checkpoint refusal; the rest are counted.
CHECKPOINT_PROBLEMS_MAX=20
PROBLEMS_PREFIX='Invalid checkpoint (%d problems):'

def checkpoint_problems(p,task):
    """Every problem with a checkpoint payload that can be found without reading the task, in order.

    Each entry is the sentence ``validate_checkpoint`` has always raised for that problem
    alone (kittrial-5bb.113): an agent that sends a payload with five faults is told all
    five at once instead of one per round trip. The sentences are unchanged, so a caller
    that matches on one still finds it.
    """
    optional={'incorporated_digests','provenance','directions','carried','direction_owner'}
    fields={'schema_version','task','previous','activity_cursor','source_commit','branch','intent','acceptance','summary','next_action','open_items','resolved'}|optional
    if not isinstance(p,dict):return ['Invalid checkpoint: expected a JSON object']
    problems=[]
    def attempt(check,*args,**kwargs):
        try:check(*args,**kwargs)
        except ValueError as error:problems.append(str(error))
    unknown=sorted(set(p)-fields);missing=sorted(fields-set(p)-optional);details=[]
    if unknown:details.append('unknown fields: '+field_names(unknown))
    if missing:details.append('missing fields: '+field_names(missing))
    if details:problems.append('Invalid checkpoint: '+'; '.join(details))
    if 'schema_version' in p and (type(p['schema_version']) is not int or p['schema_version']!=1):
        problems.append('Invalid checkpoint: schema_version must be integer 1')
    if 'task' in p and p['task']!=task:problems.append('Checkpoint task mismatch')
    if p.get('previous') is not None:attempt(identity,p['previous'])
    if p.get('direction_owner') is not None:attempt(text,p['direction_owner'],'direction_owner',96)
    for key,limit in CHECKPOINT_FIELD_LIMITS:
        if key in p:attempt(text,p[key],key,limit,empty=key in ('source_commit','branch'))
    if 'activity_cursor' in p:
        def cursor_check():
            cursor=untoken(p['activity_cursor'])
            if not isinstance(cursor,dict) or cursor.get('kind')!='activity' or cursor.get('task')!=task:raise ValueError('Expected task activity cursor from brief/history')
        attempt(cursor_check)
    # Legacy checkpoints predate per-entry digests; they stay readable with
    # unknown coverage. Digests are validated when present but never required:
    # the server computes authoritative provenance at write time.
    if 'incorporated_digests' in p:
        digests=p['incorporated_digests']
        if not isinstance(digests,dict) or len(digests)>DIGEST_WINDOW or not valid_digest_map(digests,64):problems.append('Invalid incorporated_digests')
    if 'provenance' in p:
        def provenance_check():
            prov=p['provenance']
            if not isinstance(prov,dict) or not set(prov)<={'digests','older','chain','covered','window','delta'} or not isinstance(prov.get('digests'),dict):raise ValueError('Invalid provenance')
            if 'delta' in prov and prov['delta'] is not True:raise ValueError('Invalid provenance delta')
            if not re.fullmatch(r'[a-f0-9]{64}',str(prov.get('chain',''))):raise ValueError('Invalid provenance chain')
            if type(prov.get('covered')) is not int or prov['covered']<0:raise ValueError('Invalid provenance covered count')
            if len(prov['digests'])>DIGEST_WINDOW:raise ValueError('Provenance window exceeds the digest window')
            if not valid_digest_map(prov['digests'],64):raise ValueError('Invalid provenance digests')
            if 'window' in prov and (type(prov['window']) is not int or prov['window']!=len(prov['digests'])):raise ValueError('Invalid provenance window size')
            older=prov.get('older',{})
            if not isinstance(older,dict) or len(older)>OLDER_MAX:raise ValueError('Invalid provenance older map')
            if not valid_digest_map(older,OLDER_DIGEST):raise ValueError('Invalid provenance older digest')
            if set(older)&set(prov['digests']) or prov['covered']<len(prov['digests'])+len(older):raise ValueError('Invalid provenance coverage')
        attempt(provenance_check)
    def directions_check(field):
        dirs=p[field]
        if not isinstance(dirs,list) or len(dirs)>DIRECTIONS_MAX:raise ValueError('Checkpoint '+field+' limited to '+str(DIRECTIONS_MAX))
        ids=[]
        for d in dirs:
            if not isinstance(d,dict) or not set(d)<={'id','state','digest','note','evidence'} or not {'id','state','digest'}<=set(d):raise ValueError('Invalid direction record')
            ids.append(identity(d['id']))
            if d['state'] not in DIRECTION_STATES:raise ValueError('Invalid direction state')
            if not re.fullmatch(r'[a-f0-9]{64}',str(d['digest'])):raise ValueError('Invalid direction digest')
            if d['state'] in RESOLUTION_STATES:
                text(d.get('note',''),'direction note',400);text(d.get('evidence',''),'direction evidence',240)
        if len(set(ids))!=len(ids):raise ValueError('Duplicate direction IDs')
    before=len(problems)
    for field in ('directions','carried'):
        if field in p:attempt(directions_check,field)
    # The effective count needs both lists well formed.
    if len(problems)==before and len(direction_index(p))>DIRECTIONS_MAX:
        problems.append('Effective dispositions exceed '+str(DIRECTIONS_MAX)+' entries')
    for field in ('open_items','resolved'):
        if field not in p:continue
        items=p[field]
        if not isinstance(items,list) or len(items)>CHECKPOINT_ITEMS_MAX:
            problems.append('%s: expected a list of at most %d items' % (field,CHECKPOINT_ITEMS_MAX));continue
        ids=[]
        for index,item in enumerate(items):
            path='%s[%d]' % (field,index)
            keys={'id','kind','text','source'} if field=='open_items' else {'id','reason','evidence'}
            if not isinstance(item,dict):
                problems.append('%s: expected an object with fields %s' % (path,', '.join(sorted(keys))));continue
            unknown=sorted(set(item)-keys);missing=sorted(keys-set(item));details=[]
            if unknown:details.append('unknown fields: '+field_names(unknown))
            if missing:details.append('missing fields: '+field_names(missing))
            if details:
                problems.append('%s: %s; allowed fields: %s' % (path,'; '.join(details),', '.join(sorted(keys))));continue
            try:ids.append(identity(item['id']))
            except ValueError as error:
                problems.append(str(error));continue
            # The path form `open_items[0].text` is the existing contract; the id is added after it.
            named=lambda name:'%s.%s (id %s)' % (path,name,item['id'])
            if field=='open_items':
                try:known=item['kind'] in KINDS
                except TypeError:known=False
                if not known:problems.append('%s.kind: expected one of %s' % (path,', '.join(sorted(KINDS))))
                attempt(text,item['text'],named('text'),CHECKPOINT_TEXT_LIMIT);attempt(text,item['source'],named('source'),CHECKPOINT_SOURCE_LIMIT)
            else:
                attempt(text,item['reason'],named('reason'),CHECKPOINT_TEXT_LIMIT);attempt(text,item['evidence'],named('evidence'),CHECKPOINT_SOURCE_LIMIT)
        if len(set(ids))!=len(ids):problems.append('Duplicate item IDs')
    try:size=len(canonical_bytes(p))
    except (TypeError,ValueError):size=None
    if size is not None and size>CHECKPOINT_MAX_BYTES:
        problems.append('Checkpoint: %d canonical bytes, the limit is %d (%d KB)' % (size,CHECKPOINT_MAX_BYTES,CHECKPOINT_MAX_BYTES//1000))
    return problems

def problems_message(problems):
    """One refusal for a list of problems: the sentence itself for one, a numbered list for more."""
    if len(problems)==1:return problems[0]
    shown=problems[:CHECKPOINT_PROBLEMS_MAX]
    more=' (+%d more)'%(len(problems)-len(shown)) if len(problems)>len(shown) else ''
    return (PROBLEMS_PREFIX%len(problems))+''.join(' [%d] %s'%(number,sentence) for number,sentence in enumerate(shown,1))+more

def split_problems(message):
    """The sentences of a refusal made by ``problems_message``; a single sentence is a list of one.

    The list is a convenience for the caller that sent the record. The only caller text in
    a sentence is a field name it chose itself; a name written to look like the next list
    number (``x [2] y``) can make this split its own refusal differently. Nothing else
    reads the result.
    """
    found=re.match(r'Invalid checkpoint \((\d+) problems\):',message or '')
    if not found:return [message] if message else []
    parts=re.split(r' \[(\d+)\] ',message[found.end():])
    sentences=[];expected=1
    for index in range(1,len(parts)-1,2):
        if parts[index]!=str(expected):
            # A "[n] " that is not the next number is part of the sentence before it.
            if sentences:sentences[-1]+=' [%s] %s'%(parts[index],parts[index+1])
            continue
        sentences.append(parts[index+1]);expected+=1
    if sentences:sentences[-1]=re.sub(r' \(\+\d+ more\)$','',sentences[-1])
    return sentences

def validate_checkpoint(p,task,require_digests=True):
    problems=checkpoint_problems(p,task)
    if problems:raise ValueError(problems_message(problems))

def transition(previous,current):
    old={x['id'] for x in previous['open_items']} if previous else set()
    now={x['id'] for x in current['open_items']}
    resolved={x['id'] for x in current['resolved']}
    if old-now!=resolved:raise ValueError('Carry every unresolved item forward or explicitly resolve/supersede it with reason and evidence')
    old_items={x['id']:x for x in previous['open_items']} if previous else {}
    if any(item['id'] in old_items and item!=old_items[item['id']] for item in current['open_items']):raise ValueError('Carry unresolved items unchanged; explicitly resolve/supersede changed items with new IDs')

def checkpoint_state(issue,normalize=True):
    """Parse once and follow links, irrespective of native comment ordering.

    Each retained provenance is normalized once for consumers of this read. The
    parent index avoids scanning every record for every checkpoint in the chain.
    """
    records={};invalid=[]
    for comment in issue.get('comments') or []:
        body=comment.get('text','')
        if not body.startswith(PREFIX):continue
        try:
            p=record_json.loads(body[len(PREFIX):]);validate_checkpoint(p,issue['id'])
            cid=str(comment['id'])
            if cid in records:raise ValueError('Duplicate checkpoint comment')
            records[cid]=(p,comment)
        except (ValueError,TypeError,KeyError):invalid.append(str(comment.get('id')))
    children={}
    for cid,(p,c) in records.items():children.setdefault(p['previous'],[]).append((cid,p,c))
    current=None;parent=None;visited=set();history=[];provenance={}
    while True:
        following=children.get(parent,[])
        if not following:break
        if len(following)!=1:raise ValueError('Conflicting checkpoint branches; reconcile history before briefing')
        cid,p,c=following[0]
        if cid in visited:raise ValueError('Checkpoint cycle')
        transition(current[0] if current else None,p)
        current=(p,c);parent=cid;visited.add(cid)
        history.append((cid,p))
        if normalize:provenance[cid]=normalize_provenance(p)
    if len(visited)!=len(records):raise ValueError('Unlinked checkpoint history; reconcile missing/conflicting revisions')
    return {'current':current,'invalid':invalid,'history':history,'provenance':provenance}

def checkpoints(issue):
    state=checkpoint_state(issue)
    return state['current'],state['invalid']

def excluding_checkpoint(data,cid):
    """Reuse the snapshot and its entry hashes when excluding its newest receipt."""
    eid=data['task']+'-c'+str(cid)
    return dict(data,entries=[e for e in data['entries'] if e['entry_id']!=eid],
                entry_digests={k:v for k,v in entry_digests(data).items() if k!=eid})

def clip(value,limit):
    value=str(value or '')
    return {'text':value[:limit],'omitted_chars':max(0,len(value)-limit)}

NEWER_MAX=5
# Bounded provenance window: a checkpoint records exact digests for the most
# recent incorporated entries and a rolling chain hash covering all older ones,
# so the record stays within the checkpoint size cap on long-lived tasks while
# remaining tamper-evident over the full history.
DIGEST_WINDOW=200
# Cap for the FINAL serialized record, after the server adds provenance. Both
# the write path (which shrinks the provenance window to fit) and the read path
# (checkpoints()) enforce this same value, so an accepted write always reads back.
RECORD_MAX=80000
DIRECTION_STATES=('acknowledged','resolved','superseded')
RESOLUTION_STATES=('resolved','superseded')
DIRECTIONS_MAX=100
ZERO_HASH='0'*64

def provenance_chain(digests):
    """Rolling hash over entry_id:digest pairs in sorted order, tamper-evident."""
    h=ZERO_HASH
    for k in sorted(digests):
        h=content_hash({'chain':h,'entry_id':k,'digest':digests[k]})
    return h

OLDER_MAX=300
OLDER_DIGEST=16

def valid_digest_map(values,width):
    return all(isinstance(k,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,200}',k)
               and isinstance(v,str) and re.fullmatch(r'[a-f0-9]{'+str(width)+r'}',v) for k,v in values.items())

def bounded_digests(digests,window=DIGEST_WINDOW,older=OLDER_MAX):
    """Bounded provenance: an exact digest window, a bounded truncated-digest map of
    evicted entries (so old edits/late arrivals stay verifiable per entry), and a
    chain hash over everything outside the window. Entries beyond `older` are
    covered only by the chain and are reported as unverified rather than fresh."""
    keys=sorted(digests)
    keep=keys[-window:] if window>0 else []
    kept=set(keep)
    rest=[k for k in keys if k not in kept]
    prefix={k:digests[k][:OLDER_DIGEST] for k in rest[-older:]} if older>0 else {}
    return {'digests':{k:digests[k] for k in keep},'older':prefix,
            'chain':provenance_chain({k:digests[k] for k in rest}),'covered':len(digests),'window':len(keep)}

def normalize_provenance(p):
    """Normalize a checkpoint's recorded provenance for coverage analysis.

    Returns {'digests','older','chain','covered','verifiable','complete'} or None
    when unknown (legacy checkpoint with no provenance). `older` holds truncated
    digests for evicted entries, so edits to old history are still detected;
    entries beyond both bounds are covered only by the chain and are reported as
    unverified rather than as fresh.
    """
    if 'incorporated_digests' in p:
        d=dict(p['incorporated_digests'])
        return {'digests':d,'older':{},'chain':provenance_chain({}),'covered':len(d),'verifiable':len(d),'complete':True}
    prov=p.get('provenance')
    if isinstance(prov,dict) and isinstance(prov.get('digests'),dict):
        d=dict(prov['digests']);older=dict(prov.get('older') or {})
        covered=prov.get('covered',len(d))
        return {'digests':d,'older':older,'chain':prov.get('chain',provenance_chain({})),'covered':covered,
                'verifiable':len(d)+len(older),'complete':covered==len(d)}
    return None

# Backwards-compatible alias used by the work queue.
parse_provenance=normalize_provenance

def checkpoint_history(issue):
    """Valid payloads in linked chain order, oldest first."""
    return checkpoint_state(issue)['history']

def chain_dispositions(payloads):
    """Authoritative dispositions merged across the whole checkpoint chain.

    Bounding a single payload must never erase an earlier recorded disposition:
    a resolution retired from the newest record to respect the cap is still
    authoritative from the checkpoint that recorded it. Newest value wins per
    entry; a later edit still invalidates via digest mismatch.
    """
    merged={}
    for _cid,payload in payloads:
        for d in (payload.get('carried') or []):merged[d['id']]=d
        for d in (payload.get('directions') or []):merged[d['id']]=d
    return merged

def chain_evidence(payloads,normalized=None):
    """Retained historical evidence for entry content, merged across the chain.

    Newest evidence wins per entry, even when truncated evidence replaces an
    older exact digest. A newest legacy checkpoint cannot claim incorporation
    using evidence belonging to an earlier checkpoint.
    """
    exact={};trunc={};recorded=False
    for cid,payload in payloads:
        prov=normalized[cid] if normalized is not None else normalize_provenance(payload)
        recorded=prov is not None
        if prov is None:
            exact.clear();trunc.clear()
            continue
        for k,v in prov['digests'].items():exact[k]=v;trunc.pop(k,None)
        for k,v in prov['older'].items():trunc[k]=v;exact.pop(k,None)
    if not recorded:return {},{},False
    return exact,trunc,recorded

def direction_index(checkpoint_payload):
    """Effective dispositions: server-carried entries plus the caller's own recorded
    request entries, which take precedence. The two are stored separately so an
    original request's identity can be reconstructed independently of dispositions
    the server carried forward later."""
    merged={}
    for d in (checkpoint_payload.get('carried') or []):merged[d['id']]=d
    for d in (checkpoint_payload.get('directions') or []):merged[d['id']]=d
    return merged

def recorded_directions(checkpoint_payload):
    """The caller-recorded request fields only (excludes server-carried entries)."""
    return list(checkpoint_payload.get('directions') or [])

def classify_entry(entry,prov):
    """Classify one current entry against recorded provenance.

    Returns 'incorporated', 'changed' or 'unverified'. `unverified` means the entry
    is covered only by the chain hash (beyond both the exact window and the
    truncated map), so its current content cannot be re-verified per entry.
    """
    digest=content_hash(entry)
    eid=entry['entry_id']
    if eid in prov['digests']:return 'incorporated' if prov['digests'][eid]==digest else 'changed'
    if eid in prov['older']:return 'incorporated' if prov['older'][eid]==digest[:OLDER_DIGEST] else 'changed'
    return 'unverified'

def unincorporated(data,prov):
    """Classify current snapshot entries against a checkpoint's bounded provenance.

    Returns (unincorporated, fresh, changed_late, unverified). An entry that cannot
    be re-verified (beyond both recorded bounds while the checkpoint covered more
    entries than it can verify) is counted as `unverified`, never as fresh/other,
    so counts are never asserted as complete when coverage is bounded.
    """
    if prov is None:return data['entries'],[],[],0
    # Entries the checkpoint cannot re-verify at all: covered beyond both recorded
    # bounds. When zero, a missing ID is genuinely new and can be called fresh.
    unverifiable=prov['covered']-prov['verifiable']
    fresh=[];changed=[];unverified=0
    for e in data['entries']:
        verdict=classify_entry(e,prov)
        if verdict=='changed':changed.append(e)
        elif verdict=='incorporated':pass
        elif unverifiable==0:fresh.append(e)
        else:unverified+=1
    return fresh+changed,fresh,changed,unverified

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

def direction_context(issue,state):
    """Apply the migration baseline and observed checkpoint owner boundaries.

    The newest legacy checkpoint is an unknown-history baseline. Previous owners
    are recorded by new checkpoints; old checkpoints use their native author.
    A previous owner's entries at/before their last checkpoint are not directions
    for the new owner. Later comments still require explicit dispositions.
    """
    current=state['current']
    owner=issue.get('assignee') or (current[1].get('author') if current else None)
    baseline=None;previous={};known_by_stamp={}
    comments={str(c['id']):c for c in issue.get('comments') or []}
    stamps={issue['id']+'-c'+cid:parse_moment(c['created_at']) for cid,c in comments.items()}
    for cid,p in state['history']:
        c=comments[cid];stamp=parse_moment(c['created_at'])
        prov=state['provenance'][cid]
        if prov is None:baseline=stamp
        else:
            for eid in set(prov['digests'])|set(prov['older']):
                if eid in stamps:known_by_stamp.setdefault(stamps[eid],set()).add(eid)
        old=p.get('direction_owner',c.get('author'))
        # A record may bind the observed assignee even when another actor wrote
        # it. Only the owner's OWN checkpoint can cut off that owner's comments;
        # an operator/reviewer checkpoint must not hide later owner instructions.
        if old and old!=owner and c.get('author')==old:
            previous[old]=(stamp,frozenset(known_by_stamp.get(stamp,())))
    return owner,baseline,previous

def direction_entries(data,context):
    owner,baseline,previous=context
    return [e for e in data['entries']
            if (baseline is None or parse_moment(e['timestamp'])>=baseline)
            # Native timestamps may have only second precision. A tied comment
            # may have arrived AFTER the owner's checkpoint; retain it rather
            # than inventing an ordering from its opaque native ID.
            and (e['author'] not in previous
                 or parse_moment(e['timestamp'])>previous[e['author']][0]
                 or (parse_moment(e['timestamp'])==previous[e['author']][0]
                     and e['entry_id'] not in previous[e['author']][1]))]

def retained_provenance(state):
    exact,trunc,recorded=chain_evidence(state['history'],state['provenance'])
    if not recorded:return None
    latest=state['provenance'][state['history'][-1][0]]
    return dict(latest,digests=exact,older=trunc,verifiable=len(exact)+len(trunc),
                complete=not trunc and len(exact)>=latest['covered'])

def newer_activity_summary(data,prov,checkpoint_timestamp,owner,direction_idx=None,direction_data=None):
    """Bounded, per-entry view of activity the current checkpoint did not incorporate.

    `prov` is the checkpoint's normalized provenance, so edited entries (same ID,
    new content) and late arrivals backdated before the checkpoint are identified
    exactly within the recorded window; entries older than an evicted window are
    reported as an explicit `unverified_count` rather than being called fresh, so
    bounded coverage is never presented as complete. Directions from other actors
    are tracked to an explicit acknowledged/resolved/superseded disposition; every
    collection is bounded with an explicit omitted count. Reading changes nothing.
    """
    coverage='unknown' if prov is None else ('snapshot' if prov['complete'] else 'windowed')
    if prov is None and checkpoint_timestamp:
        data=dict(data,entries=[e for e in data['entries']
                               if parse_moment(e['timestamp'])>=parse_moment(checkpoint_timestamp)])
    entries,fresh,changed_late,unverified=unincorporated(data,prov)
    own=[e for e in entries if e['author']==owner]
    others=[e for e in entries if e['author']!=owner]
    authors=sorted({e['author'] for e in others})
    refs=[{'entry_id':e['entry_id'],'kind':e['kind'],'timestamp':e['timestamp'],'author':clip(e['author'],96),
           'changed':any(c is e for c in changed_late)} for e in entries[:NEWER_MAX]]
    history='history '+data['task']
    note=('Activity exists that the saved checkpoint did not incorporate; its next_action below may be stale. '
          'Entry refs are the oldest unincorporated ones with per-entry authors; counts cover the full snapshot, including edited and late backdated entries the excerpt omits. '
          'Reading clears nothing: acknowledging a direction, reconciling it into a new checkpoint and completing it stay separate explicit acts.')
    if coverage=='unknown':
        note+=' Coverage is UNKNOWN: this checkpoint predates per-entry digests, so reconcile with history before trusting the counts.'
    elif coverage=='windowed':
        note+=(' Coverage is WINDOWED: '+str(unverified)+' older entries have no per-entry evidence recorded by this checkpoint, so their current content is unverified and they are excluded from the fresh/changed counts. '
               'Run "checkpoint '+data['task']+' --verify" for the classification supported by evidence retained across the task\'s checkpoints (entries no checkpoint ever recorded stay unknown).')
    result={'own_count':len(own),'other_count':len(others),
            'fresh_count':len(fresh),'changed_or_late_count':len(changed_late),
            'unverified_count':unverified,
            'coverage':coverage,
            'other_authors':{'items':[clip(a,96) for a in authors[:NEWER_MAX]],'omitted':max(0,len(authors)-NEWER_MAX)},
            'entries':refs,'omitted':max(0,len(entries)-NEWER_MAX),
            'history':history,
            'history_new':'history '+data['task']+' --since '+utc_text(parse_moment(checkpoint_timestamp,'checkpoint timestamp')) if checkpoint_timestamp else history,
            'verify':'checkpoint '+data['task']+' --verify',
            'note':note}
    if direction_idx is not None:
        outstanding=unresolved_directions(data['entries'] if direction_data is None else direction_data,direction_idx,owner)
        result['unresolved_directions']={'total':len(outstanding),
            'items':[{'entry_id':e['entry_id'],'timestamp':e['timestamp'],'author':clip(e['author'],96),
                      'state':(direction_idx.get(e['entry_id']) or {}).get('state','unacknowledged')} for e in outstanding[:NEWER_MAX]],
            'omitted':max(0,len(outstanding)-NEWER_MAX)}
    return result

def brief(rows,project,task,offset=0,limit=5,operators=None,journal=None,verifiers=None,actor=None):
    if type(offset) is not int or offset<BRIEF_ITEM_OFFSET_MIN or type(limit) is not int or not BRIEF_ITEM_LIMIT_MIN<=limit<=BRIEF_ITEM_LIMIT_MAX:
        raise ValueError('Invalid unresolved-item page: --items-offset must be >= %d and --items-limit must be %d..%d' % (BRIEF_ITEM_OFFSET_MIN,BRIEF_ITEM_LIMIT_MIN,BRIEF_ITEM_LIMIT_MAX))
    issue=task_row(rows,task);state=checkpoint_state(issue);current=state['current'];invalid=state['invalid']
    if issue.get('issue_type')=='event':raise ValueError('Use history/show for an event; brief requires a task or job')
    data=snapshot(rows,project,task);p,c=current if current else (None,None)
    facts=next(r for r in project_facts(rows) if r['id']==task)
    items=p['open_items'] if p else []
    if offset>len(items):raise ValueError('Unresolved-item offset exceeds total')
    deps=[d for d in (issue.get('dependencies') or []) if d.get('type')!='parent-child']
    from work import workflow
    from review_state import is_integration_warning, scopes_for
    from guidance import brief_block
    review=workflow(issue,scopes_for(rows,task),operators=operators,journal=journal)
    # ORIGINAL meaning: does the scope currently shown in `lifecycle`/`lifecycle_scope`
    # (the newest recorded scope) belong to the current contribution? The ANY-scope
    # answer is additive as `review.integration.matches_contribution`.
    matches_contribution=None if not review.get('contribution') else (facts['scope'] or {}).get('source_commit','').lower()==review['contribution']['commit'].lower()
    # A passed deployed fact is always scoped to a release; name that delivery,
    # and say plainly when it is not the task's current contribution (the release
    # may have shipped a superseded revision). Additive fields (kittrial-5bb.95).
    # A superseded release reads deployed=unknown through project_facts, so the
    # delivery is None and `deployed_live` says why (rev2 item 2).
    deployed_scope=facts['scope'] if facts['facts']['deployed']['value']=='passed' else None
    # The four scope fields stay PLAIN STRINGS clipped to 160 characters: the field
    # shipped a version ago as strings, and changing it to excerpt objects was a
    # shape change for a released field (rev2 item 6.1).
    deployed_delivery=None if deployed_scope is None else {key:str(deployed_scope.get(key) or '')[:160] for key in ('release_id','environment','source_commit','integration_commit')}
    current_commit=(review.get('contribution') or {}).get('commit')
    deployed_current=None if (deployed_scope is None or not current_commit) else deployed_scope.get('source_commit','').lower()==current_commit.lower()
    deployed_live=facts.get('live','unknown')
    pending=review.get('pending_requests',[])
    priors=review.get('prior_contributions') or []
    # The bounded slice keeps its PRIOR_BRIEF_LIMIT bound and adds the per-prior
    # `integration` block the shared projection now carries, so the compact read
    # says whether a replaced revision is integrated without re-reading review TASK.
    bounded_priors=[{key:c[key] for key in ('comment_id','commit','relation','timestamp','integration')}
                    for c in priors[-PRIOR_BRIEF_LIMIT:]]
    review_next={'changes-requested':'Address the outstanding review requests for the current contribution; read review '+task+'.',
                 'awaiting-review':'Reviewer: retrieve and verify the current contribution, then record review feedback or approval.',
                 'awaiting-integration':'Authorized integrator: integrate the approved contribution and record scoped integration evidence.',
                 'integrated':'Integration is recorded for this contribution; follow the project release/deployment workflow and scoped lifecycle evidence.',
                 'withdrawn':'The current contribution was withdrawn by its author or a coordinator; read review '+task+' or deliver a new revision.',
                 'superseded':'The current contribution was marked superseded; read review '+task+' or deliver a new revision.'}
    # The integration overlay warnings (kittrial-5bb.52) are part of the shared
    # projection's warnings; surface them in the compact read too, so a revert, a
    # resolved-elsewhere fact conflict or an ignored revert record is visible
    # without reading review TASK.
    integration_warnings=[w for w in review.get('warnings') or [] if is_integration_warning(w)]
    # The host-issued revert list is bounded the same way the prior-contribution
    # chain is; the untruncated count stays available beside the slice.
    reverts=review.get('reverts') or []
    # Reference catalog review-by attention (.41 7.1): at most 3 trust-marked items,
    # server-derived text only; it never touches the checkpoint item vocabulary.
    from reference_records import brief_attention
    attention=brief_attention(rows,issue,operators)
    # Drafts that match the task are a number only (kittrial-5bb.98): no key, title or text.
    reference_drafts=attention.get('reference_drafts_matching',0)
    # Contributed requirement proposals (.58 5.2): up to 3 more items of their own kind,
    # after the reference items; the totals count both kinds.
    from proposal_records import brief_attention as proposal_attention
    proposals=proposal_attention(rows,issue,actor,operators,project=journal)
    attention={'attention':attention['attention']+proposals['attention'],
               'attention_total':attention['attention_total']+proposals['attention_total'],
               'attention_more':((attention['attention_more'] or 0)+(proposals['attention_more'] or 0)) or None}
    # Capability index (.60 section 8, kittrial-5bb.76): up to 3 more items of their own
    # kind, after the other kinds; the totals count every kind.
    from capability_records import brief_attention as capability_attention
    capabilities=capability_attention(rows,issue,operators,verifiers=verifiers,project=journal)
    attention={'attention':attention['attention']+capabilities['attention'],
               'attention_total':attention['attention_total']+capabilities['attention_total'],
               'attention_more':((attention['attention_more'] or 0)+(capabilities['attention_more'] or 0)) or None}
    newer=None;excluded_cursor=None;next_action=review_next.get(review['review_state'],p['next_action'] if p else 'Read the task description, acceptance criteria and any history, then publish a checkpoint.')
    # Outstanding directions persist independently of cursor freshness: an
    # acknowledged-but-unresolved direction stays visible on a current checkpoint,
    # and it qualifies the next action even when the activity cursor is current.
    dir_out=None
    context=direction_context(issue,state);direction_data=direction_entries(data,context)
    if p is not None:
        dispositions=chain_dispositions(state['history'])
        outstanding=unresolved_directions(direction_data,dispositions,context[0])
        dir_out={'total':len(outstanding),
                 'items':[{'entry_id':e['entry_id'],'timestamp':e['timestamp'],'author':clip(e['author'],96),
                           'state':(dispositions.get(e['entry_id']) or {}).get('state','unacknowledged')} for e in outstanding[:NEWER_MAX]],
                 'omitted':max(0,len(outstanding)-NEWER_MAX)}
        if dir_out['total']:
            ids=', '.join(i['entry_id'] for i in dir_out['items'][:NEWER_MAX])
            ids+=(f' (+{dir_out["omitted"]} more)' if dir_out['omitted'] else '')
            next_action='OUTSTANDING DIRECTIONS ('+str(dir_out['total'])+'): '+ids+' — resolve or supersede with evidence. '+next_action
    if p is not None:
        excluded=excluding_checkpoint(data,c['id'])
        excluded_cursor=activity_cursor(excluded)
        stale=not cursor_matches(p['activity_cursor'],excluded,compatible=excluded_cursor)
        if stale:
            newer=newer_activity_summary(excluded,retained_provenance(state),c.get('created_at'),context[0],dispositions,direction_data)
            if review['review_state'] not in review_next:
                dirs=newer.get('unresolved_directions')
                dir_note='' if not dirs else '; '+str(dirs['total'])+' outstanding direction(s) not yet resolved/superseded'
                next_action=('STALE CHECKPOINT: activity the checkpoint did not incorporate exists ('+str(newer['other_count'])
                             +' by other actors, '+str(newer['own_count'])+' own'+dir_note
                             +('' if newer['coverage']!='unknown' else ', coverage UNKNOWN — reconcile with history before trusting counts')
                             +'). Read newer activity first: '+newer['history']
                             +'. The recorded checkpoint next action was: '+p['next_action'])
    result={**attention,'reference_drafts_matching':reference_drafts,'task':task,'title':clip(issue.get('title'),200),'owner':clip(issue.get('assignee') or 'unassigned',96),'status':issue.get('status'),
            'activity_cursor':activity_cursor(data),'checkpoint':None if p is None else {'comment_id':str(c['id']),'author':clip(c.get('author'),96),'timestamp':c.get('created_at'),'source_commit':p['source_commit'],'branch':p['branch'],'incorporated_activity_cursor':p['activity_cursor'],
                'newer_activity':stale},
            'newer':newer,
            'directions':dir_out,
            'intent':clip(p['intent'] if p else issue.get('description'),600),'acceptance':clip(p['acceptance'] if p else issue.get('acceptance_criteria'),1000),
            'current_position':p['summary'] if p else 'No checkpoint yet; current position and unresolved items have not been summarized.',
            'next_action':next_action,
            'unresolved':{'coverage':'explicit checkpoint items only; unsummarized prose is not classified','total':len(items) if p else None,'items':items[offset:offset+limit],'next_offset':offset+limit if offset+limit<len(items) else None},
            'review':dict(review,pending_requests=pending[:5],pending_total=len(pending),more='review '+task if len(pending)>5 else None,
                          prior_contributions=bounded_priors,prior_contributions_total=len(priors),
                          prior_contributions_more='review '+task if len(priors)>PRIOR_BRIEF_LIMIT else None,
                          reverts=reverts[-REVERT_BRIEF_LIMIT:],reverts_total=len(reverts),
                          reverts_more='review '+task if len(reverts)>REVERT_BRIEF_LIMIT else None),
            'lifecycle_matches_contribution':matches_contribution,
            'deployed_delivery':deployed_delivery,
            'deployed_delivery_is_current_contribution':deployed_current,
            'deployed_live':deployed_live,
            'dependencies':{'total':len(deps),'items':[{k:clip(d.get(k),160) for k in ('depends_on_id','type')} for d in deps[:8]],'omitted':max(0,len(deps)-8)},
            'lifecycle':{dim:dict(value=f['value'],event_id=f['event_id']) for dim,f in facts['facts'].items()},
            'lifecycle_scope':{k:clip(v,160) for k,v in (facts['scope'] or {}).items()},
            'warnings':(['Malformed checkpoint comments ignored: '+', '.join(invalid[:5])] if invalid else [])
                       +integration_warnings
                       +(['Direction baseline: comments strictly before the newest legacy checkpoint have UNKNOWN coverage and are not outstanding directions; timestamp ties remain possible directions.'] if context[1] is not None else [])
                       +['Newer activity also includes edits, deletions or changed task fields. Prose resolutions never silently clear explicit items.'],
            'evidence':{'issue':'show '+task,'history':'history '+task,'checkpoint_entry':task+'-c'+str(c['id']) if c else None}}
    if journal is not None:
        # The standing guidance channel (kittrial-5bb.99): every endpoint brief
        # carries the current version; a direct library call with no project path
        # cannot read the guidance and omits the block.
        result['guidance']=brief_block(journal,actor)
    return result

def fit_provenance(payload,digests,previous=None):
    """Attach provenance to `payload`, shrinking the digest window until the FINAL
    serialized record fits RECORD_MAX. The chain always covers every entry outside
    the window, so coverage stays truthful at any window size. Returns the fitted
    payload; raises an actionable error when the non-provenance payload alone is
    already over the cap.
    """
    window=DIGEST_WINDOW
    while True:
        prov=bounded_digests(digests,window)
        if previous is not None:
            exact,trunc=previous
            prov['digests']={k:v for k,v in prov['digests'].items() if exact.get(k)!=v}
            prov['older']={k:v for k,v in prov['older'].items()
                           if exact.get(k)!=digests[k] and trunc.get(k)!=v}
            prov['window']=len(prov['digests']);prov['delta']=True
        candidate=dict(payload,provenance=prov)
        size=len(canonical_bytes(candidate))
        if size+len(PREFIX.encode())<=RECORD_MAX:return candidate,size
        if window==0:
            raise ValueError('Checkpoint record would be '+str(size)+' bytes, over the '+str(RECORD_MAX)+'-byte record cap, even with an empty provenance window; '
                             'shorten intent/acceptance/summary/next_action or reduce open items/directions')
        window=window//2

def request_identity(payload):
    """Canonical request bytes ignoring server-derived fields — provenance and
    server-carried dispositions — so an original request reconciles against its own
    committed receipt even after later checkpoints or edits, while a changed
    payload still differs."""
    body={k:v for k,v in payload.items() if k not in ('incorporated_digests','provenance','carried','direction_owner')}
    return canonical_bytes(body)

def save_checkpoint(rows,project,task,p,actor,run,provenance_writes=False):
    validate_checkpoint(p,task,require_digests=False);issue=task_row(rows,task)
    from coordination import is_merge_slot,merge_slot_sentence
    if is_merge_slot(issue):raise ValueError(merge_slot_sentence(task)+'; it takes no checkpoint')
    if 'carried' in p or 'direction_owner' in p:raise ValueError('carried and direction_owner are server-derived; omit them from the request')
    state=checkpoint_state(issue);current=state['current'];invalid=state['invalid']
    if invalid:raise ValueError('Malformed checkpoint entries require correction before publishing another checkpoint')
    snap=snapshot(rows,project,task)
    computed=entry_digests(snap)
    # Exact-request identity against the request's OWN committed receipt: reconcile
    # before any current-state guard (direction digests, previous checkpoint,
    # activity cursor), so a retry succeeds even after later checkpoints carry new
    # dispositions or an already-saved direction is edited. Server-derived fields
    # (provenance, carried dispositions) are excluded from identity; a changed
    # payload still differs and is not reconciled.
    body={k:v for k,v in p.items() if k not in ('incorporated_digests','provenance','carried','direction_owner')}
    identity=request_identity(body)
    comments={str(c['id']):c for c in issue.get('comments') or []}
    for cid,existing in state['history']:
        c=comments[cid]
        if c.get('author')!=actor:continue
        if request_identity(existing)==identity:
            recorded=state['provenance'][cid] or {'covered':0}
            return {'comment_id':str(c['id']),'reconciled':True,'covered':recorded['covered'],'bytes':len(str(c['text']))}
    # Direction dispositions must reference the digests actually incorporated for a
    # NEW write (a stored receipt was already validated when it was written).
    if not provenance_writes and any(k in p for k in ('incorporated_digests','provenance','directions')):
        raise ValueError('checkpoint_provenance_writes is off (the installation default). Readers accept the new shape; '
                         'omit provenance/directions to write an older-kit-compatible checkpoint. '
                         'An operator enables the installation switch only after the rollback target can read these fields.')
    if not provenance_writes and any(normalize_provenance(old) is not None for _cid,old in state['history']):
        raise ValueError('This task already has checkpoint provenance records; new legacy writes are refused while '
                         'checkpoint_provenance_writes is off. Re-enable it to preserve directions. '
                         'Disabling the switch cannot make this task readable by a pre-provenance kit.')
    if p.get('directions'):
        if issue.get('assignee')!=actor:raise ValueError('Only the current task assignee may record direction dispositions')
        for d in p['directions']:
            if d['id'] not in computed:raise ValueError('Direction '+d['id']+' is not an entry on this task')
            entry=next(e for e in snap['entries'] if e['entry_id']==d['id'])
            if (entry['kind']!='comment' or entry['author']==actor
                    or str(entry.get('body','')).startswith(('Kind: task-checkpoint-v1','Kind: contribution-review-v1'))):
                raise ValueError('Direction '+d['id']+' must target another actor\'s ordinary comment, not own activity or a checkpoint/review record')
            if d['digest']!=computed[d['id']]:
                raise ValueError('Direction '+d['id']+' digest does not match the incorporated entry; reconcile before resolving')
    # Bounded carry with explicit retirement: dispositions from the previous
    # checkpoint are carried in a separate server field (so recorded request fields
    # stay distinct), retired only when they are already resolved/superseded, and
    # never silently dropped while unresolved. Overflow beyond the cap is refused
    # with guidance instead of producing an unreadable record.
    carried=chain_dispositions(state['history'])
    effective={k:dict(v) for k,v in carried.items()}
    requested={d['id'] for d in (p.get('directions') or [])}
    for d in (p.get('directions') or []):effective[d['id']]=dict(d)
    if len(effective)>DIRECTIONS_MAX:
        # Order by the last explicit disposition in the linked chain. Repeating a
        # server-carried entry does not make it younger. Protect every entry in
        # this request, including an edited resolution retired from an older one.
        age={}
        for index,(_cid,old) in enumerate(state['history']):
            for d in old.get('directions') or []:age[d['id']]=index
        for key in sorted(effective,key=lambda k:(age.get(k,-1),k)):
            if len(effective)<=DIRECTIONS_MAX:break
            if key not in requested and effective[key]['state'] in RESOLUTION_STATES:del effective[key]
        if len(effective)>DIRECTIONS_MAX:
            unresolved=sorted(k for k,v in effective.items() if v['state'] not in RESOLUTION_STATES)
            raise ValueError('Merged dispositions would exceed the '+str(DIRECTIONS_MAX)+'-entry cap with '+str(len(unresolved))
                             +' unresolved direction(s) carried; resolve or explicitly retire them before adding more')
    # Completed dispositions remain authoritative in history; only unresolved
    # acknowledgements need repeating in the bounded newest payload.
    inherited={k:v for k,v in effective.items() if k not in requested and v['state'] not in RESOLUTION_STATES}
    if inherited:body['carried']=sorted(inherited.values(),key=lambda d:d['id'])
    # Authoritative binding for new writes: the caller may not assert which
    # entries the checkpoint incorporated. Accept either the exact full digest map
    # (complete coverage) or the bounded provenance returned by
    # "checkpoint TASK --provenance"; anything missing, extra or altered is
    # rejected, so fabricated maps stay rejected.
    supplied=normalize_provenance(p)
    if supplied is not None:
        full_ok=('incorporated_digests' in p) and supplied['digests']==computed
        bounded=bounded_digests(computed)
        bounded_ok=(isinstance(p.get('provenance'),dict) and p['provenance']==bounded)
        if not (full_ok or bounded_ok):
            raise ValueError('incorporated_digests/provenance does not match the current snapshot; fetch it with "checkpoint TASK --provenance" and embed the returned provenance verbatim')
    if provenance_writes:
        body['direction_owner']=issue.get('assignee') or actor
        exact,trunc,recorded=chain_evidence(state['history'],state['provenance'])
        payload,size=fit_provenance(body,computed,(exact,trunc) if recorded else None)
    else:
        payload={k:v for k,v in body.items() if k not in ('directions','carried')}
        size=len(canonical_bytes(payload))
    # Validate the COMPLETE final server-normalized record (carried arrays, schema,
    # caps and byte size) before any native mutation, so an accepted write is
    # always readable by the same reader.
    validate_checkpoint(payload,task,require_digests=False)
    previous=str(current[1]['id']) if current else None
    if p['previous']!=previous:raise ValueError('Stale previous checkpoint; read brief again')
    if p['activity_cursor']!=activity_cursor(snap):raise ValueError('Activity changed; read/reconcile history and obtain a fresh activity cursor')
    transition(current[0] if current else None,payload)
    result=json.loads(run(['comments','add',task,PREFIX+canonical_bytes(payload).decode(),'--json']))
    return {'comment_id':str(result['id']),'reconciled':False,'covered':payload.get('provenance',{}).get('covered',0),'bytes':size}

def verify_checkpoint(rows,project,task):
    """Verify retained evidence using one linked, parsed checkpoint state."""
    state=checkpoint_state(task_row(rows,task));current=state['current']
    if state['invalid']:raise ValueError('Malformed checkpoint entries require correction before verification')
    snap=snapshot(rows,project,task)
    if current is None:
        return {'task':task,'checkpoint':None,'coverage':'none',
                'fresh':len(snap['entries']),'changed':0,'unchanged':0,'unverified':0,
                'note':'No checkpoint exists; every entry is unincorporated.'}
    cid=str(current[1]['id']);snap=excluding_checkpoint(snap,cid);total=len(snap['entries'])
    exact,trunc,recorded=chain_evidence(state['history'],state['provenance'])
    if not recorded:
        return {'task':task,'checkpoint':cid,'coverage':'unknown',
                'fresh':0,'changed':0,'unchanged':0,'unverified':total,
                'verified_entries':0,'recorded_coverage':'unknown',
                'note':'The newest checkpoint has no per-entry evidence (legacy record). Older evidence cannot '
                       'establish what it incorporated. Coverage is UNKNOWN: '+str(total)+' entries were NOT checked. '
                       'Publish a checkpoint to record evidence for current history; reconcile manually via history/show.'}
    latest=retained_provenance(state)
    gap=latest['covered']>latest['verifiable']
    fresh=changed=unchanged=unverified=0;changed_ids=[]
    for eid,digest in entry_digests(snap).items():
        if eid in exact:
            same=exact[eid]==digest
        elif eid in trunc:
            same=trunc[eid]==digest[:OLDER_DIGEST]
        else:
            if gap:unverified+=1
            else:fresh+=1
            continue
        if same:unchanged+=1
        else:changed+=1;changed_ids.append(eid)
    bounded=unverified>0
    return {'task':task,'checkpoint':cid,'coverage':'bounded' if bounded else 'verified',
            'fresh':fresh,'changed':changed,'unchanged':unchanged,'unverified':unverified,
            'changed_entry_ids':changed_ids[:NEWER_MAX],'changed_omitted':max(0,len(changed_ids)-NEWER_MAX),
            'verified_entries':unchanged+changed,'recorded_coverage':'retained-across-chain',
            'note':('Checked '+str(unchanged+changed)+' of '+str(total)+' entries against the newest per-entry '
                    'evidence retained in the linked checkpoint chain. '
                    +('COVERAGE IS BOUNDED: '+str(unverified)+' entries have no retained per-entry evidence; '
                      'their content was NOT checked and remains unverified. ' if bounded else '')
                    +'There is no shortcut that verifies never-recorded entries: publish a checkpoint to record '
                    'current evidence; reconcile history manually via history/show.')}

def checkpoint_queue_fields(rows,row,state=None):
    """Bounded queue fields; share the brief's linked state and full-row snapshot."""
    empty={'newer_activity_by_others':None,'newer_activity_own':None,
           'newer_activity_coverage':None,'unresolved_directions':None}
    try:
        if state is None:state=checkpoint_state(row)
        elif not state['provenance']:
            state=dict(state,provenance={cid:normalize_provenance(p) for cid,p in state['history']})
        if state['current'] is None or state['invalid']:return empty
        p,c=state['current'];project=untoken(p['activity_cursor']).get('project','work-queue')
        data=excluding_checkpoint(snapshot(rows,project,row['id']),c['id'])
        dispositions=chain_dispositions(state['history'])
        context=direction_context(row,state);direction_data=direction_entries(data,context)
        if cursor_matches(p['activity_cursor'],data):own=others=0;coverage='current'
        else:
            newer=newer_activity_summary(data,retained_provenance(state),c.get('created_at'),
                                         context[0],dispositions,direction_data)
            own=newer['own_count'];others=newer['other_count'];coverage=newer['coverage']
        return {'newer_activity_by_others':others,'newer_activity_own':own,
                'newer_activity_coverage':coverage,
                'unresolved_directions':len(unresolved_directions(direction_data,dispositions,context[0]))}
    except (ValueError,TypeError,KeyError):return empty

def history_page(data,project,task,limit=5,since=None,cursor=None,body_budget=4000):
    if type(limit) is not int or not HISTORY_LIMIT_MIN<=limit<=HISTORY_LIMIT_MAX:raise ValueError('History limit must be %d..%d' % (HISTORY_LIMIT_MIN,HISTORY_LIMIT_MAX))
    if type(body_budget) is not int or not HISTORY_BUDGET_MIN<=body_budget<=HISTORY_BUDGET_MAX:raise ValueError('History body budget must be %d..%d' % (HISTORY_BUDGET_MIN,HISTORY_BUDGET_MAX))
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
    hints={}
    def error(self,message):
        hint=next((text for flag,text in self.hints.items() if flag in message),None)
        raise ValueError(message+('; hint: '+hint if hint else ''))

def parse_args(action,args):
    parser=Parser(add_help=False);parser.hints=BRIEF_MISTAKEN_FLAGS if action=='brief' else {}
    parser.add_argument('task');parser.add_argument('--json',action='store_true')
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
    guidance=result.get('guidance')
    if guidance and guidance.get('present') and guidance.get('unbound'):
        # Unbound text has no version anyone may follow (kittrial-5bb.105); the warning line below says why.
        lines.append('Guidance: present but withheld until the operator repairs it | attention: True')
    elif guidance and guidance.get('present'):
        # A same-text repair keeps the original setter; show the repair beside it (kittrial-5bb.121).
        repaired=(' (repaired by %s at %s)'%(guidance['repaired_by'],guidance.get('repaired_at') or 'unknown')
                  if guidance.get('repaired_by') else '')
        lines.append('Guidance: version %s set by %s at %s%s | acknowledged: %s | attention: %s'%(
            guidance['version'],guidance.get('set_by') or 'unknown',guidance.get('set_at') or 'unknown',repaired,
            guidance.get('acknowledged'),guidance.get('attention')))
    if guidance and guidance.get('warning'):
        lines.append('Guidance warning: '+guidance['warning'])
    cp=result['checkpoint']
    if cp:lines += [f'Checkpoint: {cp["comment_id"]} by {excerpt(cp["author"])} at {cp["timestamp"]}',f'Branch: {cp["branch"] or "unknown"} | Source commit: {cp["source_commit"] or "unknown"}',
                    'Newer/changed activity: '+str(cp['newer_activity']), 'Incorporated activity cursor: '+cp['incorporated_activity_cursor']]
    else:lines += ['No checkpoint yet; unresolved items are UNKNOWN, not zero.']
    newer=result['newer']
    if newer:
        authors=', '.join(a['text'] for a in newer['other_authors']['items']) or 'none'
        if newer['other_authors']['omitted']:authors+=f' (+{newer["other_authors"]["omitted"]} more)'
        coverage='' if newer['coverage']=='snapshot' else (' Coverage UNKNOWN (checkpoint predates per-entry digests).' if newer['coverage']=='unknown' else f' Coverage WINDOWED ({newer["unverified_count"]} older entries unverifiable; run: {newer["verify"]}).')
        lines += [f'Newer activity not in checkpoint: {newer["other_count"]} by other actors ({authors}), {newer["own_count"]} own; {newer["fresh_count"]} fresh, {newer["changed_or_late_count"]} changed/late.'+coverage]
        lines += ['  '+e['entry_id']+' ['+e['kind']+(' changed' if e['changed'] else '')+'] '+e['timestamp']+' by '+e['author']['text'] for e in newer['entries']]
        if newer['omitted']:lines += [f'  ... {newer["omitted"]} more; fresh activity: '+newer['history_new']+'; full: '+newer['history']]
        lines += ['Read newer activity: '+newer['history_new'], newer['note']]
    dirs=result['directions']
    if dirs:
        ids=', '.join(i['entry_id'] for i in dirs['items'])
        if dirs['omitted']:ids+=f' (+{dirs["omitted"]} more)'
        states={}
        for i in dirs['items']:states[i['state']]=states.get(i['state'],0)+1
        lines += [f'Outstanding directions: {dirs["total"]} [{", ".join(k+"="+str(v) for k,v in sorted(states.items()))}] '+ids,
                  '  Resolve or supersede each with note+evidence (digest-checked); acknowledgement alone does not clear a direction.']
    unresolved=result['unresolved'];lines += [f'Unresolved items: {unresolved["total"] if unresolved["total"] is not None else "unknown"}']
    lines += [f'- {item["id"]} [{item["kind"]}]: {item["text"]} (source: {item["source"]})' for item in unresolved['items']]
    if unresolved['next_offset'] is not None:lines += [f'More unresolved items: brief {result["task"]} --items-offset {unresolved["next_offset"]}']
    lines += ['Native dependencies: '+json.dumps(result['dependencies'],ensure_ascii=False), 'Lifecycle: '+', '.join(k+'='+v['value'] for k,v in result['lifecycle'].items()),
              'Lifecycle scope: '+json.dumps(result['lifecycle_scope'],ensure_ascii=False),'Current activity cursor: '+result['activity_cursor'],
              'Evidence: '+json.dumps(result['evidence'],ensure_ascii=False),*result['warnings']]
    for item in result.get('attention') or []:
        if item.get('kind')=='proposal-review':
            lines.append('Proposal review [%s, %s]: %s (%s)'%(item['state'],item['trust'],item['text'],item['source']))
        elif item.get('kind')=='capability':
            lines.append('Capability [%s, %s]: %s (%s)'%(item['verification'],item['trust'],item['text'],item['source']))
        elif item.get('kind')=='reference':
            lines.append('Reference [%s, %s]: %s (%s)'%(item['authority_kind'],item['trust'],item['text'],item['source']))
        else:
            lines.append('Reference review [%s, %s]: %s (%s)'%(item['due'],item['trust'],item['text'],item['source']))
    if result.get('attention_more'):
        lines.append('More attention: %d (ref list --due expired; proposal list; capability list)'%result['attention_more'])
    if result.get('reference_drafts_matching'):
        from reference_records import DRAFTS_SHOWN_MAX
        count=result['reference_drafts_matching']
        lines.append('Draft reference entries that match this task: %s (not accepted, not authoritative)'%('%d or more'%count if count>=DRAFTS_SHOWN_MAX else count))
    return '\n'.join(lines)+'\n'

def help_limits(action):
    """Documented limits for the machine-readable help of one briefing command."""
    if action=='brief':
        return {'items-offset':'>= %d'%BRIEF_ITEM_OFFSET_MIN,
                'items-limit':'%d..%d'%(BRIEF_ITEM_LIMIT_MIN,BRIEF_ITEM_LIMIT_MAX)}
    if action=='history':
        return {'limit':'%d..%d'%(HISTORY_LIMIT_MIN,HISTORY_LIMIT_MAX),
                'body-budget':'%d..%d encoded bytes'%(HISTORY_BUDGET_MIN,HISTORY_BUDGET_MAX)}
    limits={key:'<= %d characters'%limit for key,limit in CHECKPOINT_FIELD_LIMITS}
    limits.update({'open_items':'<= %d'%CHECKPOINT_ITEMS_MAX,'resolved':'<= %d'%CHECKPOINT_ITEMS_MAX,
            'directions/carried effective entries':'<= %d'%DIRECTIONS_MAX,
            'provenance exact digests':'<= %d'%DIGEST_WINDOW,
            'provenance older digests':'<= %d'%OLDER_MAX,
            'item text/reason':'<= %d characters'%CHECKPOINT_TEXT_LIMIT,
            'item source/evidence':'<= %d characters'%CHECKPOINT_SOURCE_LIMIT,
            'payload':'<= %d KB canonical bytes'%(CHECKPOINT_MAX_BYTES//1000),
            'error field names':'<= %d names, each <= %d characters'%(FIELD_NAME_COUNT,FIELD_NAME_LIMIT)})
    return limits

def help_notes(action):
    if action=='brief':
        return ['Unresolved items come from the latest valid checkpoint; a missing checkpoint means unknown, not zero.',
                'Every endpoint brief carries a guidance block (kittrial-5bb.99): the current coordinator '
                'guidance version, who set it and whether this actor has acknowledged it; read it with '
                '`guidance get` and record the read with `guidance ack --version VERSION`, naming the version '
                'you read. An unreadable or unbound guidance record carries attention true and a warning, and '
                'unbound text is withheld (`text: null`, `version: null`, `unbound: true`) with a next action saying the '
                'guidance is being repaired by the operator; text without a setter is never followed.',
                '--limit/--offset are not brief options; use --items-limit/--items-offset.',
                'attention lists at most 3 reference-review items (entries tagged with the task\'s labels, '
                'then expired and due-soon), expired first, then at most 3 reference items (accepted entries '
                'whose key, title or tags share at least two words with the task title; '
                'reference_drafts_matching counts the drafts that match, 9 meaning 9 or more, and shows '
                'nothing else of them), '
                'then at most 3 proposal-review items (proposals '
                'that target this requirement record or one of the task\'s area labels, then, for an operator, '
                'the oldest waiting ones), then at most 3 capability items (accepted capabilities tagged with '
                'one of the task\'s labels, drifted first), each with trust; attention_total and '
                'attention_more count every kind. Reading changes nothing.']
    if action=='history':
        return ['Pages are snapshot-bound; pass next_cursor back to continue the same snapshot.']
    return ['A JSON file attachment is required; payload.task must equal TASK.',
            'The record has exactly these fields: schema_version (1), task, previous (the current checkpoint\'s '
            'comment_id, or null for the first), activity_cursor (from brief or history), source_commit and branch '
            '(text; empty when there is none), intent, acceptance, summary, next_action, open_items (each: id, kind, '
            'text, source; kind is one of blocker, correction, decision, dependency, question) and resolved (each: '
            'id, reason, evidence).',
            'A refusal names every problem with the record at once: the sentence itself for one problem, and for '
            'more a numbered list of the same sentences ("Invalid checkpoint (N problems): [1] ... [2] ..."), at '
            'most %d of them. A stale previous and a changed activity cursor are separate refusals.'
            % CHECKPOINT_PROBLEMS_MAX,
            '--json is accepted in any position; the saved checkpoint is always returned as JSON on stdout.',
            '--provenance, --verify and --directions are read-only alternatives to --file; they change no checkpoint or direction disposition.',
            'checkpoint_provenance_writes is off by default: writers emit the older-kit shape until an operator enables new writes after a reader-first rollout.',
            'The server derives provenance deltas and carried acknowledgements; newest evidence follows linked checkpoint order.',
            'Only the current assignee may record direction dispositions; --directions returns full current digests in pages of 1..100.',
            'Every unresolved item must be carried forward unchanged or explicitly resolved with reason and evidence.']

def exported_rows(run):
    """Use the bounded JSON decoder for every briefing export read."""
    return [record_json.loads(x) for x in run(['export','--all']).splitlines() if x.strip()]

def checkpoint_writes_enabled(root):
    """Reader-first rollout: only an installation operator enables new writes."""
    from admin import config
    marker=root/'deployment.private.json'
    if marker.is_symlink():raise ValueError('Deployment configuration must not be a symlink')
    if not marker.exists():return False
    settings=config(root)
    if not isinstance(settings,dict):raise ValueError('Deployment configuration must be a JSON object')
    value=settings.get('checkpoint_provenance_writes',False)
    if type(value) is not bool:
        import sys
        print('Warning: checkpoint_provenance_writes must be a boolean; treating the malformed value as OFF.',file=sys.stderr)
        return False
    return value

def direction_page(rows,project,task,offset=0,limit=50):
    if offset<0 or not 1<=limit<=100:raise ValueError('Direction page requires --offset >= 0 and --limit 1..100')
    issue=task_row(rows,task);state=checkpoint_state(issue)
    if state['invalid']:raise ValueError('Malformed checkpoint entries require correction before reading directions')
    data=snapshot(rows,project,task);context=direction_context(issue,state)
    entries=unresolved_directions(direction_entries(data,context),chain_dispositions(state['history']),context[0])
    if offset>len(entries):raise ValueError('Direction offset exceeds total')
    return {'task':task,'activity_cursor':activity_cursor(data),'total':len(entries),
            'items':[{'id':e['entry_id'],'digest':entry_digests(data)[e['entry_id']],
                      'author':clip(e['author'],96),'timestamp':e['timestamp']} for e in entries[offset:offset+limit]],
            'next_offset':offset+limit if offset+limit<len(entries) else None,
            'coverage':'Current full digests for outstanding directions, including entries outside stored provenance windows. '
                       'Pages are fresh reads; compare activity_cursor across pages and restart if it changes.'}

def execute(root,path,project,actor,action,args,attachments,run,operators=None,verifiers=None):
    """Endpoint holds project coordination lock. History caches are disposable."""
    from work import help_payload,help_requested
    if help_requested(args):
        return json.dumps(help_payload(action),ensure_ascii=False,indent=2)+'\n'
    if action=='checkpoint':
        # Structured output is always JSON, so --json is accepted in any position
        # (consistent with work/review/handoff/brief/history). It is dropped
        # before the positional check so `checkpoint TASK --file x --json`,
        # `checkpoint --json TASK --file x` and `checkpoint TASK --json --file x`
        # all behave the same.
        args=[token for token in args if token!='--json']
        if len(args)>=2 and args[1]=='--directions':
            parser=Parser(add_help=False);parser.add_argument('task');parser.add_argument('--directions',action='store_true')
            parser.add_argument('--offset',type=int,default=0);parser.add_argument('--limit',type=int,default=50)
            page=parser.parse_args(args)
            return json.dumps(direction_page(exported_rows(run),project,page.task,page.offset,page.limit),ensure_ascii=False,indent=2)+'\n'
        if len(args)==2 and args[1]=='--provenance':
            # Supported read path: return the bounded provenance and cursor that
            # save_checkpoint accepts verbatim — no private snapshot reconstruction.
            rows=exported_rows(run)
            snap=snapshot(rows,project,args[0])
            return json.dumps({'task':args[0],'activity_cursor':activity_cursor(snap),
                               'provenance':bounded_digests(entry_digests(snap))},ensure_ascii=False,indent=2)+'\n'
        if len(args)==2 and args[1]=='--verify':
            rows=exported_rows(run)
            return json.dumps(verify_checkpoint(rows,project,args[0]),ensure_ascii=False,indent=2)+'\n'
        if len(args)!=2 or not args[1].startswith('@attachment:'):raise ValueError('Use checkpoint TASK --file checkpoint.json [--json]')
        item=attachments.get(args[1].partition(':')[2],{})
        if item.get('flag') not in ('--file','-f') or not isinstance(item.get('text'),str):raise ValueError('Checkpoint needs a JSON file attachment')
        rows=exported_rows(run)
        return json.dumps(save_checkpoint(rows,project,args[0],record_json.loads(item['text']),actor,run,
                                          provenance_writes=checkpoint_writes_enabled(root)))+'\n'
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
        rows=exported_rows(run)
        if action=='brief':
            result=brief(rows,project,a.task,a.items_offset,a.items_limit,operators=operators,journal=path,
                         verifiers=verifiers,actor=actor)
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
